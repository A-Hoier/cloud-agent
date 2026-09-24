from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from aiops_agent.config import Settings
from aiops_agent.repo import GitError, GitRepository, RepoWorkspace


def _workspace() -> RepoWorkspace:
    settings = Settings.from_env(
        {
            "QUEUE_ACCOUNT_URL": "https://example.queue.core.windows.net",
            "GITHUB_REPOSITORIES": "acme/api",
            "QUEUE_NAME": "tasks",
            "TASK_STATUS_CONTAINER_URL": "https://example.blob.core.windows.net/status",
            "GIT_GITHUB_TOKEN": "fake-secret",
        }
    )
    return RepoWorkspace(settings, object())


def test_github_auth_is_scoped_to_github_host():
    env = _workspace()._auth_env("https://github.com/acme/api.git")
    assert env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    assert "fake-secret" not in env["GIT_CONFIG_VALUE_0"]


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/acme/api.git",
        "https://github.com/acme/api.git?token=secret",
        "http://github.com/acme/api.git",
    ],
)
def test_rejects_unsafe_repository_urls(url):
    with pytest.raises(GitError):
        _workspace()._auth_env(url)


def test_harness_side_git_remote_change_is_rejected(tmp_path: Path):
    def git(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=tmp_path, check=True,
                              capture_output=True, text=True).stdout.strip()

    git("init", "-b", "main")
    (tmp_path / "app.py").write_text("print('ready')\n", encoding="utf-8")
    git("add", "app.py")
    git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "Initial")
    url = "https://github.com/acme/api.git"
    git("remote", "add", "origin", url)
    repo = GitRepository(tmp_path, "main", url, git("rev-parse", "HEAD"), {})
    repo.assert_checkout_intact()
    git("config", "remote.origin.pushurl", "https://github.com/other/repo.git")
    with pytest.raises(GitError, match="push remote"):
        repo.assert_checkout_intact()
