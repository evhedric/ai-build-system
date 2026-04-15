"""
Pipeline Orchestrator — the core of the AI orchestration system.

Drives each task through the full state machine:

  pending -> (architect enriches task) -> planning
  planning -> (planner creates plan + validates) -> ready_for_execution
  ready_for_execution -> executing
  executing -> (executor runs plan) -> in_review
  in_review -> (reviewer evaluates) -> approved | revise
  approved -> (github ops) -> complete
  revise -> (planner re-plans with feedback) -> executing
  any stage failure -> failed

The runner calls `process_task(task)` for each task it picks up.
"""

from runner.config import MAX_REVISIONS
from runner.logger import runner_log, log_execution_event, log_error
from runner.schemas import load_task, save_task
from runner.state_manager import (
    transition_task, transition_task_to_failed, increment_revision
)
from runner.roles.architect import run_architect
from runner.roles.planner import run_planner
from runner.roles.executor import run_executor
from runner.roles.reviewer import run_reviewer
from runner.github_ops import handle_post_approval
from runner.plan_validator import PlanValidationError


def process_task(task: dict) -> dict:
    """
    Process a single task through the full pipeline.
    Handles state transitions, error catching, and revision loops.
    Returns the final task packet.
    """
    task_id = task["task_id"]
    runner_log.info("=" * 60)
    runner_log.info(
        "[%s] Processing task: %s",
        task_id,
        task.get("title") or task.get("request", "")[:80],
    )

    try:
        # ---------------------------------------------------------------
        # Stage 1: Architect enriches the task
        # ---------------------------------------------------------------
        task = transition_task(task_id, "planning")
        task = run_architect(task)

        # ---------------------------------------------------------------
        # Stage 2: Initial plan
        # run_planner() calls validate_plan() internally — if commands are
        # invalid it raises PlanValidationError which lands in the outer
        # except and marks the task failed with a descriptive reason.
        # ---------------------------------------------------------------
        plan = run_planner(task)
        runner_log.info(
            "[%s] Plan valid (%d steps, branch=%s)",
            task_id, len(plan.get("steps", [])), plan["branch_name"],
        )

        # Store the branch name on the task for reference
        task["branch_name"] = plan["branch_name"]
        save_task(task)

        task = transition_task(task_id, "ready_for_execution")

        # ---------------------------------------------------------------
        # Stage 3: Execution + Review loop
        # On revision cycles the planner is re-invoked with the reviewer's
        # feedback so it generates a corrected plan from scratch, rather
        # than just appending notes to the old one.
        # ---------------------------------------------------------------
        while True:
            revision_count = task.get("revision_count", 0)
            max_revisions  = task.get("max_revisions", MAX_REVISIONS)

            if revision_count > max_revisions:
                return transition_task_to_failed(
                    task_id,
                    f"Exceeded max revisions ({max_revisions})",
                )

            # --- Execute ------------------------------------------------
            task = transition_task(task_id, "executing")
            task = load_task(task_id)

            execution_result = run_executor(task, plan)

            # --- Review -------------------------------------------------
            task = transition_task(task_id, "in_review")
            task = load_task(task_id)

            review = run_reviewer(task, execution_result)

            if review["decision"] == "approved":
                # -------------------------------------------------------
                # Approved path
                # -------------------------------------------------------
                task = transition_task(task_id, "approved")
                task = load_task(task_id)
                task = handle_post_approval(task, review)
                task = transition_task(task_id, "complete")
                runner_log.info(
                    "[%s] TASK COMPLETE (score=%d/10)",
                    task_id, review.get("score", "?"),
                )
                log_execution_event(task_id, "TASK COMPLETE")
                return load_task(task_id)

            else:
                # -------------------------------------------------------
                # Revise path — re-plan with reviewer feedback
                # -------------------------------------------------------
                task = transition_task(task_id, "revise")
                task = increment_revision(task_id)
                task = load_task(task_id)

                revision_count = task["revision_count"]
                requests = review.get("revision_requests", [])
                runner_log.info(
                    "[%s] Revision requested (%d/%d): %s",
                    task_id, revision_count, max_revisions,
                    "; ".join(requests)[:120],
                )

                # Build a concise feedback string for the planner
                revision_feedback = _build_revision_feedback(review)

                # Re-plan: planner gets the reviewer's exact complaints so
                # it can generate corrected steps rather than repeating them.
                plan = run_planner(task, revision_feedback=revision_feedback)
                runner_log.info(
                    "[%s] Re-plan complete (%d steps)",
                    task_id, len(plan.get("steps", [])),
                )
                # State is now "revise" — loop top transitions to "executing"

    except PlanValidationError as exc:
        # Surface each bad step individually in the failure reason
        log_error(task_id, "plan_validation", exc)
        return transition_task_to_failed(task_id, f"Plan validation failed: {exc}")

    except Exception as exc:
        log_error(task_id, "pipeline", exc)
        return transition_task_to_failed(task_id, str(exc))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_revision_feedback(review: dict) -> str:
    """
    Produce a concise feedback string from a review packet for the planner.
    Includes the reasoning and every specific revision request.
    """
    lines = ["PREVIOUS ATTEMPT WAS REJECTED BY THE REVIEWER."]

    reasoning = review.get("reasoning", "").strip()
    if reasoning:
        lines.append(f"Reason: {reasoning}")

    requests = review.get("revision_requests", [])
    if requests:
        lines.append("Required changes:")
        lines.extend(f"  - {r}" for r in requests)

    failed = review.get("criteria_failed", [])
    if failed:
        lines.append("Criteria not met:")
        lines.extend(f"  - {c}" for c in failed)

    return "\n".join(lines)
