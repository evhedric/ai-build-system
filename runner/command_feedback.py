from __future__ import annotations

from typing import List


INVALID_COMMAND_REPLACEMENTS = {
    "pytest ": "python -m pytest ",
    "pip install ": "python -m pip install ",
}


def build_invalid_command_feedback(command: str) -> str:
    stripped = command.strip()

    for bad_prefix, good_prefix in INVALID_COMMAND_REPLACEMENTS.items():
        if stripped.startswith(bad_prefix):
            return f"Replace '{stripped}' with '{stripped.replace(bad_prefix, good_prefix, 1)}'."

    if "command to" in stripped.lower():
        return f"Remove the invalid placeholder step '{stripped}' entirely because it is not executable."

    return f"Remove or replace the invalid command '{stripped}' with a real local shell command."
