from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from aiops_agent.config import Settings
from aiops_agent.models import CodingTask, RepositoryRecord
from aiops_agent.pipeline import CodingPipeline
from aiops_agent.repository_registry import RepositoryRegistry


class FakeRepo:
    path = Path(".")

    def __init__(self) -> None:
        self.branch_created = False

    def changed_files(self) -> list[str]:
        return []

    def assert_checkout_intact(self) -> None:
        pass

    def create_branch(self, branch: str) -> None:
        self.branch_created = True


class FakeWorkspace:
    def __init__(self) -> None:
        self.repo = FakeRepo()

    def clone(self, repo_url: str, base_branch: str) -> FakeRepo:
        return self.repo


class FakeHarness:
    def run(
        self,
        repo_path: Path,
        task: CodingTask,
        repository: RepositoryRecord,
        history: list[dict[str, str]] | None = None,
    ) -> str:
        return "The requested change is already present."


def test_no_changes_creates_no_branch():
    settings = Settings.from_env(
        {
            "QUEUE_ACCOUNT_URL": "https://example.queue.core.windows.net",
            "GITHUB_REPOSITORIES": "acme/api",
            "QUEUE_NAME": "tasks",
            "TASK_STATUS_CONTAINER_URL": "https://example.blob.core.windows.net/status",
            "PUSH_ENABLED": "false",
        }
    )
    repository = RepositoryRecord("api", "API", "https://github.com/acme/api.git")
    pipeline = CodingPipeline.__new__(CodingPipeline)
    pipeline._settings = settings
    pipeline._registry = RepositoryRegistry({"api": repository})
    pipeline._workspace = FakeWorkspace()
    pipeline._harness = FakeHarness()
    task = CodingTask.parse(
        '{"task_id":"240cb99a-0287-4fa1-a296-d976dd0c24bc",'
        '"repository":"api","instruction":"Add pagination"}'
    )

    result = pipeline.handle(task)

    assert result.files_changed == []
    assert not result.pushed
    assert not pipeline._workspace.repo.branch_created


class ChangingRepo(FakeRepo):
    def __init__(self) -> None:
        super().__init__()
        self.committed = False

    def changed_files(self) -> list[str]:
        return ["app.py"]

    def has_task_commit(self, task_id: str) -> bool:
        return self.committed

    def task_commit_summary(self) -> str:
        return "Recovered the previous answer"

    def commit_and_push(self, branch: str, message: str, settings: Settings) -> bool:
        assert f"Task: {self.expected_task_id}" in message
        return True


class SessionWorkspace:
    def __init__(self) -> None:
        self.remote_exists = False
        self.cloned_branches: list[str] = []
        self.repos: list[ChangingRepo] = []

    def ls_remote(self, repo_url: str, branch: str) -> list[str]:
        return [branch] if self.remote_exists else []

    def clone(self, repo_url: str, base_branch: str) -> ChangingRepo:
        self.cloned_branches.append(base_branch)
        repo = ChangingRepo()
        self.repos.append(repo)
        return repo


class RecordingHarness:
    def __init__(self) -> None:
        self.histories = []

    def run(self, repo_path, task, repository, history=None):
        self.histories.append(history)
        return "Implemented the request"


def test_followup_turn_reuses_session_branch_and_prior_context():
    settings = Settings.from_env(
        {
            "QUEUE_ACCOUNT_URL": "https://example.queue.core.windows.net",
            "GITHUB_REPOSITORIES": "acme/api",
            "QUEUE_NAME": "tasks",
            "TASK_STATUS_CONTAINER_URL": "https://example.blob.core.windows.net/status",
        }
    )
    repository = RepositoryRecord("api", "API", "https://dev.azure.com/acme/project/_git/api")
    pipeline = CodingPipeline.__new__(CodingPipeline)
    pipeline._settings = settings
    pipeline._registry = RepositoryRegistry({"api": repository})
    pipeline._workspace = SessionWorkspace()
    pipeline._harness = RecordingHarness()
    session_id = "53c8bc3d-c6e9-4346-8835-367169989aad"
    first = CodingTask("240cb99a-0287-4fa1-a296-d976dd0c24bc", "api", "First", datetime.now(UTC),
                       session_id=session_id)
    second = CodingTask("af8bce67-378d-48af-bd30-829ca3b17212", "api", "Second", datetime.now(UTC),
                        session_id=session_id)

    pipeline._workspace.repos.clear()
    original_clone = pipeline._workspace.clone

    def clone_with_task(repo_url, base_branch):
        repo = original_clone(repo_url, base_branch)
        repo.expected_task_id = first.task_id if not pipeline._workspace.remote_exists else second.task_id
        return repo

    pipeline._workspace.clone = clone_with_task
    first_result = pipeline.handle(first)
    pipeline._workspace.remote_exists = True
    second_result = pipeline.handle(second, [{"role": "user", "content": "First"}])

    assert first_result.branch == second_result.branch
    assert pipeline._workspace.cloned_branches == ["main", first_result.branch]
    assert pipeline._workspace.repos[0].branch_created
    assert not pipeline._workspace.repos[1].branch_created
    assert pipeline._harness.histories[-1] == [{"role": "user", "content": "First"}]

    replay_repo = ChangingRepo()
    replay_repo.committed = True
    pipeline._workspace.clone = lambda repo_url, base_branch: replay_repo
    replay = pipeline.handle(second)
    assert replay.harness_summary == "Recovered the previous answer"
    assert len(pipeline._harness.histories) == 2
