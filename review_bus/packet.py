"""Review Packet extraction helpers."""

from __future__ import annotations

from typing import Any

REVIEW_PACKET_MARKER = "# Review Packet"


def assert_exact_trigger(comment_body: str) -> None:
    trimmed = comment_body.strip()
    if trimmed != "/chatgpt-review":
        raise RuntimeError(
            f"Refusing to run: trigger comment must be exactly /chatgpt-review. Got {trimmed!r}"
        )


def find_latest_review_packet(issue_body: str, comments: list[dict[str, Any]]) -> str:
    candidates: list[str] = []

    if REVIEW_PACKET_MARKER in issue_body:
        candidates.append(issue_body)

    for comment in comments:
        body = comment.get("body") or ""
        if REVIEW_PACKET_MARKER in body:
            candidates.append(body)

    if not candidates:
        raise RuntimeError("No Review Packet found in issue body or comments.")

    return candidates[-1]


def recent_context(comments: list[dict[str, Any]], limit: int = 12) -> str:
    selected = comments[-limit:]
    parts: list[str] = []

    for index, comment in enumerate(selected, start=1):
        user = (comment.get("user") or {}).get("login", "unknown")
        created_at = comment.get("created_at", "unknown-time")
        body = comment.get("body") or ""
        parts.append(f"## Recent comment {index} by {user} at {created_at}\n\n{body}")

    return "\n\n---\n\n".join(parts)


def get_recent_comment_limit() -> int:
    import os

    raw = os.getenv("MAX_RECENT_COMMENTS", "12")
    try:
        parsed = int(raw)
    except ValueError:
        return 12

    if parsed < 1 or parsed > 50:
        return 12

    return parsed
