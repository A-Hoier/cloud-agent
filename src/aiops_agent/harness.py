"""Runs the coding agent in headless mode for one user task."""

from __future__ import annotations

import os
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path

from azure.core.credentials import TokenCredential

from .config import Settings
from .logging_setup import get_logger
from .models import CodingTask, RepositoryRecord

log = get_logger(__name__)

_PROMPT_TEMPLATE = """You are an autonomous coding agent working inside a fresh checkout of
the repository "{repo_name}".

Respond to the user's latest message below. If it asks for a code change, implement it and verify
it with relevant tests and checks. If it asks a question, answer it without speculative edits.
Inspect existing code and local instructions before editing. Keep changes focused, preserve
existing conventions, and do not expose credentials or secrets.

The project to change lives under: {source_path}
Repository-specific guidance: {notes}

Important constraints:
- Do NOT run git commit, git push, git checkout, git switch, git branch, git reset, or any other
  git write command. The surrounding harness owns version control and attribution.
- Do not edit files outside this checkout.
- If the request is ambiguous or cannot be completed safely, make no speculative changes and
  explain what is missing.
- Finish with a concise report of the implementation, validation performed, and any remaining
  concerns.

{history}
## Latest user message
{instruction}
"""


class HarnessError(RuntimeError):
    """Raised when the coding harness cannot be executed."""


class CodingHarness(ABC):
    """Common interface for coding agents used by the worker."""

    @abstractmethod
    def run(
        self,
        repo_path: Path,
        task: CodingTask,
        repository: RepositoryRecord,
        history: list[dict[str, str]] | None = None,
    ) -> str:
        """Edit the checkout for one task and return a human-readable summary."""


class CopilotHarness(CodingHarness):
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def run(
        self,
        repo_path: Path,
        task: CodingTask,
        repository: RepositoryRecord,
        history: list[dict[str, str]] | None = None,
    ) -> str:
        prompt = build_prompt(task, repository, history)
        command = [
            self._settings.copilot_binary,
            "--prompt",
            prompt,
            "--allow-all-tools",
            "--no-color",
        ]
        if self._settings.copilot_model:
            command += ["--model", self._settings.copilot_model]

        log.info("harness_started", task_id=task.task_id, repository=repository.key)
        safe_environment = {
            "PATH", "HOME", "LANG", "LC_ALL", "CI", "GH_TOKEN",
            "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "NODE_EXTRA_CA_CERTS",
        }
        harness_env = {
            key: value for key, value in os.environ.items()
            if key in safe_environment or key.startswith("COPILOT_PROVIDER_")
        }
        harness_env["COPILOT_ALLOW_ALL"] = "1"
        try:
            result = subprocess.run(
                command,
                cwd=repo_path,
                env=harness_env,
                capture_output=True,
                text=True,
                timeout=self._settings.copilot_timeout_seconds,
                check=False,
            )
        except FileNotFoundError as exc:
            raise HarnessError(f"coding harness '{self._settings.copilot_binary}' not found") from exc
        except subprocess.TimeoutExpired as exc:
            raise HarnessError(
                f"coding harness timed out after {self._settings.copilot_timeout_seconds}s"
            ) from exc

        if result.returncode != 0:
            raise HarnessError(f"coding harness exited with {result.returncode}; inspect worker logs")

        summary = result.stdout.strip()
        log.info("harness_finished", task_id=task.task_id, output_chars=len(summary))
        return summary


def create_harness(settings: Settings, credential: TokenCredential | None = None) -> CodingHarness:
    """Select the worker-side coding implementation without changing pipeline behavior."""
    if settings.harness_provider == "copilot":
        return CopilotHarness(settings)
    if settings.harness_provider == "deepseek":
        from .deepseek_harness import DeepSeekHarness

        return DeepSeekHarness(settings, credential)
    if settings.harness_provider == "dsh":
        from .dsh_harness import DshHarness

        return DshHarness(settings, credential)
    raise HarnessError(f"unknown coding harness provider: {settings.harness_provider}")


def build_prompt(
    task: CodingTask, repository: RepositoryRecord, history: list[dict[str, str]] | None = None
) -> str:
    previous = ""
    if history:
        lines = [f"{item['role']}: {item['content']}" for item in history if item.get("role") in {"user", "assistant"}]
        selected: list[str] = []
        remaining = 24_000
        for line in reversed(lines):
            if len(line) > remaining:
                break
            selected.append(line)
            remaining -= len(line)
        previous = "## Previous conversation (oldest entries may be omitted)\n" + "\n\n".join(reversed(selected)) + "\n\n"
    return _PROMPT_TEMPLATE.format(
        repo_name=repository.name,
        source_path=repository.source_path,
        notes=repository.notes or "(none)",
        history=previous,
        instruction=task.instruction,
    )
