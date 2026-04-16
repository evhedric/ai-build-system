"""
Project Registry — manages projects.json at the repo root.

Projects are named sandboxes whose build artifacts live outside the AI
builder repo (under C:/dev/workspaces/<project>). The registry records
which projects exist so submit_task.py can enforce explicit project
selection without silently auto-creating unknown workspaces.

Registry file: <repo_root>/projects.json
Registry format (array of project objects):
  [
    {
      "name":       "<project_name>",
      "path":       "C:/dev/workspaces/<project_name>",
      "created_at": "<ISO-8601 UTC timestamp>"
    },
    ...
  ]
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from runner.config import BASE_DIR, WORKSPACES_ROOT

REGISTRY_PATH = BASE_DIR / "projects.json"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Low-level I/O
# ---------------------------------------------------------------------------

def load_registry() -> list[dict]:
    """Return the full list of registered projects (empty list if file absent)."""
    if not REGISTRY_PATH.exists():
        return []
    return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))


def save_registry(projects: list[dict]) -> None:
    """Persist the project list to projects.json."""
    REGISTRY_PATH.write_text(json.dumps(projects, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------

def find_project(name: str) -> dict | None:
    """Return the registry entry for *name*, or None if not found."""
    for project in load_registry():
        if project.get("name") == name:
            return project
    return None


def project_exists(name: str) -> bool:
    return find_project(name) is not None


# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------

def create_project(name: str) -> dict:
    """
    Register a new project and create its workspace directory.

    Args:
        name: The project name (becomes the workspace folder name).

    Returns:
        The new registry entry dict.

    Raises:
        ValueError: If a project with this name already exists.
    """
    projects = load_registry()

    if any(p.get("name") == name for p in projects):
        raise ValueError(f"Project {name!r} already exists in the registry.")

    workspace = WORKSPACES_ROOT / name
    workspace.mkdir(parents=True, exist_ok=True)

    entry: dict = {
        "name":       name,
        "path":       str(workspace).replace("\\", "/"),
        "created_at": _now(),
    }
    projects.append(entry)
    save_registry(projects)
    return entry
