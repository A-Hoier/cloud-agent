from __future__ import annotations

import json
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest

from aiops_agent import deepseek_harness
from aiops_agent.config import ConfigError, Settings
from aiops_agent.deepseek_harness import DeepSeekHarness
from aiops_agent.dsh_harness import DshHarness
from aiops_agent.harness import CopilotHarness, HarnessError, create_harness
from aiops_agent.models import CodingTask, RepositoryRecord


def _settings(**overrides: str) -> Settings:
    return Settings.from_env(
        {
            "QUEUE_ACCOUNT_URL": "https://example.queue.core.windows.net",
            "GITHUB_REPOSITORIES": "acme/api",
            "QUEUE_NAME": "tasks",
            "TASK_STATUS_CONTAINER_URL": "https://example.blob.core.windows.net/status",
            "MODEL_ENDPOINT": "https://models.example.internal/v1/chat/completions",
            "MODEL_API_KEY": "test-key",
            "MODEL_NAME": "my-deepseek-model",
            **overrides,
        }
    )


def _task() -> CodingTask:
    return CodingTask("task-1", "api", "Add a greeting", datetime.now(UTC))


def _repository() -> RepositoryRecord:
    return RepositoryRecord("api", "API", "https://github.com/acme/api.git")


def test_harness_provider_selects_deepseek_by_default_and_copilot_explicitly():
    assert isinstance(create_harness(_settings()), DeepSeekHarness)
    assert isinstance(create_harness(_settings(HARNESS_PROVIDER="dsh")), DshHarness)
    assert isinstance(create_harness(_settings(HARNESS_PROVIDER="copilot")), CopilotHarness)


def test_invalid_provider_and_missing_model_configuration():
    with pytest.raises(ConfigError, match="HARNESS_PROVIDER"):
        _settings(HARNESS_PROVIDER="other")
    with pytest.raises(HarnessError, match="MODEL_API_KEY"):
        create_harness(_settings(MODEL_API_KEY=""))
    assert isinstance(
        create_harness(_settings(
            MODEL_API_KEY="", MODEL_ENDPOINT="https://foundry.openai.azure.com/openai/v1/chat/completions"
        )), DeepSeekHarness
    )
    with pytest.raises(HarnessError, match="MODEL_ENDPOINT"):
        create_harness(_settings(MODEL_ENDPOINT=""))
    with pytest.raises(HarnessError, match="MODEL_NAME"):
        create_harness(_settings(MODEL_NAME=""))


def test_job_is_limited_to_one_isolated_turn():
    with pytest.raises(ConfigError, match="one isolated turn"):
        _settings(QUEUE_MAX_MESSAGES="2")


def test_public_deepseek_endpoint_is_rejected():
    with pytest.raises(HarnessError, match="self-hosted"):
        create_harness(_settings(MODEL_ENDPOINT="https://api.deepseek.com/chat/completions"))


def test_deepseek_tool_loop_edits_file_and_returns_summary(tmp_path: Path, monkeypatch):
    settings = _settings()
    harness = DeepSeekHarness(settings)
    file = tmp_path / "app.py"
    file.write_text('print("old")\n', encoding="utf-8")
    requests = []
    responses = iter(
        [
            {
                "role": "assistant",
                "content": None,
                "reasoning_content": "I should update the file.",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "replace_text",
                            "arguments": json.dumps(
                                {"path": "app.py", "old": "old", "new": "new"}
                            ),
                        },
                    }
                ],
            },
            {"role": "assistant", "content": "Changed the greeting."},
        ]
    )

    def fake_request(messages, timeout):
        requests.append(json.loads(json.dumps(messages)))
        return next(responses)

    monkeypatch.setattr(harness, "_request", fake_request)
    summary = harness.run(tmp_path, _task(), _repository())
    assert summary == "Changed the greeting."
    assert file.read_text(encoding="utf-8") == 'print("new")\n'
    assert requests[1][-1] == {
        "role": "tool",
        "tool_call_id": "call-1",
        "content": "Updated app.py",
    }
    assert requests[1][-2]["reasoning_content"] == "I should update the file."


@pytest.mark.parametrize("path", ["../outside.txt", "/tmp/outside.txt", ".git/config"])
def test_deepseek_rejects_paths_outside_checkout(tmp_path: Path, path: str):
    harness = DeepSeekHarness(_settings())
    with pytest.raises(ValueError):
        harness._run_tool(tmp_path, "write_file", {"path": path, "content": "bad"}, float("inf"))


def test_deepseek_rejects_git_and_shell_commands(tmp_path: Path):
    harness = DeepSeekHarness(_settings())
    for argv in (["git", "commit", "-am", "oops"], ["sh", "-c", "git push"]):
        with pytest.raises(ValueError, match="not an allowed"):
            harness._run_tool(tmp_path, "run_check", {"argv": argv}, float("inf"))


def test_deepseek_request_keeps_key_in_header_and_uses_tool_schema(monkeypatch):
    harness = DeepSeekHarness(_settings(MODEL_API_KEY="private-key"))
    captured = {}

    def fake_urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return BytesIO(b'{"choices":[{"message":{"role":"assistant","content":"Done"}}]}')

    monkeypatch.setattr(deepseek_harness, "urlopen", fake_urlopen)
    assert harness._request([{"role": "user", "content": "Do it"}], 10)["content"] == "Done"
    request = captured["request"]
    assert request.full_url == "https://models.example.internal/v1/chat/completions"
    assert "private-key" not in request.full_url
    assert request.get_header("Authorization") == "Bearer private-key"
    body = json.loads(request.data)
    assert body["model"] == "my-deepseek-model"
    assert any(tool["function"]["name"] == "replace_text" for tool in body["tools"])


def test_self_hosted_endpoint_can_use_api_key_header(monkeypatch):
    harness = DeepSeekHarness(_settings(MODEL_AUTH_MODE="api-key"))
    captured = {}

    def fake_urlopen(request, timeout):
        captured["request"] = request
        return BytesIO(b'{"choices":[{"message":{"role":"assistant","content":"Done"}}]}')

    monkeypatch.setattr(deepseek_harness, "urlopen", fake_urlopen)
    harness._request([{"role": "user", "content": "Do it"}], 10)
    assert captured["request"].get_header("Api-key") == "test-key"
    assert captured["request"].get_header("Authorization") is None


def test_foundry_uses_default_azure_credential_without_key(monkeypatch):
    class Credential:
        def __init__(self):
            self.scopes = []

        def get_token(self, scope):
            self.scopes.append(scope)
            return SimpleNamespace(token="managed-identity-token")

    credential = Credential()
    harness = DeepSeekHarness(
        _settings(
            MODEL_API_KEY="",
            MODEL_ENDPOINT="https://foundry.services.ai.azure.com/api/projects/demo/openai/v1/chat/completions",
        ),
        credential,
    )
    captured = {}

    def fake_urlopen(request, timeout):
        captured["request"] = request
        return BytesIO(b'{"choices":[{"message":{"role":"assistant","content":"Done"}}]}')

    monkeypatch.setattr(deepseek_harness, "urlopen", fake_urlopen)
    harness._request([{"role": "user", "content": "Do it"}], 10)
    assert credential.scopes == ["https://ai.azure.com/.default"]
    assert captured["request"].get_header("Authorization") == "Bearer managed-identity-token"


def test_search_and_delete_file_tools(tmp_path: Path, monkeypatch):
    harness = DeepSeekHarness(_settings())
    path = tmp_path / "app.py"
    path.write_text("needle in a haystack\n", encoding="utf-8")
    def fake_search(argv, **kwargs):
        assert argv[0] == "rg"
        assert argv[-2:] == ["needle", "."]
        return SimpleNamespace(returncode=0, stdout="./app.py:1:needle in a haystack\n")

    monkeypatch.setattr(deepseek_harness.subprocess, "run", fake_search)
    assert "app.py:1:needle" in harness._run_tool(
        tmp_path, "search_text", {"query": "needle"}, float("inf")
    )
    assert harness._run_tool(tmp_path, "delete_file", {"path": "app.py"}, float("inf")) == "Deleted app.py"
    assert not path.exists()


def test_dsh_uses_private_model_and_does_not_inherit_git_token(tmp_path: Path, monkeypatch):
    settings = _settings(HARNESS_PROVIDER="dsh", WORKDIR=str(tmp_path / "worker"))
    harness = DshHarness(settings)
    observed = {}

    def fake_run(command, **kwargs):
        observed["command"] = command
        observed["environment"] = kwargs["env"]
        observed["prompt"] = kwargs["input"]
        observed["settings"] = Path(kwargs["env"]["DSH_HOME"], "settings.yaml").read_text()
        observed["patch"] = Path(command[-1]).read_text()
        return SimpleNamespace(returncode=0, stdout="Implemented the change", stderr="")

    monkeypatch.setattr("aiops_agent.dsh_harness.subprocess.run", fake_run)
    monkeypatch.setenv("GITHUB_PAT", "do-not-inherit")
    result = harness.run(tmp_path, _task(), _repository())
    assert result == "Implemented the change"
    assert observed["command"][:3] == ["dsh", "--profile", "headless"]
    assert "Add a greeting" in observed["prompt"]
    assert "https://models.example.internal/v1" in observed["settings"]
    assert "my-deepseek-model" in observed["patch"]
    assert "- id: llm-deepseek\n  disabled: true" in observed["patch"]
    assert "- id: session-telemetry-otel\n  disabled: true" in observed["patch"]
    assert observed["environment"]["CLOUD_AGENT_MODEL_TOKEN"] == "test-key"
    assert "GITHUB_PAT" not in observed["environment"]


def test_copilot_subprocess_does_not_inherit_worker_secrets(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("GITHUB_PAT", "push-secret")
    monkeypatch.setenv("MODEL_API_KEY", "model-secret")
    monkeypatch.setenv("GH_TOKEN", "separate-copilot-token")
    observed = {}

    def fake_run(command, **kwargs):
        observed.update(kwargs["env"])
        return SimpleNamespace(returncode=0, stdout="No changes needed", stderr="")

    monkeypatch.setattr("aiops_agent.harness.subprocess.run", fake_run)
    result = CopilotHarness(_settings(HARNESS_PROVIDER="copilot")).run(
        tmp_path, _task(), _repository()
    )
    assert result == "No changes needed"
    assert observed["GH_TOKEN"] == "separate-copilot-token"
    assert "GITHUB_PAT" not in observed
    assert "MODEL_API_KEY" not in observed
