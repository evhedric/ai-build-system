"""
Executor Role — powered by Claude (Anthropic API).

Gold v3 — workspace-bound execution.

Responsibility: Execute plan steps exclusively inside the provisioned workspace.
  - Writes ALL files to workspace_dir (never to the orchestrator repo)
  - Runs ALL commands with cwd=workspace_dir
  - Stages and commits changes to the project repo worktree
  - Produces an ExecutionResult packet written to /artifacts/{task_id}.json
  - Never approves its own work (that is the Reviewer's job)

Branch lifecycle is owned by the workspace provisioner; the executor only
reads workspace_info and operates inside the directory it was given.
"""

import json
import subprocess
from pathlib import Path
from typing import Any

from runner.config import ANTHROPIC_API_KEY, CLAUDE_MODEL, PROMPTS_DIR
from runner.logger import executor_log, log_execution_event
from runner.schemas import make_execution_result, make_step_result, save_execution_result
from runner.executor_guard import guard_run_command
from runner.plan_validator import PlanValidationError

try:
    import anthropic
    _anthropic_available = True
except ImportError:
    _anthropic_available = False


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _load_prompt() -> str:
    path = PROMPTS_DIR / "executor.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


def _run_git(args: list[str], cwd: Path) -> tuple[int, str, str]:
    """
    Run a git command inside *cwd*. Returns (returncode, stdout, stderr).
    cwd is required — no default — to prevent accidental writes to the wrong repo.
    """
    result = subprocess.run(
        ["git"] + args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def _validate_path_in_workspace(path: Path, workspace_dir: Path) -> None:
    """
    Raise ValueError if *path* resolves outside *workspace_dir*.
    Prevents relative-escape attacks (e.g. target='../../etc/passwd').
    """
    try:
        path.resolve().relative_to(workspace_dir.resolve())
    except ValueError:
        raise ValueError(
            f"Path escape detected: {path} is not inside workspace {workspace_dir}. "
            f"All file writes must resolve within the task workspace."
        )


def _generate_file_content(
    client: "anthropic.Anthropic",
    task: dict[str, Any],
    plan: dict[str, Any],
    step: dict[str, Any],
    existing_content: str = "",
) -> str:
    """
    Use Claude to generate the content for a file creation or modification step.
    Returns the raw file content as a string.
    """
    system_prompt = _load_prompt()

    context = {
        "task_title":       task.get("title"),
        "task_request":     task.get("request"),
        "goals":            task.get("goals", []),
        "constraints":      task.get("constraints", []),
        "success_criteria": task.get("success_criteria", []),
        "all_steps":        plan.get("steps", []),
        "current_step":     step,
    }
    if existing_content:
        context["existing_file_content"] = existing_content

    user_message = (
        f"Execute this plan step and return ONLY the file content (no markdown fences, "
        f"no explanation — just the raw file content):\n\n"
        f"Step: {step['description']}\n"
        f"Target file: {step.get('target', '')}\n"
        f"Details: {step.get('details', '')}\n\n"
        f"Full task context:\n{json.dumps(context, indent=2)}"
    )

    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=4096,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text


def _execute_step(
    client: "anthropic.Anthropic",
    task: dict[str, Any],
    plan: dict[str, Any],
    step: dict[str, Any],
    workspace_dir: Path,
) -> dict[str, Any]:
    """
    Execute a single plan step entirely within *workspace_dir*.
    Returns a step result dict.
    """
    step_id     = step["step_id"]
    action      = step.get("action_type", "")
    target      = step.get("target", "")
    description = step.get("description", "")

    executor_log.info(
        "[%s] Step %d: [%s] %s -> %s  (workspace: %s)",
        task["task_id"], step_id, action, description[:60], target, workspace_dir,
    )

    try:
        if action in ("create_file", "modify_file", "document"):
            # All writes are scoped to workspace_dir
            target_path = workspace_dir / target if target else None

            # Guard: reject any target that escapes the workspace
            if target_path:
                _validate_path_in_workspace(target_path, workspace_dir)

            existing = ""
            if action == "modify_file" and target_path and target_path.exists():
                existing = target_path.read_text(encoding="utf-8")

            content = _generate_file_content(client, task, plan, step, existing)

            if target_path:
                target_path.parent.mkdir(parents=True, exist_ok=True)
                target_path.write_text(content, encoding="utf-8")
                output = f"Written {len(content)} chars to {target_path}"
                executor_log.info(
                    "[%s] Step %d: wrote %d chars -> %s",
                    task["task_id"], step_id, len(content), target_path,
                )
                # Stage inside the project repo worktree
                _run_git(["add", str(target_path)], cwd=workspace_dir)
            else:
                output = f"Generated content ({len(content)} chars) — no target path specified"

            return make_step_result(step_id, "completed", output=output)

        elif action == "run_command":
            # Command string stored in 'target' after normalisation,
            # or in 'details' for legacy steps that skipped normalisation.
            cmd = target or step.get("details", "")
            if not cmd:
                return make_step_result(step_id, "skipped", output="No command specified")

            # Last-line-of-defence guard — re-validate immediately before execution
            try:
                guard_run_command({"command": cmd})
            except PlanValidationError as exc:
                executor_log.error(
                    "[%s] Step %d: BLOCKED by executor guard — %s",
                    task["task_id"], step_id, exc,
                )
                return make_step_result(
                    step_id, "failed",
                    error=f"Executor guard blocked command: {exc}",
                )

            executor_log.info(
                "[%s] Step %d: running in %s: %s",
                task["task_id"], step_id, workspace_dir, cmd,
            )
            # Stream stdout+stderr to the terminal in real time.
            # stderr=STDOUT merges both streams into one pipe, preventing the
            # classic two-pipe deadlock.
            with subprocess.Popen(
                cmd, shell=True, cwd=str(workspace_dir),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
            ) as proc:
                output_lines: list[str] = []
                for line in proc.stdout:
                    print(line, end="", flush=True)
                    output_lines.append(line)
                try:
                    proc.wait(timeout=120)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    raise

            output = "".join(output_lines)
            if proc.returncode != 0:
                return make_step_result(
                    step_id, "failed",
                    output=output[:500],
                    error=f"Exit code {proc.returncode}",
                )
            return make_step_result(step_id, "completed", output=output[:500])

        elif action == "research":
            notes  = _generate_file_content(client, task, plan, step)
            output = f"Research notes generated ({len(notes)} chars)"
            return make_step_result(step_id, "completed", output=output)

        else:
            executor_log.warning(
                "[%s] Step %d: Unknown action_type %r — skipping",
                task["task_id"], step_id, action,
            )
            return make_step_result(step_id, "skipped", output=f"Unknown action_type: {action}")

    except Exception as exc:
        executor_log.error("[%s] Step %d failed: %s", task["task_id"], step_id, exc)
        return make_step_result(step_id, "failed", error=str(exc))


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def execute_task(
    task: dict[str, Any],
    plan: dict[str, Any],
    workspace_info: dict[str, Any],
) -> dict[str, Any]:
    """
    Execute the full plan for a task, strictly inside workspace_info["workspace_dir"].

    Args:
        task:           Task packet (must contain task_id).
        plan:           Plan packet (contains steps list).
        workspace_info: Descriptor returned by provision_workspace():
                          {workspace_dir, branch_name, repo_path, project_id}

    Returns:
        ExecutionResult packet (also saved to /artifacts/{task_id}.json).

    Raises:
        RuntimeError: If the Anthropic package is missing or API key not set.
        ValueError:   If workspace_info is incomplete.
    """
    task_id = task["task_id"]

    # ── Validate workspace_info ───────────────────────────────────────────────
    required = ("workspace_dir", "branch_name", "repo_path", "project_id")
    missing  = [k for k in required if not workspace_info.get(k)]
    if missing:
        raise ValueError(
            f"workspace_info is missing required key(s): {', '.join(missing)}"
        )

    workspace_dir = Path(workspace_info["workspace_dir"])
    branch_name   = workspace_info["branch_name"]
    project_id    = workspace_info["project_id"]

    if not workspace_dir.exists():
        raise ValueError(
            f"workspace_dir does not exist: {workspace_dir}. "
            f"Run provision_workspace() before execute_task()."
        )

    log_execution_event(
        task_id, "EXECUTOR START",
        f"workspace={workspace_dir}  branch={branch_name!r}  project={project_id}",
    )
    executor_log.info(
        "[%s] Executing task | project=%s branch=%s workspace=%s",
        task_id, project_id, branch_name, workspace_dir,
    )

    if not _anthropic_available:
        raise RuntimeError("anthropic package not installed. Run: pip install anthropic")
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not set in your .env file")

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # ── Execute steps ─────────────────────────────────────────────────────────
    steps_results  = []
    files_created  = []
    files_modified = []
    issues         = []

    for step in plan.get("steps", []):
        result = _execute_step(client, task, plan, step, workspace_dir)
        steps_results.append(result)

        target = step.get("target", "")
        if result["status"] == "completed" and target:
            if step.get("action_type") == "create_file":
                files_created.append(target)
            elif step.get("action_type") in ("modify_file", "document"):
                files_modified.append(target)
        elif result["status"] == "failed":
            issues.append(f"Step {step['step_id']}: {result.get('error', 'unknown error')}")

    # ── Commit staged changes to project repo worktree ────────────────────────
    completed_count = sum(1 for r in steps_results if r["status"] == "completed")
    commit_msg = (
        f"[{task_id}] {task.get('title', 'Task execution')}\n\n"
        f"Executed {completed_count}/{len(steps_results)} steps successfully.\n"
        f"Files created: {', '.join(files_created) or 'none'}\n"
        f"Files modified: {', '.join(files_modified) or 'none'}"
    )

    rc, _, _ = _run_git(["diff", "--cached", "--quiet"], cwd=workspace_dir)
    if rc != 0:  # staged changes present
        _run_git(["commit", "-m", commit_msg], cwd=workspace_dir)
        executor_log.info(
            "[%s] Committed to project repo on branch %s in %s",
            task_id, branch_name, workspace_dir,
        )
    else:
        executor_log.info("[%s] No staged changes to commit", task_id)

    # ── Build and persist result packet ──────────────────────────────────────
    summary = (
        f"Executed {completed_count} of {len(steps_results)} steps. "
        f"Created: {len(files_created)} files. Modified: {len(files_modified)} files."
        + (f" Issues: {len(issues)}." if issues else "")
    )

    result_packet = make_execution_result(
        task_id=task_id,
        branch_name=branch_name,
        steps_completed=steps_results,
        files_created=files_created,
        files_modified=files_modified,
        summary=summary,
        issues_encountered=issues,
    )

    save_execution_result(result_packet)
    executor_log.info("[%s] Executor complete: %s", task_id, summary)
    log_execution_event(task_id, "EXECUTOR COMPLETE", summary)
    return result_packet


# Backward-compatible alias — pipeline.py can be updated to call execute_task
# directly once the full Gold v3 pipeline integration lands.
run_executor = execute_task
