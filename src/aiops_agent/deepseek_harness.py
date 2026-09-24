"""Tool-calling coding agent backed by a self-hosted chat-completions endpoint."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from azure.core.credentials import TokenCredential
from azure.identity import DefaultAzureCredential

from .config import Settings
from .harness import CodingHarness, HarnessError, build_prompt
from .logging_setup import get_logger
from .models import CodingTask, RepositoryRecord

log = get_logger(__name__)

_MAX_FILE_CHARS = 100_000
_MAX_TOOL_OUTPUT = 12_000
_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List tracked and untracked (non-ignored) files in the checkout.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_text",
            "description": "Search repository text for a literal phrase; returns file names and line numbers.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a UTF-8 text file relative to the checkout root.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or replace a UTF-8 text file relative to the checkout root.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_file",
            "description": "Delete one checkout-relative file. Do not use for generated or unrelated files.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "replace_text",
            "description": "Replace exactly one occurrence of old text in a UTF-8 file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old": {"type": "string"},
                    "new": {"type": "string"},
                },
                "required": ["path", "old", "new"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_check",
            "description": (
                "Run a test or build command in the checkout without a shell. Supported: "
                "pytest, ruff, python -m pytest, npm test, npm run <script>, "
                "pnpm test, yarn test, go test, cargo test, dotnet test."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "argv": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["argv"],
            },
        },
    },
]


class DeepSeekHarness(CodingHarness):
    def __init__(self, settings: Settings, credential: TokenCredential | None = None) -> None:
        if not settings.model_endpoint:
            raise HarnessError("MODEL_ENDPOINT is required when HARNESS_PROVIDER=deepseek")
        if not settings.model_name:
            raise HarnessError("MODEL_NAME is required when HARNESS_PROVIDER=deepseek")
        parsed = urlsplit(settings.model_endpoint)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or not parsed.path.endswith("/chat/completions")
            or parsed.hostname == "api.deepseek.com"
        ):
            raise HarnessError(
                "MODEL_ENDPOINT must be a self-hosted HTTPS /chat/completions URL "
                "without credentials or query"
            )
        if settings.model_auth_mode not in {"auto", "bearer", "api-key"}:
            raise HarnessError("MODEL_AUTH_MODE must be 'auto', 'bearer' or 'api-key'")
        if settings.deepseek_max_steps < 1 or settings.deepseek_timeout_seconds < 1:
            raise HarnessError("DeepSeek step and timeout limits must be positive")
        self._settings = settings
        self._endpoint = settings.model_endpoint
        self._azure_endpoint = bool(parsed.hostname and parsed.hostname.endswith(
            (".openai.azure.com", ".services.ai.azure.com")
        ))
        if not settings.model_api_key and not self._azure_endpoint:
            raise HarnessError("MODEL_API_KEY is required outside Microsoft Foundry endpoints")
        self._token_scope = settings.model_token_scope
        if self._token_scope == "auto":
            self._token_scope = (
                "https://ai.azure.com/.default"
                if "/api/projects/" in parsed.path
                else "https://cognitiveservices.azure.com/.default"
            )
        self._credential = credential or (DefaultAzureCredential() if not settings.model_api_key else None)

    def run(
        self,
        repo_path: Path,
        task: CodingTask,
        repository: RepositoryRecord,
        history: list[dict[str, str]] | None = None,
    ) -> str:
        root = repo_path.resolve(strict=True)
        deadline = time.monotonic() + self._settings.deepseek_timeout_seconds
        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": (
                    "You are a coding agent with file and validation tools. Use tools to inspect and "
                    "edit the checkout. Never run Git write commands. Finish only after checking "
                    "your changes, or explain why no safe change was possible."
                ),
            },
            {"role": "user", "content": build_prompt(task, repository, history)},
        ]
        log.info("harness_started", provider="deepseek", task_id=task.task_id, repository=repository.key)
        for step in range(self._settings.deepseek_max_steps):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HarnessError("DeepSeek harness timed out")
            message = self._request(messages, min(120, remaining))
            calls = message.get("tool_calls") or []
            if not isinstance(calls, list):
                raise HarnessError("DeepSeek returned invalid tool calls")
            if len(calls) > 8:
                raise HarnessError("DeepSeek returned too many tool calls in one turn")
            if not calls:
                summary = message.get("content")
                if not isinstance(summary, str):
                    raise HarnessError("DeepSeek returned no final summary")
                log.info("harness_finished", provider="deepseek", task_id=task.task_id, steps=step + 1)
                return summary.strip()

            assistant_message = {"role": "assistant", "content": message.get("content"), "tool_calls": calls}
            if message.get("reasoning_content") is not None:
                assistant_message["reasoning_content"] = message["reasoning_content"]
            messages.append(assistant_message)
            for call in calls:
                if not isinstance(call, dict) or not isinstance(call.get("id"), str):
                    raise HarnessError("DeepSeek returned a malformed tool call")
                function = call.get("function")
                if not isinstance(function, dict):
                    raise HarnessError("DeepSeek returned a malformed tool function")
                name = function.get("name")
                if not isinstance(name, str):
                    raise HarnessError("DeepSeek returned a tool without a name")
                if time.monotonic() >= deadline:
                    raise HarnessError("DeepSeek harness timed out")
                try:
                    args = json.loads(function.get("arguments", "{}"))
                    if not isinstance(args, dict):
                        raise TypeError("tool arguments must be an object")
                    output = self._run_tool(root, name, args, deadline)
                except (OSError, UnicodeError, ValueError, TypeError) as exc:
                    output = f"Tool error: {exc}"
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": output})
                log.info("harness_tool_called", task_id=task.task_id, tool=name)
        raise HarnessError(f"DeepSeek harness exceeded {self._settings.deepseek_max_steps} steps")

    def _request(self, messages: list[dict[str, Any]], timeout: float) -> dict[str, Any]:
        payload = json.dumps(
            {"model": self._settings.model_name, "messages": messages, "tools": _TOOLS},
            ensure_ascii=False,
        ).encode("utf-8")
        if self._settings.model_api_key:
            key_header = self._settings.model_auth_mode == "api-key" or (
                self._settings.model_auth_mode == "auto" and self._azure_endpoint
            )
            auth_header = (
                {"api-key": self._settings.model_api_key}
                if key_header
                else {"Authorization": f"Bearer {self._settings.model_api_key}"}
            )
        else:
            assert self._credential is not None
            auth_header = {
                "Authorization": f"Bearer {self._credential.get_token(self._token_scope).token}"
            }
        request = Request(
            self._endpoint,
            data=payload,
            headers={
                "Content-Type": "application/json",
                **auth_header,
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                data = json.load(response)
        except HTTPError as exc:
            raise HarnessError(f"model endpoint returned HTTP {exc.code}") from exc
        except (URLError, TimeoutError) as exc:
            raise HarnessError("model endpoint request failed or timed out") from exc
        except (ValueError, UnicodeError) as exc:
            raise HarnessError("model endpoint returned invalid JSON") from exc
        try:
            message = data["choices"][0]["message"]
            if not isinstance(message, dict):
                raise TypeError
            return message
        except (KeyError, IndexError, TypeError) as exc:
            raise HarnessError("model endpoint returned an invalid completion") from exc

    def _run_tool(self, root: Path, name: str, args: dict[str, Any], deadline: float) -> str:
        if name == "list_files":
            result = subprocess.run(
                ["git", "ls-files", "-co", "--exclude-standard"],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=min(30, max(1, deadline - time.monotonic())),
                check=False,
            )
            if result.returncode:
                return "Could not list repository files"
            return _truncate(result.stdout)
        if name == "read_file":
            path = _safe_path(root, args.get("path"))
            if path.stat().st_size > _MAX_FILE_CHARS:
                raise ValueError("file is too large to read")
            return _truncate(path.read_text(encoding="utf-8"))
        if name == "search_text":
            query = args.get("query")
            if not isinstance(query, str) or not query or len(query) > 200:
                raise ValueError("query must be 1–200 characters")
            result = subprocess.run(
                ["rg", "--fixed-strings", "--line-number", "--max-count", "5", "--", query, "."],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=min(20, max(1, deadline - time.monotonic())),
                check=False,
            )
            if result.returncode not in {0, 1}:
                return "Search failed"
            return _truncate(result.stdout) if result.stdout else "No matches"
        if name == "write_file":
            path = _safe_path(root, args.get("path"))
            content = args.get("content")
            if not isinstance(content, str) or len(content) > _MAX_FILE_CHARS:
                raise ValueError("content must be text of at most 100,000 characters")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            return f"Wrote {path.relative_to(root)}"
        if name == "delete_file":
            path = _safe_path(root, args.get("path"))
            if not path.is_file():
                raise ValueError("path is not a file")
            path.unlink()
            return f"Deleted {path.relative_to(root)}"
        if name == "replace_text":
            path = _safe_path(root, args.get("path"))
            old, new = args.get("old"), args.get("new")
            if not isinstance(old, str) or not old or not isinstance(new, str):
                raise ValueError("old and new must be strings; old must be nonempty")
            content = path.read_text(encoding="utf-8")
            if len(content) > _MAX_FILE_CHARS or len(new) > _MAX_FILE_CHARS:
                raise ValueError("file or replacement is too large")
            if content.count(old) != 1:
                raise ValueError("old text must occur exactly once")
            path.write_text(content.replace(old, new, 1), encoding="utf-8")
            return f"Updated {path.relative_to(root)}"
        if name == "run_check":
            return _run_check(root, args.get("argv"), deadline)
        raise ValueError(f"unknown tool: {name}")


def _safe_path(root: Path, raw: Any) -> Path:
    if not isinstance(raw, str) or not raw or Path(raw).is_absolute() or ".git" in Path(raw).parts:
        raise ValueError("path must be a checkout-relative file outside .git")
    path = (root / raw).resolve()
    if not path.is_relative_to(root) or path == root:
        raise ValueError("path escapes the checkout")
    return path


def _run_check(root: Path, raw: Any, deadline: float) -> str:
    if not isinstance(raw, list) or not raw or any(not isinstance(arg, str) for arg in raw):
        raise ValueError("argv must be a nonempty list of strings")
    argv = raw
    allowed = (
        argv[0] in {"pytest", "ruff"}
        or argv[:3] == ["python", "-m", "pytest"]
        or argv[:3] == ["python3", "-m", "pytest"]
        or (argv[0] in {"npm", "pnpm", "yarn"} and argv[1:2] in (["test"], ["run"]))
        or (argv[0] in {"go", "cargo", "dotnet"} and argv[1:2] == ["test"])
    )
    if not allowed or len(argv) > 20 or any(len(arg) > 200 for arg in argv):
        raise ValueError("command is not an allowed test or build command")
    env = {key: os.environ[key] for key in ("PATH", "HOME", "LANG") if key in os.environ}
    env["CI"] = "true"
    try:
        result = subprocess.run(
            argv,
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=min(180, max(1, deadline - time.monotonic())),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return "Check timed out"
    return _truncate(f"Exit code: {result.returncode}\n{result.stdout}\n{result.stderr}")


def _truncate(value: str) -> str:
    if len(value) <= _MAX_TOOL_OUTPUT:
        return value
    return value[:_MAX_TOOL_OUTPUT] + "\n[output truncated]"
