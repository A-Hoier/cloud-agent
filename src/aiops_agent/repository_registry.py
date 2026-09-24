"""Loads the allow-list of repositories exposed by the frontend."""

from __future__ import annotations

import json
import re
from pathlib import Path

from azure.core.credentials import TokenCredential
from azure.storage.blob import BlobClient

from .config import Settings
from .logging_setup import get_logger
from .models import RepositoryRecord

log = get_logger(__name__)


class RepositoryNotRegisteredError(LookupError):
    """Raised when a task names a repository outside the allow-list."""


class RepositoryRegistry:
    def __init__(self, records: dict[str, RepositoryRecord]) -> None:
        self._records = records

    @property
    def records(self) -> list[RepositoryRecord]:
        return list(self._records.values())

    @classmethod
    def load(cls, settings: Settings, credential: TokenCredential) -> RepositoryRegistry:
        if settings.github_repositories:
            records = {}
            for item in settings.github_repositories.split(","):
                name, separator, branch = item.strip().partition("@")
                if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", name) or ".." in name:
                    raise ValueError(f"invalid GITHUB_REPOSITORIES entry: {name!r}")
                if separator and (
                    not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}", branch)
                    or ".." in branch or "//" in branch or branch.endswith(("/", ".lock"))
                ):
                    raise ValueError(f"invalid base branch in GITHUB_REPOSITORIES entry: {item!r}")
                records[name] = RepositoryRecord(
                    key=name, name=name, repo_url=f"https://github.com/{name}.git",
                    default_branch=branch if separator else "main",
                )
            log.info("repository_registry_loaded", count=len(records))
            return cls(records)
        if settings.registry_blob_url:
            raw = _read_blob(settings.registry_blob_url, credential)
        else:
            path = Path(settings.registry_path or "")
            if not path.is_file():
                raise FileNotFoundError(f"repository registry not found at {path}")
            raw = path.read_text(encoding="utf-8")

        payload = json.loads(raw)
        entries = payload.get("repositories", payload)
        if not isinstance(entries, dict):
            raise TypeError("registry must be an object keyed by repository id")
        for key in entries:
            if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_./-]{1,200}", key):
                raise ValueError(f"invalid repository id: {key!r}")
        records = {key: RepositoryRecord.from_dict(key, value) for key, value in entries.items()}
        log.info("repository_registry_loaded", count=len(records))
        return cls(records)

    def resolve(self, repository_id: str) -> RepositoryRecord:
        record = self._records.get(repository_id)
        if record is None:
            raise RepositoryNotRegisteredError(f"repository {repository_id!r} is not registered")
        return record


def _read_blob(blob_url: str, credential: TokenCredential) -> str:
    client = BlobClient.from_blob_url(blob_url, credential=credential)
    try:
        return client.download_blob().readall().decode("utf-8")
    finally:
        client.close()
