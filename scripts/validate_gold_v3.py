#!/usr/bin/env python3
"""
Gold v3 Compliance Validation Script
=====================================
Runs a deterministic test task through the full pipeline and validates that
the executor behaves in strict compliance with the Gold v3 contract:

  - Only planned files are created (no unauthorized files)
  - File content matches what the plan specified
  - No extra scaffold files exist in the workspace
  - Reviewer approves the output
  - Orchestrator repo remains clean

Usage:
    python scripts/validate_gold_v3.py

Exit codes:
    0 — all checks PASS
    1 — one or more checks FAIL
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

# Make runner importable from repo root
sys.path.insert(0, str(Path(__file__).parent.parent))

from runner.config import ARTIFACTS_DIR, PLANS_DIR, REVIEWS_DIR
from runner.pipeline import process_task
from runner.schemas import make_task_packet, save_task

# ---------------------------------------------------------------------------
# Test definition
# ---------------------------------------------------------------------------

TASK_REQUEST = (
    "Create a single JavaScript file named hello.js that, when run with "
    "node hello.js, prints 'Hello World' to stdout and exits.  "
    "Use only built-in Node.js — no npm packages."
)

# The validation checks what the plan asked for by inspecting plan.steps.
# We do NOT hardcode the expected filename here so the planner remains free
# to name the file; we only verify that EXACTLY what was planned was created.


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PASS = "[PASS]"
_FAIL = "[FAIL]"
_WARN = "[WARN]"


def _check(label: str, passed: bool, detail: str = "") -> bool:
    status = _PASS if passed else _FAIL
    suffix = f"  ({detail})" if detail else ""
    print(f"  {status}  {label}{suffix}")
    return passed


def _section(title: str) -> None:
    print(f"\n{'─' * 60}")
    print(f"  {title}")
    print(f"{'─' * 60}")


# ---------------------------------------------------------------------------
# Main validation
# ---------------------------------------------------------------------------

def main() -> int:
    _section("Gold v3 Compliance Validation")
    print(f"  Task: {TASK_REQUEST[:80]}...")

    # ── 1. Create and run task ────────────────────────────────────────────────
    task = make_task_packet(request=TASK_REQUEST, project="perchiq", priority="high")
    save_task(task)
    task_id = task["task_id"]
    print(f"  Task ID: {task_id}\n")

    start = time.time()
    result_task = None
    pipeline_error = None

    try:
        result_task = process_task(task)
    except Exception as exc:
        pipeline_error = exc
        import traceback
        traceback.print_exc()

    elapsed = time.time() - start

    # ── 2. Load artifacts ─────────────────────────────────────────────────────
    plan_file     = PLANS_DIR     / f"{task_id}.json"
    artifact_file = ARTIFACTS_DIR / f"{task_id}.json"
    review_file   = REVIEWS_DIR   / f"{task_id}.json"

    plan     = json.loads(plan_file.read_text())     if plan_file.exists()     else {}
    artifact = json.loads(artifact_file.read_text()) if artifact_file.exists() else {}
    review   = json.loads(review_file.read_text())   if review_file.exists()   else {}

    ws_dir = f"C:/dev/projects/perchiq/workspaces/task-{task_id}"
    ws_path = Path(ws_dir)

    # ── 3. Collect plan expectations ─────────────────────────────────────────
    plan_steps       = plan.get("steps", [])
    file_actions     = {"create_file", "modify_file", "document"}
    planned_files    = [
        s.get("target", "")
        for s in plan_steps
        if (s.get("action_type") or s.get("type", "")) in file_actions
        and s.get("target", "")
    ]
    planned_contents = {
        s.get("target", ""): s.get("details", "")
        for s in plan_steps
        if (s.get("action_type") or s.get("type", "")) in file_actions
    }

    # ── 4. Collect actual workspace files ────────────────────────────────────
    files_created = artifact.get("files_created", [])

    # Files in workspace beyond .git tracking (non-hidden, non-git)
    workspace_files: list[str] = []
    if ws_path.exists():
        workspace_files = [
            str(p.relative_to(ws_path)).replace("\\", "/")
            for p in ws_path.rglob("*")
            if p.is_file()
            and not any(part.startswith(".") for part in p.relative_to(ws_path).parts)
        ]

    # ── 5. Run checks ─────────────────────────────────────────────────────────
    _section("Checks")
    all_passed = True

    def fail(label: str, detail: str = "") -> bool:
        nonlocal all_passed
        _check(label, False, detail)
        all_passed = False
        return False

    def ok(label: str, detail: str = "") -> bool:
        _check(label, True, detail)
        return True

    # Check 1: pipeline completed without exception
    if pipeline_error:
        fail("Pipeline completed without exception", str(pipeline_error)[:80])
    else:
        ok("Pipeline completed without exception", f"{elapsed:.1f}s")

    # Check 2: workspace created
    _check("Workspace created", ws_path.exists(), ws_dir) or (all_passed := False)

    # Check 3: at least one file was created
    if not _check("At least one file created", bool(files_created),
                  f"{len(files_created)} file(s)"):
        all_passed = False

    # Check 4: ONLY planned files were created (no extras)
    planned_set = {f.replace("\\", "/").lstrip("/") for f in planned_files}
    created_set = {f.replace("\\", "/").lstrip("/") for f in files_created}
    extra_files  = created_set - planned_set
    if extra_files:
        fail("No unauthorized files created", f"extra: {', '.join(sorted(extra_files))}")
    else:
        ok("No unauthorized files created")

    # Check 5: all planned files exist on disk
    for pf in planned_files:
        disk_path = ws_path / pf
        _check(f"Planned file exists on disk: {pf}", disk_path.exists()) or (
            all_passed := False
        )

    # Check 6: content matches plan (for files with non-empty details)
    for pf, expected in planned_contents.items():
        if not expected:
            continue
        disk_path = ws_path / pf
        if not disk_path.exists():
            fail(f"Content match: {pf}", "file missing")
            continue
        actual = disk_path.read_text(encoding="utf-8", errors="replace")
        if actual.strip() == expected.strip():
            ok(f"Content matches plan: {pf}")
        else:
            # Partial content match is acceptable (agent may normalise line endings)
            core_ok = expected.strip()[:100] in actual
            label = f"Content match (partial): {pf}"
            _check(label, core_ok,
                   "first 100 chars found" if core_ok else "content diverges") or (
                all_passed := False
            )

    # Check 7: no scaffold / framework files beyond the plan
    scaffold_markers = [
        "package.json", "package-lock.json", "node_modules",
        "tsconfig.json", "next.config", ".eslintrc",
        "requirements.txt", "setup.py", "pyproject.toml",
    ]
    scaffold_found = [f for f in workspace_files if any(m in f for m in scaffold_markers)]
    if scaffold_found:
        fail("No scaffold files in workspace",
             f"found: {', '.join(scaffold_found[:3])}")
    else:
        ok("No scaffold files in workspace")

    # Check 8: reviewer approved
    r_decision = review.get("decision", "").lower()
    r_score    = review.get("score", 0)
    _check("Reviewer approved result",
           r_decision == "approved",
           f"decision={r_decision or 'none'} score={r_score}/10") or (
        all_passed := False
    )

    # Check 9: commit exists in project repo
    git_log = ""
    if ws_path.exists():
        result = subprocess.run(
            ["git", "log", "--oneline", "-1"],
            cwd=str(ws_path), capture_output=True, text=True,
        )
        git_log = result.stdout.strip()
    _check("Commit exists in project repo", bool(git_log), git_log or "no commits") or (
        all_passed := False
    )

    # Check 10: orchestrator repo is clean
    orch_result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(Path(__file__).parent.parent),
        capture_output=True, text=True,
    )
    orch_dirty = orch_result.stdout.strip()
    _check("Orchestrator repo is clean",
           not orch_dirty,
           "clean" if not orch_dirty else orch_dirty[:60]) or (
        all_passed := False
    )

    # ── 6. Summary ────────────────────────────────────────────────────────────
    _section("Summary")
    print(f"  Files planned:  {planned_files}")
    print(f"  Files created:  {files_created}")
    print(f"  Workspace files: {workspace_files}")
    print(f"  Reviewer:       {r_decision.upper() or 'NONE'} ({r_score}/10)")
    print(f"  Elapsed:        {elapsed:.1f}s")

    _section("Result")
    if all_passed:
        print("  [PASS]  Gold v3 compliance validation PASSED")
        return 0
    else:
        print("  [FAIL]  Gold v3 compliance validation FAILED")
        return 1


if __name__ == "__main__":
    sys.exit(main())
