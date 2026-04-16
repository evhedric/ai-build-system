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
    from claude_code_sdk._internal.message_parser import parse_message as _sdk_parse_message
    from claude_code_sdk._internal.transport.subprocess_cli import SubprocessCLITransport
    from claude_code_sdk._errors import MessageParseError
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

_EXECUTOR_SYSTEM_PROMPT = """\
You are an autonomous code executor embedded in an AI build pipeline.

Your ONLY job is to immediately create files and run commands as instructed in each task.

Rules (non-negotiable):
- Use the Write tool to create the first file BEFORE doing anything else.
- Do NOT read memory files, project history, or other task workspaces.
- Do NOT explore the filesystem before acting.
- Do NOT ask questions or wait for confirmation.
- Execute every step in the order given, then stop.
- All file paths are absolute. Write files exactly where specified.
- Never start a persistent server (http.listen / npm start / npm run dev).
- When finished, output a short summary of every file created and every command run.
"""


def build_executor_prompt(
    task: dict[str, Any],
    plan: dict[str, Any],
    workspace_dir: Path,
) -> str:
    """
    Build the imperative query prompt sent to the Claude Code agent.

    Design rules:
    - Opens with an action verb ("Create" / "Write") on the very first line so
      the agent's natural first response is a Write tool call, not exploration.
    - Every file step is expressed as an absolute path + what to put in it.
    - Every command step is expressed as: run <cmd> in <workspace_dir>.
    - Ends with a single anti-exploration reminder, not an extended preamble.

    The system_prompt (_EXECUTOR_SYSTEM_PROMPT) handles role/behavior locking.
    This query prompt carries only the concrete task instructions.
    """
    ws = str(workspace_dir)
    steps = plan.get("steps", [])

    lines: list[str] = []

    # ── Opening action line ───────────────────────────────────────────────────
    # Must start with a verb so the model's first natural act is tool use.
    lines += [
        f"Create and write the required project files in this workspace: {ws}",
        "",
        f"Task: {task.get('title', task.get('request', ''))}",
        f"Goal: {task.get('request', '')}",
        "",
    ]

    if task.get("success_criteria"):
        lines.append("Success criteria:")
        lines += [f"  - {c}" for c in task["success_criteria"]]
        lines.append("")

    # ── Steps as direct imperatives ───────────────────────────────────────────
    if steps:
        lines.append("Execute these steps in order — start with step 1 immediately:")
        lines.append("")
        for step in steps:
            sid    = step.get("step_id", "?")
            action = step.get("action_type", step.get("type", ""))
            desc   = step.get("description", "")
            target = step.get("target", "")
            details = step.get("details", "")

            if action in ("create_file", "modify_file", "document"):
                abs_target = f"{ws}\\{target}" if target else ws
                lines.append(f"Step {sid}: Write file `{abs_target}`")
                lines.append(f"  Purpose: {desc}")
                if details:
                    # If the details field contains actual file content (code, text),
                    # present it as the required content to write — not just "guidance".
                    # This prevents the agent from substituting its own interpretation.
                    lines.append(f"  Write EXACTLY this content to the file:")
                    lines.append("  ```")
                    for content_line in details.splitlines():
                        lines.append(f"  {content_line}")
                    lines.append("  ```")
                else:
                    lines.append(f"  (Implement the full required functionality for: {desc})")

            elif action == "run_command":
                cmd = target or details
                lines.append(f"Step {sid}: Run command in {ws}:")
                lines.append(f"  $ {cmd}")
                if desc:
                    lines.append(f"  ({desc})")

            elif action == "research":
                lines.append(f"Step {sid}: Research — {desc}")
                if details:
                    lines.append(f"  Focus: {details}")

            else:
                lines.append(f"Step {sid}: [{action}] {desc}")
                if target:
                    lines.append(f"  Target: {target}")
                if details:
                    lines.append(f"  Details: {details}")

            lines.append("")

    # ── Constraints ───────────────────────────────────────────────────────────
    lines += [
        "Constraints:",
        f"  - All files must be written inside {ws}",
        "  - Do not start any persistent server process",
        "  - Do not explore the workspace or read memory files before acting",
        "  - Begin with the Write tool on step 1 right now",
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

    Uses SubprocessCLITransport directly (bypassing query()) so we can
    silently skip message types that the SDK version doesn't know about
    (e.g. 'rate_limit_event' added in CLI 2.1.111 but absent from SDK 0.0.25).
    This makes the executor resilient to CLI/SDK version drift.

    The agent runs with:
      cwd             = workspace_dir   (path-containment safety)
      permission_mode = "acceptEdits"   (auto-accept file writes)
      system_prompt   = _EXECUTOR_SYSTEM_PROMPT  (behavioral locking)
      max_turns       = 30              (generous for complex tasks)

    Returns:
        (all_messages, result_message, had_error)
    """
    # Message types emitted by the CLI that we silently skip rather than crash on.
    # rate_limit_event was added in CLI 2.1.111; SDK 0.0.25 raises MessageParseError.
    _SKIP_TYPES = frozenset({"rate_limit_event"})

    # The CLI's --system-prompt flag does not accept multiline strings when
    # using --print mode on Windows (newlines break argument parsing).
    # Collapse all newlines + blank lines to single spaces so the prompt
    # fits on one logical line while preserving the full rule set.
    _system_prompt_flat = " ".join(
        line.strip() for line in _EXECUTOR_SYSTEM_PROMPT.splitlines() if line.strip()
    )

    options = ClaudeCodeOptions(
        allowed_tools=allowed_tools,
        cwd=workspace_dir,
        max_turns=30,
        permission_mode="acceptEdits",
        system_prompt=_system_prompt_flat,
    )

    transport = SubprocessCLITransport(prompt=prompt, options=options)

    all_messages: list[Any] = []
    result_msg:   ResultMessage | None = None
    had_error = False

    try:
        await transport.connect()

        async for raw_data in transport.read_messages():
            msg_type = raw_data.get("type", "")

            # Skip known-but-unhandled message types (e.g. rate_limit_event)
            if msg_type in _SKIP_TYPES:
                executor_log.debug(
                    "[%s] Skipping CLI message type '%s'", task_id, msg_type
                )
                continue

            try:
                msg = _sdk_parse_message(raw_data)
            except MessageParseError as exc:
                executor_log.warning(
                    "[%s] Could not parse CLI message (type=%r): %s — skipping",
                    task_id, msg_type, exc,
                )
                continue

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
    finally:
        await transport.close()

    return all_messages, result_msg, had_error


# ---------------------------------------------------------------------------
# Direct file writer (for steps with explicit content)
# ---------------------------------------------------------------------------

def _write_file_step(
    step: dict[str, Any],
    workspace_dir: Path,
    task_id: str,
) -> tuple[str, str, str | None]:
    """
    Write a create_file / modify_file / document step directly to disk.

    This bypasses the Claude Code agent entirely — the plan content is
    written verbatim, so the agent's training priors cannot override it.

    Returns:
        (relative_path, status, error_message | None)
    """
    target  = step.get("target", "").strip()
    details = step.get("details", "").strip()

    if not target:
        return ("", "skipped", "step has no target path")

    abs_path = workspace_dir / target
    _validate_path_in_workspace(abs_path, workspace_dir)

    try:
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text(details, encoding="utf-8")
        executor_log.info(
            "[%s] Wrote file: %s (%d bytes)",
            task_id, target, len(details),
        )
        return (target, "written", None)
    except Exception as exc:
        executor_log.error(
            "[%s] Failed to write file %s: %s", task_id, target, exc
        )
        return (target, "failed", str(exc))


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def execute_task(
    task: dict[str, Any],
    plan: dict[str, Any],
    workspace_info: dict[str, Any],
) -> dict[str, Any]:
    """
    Execute the full plan for a task.

    HYBRID EXECUTION STRATEGY
    ─────────────────────────
    File steps (create_file, modify_file, document):
      Written directly to disk from the plan's ``details`` content.
      This is deterministic — the exact planned content is always written,
      bypassing any risk of the agent substituting its own interpretation.

    Command steps (run_command):
      Executed via the Claude Code SDK agent so the agent can observe
      command output, handle errors, and adapt as needed.

    If no run_command steps exist, the agent is not invoked at all.

    Args:
        task:           Task packet (must contain task_id).
        plan:           Plan packet (contains steps list).
        workspace_info: Descriptor returned by provision_workspace():
                          {workspace_dir, branch_name, repo_path, project_id}

    Returns:
        ExecutionResult packet (also saved to /artifacts/{task_id}.json).
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
        "[%s] Executing task (hybrid) | project=%s branch=%s workspace=%s",
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

    steps = plan.get("steps", [])
    issues:         list[str] = []
    steps_completed: list[dict[str, Any]] = []
    files_written:   list[str] = []   # directly written (not via agent)

    # ── Phase 1: Write all file steps directly ────────────────────────────────
    FILE_ACTIONS = {"create_file", "modify_file", "document"}
    file_steps    = [s for s in steps if (s.get("action_type") or s.get("type", "")) in FILE_ACTIONS]
    command_steps = [s for s in steps if (s.get("action_type") or s.get("type", "")) == "run_command"]

    executor_log.info(
        "[%s] Plan split: %d file step(s), %d command step(s)",
        task_id, len(file_steps), len(command_steps),
    )

    for step in file_steps:
        sid = step.get("step_id", "?")
        target, status, err = _write_file_step(step, workspace_dir, task_id)
        if status == "written":
            files_written.append(target)
            steps_completed.append(
                make_step_result(step_id=sid, status="completed",
                                 output=f"Wrote {target}")
            )
        elif status == "skipped":
            executor_log.warning("[%s] Skipped step %s: %s", task_id, sid, err)
            steps_completed.append(
                make_step_result(step_id=sid, status="skipped", output=err or "")
            )
        else:  # failed
            issues.append(f"Step {sid}: failed to write {target}: {err}")
            steps_completed.append(
                make_step_result(step_id=sid, status="failed", output=err or "")
            )

    # ── Phase 2: Run command steps via Claude Code agent ─────────────────────
    num_turns         = 0
    agent_result_text = ""
    had_agent_error   = False

    if command_steps:
        # Build a focused agent prompt — file writes are already done.
        cmd_prompt = build_executor_prompt(task, {**plan, "steps": command_steps}, workspace_dir)
        allowed_tools = ["Read", "Write", "Edit", "Bash", "Glob", "Grep"]

        executor_log.info(
            "[%s] Launching Claude Code agent for %d command step(s)",
            task_id, len(command_steps),
        )

        all_messages, result_msg, had_agent_error = asyncio.run(
            _run_agent(cmd_prompt, workspace_dir, task_id, allowed_tools)
        )
        num_turns         = result_msg.num_turns if result_msg else 0
        agent_result_text = (result_msg.result or "") if result_msg else ""

        executor_log.info(
            "[%s] Agent complete | turns=%d had_error=%s",
            task_id, num_turns, had_agent_error,
        )

        if had_agent_error:
            issues.append(f"Agent reported error: {agent_result_text[:200]}")

        # Add a synthetic step entry for the agent's work
        step_status = "failed" if had_agent_error else "completed"
        for step in command_steps:
            sid = step.get("step_id", "?")
            steps_completed.append(
                make_step_result(
                    step_id=sid, status=step_status,
                    output=agent_result_text[:300] if agent_result_text else f"Agent ran {num_turns} turns.",
                )
            )
    else:
        executor_log.info("[%s] No command steps — skipping agent invocation", task_id)

    # ── Discover all workspace changes (file writes + agent commands) ──────────
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
        f"Hybrid execution: {len(files_written)} file(s) written directly, "
        f"{num_turns} agent turn(s).\n"
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
    summary = (
        f"Wrote {len(files_written)} file(s) directly. "
        f"Agent ran {num_turns} turn(s). "
        f"Workspace: {len(files_created)} created, {len(files_modified)} modified."
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
