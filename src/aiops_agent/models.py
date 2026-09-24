"""Domain objects passed between the coding-task pipeline stages."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID


class MessageFormatError(ValueError):
    """Raised when a queue message cannot be interpreted as a coding task."""


@dataclass(frozen=True)
class CodingTask:
    """A user-requested change to one of the repositories in the allow-list."""

    task_id: str
    repository: str
    instruction: str
    created_at: datetime
    target_branch: str | None = None
    merge_when_ready: bool = False
    session_id: str | None = None
    owner_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def parse(cls, body: str) -> CodingTask:
        body = body.strip()
        if not body:
            raise MessageFormatError("queue message is empty")

        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise MessageFormatError(f"queue message is not valid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise MessageFormatError("queue message JSON must be an object")

        repository = payload.get("repository")
        instruction = payload.get("instruction")
        if not isinstance(repository, str) or not repository.strip():
            raise MessageFormatError("queue message is missing 'repository'")
        if not isinstance(instruction, str) or not instruction.strip():
            raise MessageFormatError("queue message is missing 'instruction'")
        repository = repository.strip()
        instruction = instruction.strip()
        if len(repository) > 200:
            raise MessageFormatError("repository id exceeds 200 characters")
        if len(instruction) > 20_000:
            raise MessageFormatError("task instruction exceeds 20000 characters")
        if len(instruction.encode("utf-8")) > 48_000:
            raise MessageFormatError("task instruction exceeds 48000 UTF-8 bytes")

        task_id = payload.get("task_id")
        if not isinstance(task_id, str):
            raise MessageFormatError("task_id must be a UUID string")
        try:
            task_id = str(UUID(task_id))
        except ValueError as exc:
            raise MessageFormatError("task_id must be a UUID") from exc

        session_id = payload.get("session_id")
        if session_id is not None:
            if not isinstance(session_id, str):
                raise MessageFormatError("session_id must be a UUID string")
            try:
                session_id = str(UUID(session_id))
            except ValueError as exc:
                raise MessageFormatError("session_id must be a UUID") from exc

        owner_id = payload.get("owner_id")
        if owner_id is not None and (not isinstance(owner_id, str) or not owner_id.strip() or len(owner_id) > 200):
            raise MessageFormatError("owner_id must be a non-empty identity")

        created_raw = payload.get("created_at")
        try:
            created_at = _parse_timestamp(created_raw) if created_raw else datetime.now(UTC)
        except ValueError as exc:
            raise MessageFormatError("created_at must be an ISO 8601 timestamp") from exc
        merge_when_ready = payload.get("merge_when_ready", False)
        if not isinstance(merge_when_ready, bool):
            raise MessageFormatError("merge_when_ready must be a boolean")
        target_branch = payload.get("target_branch")
        if target_branch is not None and (
            not isinstance(target_branch, str) or not target_branch.strip() or len(target_branch) > 200
        ):
            raise MessageFormatError("target_branch must be a non-empty branch name")

        return cls(
            task_id=task_id,
            repository=repository,
            instruction=instruction,
            created_at=created_at,
            target_branch=target_branch.strip() if target_branch else None,
            merge_when_ready=merge_when_ready,
            session_id=session_id,
            owner_id=owner_id,
            raw=payload,
        )


@dataclass(frozen=True)
class RepositoryRecord:
    """An allow-listed repository users may select in the frontend."""

    key: str
    name: str
    repo_url: str
    default_branch: str = "main"
    source_path: str = "."
    notes: str | None = None
    github_merge_method: str = "SQUASH"

    @classmethod
    def from_dict(cls, key: str, data: dict[str, Any]) -> RepositoryRecord:
        missing = {"repo_url"} - data.keys()
        if missing:
            raise ValueError(f"repository '{key}' is missing required field(s): {sorted(missing)}")
        merge_method = data.get("github_merge_method", "SQUASH")
        if not isinstance(merge_method, str):
            raise TypeError(f"repository '{key}' has invalid github_merge_method")
        merge_method = merge_method.upper()
        if merge_method not in {"MERGE", "SQUASH", "REBASE"}:
            raise ValueError(f"repository '{key}' has invalid github_merge_method")
        return cls(
            key=key,
            name=data.get("name", key),
            repo_url=data["repo_url"],
            default_branch=data.get("default_branch", "main"),
            source_path=data.get("source_path", "."),
            notes=data.get("notes"),
            github_merge_method=merge_method,
        )


@dataclass(frozen=True)
class TaskResult:
    task_id: str
    branch: str
    pushed: bool
    files_changed: list[str]
    harness_summary: str
    pull_request_url: str | None = None
    auto_merge_enabled: bool = False


def _parse_timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
