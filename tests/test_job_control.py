from __future__ import annotations

from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from aiops_agent import job_control
from aiops_agent.job_control import JobStopError, JobStopper


class FakeCredential:
    def get_token(self, scope: str) -> SimpleNamespace:
        assert scope == "https://management.azure.com/.default"
        return SimpleNamespace(token="test-token")


class FakeResponse:
    status = 202

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_stop_targets_only_configured_job_execution(monkeypatch):
    captured = []

    def fake_open(request, timeout):
        captured.append((request, timeout))
        return FakeResponse()

    monkeypatch.setattr(job_control, "urlopen", fake_open)
    stopper = JobStopper(
        FakeCredential(), "bcfe4a56-5d42-4671-aaef-80513d1ad372", "rg-cloud-agent-cvm8",
        "cloud-agent-worker",
    )
    stopper.stop_execution("cloud-agent-worker-abc123")
    request, timeout = captured[0]
    assert request.get_method() == "POST"
    assert "/jobs/cloud-agent-worker/executions/cloud-agent-worker-abc123/stop?" in request.full_url
    assert request.get_header("Authorization") == "Bearer test-token"
    assert timeout == 10
    with pytest.raises(JobStopError, match="invalid worker execution name"):
        stopper.stop_execution("other-job-abc123")
    assert len(captured) == 1


def test_azure_stop_error_is_retryable(monkeypatch):
    def forbidden(request, timeout):
        raise HTTPError(request.full_url, 403, "Forbidden", {}, None)

    monkeypatch.setattr(job_control, "urlopen", forbidden)
    stopper = JobStopper(
        FakeCredential(), "bcfe4a56-5d42-4671-aaef-80513d1ad372", "rg-cloud-agent-cvm8",
        "cloud-agent-worker",
    )
    with pytest.raises(JobStopError, match="HTTP 403"):
        stopper.stop_execution("cloud-agent-worker-abc123")
