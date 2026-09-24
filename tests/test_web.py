from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

from fastapi.testclient import TestClient

from aiops_agent.models import CodingTask, RepositoryRecord
from aiops_agent.repository_registry import RepositoryRegistry
from aiops_agent.session import SessionConflictError
from aiops_agent.web import TaskRequest, app, create_task, get_task, list_repositories


class FakeQueue:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.fail = False

    def send_message(self, body: str) -> None:
        if self.fail:
            raise RuntimeError("queue unavailable")
        self.messages.append(body)


class FakeStatusStore:
    def __init__(self) -> None:
        self.statuses: dict[str, dict[str, object]] = {}

    def write(self, task_id: str, **fields: object) -> None:
        self.statuses[task_id] = {"task_id": task_id, **fields}

    def read(self, task_id: str) -> dict[str, object]:
        return self.statuses[task_id]

    def read_for_owner(self, task_id: str, owner_id: str) -> dict[str, object]:
        status = self.read(task_id)
        if status.get("owner_id") != owner_id:
            raise ValueError("task not found")
        return status


class FakeSessionStore:
    def __init__(self) -> None:
        self.sessions: dict[str, dict[str, object]] = {}

    def create(self, repository: str, target_branch: str | None, owner_id: str) -> dict[str, object]:
        session = {
            "session_id": str(uuid4()),
            "repository": repository,
            "owner_id": owner_id,
            "target_branch": target_branch,
            "messages": [],
            "active_task_id": None,
            "state": "idle",
        }
        self.sessions[session["session_id"]] = session
        return session

    def read(self, session_id: str) -> dict[str, object]:
        return self.sessions[session_id]

    def read_for_owner(self, session_id: str, owner_id: str) -> dict[str, object]:
        session = self.read(session_id)
        if session["owner_id"] != owner_id:
            raise ValueError("session not found")
        return session

    def list_for_owner(self, owner_id: str) -> list[dict[str, object]]:
        return [session for session in self.sessions.values() if session["owner_id"] == owner_id]

    def queue_turn(self, session_id: str, instruction: str, owner_id: str) -> CodingTask:
        session = self.sessions[session_id]
        if session["owner_id"] != owner_id:
            raise ValueError("session not found")
        if session["active_task_id"]:
            raise SessionConflictError("busy")
        task = CodingTask(
            str(uuid4()),
            session["repository"],
            instruction,
            datetime.now(UTC),
            target_branch=session["target_branch"],
            session_id=session_id,
            owner_id=owner_id,
        )
        session["active_task_id"] = task.task_id
        session["messages"].append({"role": "user", "content": instruction})
        return task

    def fail_turn(self, task: CodingTask, message: str) -> None:
        self.sessions[task.session_id]["active_task_id"] = None
        self.sessions[task.session_id]["state"] = "failed"


def _configure_app() -> FakeQueue:
    queue = FakeQueue()
    app.state.registry = RepositoryRegistry(
        {
            "api": RepositoryRecord(
                key="api",
                name="API",
                repo_url="https://github.com/acme/api.git",
                default_branch="main",
            )
        }
    )
    app.state.queue = queue
    app.state.status_store = FakeStatusStore()
    app.state.session_store = FakeSessionStore()
    return queue


def test_frontend_lists_safe_repository_metadata():
    _configure_app()
    assert list_repositories("user-a") == [
        {
            "id": "api",
            "name": "API",
            "default_branch": "main",
            "source_path": ".",
            "notes": "",
            "supports_auto_merge": True,
        }
    ]


def test_frontend_queues_canonical_task():
    queue = _configure_app()
    response = create_task(TaskRequest(repository="api", instruction="Add a health endpoint"), "user-a")
    payload = json.loads(queue.messages[0])
    assert response["status"] == "queued"
    assert payload["task_id"] == response["task_id"]
    assert payload["repository"] == "api"
    assert payload["instruction"] == "Add a health endpoint"
    assert get_task(response["task_id"], "user-a")["state"] == "queued"


def test_frontend_queues_auto_merge_choice():
    queue = _configure_app()
    create_task(TaskRequest(repository="api", instruction="Add a health endpoint", merge_when_ready=True), "user-a")
    assert json.loads(queue.messages[0])["merge_when_ready"] is True


def test_http_task_roundtrip():
    _configure_app()
    client = TestClient(app, headers={"x-ms-client-principal-id": "user-a"})
    created = client.post("/api/tasks", json={"repository": "api", "instruction": "Add a health endpoint"})
    assert created.status_code == 202
    task_id = created.json()["task_id"]
    status = client.get(f"/api/tasks/{task_id}")
    assert status.status_code == 200
    assert status.json()["state"] == "queued"


def test_http_sessions_keep_separate_chats_and_queue_turns():
    queue = _configure_app()
    client = TestClient(app, headers={"x-ms-client-principal-id": "user-a"})
    first = client.post("/api/sessions", json={"repository": "api"}).json()["session_id"]
    second = client.post("/api/sessions", json={"repository": "api"}).json()["session_id"]
    sent = client.post(f"/api/sessions/{first}/messages", json={"instruction": "Add a greeting"})
    assert sent.status_code == 202
    assert json.loads(queue.messages[0])["session_id"] == first
    assert json.loads(queue.messages[0])["target_branch"] == "main"
    assert client.get(f"/api/sessions/{first}").json()["messages"][0]["content"] == "Add a greeting"
    assert client.get(f"/api/sessions/{second}").json()["messages"] == []
    assert client.post(f"/api/sessions/{first}/messages", json={"instruction": "Second"}).status_code == 409


def test_session_recovers_if_queue_send_fails():
    queue = _configure_app()
    client = TestClient(app, headers={"x-ms-client-principal-id": "user-a"})
    session_id = client.post("/api/sessions", json={"repository": "api"}).json()["session_id"]
    queue.fail = True
    response = client.post(f"/api/sessions/{session_id}/messages", json={"instruction": "Do it"})
    assert response.status_code == 503
    state = client.get(f"/api/sessions/{session_id}").json()
    assert state["active_task_id"] is None
    assert state["state"] == "failed"


def test_api_requires_identity_and_blocks_cross_user_access():
    _configure_app()
    anonymous = TestClient(app)
    assert anonymous.get("/api/repositories").status_code == 401
    assert anonymous.post("/api/sessions", json={"repository": "api"}).status_code == 401
    alice = TestClient(app, headers={"x-ms-client-principal-id": "alice"})
    bob = TestClient(app, headers={"x-ms-client-principal-id": "bob"})
    session_id = alice.post("/api/sessions", json={"repository": "api"}).json()["session_id"]
    assert [item["session_id"] for item in alice.get("/api/sessions").json()] == [session_id]
    assert bob.get("/api/sessions").json() == []
    assert bob.get(f"/api/sessions/{session_id}").status_code == 404
    assert bob.post(f"/api/sessions/{session_id}/messages", json={"instruction": "Change"}).status_code == 404
    task_id = alice.post("/api/tasks", json={"repository": "api", "instruction": "Change"}).json()["task_id"]
    assert bob.get(f"/api/tasks/{task_id}").status_code == 404
