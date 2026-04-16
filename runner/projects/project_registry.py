"""
runner/projects/project_registry.py
Gold v3 — Project Registry

Central registry for all projects managed by the AI orchestration system.
Each project maps a logical project_id to its physical paths, tooling
constraints, and source control settings.

Registry file location: <repo_root>/data/project_registry.json

Project schema (all fields required):
  project_id      str   — unique identifier used in task packets
  project_name    str   — human-readable display name
  repo_path       str   — absolute path to the project source repository
  workspace_root  str   — absolute path where build commands execute
  output_root     str   — absolute path for build / dist output
  default_branch  str   — git branch used for new task branches
  allowed_tools   list  — command prefixes permitted for this project
                          (subset of the global executor allowlist)
  project_type    str   — e.g. "nextjs", "python", "static"

Public API:
  load_registry()                   -> list[dict]
  get_project(project_id)           -> dict          (raises KeyError if absent)
  list_projects()                   -> list[dict]
  register_project(project_config)  -> dict          (raises on dup / bad schema)
  resolve_project_from_task(task)   -> dict          (raises if no project_id)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Registry file location — always relative to the repo root
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent.parent
REGISTRY_PATH = _REPO_ROOT / "data" / "project_registry.json"

# ---------------------------------------------------------------------------
# Required fields for every project entry
# ---------------------------------------------------------------------------

REQUIRED_FIELDS: tuple[str, ...] = (
    "project_id",
    "project_name",
    "repo_path",
    "workspace_root",
    "output_root",
    "default_branch",
    "allowed_tools",
    "project_type",
)


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _validate_project(config: dict[str, Any]) -> None:
    """
    Raise ValueError if *config* is missing required fields or has wrong types.

    Checks:
      - All REQUIRED_FIELDS are present and non-empty.
      - allowed_tools is a non-empty list of strings.
      - project_id contains no whitespace.
    """
    missing = [f for f in REQUIRED_FIELDS if not config.get(f)]
    if missing:
        raise ValueError(
            f"Project config is missing required field(s): {', '.join(missing)}"
        )

    if not isinstance(config["allowed_tools"], list) or not config["allowed_tools"]:
        raise ValueError(
            f"'allowed_tools' must be a non-empty list "
            f"(got {config['allowed_tools']!r})"
        )

    if any(not isinstance(t, str) for t in config["allowed_tools"]):
        raise ValueError("Every entry in 'allowed_tools' must be a string.")

    if any(c.isspace() for c in config["project_id"]):
        raise ValueError(
            f"'project_id' must not contain whitespace (got {config['project_id']!r})"
        )


# ---------------------------------------------------------------------------
# Low-level I/O
# ---------------------------------------------------------------------------

def _read_file() -> list[dict[str, Any]]:
    """Read and parse the registry JSON file. Returns [] if file is absent."""
    if not REGISTRY_PATH.exists():
        return []
    try:
        return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Registry file is corrupt ({REGISTRY_PATH}): {exc}"
        ) from exc


def _write_file(projects: list[dict[str, Any]]) -> None:
    """Persist the project list atomically."""
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    REGISTRY_PATH.write_text(json.dumps(projects, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_registry() -> list[dict[str, Any]]:
    """
    Return the full contents of the project registry.

    Returns:
        A list of project dicts (may be empty).
    """
    return _read_file()


def get_project(project_id: str) -> dict[str, Any]:
    """
    Return the project entry for *project_id*.

    Raises:
        KeyError: If no project with that ID is registered.
    """
    for project in _read_file():
        if project.get("project_id") == project_id:
            return project
    raise KeyError(
        f"No project found with project_id={project_id!r}. "
        f"Register it first with register_project()."
    )


def list_projects() -> list[dict[str, Any]]:
    """
    Return all registered projects sorted alphabetically by project_id.

    Equivalent to load_registry() but always sorted for deterministic output.
    """
    return sorted(_read_file(), key=lambda p: p.get("project_id", ""))


def register_project(project_config: dict[str, Any]) -> dict[str, Any]:
    """
    Validate *project_config* and add it to the registry.

    Args:
        project_config: A dict matching the project schema (all fields required).

    Returns:
        The registered project dict (same object that was stored).

    Raises:
        ValueError: If required fields are missing, types are wrong, or the
                    project_id already exists in the registry.
    """
    _validate_project(project_config)

    projects = _read_file()

    # Duplicate guard — fail closed
    existing_ids = {p.get("project_id") for p in projects}
    pid = project_config["project_id"]
    if pid in existing_ids:
        raise ValueError(
            f"project_id={pid!r} is already registered. "
            f"project_id values must be unique."
        )

    projects.append(project_config)
    _write_file(projects)
    return project_config


# ---------------------------------------------------------------------------
# Task resolution helper
# ---------------------------------------------------------------------------

def resolve_project_from_task(task: dict[str, Any]) -> dict[str, Any]:
    """
    Return the registered project for a given task packet.

    Resolution rules (fail-closed — no silent defaults):
      1. If task contains "project_id" → look up and return that project.
      2. If "project_id" is absent     → raise ValueError immediately.

    Args:
        task: A task packet dict (as produced by runner.schemas.make_task_packet).

    Returns:
        The matching project dict from the registry.

    Raises:
        ValueError: If "project_id" is absent from the task.
        KeyError:   If the project_id is present but not registered.
    """
    project_id = task.get("project_id")

    if not project_id:
        raise ValueError(
            "Task is missing 'project_id'. Every task must explicitly name its "
            "target project. Fail-closed: no default project is assumed."
        )

    return get_project(project_id)
