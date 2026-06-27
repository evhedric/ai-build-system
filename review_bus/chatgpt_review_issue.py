"""GitHub Issue → ChatGPT Review command.

Triggered by GitHub Actions when someone comments exactly:

    /chatgpt-review

The script reads the latest Review Packet from the issue, asks OpenAI for a
review, and creates or updates the latest ChatGPT review issue comment.
"""

from __future__ import annotations

import os

from openai import OpenAI

from review_bus.github_issue import (
    get_issue,
    get_repo_context,
    list_issue_comments,
    upsert_chatgpt_review_comment,
)
from review_bus.packet import (
    assert_exact_trigger,
    find_latest_review_packet,
    get_recent_comment_limit,
    recent_context,
)
from review_bus.prompts import build_chatgpt_review_prompt


def _require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _call_openai(prompt: str) -> str:
    api_key = _require_env("OPENAI_API_KEY")
    model = os.getenv("REVIEW_OPENAI_MODEL", os.getenv("OPENAI_MODEL", "gpt-4o"))

    client = OpenAI(api_key=api_key)

    # Use Chat Completions for compatibility with the repo's existing
    # openai>=1.30.0 dependency.
    response = client.chat.completions.create(
        model=model,
        temperature=0,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a strict engineering review governor. "
                    "You must return a concrete RUN or NO-RUN style review."
                ),
            },
            {"role": "user", "content": prompt},
        ],
    )

    content = response.choices[0].message.content
    if not content:
        raise RuntimeError("OpenAI returned empty review content.")
    return content


def main() -> None:
    assert_exact_trigger(os.getenv("TRIGGER_COMMENT_BODY", ""))

    owner, repo, issue_number = get_repo_context()

    issue = get_issue(owner, repo, issue_number)
    comments = list_issue_comments(owner, repo, issue_number)

    packet = find_latest_review_packet(issue.get("body") or "", comments)
    context = recent_context(comments, get_recent_comment_limit())

    prompt = build_chatgpt_review_prompt(
        review_packet=packet,
        recent_context=context,
    )

    review = _call_openai(prompt)

    upsert_chatgpt_review_comment(owner, repo, issue_number, comments, review)


if __name__ == "__main__":
    main()
