"""Create GitHub pull requests and optionally enable branch-protected auto merge."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen


class GitHubError(RuntimeError):
    """Raised when a GitHub pull request operation fails."""

    def __init__(self, message: str, pull_request_url: str | None = None) -> None:
        super().__init__(message)
        self.pull_request_url = pull_request_url


@dataclass(frozen=True)
class PullRequest:
    url: str
    auto_merge_enabled: bool


class GitHubPullRequests:
    def __init__(self, token: str) -> None:
        if not token:
            raise GitHubError("a GitHub token is required to create pull requests")
        self._token = token

    def ensure(
        self,
        repo_url: str,
        head: str,
        base: str,
        title: str,
        body: str,
        merge_when_ready: bool,
        merge_method: str = "SQUASH",
    ) -> PullRequest:
        owner, repo = _github_repository(repo_url)
        path = f"/repos/{owner}/{repo}/pulls"
        query = urlencode({"state": "all", "head": f"{owner}:{head}", "base": base, "per_page": 100})
        existing = self._request("GET", f"{path}?{query}")
        if not isinstance(existing, list):
            raise GitHubError("GitHub returned an invalid pull request list")
        matching = [item for item in existing if item.get("head", {}).get("ref") == head]
        pull = next((item for item in matching if item.get("state") == "open"), None)
        if pull is None:
            pull = self._request(
                "POST",
                path,
                {"title": title, "body": body, "head": head, "base": base},
            )
        if not isinstance(pull, dict) or not pull.get("html_url"):
            raise GitHubError("GitHub returned an invalid pull request")

        auto_merge_enabled = bool(pull.get("auto_merge"))
        if merge_when_ready and not auto_merge_enabled and not pull.get("merged_at"):
            node_id = pull.get("node_id")
            if not node_id:
                raise GitHubError("GitHub did not return a pull request node ID")
            try:
                self._enable_auto_merge(node_id, merge_method)
            except GitHubError as exc:
                raise GitHubError(str(exc), pull_request_url=pull["html_url"]) from exc
            auto_merge_enabled = True
        return PullRequest(url=pull["html_url"], auto_merge_enabled=auto_merge_enabled)

    def _enable_auto_merge(self, node_id: str, merge_method: str) -> None:
        result = self._request(
            "POST",
            "/graphql",
            {
                "query": "mutation($id: ID!, $method: PullRequestMergeMethod!) { "
                "enablePullRequestAutoMerge(input: {pullRequestId: $id, mergeMethod: $method}) "
                "{ pullRequest { id } } }",
                "variables": {"id": node_id, "method": merge_method},
            },
        )
        if result.get("errors") or not result.get("data", {}).get("enablePullRequestAutoMerge"):
            raise GitHubError("GitHub could not enable auto merge; check repository settings and token access")

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = Request(
            f"https://api.github.com{path}",
            data=data,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "cloud-agent",
            },
        )
        try:
            with urlopen(request, timeout=30) as response:
                return json.load(response)
        except HTTPError as exc:
            raise GitHubError(f"GitHub API request failed with HTTP {exc.code}") from exc
        except URLError as exc:
            raise GitHubError("GitHub API request could not connect") from exc


def _github_repository(repo_url: str) -> tuple[str, str]:
    parsed = urlparse(repo_url)
    if parsed.scheme != "https" or parsed.hostname != "github.com":
        raise GitHubError("pull requests are supported for github.com HTTPS repositories")
    parts = parsed.path.strip("/").removesuffix(".git").split("/")
    if len(parts) != 2 or not all(parts):
        raise GitHubError("GitHub clone URL must be https://github.com/owner/repo.git")
    return parts[0], parts[1]
