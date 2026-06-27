"""Unit tests for review_bus.state — BusState, format/parse, hash, label routing."""

from __future__ import annotations

import pytest

from review_bus.state import (
    BUS_STATE_MARKER,
    DEFAULT_MAX_TURNS,
    LABEL_APPROVED,
    LABEL_BLOCKED,
    LABEL_CHATGPT_NEEDED,
    LABEL_CLAUDE_NEEDED,
    LABEL_CODEX_NEEDED,
    LABEL_STOPPED,
    STATUS_APPROVED,
    STATUS_BLOCKED,
    STATUS_IDLE,
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
# BusState
# ---------------------------------------------------------------------------


class TestBusState:
    def test_defaults(self) -> None:
        s = BusState()
        assert s.status == STATUS_IDLE
        assert s.turn == 0
        assert s.max_turns == DEFAULT_MAX_TURNS
        assert s.next_actor == ""
        assert s.last_actor == ""
        assert s.last_verdict == ""
        assert s.packet_hash == ""
        assert s.stopped_reason == ""

    def test_is_terminal_idle_false(self) -> None:
        assert not BusState().is_terminal()

    def test_is_terminal_in_progress_false(self) -> None:
        assert not BusState(status=STATUS_IN_PROGRESS).is_terminal()

    def test_is_terminal_approved(self) -> None:
        assert BusState(status=STATUS_APPROVED).is_terminal()

    def test_is_terminal_blocked(self) -> None:
        assert BusState(status=STATUS_BLOCKED).is_terminal()

    def test_is_terminal_stopped(self) -> None:
        assert BusState(status=STATUS_STOPPED).is_terminal()

    def test_is_terminal_max_turns(self) -> None:
        assert BusState(status=STATUS_MAX_TURNS).is_terminal()


# ---------------------------------------------------------------------------
# compute_packet_hash
# ---------------------------------------------------------------------------


class TestComputePacketHash:
    def test_stable_for_same_input(self) -> None:
        assert compute_packet_hash("hello") == compute_packet_hash("hello")

    def test_different_for_different_input(self) -> None:
        assert compute_packet_hash("a") != compute_packet_hash("b")

    def test_returns_12_hex_chars(self) -> None:
        h = compute_packet_hash("some packet text")
        assert len(h) == 12
        assert all(c in "0123456789abcdef" for c in h)

    def test_empty_string_returns_hash(self) -> None:
        h = compute_packet_hash("")
        assert len(h) == 12


# ---------------------------------------------------------------------------
# format_bus_state_comment / parse_bus_state_comment
# ---------------------------------------------------------------------------


class TestBusStateFormatParse:
    def test_format_contains_marker(self) -> None:
        body = format_bus_state_comment(BusState())
        assert BUS_STATE_MARKER in body

    def test_format_contains_json_block(self) -> None:
        body = format_bus_state_comment(BusState())
        assert "```json" in body

    def test_format_contains_table_header(self) -> None:
        body = format_bus_state_comment(BusState())
        assert "# AI Review Bus" in body
        assert "| Field | Value |" in body

    def test_round_trip_default(self) -> None:
        original = BusState()
        parsed = parse_bus_state_comment(format_bus_state_comment(original))
        assert parsed.status == original.status
        assert parsed.turn == original.turn
        assert parsed.max_turns == original.max_turns

    def test_round_trip_full_state(self) -> None:
        original = BusState(
            status=STATUS_IN_PROGRESS,
            turn=2,
            max_turns=6,
            next_actor="claude",
            last_actor="chatgpt",
            last_verdict="NO-RUN",
            packet_hash="abc123456789",
            stopped_reason="",
        )
        parsed = parse_bus_state_comment(format_bus_state_comment(original))
        assert parsed.status == STATUS_IN_PROGRESS
        assert parsed.turn == 2
        assert parsed.max_turns == 6
        assert parsed.next_actor == "claude"
        assert parsed.last_actor == "chatgpt"
        assert parsed.last_verdict == "NO-RUN"
        assert parsed.packet_hash == "abc123456789"
        assert parsed.stopped_reason == ""

    def test_round_trip_with_stopped_reason(self) -> None:
        original = BusState(status=STATUS_BLOCKED, stopped_reason="label:blocked")
        parsed = parse_bus_state_comment(format_bus_state_comment(original))
        assert parsed.stopped_reason == "label:blocked"

    def test_parse_returns_default_on_empty_body(self) -> None:
        parsed = parse_bus_state_comment("")
        assert parsed.status == STATUS_IDLE
        assert parsed.turn == 0

    def test_parse_returns_default_on_invalid_json(self) -> None:
        parsed = parse_bus_state_comment("```json\nnot valid json\n```")
        assert parsed.status == STATUS_IDLE

    def test_parse_returns_default_on_missing_json_block(self) -> None:
        parsed = parse_bus_state_comment(BUS_STATE_MARKER + "\nNo JSON here.")
        assert parsed.status == STATUS_IDLE

    def test_format_shows_last_actor_in_table(self) -> None:
        state = BusState(status=STATUS_IN_PROGRESS, turn=1, last_actor="chatgpt")
        body = format_bus_state_comment(state)
        assert "chatgpt" in body

    def test_format_shows_stopped_reason_when_set(self) -> None:
        state = BusState(status=STATUS_BLOCKED, stopped_reason="label:blocked")
        body = format_bus_state_comment(state)
        assert "label:blocked" in body

    def test_format_omits_stopped_reason_row_when_empty(self) -> None:
        state = BusState()
        body = format_bus_state_comment(state)
        assert "Stopped reason" not in body

    def test_format_is_deterministic(self) -> None:
        state = BusState(status=STATUS_IN_PROGRESS, turn=1, last_actor="chatgpt")
        assert format_bus_state_comment(state) == format_bus_state_comment(state)


# ---------------------------------------------------------------------------
# next_actor_from_labels
# ---------------------------------------------------------------------------


class TestNextActorFromLabels:
    def test_empty_labels_returns_empty(self) -> None:
        assert next_actor_from_labels([]) == ""

    def test_chatgpt_needed(self) -> None:
        assert next_actor_from_labels([LABEL_CHATGPT_NEEDED]) == "chatgpt"

    def test_claude_needed(self) -> None:
        assert next_actor_from_labels([LABEL_CLAUDE_NEEDED]) == "claude"

    def test_codex_needed(self) -> None:
        assert next_actor_from_labels([LABEL_CODEX_NEEDED]) == "codex"

    def test_approved(self) -> None:
        assert next_actor_from_labels([LABEL_APPROVED]) == "approved"

    def test_blocked(self) -> None:
        assert next_actor_from_labels([LABEL_BLOCKED]) == "blocked"

    def test_stopped(self) -> None:
        assert next_actor_from_labels([LABEL_STOPPED]) == "stopped"

    def test_blocked_wins_over_chatgpt(self) -> None:
        assert next_actor_from_labels([LABEL_BLOCKED, LABEL_CHATGPT_NEEDED]) == "blocked"

    def test_blocked_wins_over_claude(self) -> None:
        assert next_actor_from_labels([LABEL_BLOCKED, LABEL_CLAUDE_NEEDED]) == "blocked"

    def test_stopped_wins_over_chatgpt(self) -> None:
        assert next_actor_from_labels([LABEL_STOPPED, LABEL_CHATGPT_NEEDED]) == "stopped"

    def test_stopped_wins_over_claude(self) -> None:
        assert next_actor_from_labels([LABEL_STOPPED, LABEL_CLAUDE_NEEDED]) == "stopped"

    def test_approved_wins_over_routing_labels(self) -> None:
        assert next_actor_from_labels([LABEL_APPROVED, LABEL_CODEX_NEEDED]) == "approved"

    def test_blocked_wins_over_stopped(self) -> None:
        assert next_actor_from_labels([LABEL_BLOCKED, LABEL_STOPPED]) == "blocked"

    def test_chatgpt_wins_over_claude_when_both_present(self) -> None:
        assert next_actor_from_labels([LABEL_CHATGPT_NEEDED, LABEL_CLAUDE_NEEDED]) == "chatgpt"

    def test_claude_wins_over_codex_when_both_present(self) -> None:
        assert next_actor_from_labels([LABEL_CLAUDE_NEEDED, LABEL_CODEX_NEEDED]) == "claude"

    def test_unknown_labels_ignored(self) -> None:
        assert next_actor_from_labels(["some-other-label", "unrelated"]) == ""

    def test_mixed_known_and_unknown(self) -> None:
        assert next_actor_from_labels(["unrelated", LABEL_CLAUDE_NEEDED]) == "claude"
