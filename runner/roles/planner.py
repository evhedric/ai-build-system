"""
Planner Role — powered by OpenAI.

Converts a structured task packet into an ordered execution plan with validated
run_command steps. Validation (via plan_validator) happens immediately at parse
time so the pipeline never receives a plan with bad commands.

On revision cycles the caller passes revision_feedback, which is embedded in
the user message so the model knows exactly what to fix.

Step field schema (what the LLM returns)
-----------------------------------------
  type     : create_file | modify_file | run_command | research | document
  title    : short description
  target   : repo-relative file path (create_file / modify_file / document)
           | topic string (research)
  command  : executable shell command (run_command only)
  content  : file content (create_file / modify_file — optional; executor
             uses Claude to generate content if absent)

Normalisation (internal → rest of system)
------------------------------------------
  type     → action_type
  command  → target   (for run_command steps)
  title    → description
  content  → details  (carries existing file content as context)
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from runner.config import OPENAI_API_KEY, OPENAI_MODEL, PROMPTS_DIR
from runner.logger import planner_log, log_execution_event
from runner.schemas import make_plan_packet, save_plan
from runner.plan_validator import validate_plan, PlanValidationError

try:
    from openai import OpenAI
    _openai_available = True
except ImportError:
    _openai_available = False


# ---------------------------------------------------------------------------
# Prompt — written to disk for inspection / manual editing
# ---------------------------------------------------------------------------

PLANNER_PROMPT = """You are the Planner in a local autonomous AI build system.

You must return a JSON object with a top-level "steps" array.
Each step must contain:
- "title"
- "type" (one of: create_file, modify_file, run_command, research, document)
- "target"
- "content" (required for create_file and modify_file)
- "command" (required for run_command)

STRICT RULES:
1. Every run_command step must be a real, executable shell command.
2. NEVER use placeholder or vague text such as:
   - command to ...
   - test ...
   - verify ...
   - check ...
   - ensure ...
3. ALL pytest commands MUST use:
   python -m pytest <path>
4. NEVER use:
   pytest <path>
5. ALL dependency installation commands MUST use:
   python -m pip install <package>
6. NEVER use:
   pip install <package>
7. If no valid executable command can be determined, OMIT the run_command step entirely.
8. Steps must be concrete, repo-relative, and directly actionable in a local Python environment.
9. If revision feedback is provided, you MUST change the failing step instead of repeating it.
10. Do not repeat a previously failed invalid command pattern.
11. ALL commands that scaffold or initialize a project MUST be fully non-interactive.
    This system runs autonomously — there is no user present to answer prompts.
    ALWAYS include flags that suppress interactive prompts, for example:
      - npx create-next-app@latest . --typescript --tailwind --eslint --app --src-dir --use-npm --yes
    NEVER use a bare scaffolding command without --yes or equivalent suppression flags.

Return only valid JSON.
"""

_PROMPT_FILE = PROMPTS_DIR / "planner.txt"


def _ensure_prompt_file() -> None:
    """Write the canonical prompt to disk so it can be inspected / overridden."""
    _PROMPT_FILE.parent.mkdir(parents=True, exist_ok=True)
    _PROMPT_FILE.write_text(PLANNER_PROMPT, encoding="utf-8")


def _load_prompt() -> str:
    """Return the prompt from disk (allows manual edits to take effect)."""
    _ensure_prompt_file()
    return _PROMPT_FILE.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Message builder
# ---------------------------------------------------------------------------

def build_planner_messages(
    task: dict[str, Any],
    revision_feedback: str | None = None,
) -> list[dict[str, str]]:
    """Return the [system, user] messages list for the OpenAI chat call."""
    user_payload: dict[str, Any] = {
        "title":            task.get("title", ""),
        "request":          task.get("request", ""),
        "goals":            task.get("goals", []),
        "constraints":      task.get("constraints", []),
        "success_criteria": task.get("success_criteria", []),
        "revision_feedback": revision_feedback or "",
    }
    return [
        {"role": "system", "content": _load_prompt()},
        {"role": "user",   "content": json.dumps(user_payload, ensure_ascii=False)},
    ]


# ---------------------------------------------------------------------------
# Output parsing + validation
# ---------------------------------------------------------------------------

def parse_planner_output(raw_text: str) -> dict[str, Any]:
    """
    Parse the raw LLM response, validate all run_command steps, and return
    the plan dict.  Raises PlanValidationError if any command is invalid.
    """
    plan = json.loads(raw_text)
    validate_plan(plan)          # raises PlanValidationError on bad commands
    return plan


# ---------------------------------------------------------------------------
# Schema normalisation
# ---------------------------------------------------------------------------

def _normalise_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Convert planner-schema fields to the system-wide step schema consumed by
    the executor and state manager.

    Planner schema  →  System schema
    ─────────────────────────────────
    type            →  action_type
    title           →  description
    command         →  target          (run_command only)
    content         →  details         (create_file / modify_file)
    target          →  target          (file path — kept as-is for non-command steps)
    """
    normalised = []
    for i, step in enumerate(steps, start=1):
        action_type = step.get("type") or step.get("action_type", "document")
        target      = step.get("target", "")
        command     = step.get("command", "")

        normalised.append({
            "step_id":     step.get("step_id", i),
            "description": step.get("title") or step.get("description", ""),
            "action_type": action_type,
            # run_command steps: the executable command goes into "target"
            # all other steps: the file path stays in "target"
            "target":      command if action_type == "run_command" else target,
            # content / details carries existing file text as context for executor
            "details":     step.get("content") or step.get("details", ""),
        })
    return normalised


# ---------------------------------------------------------------------------
# Branch name helper
# ---------------------------------------------------------------------------

def _make_branch_name(task: dict[str, Any]) -> str:
    title = task.get("title", task["task_id"])
    slug  = re.sub(r"[^a-zA-Z0-9]+", "-", title).strip("-").lower()[:50]
    return f"task/{task['task_id']}/{slug}"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_planner(task: dict[str, Any], revision_feedback: str | None = None) -> dict[str, Any]:
    """
    Generate (or re-generate) a plan for the given task.

    Args:
        task:              The task packet.
        revision_feedback: On revision cycles, a summary of what the previous
                           plan got wrong.  Injected into the user message so
                           the model knows what to fix.

    Returns:
        The normalised PlanPacket dict, already saved to /plans/.
    """
    task_id = task["task_id"]
    is_revision = bool(revision_feedback)
    log_execution_event(task_id, "PLANNER START" + (" (revision)" if is_revision else ""))
    planner_log.info(
        "[%s] Running planner for: %s%s",
        task_id,
        task.get("title"),
        " [REVISION]" if is_revision else "",
    )

    if not _openai_available:
        raise RuntimeError("openai package not installed. Run: pip install openai")
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not set in your .env file")

    messages = build_planner_messages(task, revision_feedback=revision_feedback)

    client = OpenAI(api_key=OPENAI_API_KEY)
    response = client.chat.completions.create(
        model=OPENAI_MODEL,
        response_format={"type": "json_object"},
        messages=messages,
        temperature=0.2,
    )

    raw = response.choices[0].message.content
    planner_log.debug("[%s] Raw planner response: %s", task_id, raw)

    try:
        plan_data = parse_planner_output(raw)
    except PlanValidationError as exc:
        # Surface the specific validation failures in the log
        planner_log.error("[%s] Planner produced invalid commands: %s", task_id, exc)
        raise
    except json.JSONDecodeError as exc:
        raise ValueError(f"Planner returned invalid JSON: {exc}\nRaw: {raw}") from exc

    raw_steps = plan_data.get("steps", [])
    if not raw_steps:
        raise ValueError("Planner returned an empty steps array.")

    normalised = _normalise_steps(raw_steps)
    branch_name = _make_branch_name(task)

    plan = make_plan_packet(
        task_id=task_id,
        steps=normalised,
        branch_name=branch_name,
        estimated_complexity=plan_data.get("estimated_complexity", "medium"),
        notes=plan_data.get("notes", ""),
    )

    save_plan(plan)
    planner_log.info(
        "[%s] Plan %s with %d steps. Branch: %s",
        task_id,
        "revised" if is_revision else "created",
        len(normalised),
        branch_name,
    )
    log_execution_event(
        task_id,
        "PLANNER COMPLETE",
        f"steps={len(normalised)}, branch={branch_name!r}",
    )
    return plan
