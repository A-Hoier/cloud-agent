"""Shared setup for the integration suite.

These tests talk to real Azure resources and a real Azure DevOps repo. They are
skipped unless AIOPS_INTEGRATION=1 is set, so the unit suite stays offline.
"""

from __future__ import annotations

import os

import pytest

from aiops_agent.config import ConfigError, Settings


def pytest_collection_modifyitems(config, items):
    if os.environ.get("AIOPS_INTEGRATION") == "1":
        return
    skip = pytest.mark.skip(reason="set AIOPS_INTEGRATION=1 to run integration tests")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def settings() -> Settings:
    try:
        return Settings.from_env()
    except ConfigError as exc:
        pytest.fail(f"integration environment is incomplete: {exc}")


@pytest.fixture(scope="session")
def credential():
    from azure.identity import DefaultAzureCredential

    cred = DefaultAzureCredential()
    yield cred
    cred.close()


@pytest.fixture(scope="session")
def registry(settings: Settings, credential):
    from aiops_agent.repository_registry import RepositoryRegistry

    return RepositoryRegistry.load(settings, credential)


@pytest.fixture(scope="session")
def target_repository(registry):
    """The repository the connectivity checks operate on."""
    repository_id = os.environ.get("AIOPS_IT_REPOSITORY")
    if not repository_id:
        pytest.skip("set AIOPS_IT_REPOSITORY to the registry id under test")
    return registry.resolve(repository_id)
