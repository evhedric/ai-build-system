"""Unit tests for the ChatGPT review comment upsert helpers."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from review_bus.github_issue import (
    CHATGPT_REVIEW_MARKER,
    find_comment_with_marker,
    format_chatgpt_review_comment,
    upsert_chatgpt_review_comment,
)


# ---------------------------------------------------------------------------
# format_chatgpt_review_comment
# ---------------------------------------------------------------------------

def test_format_prepends_marker_and_heading() -> None:
    result = format_chatgpt_review_comment("Some review body.")
    assert result.startswith(CHATGPT_REVIEW_MARKER)
    assert "# ChatGPT Review" in result
    assert "Some review body." in result


def test_format_does_not_duplicate_heading() -> None:
    review = "# ChatGPT Review\n\nAlready has heading."
    result = format_chatgpt_review_comment(review)
    assert result.count("# ChatGPT Review") == 1


def test_format_raises_on_empty_review() -> None:
    with pytest.raises(RuntimeError, match="empty"):
        format_chatgpt_review_comment("   ")


def test_format_ends_with_newline() -> None:
    result = format_chatgpt_review_comment("body")
    assert result.endswith("\n")


# ---------------------------------------------------------------------------
# find_comment_with_marker
# ---------------------------------------------------------------------------

def test_find_returns_matching_comment() -> None:
    comments = [
        {"id": 1, "body": "Normal comment"},
        {"id": 2, "body": f"{CHATGPT_REVIEW_MARKER}\n# ChatGPT Review\n\nOld review."},
        {"id": 3, "body": "Another comment"},
    ]
    found = find_comment_with_marker(comments)
    assert found is not None
    assert found["id"] == 2


def test_find_returns_none_when_no_marker() -> None:
    comments = [
        {"id": 1, "body": "No marker here"},
        {"id": 2, "body": "Also nothing"},
    ]
    assert find_comment_with_marker(comments) is None


def test_find_handles_empty_list() -> None:
    assert find_comment_with_marker([]) is None


def test_find_handles_none_body() -> None:
    comments = [{"id": 1, "body": None}]
    assert find_comment_with_marker(comments) is None


# ---------------------------------------------------------------------------
# upsert_chatgpt_review_comment
# ---------------------------------------------------------------------------

@patch("review_bus.github_issue.create_issue_comment")
def test_upsert_creates_when_no_existing_marker(mock_create: MagicMock) -> None:
    mock_create.return_value = {"id": 99}
    comments: list = []

    result = upsert_chatgpt_review_comment("owner", "repo", 1, comments, "review text")

    mock_create.assert_called_once()
    assert result == {"id": 99}


@patch("review_bus.github_issue.update_issue_comment")
def test_upsert_updates_when_marker_exists(mock_update: MagicMock) -> None:
    mock_update.return_value = {"id": 42}
    comments = [
        {"id": 42, "body": f"{CHATGPT_REVIEW_MARKER}\n# ChatGPT Review\n\nOld."},
    ]

    result = upsert_chatgpt_review_comment("owner", "repo", 1, comments, "new review")

    mock_update.assert_called_once_with("owner", "repo", 42, mock_update.call_args[0][3])
    assert result == {"id": 42}


@patch("review_bus.github_issue.update_issue_comment")
@patch("review_bus.github_issue.create_issue_comment")
def test_upsert_does_not_create_when_marker_exists(
    mock_create: MagicMock, mock_update: MagicMock
) -> None:
    mock_update.return_value = {"id": 42}
    comments = [
        {"id": 42, "body": f"{CHATGPT_REVIEW_MARKER}\n# ChatGPT Review\n\nOld."},
    ]

    upsert_chatgpt_review_comment("owner", "repo", 1, comments, "new review")

    mock_create.assert_not_called()
    mock_update.assert_called_once()
