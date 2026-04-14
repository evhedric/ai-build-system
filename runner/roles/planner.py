"""
Planner Role — powered by OpenAI.

Responsibility: Convert a structured task packet into an ordered execution plan.
Produces a PlanPacket written to /plans/{task_id}.json.

Each step in the plan has:
  - step_id: sequential integer
  - description: what to do
  - action_type: create_file | modify_file | run_command | research | document
  - target: file path or command
  - details: additional context for the executor
"""

import json
import re

from runner.config import OPENAI_API_KEY, OPENAI_MODEL, PROMPTS_DIR
from runner.logger import planner_log, log_execution_event
from runner.schemas import make_plan_packet, save_plan

try:
    from openai import OpenAI
    _openai_available = True
except ImportError:
    _openai_available = False


def _load_prompt() -> str:
    path = PROMPTS_DIR / "planner.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


def _make_branch_name(task: dict) -> str:
    """Generate a git-safe branch name from the task."""
    title = task.get("title", task["task_id"])
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", title).strip("-").lower()
    slug = slug[:50]
    return f"task/{task['task_id']}/{slug}"


def run_planner(task: dict) -> dict:
    """
    Generate a plan for the given task.
    Returns the PlanPacket dict and saves it to /plans/.
    """
    task_id = task["task_id"]
    log_execution_event(task_id, "PLANNER START")
    planner_log.info("[%s] Running planner for: %s", task_id, task.get("title"))

    if not _openai_available:
        raise RuntimeError("openai package not installed. Run: pip install openai")
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not set in your .env file")

    system_prompt = _load_prompt()
    user_message = json.dumps({
        "task_id": task["task_id"],
        "title": task["title"],
        "request": task["request"],
        "goals": task["goals"],
        "constraints": task["constraints"],
        "success_criteria": task["success_criteria"],
    }, indent=2)

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
    planner_log.debug("[%s] Raw planner response: %s", task_id, raw)

    try:
        plan_data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"Planner returned invalid JSON: {e}\nRaw: {raw}") from e

    if "steps" not in plan_data or not isinstance(plan_data["steps"], list):
        raise ValueError(f"Planner response missing 'steps' list. Got: {list(plan_data.keys())}")

    branch_name = _make_branch_name(task)

    plan = make_plan_packet(
        task_id=task_id,
        steps=plan_data["steps"],
        branch_name=branch_name,
        estimated_complexity=plan_data.get("estimated_complexity", "medium"),
        notes=plan_data.get("notes", ""),
    )

    save_plan(plan)
    planner_log.info(
        "[%s] Plan created with %d steps. Branch: %s",
        task_id, len(plan["steps"]), branch_name
    )
    log_execution_event(
        task_id, "PLANNER COMPLETE",
        f"steps={len(plan['steps'])}, branch={branch_name!r}"
    )
    return plan
