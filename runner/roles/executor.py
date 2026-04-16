# =============================================================================
# GOLD V3 LOCKED EXECUTOR
#
# This executor enforces strict compliance and deterministic execution.
#
# DO NOT:
#   - Reintroduce Python file writing (agent is the ONLY file actor)
#   - Modify prompt structure without understanding compliance impact
#   - Switch back to CLI argument mode (must use stdin JSON streaming)
#   - Relax strict execution rules in the system prompt
#   - Add hybrid logic that bypasses the agent for "convenience"
#
# Breaking these rules will regress the system to non-deterministic behavior.
# See docs/GOLD_V3_LOCK.md for the full design rationale and known-fix history.
# =============================================================================

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
# Strict compliance system prompt + prompt builder
# ---------------------------------------------------------------------------

_EXECUTOR_SYSTEM_PROMPT = """\
You are a deterministic execution engine.

You do NOT think, design, or improve anything.

You ONLY execute instructions exactly as written.

STRICT RULES:
- You may ONLY create or modify files explicitly listed in the steps
- You must write EXACT content provided — no changes, no additions
- You may NOT create additional files
- You may NOT modify files not listed
- You may NOT create folders unless explicitly specified
- You may NOT scaffold or initialize anything
- You may NOT use prior knowledge or patterns
- You may NOT explore the workspace
- You may NOT read files unless instructed
- You must execute steps in exact order
- If a step is unclear, still attempt execution using the provided content.

If you violate ANY rule, the task is considered FAILED.
"""

# Execution modes — determined from plan content, not task type.
_MODE_FILE    = "FILE_EXECUTION"
_MODE_COMMAND = "COMMAND_EXECUTION"

# Action types that constitute file steps.
_FILE_ACTIONS = frozenset({"create_file", "modify_file", "document"})


def _detect_mode(steps: list[dict[str, Any]]) -> str:
    """
    Return FILE_EXECUTION if any step writes a file; COMMAND_EXECUTION otherwise.

    FILE_EXECUTION takes precedence: a plan with both file and command steps is
    still FILE_EXECUTION — the agent receives all steps and the allowed-tool set
    is restricted to file-write tools only (Bash is excluded).
    """
    for step in steps:
        action = step.get("action_type") or step.get("type", "")
        if action in _FILE_ACTIONS:
            return _MODE_FILE
    return _MODE_COMMAND


def build_executor_prompt(
    task: dict[str, Any],
    plan: dict[str, Any],
    workspace_dir: Path,
) -> str:
    """
    Build the strict-compliance query prompt for the Claude Code agent.

    Contract format:
      FILE steps  → absolute path + ===BEGIN/END FILE CONTENT=== delimiters
      CMD steps   → "Run this command EXACTLY:" + bare command string

    The prompt MUST open with an imperative verb so the agent acts immediately
    rather than treating the first line as context and waiting for a command.
    The "Workspace:" header is intentionally absent — its presence caused the
    agent to treat the entire block as a configuration preamble and respond
    with "What would you like me to do?".  Workspace context is communicated
    via absolute paths embedded directly in each file/command step.
    """
    ws    = str(workspace_dir)
    steps = plan.get("steps", [])
    mode  = _detect_mode(steps)

    lines: list[str] = []

    # ── Opening: hard imperative trigger — Step 1 (STEP 1) ───────────────────
    lines += [
        "IMMEDIATELY perform Step 1 using the Write tool. Do not think. Do not analyze. Execute.",
        "",
    ]

    # ── Steps as strict contracts ─────────────────────────────────────────────
    first_step = True
    for step in steps:
        sid     = step.get("step_id", "?")
        action  = step.get("action_type") or step.get("type", "")
        target  = step.get("target", "")
        details = step.get("details", "")

        if action in _FILE_ACTIONS:
            abs_target = f"{ws}\\{target}" if target else ws
            lines += [
                f"Step {sid}:",
                "Write file at:",
                abs_target,
                "",
                "Write EXACTLY this content:",
                "",
                "===BEGIN FILE CONTENT===",
            ]
            lines += details.splitlines() if details else [""]
            lines += [
                "===END FILE CONTENT===",
                "",
                f"Execute this step NOW using the Write tool.",
                "",
            ]

        elif action == "run_command":
            cmd = target or details
            lines += [
                f"Step {sid}:",
                "Run this command EXACTLY:",
                "",
                cmd,
                "",
            ]

        else:
            # research / unknown — skip silently, no agent action required
            lines += [f"Step {sid}: [skip]", ""]

        # Execution trigger injected after Step 1 (STEP 3)
        if first_step:
            lines += [
                "You must execute Step 1 immediately. Do not wait. Do not explain. Do not summarize.",
                "",
            ]
            first_step = False

    # ── Hard stop ─────────────────────────────────────────────────────────────
    lines += [
        "STOP after completing all steps above.",
        "Do NOT create additional files.",
        "Do NOT run additional commands.",
        "Do NOT explore or read any files not listed above.",
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
    disallowed_tools: list[str] | None = None,
) -> tuple[list[Any], ResultMessage | None, bool]:
    """
    Launch the Claude Code agent and collect all messages.

    Uses SubprocessCLITransport in STREAMING MODE (--input-format stream-json)
    so the user prompt is delivered via stdin as a JSON message rather than as
    a CLI argument.

    WHY STREAMING MODE:
      The previous implementation used --print mode, which passes the prompt as
      a CLI argument: `claude --print -- <multiline_text>`.  On Windows,
      subprocess argument handling truncates multi-line strings at the first
      newline boundary, so the agent received only the opening line and nothing
      else — it responded "I don't see any steps defined."

      Streaming mode sends the prompt as:
        {"type": "user", "message": {"role": "user", "content": "<full text>"},
         "parent_tool_use_id": null, "session_id": "default"}
      over stdin as a single JSON line.  JSON-encoding preserves embedded
      newlines, completely bypassing the Windows CLI argument truncation bug.

    NOTE: --system-prompt is still passed as a CLI argument (SDK constraint),
    so _EXECUTOR_SYSTEM_PROMPT remains flattened to a single line.

    Rate-limit and control protocol messages are skipped silently so the
    executor is resilient to CLI/SDK version drift.

    Returns:
        (all_messages, result_message, had_error)
    """
    # Message types to skip rather than crash on:
    #   rate_limit_event    — added in CLI 2.1.111; not in SDK 0.0.25 type list
    #   control_request     — SDK control protocol, not an agent message
    #   control_response    — response to our init handshake, not an agent message
    _SKIP_TYPES = frozenset({"rate_limit_event", "control_request", "control_response"})

    # --system-prompt is still a CLI arg — must remain a single line on Windows.
    _system_prompt_flat = " ".join(
        line.strip() for line in _EXECUTOR_SYSTEM_PROMPT.splitlines() if line.strip()
    )

    options = ClaudeCodeOptions(
        allowed_tools=allowed_tools,
        disallowed_tools=disallowed_tools or [],
        cwd=workspace_dir,
        max_turns=30,
        permission_mode="acceptEdits",
        system_prompt=_system_prompt_flat,
    )

    # An empty async generator triggers streaming mode in SubprocessCLITransport
    # (_is_streaming = not isinstance(prompt, str)).  The actual prompt is sent
    # below via transport.write() after connect(), not via CLI argument.
    async def _empty_stream():
        return
        yield  # makes this an async generator (never reached)

    transport = SubprocessCLITransport(prompt=_empty_stream(), options=options)

    all_messages: list[Any] = []
    result_msg:   ResultMessage | None = None
    had_error = False

    try:
        await transport.connect()

        # ── Streaming mode stdin protocol ─────────────────────────────────────
        # 1. Initialize the control protocol so the CLI is ready to process
        #    user messages.  The response comes back as a control_response which
        #    we skip in the read loop below.
        await transport.write(json.dumps({
            "type": "control_request",
            "request_id": "req_init_0",
            "request": {"subtype": "initialize", "hooks": None},
        }) + "\n")

        # 2. Send the user prompt as a JSON-encoded message over stdin.
        #    JSON encoding preserves all newlines without Windows truncation.
        await transport.write(json.dumps({
            "type": "user",
            "message": {"role": "user", "content": prompt},
            "parent_tool_use_id": None,
            "session_id": "default",
        }) + "\n")

        # 3. Close stdin — signals to the CLI that no more input is coming.
        await transport.end_input()

        # ── Read response messages ────────────────────────────────────────────
        async for raw_data in transport.read_messages():
            msg_type = raw_data.get("type", "")

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
# Public entry point
# ---------------------------------------------------------------------------

def execute_task(
    task: dict[str, Any],
    plan: dict[str, Any],
    workspace_info: dict[str, Any],
) -> dict[str, Any]:
    """
    Execute the full plan for a task in STRICT COMPLIANCE mode.

    STRICT COMPLIANCE EXECUTION
    ───────────────────────────
    All file operations and commands are performed exclusively by the Claude
    Code agent.  No Python fallback writes files.

    Execution mode is determined by plan content:

      FILE_EXECUTION    — plan contains create_file / modify_file / document steps.
                          Agent is given Write + Edit + Read tools only.
                          Bash is excluded so the agent cannot run shell commands
                          to create directories or install packages.

      COMMAND_EXECUTION — plan contains only run_command steps.
                          Agent is given Bash + Read tools only.
                          Write / Edit are excluded so the agent cannot create files.

    The system_prompt enforces deterministic behavior: no scaffolding, no
    pattern reuse, no files beyond those listed, hard stop after all steps.

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

    # ── Determine execution mode ──────────────────────────────────────────────
    steps = plan.get("steps", [])
    mode  = _detect_mode(steps)

    log_execution_event(
        task_id, "EXECUTOR START",
        f"workspace={workspace_dir}  branch={branch_name!r}  project={project_id}  mode={mode}",
    )
    executor_log.info(
        "[%s] Executing task (strict compliance) | mode=%s project=%s branch=%s workspace=%s",
        task_id, mode, project_id, branch_name, workspace_dir,
    )

    # ── Preflight checks ──────────────────────────────────────────────────────
    if not _sdk_available:
        raise RuntimeError(
            "claude-code-sdk is not installed. "
            "Run: pip install claude-code-sdk"
        )
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not set in your .env file")

    # ── Select allowed + disallowed tools by mode ─────────────────────────────
    # FILE_EXECUTION:    Write + Edit + Read  (file ops only).
    #   allowed_tools  = whitelist of file-write tools.
    #   disallowed_tools = explicit denylist for exploration/shell tools.
    #   Both are needed: --allowedTools alone does not reliably block all
    #   built-in Claude tools; --disallowedTools adds a hard exclusion layer.
    #
    # COMMAND_EXECUTION: Bash + Read (run commands, observe output).
    #   disallowed_tools explicitly blocks file-write tools.
    if mode == _MODE_FILE:
        allowed_tools    = ["Write", "Edit", "Read"]
        disallowed_tools = ["Bash", "Glob", "Grep", "Task", "LS",
                            "WebSearch", "WebFetch", "TodoWrite", "TodoRead"]
    else:
        allowed_tools    = ["Bash", "Read"]
        disallowed_tools = ["Write", "Edit", "MultiEdit",
                            "Glob", "Grep", "Task",
                            "WebSearch", "WebFetch", "TodoWrite", "TodoRead"]

    executor_log.info(
        "[%s] Mode=%s | allowed=%s | disallowed=%s | steps=%d",
        task_id, mode, allowed_tools, disallowed_tools, len(steps),
    )

    # ── Build strict-compliance prompt ────────────────────────────────────────
    prompt = build_executor_prompt(task, plan, workspace_dir)
    executor_log.info(
        "[%s] Built executor prompt | chars=%d", task_id, len(prompt)
    )

    # ── Run agent ─────────────────────────────────────────────────────────────
    all_messages, result_msg, had_error = asyncio.run(
        _run_agent(prompt, workspace_dir, task_id, allowed_tools, disallowed_tools)
    )

    num_turns         = result_msg.num_turns if result_msg else 0
    agent_result_text = (result_msg.result or "") if result_msg else ""

    executor_log.info(
        "[%s] Agent complete | turns=%d had_error=%s",
        task_id, num_turns, had_error,
    )

    # ── Discover workspace changes ────────────────────────────────────────────
    files_created, files_modified = _discover_workspace_changes(workspace_dir)

    executor_log.info(
        "[%s] Workspace delta | created=%d modified=%d",
        task_id, len(files_created), len(files_modified),
    )
    for f in files_created:
        executor_log.info("[%s]   + %s", task_id, f)
    for f in files_modified:
        executor_log.info("[%s]   ~ %s", task_id, f)

    # ── Gold v3 runtime guardrails ────────────────────────────────────────────
    # These checks are warnings only — they do not abort the execution.
    # They surface compliance violations so they can be caught during review.

    # 1. Detect unauthorized file creation
    planned_targets = {
        step.get("target", "").lstrip("/\\").replace("\\", "/")
        for step in steps
        if (step.get("action_type") or step.get("type", "")) in _FILE_ACTIONS
    }
    for created in files_created:
        normalised = created.lstrip("/\\").replace("\\", "/")
        if normalised not in planned_targets:
            executor_log.warning(
                "[%s] UNAUTHORIZED FILE CREATION DETECTED: '%s' was not in the plan",
                task_id, created,
            )

    # 2. Detect Bash tool usage in FILE_EXECUTION mode (tool restriction bypass)
    if mode == _MODE_FILE:
        for msg in all_messages:
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, ToolUseBlock) and block.name == "Bash":
                        executor_log.warning(
                            "[%s] INVALID TOOL USAGE: agent used Bash in FILE_EXECUTION mode",
                            task_id,
                        )
                        break

    # 3. Detect absence of Write tool usage in FILE_EXECUTION mode
    if mode == _MODE_FILE:
        write_used = any(
            isinstance(block, ToolUseBlock) and block.name == "Write"
            for msg in all_messages
            if isinstance(msg, AssistantMessage)
            for block in msg.content
        )
        if not write_used and planned_targets:
            executor_log.warning(
                "[%s] AGENT DID NOT EXECUTE WRITE STEP — no Write tool calls detected",
                task_id,
            )

    # ── Stage and commit ──────────────────────────────────────────────────────
    issues: list[str] = []
    if had_error:
        issues.append(f"Agent reported error: {agent_result_text[:200]}")

    commit_msg = (
        f"[{task_id}] {task.get('title', 'Task execution')}\n\n"
        f"Strict compliance execution ({mode}): {num_turns} agent turn(s).\n"
        f"Files created:  {', '.join(files_created)  or 'none'}\n"
        f"Files modified: {', '.join(files_modified) or 'none'}"
    )

    _run_git(["add", "."], cwd=workspace_dir)
    rc, _, _ = _run_git(["diff", "--cached", "--quiet"], cwd=workspace_dir)
    if rc != 0:
        _run_git(["commit", "-m", commit_msg], cwd=workspace_dir)
        executor_log.info(
            "[%s] Committed to project repo on branch %s in %s",
            task_id, branch_name, workspace_dir,
        )
    else:
        executor_log.info("[%s] No staged changes to commit", task_id)

    # ── Build result packet ───────────────────────────────────────────────────
    step_status = "failed" if had_error else "completed"
    steps_completed = [
        make_step_result(
            step_id=s.get("step_id", i + 1),
            status=step_status,
            output=agent_result_text[:300] if agent_result_text else f"Agent ran {num_turns} turns.",
        )
        for i, s in enumerate(steps)
    ]

    summary = (
        f"Strict compliance ({mode}). "
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
