"""
Reviewer Role — powered by OpenAI.

Responsibility: Evaluate the ExecutionResult against the task's success criteria
and return a structured decision: "approved" or "revise".

The reviewer has no ability to directly modify files — it can only judge what
the executor produced and provide actionable feedback for revision.
"""

import json

from runner.config import OPENAI_API_KEY, OPENAI_MODEL, PROMPTS_DIR
from runner.logger import reviewer_log, log_execution_event
from runner.schemas import make_review_packet, save_review

try:
    from openai import OpenAI
    _openai_available = True
except ImportError:
    _openai_available = False


def _load_prompt() -> str:
    path = PROMPTS_DIR / "reviewer.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


def run_reviewer(task: dict, execution_result: dict) -> dict:
    """
    Review the execution result and produce a ReviewPacket.
    Returns the ReviewPacket dict and saves it to /reviews/.
    """
    task_id = task["task_id"]
    log_execution_event(task_id, "REVIEWER START")
    reviewer_log.info("[%s] Reviewing execution result for: %s", task_id, task.get("title"))

    if not _openai_available:
        raise RuntimeError("openai package not installed. Run: pip install openai")
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not set in your .env file")

    system_prompt = _load_prompt()

    # Build the review context — include task goals, criteria, and what was done
    review_context = {
        "task": {
            "task_id": task["task_id"],
            "title": task["title"],
            "request": task["request"],
            "goals": task.get("goals", []),
            "success_criteria": task.get("success_criteria", []),
            "constraints": task.get("constraints", []),
            "revision_count": task.get("revision_count", 0),
            "max_revisions": task.get("max_revisions", 3),
        },
        "execution_result": execution_result,
    }

    # Also load created file contents for the reviewer to inspect
    from runner.config import BASE_DIR
    file_snapshots = {}
    for file_path in execution_result.get("files_created", []) + execution_result.get("files_modified", []):
        full_path = BASE_DIR / file_path
        if full_path.exists():
            try:
                content = full_path.read_text(encoding="utf-8")
                file_snapshots[file_path] = content[:3000]  # Cap at 3k chars per file
            except Exception:
                file_snapshots[file_path] = "[could not read file]"
    if file_snapshots:
        review_context["file_contents"] = file_snapshots

    user_message = (
        "Review the following task execution and return your structured decision.\n\n"
        + json.dumps(review_context, indent=2)
    )

    client = OpenAI(api_key=OPENAI_API_KEY)
    response = client.chat.completions.create(
        model=OPENAI_MODEL,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        temperature=0.2,
    )

    raw = response.choices[0].message.content
    reviewer_log.debug("[%s] Raw reviewer response: %s", task_id, raw)

    try:
        review_data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"Reviewer returned invalid JSON: {e}\nRaw: {raw}") from e

    # Validate
    decision = review_data.get("decision", "").lower()
    if decision not in ("approved", "revise"):
        raise ValueError(
            f"Reviewer returned invalid decision: {decision!r}. Must be 'approved' or 'revise'."
        )

    review = make_review_packet(
        task_id=task_id,
        decision=decision,
        score=int(review_data.get("score", 5)),
        reasoning=review_data.get("reasoning", ""),
        criteria_met=review_data.get("criteria_met", []),
        criteria_failed=review_data.get("criteria_failed", []),
        revision_requests=review_data.get("revision_requests", []),
        next_steps=review_data.get("next_steps", []),
    )

    save_review(review)
    reviewer_log.info(
        "[%s] Reviewer decision: %s (score=%d/10)",
        task_id, decision.upper(), review["score"]
    )
    log_execution_event(
        task_id, "REVIEWER COMPLETE",
        f"decision={decision!r}, score={review['score']}/10"
    )
    return review
