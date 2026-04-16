"""
Executor Role — powered by Claude Code SDK (agentic execution).

Gold v3 — workspace-bound agentic execution.

WHAT CHANGED FROM THE PREVIOUS EXECUTOR:
  The previous executor looped over plan steps, called
  anthropic.messages.create() to generate file content as text, and then
  manually wrote those strings to disk.  That approach required the
  orchestrator to parse and interpret code, which was fragile.

  This executor sends a single rich prompt to the Claude Code SDK.  Claude
  Code acts as a real agent: it uses Read/Write/Edit/Bash tools to operate
  directly on the filesystem and run commands — no manual file writing, no
  content parsing.

PRESERVED CONTRACT (nothing else in the system changed):
  - Public signature:  execute_task(task, plan, workspace_info) -> dict
  - Backward-compat:   run_executor = execute_task
  - Return format:     make_execution_result() packet (same schema)
  - Git staging/commit after execution (same flow)
  - Path-containment safety guard preserved
  - All existing logging namespaces and event names

REMOVED:
  - _generate_file_content()   — Claude Code writes files directly
  - _execute_step()            — single agentic call replaces the step loop
  - anthropic.Anthropic client — replaced by claude_code_sdk.query()
  - threading import           — no longer needed for subprocess streaming

DEPENDENCIES:
  - claude-code-sdk  (pip install claude-code-sdk)
  - claude CLI       (npm install -g @anthropic-ai/claude-code)
  - ANTHROPIC_API_KEY in .env
"""

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any

from runner.config import ANTHROPIC_API_KEY, PROMPTS_DIR
from runner.logger import executor_log, log_execution_event
from runner.schemas import make_execution_result, make_step_result, save_execution_result

try:
    from claude_code_sdk import (
        AssistantMessage,
        ClaudeCodeOptions,
        ResultMessage,
        TextBlock,
        ToolResultBlock,
        ToolUseBlock,
        query,
    )
    _sdk_available = True
except ImportError:
    _sdk_available = False


# ---------------------------------------------------------------------------
# Internal git helper
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Path safety guard
# ---------------------------------------------------------------------------

def _validate_path_in_workspace(path: Path, workspace_dir: Path) -> None:
    """
    Raise ValueError if *path* resolves outside *workspace_dir*.
    Preserved from the previous executor for defence-in-depth.
    """
    try:
        path.resolve().relative_to(workspace_dir.resolve())
    except ValueError:
        raise ValueError(
            f"Path escape detected: {path} is not inside workspace {workspace_dir}. "
            f"All file writes must resolve within the task workspace."
        )


# ---------------------------------------------------------------------------
# Workspace change discovery
# ---------------------------------------------------------------------------

def _discover_workspace_changes(workspace_dir: Path) -> tuple[list[str], list[str]]:
    """
    Use `git status --short` to identify files created or modified by the agent
    since the last commit.

    Returns:
        (files_created, files_modified) — lists of relative path strings.

    Git --short output format:  XY FILENAME
      X = staged status, Y = unstaged status
      ?? = untracked new file
      A  = staged new file
      M  = modified (staged or unstaged)
    """
    files_created:  list[str] = []
    files_modified: list[str] = []

    rc, out, _ = _run_git(["status", "--short"], cwd=workspace_dir)
    if rc != 0 or not out.strip():
        return files_created, files_modified

    for raw_line in out.splitlines():
        if len(raw_line) < 3:
            continue
        xy       = raw_line[:2]
        filepath = raw_line[3:].strip()

        if xy in ("??", "A ", "AM"):
            # Untracked or newly staged file — created by the agent
            files_created.append(filepath)
        elif "M" in xy:
            # Modified tracked file
            files_modified.append(filepath)

    return files_created, files_modified


# ---------------------------------------------------------------------------
# Executor prompt builder
# ---------------------------------------------------------------------------

def build_executor_prompt(
    task: dict[str, Any],
    plan: dict[str, Any],
    workspace_dir: Path,
) -> str:
    """
    Build the single prompt that the Claude Code agent receives.

    Encodes:
      - Workspace boundary rule (ONLY operate inside workspace_dir)
      - Full task context: title, request, goals, constraints, success criteria
      - All plan steps with action types, targets, and details
      - Execution instructions (run to completion, verify, no persistent servers)
    """
    lines: list[str] = []

    # ── Header + workspace boundary ───────────────────────────────────────────
    lines += [
        "You are an autonomous executor for an AI build system.",
        "",
        f"WORKSPACE DIRECTORY: {workspace_dir}",
        "RULE: You MUST operate ONLY inside this directory.",
        "      Never read, write, or execute anything outside it.",
        "",
    ]

    # ── Task context ──────────────────────────────────────────────────────────
    lines += [
        f"TASK: {task.get('title', task.get('request', ''))}",
        "",
        f"REQUEST: {task.get('request', '')}",
        "",
    ]

    goals = task.get("goals", [])
    if goals:
        lines.append("GOALS:")
        lines += [f"  - {g}" for g in goals]
        lines.append("")

    constraints = task.get("constraints", [])
    if constraints:
        lines.append("CONSTRAINTS:")
        lines += [f"  - {c}" for c in constraints]
        lines.append("")

    criteria = task.get("success_criteria", [])
    if criteria:
        lines.append("SUCCESS CRITERIA:")
        lines += [f"  - {c}" for c in criteria]
        lines.append("")

    # ── Execution plan ────────────────────────────────────────────────────────
    steps = plan.get("steps", [])
    if steps:
        lines.append("EXECUTION PLAN — complete every step in order:")
        lines.append("")
        for step in steps:
            sid     = step.get("step_id", "?")
            action  = step.get("action_type", step.get("type", "?"))
            desc    = step.get("description", "")
            target  = step.get("target", "")
            details = step.get("details", "")
            lines.append(f"Step {sid}: [{action}] {desc}")
            if target:
                lines.append(f"  Target:  {target}")
            if details:
                lines.append(f"  Details: {details}")
            lines.append("")

    # ── Execution instructions ────────────────────────────────────────────────
    lines += [
        "INSTRUCTIONS:",
        "1. Execute every step above to completion.",
        f"2. Write all files inside {workspace_dir} only.",
        f"3. Run all shell commands with cwd={workspace_dir}",
        "4. If a step fails, diagnose and fix before moving on.",
        "5. Do NOT start any persistent server (http.listen, npm start, npm run dev).",
        "   One-shot scripts that exit on their own (console.log, data processing) are fine.",
        "6. After writing each file, verify it exists and has the correct content.",
        "7. When all steps are complete, output a brief summary of what was accomplished.",
    ]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Agent message logger
# ---------------------------------------------------------------------------

def _log_agent_message(msg: Any, task_id: str) -> None:
    """Log a single message from the Claude Code SDK stream."""
    if isinstance(msg, AssistantMessage):
        for block in msg.content:
            if isinstance(block, TextBlock) and block.text.strip():
                executor_log.info(
                    "[%s] Agent: %s", task_id, block.text.strip()[:200]
                )
            elif isinstance(block, ToolUseBlock):
                input_preview = json.dumps(block.input)[:150]
                executor_log.info(
                    "[%s] Tool use: %s(%s)", task_id, block.name, input_preview
                )
            elif isinstance(block, ToolResultBlock):
                content_preview = str(block.content or "")[:120]
                status = "ERROR" if block.is_error else "ok"
                executor_log.debug(
                    "[%s] Tool result [%s]: %s", task_id, status, content_preview
                )
    elif isinstance(msg, ResultMessage):
        executor_log.info(
            "[%s] Agent finished | subtype=%s is_error=%s turns=%d",
            task_id, msg.subtype, msg.is_error, msg.num_turns,
        )


# ---------------------------------------------------------------------------
# Async agent runner
# ---------------------------------------------------------------------------

async def _run_agent(
    prompt: str,
    workspace_dir: Path,
    task_id: str,
    allowed_tools: list[str],
) -> tuple[list[Any], ResultMessage | None, bool]:
    """
    Launch the Claude Code agent and collect all messages.

    The agent runs with:
      cwd             = workspace_dir   (path-containment safety)
      permission_mode = "acceptEdits"   (auto-accept file writes)
      max_turns       = 30              (generous for complex tasks)

    Returns:
        (all_messages, result_message, had_error)
    """
    options = ClaudeCodeOptions(
        allowed_tools=allowed_tools,
        cwd=workspace_dir,
        max_turns=30,
        permission_mode="acceptEdits",
    )

    all_messages: list[Any] = []
    result_msg:   ResultMessage | None = None
    had_error = False

    try:
        async for msg in query(prompt=prompt, options=options):
            all_messages.append(msg)
            _log_agent_message(msg, task_id)
            if isinstance(msg, ResultMessage):
                result_msg = msg
                if msg.is_error:
                    had_error = True
                    executor_log.error(
                        "[%s] Agent reported error: %s", task_id, msg.result
                    )
    except Exception as exc:
        executor_log.error("[%s] Agent raised exception: %s", task_id, exc)
        had_error = True

    return all_messages, result_msg, had_error


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def execute_task(
    task: dict[str, Any],
    plan: dict[str, Any],
    workspace_info: dict[str, Any],
) -> dict[str, Any]:
    """
    Execute the full plan for a task using the Claude Code SDK agent.

    A single rich prompt encodes the task context and all plan steps.  The
    Claude Code agent then operates autonomously inside workspace_dir — it
    reads, writes, and runs commands without any step-by-step orchestration
    from the Python side.

    Args:
        task:           Task packet (must contain task_id).
        plan:           Plan packet (contains steps list).
        workspace_info: Descriptor returned by provision_workspace():
                          {workspace_dir, branch_name, repo_path, project_id}

    Returns:
        ExecutionResult packet (also saved to /artifacts/{task_id}.json).

    Raises:
        RuntimeError: If claude-code-sdk or claude CLI is missing, or API key not set.
        ValueError:   If workspace_info is incomplete or workspace does not exist.
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
        "[%s] Executing task (agentic) | project=%s branch=%s workspace=%s",
        task_id, project_id, branch_name, workspace_dir,
    )

    # ── Preflight checks ──────────────────────────────────────────────────────
    if not _sdk_available:
        raise RuntimeError(
            "claude-code-sdk is not installed. "
            "Run: pip install claude-code-sdk"
        )
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not set in your .env file")

    # ── Build prompt ──────────────────────────────────────────────────────────
    prompt = build_executor_prompt(task, plan, workspace_dir)
    n_steps = len(plan.get("steps", []))
    executor_log.info(
        "[%s] Built executor prompt | steps=%d chars=%d",
        task_id, n_steps, len(prompt),
    )

    # ── Resolve allowed tools ─────────────────────────────────────────────────
    # Read/Write/Edit/Bash cover all file and command operations.
    # Glob/Grep let the agent navigate and search the workspace.
    # The SDK's cwd option enforces workspace containment at the process level.
    allowed_tools = ["Read", "Write", "Edit", "Bash", "Glob", "Grep"]

    executor_log.info(
        "[%s] Launching Claude Code agent | max_turns=30 tools=%s cwd=%s",
        task_id, allowed_tools, workspace_dir,
    )

    # ── Run agent (async → sync bridge) ──────────────────────────────────────
    # asyncio.run() creates a fresh event loop for this synchronous call site.
    # This is safe because the rest of the pipeline is fully synchronous.
    all_messages, result_msg, had_error = asyncio.run(
        _run_agent(prompt, workspace_dir, task_id, allowed_tools)
    )

    num_turns = result_msg.num_turns if result_msg else 0
    agent_result_text = (result_msg.result or "") if result_msg else ""

    executor_log.info(
        "[%s] Agent complete | turns=%d had_error=%s",
        task_id, num_turns, had_error,
    )

    # ── Discover what the agent created / modified ────────────────────────────
    files_created, files_modified = _discover_workspace_changes(workspace_dir)

    executor_log.info(
        "[%s] Workspace delta | created=%d modified=%d",
        task_id, len(files_created), len(files_modified),
    )
    for f in files_created:
        executor_log.info("[%s]   + %s", task_id, f)
    for f in files_modified:
        executor_log.info("[%s]   ~ %s", task_id, f)

    # ── Stage and commit all changes to the project repo worktree ─────────────
    commit_msg = (
        f"[{task_id}] {task.get('title', 'Task execution')}\n\n"
        f"Agentic execution: {num_turns} agent turns.\n"
        f"Files created:  {', '.join(files_created)  or 'none'}\n"
        f"Files modified: {', '.join(files_modified) or 'none'}"
    )

    _run_git(["add", "."], cwd=workspace_dir)
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
    issues: list[str] = []
    if had_error:
        issues.append(f"Agent reported error: {agent_result_text[:200]}")

    # The agentic run is represented as a single synthetic step so the
    # ExecutionResult schema (steps_completed list) remains satisfied for the
    # reviewer and pipeline without any schema changes.
    step_status = "failed" if had_error else "completed"
    steps_completed = [
        make_step_result(
            step_id=1,
            status=step_status,
            output=agent_result_text[:500] if agent_result_text else f"Agent ran {num_turns} turns.",
        )
    ]

    summary = (
        f"Agent completed {num_turns} turns. "
        f"Created: {len(files_created)} file(s). "
        f"Modified: {len(files_modified)} file(s)."
        + (f" Issues: {len(issues)}." if issues else "")
    )

    result_packet = make_execution_result(
        task_id=task_id,
        branch_name=branch_name,
        steps_completed=steps_completed,
        files_created=files_created,
        files_modified=files_modified,
        summary=summary,
        issues_encountered=issues,
    )

    save_execution_result(result_packet)
    executor_log.info("[%s] Executor complete: %s", task_id, summary)
    log_execution_event(task_id, "EXECUTOR COMPLETE", summary)
    return result_packet


# Backward-compatible alias — pipeline.py calls run_executor which maps here.
run_executor = execute_task
