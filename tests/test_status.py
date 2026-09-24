from __future__ import annotations

import json

from azure.storage.blob import ContainerClient

from aiops_agent.status import TaskStatusStore


class FakeDownload:
    def __init__(self, data: str) -> None:
        self._data = data

    def readall(self) -> bytes:
        return self._data.encode("utf-8")


class FakeContainer:
    def __init__(self) -> None:
        self.blobs = {}

    def upload_blob(self, name, data, overwrite, content_settings):
        assert overwrite
        assert content_settings.content_type == "application/json"
        self.blobs[name] = data

    def download_blob(self, name):
        return FakeDownload(self.blobs[name])

    def close(self):
        pass


def test_task_status_roundtrip(monkeypatch):
    container = FakeContainer()
    monkeypatch.setattr(ContainerClient, "from_container_url", lambda url, credential: container)
    store = TaskStatusStore("https://example.blob.core.windows.net/status", object())
    task_id = "240cb99a-0287-4fa1-a296-d976dd0c24bc"

    store.write(task_id, repository="api", state="queued")

    assert json.loads(container.blobs[f"{task_id}.json"])["state"] == "queued"
    assert store.read(task_id)["repository"] == "api"
