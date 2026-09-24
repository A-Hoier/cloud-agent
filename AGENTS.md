# AGENTS.md

## What this is

`cloud-agent` has two Azure Container Apps roles using one trusted image. A web frontend holds
separate coding sessions for allow-listed repositories and enqueues each message as a turn. A
disposable Container Apps Job handles one turn, cloning the latest base branch for the first turn
or the session branch for follow-ups. It runs the selected coding harness (self-hosted model by
default or Copilot CLI), pushes any code changes, and creates/updates a GitHub PR. Conversation
history and status are persisted in Blob Storage, not in the worker container.

Pipeline: frontend → queue → repository registry → clone → coding harness → commit/push → GitHub PR.

## Layout

| Path | Purpose |
| --- | --- |
| [src/aiops_agent/web.py](src/aiops_agent/web.py) | Frontend, repository selection, task queueing and status |
| [src/aiops_agent/__main__.py](src/aiops_agent/__main__.py) | Worker entrypoint, queue retry/poison handling |
| [src/aiops_agent/pipeline.py](src/aiops_agent/pipeline.py) | Runs one coding task |
| [src/aiops_agent/repository_registry.py](src/aiops_agent/repository_registry.py) | Repository allow-list |
| [src/aiops_agent/status.py](src/aiops_agent/status.py) | Blob-backed task status |
| [src/aiops_agent/session.py](src/aiops_agent/session.py) | Blob-backed session conversation and turn concurrency |
| [src/aiops_agent/repo.py](src/aiops_agent/repo.py) | Clone, branch, commit, push |
| [src/aiops_agent/github.py](src/aiops_agent/github.py) | GitHub PR creation and optional auto merge |
| [src/aiops_agent/harness.py](src/aiops_agent/harness.py) | Shared harness interface and Copilot CLI implementation |
| [src/aiops_agent/deepseek_harness.py](src/aiops_agent/deepseek_harness.py) | Self-hosted/Foundry model coding agent with file/test tools |
| [src/aiops_agent/dsh_harness.py](src/aiops_agent/dsh_harness.py) | Optional upstream DeepSeek Harness headless adapter |
| [config/repositories.json](config/repositories.json) | Example allow-list |
| [tests/integration/](tests/integration) | Connectivity checks against real resources |

## Commands

```bash
python -m pip install -e ".[dev]"
python -m pytest -m "not integration"
ruff check .
docker build -t cloud-agent:dev .
```

Integration tests need `AIOPS_INTEGRATION=1` and `AIOPS_IT_REPOSITORY`; see [README.md](README.md).

## Conventions

- Python 3.12, `from __future__ import annotations`, dataclasses for domain objects, 110-char lines.
- All Azure access goes through `DefaultAzureCredential` — no connection strings.
- Structured JSON logging via `structlog`; event names are snake_case (`task_handled`).
- Config is environment-only, parsed in [config.py](src/aiops_agent/config.py). Add new knobs there,
  to [.env.example](.env.example), and to the README. Never commit secrets.

## Rules that matter

- Never put a token in argv or in a URL. Git auth uses scoped `GIT_CONFIG_*` environment settings;
  Git stderr passes through `_redact()` before logging.
- The coding harness must not run Git write commands. The prompt forbids it and the harness process
  does not inherit the Git push token. The wrapper owns branch creation, commit, and push.
- No harness changes means no commit and no branch.
- Session IDs are stable across turns; task IDs are unique per turn and stable across retries.
- Only one turn may be active in a session. Follow-ups clone the session branch, not the base branch.
- API requests require ACA Easy Auth and owner checks; never expose another user's session or task.
- Unparseable queue messages are poison: delete immediately, never retry.
- Exit codes: `0` all handled, `1` at least one failure, `2` startup/config failure.
- Unit tests stay offline. Use `PUSH_ENABLED=false` to test without pushing.
