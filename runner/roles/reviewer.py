"""
Reviewer Role — powered by OpenAI.

Gold v3 — workspace-grounded evaluation.

Responsibility: Evaluate the ExecutionResult against the task's success
criteria by inspecting REAL artifacts from the task workspace, then return
a structured decision: "approved" or "revise".

The reviewer has no ability to directly modify files — it can only judge what
the executor produced and provide actionable feedback for revision.

Key upgrade from Gold v2:
  - scan_workspace() reads actual file contents and git history from the
    task worktree rather than guessing paths relative to BASE_DIR.
  - The artifact manifest is injected into the review prompt so the model
    evaluates real outputs, not just executor-reported descriptions.
"""

import json
import subprocess
from pathlib import Path
from typing import Any

from runner.config import OPENAI_API_KEY, OPENAI_MODEL, PROMPTS_DIR
from runner.logger import reviewer_log, log_execution_event
from runner.schemas import make_review_packet, save_review

try:
    from openai import OpenAI
    _openai_available = True
except ImportError:
    _openai_available = False


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Maximum characters read from a single file before truncation.
_PER_FILE_CAP  = 8_000
# Aggregate character budget across all files in the manifest.
_TOTAL_FILE_CAP = 60_000


# ---------------------------------------------------------------------------
# Workspace scanner
# ---------------------------------------------------------------------------

def scan_workspace(workspace_dir: Path, execution_result: dict[str, Any]) -> dict[str, Any]:
    """
    Inspect the task workspace and return an artifact manifest suitable for
    inclusion in the review prompt.

    The manifest contains:
      workspace_dir  — absolute path to the worktree
      changed_files  — files changed in the last commit (via git diff HEAD~1)
                       or falls back to execution_result file lists
      file_contents  — dict of {rel_path: content} for every touched file,
                       truncated to _PER_FILE_CAP chars each and _TOTAL_FILE_CAP total
      git_log        — last 5 commits on the branch (one-liner format)
      step_outputs   — step_id / status / output / error for every completed step
      scan_errors    — list of non-fatal errors encountered during scanning

    Args:
        workspace_dir:    Path to the git worktree the executor wrote into.
        execution_result: ExecutionResult packet from the executor.

    Returns:
        Artifact manifest dict.
    """
    manifest: dict[str, Any] = {
        "workspace_dir": str(workspace_dir),
        "changed_files": [],
        "file_contents": {},
        "git_log":       "",
        "step_outputs":  [],
        "scan_errors":   [],
    }

    # ── 1. Discover changed files via git diff ────────────────────────────────
    try:
        git_diff = subprocess.run(
            ["git", "diff", "HEAD~1", "--name-only"],
            cwd=str(workspace_dir),
            capture_output=True,
            text=True,
        )
        if git_diff.returncode == 0 and git_diff.stdout.strip():
            manifest["changed_files"] = git_diff.stdout.strip().splitlines()
        else:
            # Worktree may have only one commit (no HEAD~1) — fall back to
            # the executor's self-reported file lists.
            manifest["changed_files"] = list(dict.fromkeys(
                execution_result.get("files_created",  []) +
                execution_result.get("files_modified", [])
            ))
    except Exception as exc:
        manifest["scan_errors"].append(f"git diff HEAD~1: {exc}")
        manifest["changed_files"] = list(dict.fromkeys(
            execution_result.get("files_created",  []) +
            execution_result.get("files_modified", [])
        ))

    # ── 2. Read file contents ─────────────────────────────────────────────────
    # Union of changed_files + executor file lists, deduped, order-preserved.
    all_rel_paths: list[str] = list(dict.fromkeys(
        execution_result.get("files_created",  []) +
        execution_result.get("files_modified", []) +
        manifest["changed_files"]
    ))

    total_chars = 0
    for rel_path in all_rel_paths:
        if total_chars >= _TOTAL_FILE_CAP:
            manifest["file_contents"][rel_path] = (
                "[OMITTED — aggregate file content cap reached]"
            )
            continue

        full_path = workspace_dir / rel_path
        if not full_path.exists():
            manifest["file_contents"][rel_path] = "[file not found in workspace]"
            continue

        if full_path.is_dir():
            manifest["file_contents"][rel_path] = "[path is a directory — skipped]"
            continue

        try:
            raw = full_path.read_text(encoding="utf-8", errors="replace")
            if len(raw) > _PER_FILE_CAP:
                content = (
                    raw[:_PER_FILE_CAP]
                    + f"\n... [truncated — {len(raw):,} total chars, showing first {_PER_FILE_CAP:,}]"
                )
            else:
                content = raw
            manifest["file_contents"][rel_path] = content
            total_chars += len(content)
        except Exception as exc:
            manifest["file_contents"][rel_path] = f"[read error: {exc}]"
            manifest["scan_errors"].append(f"{rel_path}: {exc}")

    # ── 3. Git log — last 5 commits on the branch ────────────────────────────
    try:
        git_log = subprocess.run(
            ["git", "log", "--oneline", "-5"],
            cwd=str(workspace_dir),
            capture_output=True,
            text=True,
        )
        if git_log.returncode == 0:
            manifest["git_log"] = git_log.stdout.strip()
        else:
            manifest["scan_errors"].append(f"git log: exit {git_log.returncode}")
    except Exception as exc:
        manifest["scan_errors"].append(f"git log: {exc}")

    # ── 4. Step outputs from the execution result ─────────────────────────────
    for step in execution_result.get("steps_completed", []):
        entry: dict[str, Any] = {
            "step_id": step.get("step_id"),
            "status":  step.get("status"),
            "output":  (step.get("output") or "")[:2_000],
        }
        if step.get("error"):
            entry["error"] = step["error"]
        manifest["step_outputs"].append(entry)

    reviewer_log.debug(
        "scan_workspace: %d files read, %d changed, %d scan errors | total_chars=%d",
        len(manifest["file_contents"]),
        len(manifest["changed_files"]),
        len(manifest["scan_errors"]),
        total_chars,
    )
    return manifest


# ---------------------------------------------------------------------------
# Prompt loader
# ---------------------------------------------------------------------------

def _load_prompt() -> str:
    path = PROMPTS_DIR / "reviewer.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_reviewer(
    task: dict[str, Any],
    execution_result: dict[str, Any],
    workspace_info: dict[str, Any],
) -> dict[str, Any]:
    """
    Review the execution result against real workspace artifacts and produce
    a ReviewPacket.

    Args:
        task:             Task packet (goals, success_criteria, constraints…).
        execution_result: ExecutionResult packet returned by the executor.
        workspace_info:   Workspace descriptor from provision_workspace():
                            {workspace_dir, branch_name, repo_path, project_id}

    Returns:
        ReviewPacket dict (also saved to /reviews/{task_id}.json).

    Raises:
        RuntimeError: If the OpenAI package is missing or API key not set.
        ValueError:   If workspace_info is missing workspace_dir, or the
                      model returns an invalid decision value.
    """
    task_id = task["task_id"]
    log_execution_event(task_id, "REVIEWER START")
    reviewer_log.info("[%s] Reviewing execution result for: %s", task_id, task.get("title"))

    if not _openai_available:
        raise RuntimeError("openai package not installed. Run: pip install openai")
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not set in your .env file")

    # ── Resolve workspace_dir ─────────────────────────────────────────────────
    ws_dir_str = workspace_info.get("workspace_dir", "")
    if not ws_dir_str:
        raise ValueError(
            "workspace_info is missing 'workspace_dir'. "
            "Pass the descriptor returned by provision_workspace()."
        )
    workspace_dir = Path(ws_dir_str)

    reviewer_log.info(
        "[%s] Scanning workspace: %s (branch=%s)",
        task_id, workspace_dir, workspace_info.get("branch_name", "?"),
    )

    # ── Scan real artifacts ───────────────────────────────────────────────────
    artifact_manifest = scan_workspace(workspace_dir, execution_result)

    if artifact_manifest["scan_errors"]:
        reviewer_log.warning(
            "[%s] scan_workspace reported %d error(s): %s",
            task_id,
            len(artifact_manifest["scan_errors"]),
            "; ".join(artifact_manifest["scan_errors"]),
        )

    reviewer_log.info(
        "[%s] Artifact manifest ready | files_read=%d changed=%d",
        task_id,
        len(artifact_manifest["file_contents"]),
        len(artifact_manifest["changed_files"]),
    )

    # ── Build review context ──────────────────────────────────────────────────
    review_context: dict[str, Any] = {
        "task": {
            "task_id":        task["task_id"],
            "title":          task["title"],
            "request":        task["request"],
            "goals":          task.get("goals", []),
            "success_criteria": task.get("success_criteria", []),
            "constraints":    task.get("constraints", []),
            "revision_count": task.get("revision_count", 0),
            "max_revisions":  task.get("max_revisions", 3),
        },
        "workspace": {
            "project_id":    workspace_info.get("project_id"),
            "branch_name":   workspace_info.get("branch_name"),
            "workspace_dir": ws_dir_str,
        },
        "execution_result":  execution_result,
        "artifact_manifest": artifact_manifest,
    }

    system_prompt = _load_prompt()
    user_message  = (
        "Review the following task execution and return your structured decision.\n\n"
        "The `artifact_manifest` section contains the ACTUAL file contents and git "
        "history from the task workspace — use it as your primary source of truth "
        "when evaluating whether success criteria have been met.\n\n"
        + json.dumps(review_context, indent=2)
    )

    # ── Call OpenAI ───────────────────────────────────────────────────────────
    client   = OpenAI(api_key=OPENAI_API_KEY)
    response = client.chat.completions.create(
        model=OPENAI_MODEL,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_message},
        ],
        temperature=0.2,
    )

    raw = response.choices[0].message.content
    reviewer_log.debug("[%s] Raw reviewer response: %s", task_id, raw)

    # ── Parse + validate ──────────────────────────────────────────────────────
    try:
        review_data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Reviewer returned invalid JSON: {exc}\nRaw: {raw}"
        ) from exc

    decision = review_data.get("decision", "").lower()
    if decision not in ("approved", "revise"):
        raise ValueError(
            f"Reviewer returned invalid decision: {decision!r}. "
            f"Must be 'approved' or 'revise'."
        )

    # ── Persist + return ──────────────────────────────────────────────────────
    review = make_review_packet(
        task_id=task_id,
        decision=decision,
        score=int(review_data.get("score", 5)),
        reasoning=review_data.get("reasoning", ""),
        criteria_met=review_data.get("criteria_met", []),
        criteria_failed=review_data.get("criteria_failed", []),
        revision_requests=review_data.get("revision_requests", []),
        next_steps=review_data.get("next_steps", []),
    )

    save_review(review)
    reviewer_log.info(
        "[%s] Reviewer decision: %s (score=%d/10)",
        task_id, decision.upper(), review["score"],
    )
    log_execution_event(
        task_id, "REVIEWER COMPLETE",
        f"decision={decision!r}, score={review['score']}/10",
    )
    return review
