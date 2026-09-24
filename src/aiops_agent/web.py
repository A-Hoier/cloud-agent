"""Small web frontend that validates and queues coding tasks."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated
from uuid import uuid4

from azure.core.exceptions import ResourceNotFoundError
from azure.identity import DefaultAzureCredential
from azure.storage.queue import QueueClient
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from .config import Settings
from .models import CodingTask, RepositoryRecord
from .repository_registry import RepositoryNotRegisteredError, RepositoryRegistry
from .session import SessionConflictError, SessionStore
from .status import TaskStatusStore


class TaskRequest(BaseModel):
    repository: Annotated[str, Field(min_length=1, max_length=200)]
    instruction: Annotated[str, Field(min_length=1, max_length=20_000)]
    target_branch: Annotated[str | None, Field(max_length=200)] = None
    merge_when_ready: bool = False
    direct_to_main: bool = False


class SessionRequest(BaseModel):
    repository: Annotated[str, Field(min_length=1, max_length=200)]
    target_branch: Annotated[str | None, Field(max_length=200)] = None
    direct_to_main: bool = False


class MessageRequest(BaseModel):
    instruction: Annotated[str, Field(min_length=1, max_length=20_000)]


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings.from_env()
    credential = DefaultAzureCredential()
    queue = None
    status_store = None
    session_store = None
    try:
        queue = QueueClient(
            account_url=settings.queue_account_url,
            queue_name=settings.queue_name,
            credential=credential,
        )
        app.state.registry = RepositoryRegistry.load(settings, credential)
        status_store = TaskStatusStore(settings.status_container_url, credential)
        session_store = SessionStore(settings.status_container_url, credential)
        app.state.queue = queue
        app.state.status_store = status_store
        app.state.session_store = session_store
        yield
    finally:
        if status_store is not None:
            status_store.close()
        if session_store is not None:
            session_store.close()
        if queue is not None:
            queue.close()
        credential.close()


app = FastAPI(title="Coding Agent", lifespan=lifespan)


def _require_owner(request: Request) -> str:
    """Trust only the identity header injected by Container Apps Easy Auth.

    Container Apps strips client-supplied X-MS-CLIENT-PRINCIPAL-* headers. The deployment
    must enable built-in authentication and require sign-in for all routes.
    """
    owner = request.headers.get("x-ms-client-principal-id", "").strip()
    if not owner or len(owner) > 200:
        raise HTTPException(status_code=401, detail="authentication required")
    return owner


def main() -> None:
    import uvicorn

    uvicorn.run("aiops_agent.web:app", host="0.0.0.0", port=8000)


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return _INDEX_HTML


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/repositories")
def list_repositories(owner_id: str = Depends(_require_owner)) -> list[dict[str, str | bool]]:
    return [
        {
            "id": item.key,
            "name": item.name,
            "default_branch": item.default_branch,
            "source_path": item.source_path,
            "notes": item.notes or "",
            "supports_auto_merge": item.repo_url.startswith("https://github.com/"),
        }
        for item in app.state.registry.records
    ]


@app.post("/api/tasks", status_code=202)
def create_task(request: TaskRequest, owner_id: str = Depends(_require_owner)) -> dict[str, str]:
    try:
        repository = app.state.registry.resolve(request.repository)
    except RepositoryNotRegisteredError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if request.merge_when_ready and not repository.repo_url.startswith("https://github.com/"):
        raise HTTPException(status_code=400, detail="auto merge is available only for GitHub repositories")
    if request.direct_to_main:
        _validate_direct_to_main(repository, request.target_branch, request.merge_when_ready)
    if not request.instruction.strip():
        raise HTTPException(status_code=400, detail="instruction cannot be blank")
    if len(request.instruction.encode("utf-8")) > 48_000:
        raise HTTPException(status_code=400, detail="instruction exceeds 48000 UTF-8 bytes")

    task = CodingTask(
        task_id=str(uuid4()),
        repository=repository.key,
        instruction=request.instruction.strip(),
        created_at=datetime.now(UTC),
        target_branch=request.target_branch.strip() if request.target_branch else None,
        merge_when_ready=request.merge_when_ready,
        direct_to_main=request.direct_to_main,
        owner_id=owner_id,
    )
    app.state.status_store.write(task.task_id, repository=task.repository, owner_id=owner_id, state="queued")
    try:
        app.state.queue.send_message(
            json.dumps(
                {
                    "task_id": task.task_id,
                    "repository": task.repository,
                    "instruction": task.instruction,
                    "created_at": task.created_at.isoformat(),
                    "target_branch": task.target_branch,
                    "merge_when_ready": task.merge_when_ready,
                    "direct_to_main": task.direct_to_main,
                    "owner_id": owner_id,
                },
                ensure_ascii=False,
            )
        )
    except Exception as exc:
        app.state.status_store.write(task.task_id, repository=task.repository, owner_id=owner_id, state="failed")
        raise HTTPException(status_code=503, detail="could not queue task") from exc
    return {"task_id": task.task_id, "status": "queued", "repository": task.repository}


@app.get("/api/tasks/{task_id}")
def get_task(task_id: str, owner_id: str = Depends(_require_owner)) -> dict[str, object]:
    try:
        return app.state.status_store.read_for_owner(task_id, owner_id)
    except (ResourceNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="task not found") from exc


@app.post("/api/sessions", status_code=201)
def create_session(request: SessionRequest, owner_id: str = Depends(_require_owner)) -> dict[str, object]:
    if request.target_branch is not None and not request.target_branch.strip():
        raise HTTPException(status_code=400, detail="base branch cannot be blank")
    try:
        repository = app.state.registry.resolve(request.repository)
    except RepositoryNotRegisteredError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if request.direct_to_main:
        _validate_direct_to_main(repository, request.target_branch or repository.default_branch, False)
    return app.state.session_store.create(
        repository.key, request.target_branch.strip() if request.target_branch else repository.default_branch,
        owner_id=owner_id,
        direct_to_main=request.direct_to_main,
    )


@app.get("/api/sessions")
def list_sessions(owner_id: str = Depends(_require_owner)) -> list[dict[str, object]]:
    return app.state.session_store.list_for_owner(owner_id)


@app.get("/api/sessions/{session_id}")
def get_session(session_id: str, owner_id: str = Depends(_require_owner)) -> dict[str, object]:
    try:
        return app.state.session_store.read_for_owner(session_id, owner_id)
    except (ResourceNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="session not found") from exc


@app.post("/api/sessions/{session_id}/messages", status_code=202)
def send_session_message(
    session_id: str, request: MessageRequest, owner_id: str = Depends(_require_owner)
) -> dict[str, str]:
    instruction = request.instruction.strip()
    if not instruction or len(instruction.encode("utf-8")) > 48_000:
        raise HTTPException(status_code=400, detail="instruction must be 1–48000 UTF-8 bytes")
    try:
        task = app.state.session_store.queue_turn(session_id, instruction, owner_id=owner_id)
    except (ResourceNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="session not found") from exc
    except SessionConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    try:
        app.state.status_store.write(
            task.task_id, repository=task.repository, owner_id=owner_id, state="queued"
        )
        app.state.queue.send_message(
            json.dumps(
                {
                    "task_id": task.task_id,
                    "session_id": task.session_id,
                    "repository": task.repository,
                    "instruction": task.instruction,
                    "created_at": task.created_at.isoformat(),
                    "target_branch": task.target_branch,
                    "merge_when_ready": False,
                    "direct_to_main": task.direct_to_main,
                    "owner_id": owner_id,
                },
                ensure_ascii=False,
            )
        )
    except Exception as exc:
        app.state.session_store.fail_turn(task, "Could not queue the message. Please try again.")
        app.state.status_store.write(
            task.task_id, repository=task.repository, owner_id=owner_id, state="failed"
        )
        raise HTTPException(status_code=503, detail="could not queue message") from exc
    return {"session_id": session_id, "task_id": task.task_id, "status": "queued"}


def _validate_direct_to_main(
    repository: RepositoryRecord, target_branch: str | None, merge_when_ready: bool
) -> None:
    if not repository.repo_url.startswith("https://github.com/"):
        raise HTTPException(status_code=400, detail="direct-to-main is available only for GitHub repositories")
    if (target_branch or repository.default_branch).strip() != "main":
        raise HTTPException(status_code=400, detail="direct-to-main requires the main base branch")
    if merge_when_ready:
        raise HTTPException(status_code=400, detail="direct-to-main cannot be combined with auto merge")


_INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Coding Agent</title>
  <script>
    try {
      if (localStorage.getItem('theme') === 'light') document.documentElement.dataset.theme = 'light';
    } catch (error) { /* Storage may be unavailable; keep the default dark theme. */ }
  </script>
  <style>
    :root { color-scheme: dark; font-family: ui-sans-serif, system-ui, sans-serif;
      --page: #0b1020; --text: #e8edf8; --card: #131b31; --border: #263452;
      --muted: #aebbd3; --field-border: #3a4967; --surface: #0c1428;
      --accent: #6d7cff; --accent-text: #fff; --user: #6d7cff;
      --assistant: #3bbf9b; --code: #b9c2ff; }
    :root[data-theme="light"] { color-scheme: light;
      --page: #f4f6fb; --text: #19253b; --card: #fff; --border: #cbd4e3;
      --muted: #44536a; --field-border: #8998ae; --surface: #f0f3f9;
      --accent: #3446b9; --accent-text: #fff; --user: #3446b9;
      --assistant: #17765b; --code: #3446b9; }
    body { margin: 0; background: var(--page); color: var(--text); }
    main { max-width: 760px; margin: 8vh auto; padding: 0 24px; }
    .card { background: var(--card); border: 1px solid var(--border); border-radius: 16px; padding: 28px; }
    .card-heading { display: flex; flex-wrap: wrap; align-items: start; justify-content: space-between; gap: 16px; }
    h1 { margin-top: 0; font-size: 30px; }
    p { color: var(--muted); line-height: 1.55; }
    label { display: block; margin: 20px 0 8px; font-weight: 650; }
    select, textarea, input { box-sizing: border-box; width: 100%; border: 1px solid var(--field-border);
      border-radius: 9px; background: var(--surface); color: inherit; padding: 12px; font: inherit; }
    input[type=checkbox] { width: auto; margin-right: 8px; }
    textarea { min-height: 220px; resize: vertical; }
    button { margin-top: 20px; border: 0; border-radius: 9px; padding: 12px 18px;
      background: var(--accent); color: var(--accent-text); font: inherit; font-weight: 700; cursor: pointer; }
    button:disabled { opacity: .55; cursor: wait; }
    #theme-toggle { margin-top: 0; background: var(--surface); color: var(--text);
      border: 1px solid var(--field-border); white-space: nowrap; }
    #result { margin-top: 18px; padding: 12px; border-radius: 9px; background: var(--surface); display: none; }
    #chat { margin-top: 20px; display: grid; gap: 12px; }
    .message { white-space: pre-wrap; background: var(--surface); padding: 14px; border-radius: 9px; }
    .message.user { border-left: 3px solid var(--user); }
    .message.assistant { border-left: 3px solid var(--assistant); }
    code { color: var(--code); }
  </style>
</head>
<body><main><div class="card">
  <div class="card-heading">
    <h1>Coding sessions</h1>
    <button type="button" id="theme-toggle" aria-label="Switch to light theme">Light theme</button>
  </div>
  <p>Each message runs in a fresh, isolated job. The conversation is saved, and code changes stay
  on this session's Git branch, or go directly to main if you explicitly choose that mode.</p>
  <label for="sessions">Session</label><select id="sessions"></select>
  <label for="repository">Repository for a new session</label><select id="repository" required></select>
  <label for="branch">Base branch for a new session <small>(optional)</small></label>
  <input id="branch" placeholder="Uses the repository default">
  <label><input id="direct-to-main" type="checkbox"> Commit directly to main (no pull request)</label>
  <button id="new-session" type="button">Start new session</button>
  <div id="session-status"></div>
  <div id="chat"></div>
  <form id="message-form">
    <label for="instruction">Message</label>
    <textarea id="instruction" required maxlength="20000"
      placeholder="Ask for a feature, a follow-up change, or an explanation..."></textarea>
    <button id="submit" type="submit">Send message</button>
  </form>
  <div id="result"></div>
</div></main>
<script>
const themeToggle = document.querySelector('#theme-toggle');
function updateThemeToggle() {
  const next = document.documentElement.dataset.theme === 'light' ? 'dark' : 'light';
  themeToggle.textContent = `${next[0].toUpperCase()}${next.slice(1)} theme`;
  themeToggle.setAttribute('aria-label', `Switch to ${next} theme`);
}
updateThemeToggle();
themeToggle.addEventListener('click', () => {
  const next = document.documentElement.dataset.theme === 'light' ? 'dark' : 'light';
  if (next === 'dark') delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = 'light';
  try { localStorage.setItem('theme', next); } catch (error) { /* Keep the in-page choice. */ }
  updateThemeToggle();
});
const form = document.querySelector('#message-form');
const repos = document.querySelector('#repository');
const sessions = document.querySelector('#sessions');
const chat = document.querySelector('#chat');
const sessionStatus = document.querySelector('#session-status');
const result = document.querySelector('#result');
const button = document.querySelector('#submit');
let sessionIds = [];
let sessionNames = {};
const linkedSession = new URLSearchParams(location.search).get('session');
async function loadSessions() {
  const response = await fetch('/api/sessions');
  if (!response.ok) throw new Error(`Could not load sessions (${response.status})`);
  const available = await response.json();
  sessionIds = available.map(item => item.session_id);
  sessionNames = Object.fromEntries(available.map(item =>
    [item.session_id, `${item.repository} · ${item.session_id.slice(0, 8)}`]));
  if (sessionIds.length) rememberSession(
    linkedSession && sessionIds.includes(linkedSession) ? linkedSession : sessionIds[0]);
}
async function loadRepositories() {
  const response = await fetch('/api/repositories');
  const available = await response.json();
  for (const repo of available) {
    const option = document.createElement('option');
    option.value = repo.id;
    option.textContent = `${repo.name} (${repo.default_branch})`;
    repos.appendChild(option);
  }
}
function showMessage(message) {
  result.style.display = 'block'; result.textContent = message;
}
function rememberSession(id) {
  sessionIds = [id, ...sessionIds.filter(item => item !== id)].slice(0, 20);
  sessions.replaceChildren();
  for (const sessionId of sessionIds) {
    const option = document.createElement('option');
    option.value = sessionId;
    option.textContent = sessionNames[sessionId] || sessionId.slice(0, 8);
    sessions.appendChild(option);
  }
  sessions.value = id;
  history.replaceState({}, '', `?session=${encodeURIComponent(id)}`);
}
async function refreshSession() {
  if (!sessions.value) { sessionStatus.textContent = 'Start a session to begin.'; return; }
  const selected = sessions.value;
  const response = await fetch(`/api/sessions/${encodeURIComponent(selected)}`);
  if (!response.ok) { sessionStatus.textContent = 'Session unavailable.'; return; }
  const session = await response.json();
  if (sessions.value !== selected) return;
  sessionNames[selected] = `${session.repository} · ${selected.slice(0, 8)}`;
  sessions.selectedOptions[0].textContent = sessionNames[selected];
  sessionStatus.replaceChildren();
  const label = document.createElement('p');
  label.textContent = `${session.repository} · ${session.state}` +
    (session.branch ? ` · ${session.branch}` : '') +
    (session.direct_to_main ? ' · direct to main' : '');
  sessionStatus.appendChild(label);
  if (session.pull_request_url?.startsWith('https://github.com/')) {
    const anchor = document.createElement('a');
    anchor.href = session.pull_request_url;
    anchor.textContent = 'Open pull request';
    anchor.rel = 'noopener noreferrer'; anchor.target = '_blank';
    sessionStatus.appendChild(anchor);
  }
  chat.replaceChildren();
  for (const message of session.messages) {
    const card = document.createElement('div');
    card.className = `message ${message.role}`;
    card.textContent = `${message.role === 'user' ? 'You' : 'Agent'}\n${message.content}`;
    chat.appendChild(card);
  }
  button.disabled = Boolean(session.active_task_id);
}
document.querySelector('#new-session').addEventListener('click', async () => {
  result.style.display = 'none';
  const response = await fetch('/api/sessions', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({repository: repos.value,
      target_branch: document.querySelector('#branch').value || null,
      direct_to_main: document.querySelector('#direct-to-main').checked})});
  const body = await response.json();
  if (!response.ok) { showMessage(body.detail || 'Could not start session'); return; }
  rememberSession(body.session_id);
  await refreshSession();
});
sessions.addEventListener('change', () => { rememberSession(sessions.value); refreshSession(); });
form.addEventListener('submit', async event => {
  event.preventDefault(); button.disabled = true; result.style.display = 'none';
  if (!sessions.value) { showMessage('Start a session first.'); button.disabled = false; return; }
  const response = await fetch(`/api/sessions/${encodeURIComponent(sessions.value)}/messages`,
    {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({instruction: document.querySelector('#instruction').value})});
  const body = await response.json();
  if (response.ok) {
    document.querySelector('#instruction').value = '';
    showMessage('Message queued. A fresh worker is starting...');
    await refreshSession();
  } else {
    showMessage(`Could not send message: ${body.detail || response.statusText}`);
    button.disabled = false;
  }
});
setInterval(() => refreshSession().catch(() => {}), 4000);
Promise.all([loadRepositories(), loadSessions()])
  .then(() => refreshSession())
  .catch(error => showMessage(String(error)));
</script></body></html>"""
