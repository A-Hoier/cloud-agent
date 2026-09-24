from __future__ import annotations

import pytest

from aiops_agent.github import GitHubError, GitHubPullRequests, _github_repository


def test_creates_pull_request_and_enables_auto_merge(monkeypatch):
    requests = []

    def fake_request(self, method, path, payload=None):
        requests.append((method, path, payload))
        if method == "GET":
            return []
        if path == "/graphql":
            return {"data": {"enablePullRequestAutoMerge": {"pullRequest": {"id": "PR_1"}}}}
        return {"html_url": "https://github.com/acme/api/pull/12", "node_id": "PR_1"}

    monkeypatch.setattr(GitHubPullRequests, "_request", fake_request)
    result = GitHubPullRequests("fake-token").ensure(
        "https://github.com/acme/api.git",
        "agent/task/api-123",
        "main",
        "Add pagination",
        "Implemented pagination",
        True,
    )
    assert result.url == "https://github.com/acme/api/pull/12"
    assert result.auto_merge_enabled
    assert requests[1][2]["head"] == "agent/task/api-123"
    assert requests[1][2]["base"] == "main"
    assert requests[2][2]["variables"] == {"id": "PR_1", "method": "SQUASH"}


def test_reuses_existing_pull_request(monkeypatch):
    requests = []

    def fake_request(self, method, path, payload=None):
        requests.append((method, path))
        return [
            {
                "head": {"ref": "agent/task/api-123"},
                "state": "open",
                "html_url": "https://github.com/acme/api/pull/12",
                "auto_merge": {"enabled_by": {"login": "bot"}},
            }
        ]

    monkeypatch.setattr(GitHubPullRequests, "_request", fake_request)
    result = GitHubPullRequests("fake-token").ensure(
        "https://github.com/acme/api.git",
        "agent/task/api-123",
        "main",
        "Add pagination",
        "Implemented pagination",
        True,
    )
    assert result.auto_merge_enabled
    assert len(requests) == 1


def test_merged_pull_request_is_not_reused_for_new_changes(monkeypatch):
    requests = []

    def fake_request(self, method, path, payload=None):
        requests.append(method)
        if method == "GET":
            return [{"head": {"ref": "agent/task/api-123"}, "state": "closed",
                     "merged_at": "2026-09-24T00:00:00Z", "html_url": "https://github.com/acme/api/pull/12"}]
        return {"html_url": "https://github.com/acme/api/pull/13", "node_id": "PR_2"}

    monkeypatch.setattr(GitHubPullRequests, "_request", fake_request)
    result = GitHubPullRequests("fake-token").ensure(
        "https://github.com/acme/api.git", "agent/task/api-123", "main",
        "Follow-up change", "Implemented follow-up", False,
    )
    assert result.url.endswith("/13")
    assert requests == ["GET", "POST"]


def test_auto_merge_failure_keeps_pull_request_url(monkeypatch):
    def fake_request(self, method, path, payload=None):
        if method == "GET":
            return []
        if path == "/graphql":
            return {"errors": [{"message": "Auto merge is disabled"}]}
        return {"html_url": "https://github.com/acme/api/pull/12", "node_id": "PR_1"}

    monkeypatch.setattr(GitHubPullRequests, "_request", fake_request)
    with pytest.raises(GitHubError) as raised:
        GitHubPullRequests("fake-token").ensure(
            "https://github.com/acme/api.git",
            "agent/task/api-123",
            "main",
            "Add pagination",
            "Implemented pagination",
            True,
        )
    assert raised.value.pull_request_url == "https://github.com/acme/api/pull/12"


@pytest.mark.parametrize(
    "url",
    ["http://github.com/acme/api.git", "https://evil.example/acme/api.git", "https://github.com/acme"],
)
def test_github_repo_parser_rejects_unsupported_urls(url):
    with pytest.raises(GitHubError):
        _github_repository(url)
