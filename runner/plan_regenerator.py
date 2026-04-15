from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List

from runner.plan_validator import PlanValidationError, validate_plan


@dataclass
class RegenerationSignal:
    """
    Raised (or returned) to indicate a plan must be regenerated.
    Carries the human-readable reason for the rejection.
    """
    reason: str


def validate_plan_or_regenerate(plan: Dict[str, Any]) -> None:
    """
    Validate the plan.  Currently a direct delegate to validate_plan();
    the separate function exists so callers can be extended (e.g. to
    return a RegenerationSignal instead of raising) without touching
    validate_plan itself.
    """
    validate_plan(plan)


def build_revision_feedback(existing_feedback: str, validation_error: PlanValidationError) -> str:
    """
    Append the current validation failure to any previously accumulated
    feedback, producing the string that will be passed back to the planner.

    Example output (second failure after a first):
        PLAN REJECTED BEFORE EXECUTION: bare pytest is forbidden; ...
        PLAN REJECTED BEFORE EXECUTION: command must start with one of: ...
    """
    validation_message = "PLAN REJECTED BEFORE EXECUTION: " + "; ".join(validation_error.reasons)
    if existing_feedback:
        return existing_feedback + "\n" + validation_message
    return validation_message


def should_fail_for_repeated_invalid_pattern(previous_feedback: List[str], new_feedback: str) -> bool:
    """
    Return True when the planner is stuck: it produced the exact same
    validation failure message on a previous attempt.

    Args:
        previous_feedback: individual (non-accumulated) error strings from
                           earlier failed plan attempts.
        new_feedback:      the individual error string for the current attempt.

    Returns:
        True  → the system should stop retrying and fail the task.
        False → this is a new error (or the first attempt); safe to retry.
    """
    if not previous_feedback:
        return False
    repeated = sum(1 for item in previous_feedback if item == new_feedback)
    return repeated >= 1
