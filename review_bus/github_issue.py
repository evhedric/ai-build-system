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

CHATGPT_REVIEW_MARKER = "<!-- ai-review-bus:chatgpt-latest -->"


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


def update_issue_comment(owner: str, repo: str, comment_id: int, body: str) -> dict[str, Any]:
    url = f"https://api.github.com/repos/{owner}/{repo}/issues/comments/{comment_id}"
    return _request("PATCH", url, body={"body": body})


def find_comment_with_marker(
    comments: list[dict[str, Any]],
    marker: str = CHATGPT_REVIEW_MARKER,
) -> dict[str, Any] | None:
    for comment in comments:
        body = comment.get("body") or ""
        if marker in body:
            return comment
    return None


def format_chatgpt_review_comment(review: str) -> str:
    trimmed_review = review.strip()
    if not trimmed_review:
        raise RuntimeError("Refusing to post an empty ChatGPT review.")

    if not trimmed_review.startswith("# ChatGPT Review"):
        trimmed_review = f"# ChatGPT Review\n\n{trimmed_review}"

    return f"{CHATGPT_REVIEW_MARKER}\n{trimmed_review}\n"


def upsert_chatgpt_review_comment(
    owner: str,
    repo: str,
    issue_number: int,
    comments: list[dict[str, Any]],
    review: str,
) -> dict[str, Any]:
    body = format_chatgpt_review_comment(review)
    existing = find_comment_with_marker(comments)

    if existing is None:
        return create_issue_comment(owner, repo, issue_number, body)

    comment_id = existing.get("id")
    if not isinstance(comment_id, int):
        raise RuntimeError("Existing ChatGPT review comment is missing a numeric id.")

    return update_issue_comment(owner, repo, comment_id, body)
