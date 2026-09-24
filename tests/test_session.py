from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from azure.core.exceptions import ResourceModifiedError
from azure.storage.blob import ContainerClient

from aiops_agent.__main__ import _process
from aiops_agent.models import CodingTask, MessageFormatError, TaskResult
from aiops_agent.session import SessionConflictError, SessionStore, TurnAlreadyCompleted


class FakeDownload:
    def __init__(self, data: str, etag: str) -> None:
        self._data = data
        self.properties = SimpleNamespace(etag=etag)

    def readall(self) -> bytes:
        return self._data.encode("utf-8")


class FakeBlob:
    def __init__(self) -> None:
        self.data: str | None = None
        self.version = 0
        self.conflict_once = False
        self.metadata = {}

    def upload_blob(self, data, *, overwrite, etag=None, match_condition=None,
                    content_settings=None, metadata=None):
        if self.conflict_once:
            self.conflict_once = False
            raise ResourceModifiedError("Simulated competing writer")
        if overwrite and etag != str(self.version):
            raise ResourceModifiedError("ETag mismatch")
        if not overwrite and self.data is not None:
            raise ResourceModifiedError("Already exists")
        self.data = data
        self.metadata = metadata or {}
        self.version += 1

    def download_blob(self) -> FakeDownload:
        if self.data is None:
            raise FileNotFoundError
        return FakeDownload(self.data, str(self.version))


class FakeContainer:
    def __init__(self) -> None:
        self.blobs: dict[str, FakeBlob] = {}

    def get_blob_client(self, name: str) -> FakeBlob:
        return self.blobs.setdefault(name, FakeBlob())

    def list_blobs(self, name_starts_with: str, include=None):
        return [SimpleNamespace(name=name, metadata=blob.metadata) for name, blob in self.blobs.items()
                if name.startswith(name_starts_with) and blob.data is not None]

    def close(self) -> None:
        pass


@pytest.fixture
def store(monkeypatch) -> SessionStore:
    container = FakeContainer()
    monkeypatch.setattr(ContainerClient, "from_container_url", lambda url, credential: container)
    return SessionStore("https://example.blob.core.windows.net/status", object())


def test_turns_share_branch_context_but_separate_sessions_do_not(store: SessionStore):
    first_session = store.create("api")
    other_session = store.create("api")
    first_id = first_session["session_id"]

    first_task = store.queue_turn(first_id, "Add a greeting")
    assert first_task.session_id == first_id
    assert store.start_turn(first_task) == []
    result = TaskResult(first_task.task_id, "agent/task/api-branch", True, ["app.py"], "Added greeting")
    store.complete_turn(first_task, result)

    second_task = store.queue_turn(first_id, "Now add tests")
    history = store.start_turn(second_task)
    assert history == [
        {"role": "user", "content": "Add a greeting"},
        {"role": "assistant", "content": "Added greeting"},
    ]
    assert store.read(first_id)["branch"] == "agent/task/api-branch"
    assert store.read(other_session["session_id"])["messages"] == []
    assert store.read(other_session["session_id"])["branch"] is None


def test_only_one_active_turn_per_session(store: SessionStore):
    session_id = store.create("api")["session_id"]
    task = store.queue_turn(session_id, "First")
    with pytest.raises(SessionConflictError, match="already working"):
        store.queue_turn(session_id, "Second")
    store.fail_turn(task)
    assert store.read(session_id)["state"] == "failed"
    assert store.queue_turn(session_id, "Try again").instruction == "Try again"


def test_direct_to_main_mode_is_immutable_and_checked_for_queued_turns(store: SessionStore):
    session_id = store.create("api", "main", owner_id="alice", direct_to_main=True)["session_id"]
    task = store.queue_turn(session_id, "Ship it", owner_id="alice")
    assert task.direct_to_main is True
    assert store.start_turn(task) == []
    altered = CodingTask(
        task.task_id, task.repository, task.instruction, task.created_at,
        target_branch="main", session_id=session_id, owner_id="alice", direct_to_main=False,
    )
    with pytest.raises(SessionConflictError, match="delivery mode"):
        store.start_turn(altered)


def test_owner_scoped_sessions_are_discoverable_and_private(store: SessionStore):
    alice_id = store.create("api", "main", owner_id="alice")["session_id"]
    store.create("api", "main", owner_id="bob")
    assert [item["session_id"] for item in store.list_for_owner("alice")] == [alice_id]
    assert store.list_for_owner("charlie") == []
    with pytest.raises((ValueError, FileNotFoundError)):
        store.read_for_owner(alice_id, "bob")
    with pytest.raises((ValueError, FileNotFoundError)):
        store.queue_turn(alice_id, "Cross-user change", owner_id="bob")


def test_session_update_retries_an_etag_conflict(store: SessionStore):
    session_id = store.create("api")["session_id"]
    store._blob(session_id).conflict_once = True
    task = store.queue_turn(session_id, "Try once")
    assert store.read(session_id)["active_task_id"] == task.task_id


def test_completed_turn_cannot_be_replayed(store: SessionStore):
    session_id = store.create("api")["session_id"]
    task = store.queue_turn(session_id, "Do it")
    store.complete_turn(task, TaskResult(task.task_id, "branch", False, [], "Already done"))
    with pytest.raises(TurnAlreadyCompleted):
        store.start_turn(task)


def test_dry_run_changes_are_marked_as_not_persisted(store: SessionStore):
    session_id = store.create("api")["session_id"]
    task = store.queue_turn(session_id, "Change it")
    store.complete_turn(task, TaskResult(task.task_id, "branch", False, ["app.py"], "Changed app.py"))
    session = store.read(session_id)
    assert session["state"] == "unpersisted"
    assert "later turns will not see" in session["messages"][-1]["content"]


def test_task_parser_accepts_session_id_and_rejects_bad_id():
    session_id = "240cb99a-0287-4fa1-a296-d976dd0c24bc"
    task = CodingTask.parse(
        json.dumps(
            {
                "task_id": "53c8bc3d-c6e9-4346-8835-367169989aad",
                "session_id": session_id,
                "repository": "api",
                "instruction": "Hello",
                "created_at": datetime.now(UTC).isoformat(),
            }
        )
    )
    assert task.session_id == session_id
    with pytest.raises(MessageFormatError, match="session_id"):
        CodingTask.parse(
            '{"task_id":"53c8bc3d-c6e9-4346-8835-367169989aad",'
            '"session_id":"not-a-uuid","repository":"api","instruction":"Hello"}'
        )


def test_worker_turn_roundtrip_uses_persisted_history(store: SessionStore):
    session_id = store.create("api", "main")["session_id"]
    first = store.queue_turn(session_id, "First")
    store.complete_turn(first, TaskResult(first.task_id, "session-branch", True, ["app.py"], "First done"))
    second = store.queue_turn(session_id, "Second")

    class Message:
        body = json.dumps(
            {
                "task_id": second.task_id,
                "session_id": second.session_id,
                "repository": second.repository,
                "instruction": second.instruction,
                "target_branch": second.target_branch,
            }
        )
        dequeue_count = 1
        completed = False

        def complete(self):
            self.completed = True

        def abandon(self):
            raise AssertionError("unexpected retry")

    class Pipeline:
        history = None

        def handle(self, task, history):
            self.history = history
            return TaskResult(task.task_id, "session-branch", True, ["tests.py"], "Second done")

    class Status:
        def write(self, task_id, **fields):
            pass

    message = Message()
    pipeline = Pipeline()
    assert _process(message, pipeline, Status(), 3, store)
    assert message.completed
    assert pipeline.history == [
        {"role": "user", "content": "First"},
        {"role": "assistant", "content": "First done"},
    ]
    assert [item["content"] for item in store.read(session_id)["messages"]] == [
        "First", "First done", "Second", "Second done"
    ]
