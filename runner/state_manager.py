"""
State manager for the AI orchestration system.

Maintains the system_state.json file and enforces valid state transitions.
All state changes are logged to execution_history.log.

State machine:
  pending → planning → ready_for_execution → executing → in_review
  in_review → approved → complete
  in_review → revise → executing (loop back, up to max_revisions)
  Any state → failed
"""

import json
from pathlib import Path

from runner.config import STATE_DIR
from runner.logger import log_execution_event
from runner.schemas import (
    make_system_state, save_task, load_task, load_all_tasks, _now
)

STATE_FILE = STATE_DIR / "system_state.json"

# Valid forward transitions
TRANSITIONS: dict[str, list[str]] = {
    "pending":              ["planning", "failed"],
    "planning":             ["ready_for_execution", "failed"],
    "ready_for_execution":  ["executing", "failed"],
    "executing":            ["in_review", "failed"],
    "in_review":            ["approved", "revise", "failed"],
    "approved":             ["complete"],
    "revise":               ["executing", "failed"],
    "complete":             [],
    "failed":               [],
}


# ---------------------------------------------------------------------------
# System State file helpers
# ---------------------------------------------------------------------------

def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return make_system_state()


def _save_state(state: dict) -> None:
    state["last_updated"] = _now()
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def initialize_state() -> None:
    """Create system_state.json if it doesn't exist."""
    if not STATE_FILE.exists():
        state = make_system_state()
        state["runner_status"] = "running"
        _save_state(state)
    else:
        state = _load_state()
        state["runner_status"] = "running"
        _save_state(state)


def shutdown_state() -> None:
    state = _load_state()
    state["runner_status"] = "stopped"
    _save_state(state)


# ---------------------------------------------------------------------------
# Task state transitions
# ---------------------------------------------------------------------------

def transition_task(task_id: str, new_status: str) -> dict:
    """
    Transition a task to a new status.
    Validates the transition, updates the task file, and syncs system state.
    Returns the updated task packet.
    """
    task = load_task(task_id)
    current = task["status"]

    allowed = TRANSITIONS.get(current, [])
    if new_status not in allowed:
        raise ValueError(
            f"Invalid transition for {task_id}: {current!r} -> {new_status!r}. "
            f"Allowed: {allowed}"
        )

    task["status"] = new_status
    save_task(task)
    _sync_system_state(task_id, new_status)
    log_execution_event(task_id, f"STATUS CHANGE: {current} -> {new_status}")
    return task


def transition_task_to_failed(task_id: str, reason: str) -> dict:
    """Force a task to failed status regardless of current state."""
    task = load_task(task_id)
    task["status"] = "failed"
    task["failure_reason"] = reason
    save_task(task)
    _sync_system_state(task_id, "failed")
    log_execution_event(task_id, "STATUS CHANGE: -> failed", reason)
    return task


def increment_revision(task_id: str) -> dict:
    """Increment the revision counter on a task."""
    task = load_task(task_id)
    task["revision_count"] = task.get("revision_count", 0) + 1
    save_task(task)
    log_execution_event(task_id, f"REVISION #{task['revision_count']}")
    return task


def _sync_system_state(task_id: str, status: str) -> None:
    state = _load_state()

    # Keep active_tasks current
    if status in ("complete", "failed"):
        state["active_tasks"].pop(task_id, None)
        if status == "complete":
            if task_id not in state["completed_tasks"]:
                state["completed_tasks"].append(task_id)
            state["stats"]["total_approved"] += 1
        else:
            if task_id not in state["failed_tasks"]:
                state["failed_tasks"].append(task_id)
            state["stats"]["total_failed"] += 1
        state["stats"]["total_processed"] += 1
    else:
        state["active_tasks"][task_id] = status
        if status == "executing" and state["active_tasks"].get(task_id) == "revise":
            state["stats"]["total_revisions"] += 1

    _save_state(state)


def register_task(task_id: str) -> None:
    """Register a newly discovered task in system state."""
    state = _load_state()
    if task_id not in state["active_tasks"]:
        state["active_tasks"][task_id] = "pending"
        _save_state(state)


def get_system_state() -> dict:
    return _load_state()


def get_pending_tasks() -> list[dict]:
    """Return all task packets that are in 'pending' status."""
    return [t for t in load_all_tasks() if t.get("status") == "pending"]
