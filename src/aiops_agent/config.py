"""Environment-driven configuration for the job."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass


class ConfigError(RuntimeError):
    """Raised when required configuration is absent or invalid."""


@dataclass(frozen=True)
class Settings:
    # Queue
    queue_account_url: str
    queue_name: str
    queue_visibility_timeout: int
    queue_max_messages: int
    queue_max_dequeue_count: int

    # Repository allow-list
    registry_path: str | None
    registry_blob_url: str | None
    github_repositories: str | None
    status_container_url: str

    # Git
    git_author_name: str
    git_author_email: str
    branch_prefix: str
    azure_devops_pat: str | None
    github_token: str | None
    push_enabled: bool

    # Coding harness
    harness_provider: str
    copilot_binary: str
    copilot_model: str | None
    copilot_timeout_seconds: int
    model_api_key: str | None
    model_endpoint: str | None
    model_name: str | None
    model_auth_mode: str
    model_token_scope: str
    model_reasoning_effort: str | None
    deepseek_max_steps: int
    deepseek_timeout_seconds: int

    workdir: str
    dry_run: bool

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        e = dict(os.environ if env is None else env)

        registry_path = e.get("REPOSITORY_REGISTRY_PATH") or None
        registry_blob_url = e.get("REPOSITORY_REGISTRY_BLOB_URL") or None
        github_repositories = e.get("GITHUB_REPOSITORIES") or None
        if not registry_path and not registry_blob_url and not github_repositories:
            raise ConfigError(
                "configure GITHUB_REPOSITORIES, REPOSITORY_REGISTRY_PATH, or REPOSITORY_REGISTRY_BLOB_URL"
            )

        harness_provider = e.get("HARNESS_PROVIDER", "deepseek").strip().lower()
        if harness_provider not in {"deepseek", "dsh", "copilot"}:
            raise ConfigError("HARNESS_PROVIDER must be 'deepseek', 'dsh' or 'copilot'")
        queue_max_messages = _int(e, "QUEUE_MAX_MESSAGES", 1)
        if queue_max_messages != 1:
            raise ConfigError("QUEUE_MAX_MESSAGES must be 1 so each job handles one isolated turn")

        account_name = e.get("AZURE_STORAGE_ACCOUNT_NAME", "").strip()
        if account_name and not re.fullmatch(r"[a-z0-9]{3,24}", account_name):
            raise ConfigError("AZURE_STORAGE_ACCOUNT_NAME must be 3–24 lowercase letters or digits")
        queue_url = e.get("QUEUE_ACCOUNT_URL") or (f"https://{account_name}.queue.core.windows.net" if account_name else "")
        status_url = e.get("TASK_STATUS_CONTAINER_URL") or (
            f"https://{account_name}.blob.core.windows.net/cloud-agent" if account_name else ""
        )
        return cls(
            queue_account_url=queue_url or _required(e, "QUEUE_ACCOUNT_URL"),
            queue_name=e.get("QUEUE_NAME", "cloud-agent-tasks"),
            queue_visibility_timeout=_int(e, "QUEUE_VISIBILITY_TIMEOUT", 2100),
            queue_max_messages=queue_max_messages,
            queue_max_dequeue_count=_int(e, "QUEUE_MAX_DEQUEUE_COUNT", 3),
            registry_path=registry_path,
            registry_blob_url=registry_blob_url,
            github_repositories=github_repositories,
            status_container_url=status_url or _required(e, "TASK_STATUS_CONTAINER_URL"),
            git_author_name=e.get("GIT_AUTHOR_NAME", "coding-agent"),
            git_author_email=e.get("GIT_AUTHOR_EMAIL", "coding-agent@noreply.local"),
            branch_prefix=e.get("BRANCH_PREFIX", "agent/task"),
            azure_devops_pat=e.get("AZURE_DEVOPS_PAT") or None,
            github_token=e.get("GITHUB_PAT") or e.get("GIT_GITHUB_TOKEN") or None,
            push_enabled=_bool(e, "PUSH_ENABLED", True),
            harness_provider=harness_provider,
            copilot_binary=e.get("COPILOT_BINARY", "copilot"),
            copilot_model=e.get("COPILOT_MODEL") or None,
            copilot_timeout_seconds=_int(e, "COPILOT_TIMEOUT_SECONDS", 1200),
            model_api_key=e.get("MODEL_API_KEY") or None,
            model_endpoint=e.get("MODEL_ENDPOINT") or None,
            model_name=e.get("MODEL_NAME") or None,
            model_auth_mode=e.get("MODEL_AUTH_MODE", "auto").strip().lower(),
            model_token_scope=e.get("MODEL_TOKEN_SCOPE", "auto"),
            model_reasoning_effort=e.get("MODEL_REASONING_EFFORT", "").strip().lower() or None,
            deepseek_max_steps=_int(e, "DEEPSEEK_MAX_STEPS", 40),
            deepseek_timeout_seconds=_int(e, "DEEPSEEK_TIMEOUT_SECONDS", 1200),
            workdir=e.get("WORKDIR", "/tmp/coding-agent"),
            dry_run=_bool(e, "DRY_RUN", False),
        )


def _required(env: dict[str, str], key: str) -> str:
    value = env.get(key, "").strip()
    if not value:
        raise ConfigError(f"required environment variable {key} is not set")
    return value


def _int(env: dict[str, str], key: str, default: int) -> int:
    raw = env.get(key, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"environment variable {key} must be an integer, got {raw!r}") from exc


def _bool(env: dict[str, str], key: str, default: bool) -> bool:
    raw = env.get(key, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}
