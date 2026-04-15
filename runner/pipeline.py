"""
Pipeline Orchestrator — the core of the AI orchestration system.

Drives each task through the full state machine:

  pending → (architect enriches task) → planning
  planning → (planner creates plan) → ready_for_execution
  ready_for_execution → executing
  executing → (executor runs plan) → in_review
  in_review → (reviewer evaluates) → approved | revise
  approved → (github ops) → complete
  revise → executing (up to max_revisions, then failed)
  any stage failure → failed

The runner calls `process_task(task)` for each task it picks up.
"""

from runner.config import MAX_REVISIONS
from runner.logger import runner_log, log_execution_event, log_error
from runner.schemas import load_task, load_plan, load_execution_result, save_task
from runner.state_manager import (
    transition_task, transition_task_to_failed, increment_revision
)
from runner.roles.architect import run_architect
from runner.roles.planner import run_planner
from runner.roles.executor import run_executor
from runner.roles.reviewer import run_reviewer
from runner.github_ops import handle_post_approval
from runner.plan_validator import validate_plan, PlanValidationError


def process_task(task: dict) -> dict:
    """
    Process a single task through the full pipeline.
    Handles state transitions, error catching, and revision loops.
    Returns the final task packet.
    """
    task_id = task["task_id"]
    runner_log.info("=" * 60)
    runner_log.info("[%s] Processing task: %s", task_id, task.get("title") or task.get("request", "")[:80])

    try:
        # ---------------------------------------------------------------
        # Stage 1: Architect enriches the task
        # ---------------------------------------------------------------
        task = transition_task(task_id, "planning")
        task = run_architect(task)

        # ---------------------------------------------------------------
        # Stage 2: Planner creates the execution plan
        # ---------------------------------------------------------------
        plan = run_planner(task)

        # Validate all run_command steps before execution begins.
        # Raises PlanValidationError (caught below) if any command is
        # malformed, uses a disallowed pattern, or has a forbidden prefix.
        try:
            validate_plan(plan)
            runner_log.info("[%s] Plan validation passed (%d steps)", task_id, len(plan.get("steps", [])))
            log_execution_event(task_id, "PLAN VALID", f"steps={len(plan.get('steps', []))}")
        except PlanValidationError as exc:
            runner_log.error("[%s] Plan validation FAILED: %s", task_id, exc)
            log_execution_event(task_id, "PLAN INVALID", str(exc))
            raise  # bubbles to outer except → task marked failed with clear reason

        # Store the branch name on the task for reference
        task["branch_name"] = plan["branch_name"]
        save_task(task)

        task = transition_task(task_id, "ready_for_execution")

        # ---------------------------------------------------------------
        # Stage 3: Execution + Review loop
        # ---------------------------------------------------------------
        while True:
            revision_count = task.get("revision_count", 0)
            max_revisions = task.get("max_revisions", MAX_REVISIONS)

            if revision_count > max_revisions:
                return transition_task_to_failed(
                    task_id,
                    f"Exceeded max revisions ({max_revisions})"
                )

            # Execute
            task = transition_task(task_id, "executing")
            task = load_task(task_id)  # Reload to get latest state

            execution_result = run_executor(task, plan)

            # Review
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
                    task_id, review.get("score", "?")
                )
                log_execution_event(task_id, "TASK COMPLETE")
                return load_task(task_id)

            else:
                # -------------------------------------------------------
                # Revise path
                # -------------------------------------------------------
                task = transition_task(task_id, "revise")
                task = increment_revision(task_id)
                task = load_task(task_id)

                revision_count = task["revision_count"]
                runner_log.info(
                    "[%s] Revision requested (%d/%d): %s",
                    task_id, revision_count, max_revisions,
                    "; ".join(review.get("revision_requests", []))[:120]
                )

                # Incorporate reviewer feedback into the plan for the next execution
                # by appending revision instructions to step details
                plan = _apply_revision_feedback(plan, review)
                # State is now "revise" — the loop top will transition to "executing"

    except Exception as e:
        log_error(task_id, "pipeline", e)
        return transition_task_to_failed(task_id, str(e))


def _apply_revision_feedback(plan: dict, review: dict) -> dict:
    """
    Augment plan steps with revision feedback so the executor addresses the issues.
    Appends a note to each step's details field with the reviewer's requests.
    """
    requests = review.get("revision_requests", [])
    if not requests:
        return plan

    feedback_note = "\n\nREVISION FEEDBACK FROM REVIEWER:\n" + "\n".join(f"- {r}" for r in requests)

    for step in plan.get("steps", []):
        step["details"] = step.get("details", "") + feedback_note

    return plan
