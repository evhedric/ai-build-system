"""AI Review Bus state machine — constants, BusState dataclass, format/parse, routing."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass

# ---------------------------------------------------------------------------
# HTML comment markers — each identifies ONE stable comment on the issue
# ---------------------------------------------------------------------------

BUS_STATE_MARKER = "<!-- ai-review-bus:state -->"
CLAUDE_REVIEW_MARKER = "<!-- ai-review-bus:claude-latest -->"
CODEX_REVIEW_MARKER = "<!-- ai-review-bus:codex-latest -->"
CLAUDE_HANDOFF_MARKER = "<!-- ai-review-bus:claude-handoff -->"

# ---------------------------------------------------------------------------
# GitHub issue labels
# ---------------------------------------------------------------------------

LABEL_CHATGPT_NEEDED = "ai-bus:chatgpt-needed"
LABEL_CLAUDE_NEEDED = "ai-bus:claude-needed"
LABEL_CODEX_NEEDED = "ai-bus:codex-needed"
LABEL_APPROVED = "ai-bus:approved"
LABEL_BLOCKED = "ai-bus:blocked"
LABEL_STOPPED = "ai-bus:stopped"

STOP_LABELS: frozenset[str] = frozenset({LABEL_BLOCKED, LABEL_STOPPED, LABEL_APPROVED})
ROUTING_LABELS: frozenset[str] = frozenset(
    {LABEL_CHATGPT_NEEDED, LABEL_CLAUDE_NEEDED, LABEL_CODEX_NEEDED}
)

# ---------------------------------------------------------------------------
# Status strings
# ---------------------------------------------------------------------------

STATUS_IDLE = "idle"
STATUS_IN_PROGRESS = "in_progress"
STATUS_APPROVED = "approved"
STATUS_BLOCKED = "blocked"
STATUS_STOPPED = "stopped"
STATUS_MAX_TURNS = "max_turns_exceeded"

_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {STATUS_APPROVED, STATUS_BLOCKED, STATUS_STOPPED, STATUS_MAX_TURNS}
)

DEFAULT_MAX_TURNS = 4

# ---------------------------------------------------------------------------
# BusState dataclass
# ---------------------------------------------------------------------------


@dataclass
class BusState:
    status: str = STATUS_IDLE
    turn: int = 0
    max_turns: int = DEFAULT_MAX_TURNS
    next_actor: str = ""
    last_actor: str = ""
    last_verdict: str = ""
    packet_hash: str = ""
    stopped_reason: str = ""

    def is_terminal(self) -> bool:
        return self.status in _TERMINAL_STATUSES


# ---------------------------------------------------------------------------
# Packet hashing
# ---------------------------------------------------------------------------


def compute_packet_hash(packet: str) -> str:
    """First 12 hex chars of the SHA-256 of the packet text."""
    return hashlib.sha256(packet.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Bus state comment — format and parse
# ---------------------------------------------------------------------------

_STATE_JSON_RE = re.compile(r"```json\s*\n(.*?)\n```", re.DOTALL)

_STATUS_EMOJI: dict[str, str] = {
    STATUS_IDLE: "⏸",
    STATUS_IN_PROGRESS: "🔄",
    STATUS_APPROVED: "✅",
    STATUS_BLOCKED: "🚫",
    STATUS_STOPPED: "⏹",
    STATUS_MAX_TURNS: "🛑",
}


def format_bus_state_comment(state: BusState) -> str:
    """Render a BusState as a GitHub issue comment body."""
    state_json = json.dumps(asdict(state), indent=2)
    emoji = _STATUS_EMOJI.get(state.status, "❓")

    rows = [
        f"| Status | {emoji} {state.status} |",
        f"| Turn | {state.turn} / {state.max_turns} |",
        f"| Next actor | {state.next_actor or '—'} |",
        f"| Last actor | {state.last_actor or '—'} |",
        f"| Last verdict | {state.last_verdict or '—'} |",
        f"| Packet hash | `{state.packet_hash or '—'}` |",
    ]
    if state.stopped_reason:
        rows.append(f"| Stopped reason | {state.stopped_reason} |")

    parts = [
        BUS_STATE_MARKER,
        "```json",
        state_json,
        "```",
        "",
        "# AI Review Bus — State",
        "",
        "| Field | Value |",
        "|---|---|",
        *rows,
        "",
    ]
    return "\n".join(parts)


def parse_bus_state_comment(body: str) -> BusState:
    """Parse a BusState from a bus-state comment body. Returns a default on any failure."""
    match = _STATE_JSON_RE.search(body)
    if not match:
        return BusState()
    try:
        data = json.loads(match.group(1))
        return BusState(
            status=str(data.get("status", STATUS_IDLE)),
            turn=int(data.get("turn", 0)),
            max_turns=int(data.get("max_turns", DEFAULT_MAX_TURNS)),
            next_actor=str(data.get("next_actor", "")),
            last_actor=str(data.get("last_actor", "")),
            last_verdict=str(data.get("last_verdict", "")),
            packet_hash=str(data.get("packet_hash", "")),
            stopped_reason=str(data.get("stopped_reason", "")),
        )
    except (json.JSONDecodeError, ValueError, TypeError):
        return BusState()


# ---------------------------------------------------------------------------
# Label → next-actor routing
# ---------------------------------------------------------------------------


def next_actor_from_labels(labels: list[str]) -> str:
    """
    Derive the next actor from the set of active issue labels.

    Stop labels (blocked / stopped / approved) always win over routing labels.
    Priority within each tier:
      blocked > stopped > approved  (stop tier)
      chatgpt > claude > codex      (routing tier)

    Returns one of:
      'chatgpt' | 'claude' | 'codex' | 'approved' | 'blocked' | 'stopped' | ''
    """
    label_set = set(labels)

    if LABEL_BLOCKED in label_set:
        return "blocked"
    if LABEL_STOPPED in label_set:
        return "stopped"
    if LABEL_APPROVED in label_set:
        return "approved"
    if LABEL_CHATGPT_NEEDED in label_set:
        return "chatgpt"
    if LABEL_CLAUDE_NEEDED in label_set:
        return "claude"
    if LABEL_CODEX_NEEDED in label_set:
        return "codex"
    return ""
