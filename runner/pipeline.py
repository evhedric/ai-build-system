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
from runner.plan_regenerator import build_revision_feedback, should_fail_for_repeated_invalid_pattern
from runner.projects.project_registry import resolve_project_from_task
from runner.workspace.provisioner import provision_workspace


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
        # Stage 1.5: Project resolution + workspace provisioning
        #
        # resolve_project_from_task() looks up "project_id" (Gold v3) or
        # the legacy "project" field in the task packet.  Raises ValueError
        # immediately if neither is present — no silent defaults.
        #
        # provision_workspace() creates an isolated git worktree for this
        # task inside the project's workspace_root.  The returned descriptor
        # is threaded through to the executor so all writes land in the
        # correct directory and on the correct branch.
        # ---------------------------------------------------------------
        project      = resolve_project_from_task(task)
        workspace_info = provision_workspace(task, project)

        runner_log.info(
            "[%s] Workspace provisioned | project=%s workspace=%s branch=%s",
            task_id,
            workspace_info["project_id"],
            workspace_info["workspace_dir"],
            workspace_info["branch_name"],
        )
        log_execution_event(
            task_id,
            "WORKSPACE READY",
            f"project={workspace_info['project_id']}  "
            f"branch={workspace_info['branch_name']}  "
            f"dir={workspace_info['workspace_dir']}",
        )

        # ---------------------------------------------------------------
        # Stage 2: Plan generation with pre-execution validation retry
        #
        # run_planner() calls validate_plan() internally.  If the model
        # produces invalid run_command steps, we feed the error back as
        # revision_feedback and retry rather than immediately failing.
        # should_fail_for_repeated_invalid_pattern() detects when the model
        # is stuck producing the same bad command on successive attempts and
        # escalates to a hard failure at that point.
        # ---------------------------------------------------------------
        plan = _generate_valid_plan(task_id, task)
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

            execution_result = run_executor(task, plan, workspace_info)

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
        # Reached only if _generate_valid_plan's loop-detection gave up
        log_error(task_id, "plan_validation", exc)
        return transition_task_to_failed(
            task_id,
            f"Planner stuck in invalid command loop: {exc}",
        )

    except Exception as exc:
        log_error(task_id, "pipeline", exc)
        return transition_task_to_failed(task_id, str(exc))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _generate_valid_plan(task_id: str, task: dict) -> dict:
    """
    Call run_planner() in a retry loop, feeding validation errors back as
    revision_feedback until the planner produces a valid plan.

    Two separate pieces of state are maintained:
      accumulated_feedback   — grows with every failure; sent to the planner
                               so it has the full history of what went wrong.
      past_individual_errors — list of individual (non-accumulated) error
                               strings used to detect repetition.

    The loop gives up (re-raises PlanValidationError) when
    should_fail_for_repeated_invalid_pattern() signals that the model has
    produced the same bad command on at least two separate attempts.
    """
    accumulated_feedback: str = ""
    past_individual_errors: list[str] = []

    while True:
        try:
            plan = run_planner(
                task,
                revision_feedback=accumulated_feedback or None,
            )
            return plan  # valid — exit the loop

        except PlanValidationError as exc:
            # Build the individual error string (no prior context) for
            # loop-detection comparison.
            individual_error = build_revision_feedback("", exc)

            if should_fail_for_repeated_invalid_pattern(past_individual_errors, individual_error):
                runner_log.error(
                    "[%s] Planner stuck: repeated invalid pattern — %s",
                    task_id, individual_error,
                )
                log_execution_event(task_id, "PLAN LOOP DETECTED", individual_error[:200])
                raise  # escalate to process_task's outer handler

            # New (unseen) validation failure — accumulate and retry
            past_individual_errors.append(individual_error)
            accumulated_feedback = build_revision_feedback(accumulated_feedback, exc)

            runner_log.warning(
                "[%s] Plan invalid (%d unique error(s) so far) — retrying with feedback",
                task_id, len(past_individual_errors),
            )
            log_execution_event(
                task_id,
                "PLAN INVALID — RETRY",
                individual_error[:200],
            )


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
