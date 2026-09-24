"""Orchestrates one task: resolve repository -> clone -> code -> commit -> push."""

from __future__ import annotations

from azure.core.credentials import TokenCredential

from .config import ConfigError, Settings
from .github import GitHubPullRequests
from .harness import create_harness
from .logging_setup import get_logger
from .models import CodingTask, RepositoryRecord, TaskResult
from .repo import RepoWorkspace, build_branch_name
from .repository_registry import RepositoryRegistry

log = get_logger(__name__)


class CodingPipeline:
    def __init__(self, settings: Settings, credential: TokenCredential) -> None:
        self._settings = settings
        self._registry = RepositoryRegistry.load(settings, credential)
        if settings.push_enabled and any(
            item.repo_url.startswith("https://github.com/") for item in self._registry.records
        ) and not settings.github_token:
            raise ConfigError("GITHUB_PAT is required to push GitHub repositories")
        self._harness = create_harness(settings, credential)
        self._workspace = RepoWorkspace(settings, credential)

    def handle(self, task: CodingTask, history: list[dict[str, str]] | None = None) -> TaskResult:
        repository = self._registry.resolve(task.repository)
        is_github = repository.repo_url.startswith("https://github.com/")
        if task.merge_when_ready and not is_github:
            raise ValueError("auto merge is available only for GitHub repositories")
        base_branch = task.target_branch or repository.default_branch
        if task.direct_to_main:
            if not is_github or base_branch != "main" or task.merge_when_ready:
                raise ValueError("direct-to-main requires a GitHub repository on main without auto merge")
            return self._handle_direct_to_main(task, repository, history)
        log.info(
            "repository_resolved",
            task_id=task.task_id,
            repository=repository.key,
            base_branch=base_branch,
        )

        branch = build_branch_name(
            self._settings.branch_prefix, repository.key, task.session_id or task.task_id
        )
        branch_exists = (
            self._settings.push_enabled
            and not self._settings.dry_run
            and bool(self._workspace.ls_remote(repository.repo_url, branch))
        )
        if branch_exists and not task.session_id:
            log.info("task_branch_reused", task_id=task.task_id, branch=branch)
            return self._result_after_push(task, repository, base_branch, branch, [], "Existing task branch reused")
        repo = self._workspace.clone(repository.repo_url, branch if branch_exists else base_branch)
        if branch_exists and repo.has_task_commit(task.task_id):
            log.info("task_commit_reused", task_id=task.task_id, branch=branch)
            return self._result_after_push(
                task, repository, base_branch, branch, [], repo.task_commit_summary()
            )

        summary = self._harness.run(repo.path, task, repository, history)
        repo.assert_checkout_intact()
        changed = repo.changed_files()
        if not changed:
            log.warning("no_changes_proposed", task_id=task.task_id, repository=repository.key)
            return TaskResult(task.task_id, branch, False, [], summary)

        if not branch_exists:
            repo.create_branch(branch)
        pushed = repo.commit_and_push(branch, _commit_message(task, repository, summary), self._settings)
        log.info("task_prepared", task_id=task.task_id, branch=branch, pushed=pushed, files=changed)
        if pushed:
            return self._result_after_push(task, repository, base_branch, branch, changed, summary)
        return TaskResult(task.task_id, branch, False, changed, summary)

    def _handle_direct_to_main(
        self, task: CodingTask, repository: RepositoryRecord, history: list[dict[str, str]] | None
    ) -> TaskResult:
        repo = self._workspace.clone(repository.repo_url, "main")
        prior_summary = repo.recent_task_commit_summary(task.task_id)
        if prior_summary is not None:
            log.info("task_commit_reused", task_id=task.task_id, branch="main")
            return TaskResult(task.task_id, "main", True, [], prior_summary)
        summary = self._harness.run(repo.path, task, repository, history)
        repo.assert_checkout_intact()
        changed = repo.changed_files()
        if not changed:
            log.warning("no_changes_proposed", task_id=task.task_id, repository=repository.key)
            return TaskResult(task.task_id, "main", False, [], summary)
        pushed = repo.commit_and_push("main", _commit_message(task, repository, summary), self._settings)
        log.info("task_prepared", task_id=task.task_id, branch="main", pushed=pushed, files=changed)
        return TaskResult(task.task_id, "main", pushed, changed, summary)

    def _result_after_push(
        self,
        task: CodingTask,
        repository: RepositoryRecord,
        base_branch: str,
        branch: str,
        changed: list[str],
        summary: str,
    ) -> TaskResult:
        if not repository.repo_url.startswith("https://github.com/"):
            return TaskResult(task.task_id, branch, True, changed, summary)

        title = task.instruction.splitlines()[0].strip()[:120] or "Complete coding task"
        body = f"Automated coding task `{task.task_id}`.\n\n{summary[:6000]}"
        pull = GitHubPullRequests(self._settings.github_token or "").ensure(
            repository.repo_url,
            branch,
            base_branch,
            title,
            body,
            task.merge_when_ready,
            repository.github_merge_method,
        )
        log.info("pull_request_ready", task_id=task.task_id, url=pull.url)
        return TaskResult(
            task.task_id,
            branch,
            True,
            changed,
            summary,
            pull_request_url=pull.url,
            auto_merge_enabled=pull.auto_merge_enabled,
        )

    def close(self) -> None:
        self._workspace.cleanup()


def _commit_message(task: CodingTask, repository: RepositoryRecord, summary: str) -> str:
    headline = task.instruction.splitlines()[0].strip()[:72] or "complete coding task"
    body = summary.strip()[:3000]
    return (
        f"feat: {headline}\n\n"
        f"Automated change proposed by the coding agent.\n"
        f"Task: {task.task_id}\n"
        f"Repository: {repository.name}\n\n"
        f"{body}\n"
    )
