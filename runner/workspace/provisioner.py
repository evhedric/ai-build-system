"""
runner/workspace/provisioner.py
Gold v3 — Workspace Provisioner

Creates an isolated git worktree for each task so that concurrent tasks
never touch each other's files and every branch is checked out in its
own dedicated directory.

Worktree layout:
  {workspace_root}/
    task-{task_id}/    ← git worktree on branch feat/task-{task_id}

Public API:
  provision_workspace(task, project) -> dict

The returned dict carries all paths the executor needs:
  {
    "workspace_dir": str,    # absolute path to the worktree directory
    "branch_name":   str,    # git branch checked out inside the worktree
    "repo_path":     str,    # absolute path to the source repository
    "project_id":    str,    # project identifier from the registry
  }
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# task_id must start with "task-" followed by alphanumeric chars only.
# This prevents any path-separator or traversal characters from slipping in.
_TASK_ID_RE = re.compile(r"^task-[A-Za-z0-9]+$")

REQUIRED_PROJECT_FIELDS: tuple[str, ...] = (
    "project_id",
    "repo_path",
    "workspace_root",
)


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _validate_task_id(task_id: str) -> None:
    """Reject task IDs that could inject path components."""
    if not _TASK_ID_RE.match(task_id):
        raise ValueError(
            f"Invalid task_id {task_id!r}. "
            f"Must match 'task-<alphanumeric>' with no spaces, slashes, or dots."
        )


def _validate_repo(repo_path: Path) -> None:
    """Fail fast if repo_path is not a real git repository."""
    if not repo_path.exists():
        raise ValueError(f"repo_path does not exist: {repo_path}")
    if not repo_path.is_dir():
        raise ValueError(f"repo_path is not a directory: {repo_path}")
    if not (repo_path / ".git").exists():
        raise ValueError(
            f"repo_path is not a git repository (no .git found): {repo_path}"
        )


def _validate_containment(child: Path, parent: Path) -> None:
    """
    Ensure *child* resolves to a path inside *parent*.
    Prevents ../.. traversal attacks in workspace_dir construction.
    """
    try:
        child.resolve().relative_to(parent.resolve())
    except ValueError:
        raise ValueError(
            f"Path injection detected: {child} is not contained within {parent}. "
            f"workspace_dir must resolve inside workspace_root."
        )


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def _run_git(args: list[str], cwd: Path) -> tuple[int, str, str]:
    """Run a git command; return (returncode, stdout, stderr)."""
    result = subprocess.run(
        ["git"] + args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def _branch_exists(repo_path: Path, branch_name: str) -> bool:
    """Return True if *branch_name* exists locally in the repo."""
    _, out, _ = _run_git(["branch", "--list", branch_name], cwd=repo_path)
    return branch_name in out


def _worktree_exists(repo_path: Path, workspace_dir: Path) -> bool:
    """
    Return True if *workspace_dir* is already registered as a git worktree.
    Uses `git worktree list --porcelain` for reliable path comparison.
    """
    _, out, _ = _run_git(["worktree", "list", "--porcelain"], cwd=repo_path)
    resolved = str(workspace_dir.resolve()).replace("\\", "/")
    # Porcelain output lines look like: worktree /absolute/path
    return any(
        line.startswith("worktree ") and line[len("worktree "):].replace("\\", "/") == resolved
        for line in out.splitlines()
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def provision_workspace(task: dict[str, Any], project: dict[str, Any]) -> dict[str, Any]:
    """
    Create an isolated git worktree for *task* inside the project's workspace.

    Steps:
      1. Validate task_id and project config.
      2. Derive branch_name and workspace_dir.
      3. Guard against path injection.
      4. Create the git branch (if absent) and worktree.
      5. Verify the worktree directory exists.
      6. Return the workspace descriptor dict.

    Args:
        task:    Task packet dict — must contain "task_id".
        project: Project registry entry — must contain "project_id",
                 "repo_path", and "workspace_root".

    Returns:
        {
            "workspace_dir": str,
            "branch_name":   str,
            "repo_path":     str,
            "project_id":    str,
        }

    Raises:
        ValueError:   On invalid inputs or path injection attempt.
        RuntimeError: If the git worktree command fails.
    """
    # ── 1. Extract + validate inputs ─────────────────────────────────────────
    task_id = task.get("task_id", "").strip()
    if not task_id:
        raise ValueError("Task is missing required field 'task_id'.")

    _validate_task_id(task_id)

    missing = [f for f in REQUIRED_PROJECT_FIELDS if not project.get(f)]
    if missing:
        raise ValueError(
            f"Project config is missing required field(s): {', '.join(missing)}"
        )

    project_id   = project["project_id"]
    repo_path    = Path(project["repo_path"]).resolve()
    workspace_root = Path(project["workspace_root"]).resolve()

    _validate_repo(repo_path)

    # ── 2. Derive paths ───────────────────────────────────────────────────────
    branch_name   = f"feat/task-{task_id}"
    workspace_dir = workspace_root / f"task-{task_id}"

    log.info(
        "[%s] Provisioning workspace | project=%s branch=%s dir=%s",
        task_id, project_id, branch_name, workspace_dir,
    )

    # ── 3. Guard path containment ─────────────────────────────────────────────
    _validate_containment(workspace_dir, workspace_root)

    # ── 4. Create worktree ────────────────────────────────────────────────────
    if _worktree_exists(repo_path, workspace_dir):
        log.info(
            "[%s] Worktree already registered at %s — skipping creation.",
            task_id, workspace_dir,
        )

    elif workspace_dir.exists():
        # Directory exists but is NOT a git worktree — refuse to clobber it
        raise RuntimeError(
            f"workspace_dir {workspace_dir} already exists on disk but is not "
            f"a registered git worktree. Remove it manually before reprovisioning."
        )

    else:
        if _branch_exists(repo_path, branch_name):
            log.info("[%s] Branch '%s' exists — attaching worktree.", task_id, branch_name)
            rc, _, err = _run_git(
                ["worktree", "add", str(workspace_dir), branch_name],
                cwd=repo_path,
            )
        else:
            log.info("[%s] Creating new branch '%s' with worktree.", task_id, branch_name)
            rc, _, err = _run_git(
                ["worktree", "add", "-b", branch_name, str(workspace_dir)],
                cwd=repo_path,
            )

        if rc != 0:
            raise RuntimeError(
                f"git worktree add failed (exit {rc}) for task {task_id}:\n{err}"
            )

        log.info(
            "[%s] Worktree created: branch=%s path=%s",
            task_id, branch_name, workspace_dir,
        )

    # ── 5. Verify ─────────────────────────────────────────────────────────────
    if not workspace_dir.exists():
        raise RuntimeError(
            f"git worktree add reported success but workspace_dir does not exist: {workspace_dir}"
        )

    git_dir = workspace_dir / ".git"
    if not git_dir.exists():
        raise RuntimeError(
            f"workspace_dir exists but contains no .git file — worktree setup incomplete: {workspace_dir}"
        )

    log.info("[%s] Workspace ready: %s", task_id, workspace_dir)

    # ── 6. Return descriptor ──────────────────────────────────────────────────
    return {
        "workspace_dir": str(workspace_dir).replace("\\", "/"),
        "branch_name":   branch_name,
        "repo_path":     str(repo_path).replace("\\", "/"),
        "project_id":    project_id,
    }
