"""Adapter for the upstream DeepSeek Harness headless CLI and a private model."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

from azure.core.credentials import TokenCredential

from .config import Settings
from .deepseek_harness import DeepSeekHarness
from .harness import CodingHarness, HarnessError, build_prompt
from .logging_setup import get_logger
from .models import CodingTask, RepositoryRecord

log = get_logger(__name__)


class DshHarness(CodingHarness):
    """Run a single dsh headless turn in an ephemeral profile; Blob owns history."""

    def __init__(self, settings: Settings, credential: TokenCredential | None = None) -> None:
        self._settings = settings
        self._model = DeepSeekHarness(settings, credential)
        if self._model._responses:
            raise HarnessError("dsh requires a /chat/completions endpoint; use HARNESS_PROVIDER=deepseek")
        if settings.model_api_key and (
            settings.model_auth_mode == "api-key"
            or (settings.model_auth_mode == "auto" and self._model._azure_endpoint)
        ):
            raise HarnessError("dsh requires Bearer model authentication; use HARNESS_PROVIDER=deepseek for api-key")

    def run(
        self,
        repo_path: Path,
        task: CodingTask,
        repository: RepositoryRecord,
        history: list[dict[str, str]] | None = None,
    ) -> str:
        endpoint = self._model._endpoint
        base_url = endpoint[: -len("/chat/completions")]
        credential = self._model._credential
        model_token = self._settings.model_api_key
        if not model_token:
            assert credential is not None
            model_token = credential.get_token(self._model._token_scope).token

        Path(self._settings.workdir).mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="dsh-", dir=self._settings.workdir) as temporary:
            home = Path(temporary)
            (home / "settings.yaml").write_text(
                "llm-pi-ai:\n  providers:\n    cloud-agent:\n"
                "      apiKeyEnv: CLOUD_AGENT_MODEL_TOKEN\n"
                "      api: openai-completions\n"
                f"      baseURL: {json.dumps(base_url)}\n"
                "      compat:\n        supportsDeveloperRole: false\n"
                "        maxTokensField: max_tokens\n"
                f"      models:\n        - id: {json.dumps(self._settings.model_name)}\n",
                encoding="utf-8",
            )
            patch = home / "model.patch.yml"
            patch.write_text(
                "- id: agent-default-model\n"
                "  config:\n"
                "    provider: cloud-agent\n"
                f"    model: {json.dumps(self._settings.model_name)}\n"
                "- id: llm-deepseek\n  disabled: true\n"
                "- id: web-search-deepseek\n  disabled: true\n"
                "- id: session-telemetry-otel\n  disabled: true\n"
                "- id: web\n  disabled: true\n"
                "- id: web-fetch-http\n  disabled: true\n"
                "- id: tool-web\n  disabled: true\n",
                encoding="utf-8",
            )
            env = {key: os.environ[key] for key in ("PATH", "LANG") if key in os.environ}
            env.update({
                "HOME": str(home),
                "DSH_HOME": str(home),
                "DSH_TOOLS_MODE": "ptc",
                "DSH_PERMISSION_MODE": "workspace-write",
                "CLOUD_AGENT_MODEL_TOKEN": model_token,
                "CI": "true",
            })
            log.info("harness_started", provider="dsh", task_id=task.task_id, repository=repository.key)
            try:
                result = subprocess.run(
                    ["dsh", "--profile", "headless", "--patch", str(patch)],
                    input=build_prompt(task, repository, history),
                    cwd=repo_path,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=self._settings.deepseek_timeout_seconds,
                    check=False,
                )
            except FileNotFoundError as exc:
                raise HarnessError("dsh CLI is not installed") from exc
            except subprocess.TimeoutExpired as exc:
                raise HarnessError("dsh harness timed out") from exc
            if result.returncode:
                raise HarnessError(f"dsh harness exited with {result.returncode}; inspect worker logs")
            log.info("harness_finished", provider="dsh", task_id=task.task_id)
            return result.stdout.strip()
