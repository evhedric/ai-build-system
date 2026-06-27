"""Unit tests for review_bus.client — submit, poll, status subcommands."""

from __future__ import annotations

from argparse import Namespace
from unittest.mock import MagicMock, patch

import pytest

from review_bus.github_issue import CHATGPT_REVIEW_MARKER
from review_bus.state import BUS_STATE_MARKER, BusState, format_bus_state_comment
from review_bus.client import cmd_poll, cmd_status, cmd_submit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_comment(comment_id: int, body: str) -> dict:
    return {"id": comment_id, "body": body, "user": {"login": "bot"}}


def _bus_state_comment(turn: int = 1, last_actor: str = "chatgpt") -> dict:
    state = BusState(turn=turn, last_actor=last_actor)
    return _make_comment(99, format_bus_state_comment(state))


def _chatgpt_review_comment() -> dict:
    return _make_comment(
        100,
        f"{CHATGPT_REVIEW_MARKER}\n# ChatGPT Review\n\n## Verdict\nRUN\n",
    )


# ---------------------------------------------------------------------------
# cmd_submit
# ---------------------------------------------------------------------------


class TestCmdSubmit:
    @patch("review_bus.client._save_state")
    @patch("review_bus.client._load_state", return_value={})
    @patch("review_bus.client._resolve_repo", return_value=("owner", "repo"))
    @patch("review_bus.client.create_issue_comment")
    @patch("review_bus.client.create_issue")
    def test_creates_new_issue_when_no_state(
        self,
        mock_create: MagicMock,
        mock_comment: MagicMock,
        _mock_repo: MagicMock,
        _mock_load: MagicMock,
        mock_save: MagicMock,
        tmp_path,
    ) -> None:
        mock_create.return_value = {
            "number": 7,
            "html_url": "https://github.com/owner/repo/issues/7",
        }
        mock_comment.return_value = {"id": 1}

        packet = tmp_path / "packet.md"
        packet.write_text("# Review Packet\nSome content.", encoding="utf-8")

        rc = cmd_submit(Namespace(packet=str(packet), issue_num=None))
        assert rc == 0
        mock_create.assert_called_once()
        title_arg = mock_create.call_args[0][2]
        assert "[AI Review Bus]" in title_arg
        mock_comment.assert_called_once()
        assert mock_comment.call_args[0][3] == "/chatgpt-review"
        saved = mock_save.call_args[0][0]
        assert saved["issue_number"] == 7

    @patch("review_bus.client._save_state")
    @patch("review_bus.client._load_state", return_value={"issue_number": 42})
    @patch("review_bus.client._resolve_repo", return_value=("owner", "repo"))
    @patch("review_bus.client.create_issue_comment")
    @patch("review_bus.client.create_issue")
    @patch("review_bus.client.update_issue")
    @patch("review_bus.client.get_issue")
    def test_reuses_open_issue(
        self,
        mock_get: MagicMock,
        mock_update: MagicMock,
        mock_create: MagicMock,
        mock_comment: MagicMock,
        _mock_repo: MagicMock,
        _mock_load: MagicMock,
        _mock_save: MagicMock,
        tmp_path,
    ) -> None:
        mock_get.return_value = {"number": 42, "state": "open"}
        mock_update.return_value = {"number": 42}
        mock_comment.return_value = {"id": 2}

        packet = tmp_path / "packet.md"
        packet.write_text("# Review Packet\nUpdated.", encoding="utf-8")

        rc = cmd_submit(Namespace(packet=str(packet), issue_num=None))
        assert rc == 0
        mock_create.assert_not_called()
        mock_update.assert_called_once()
        assert mock_update.call_args[0][2] == 42

    @patch("review_bus.client._save_state")
    @patch("review_bus.client._load_state", return_value={"issue_number": 42})
    @patch("review_bus.client._resolve_repo", return_value=("owner", "repo"))
    @patch("review_bus.client.create_issue_comment")
    @patch("review_bus.client.create_issue")
    @patch("review_bus.client.get_issue")
    def test_creates_new_issue_when_existing_closed(
        self,
        mock_get: MagicMock,
        mock_create: MagicMock,
        mock_comment: MagicMock,
        _mock_repo: MagicMock,
        _mock_load: MagicMock,
        _mock_save: MagicMock,
        tmp_path,
    ) -> None:
        mock_get.return_value = {"number": 42, "state": "closed"}
        mock_create.return_value = {
            "number": 43,
            "html_url": "https://github.com/owner/repo/issues/43",
        }
        mock_comment.return_value = {"id": 3}

        packet = tmp_path / "packet.md"
        packet.write_text("# Review Packet\nNew round.", encoding="utf-8")

        rc = cmd_submit(Namespace(packet=str(packet), issue_num=None))
        assert rc == 0
        mock_create.assert_called_once()

    def test_returns_error_when_packet_missing(self, tmp_path) -> None:
        rc = cmd_submit(Namespace(packet=str(tmp_path / "missing.md"), issue_num=None))
        assert rc == 1

    @patch("review_bus.client._save_state")
    @patch("review_bus.client._load_state", return_value={})
    @patch("review_bus.client._resolve_repo", return_value=("owner", "repo"))
    @patch("review_bus.client.create_issue_comment")
    @patch("review_bus.client.create_issue")
    def test_explicit_issue_num_reused_when_open(
        self,
        mock_create: MagicMock,
        mock_comment: MagicMock,
        _mock_repo: MagicMock,
        _mock_load: MagicMock,
        _mock_save: MagicMock,
        tmp_path,
    ) -> None:
        mock_comment.return_value = {"id": 4}
        with (
            patch("review_bus.client.get_issue", return_value={"state": "open"}),
            patch("review_bus.client.update_issue", return_value={}),
        ):
            packet = tmp_path / "packet.md"
            packet.write_text("# Review Packet\nHello.", encoding="utf-8")
            rc = cmd_submit(Namespace(packet=str(packet), issue_num=55))
        assert rc == 0
        mock_create.assert_not_called()


# ---------------------------------------------------------------------------
# cmd_poll
# ---------------------------------------------------------------------------


class TestCmdPoll:
    @patch("review_bus.client.time.sleep")
    @patch("review_bus.client.time.monotonic")
    @patch("review_bus.client.list_issue_comments")
    @patch("review_bus.client._resolve_repo", return_value=("owner", "repo"))
    @patch("review_bus.client._load_state", return_value={"issue_number": 5})
    def test_returns_review_when_found_immediately(
        self,
        _mock_load: MagicMock,
        _mock_repo: MagicMock,
        mock_comments: MagicMock,
        mock_monotonic: MagicMock,
        mock_sleep: MagicMock,
        capsys,
    ) -> None:
        # deadline = 0 + 300 = 300; loop check: 0 < 300 → True; returns immediately
        mock_monotonic.side_effect = [0, 0, 0]
        mock_comments.return_value = [_bus_state_comment(turn=1), _chatgpt_review_comment()]

        rc = cmd_poll(Namespace(issue_num=None, turn_before=0, timeout=300, interval=15))
        assert rc == 0
        captured = capsys.readouterr()
        assert "RUN" in captured.out
        mock_sleep.assert_not_called()

    @patch("review_bus.client.time.sleep")
    @patch("review_bus.client.time.monotonic")
    @patch("review_bus.client.list_issue_comments")
    @patch("review_bus.client._resolve_repo", return_value=("owner", "repo"))
    @patch("review_bus.client._load_state", return_value={"issue_number": 5})
    def test_times_out_when_no_review(
        self,
        _mock_load: MagicMock,
        _mock_repo: MagicMock,
        mock_comments: MagicMock,
        mock_monotonic: MagicMock,
        mock_sleep: MagicMock,
    ) -> None:
        # deadline = 0 + 300 = 300; loop check: 400 < 300 → False; exits immediately
        mock_monotonic.side_effect = [0, 400]
        mock_comments.return_value = []

        rc = cmd_poll(Namespace(issue_num=None, turn_before=-1, timeout=300, interval=15))
        assert rc == 1

    @patch("review_bus.client.time.sleep")
    @patch("review_bus.client.time.monotonic")
    @patch("review_bus.client.list_issue_comments")
    @patch("review_bus.client._resolve_repo", return_value=("owner", "repo"))
    @patch("review_bus.client._load_state", return_value={"issue_number": 5})
    def test_skips_review_if_turn_not_advanced(
        self,
        _mock_load: MagicMock,
        _mock_repo: MagicMock,
        mock_comments: MagicMock,
        mock_monotonic: MagicMock,
        mock_sleep: MagicMock,
    ) -> None:
        # turn_before=1, bus state turn=1 — not advanced; next monotonic exceeds deadline
        mock_monotonic.side_effect = [0, 0, 0, 400]
        mock_comments.return_value = [
            _bus_state_comment(turn=1, last_actor="chatgpt"),
            _chatgpt_review_comment(),
        ]

        rc = cmd_poll(Namespace(issue_num=None, turn_before=1, timeout=300, interval=15))
        assert rc == 1

    def test_returns_error_when_no_issue_number(self) -> None:
        with (
            patch("review_bus.client._resolve_repo", return_value=("o", "r")),
            patch("review_bus.client._load_state", return_value={}),
        ):
            rc = cmd_poll(
                Namespace(issue_num=None, turn_before=-1, timeout=300, interval=15)
            )
        assert rc == 1


# ---------------------------------------------------------------------------
# cmd_status
# ---------------------------------------------------------------------------


class TestCmdStatus:
    @patch("review_bus.client.list_issue_comments")
    @patch("review_bus.client._resolve_repo", return_value=("owner", "repo"))
    @patch("review_bus.client._load_state", return_value={"issue_number": 10})
    def test_prints_state_comment(
        self,
        _mock_load: MagicMock,
        _mock_repo: MagicMock,
        mock_comments: MagicMock,
        capsys,
    ) -> None:
        mock_comments.return_value = [_bus_state_comment(turn=2)]
        rc = cmd_status(Namespace(issue_num=None))
        assert rc == 0
        captured = capsys.readouterr()
        assert BUS_STATE_MARKER in captured.out

    @patch("review_bus.client.list_issue_comments")
    @patch("review_bus.client._resolve_repo", return_value=("owner", "repo"))
    @patch("review_bus.client._load_state", return_value={"issue_number": 10})
    def test_no_state_comment_prints_message(
        self,
        _mock_load: MagicMock,
        _mock_repo: MagicMock,
        mock_comments: MagicMock,
        capsys,
    ) -> None:
        mock_comments.return_value = []
        rc = cmd_status(Namespace(issue_num=None))
        assert rc == 0
        captured = capsys.readouterr()
        assert "no bus state comment" in captured.out

    def test_returns_error_when_no_issue_number(self) -> None:
        with (
            patch("review_bus.client._resolve_repo", return_value=("o", "r")),
            patch("review_bus.client._load_state", return_value={}),
        ):
            rc = cmd_status(Namespace(issue_num=None))
        assert rc == 1


# ---------------------------------------------------------------------------
# _resolve_repo
# ---------------------------------------------------------------------------


class TestResolveRepo:
    def test_reads_from_env_var(self, monkeypatch) -> None:
        from review_bus.client import _resolve_repo

        monkeypatch.setenv("GITHUB_REPOSITORY", "myorg/myrepo")
        owner, repo = _resolve_repo()
        assert owner == "myorg"
        assert repo == "myrepo"

    def test_raises_when_no_env_and_no_remote(self, monkeypatch) -> None:
        from review_bus.client import _resolve_repo

        monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
        with patch("review_bus.client.subprocess.run", side_effect=Exception("no remote")):
            with pytest.raises(RuntimeError, match="Cannot determine"):
                _resolve_repo()
