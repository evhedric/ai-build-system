"""
Schema definitions and factory functions for all inter-agent JSON packets.

These are the canonical structures for all data flowing through the pipeline:
  - TaskPacket       → /tasks/{task_id}.json
  - PlanPacket       → /plans/{task_id}.json
  - ExecutionResult  → /artifacts/{task_id}.json
  - ReviewPacket     → /reviews/{task_id}.json
  - SystemState      → /state/system_state.json
"""

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from runner.config import (
    TASKS_DIR, PLANS_DIR, ARTIFACTS_DIR, REVIEWS_DIR, STATE_DIR
)

# ---------------------------------------------------------------------------
# Valid status values (state machine)
# ---------------------------------------------------------------------------
VALID_STATUSES = {
    "pending",
    "planning",
    "ready_for_execution",
    "executing",
    "in_review",
    "approved",
    "revise",
    "complete",
    "failed",
}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_task_id() -> str:
    return f"task-{uuid.uuid4().hex[:8].upper()}"


# ---------------------------------------------------------------------------
# TaskPacket
# ---------------------------------------------------------------------------

def make_task_packet(
    request: str,
    task_id: str | None = None,
    title: str = "",
    goals: list[str] | None = None,
    constraints: list[str] | None = None,
    success_criteria: list[str] | None = None,
    tags: list[str] | None = None,
    priority: str = "medium",
    project: str = "perchiq",
) -> dict[str, Any]:
    return {
        "task_id": task_id or new_task_id(),
        "created_at": _now(),
        "updated_at": _now(),
        "status": "pending",
        "project": project,
        "request": request,
        "title": title or request[:80],
        "goals": goals or [],
        "constraints": constraints or [],
        "success_criteria": success_criteria or [],
        "tags": tags or [],
        "priority": priority,
        "revision_count": 0,
        "max_revisions": 3,
        "branch_name": "",
        "pr_url": "",
    }


# ---------------------------------------------------------------------------
# PlanPacket
# ---------------------------------------------------------------------------

def make_plan_packet(
    task_id: str,
    steps: list[dict[str, Any]],
    branch_name: str,
    estimated_complexity: str = "medium",
    notes: str = "",
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "created_at": _now(),
        "branch_name": branch_name,
        "estimated_complexity": estimated_complexity,
        "notes": notes,
        "steps": steps,
    }


def make_plan_step(
    step_id: int,
    description: str,
    action_type: str,
    target: str = "",
    details: str = "",
) -> dict[str, Any]:
    """
    action_type: create_file | modify_file | run_command | research | document
    target: file path or command string
    """
    return {
        "step_id": step_id,
        "description": description,
        "action_type": action_type,
        "target": target,
        "details": details,
    }


# ---------------------------------------------------------------------------
# ExecutionResult
# ---------------------------------------------------------------------------

def make_execution_result(
    task_id: str,
    branch_name: str,
    steps_completed: list[dict[str, Any]],
    files_created: list[str],
    files_modified: list[str],
    summary: str,
    issues_encountered: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "executed_at": _now(),
        "branch_name": branch_name,
        "steps_completed": steps_completed,
        "files_created": files_created,
        "files_modified": files_modified,
        "summary": summary,
        "issues_encountered": issues_encountered or [],
    }


def make_step_result(
    step_id: int,
    status: str,
    output: str = "",
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "step_id": step_id,
        "status": status,   # completed | failed | skipped
        "output": output,
        "error": error,
    }


# ---------------------------------------------------------------------------
# ReviewPacket
# ---------------------------------------------------------------------------

def make_review_packet(
    task_id: str,
    decision: str,
    score: int,
    reasoning: str,
    criteria_met: list[str],
    criteria_failed: list[str],
    revision_requests: list[str] | None = None,
    next_steps: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "reviewed_at": _now(),
        "decision": decision,       # approved | revise
        "score": score,             # 0-10
        "reasoning": reasoning,
        "criteria_met": criteria_met,
        "criteria_failed": criteria_failed,
        "revision_requests": revision_requests or [],
        "next_steps": next_steps or [],
    }


# ---------------------------------------------------------------------------
# SystemState
# ---------------------------------------------------------------------------

def make_system_state() -> dict[str, Any]:
    return {
        "last_updated": _now(),
        "runner_status": "stopped",
        "active_tasks": {},
        "completed_tasks": [],
        "failed_tasks": [],
        "stats": {
            "total_processed": 0,
            "total_approved": 0,
            "total_failed": 0,
            "total_revisions": 0,
        },
    }


# ---------------------------------------------------------------------------
# File I/O helpers
# ---------------------------------------------------------------------------

def save_task(packet: dict[str, Any]) -> Path:
    packet["updated_at"] = _now()
    path = TASKS_DIR / f"{packet['task_id']}.json"
    path.write_text(json.dumps(packet, indent=2), encoding="utf-8")
    return path


def load_task(task_id: str) -> dict[str, Any]:
    path = TASKS_DIR / f"{task_id}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def save_plan(packet: dict[str, Any]) -> Path:
    path = PLANS_DIR / f"{packet['task_id']}.json"
    path.write_text(json.dumps(packet, indent=2), encoding="utf-8")
    return path


def load_plan(task_id: str) -> dict[str, Any]:
    path = PLANS_DIR / f"{task_id}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def save_execution_result(packet: dict[str, Any]) -> Path:
    path = ARTIFACTS_DIR / f"{packet['task_id']}.json"
    path.write_text(json.dumps(packet, indent=2), encoding="utf-8")
    return path


def load_execution_result(task_id: str) -> dict[str, Any]:
    path = ARTIFACTS_DIR / f"{task_id}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def save_review(packet: dict[str, Any]) -> Path:
    path = REVIEWS_DIR / f"{packet['task_id']}.json"
    path.write_text(json.dumps(packet, indent=2), encoding="utf-8")
    return path


def load_review(task_id: str) -> dict[str, Any]:
    path = REVIEWS_DIR / f"{task_id}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def load_all_tasks() -> list[dict[str, Any]]:
    """Return all task packets sorted by creation time."""
    tasks = []
    for path in TASKS_DIR.glob("task-*.json"):
        try:
            tasks.append(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            pass
    return sorted(tasks, key=lambda t: t.get("created_at", ""))
