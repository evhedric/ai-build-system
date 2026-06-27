"""Minimal GitHub Issues client using only the Python standard library.

This avoids adding another dependency to ai-build-system.  The broker only
needs to read one issue, read its comments, and post one comment.
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from typing import Any


class GitHubApiError(RuntimeError):
    pass


def _require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def get_repo_context() -> tuple[str, str, int]:
    repo_full_name = _require_env("GITHUB_REPOSITORY")
    issue_number_raw = _require_env("ISSUE_NUMBER")

    try:
        issue_number = int(issue_number_raw)
    except ValueError as exc:
        raise RuntimeError(f"ISSUE_NUMBER must be an integer: {issue_number_raw!r}") from exc

    if "/" not in repo_full_name:
        raise RuntimeError(f"GITHUB_REPOSITORY must be owner/repo: {repo_full_name!r}")

    owner, repo = repo_full_name.split("/", 1)
    return owner, repo, issue_number


def _request(method: str, url: str, *, body: dict[str, Any] | None = None) -> Any:
    token = _require_env("GITHUB_TOKEN")

    data = None
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "ai-review-bus-cleanroom",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)

    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            raw = response.read().decode("utf-8")
            if not raw:
                return None
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise GitHubApiError(f"GitHub API {method} {url} failed: {exc.code} {detail}") from exc


def get_issue(owner: str, repo: str, issue_number: int) -> dict[str, Any]:
    url = f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_number}"
    issue = _request("GET", url)
    if issue.get("pull_request"):
        raise RuntimeError("Refusing to review pull request comments in cleanroom mode.")
    return issue


def list_issue_comments(owner: str, repo: str, issue_number: int) -> list[dict[str, Any]]:
    comments: list[dict[str, Any]] = []
    page = 1

    while True:
        query = urllib.parse.urlencode({"per_page": 100, "page": page})
        url = f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_number}/comments?{query}"
        batch = _request("GET", url)
        if not isinstance(batch, list):
            raise RuntimeError("Unexpected GitHub comments response.")
        comments.extend(batch)
        if len(batch) < 100:
            break
        page += 1

    return comments


def create_issue_comment(owner: str, repo: str, issue_number: int, body: str) -> dict[str, Any]:
    url = f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_number}/comments"
    return _request("POST", url, body={"body": body})
