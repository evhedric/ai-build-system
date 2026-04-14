"""
Architect Role — powered by OpenAI.

Responsibility: Take a raw user request and produce a structured task packet
with well-defined goals, constraints, and success criteria.

Output enriches the existing task packet in-place (title, goals, constraints,
success_criteria, tags, priority).
"""

import json

from runner.config import OPENAI_API_KEY, OPENAI_MODEL, PROMPTS_DIR
from runner.logger import architect_log, log_execution_event
from runner.schemas import save_task

try:
    from openai import OpenAI
    _openai_available = True
except ImportError:
    _openai_available = False


def _load_prompt() -> str:
    path = PROMPTS_DIR / "architect.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


def run_architect(task: dict) -> dict:
    """
    Enrich a task packet with structured goals, constraints, and success criteria.
    Returns the updated task packet.
    """
    task_id = task["task_id"]
    log_execution_event(task_id, "ARCHITECT START")
    architect_log.info("[%s] Running architect for request: %s", task_id, task["request"][:100])

    if not _openai_available:
        raise RuntimeError("openai package not installed. Run: pip install openai")
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not set in your .env file")

    system_prompt = _load_prompt()
    user_message = f"User Request:\n{task['request']}"

    client = OpenAI(api_key=OPENAI_API_KEY)
    response = client.chat.completions.create(
        model=OPENAI_MODEL,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        temperature=0.3,
    )

    raw = response.choices[0].message.content
    architect_log.debug("[%s] Raw architect response: %s", task_id, raw)

    try:
        enrichment = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"Architect returned invalid JSON: {e}\nRaw: {raw}") from e

    # Validate required fields
    for field in ("title", "goals", "constraints", "success_criteria"):
        if field not in enrichment:
            raise ValueError(f"Architect response missing required field: {field!r}")

    # Apply enrichment to task packet
    task["title"] = enrichment.get("title", task["title"])
    task["goals"] = enrichment.get("goals", [])
    task["constraints"] = enrichment.get("constraints", [])
    task["success_criteria"] = enrichment.get("success_criteria", [])
    task["tags"] = enrichment.get("tags", [])
    task["priority"] = enrichment.get("priority", task.get("priority", "medium"))

    save_task(task)
    architect_log.info("[%s] Architect complete. Title: %s", task_id, task["title"])
    log_execution_event(task_id, "ARCHITECT COMPLETE", f"title={task['title']!r}")
    return task
