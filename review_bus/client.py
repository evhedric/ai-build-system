"""AI Review Bus local client — submit packets, poll for reviews, check bus state.

Subcommands:
  submit --packet <file>   Create/update the Review Bus issue and trigger ChatGPT review.
  poll   [--issue-num N]   Block until the next ChatGPT review comment arrives.
  status [--issue-num N]   Print the current bus-state comment.

Environment variables required for GitHub API calls:
  GITHUB_TOKEN       — personal access token with issues: write on the repo
  GITHUB_REPOSITORY  — owner/repo (inferred from git remote if not set)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from review_bus.github_issue import (
    CHATGPT_REVIEW_MARKER,
    create_issue,
    create_issue_comment,
    find_comment_with_marker,
    get_issue,
    list_issue_comments,
    update_issue,
)
from review_bus.state import BUS_STATE_MARKER, parse_bus_state_comment

# ---------------------------------------------------------------------------
# Local state file
# ---------------------------------------------------------------------------

STATE_FILE = Path(".ai-review-bus.json")
ISSUE_TITLE_PREFIX = "[AI Review Bus]"
REVIEW_BUS_LABEL = "ai-bus:review-bus"


def _load_state() -> dict[str, Any]:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def _save_state(data: dict[str, Any]) -> None:
    STATE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Repo resolution
# ---------------------------------------------------------------------------


def _resolve_repo() -> tuple[str, str]:
    """Return (owner, repo) from GITHUB_REPOSITORY env var or git remote."""
    repo_full = os.getenv("GITHUB_REPOSITORY", "").strip()
    if "/" in repo_full:
        owner, repo = repo_full.split("/", 1)
        return owner, repo

    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=True,
        )
        url = result.stdout.strip()
        if "github.com" in url:
            path = (
                url.split(":", 1)[1]
                if url.startswith("git@")
                else url.split("github.com/", 1)[1]
            )
            path = path.removesuffix(".git")
            owner, repo = path.split("/", 1)
            return owner, repo
    except Exception:
        pass

    raise RuntimeError(
        "Cannot determine GitHub repository. "
        "Set GITHUB_REPOSITORY=owner/repo or ensure git remote origin is a GitHub URL."
    )


def _current_branch() -> str:
    try:
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


# ---------------------------------------------------------------------------
# submit subcommand
# ---------------------------------------------------------------------------


def cmd_submit(args: argparse.Namespace) -> int:
    packet_path = Path(args.packet)
    if not packet_path.exists():
        print(f"ERROR: packet file not found: {packet_path}", file=sys.stderr)
        return 1

    packet_body = packet_path.read_text(encoding="utf-8")
    owner, repo = _resolve_repo()
    local = _load_state()
    issue_number: int | None = args.issue_num or local.get("issue_number")

    if issue_number is not None:
        try:
            existing = get_issue(owner, repo, issue_number)
            if existing.get("state") == "open":
                update_issue(owner, repo, issue_number, body=packet_body)
                print(f"Updated issue #{issue_number} with new Review Packet.")
            else:
                issue_number = None  # closed — create fresh
        except Exception:
            issue_number = None

    if issue_number is None:
        branch = _current_branch()
        title = f"{ISSUE_TITLE_PREFIX} {branch}"
        created = create_issue(owner, repo, title, packet_body, labels=[REVIEW_BUS_LABEL])
        issue_number = created["number"]
        print(f"Created issue #{issue_number}: {created.get('html_url', '')}")

    # Trigger ChatGPT review via the stable comment convention
    create_issue_comment(owner, repo, issue_number, "/chatgpt-review")
    print(f"Triggered ChatGPT review on issue #{issue_number}.")

    _save_state({"issue_number": issue_number, "owner": owner, "repo": repo})

    print(f"Issue: #{issue_number}")
    print(f"URL: https://github.com/{owner}/{repo}/issues/{issue_number}")
    return 0


# ---------------------------------------------------------------------------
# poll subcommand
# ---------------------------------------------------------------------------


def cmd_poll(args: argparse.Namespace) -> int:
    owner, repo = _resolve_repo()
    local = _load_state()
    issue_number: int | None = args.issue_num or local.get("issue_number")
    if not issue_number:
        print(
            "ERROR: No issue number. Pass --issue-num or run submit first.",
            file=sys.stderr,
        )
        return 1

    turn_before: int = args.turn_before if args.turn_before is not None else -1
    timeout: int = args.timeout
    interval: int = args.interval

    deadline = time.monotonic() + timeout
    print(
        f"Polling issue #{issue_number} for ChatGPT review "
        f"(turn > {turn_before}, timeout {timeout}s, interval {interval}s)...",
        file=sys.stderr,
    )

    while time.monotonic() < deadline:
        comments = list_issue_comments(owner, repo, issue_number)

        state_cmt = find_comment_with_marker(comments, BUS_STATE_MARKER)
        if state_cmt:
            bus_state = parse_bus_state_comment(state_cmt.get("body") or "")
            if bus_state.last_actor == "chatgpt" and bus_state.turn > turn_before:
                review_cmt = find_comment_with_marker(comments, CHATGPT_REVIEW_MARKER)
                if review_cmt:
                    print(review_cmt.get("body", ""))
                    return 0

        remaining = int(deadline - time.monotonic())
        print(f"  waiting... ({remaining}s remaining)", file=sys.stderr)
        time.sleep(interval)

    print(
        f"ERROR: Timed out waiting for ChatGPT review after {timeout}s.",
        file=sys.stderr,
    )
    return 1


# ---------------------------------------------------------------------------
# status subcommand
# ---------------------------------------------------------------------------


def cmd_status(args: argparse.Namespace) -> int:
    owner, repo = _resolve_repo()
    local = _load_state()
    issue_number: int | None = args.issue_num or local.get("issue_number")
    if not issue_number:
        print("No active Review Bus issue. Run submit first.", file=sys.stderr)
        return 1

    comments = list_issue_comments(owner, repo, issue_number)
    state_cmt = find_comment_with_marker(comments, BUS_STATE_MARKER)
    if not state_cmt:
        print(f"Issue #{issue_number} has no bus state comment yet.")
        return 0

    print(state_cmt.get("body", ""))
    return 0


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="AI Review Bus local client")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_submit = sub.add_parser("submit", help="Submit packet and trigger ChatGPT review")
    p_submit.add_argument("--packet", required=True, help="Path to Review Packet markdown file")
    p_submit.add_argument("--issue-num", type=int, default=None, dest="issue_num")

    p_poll = sub.add_parser("poll", help="Wait for ChatGPT review to arrive")
    p_poll.add_argument("--issue-num", type=int, default=None, dest="issue_num")
    p_poll.add_argument(
        "--turn-before",
        type=int,
        default=None,
        dest="turn_before",
        help="Review is new only if bus state turn > this value (default: any turn)",
    )
    p_poll.add_argument(
        "--timeout", type=int, default=300, help="Max seconds to wait (default 300)"
    )
    p_poll.add_argument(
        "--interval", type=int, default=15, help="Poll interval in seconds (default 15)"
    )

    p_status = sub.add_parser("status", help="Print current bus state")
    p_status.add_argument("--issue-num", type=int, default=None, dest="issue_num")

    args = parser.parse_args()
    handlers = {"submit": cmd_submit, "poll": cmd_poll, "status": cmd_status}
    sys.exit(handlers[args.cmd](args))


if __name__ == "__main__":
    main()
