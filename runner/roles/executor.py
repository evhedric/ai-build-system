"""
Executor Role — powered by Claude (Anthropic API).

Responsibility: Execute the plan steps against the repository.
  - Creates a new git branch per task
  - Executes each step (file creation, file modification, commands)
  - Uses the Claude API to generate file content when needed
  - Produces an ExecutionResult packet written to /artifacts/{task_id}.json
  - Never approves its own work (that is the Reviewer's job)
"""

import json
import subprocess
from pathlib import Path

from runner.config import ANTHROPIC_API_KEY, CLAUDE_MODEL, PROMPTS_DIR, BASE_DIR
from runner.logger import executor_log, log_execution_event
from runner.schemas import make_execution_result, make_step_result, save_execution_result

try:
    import anthropic
    _anthropic_available = True
except ImportError:
    _anthropic_available = False


def _load_prompt() -> str:
    path = PROMPTS_DIR / "executor.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


def _run_git(args: list[str], cwd: Path = BASE_DIR) -> tuple[int, str, str]:
    """Run a git command. Returns (returncode, stdout, stderr)."""
    result = subprocess.run(
        ["git"] + args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def _ensure_branch(branch_name: str, task_id: str) -> None:
    """Create and checkout a new branch for this task."""
    # Check if branch already exists
    rc, out, _ = _run_git(["branch", "--list", branch_name])
    if branch_name in out:
        executor_log.info("[%s] Branch already exists, checking out: %s", task_id, branch_name)
        rc, _, err = _run_git(["checkout", branch_name])
    else:
        executor_log.info("[%s] Creating branch: %s", task_id, branch_name)
        # Make sure we're on main/Claude-Workflow first
        _run_git(["checkout", "main"])
        rc, _, err = _run_git(["checkout", "-b", branch_name])

    if rc != 0:
        raise RuntimeError(f"Failed to create/checkout branch {branch_name!r}: {err}")


def _generate_file_content(
    client: "anthropic.Anthropic",
    task: dict,
    plan: dict,
    step: dict,
    existing_content: str = "",
) -> str:
    """
    Use Claude to generate the content for a file creation or modification step.
    Returns the raw file content as a string.
    """
    system_prompt = _load_prompt()

    context = {
        "task_title": task.get("title"),
        "task_request": task.get("request"),
        "goals": task.get("goals", []),
        "constraints": task.get("constraints", []),
        "success_criteria": task.get("success_criteria", []),
        "all_steps": plan.get("steps", []),
        "current_step": step,
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
    task: dict,
    plan: dict,
    step: dict,
) -> dict:
    """Execute a single plan step. Returns a step result dict."""
    step_id = step["step_id"]
    action = step.get("action_type", "")
    target = step.get("target", "")
    description = step.get("description", "")

    executor_log.info(
        "[%s] Step %d: [%s] %s → %s",
        task["task_id"], step_id, action, description[:60], target
    )

    try:
        if action in ("create_file", "modify_file", "document"):
            # Generate content with Claude and write the file
            target_path = BASE_DIR / target if target else None

            existing = ""
            if action == "modify_file" and target_path and target_path.exists():
                existing = target_path.read_text(encoding="utf-8")

            content = _generate_file_content(client, task, plan, step, existing)

            if target_path:
                target_path.parent.mkdir(parents=True, exist_ok=True)
                target_path.write_text(content, encoding="utf-8")
                output = f"Written {len(content)} chars to {target}"
                executor_log.info("[%s] Step %d: %s", task["task_id"], step_id, output)

                # Stage the file
                _run_git(["add", target])
            else:
                output = f"Generated content ({len(content)} chars) — no target path specified"

            return make_step_result(step_id, "completed", output=output)

        elif action == "run_command":
            # Run shell command
            cmd = target or step.get("details", "")
            if not cmd:
                return make_step_result(step_id, "skipped", output="No command specified")

            executor_log.info("[%s] Step %d: Running command: %s", task["task_id"], step_id, cmd)
            result = subprocess.run(
                cmd, shell=True, cwd=str(BASE_DIR),
                capture_output=True, text=True, timeout=120,
            )
            output = result.stdout + (f"\nSTDERR: {result.stderr}" if result.stderr else "")
            if result.returncode != 0:
                return make_step_result(step_id, "failed", output=output, error=f"Exit code {result.returncode}")
            return make_step_result(step_id, "completed", output=output[:500])

        elif action == "research":
            # Research steps: use Claude to produce notes (written to artifacts)
            notes = _generate_file_content(client, task, plan, step)
            output = f"Research notes generated ({len(notes)} chars)"
            return make_step_result(step_id, "completed", output=output)

        else:
            executor_log.warning("[%s] Step %d: Unknown action_type %r — skipping", task["task_id"], step_id, action)
            return make_step_result(step_id, "skipped", output=f"Unknown action_type: {action}")

    except Exception as e:
        executor_log.error("[%s] Step %d failed: %s", task["task_id"], step_id, e)
        return make_step_result(step_id, "failed", error=str(e))


def run_executor(task: dict, plan: dict) -> dict:
    """
    Execute the full plan for a task.
    Returns the ExecutionResult packet and saves it to /artifacts/.
    """
    task_id = task["task_id"]
    branch_name = plan["branch_name"]
    log_execution_event(task_id, "EXECUTOR START", f"branch={branch_name!r}")

    if not _anthropic_available:
        raise RuntimeError("anthropic package not installed. Run: pip install anthropic")
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not set in your .env file")

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # Create/checkout the task branch
    _ensure_branch(branch_name, task_id)

    steps_results = []
    files_created = []
    files_modified = []
    issues = []

    for step in plan.get("steps", []):
        result = _execute_step(client, task, plan, step)
        steps_results.append(result)

        target = step.get("target", "")
        if result["status"] == "completed" and target:
            if step.get("action_type") == "create_file":
                files_created.append(target)
            elif step.get("action_type") in ("modify_file", "document"):
                files_modified.append(target)
        elif result["status"] == "failed":
            issues.append(f"Step {step['step_id']}: {result.get('error', 'unknown error')}")

    # Commit all staged changes
    completed_count = sum(1 for r in steps_results if r["status"] == "completed")
    commit_msg = (
        f"[{task_id}] {task.get('title', 'Task execution')}\n\n"
        f"Executed {completed_count}/{len(steps_results)} steps successfully.\n"
        f"Files created: {', '.join(files_created) or 'none'}\n"
        f"Files modified: {', '.join(files_modified) or 'none'}"
    )

    rc, _, err = _run_git(["diff", "--cached", "--quiet"])
    if rc != 0:  # There are staged changes
        _run_git(["commit", "-m", commit_msg])
        executor_log.info("[%s] Committed changes on branch %s", task_id, branch_name)
    else:
        executor_log.info("[%s] No changes to commit", task_id)

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
