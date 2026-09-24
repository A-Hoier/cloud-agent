from __future__ import annotations

import pytest

from aiops_agent.config import ConfigError, Settings
from aiops_agent.repository_registry import RepositoryRegistry


def test_minimal_deployment_configuration_builds_storage_urls_and_repo_allowlist():
    settings = Settings.from_env({
        "AZURE_STORAGE_ACCOUNT_NAME": "cloudagentstore",
        "GITHUB_REPOSITORIES": "acme/api, acme/web@develop",
        "GITHUB_PAT": "dummy-token",
    })
    assert settings.queue_account_url == "https://cloudagentstore.queue.core.windows.net"
    assert settings.status_container_url == "https://cloudagentstore.blob.core.windows.net/cloud-agent"
    assert settings.queue_name == "cloud-agent-tasks"
    assert settings.github_token == "dummy-token"
    registry = RepositoryRegistry.load(settings, object())
    assert [item.key for item in registry.records] == ["acme/api", "acme/web"]
    assert registry.resolve("acme/api").repo_url == "https://github.com/acme/api.git"
    assert registry.resolve("acme/web").default_branch == "develop"


def test_missing_allowlist_and_malformed_repo_fail_closed():
    with pytest.raises(ConfigError, match="GITHUB_REPOSITORIES"):
        Settings.from_env({"AZURE_STORAGE_ACCOUNT_NAME": "cloudagentstore"})
    settings = Settings.from_env({
        "AZURE_STORAGE_ACCOUNT_NAME": "cloudagentstore",
        "GITHUB_REPOSITORIES": "acme/api,https://evil.example/repo",
    })
    with pytest.raises(ValueError, match="invalid GITHUB_REPOSITORIES"):
        RepositoryRegistry.load(settings, object())


def test_storage_account_name_validation():
    with pytest.raises(ConfigError, match="AZURE_STORAGE_ACCOUNT_NAME"):
        Settings.from_env({
            "AZURE_STORAGE_ACCOUNT_NAME": "Invalid-Name",
            "GITHUB_REPOSITORIES": "acme/api",
        })
