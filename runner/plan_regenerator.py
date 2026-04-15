from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List

from runner.plan_validator import PlanValidationError, validate_plan
from runner.command_feedback import build_invalid_command_feedback

# Matches the command inside reason strings produced by plan_validator and
# executor_guard, e.g.:
#   "step 2 invalid command 'pytest tests/': bare pytest is forbidden..."
#   "executor blocked invalid command 'pip install X': ..."
_COMMAND_RE = re.compile(r"invalid command '([^']+)'")


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


def _extract_commands(reasons: List[str]) -> List[str]:
    """
    Pull the bad command strings out of PlanValidationError reason messages.
    Returns deduplicated commands in the order first seen.
    """
    seen: set[str] = set()
    commands: List[str] = []
    for reason in reasons:
        m = _COMMAND_RE.search(reason)
        if m:
            cmd = m.group(1)
            if cmd not in seen:
                seen.add(cmd)
                commands.append(cmd)
    return commands


def build_revision_feedback(existing_feedback: str, validation_error: PlanValidationError) -> str:
    """
    Append the current validation failure — plus command-specific fix
    suggestions — to any previously accumulated feedback.

    The planner receives two blocks per failure:

        PLAN REJECTED BEFORE EXECUTION: <raw reason 1>; <raw reason 2>
        HOW TO FIX:
          - Replace 'pytest tests/' with 'python -m pytest tests/'.
          - Remove the invalid placeholder step 'command to ...' entirely...

    The HOW TO FIX block is only emitted when bad commands can be extracted
    from the reason strings; otherwise the raw reasons alone are returned.
    """
    validation_message = "PLAN REJECTED BEFORE EXECUTION: " + "; ".join(validation_error.reasons)

    bad_commands = _extract_commands(validation_error.reasons)
    if bad_commands:
        fix_lines = "\n".join(
            f"  - {build_invalid_command_feedback(cmd)}"
            for cmd in bad_commands
        )
        validation_message += f"\nHOW TO FIX:\n{fix_lines}"

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
