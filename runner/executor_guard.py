from __future__ import annotations

from typing import Dict, Any

from runner.plan_validator import PlanValidationError, validate_run_command


def guard_run_command(step: Dict[str, Any]) -> None:
    command = step.get("command", "")
    result = validate_run_command(command)
    if not result.valid:
        raise PlanValidationError(
            [f"executor blocked invalid command '{command}': {reason}" for reason in result.reasons]
        )
