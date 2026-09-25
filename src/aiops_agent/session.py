"""Durable conversations shared by the frontend and disposable coding jobs."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

from azure.core import MatchConditions
from azure.core.credentials import TokenCredential
from azure.core.exceptions import ResourceModifiedError
from azure.storage.blob import ContainerClient, ContentSettings

from .models import CodingTask, TaskResult


class SessionConflictError(RuntimeError):
    """The session already has an active turn or changed concurrently."""


class TurnAlreadyCompleted(RuntimeError):
    """A duplicate queue delivery for a turn already recorded in the session."""


class SessionStore:
    def __init__(self, container_url: str, credential: TokenCredential) -> None:
        self._client = ContainerClient.from_container_url(container_url, credential=credential)

    def create(
        self, repository: str, target_branch: str | None = None, owner_id: str | None = None,
        direct_to_main: bool = False,
    ) -> dict[str, Any]:
        now = _now()
        session = {
            "session_id": str(uuid4()),
            "repository": repository,
            "owner_id": owner_id,
            "target_branch": target_branch,
            "direct_to_main": direct_to_main,
            "created_at": now,
            "updated_at": now,
            "state": "idle",
            "active_task_id": None,
            "active_execution_name": None,
            "last_task_id": None,
            "branch": None,
            "pull_request_url": None,
            "messages": [],
        }
        self._blob(session["session_id"], owner_id).upload_blob(
            json.dumps(session),
            overwrite=False,
            content_settings=ContentSettings(content_type="application/json"),
            metadata=_summary_metadata(session),
        )
        return session

    def read(self, session_id: str, owner_id: str | None = None) -> dict[str, Any]:
        return self._read_with_etag(session_id, owner_id)[0]

    def read_for_owner(self, session_id: str, owner_id: str) -> dict[str, Any]:
        session = self.read(session_id, owner_id)
        if session.get("owner_id") != owner_id:
            raise ValueError("session not found")
        return session

    def activate_direct_to_main(self, session_id: str, owner_id: str) -> dict[str, Any]:
        """Move an idle, owner-controlled session to main for all future turns."""

        def mutate(session: dict[str, Any]) -> None:
            if session.get("owner_id") != owner_id:
                raise ValueError("session not found")
            if session.get("target_branch") != "main":
                raise ValueError("direct-to-main requires the main base branch")
            if session.get("active_task_id") or session.get("state") == "unpersisted":
                raise SessionConflictError("finish the current turn before changing delivery mode")
            session["direct_to_main"] = True
            session["branch"] = "main"
            session["pull_request_url"] = None

        return self._update(session_id, mutate, owner_id)

    def cancel_turn(self, session_id: str, task_id: str, owner_id: str) -> dict[str, Any]:
        """Cancel only the owner's currently active turn; retries become no-ops."""

        def mutate(session: dict[str, Any]) -> None:
            if session.get("owner_id") != owner_id:
                raise ValueError("session not found")
            if session.get("state") == "cancelled" and session.get("last_task_id") == task_id:
                return
            if session.get("active_task_id") != task_id:
                raise SessionConflictError("this run is no longer active")
            session["cancelled_execution_name"] = session.get("active_execution_name")
            session["active_execution_name"] = None
            session["active_task_id"] = None
            session["last_task_id"] = task_id
            session["state"] = "cancelled"
            session["messages"].append({
                "role": "assistant", "content": "Run cancelled by the user.",
                "task_id": task_id, "created_at": _now(),
            })

        return self._update(session_id, mutate, owner_id)

    def acknowledge_execution_stop(self, session_id: str, task_id: str, owner_id: str) -> None:
        def mutate(session: dict[str, Any]) -> None:
            if session.get("owner_id") != owner_id:
                raise ValueError("session not found")
            if session.get("state") != "cancelled" or session.get("last_task_id") != task_id:
                raise SessionConflictError("this run is no longer cancelled")
            session["cancelled_execution_name"] = None

        self._update(session_id, mutate, owner_id)

    def ensure_turn_active(self, task: CodingTask) -> None:
        if not task.session_id:
            return
        session = self.read(task.session_id, task.owner_id)
        if session.get("owner_id") != task.owner_id:
            raise SessionConflictError("task does not match its session owner")
        _check_active_turn(session, task.task_id)

    def list_for_owner(self, owner_id: str) -> list[dict[str, Any]]:
        prefix = f"sessions/{_owner_bucket(owner_id)}/"
        result = []
        for item in self._client.list_blobs(name_starts_with=prefix, include=["metadata"]):
            session_id = item.name.removeprefix(prefix).removesuffix(".json")
            metadata = item.metadata or {}
            if all(key in metadata for key in ("repository", "state", "updated_at")):
                result.append({"session_id": session_id, **metadata})
                continue
            try:
                session = self.read_for_owner(session_id, owner_id)
            except ValueError:
                continue
            result.append({
                "session_id": session["session_id"],
                "repository": session["repository"],
                "state": session["state"],
                "updated_at": session["updated_at"],
            })
        return sorted(result, key=lambda item: item["updated_at"], reverse=True)

    def queue_turn(self, session_id: str, instruction: str, owner_id: str | None = None) -> CodingTask:
        task_id = str(uuid4())
        created_at = datetime.now(UTC)

        def mutate(session: dict[str, Any]) -> None:
            if owner_id is not None and session.get("owner_id") != owner_id:
                raise ValueError("session not found")
            if session["active_task_id"]:
                raise SessionConflictError("this session is already working on a message")
            if session.get("cancelled_execution_name"):
                raise SessionConflictError("the cancelled worker is still being stopped")
            session["messages"].append(
                {
                    "role": "user",
                    "content": instruction,
                    "task_id": task_id,
                    "created_at": created_at.isoformat(),
                }
            )
            session["active_task_id"] = task_id
            session["active_execution_name"] = None
            session["cancelled_execution_name"] = None
            session["state"] = "queued"

        session = self._update(session_id, mutate, owner_id)
        return CodingTask(
            task_id=task_id,
            session_id=session_id,
            repository=session["repository"],
            instruction=instruction,
            created_at=created_at,
            target_branch=session["target_branch"],
            direct_to_main=session.get("direct_to_main", False),
            owner_id=session.get("owner_id"),
        )

    def start_turn(self, task: CodingTask, execution_name: str | None = None) -> list[dict[str, str]]:
        if not task.session_id:
            return []

        def mutate(session: dict[str, Any]) -> None:
            _check_active_turn(session, task.task_id)
            if session.get("owner_id") != task.owner_id:
                raise SessionConflictError("task does not match its session owner")
            if session["repository"] != task.repository or session["target_branch"] != task.target_branch:
                raise SessionConflictError("task does not match its session repository and base branch")
            if session.get("direct_to_main", False) != task.direct_to_main:
                raise SessionConflictError("task does not match its session delivery mode")
            matching_message = next(
                (item for item in session["messages"] if item["task_id"] == task.task_id), None
            )
            if matching_message is None or matching_message["content"] != task.instruction:
                raise SessionConflictError("task instruction does not match its session message")
            session["state"] = "running"
            session["active_execution_name"] = execution_name

        session = self._update(task.session_id, mutate, task.owner_id)
        return [
            {"role": item["role"], "content": item["content"]}
            for item in session["messages"]
            if item["task_id"] != task.task_id
        ]

    def complete_turn(self, task: CodingTask, result: TaskResult) -> None:
        if not task.session_id:
            return

        def mutate(session: dict[str, Any]) -> None:
            _check_active_turn(session, task.task_id)
            report = result.harness_summary[:12_000]
            if result.files_changed and not result.pushed:
                report += "\n\nChanges were not pushed, so later turns will not see them."
            session["messages"].append(
                {
                    "role": "assistant",
                    "content": report,
                    "task_id": task.task_id,
                    "created_at": _now(),
                }
            )
            if result.pushed:
                session["branch"] = result.branch
            if result.pull_request_url:
                session["pull_request_url"] = result.pull_request_url
            session["state"] = "unpersisted" if result.files_changed and not result.pushed else "idle"
            session["active_task_id"] = None
            session["active_execution_name"] = None
            session["last_task_id"] = task.task_id

        self._update(task.session_id, mutate, task.owner_id)

    def mark_retrying(self, task: CodingTask) -> None:
        if task.session_id:
            def mutate(session: dict[str, Any]) -> None:
                _set_turn_state(session, task.task_id, "retrying")
                session["active_execution_name"] = None

            self._update(task.session_id, mutate, task.owner_id)

    def fail_turn(self, task: CodingTask, message: str = "The coding job failed. You can try again.") -> None:
        if not task.session_id:
            return

        def mutate(session: dict[str, Any]) -> None:
            _check_active_turn(session, task.task_id)
            session["messages"].append(
                {"role": "assistant", "content": message, "task_id": task.task_id, "created_at": _now()}
            )
            session["state"] = "failed"
            session["active_task_id"] = None
            session["active_execution_name"] = None
            session["last_task_id"] = task.task_id

        self._update(task.session_id, mutate, task.owner_id)

    def close(self) -> None:
        self._client.close()

    def _blob(self, session_id: str, owner_id: str | None = None):
        return self._client.get_blob_client(f"sessions/{_owner_bucket(owner_id)}/{UUID(session_id)}.json")

    def _read_with_etag(
        self, session_id: str, owner_id: str | None = None
    ) -> tuple[dict[str, Any], str]:
        stream = self._blob(session_id, owner_id).download_blob()
        return json.loads(stream.readall()), stream.properties.etag

    def _update(
        self, session_id: str, mutate: Callable[[dict[str, Any]], None], owner_id: str | None = None
    ) -> dict[str, Any]:
        for _ in range(4):
            session, etag = self._read_with_etag(session_id, owner_id)
            mutate(session)
            session["updated_at"] = _now()
            try:
                self._blob(session_id, owner_id).upload_blob(
                    json.dumps(session),
                    overwrite=True,
                    etag=etag,
                    match_condition=MatchConditions.IfNotModified,
                    content_settings=ContentSettings(content_type="application/json"),
                    metadata=_summary_metadata(session),
                )
                return session
            except ResourceModifiedError:
                continue
        raise SessionConflictError("session was updated concurrently; please retry")


def _check_active_turn(session: dict[str, Any], task_id: str) -> None:
    if session["active_task_id"] == task_id:
        return
    if session["last_task_id"] == task_id:
        raise TurnAlreadyCompleted(f"turn {task_id} was already completed")
    raise SessionConflictError("turn is not active in this session")


def _set_turn_state(session: dict[str, Any], task_id: str, state: str) -> None:
    _check_active_turn(session, task_id)
    session["state"] = state


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _owner_bucket(owner_id: str | None) -> str:
    return sha256((owner_id or "legacy").encode("utf-8")).hexdigest()


def _summary_metadata(session: dict[str, Any]) -> dict[str, str]:
    return {
        "repository": session["repository"],
        "state": session["state"],
        "updated_at": session["updated_at"],
    }
