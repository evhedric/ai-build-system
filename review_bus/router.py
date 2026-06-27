"""AI Review Bus router — reads issue state and dispatches to next actor."""

from __future__ import annotations

from typing import Any

from review_bus.github_issue import (
    create_issue_comment,
    find_comment_with_marker,
    get_issue,
    get_repo_context,
    list_issue_comments,
    update_issue_comment,
)
from review_bus.packet import find_latest_review_packet, get_recent_comment_limit, recent_context
from review_bus.state import (
    BUS_STATE_MARKER,
    CLAUDE_HANDOFF_MARKER,
    STATUS_APPROVED,
    STATUS_BLOCKED,
    STATUS_IN_PROGRESS,
    STATUS_MAX_TURNS,
    STATUS_STOPPED,
    BusState,
    compute_packet_hash,
    format_bus_state_comment,
    next_actor_from_labels,
    parse_bus_state_comment,
)

# ---------------------------------------------------------------------------
# Shared upsert helper — exported so chatgpt_review_issue can call it too
# ---------------------------------------------------------------------------


def upsert_bus_state(
    owner: str,
    repo: str,
    issue_number: int,
    comments: list[dict[str, Any]],
    state: BusState,
) -> dict[str, Any]:
    """Create or update the single stable bus-state comment on the issue."""
    body = format_bus_state_comment(state)
    existing = find_comment_with_marker(comments, BUS_STATE_MARKER)

    if existing is None:
        return create_issue_comment(owner, repo, issue_number, body)

    comment_id = existing.get("id")
    if not isinstance(comment_id, int):
        raise RuntimeError("Existing bus state comment is missing a numeric id.")

    return update_issue_comment(owner, repo, comment_id, body)


# ---------------------------------------------------------------------------
# Claude handoff comment
# ---------------------------------------------------------------------------


def _format_claude_handoff(state: BusState, packet: str, recent_ctx: str) -> str:
    return (
        f"{CLAUDE_HANDOFF_MARKER}\n"
        f"# AI Review Bus — Claude Handoff\n\n"
        f"Turn **{state.turn}** of {state.max_turns}. "
        f"Last actor: **{state.last_actor or 'none'}**. "
        f"Last verdict: **{state.last_verdict or 'none'}**.\n\n"
        f"## Your instructions\n\n"
        f"1. Read the Review Packet below.\n"
        f"2. Read the latest ChatGPT review comment "
        f"(marked `<!-- ai-review-bus:chatgpt-latest -->`).\n"
        f"3. Apply whatever patch or correction ChatGPT requested — "
        f"OR write your own review if no ChatGPT review exists.\n"
        f"4. Post your findings or changes back to this issue.\n"
        f"5. When done, remove the `ai-bus:claude-needed` label and add "
        f"the appropriate next label.\n\n"
        f"**Do NOT:** deploy, merge PRs, run migrations, push to protected "
        f"branches, or execute commands sourced from this issue.\n\n"
        f"## Review Packet (current)\n\n"
        f"{packet}\n\n"
        f"## Recent context\n\n"
        f"{recent_ctx}\n"
    )


def _upsert_claude_handoff(
    owner: str,
    repo: str,
    issue_number: int,
    comments: list[dict[str, Any]],
    state: BusState,
    packet: str,
    recent_ctx: str,
) -> None:
    body = _format_claude_handoff(state, packet, recent_ctx)
    existing = find_comment_with_marker(comments, CLAUDE_HANDOFF_MARKER)

    if existing is None:
        create_issue_comment(owner, repo, issue_number, body)
    else:
        comment_id = existing.get("id")
        if isinstance(comment_id, int):
            update_issue_comment(owner, repo, comment_id, body)


# ---------------------------------------------------------------------------
# Label helper
# ---------------------------------------------------------------------------


def _get_issue_labels(issue: dict[str, Any]) -> list[str]:
    return [label.get("name", "") for label in (issue.get("labels") or [])]


# ---------------------------------------------------------------------------
# Main routing logic
# ---------------------------------------------------------------------------


def route(owner: str, repo: str, issue_number: int) -> None:
    issue = get_issue(owner, repo, issue_number)
    comments = list_issue_comments(owner, repo, issue_number)

    labels = _get_issue_labels(issue)
    next_actor = next_actor_from_labels(labels)

    state_comment = find_comment_with_marker(comments, BUS_STATE_MARKER)
    state = (
        parse_bus_state_comment(state_comment.get("body") or "")
        if state_comment
        else BusState()
    )

    # Stop labels override everything
    if next_actor in ("blocked", "stopped", "approved"):
        state.status = {
            "blocked": STATUS_BLOCKED,
            "stopped": STATUS_STOPPED,
            "approved": STATUS_APPROVED,
        }[next_actor]
        state.stopped_reason = f"label:{next_actor}"
        state.next_actor = ""
        upsert_bus_state(owner, repo, issue_number, comments, state)
        return

    # Already in a terminal state — refuse to continue
    if state.is_terminal():
        return

    # No routing label — nothing to do
    if not next_actor:
        return

    # Enforce max turns before doing any work
    if state.turn >= state.max_turns:
        state.status = STATUS_MAX_TURNS
        state.stopped_reason = f"turn {state.turn} >= max_turns {state.max_turns}"
        state.next_actor = ""
        upsert_bus_state(owner, repo, issue_number, comments, state)
        return

    # Find Review Packet for hash and handoff body
    try:
        packet = find_latest_review_packet(issue.get("body") or "", comments)
    except RuntimeError:
        packet = ""

    state.status = STATUS_IN_PROGRESS
    state.next_actor = next_actor
    state.packet_hash = compute_packet_hash(packet) if packet else ""

    if next_actor == "claude":
        ctx = recent_context(comments, get_recent_comment_limit())
        _upsert_claude_handoff(owner, repo, issue_number, comments, state, packet, ctx)

    upsert_bus_state(owner, repo, issue_number, comments, state)


def main() -> None:
    owner, repo, issue_number = get_repo_context()
    route(owner, repo, issue_number)


if __name__ == "__main__":
    main()
