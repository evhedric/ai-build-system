# Gold v3 — Locked Baseline

**Status: LOCKED**
**Tag: `gold-v3`**
**Validated: 2026-04-16**
**Validation score: 9/10 (APPROVED)**

This document defines the Gold v3 baseline of the AI Build System orchestrator.
No functional changes should be made to the executor, planner, reviewer, or
pipeline without understanding the compliance model described here.

---

## 1. What is Gold v3?

Gold v3 is the first deterministic, agent-driven execution baseline of the
AI Build System.  It replaces all previous approaches where the Python
orchestrator generated file content or wrote files directly.

### Core principles

| Principle | Implementation |
|-----------|---------------|
| **Deterministic execution** | Agent follows plan exactly; no creative interpretation |
| **Plan-as-contract** | Every step in the plan is a binding instruction, not a suggestion |
| **Strict compliance** | Agent may only create files explicitly listed in the plan |
| **Workspace isolation** | Every task runs in a dedicated git worktree; main repo is never touched |
| **Agent-only file operations** | No Python code writes files; all writes are performed by the Claude Code agent |

---

## 2. Architectural rules

These rules are invariants of the Gold v3 system.  Violating any of them
constitutes a regression.

### 2.1 Repo separation

- The **orchestrator repo** (`ai-build-system/`) is **never** a build target.
- All code generation, file creation, and command execution happens inside
  **project workspace repos** under `C:/dev/projects/<project>/workspaces/`.
- The orchestrator's `git status` must be clean after any task run.

### 2.2 Execution contract

- The agent **must** follow the plan exactly as written.
- The agent **must not** create files beyond those listed in the plan.
- The agent **must not** scaffold, initialize frameworks, or apply patterns
  from its training data.
- The agent **must not** explore the workspace unless a step explicitly
  requires reading a file.
- Workspace cleanup (the node `-e` one-liner) runs as the first step when
  the plan includes scaffolding commands.

### 2.3 Git operations

- All commits are made to the **project repo** worktree branch, never to the
  orchestrator repo.
- Branch names follow `feat/task-<task_id>` for workspace branches.
- Commits are made by the executor after the agent completes, not by the agent.

---

## 3. Executor design

### 3.1 Transport: stdin JSON streaming

**The executor uses `--input-format stream-json` (stdin) to deliver the prompt
to the Claude Code CLI.  It does NOT use `--print -- <text>` (CLI argument).**

Reason: On Windows, `--print` mode passes the prompt as a CLI argument.
Windows subprocess argument handling truncates multi-line strings at the first
newline boundary.  The agent received only the first line of the prompt and
nothing after — zero files were written across all revision cycles.

The fix: pass the prompt as a JSON-encoded message over stdin:
```json
{"type": "user", "message": {"role": "user", "content": "<full prompt>"}, ...}
```
JSON encoding preserves embedded newlines without truncation.

The `--system-prompt` flag is still a CLI argument (SDK constraint) and is
therefore flattened to a single line before passing.

### 3.2 System prompt

The executor injects a flat system prompt that establishes the deterministic
execution engine persona:

```
You are a deterministic execution engine. You do NOT think, design, or improve
anything. You ONLY execute instructions exactly as written. [strict rules] ...
```

This prompt is collapsed to a single line to avoid the Windows CLI argument
newline bug.

### 3.3 Prompt structure

The user prompt opens with an **imperative verb** that forces immediate action:

```
IMMEDIATELY perform Step 1 using the Write tool. Do not think. Do not analyze. Execute.

Step 1:
Write file at:
<ABSOLUTE_PATH>

Write EXACTLY this content:

===BEGIN FILE CONTENT===
<content>
===END FILE CONTENT===

Execute this step NOW using the Write tool.

You must execute Step 1 immediately. Do not wait. Do not explain. Do not summarize.
```

**Do NOT open the prompt with a noun** (`Workspace:`, `Task:`, `Context:`).
Noun-first openings cause the agent to treat the prompt as a context declaration
and enter listening mode, waiting for a command that never comes.

### 3.4 Execution modes

| Mode | Trigger | Allowed tools | Disallowed tools |
|------|---------|--------------|-----------------|
| `FILE_EXECUTION` | Any `create_file` / `modify_file` / `document` step | Write, Edit, Read | Bash, Glob, Grep, Task, LS, WebSearch, WebFetch, TodoWrite, TodoRead |
| `COMMAND_EXECUTION` | Only `run_command` steps | Bash, Read | Write, Edit, MultiEdit, Glob, Grep, Task, WebSearch, WebFetch, TodoWrite, TodoRead |

Both `--allowedTools` and `--disallowedTools` are used together.
`--allowedTools` alone does not reliably block built-in Claude Code tools.
`--disallowedTools` provides the hard exclusion layer.

### 3.5 Initialization handshake

Before the user prompt is sent, the executor sends an initialize control
request over stdin:
```json
{"type": "control_request", "request_id": "req_init_0", "request": {"subtype": "initialize", "hooks": null}}
```
The resulting `control_response` is skipped in the message read loop.

---

## 4. Known critical fixes (do not revert)

### 4.1 Windows CLI newline truncation

**Symptom:** Agent responds "I don't see any steps defined" / "no file path or
content has been provided."  Zero files written.  Consistent across all retries.

**Root cause:** `--print -- <multiline_text>` passes the prompt as a Windows
subprocess CLI argument.  Windows truncates at the first newline, so the agent
only receives the first line of the prompt.

**Fix:** Use `--input-format stream-json` mode.  Pass prompt via stdin as JSON.
Committed: `e4ca038`.

### 4.2 Noun-first prompt opening

**Symptom:** Agent responds "What would you like me to do?" / "Please provide
the steps."  Zero files written.

**Root cause:** Prompt opened with `"Workspace: C:\..."`.  The agent treated
this as a context/setup header and waited for an actual command.

**Fix:** Open with imperative verb: `"IMMEDIATELY perform Step 1..."`.
Committed: `9699da3`.

### 4.3 Hybrid executor (removed)

**Risk:** A prior implementation wrote file content directly from Python when
`plan.details` was populated, bypassing the agent entirely.  This was compliant
in output but non-deterministic in behavior (agent was not the actor).

**Fix:** Removed all Python file-write fallback.  Agent is the sole actor for
all file operations.  Committed during Gold v3 implementation.

### 4.4 Strict compliance enforcement

**Risk:** Agent's training prior ("professional Node.js project structure")
overrode explicit "Write EXACTLY this content" instructions, creating
unauthorized scaffold files.

**Fix:** System prompt establishes deterministic execution engine identity.
Mode-based tool restriction blocks `Bash` in FILE_EXECUTION mode (prevents
shell-based bootstrapping).  `disallowed_tools` adds hard exclusion layer.

---

## 5. Validation criteria

A Gold v3 execution is valid if and only if:

| Criterion | Check |
|-----------|-------|
| Only planned files created | `files_created` ⊆ `plan.steps[*].target` |
| No extra files | `len(files_created) == count(create_file steps in plan)` |
| Exact content | File on disk matches `step.details` verbatim |
| Agent used Write tool | At least one `Write` ToolUseBlock in agent messages |
| Reviewer approves | `review.decision == "approved"` |
| Workspace isolated | Project repo has commit; orchestrator repo is clean |

### Running the validation script

```bash
python scripts/validate_gold_v3.py
```

---

## 6. File locations

| Component | Path |
|-----------|------|
| Executor | `runner/roles/executor.py` |
| Planner | `runner/roles/planner.py` |
| Reviewer | `runner/roles/reviewer.py` |
| Pipeline | `runner/pipeline.py` |
| Workspace provisioner | `runner/workspace/provisioner.py` |
| Plan validator | `runner/plan_validator.py` |
| Schemas | `runner/schemas.py` |
| Config | `runner/config.py` |
| E2E test | `test_e2e_gold_v3.py` |
| Validation script | `scripts/validate_gold_v3.py` |

---

## 7. Dependency versions (validated)

| Package | Version |
|---------|---------|
| claude-code-sdk | 0.0.25 |
| Claude CLI | 2.1.111 |
| openai | latest |
| Python | 3.14 |

---

*This document is part of the Gold v3 locked baseline.
Do not modify without updating the git tag and re-running validation.*
