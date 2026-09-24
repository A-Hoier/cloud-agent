"""Git operations against GitHub and Azure DevOps repositories.

Credentials are injected through `GIT_CONFIG_*` environment variables so the
token never lands in argv (visible via `ps`) or in the cloned `.git/config`.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from azure.core.credentials import TokenCredential

from .config import Settings
from .logging_setup import get_logger

log = get_logger(__name__)

# Well-known first-party application id for Azure DevOps.
AZURE_DEVOPS_SCOPE = "499b84ac-1321-427f-aa17-267ca6975798/.default"


class GitError(RuntimeError):
    """Raised when a git command fails."""


@dataclass
class GitRepository:
    path: Path
    default_branch: str
    repo_url: str
    initial_head: str
    _env: dict[str, str]
    initial_config_hash: str | None = None

    def assert_checkout_intact(self) -> None:
        """Reject harness-side Git history, branch, or remote changes before committing."""
        if self._git("rev-parse", "HEAD").stdout.strip() != self.initial_head:
            raise GitError("coding harness changed Git history")
        if self._git("branch", "--show-current").stdout.strip() != self.default_branch:
            raise GitError("coding harness changed the checkout branch")
        if self._git("remote", "get-url", "origin").stdout.strip() != self.repo_url:
            raise GitError("coding harness changed the Git remote")
        if self._git("remote", "get-url", "--push", "origin").stdout.strip() != self.repo_url:
            raise GitError("coding harness changed the Git push remote")
        if self.initial_config_hash and _config_hash(self.path) != self.initial_config_hash:
            raise GitError("coding harness changed Git configuration")

    def create_branch(self, name: str) -> None:
        self._git("checkout", "-b", name)

    def changed_files(self) -> list[str]:
        output = self._git("status", "--porcelain").stdout
        return [line[3:].strip() for line in output.splitlines() if line.strip()]

    def has_task_commit(self, task_id: str) -> bool:
        """Detect a retry after this turn's commit was already pushed."""
        message = self._git("log", "-1", "--format=%B").stdout
        return f"Task: {task_id}" in message.splitlines()

    def task_commit_summary(self) -> str:
        """Recover the agent's report from the commit body after a push/PR retry."""
        message = self._git("log", "-1", "--format=%B").stdout
        sections = message.split("\n\n", 2)
        return sections[2].strip() if len(sections) == 3 else "Existing turn commit reused"

    def commit_and_push(self, branch: str, message: str, settings: Settings) -> bool:
        if not self.changed_files():
            return False
        self._git("add", "--all")
        self._git(
            "-c",
            f"user.name={settings.git_author_name}",
            "-c",
            f"user.email={settings.git_author_email}",
            "commit",
            "--message",
            message,
        )
        if not settings.push_enabled or settings.dry_run:
            log.warning("push_skipped", branch=branch, dry_run=settings.dry_run)
            return False
        self._git("push", "--set-upstream", "origin", branch)
        return True

    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return _run_git(args, cwd=self.path, env=self._env)


class RepoWorkspace:
    """Clones repositories into an isolated working directory."""

    def __init__(self, settings: Settings, credential: TokenCredential) -> None:
        self._settings = settings
        self._credential = credential
        self._root = Path(settings.workdir) / "repos"

    def clone(self, repo_url: str, default_branch: str) -> GitRepository:
        target = self._root / _slug(repo_url)
        if target.exists():
            shutil.rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)

        env = self._auth_env(repo_url)
        _run_git(
            ("clone", "--depth", "50", "--branch", default_branch, repo_url, str(target)),
            cwd=target.parent,
            env=env,
        )
        log.info("repo_cloned", repo=repo_url, branch=default_branch, path=str(target))
        initial_head = _run_git(("rev-parse", "HEAD"), cwd=target, env=env).stdout.strip()
        return GitRepository(
            path=target, default_branch=default_branch, repo_url=repo_url,
            initial_head=initial_head, _env=env, initial_config_hash=_config_hash(target)
        )

    def cleanup(self) -> None:
        shutil.rmtree(self._root, ignore_errors=True)

    def ls_remote(self, repo_url: str, branch: str) -> list[str]:
        """Reachability/read-access probe that does not transfer any objects."""
        self._root.mkdir(parents=True, exist_ok=True)
        result = _run_git(
            ("ls-remote", "--heads", repo_url, branch),
            cwd=self._root,
            env=self._auth_env(repo_url),
        )
        return [line.split("\t")[-1] for line in result.stdout.splitlines() if line.strip()]

    def _auth_env(self, repo_url: str) -> dict[str, str]:
        parsed = urlparse(repo_url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise GitError("repository clone URL must be HTTPS without embedded credentials or query data")
        if host == "github.com":
            if not self._settings.github_token:
                if not self._settings.push_enabled or self._settings.dry_run:
                    return {"GIT_TERMINAL_PROMPT": "0"}
                raise GitError("GIT_GITHUB_TOKEN (or GH_TOKEN) is required to push to GitHub")
            encoded = base64.b64encode(f"x-access-token:{self._settings.github_token}".encode()).decode()
            header = f"AUTHORIZATION: Basic {encoded}"
        elif host == "dev.azure.com" or host.endswith(".visualstudio.com"):
            if self._settings.azure_devops_pat:
                encoded = base64.b64encode(f":{self._settings.azure_devops_pat}".encode()).decode()
                header = f"AUTHORIZATION: Basic {encoded}"
            else:
                token = self._credential.get_token(AZURE_DEVOPS_SCOPE).token
                header = f"AUTHORIZATION: Bearer {token}"
        else:
            raise GitError(f"unsupported repository host: {host}")
        return {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": f"http.https://{host}/.extraheader",
            "GIT_CONFIG_VALUE_0": header,
            "GIT_TERMINAL_PROMPT": "0",
        }


def build_branch_name(prefix: str, repository_key: str, task_id: str) -> str:
    return f"{prefix}/{_slug(repository_key)}-{_slug(task_id)[:12]}"


def _run_git(
    args: tuple[str, ...] | list[str], cwd: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    merged = {key: value for key, value in os.environ.items() if not key.startswith("GIT_CONFIG_")}
    merged.update(env)
    merged["GIT_CONFIG_NOSYSTEM"] = "1"
    merged["GIT_CONFIG_GLOBAL"] = "/dev/null"
    result = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", *args],
        cwd=cwd,
        env=merged,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise GitError(f"git {args[0]} failed ({result.returncode}): {_redact(result.stderr)}")
    return result


def _redact(text: str) -> str:
    return re.sub(r"(?i)(bearer|basic)\s+[A-Za-z0-9._\-=+/]+", r"\1 <redacted>", text).strip()


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9._-]+", "-", value.lower()).strip("-")[:60] or "repo"


def _config_hash(path: Path) -> str:
    return hashlib.sha256((path / ".git" / "config").read_bytes()).hexdigest()
