"""Unit tests for review_bus.router — upsert_bus_state and route() logic."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from review_bus.state import (
    BUS_STATE_MARKER,
    CLAUDE_HANDOFF_MARKER,
    LABEL_BLOCKED,
    LABEL_CHATGPT_NEEDED,
    LABEL_CLAUDE_NEEDED,
    LABEL_STOPPED,
    STATUS_BLOCKED,
    STATUS_IN_PROGRESS,
    STATUS_MAX_TURNS,
    STATUS_STOPPED,
    BusState,
    format_bus_state_comment,
)
from review_bus.router import route, upsert_bus_state


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_issue(labels: list[str], body: str = "# Review Packet\nSome content.") -> dict:
    return {
        "number": 1,
        "body": body,
        "labels": [{"name": lbl} for lbl in labels],
    }


def _make_comment(comment_id: int, body: str) -> dict:
    return {
        "id": comment_id,
        "body": body,
        "user": {"login": "bot"},
        "created_at": "2026-01-01T00:00:00Z",
    }


# ---------------------------------------------------------------------------
# upsert_bus_state — no-duplicate behaviour
# ---------------------------------------------------------------------------


class TestUpsertBusState:
    @patch("review_bus.router.create_issue_comment")
    def test_creates_when_no_existing_comment(self, mock_create: MagicMock) -> None:
        mock_create.return_value = {"id": 10}
        state = BusState(status=STATUS_IN_PROGRESS, turn=1)
        upsert_bus_state("owner", "repo", 1, [], state)
        mock_create.assert_called_once()
        body_arg = mock_create.call_args[0][3]
        assert BUS_STATE_MARKER in body_arg

    @patch("review_bus.router.update_issue_comment")
    def test_updates_when_existing_comment(self, mock_update: MagicMock) -> None:
        mock_update.return_value = {"id": 42}
        existing_body = format_bus_state_comment(BusState())
        comments = [_make_comment(42, existing_body)]
        state = BusState(status=STATUS_IN_PROGRESS, turn=1)
        upsert_bus_state("owner", "repo", 1, comments, state)
        mock_update.assert_called_once()
        assert mock_update.call_args[0][2] == 42

    @patch("review_bus.router.update_issue_comment")
    @patch("review_bus.router.create_issue_comment")
    def test_does_not_create_when_existing(
        self, mock_create: MagicMock, mock_update: MagicMock
    ) -> None:
        mock_update.return_value = {"id": 42}
        existing_body = format_bus_state_comment(BusState())
        comments = [_make_comment(42, existing_body)]
        upsert_bus_state("owner", "repo", 1, comments, BusState())
        mock_create.assert_not_called()
        mock_update.assert_called_once()

    @patch("review_bus.router.update_issue_comment")
    @patch("review_bus.router.create_issue_comment")
    def test_updates_body_with_new_state(
        self, mock_create: MagicMock, mock_update: MagicMock
    ) -> None:
        mock_update.return_value = {"id": 42}
        existing_body = format_bus_state_comment(BusState())
        comments = [_make_comment(42, existing_body)]
        new_state = BusState(status=STATUS_IN_PROGRESS, turn=2, last_actor="chatgpt")
        upsert_bus_state("owner", "repo", 1, comments, new_state)
        updated_body = mock_update.call_args[0][3]
        assert "chatgpt" in updated_body
        assert STATUS_IN_PROGRESS in updated_body


# ---------------------------------------------------------------------------
# route — terminal labels
# ---------------------------------------------------------------------------


class TestRouteTerminalLabels:
    @patch("review_bus.router.upsert_bus_state")
    @patch("review_bus.router.list_issue_comments", return_value=[])
    @patch("review_bus.router.get_issue")
    def test_blocked_label_sets_blocked_status(
        self,
        mock_issue: MagicMock,
        _mock_comments: MagicMock,
        mock_upsert: MagicMock,
    ) -> None:
        mock_issue.return_value = _make_issue([LABEL_BLOCKED])
        mock_upsert.return_value = {"id": 1}
        route("owner", "repo", 1)
        state = mock_upsert.call_args[0][4]
        assert state.status == STATUS_BLOCKED
        assert "blocked" in state.stopped_reason

    @patch("review_bus.router.upsert_bus_state")
    @patch("review_bus.router.list_issue_comments", return_value=[])
    @patch("review_bus.router.get_issue")
    def test_stopped_label_sets_stopped_status(
        self,
        mock_issue: MagicMock,
        _mock_comments: MagicMock,
        mock_upsert: MagicMock,
    ) -> None:
        mock_issue.return_value = _make_issue([LABEL_STOPPED])
        mock_upsert.return_value = {"id": 1}
        route("owner", "repo", 1)
        state = mock_upsert.call_args[0][4]
        assert state.status == STATUS_STOPPED

    @patch("review_bus.router.upsert_bus_state")
    @patch("review_bus.router.list_issue_comments", return_value=[])
    @patch("review_bus.router.get_issue")
    def test_blocked_clears_next_actor(
        self,
        mock_issue: MagicMock,
        _mock_comments: MagicMock,
        mock_upsert: MagicMock,
    ) -> None:
        mock_issue.return_value = _make_issue([LABEL_BLOCKED, LABEL_CHATGPT_NEEDED])
        mock_upsert.return_value = {"id": 1}
        route("owner", "repo", 1)
        state = mock_upsert.call_args[0][4]
        assert state.status == STATUS_BLOCKED
        assert state.next_actor == ""


# ---------------------------------------------------------------------------
# route — already terminal state refuses to continue
# ---------------------------------------------------------------------------


class TestRouteTerminalState:
    @patch("review_bus.router.upsert_bus_state")
    @patch("review_bus.router.list_issue_comments")
    @patch("review_bus.router.get_issue")
    def test_terminal_state_stops_without_upsert(
        self,
        mock_issue: MagicMock,
        mock_comments: MagicMock,
        mock_upsert: MagicMock,
    ) -> None:
        blocked_state = BusState(status=STATUS_BLOCKED, stopped_reason="label:blocked")
        mock_issue.return_value = _make_issue([LABEL_CHATGPT_NEEDED])
        mock_comments.return_value = [
            _make_comment(10, format_bus_state_comment(blocked_state))
        ]
        route("owner", "repo", 1)
        mock_upsert.assert_not_called()


# ---------------------------------------------------------------------------
# route — max turns
# ---------------------------------------------------------------------------


class TestRouteMaxTurns:
    @patch("review_bus.router.upsert_bus_state")
    @patch("review_bus.router.list_issue_comments")
    @patch("review_bus.router.get_issue")
    def test_max_turns_stops_routing(
        self,
        mock_issue: MagicMock,
        mock_comments: MagicMock,
        mock_upsert: MagicMock,
    ) -> None:
        exhausted = BusState(turn=4, max_turns=4)
        mock_issue.return_value = _make_issue([LABEL_CHATGPT_NEEDED])
        mock_comments.return_value = [_make_comment(99, format_bus_state_comment(exhausted))]
        mock_upsert.return_value = {"id": 99}
        route("owner", "repo", 1)
        state = mock_upsert.call_args[0][4]
        assert state.status == STATUS_MAX_TURNS

    @patch("review_bus.router.upsert_bus_state")
    @patch("review_bus.router.list_issue_comments")
    @patch("review_bus.router.get_issue")
    def test_turn_below_max_continues(
        self,
        mock_issue: MagicMock,
        mock_comments: MagicMock,
        mock_upsert: MagicMock,
    ) -> None:
        active = BusState(turn=2, max_turns=4)
        mock_issue.return_value = _make_issue([LABEL_CHATGPT_NEEDED])
        mock_comments.return_value = [_make_comment(99, format_bus_state_comment(active))]
        mock_upsert.return_value = {"id": 99}
        route("owner", "repo", 1)
        state = mock_upsert.call_args[0][4]
        assert state.status == STATUS_IN_PROGRESS


# ---------------------------------------------------------------------------
# route — Claude handoff
# ---------------------------------------------------------------------------


class TestRouteClaudeHandoff:
    @patch("review_bus.router.upsert_bus_state")
    @patch("review_bus.router.update_issue_comment")
    @patch("review_bus.router.create_issue_comment")
    @patch("review_bus.router.list_issue_comments", return_value=[])
    @patch("review_bus.router.get_issue")
    def test_claude_needed_creates_handoff_comment(
        self,
        mock_issue: MagicMock,
        _mock_comments: MagicMock,
        mock_create: MagicMock,
        _mock_update: MagicMock,
        mock_upsert: MagicMock,
    ) -> None:
        mock_issue.return_value = _make_issue([LABEL_CLAUDE_NEEDED])
        mock_create.return_value = {"id": 50}
        mock_upsert.return_value = {"id": 51}
        route("owner", "repo", 1)
        assert mock_create.call_count >= 1
        body = mock_create.call_args_list[0][0][3]
        assert CLAUDE_HANDOFF_MARKER in body

    @patch("review_bus.router.upsert_bus_state")
    @patch("review_bus.router.update_issue_comment")
    @patch("review_bus.router.create_issue_comment")
    @patch("review_bus.router.list_issue_comments")
    @patch("review_bus.router.get_issue")
    def test_claude_needed_updates_existing_handoff(
        self,
        mock_issue: MagicMock,
        mock_comments: MagicMock,
        mock_create: MagicMock,
        mock_update: MagicMock,
        mock_upsert: MagicMock,
    ) -> None:
        existing_handoff = _make_comment(
            77, f"{CLAUDE_HANDOFF_MARKER}\n# AI Review Bus — Claude Handoff\n"
        )
        mock_issue.return_value = _make_issue([LABEL_CLAUDE_NEEDED])
        mock_comments.return_value = [existing_handoff]
        mock_update.return_value = {"id": 77}
        mock_upsert.return_value = {"id": 78}
        route("owner", "repo", 1)
        mock_create.assert_not_called()
        mock_update.assert_called_once()
        assert mock_update.call_args[0][2] == 77

    @patch("review_bus.router.upsert_bus_state")
    @patch("review_bus.router.update_issue_comment")
    @patch("review_bus.router.create_issue_comment")
    @patch("review_bus.router.list_issue_comments", return_value=[])
    @patch("review_bus.router.get_issue")
    def test_claude_handoff_sets_in_progress_state(
        self,
        mock_issue: MagicMock,
        _mock_comments: MagicMock,
        mock_create: MagicMock,
        _mock_update: MagicMock,
        mock_upsert: MagicMock,
    ) -> None:
        mock_issue.return_value = _make_issue([LABEL_CLAUDE_NEEDED])
        mock_create.return_value = {"id": 50}
        mock_upsert.return_value = {"id": 51}
        route("owner", "repo", 1)
        state = mock_upsert.call_args[0][4]
        assert state.status == STATUS_IN_PROGRESS
        assert state.next_actor == "claude"


# ---------------------------------------------------------------------------
# route — no labels → no-op
# ---------------------------------------------------------------------------


class TestRouteNoLabels:
    @patch("review_bus.router.upsert_bus_state")
    @patch("review_bus.router.list_issue_comments", return_value=[])
    @patch("review_bus.router.get_issue")
    def test_no_routing_label_does_not_update_state(
        self,
        mock_issue: MagicMock,
        _mock_comments: MagicMock,
        mock_upsert: MagicMock,
    ) -> None:
        mock_issue.return_value = _make_issue([])
        route("owner", "repo", 1)
        mock_upsert.assert_not_called()
