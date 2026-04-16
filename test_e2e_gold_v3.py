#!/usr/bin/env python3
"""
Gold v3 End-to-End Integration Test
====================================
Runs a single task through the full pipeline and prints a structured report.

Usage:
    python test_e2e_gold_v3.py
"""

import json
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from runner.schemas import make_task_packet, save_task
from runner.pipeline import process_task
from runner.config import TASKS_DIR, PLANS_DIR, ARTIFACTS_DIR, REVIEWS_DIR

# ---------------------------------------------------------------------------
# Task definition
# ---------------------------------------------------------------------------
TASK_REQUEST = (
    "Create a simple Node.js app with one API endpoint that returns 'Hello World'. "
    "The app should use the built-in http module (no Express), listen on port 3000, "
    "and respond to GET / with the text 'Hello World'."
)


def _section(title: str) -> None:
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


def _check(label: str, value, ok: bool) -> None:
    status = "OK" if ok else "FAIL"
    print(f"  [{status}] {label}: {value}")


def main() -> None:
    _section("Gold v3 End-to-End Test — Node.js Hello World")
    print(f"  Request: {TASK_REQUEST[:80]}...")

    # ── 1. Create and save task ───────────────────────────────────────────────
    task = make_task_packet(request=TASK_REQUEST, project="perchiq", priority="high")
    save_task(task)
    task_id = task["task_id"]

    _section(f"Task Created: {task_id}")
    print(f"  project:  {task['project']}")
    print(f"  status:   {task['status']}")
    print(f"  saved to: tasks/{task_id}.json")

    # ── 2. Run the pipeline ───────────────────────────────────────────────────
    _section("Running Pipeline")
    print("  (This will call Architect, Planner, Executor, Reviewer via API...)\n")

    start = time.time()
    result_task = None
    pipeline_error = None

    try:
        result_task = process_task(task)
    except Exception as exc:
        pipeline_error = exc
        traceback.print_exc()

    elapsed = time.time() - start

    # ── 3. Load artifacts ─────────────────────────────────────────────────────
    plan_file      = PLANS_DIR     / f"{task_id}.json"
    artifact_file  = ARTIFACTS_DIR / f"{task_id}.json"
    review_file    = REVIEWS_DIR   / f"{task_id}.json"

    plan     = json.loads(plan_file.read_text())     if plan_file.exists()     else {}
    artifact = json.loads(artifact_file.read_text()) if artifact_file.exists() else {}
    review   = json.loads(review_file.read_text())   if review_file.exists()   else {}

    # ── 4. Report ─────────────────────────────────────────────────────────────
    _section("Pipeline Results")
    print(f"  Elapsed: {elapsed:.1f}s")

    final_status = (result_task or {}).get("status", "ERROR")
    _check("Pipeline completed",   final_status,         pipeline_error is None)
    _check("Final task status",    final_status,         final_status in ("complete", "failed"))

    _section("Checklist")

    # 1. Project resolved
    workspace_info = {}
    branch_name = (result_task or task).get("branch_name", "")
    _check("1. Project resolved (perchiq)",
           task.get("project"), task.get("project") == "perchiq")

    # 2. Plan generated
    steps = plan.get("steps", [])
    _check("2. Plan generated", f"{len(steps)} steps", bool(steps))
    if steps:
        for s in steps:
            print(f"       [{s.get('action_type','?'):15s}] {s.get('description','')[:60]}")

    # 3. Workspace created
    ws_dir = ""
    if artifact:
        # Reconstruct from branch_name in artifact
        a_branch = artifact.get("branch_name", "")
        if a_branch:
            ws_dir = f"C:/dev/projects/perchiq/workspaces/task-{task_id}"
    ws_exists = Path(ws_dir).exists() if ws_dir else False
    _check("3. Workspace created (git worktree)", ws_dir or "?", ws_exists)

    # 4. Branch name
    a_branch = artifact.get("branch_name", "")
    _check("4. Task branch created", a_branch, bool(a_branch))

    # 5. Files created
    files_created  = artifact.get("files_created", [])
    files_modified = artifact.get("files_modified", [])
    _check("5. Files created in workspace",
           files_created, bool(files_created))
    for f in files_created:
        full = Path(ws_dir) / f if ws_dir else None
        exists = full.exists() if full else False
        mark = "OK" if exists else "!!"
        print(f"       [{mark}] {f}")
    for f in files_modified:
        full = Path(ws_dir) / f if ws_dir else None
        exists = full.exists() if full else False
        mark = "OK" if exists else "!!"
        print(f"       [~] {f}")

    # 6. Steps completed
    steps_done = artifact.get("steps_completed", [])
    passed = sum(1 for s in steps_done if s.get("status") == "completed")
    failed = sum(1 for s in steps_done if s.get("status") == "failed")
    _check("6. Steps executed",
           f"{passed} completed / {failed} failed / {len(steps_done)} total",
           passed > 0)

    # 7. Reviewer evaluated
    r_decision = review.get("decision", "")
    r_score    = review.get("score", "?")
    _check("7. Reviewer produced decision",
           f"{r_decision.upper() or 'NONE'} (score={r_score}/10)", bool(r_decision))
    if review.get("reasoning"):
        print(f"       Reasoning: {review['reasoning'][:100]}")

    # 8. Commit in project repo
    import subprocess
    git_log_result = subprocess.run(
        ["git", "log", "--oneline", "-3"],
        cwd="C:/dev/projects/perchiq/workspaces/" + f"task-{task_id}"
            if ws_exists else "C:/dev/projects/perchiq/repo",
        capture_output=True, text=True,
    )
    git_log = git_log_result.stdout.strip()
    _check("8. Commit exists in project repo", "", bool(git_log))
    if git_log:
        for line in git_log.splitlines():
            print(f"       {line}")

    # 9. Orchestrator repo untouched
    orch_status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(Path(__file__).parent),
        capture_output=True, text=True,
    )
    orch_clean = not orch_status.stdout.strip()
    _check("9. Orchestrator repo unchanged (no dirty files)",
           "clean" if orch_clean else orch_status.stdout.strip()[:80],
           orch_clean)

    # 10. Issues
    issues = artifact.get("issues_encountered", [])
    _check("10. No issues encountered", f"{len(issues)} issue(s)", len(issues) == 0)
    for iss in issues:
        print(f"       ! {iss}")

    # ── Summary ───────────────────────────────────────────────────────────────
    _section("Artifact Summary")
    print(f"  Execution summary: {artifact.get('summary', 'N/A')}")
    if pipeline_error:
        print(f"\n  PIPELINE ERROR: {pipeline_error}")

    _section("Done")
    if not pipeline_error and final_status == "complete":
        print("  [PASSED] Gold v3 end-to-end test PASSED")
    else:
        print("  [NEEDS REVIEW] Gold v3 end-to-end test NEEDS REVIEW")
        print(f"    Final status: {final_status}")
    print()


if __name__ == "__main__":
    main()
