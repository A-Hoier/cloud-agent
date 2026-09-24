"""Opt-in connectivity checks for the queue, repositories, and coding harness."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from azure.core.exceptions import ClientAuthenticationError, ResourceNotFoundError
from azure.storage.blob import ContainerClient
from azure.storage.queue import QueueClient

from aiops_agent.config import Settings
from aiops_agent.repo import RepoWorkspace, _run_git

pytestmark = pytest.mark.integration


@pytest.fixture
def queue_client(settings: Settings, credential) -> QueueClient:
    client = QueueClient(
        account_url=settings.queue_account_url,
        queue_name=settings.queue_name,
        credential=credential,
    )
    yield client
    client.close()


def test_queue_exists_and_is_readable(queue_client: QueueClient, settings: Settings):
    try:
        properties = queue_client.get_queue_properties()
    except ResourceNotFoundError:
        pytest.fail(f"queue '{settings.queue_name}' does not exist at {settings.queue_account_url}")
    except ClientAuthenticationError as exc:
        pytest.fail(f"queue authentication failed; check the identity role: {exc}")
    assert properties.name == settings.queue_name
    queue_client.peek_messages(max_messages=1)


def test_status_container_exists_and_is_readable(settings: Settings, credential):
    client = ContainerClient.from_container_url(settings.status_container_url, credential=credential)
    try:
        client.get_container_properties()
    finally:
        client.close()


@pytest.mark.skipif(
    os.environ.get("AIOPS_IT_ALLOW_QUEUE_WRITE") != "1",
    reason="set AIOPS_IT_ALLOW_QUEUE_WRITE=1 to exercise send/receive/delete",
)
def test_queue_message_roundtrip(queue_client: QueueClient):
    marker = str(uuid4())
    payload = json.dumps(
        {"task_id": marker, "repository": "nonexistent-connectivity-probe", "instruction": "probe only"}
    )
    queue_client.send_message(payload)
    received = None
    for message in queue_client.receive_messages(max_messages=32, visibility_timeout=30):
        if marker in message.content:
            received = message
            break
        queue_client.update_message(message, visibility_timeout=0)
    assert received is not None
    queue_client.delete_message(received)


@pytest.fixture
def workspace(settings: Settings, credential) -> RepoWorkspace:
    workspace = RepoWorkspace(settings, credential)
    yield workspace
    workspace.cleanup()


def test_git_binary_available():
    assert shutil.which("git"), "git is not on PATH"


def test_repo_is_reachable(workspace: RepoWorkspace, target_repository):
    refs = workspace.ls_remote(target_repository.repo_url, target_repository.default_branch)
    assert refs == [f"refs/heads/{target_repository.default_branch}"]


def test_every_registered_repo_is_reachable(workspace: RepoWorkspace, registry):
    unreachable = []
    for record in registry.records:
        try:
            if not workspace.ls_remote(record.repo_url, record.default_branch):
                unreachable.append(f"{record.key}: branch '{record.default_branch}' missing")
        except Exception as exc:  # noqa: BLE001
            unreachable.append(f"{record.key}: {exc}")
    assert not unreachable, "unreachable repositories:\n" + "\n".join(unreachable)


@pytest.mark.skipif(
    os.environ.get("AIOPS_IT_ALLOW_PUSH") != "1",
    reason="set AIOPS_IT_ALLOW_PUSH=1 to verify push permission against the real repo",
)
def test_push_permission(workspace: RepoWorkspace, settings: Settings, target_repository):
    repo = workspace.clone(target_repository.repo_url, target_repository.default_branch)
    branch = f"agent/connectivity-probe-{uuid4().hex[:8]}"
    probe_path = repo.path / ".coding-agent-connectivity-probe"
    probe_path.write_text(f"coding agent push probe {datetime.now(UTC).isoformat()}\n", encoding="utf-8")
    repo.create_branch(branch)

    try:
        pushed = repo.commit_and_push(
            branch,
            "chore: coding agent connectivity probe",
            replace(settings, push_enabled=True, dry_run=False),
        )
        assert pushed
    finally:
        if workspace.ls_remote(target_repository.repo_url, branch):
            _run_git(("push", "origin", "--delete", branch), cwd=repo.path, env=repo._env)


def test_copilot_cli_available(settings: Settings):
    if settings.harness_provider != "copilot":
        pytest.skip("Copilot CLI is not the selected harness")
    binary = shutil.which(settings.copilot_binary)
    if not binary:
        pytest.fail(f"coding harness '{settings.copilot_binary}' is not on PATH")
    result = subprocess.run(
        [binary, "--version"], capture_output=True, text=True, timeout=120, check=False
    )
    assert result.returncode == 0, result.stderr.strip()
