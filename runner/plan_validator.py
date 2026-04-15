from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from typing import Iterable, List


DISALLOWED_PATTERNS = [
    r"\bcommand to\b",
    r"\btest\b",
    r"\bverify\b",
    r"\bcheck\b",
    r"\bensure\b",
    r"\bplaceholder\b",
    r"\bexample\b",
    r"\betc\b",
    r"\bthe following\b",
]

ALLOWED_COMMAND_PREFIXES = (
    "python ",
    "python -m ",
    "py ",
    "git ",
    "pip ",
)


@dataclass
class CommandValidationResult:
    valid: bool
    reasons: List[str]


class PlanValidationError(Exception):
    def __init__(self, reasons: Iterable[str]):
        self.reasons = list(reasons)
        super().__init__("; ".join(self.reasons))


def _has_disallowed_text(command: str) -> List[str]:
    reasons: List[str] = []
    stripped = command.strip().lower()

    for pattern in DISALLOWED_PATTERNS:
        if re.search(pattern, stripped):
            reasons.append(f"disallowed pattern matched: {pattern}")

    if stripped.startswith("pytest "):
        reasons.append("bare pytest is forbidden; use 'python -m pytest ...'")

    if stripped.startswith("pip install "):
        reasons.append("bare pip install is forbidden; use 'python -m pip install ...'")

    return reasons


def _is_executable_shell_command(command: str) -> List[str]:
    reasons: List[str] = []
    stripped = command.strip()

    if not stripped:
        reasons.append("command is empty")
        return reasons

    if "\n" in stripped:
        reasons.append("command must be a single executable shell command")

    if not stripped.startswith(ALLOWED_COMMAND_PREFIXES):
        reasons.append(
            "command must start with one of: "
            + ", ".join(ALLOWED_COMMAND_PREFIXES)
        )

    try:
        shlex.split(stripped, posix=False)
    except ValueError as exc:
        reasons.append(f"command is not parseable: {exc}")

    return reasons


def validate_run_command(command: str) -> CommandValidationResult:
    reasons: List[str] = []
    reasons.extend(_has_disallowed_text(command))
    reasons.extend(_is_executable_shell_command(command))
    return CommandValidationResult(valid=len(reasons) == 0, reasons=reasons)


def validate_plan(plan: dict) -> None:
    reasons: List[str] = []

    steps = plan.get("steps", [])
    for index, step in enumerate(steps, start=1):
        if step.get("action_type") != "run_command":
            continue
        command = step.get("target", step.get("command", ""))
        result = validate_run_command(command)
        if not result.valid:
            reasons.extend(
                [f"step {index} invalid command '{command}': {reason}" for reason in result.reasons]
            )

    if reasons:
        raise PlanValidationError(reasons)
