from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

from fastapi.testclient import TestClient

from aiops_agent.job_control import JobStopError
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

    def create(
        self, repository: str, target_branch: str | None, owner_id: str, direct_to_main: bool = False
    ) -> dict[str, object]:
        session = {
            "session_id": str(uuid4()),
            "repository": repository,
            "owner_id": owner_id,
            "target_branch": target_branch,
            "direct_to_main": direct_to_main,
            "messages": [],
            "active_task_id": None,
            "active_execution_name": None,
            "cancelled_execution_name": None,
            "state": "idle",
            "updated_at": datetime.now(UTC).isoformat(),
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
        if session["cancelled_execution_name"]:
            raise SessionConflictError("the cancelled worker is still being stopped")
        task = CodingTask(
            str(uuid4()),
            session["repository"],
            instruction,
            datetime.now(UTC),
            target_branch=session["target_branch"],
            direct_to_main=session["direct_to_main"],
            session_id=session_id,
            owner_id=owner_id,
        )
        session["active_task_id"] = task.task_id
        session["messages"].append({"role": "user", "content": instruction})
        return task

    def fail_turn(self, task: CodingTask, message: str) -> None:
        self.sessions[task.session_id]["active_task_id"] = None
        self.sessions[task.session_id]["state"] = "failed"

    def activate_direct_to_main(self, session_id: str, owner_id: str) -> dict[str, object]:
        session = self.read_for_owner(session_id, owner_id)
        if session["active_task_id"]:
            raise SessionConflictError("finish the current turn before changing delivery mode")
        session["direct_to_main"] = True
        session["branch"] = "main"
        return session

    def cancel_turn(self, session_id: str, task_id: str, owner_id: str) -> dict[str, object]:
        session = self.read_for_owner(session_id, owner_id)
        if session["state"] == "cancelled" and session.get("last_task_id") == task_id:
            return session
        if session["active_task_id"] != task_id:
            raise SessionConflictError("this run is no longer active")
        session["cancelled_execution_name"] = session["active_execution_name"]
        session["active_execution_name"] = None
        session["active_task_id"] = None
        session["last_task_id"] = task_id
        session["state"] = "cancelled"
        return session

    def acknowledge_execution_stop(self, session_id: str, task_id: str, owner_id: str) -> None:
        self.read_for_owner(session_id, owner_id)["cancelled_execution_name"] = None


class FakeJobStopper:
    def __init__(self) -> None:
        self.stopped: list[str] = []

    def stop_execution(self, execution_name: str) -> None:
        self.stopped.append(execution_name)


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
    app.state.job_stopper = FakeJobStopper()
    app.state.default_direct_to_main = False
    return queue


def test_frontend_offers_persistent_theme_toggle():
    response = TestClient(app).get("/")
    assert response.status_code == 200
    html = response.text
    assert '<button id="theme-toggle" type="button">Switch to light theme</button>' in html
    assert ':root[data-theme="light"]' in html
    assert 'color-scheme: light' in html
    assert "localStorage.getItem('coding-agent-theme')" in html
    assert "localStorage.setItem('coding-agent-theme', theme)" in html
    assert "themeToggle.addEventListener('click'" in html


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


def test_frontend_queues_explicit_direct_to_main_choice():
    queue = _configure_app()
    client = TestClient(app, headers={"x-ms-client-principal-id": "user-a"})
    created = client.post("/api/sessions", json={"repository": "api", "direct_to_main": True})
    assert created.status_code == 201
    session_id = created.json()["session_id"]
    assert created.json()["direct_to_main"] is True
    sent = client.post(f"/api/sessions/{session_id}/messages", json={"instruction": "Do it"})
    assert sent.status_code == 202
    assert json.loads(queue.messages[0])["direct_to_main"] is True
    assert client.post("/api/tasks", json={
        "repository": "api", "instruction": "Do it", "direct_to_main": True,
    }).status_code == 202
    assert json.loads(queue.messages[1])["direct_to_main"] is True


def test_direct_to_main_rejects_non_main_and_auto_merge():
    _configure_app()
    client = TestClient(app, headers={"x-ms-client-principal-id": "user-a"})
    assert client.post("/api/sessions", json={
        "repository": "api", "target_branch": "develop", "direct_to_main": True,
    }).status_code == 400
    assert client.post("/api/tasks", json={
        "repository": "api", "instruction": "Do it", "direct_to_main": True, "merge_when_ready": True,
    }).status_code == 400


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
    assert bob.post(f"/api/sessions/{session_id}/direct-to-main").status_code == 404
    assert bob.post(f"/api/sessions/{session_id}/cancel", json={"task_id": str(uuid4())}).status_code == 404
    assert bob.post(f"/api/sessions/{session_id}/messages", json={"instruction": "Change"}).status_code == 404
    task_id = alice.post("/api/tasks", json={"repository": "api", "instruction": "Change"}).json()["task_id"]
    assert bob.get(f"/api/tasks/{task_id}").status_code == 404


def test_existing_session_can_switch_to_main_only_when_idle():
    _configure_app()
    client = TestClient(app, headers={"x-ms-client-principal-id": "alice"})
    session_id = client.post("/api/sessions", json={"repository": "api"}).json()["session_id"]
    sent = client.post(f"/api/sessions/{session_id}/messages", json={"instruction": "Do it"})
    assert sent.status_code == 202
    assert client.post(f"/api/sessions/{session_id}/direct-to-main").status_code == 409
    app.state.session_store.sessions[session_id]["active_task_id"] = None
    switched = client.post(f"/api/sessions/{session_id}/direct-to-main")
    assert switched.status_code == 200
    assert switched.json()["direct_to_main"] is True


def test_cancel_stops_only_owned_active_execution():
    _configure_app()
    alice = TestClient(app, headers={"x-ms-client-principal-id": "alice"})
    bob = TestClient(app, headers={"x-ms-client-principal-id": "bob"})
    session_id = alice.post("/api/sessions", json={"repository": "api"}).json()["session_id"]
    task_id = alice.post(f"/api/sessions/{session_id}/messages", json={"instruction": "Do it"}).json()["task_id"]
    app.state.session_store.sessions[session_id]["active_execution_name"] = "cloud-agent-worker-abc123"
    assert bob.post(f"/api/sessions/{session_id}/cancel", json={"task_id": task_id}).status_code == 404
    assert alice.post(f"/api/sessions/{session_id}/cancel", json={"task_id": str(uuid4())}).status_code == 409
    assert app.state.job_stopper.stopped == []
    cancelled = alice.post(f"/api/sessions/{session_id}/cancel", json={"task_id": task_id})
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert app.state.job_stopper.stopped == ["cloud-agent-worker-abc123"]
    assert alice.get(f"/api/tasks/{task_id}").json()["state"] == "cancelled"
    assert alice.get(f"/api/sessions/{session_id}").json()["state"] == "cancelled"


def test_failed_azure_stop_can_be_retried_without_accepting_a_new_turn():
    _configure_app()
    client = TestClient(app, headers={"x-ms-client-principal-id": "alice"})
    session_id = client.post("/api/sessions", json={"repository": "api"}).json()["session_id"]
    task_id = client.post(f"/api/sessions/{session_id}/messages", json={"instruction": "Do it"}).json()["task_id"]
    app.state.session_store.sessions[session_id]["active_execution_name"] = "cloud-agent-worker-abc123"

    def fails_once(execution_name):
        app.state.job_stopper.stop_execution = lambda name: app.state.job_stopper.stopped.append(name)
        raise JobStopError("unavailable")

    app.state.job_stopper.stop_execution = fails_once
    assert client.post(f"/api/sessions/{session_id}/cancel", json={"task_id": task_id}).status_code == 503
    assert client.post(f"/api/sessions/{session_id}/messages", json={"instruction": "Another"}).status_code == 409
    assert client.post(f"/api/sessions/{session_id}/cancel", json={"task_id": task_id}).status_code == 200
    assert app.state.job_stopper.stopped == ["cloud-agent-worker-abc123"]


def test_frontend_shows_session_dates_and_cancel_button():
    _configure_app()
    html = TestClient(app).get("/").text
    assert "sessionIds.sort((a, b) => Date.parse(sessionDates[b]" in html
    assert "Updated ${formatDate(session.updated_at)}" in html
    assert "Stop run now" in html
