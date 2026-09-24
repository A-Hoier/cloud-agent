"""Durable task status shared by the web app and disposable workers."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from azure.core.credentials import TokenCredential
from azure.storage.blob import ContainerClient, ContentSettings


class TaskStatusStore:
    def __init__(self, container_url: str, credential: TokenCredential) -> None:
        self._client = ContainerClient.from_container_url(container_url, credential=credential)

    def write(self, task_id: str, **fields: Any) -> dict[str, Any]:
        status = {
            "task_id": task_id,
            "updated_at": datetime.now(UTC).isoformat(),
            **fields,
        }
        self._client.upload_blob(
            name=f"{_checked_id(task_id)}.json",
            data=json.dumps(status),
            overwrite=True,
            content_settings=ContentSettings(content_type="application/json"),
        )
        return status

    def read(self, task_id: str) -> dict[str, Any]:
        data = self._client.download_blob(f"{_checked_id(task_id)}.json").readall()
        return json.loads(data)

    def read_for_owner(self, task_id: str, owner_id: str) -> dict[str, Any]:
        status = self.read(task_id)
        if status.get("owner_id") != owner_id:
            raise ValueError("task not found")
        return status

    def close(self) -> None:
        self._client.close()


def _checked_id(task_id: str) -> str:
    return str(UUID(task_id))
