#!/usr/bin/env python3
"""
Submit a new task to the AI orchestration system.

Primary usage — pass a JSON payload:
    python submit_task.py '{"project": "perchiq", "task": "Your description"}'
    python submit_task.py --file task.json
    python submit_task.py  # (interactive JSON prompt)

JSON input format:
    {
      "project": "<name>",        # required — must exist in projects.json
      "task":    "<description>", # required
      "create":  true | false     # optional — create project if it doesn't exist
    }

Project selection rules:
    - "project" is required.  Missing project → rejected.
    - If the project exists in projects.json → proceed.
    - If the project does NOT exist:
        - create == true  → create workspace + register → proceed.
        - create == false (or omitted) → rejected with an actionable error.

Legacy plain-text usage (project defaults to "perchiq"):
    python submit_task.py --text "Your task description"
    python submit_task.py --text "..." --project perchiq --priority high
"""

import argparse
import json
import sys
from pathlib import Path

# Ensure the project root is on the path
sys.path.insert(0, str(Path(__file__).parent))

from runner.schemas import make_task_packet, save_task
from runner.config import TASKS_DIR
from runner.project_registry import (
    find_project,
    project_exists,
    create_project,
)


# ---------------------------------------------------------------------------
# Project validation gate
# ---------------------------------------------------------------------------

def _resolve_project(project_name: str, create: bool) -> dict:
    """
    Enforce explicit project selection.

    Returns the project registry entry on success.
    Calls sys.exit(1) on any validation failure.
    """
    if not project_name:
        print("ERROR: 'project' is required.", file=sys.stderr)
        sys.exit(1)

    entry = find_project(project_name)

    if entry:
        return entry

    # Project not in registry
    if create:
        entry = create_project(project_name)
        print(f"Project created: {entry['name']}  ->  {entry['path']}")
        return entry

    print(
        f"ERROR: Project '{project_name}' does not exist.\n"
        f"       Set \"create\": true to create it, or choose an existing project.",
        file=sys.stderr,
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# Core submit function
# ---------------------------------------------------------------------------

def submit(request: str, project: str, priority: str = "medium", tags: list[str] | None = None) -> dict:
    """Create and save a new task packet. Returns the packet."""
    task = make_task_packet(
        request=request,
        project=project,
        priority=priority,
        tags=tags or [],
    )
    save_task(task)
    return task


# ---------------------------------------------------------------------------
# JSON input parsing
# ---------------------------------------------------------------------------

def _parse_json_input(raw: str) -> tuple[str, str, bool]:
    """
    Parse the JSON payload and return (project, task_description, create).
    Exits with error on missing required fields.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"ERROR: Invalid JSON input: {exc}", file=sys.stderr)
        sys.exit(1)

    if not isinstance(data, dict):
        print("ERROR: JSON input must be an object.", file=sys.stderr)
        sys.exit(1)

    project = data.get("project", "").strip()
    task_desc = data.get("task", "").strip()
    create = bool(data.get("create", False))

    if not project:
        print("ERROR: JSON input is missing required field: \"project\"", file=sys.stderr)
        sys.exit(1)
    if not task_desc:
        print("ERROR: JSON input is missing required field: \"task\"", file=sys.stderr)
        sys.exit(1)

    return project, task_desc, create


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Submit a task to the AI orchestration system",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # JSON input mode (primary)
    parser.add_argument(
        "json_input",
        nargs="?",
        help='JSON payload: \'{"project": "...", "task": "..."}\'',
    )
    parser.add_argument(
        "--file",
        type=Path,
        help="Read the JSON payload from a file",
    )

    # Legacy plain-text mode
    parser.add_argument(
        "--text",
        help="Plain-text task description (legacy mode; project defaults to --project value)",
    )
    parser.add_argument(
        "--project",
        default="perchiq",
        help="Project name for plain-text mode (default: perchiq)",
    )
    parser.add_argument(
        "--create",
        action="store_true",
        help="Create the project if it does not exist (plain-text mode)",
    )
    parser.add_argument(
        "--priority",
        choices=["low", "medium", "high"],
        default="medium",
        help="Task priority (default: medium)",
    )
    parser.add_argument(
        "--tags",
        nargs="*",
        default=[],
        help="Optional tags for the task",
    )
    parser.add_argument(
        "--output-json",
        action="store_true",
        help="Print the task packet as JSON",
    )

    args = parser.parse_args()

    # ------------------------------------------------------------------
    # Determine input mode and parse project/task
    # ------------------------------------------------------------------

    if args.file:
        if not args.file.exists():
            print(f"ERROR: File not found: {args.file}", file=sys.stderr)
            sys.exit(1)
        raw = args.file.read_text(encoding="utf-8").strip()
        project_name, task_desc, create = _parse_json_input(raw)

    elif args.json_input:
        project_name, task_desc, create = _parse_json_input(args.json_input)

    elif args.text:
        # Legacy plain-text mode
        project_name = args.project
        task_desc = args.text.strip()
        create = args.create
        if not task_desc:
            print("ERROR: --text value is empty.", file=sys.stderr)
            sys.exit(1)

    else:
        # Interactive mode — prompt for JSON
        print("Enter JSON task payload (single line), then press Enter:")
        print('  Example: {"project": "perchiq", "task": "Your description"}')
        try:
            raw = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.", file=sys.stderr)
            sys.exit(1)
        project_name, task_desc, create = _parse_json_input(raw)

    # ------------------------------------------------------------------
    # Project validation gate
    # ------------------------------------------------------------------
    _resolve_project(project_name, create)

    # ------------------------------------------------------------------
    # Submit
    # ------------------------------------------------------------------
    task = submit(task_desc, project=project_name, priority=args.priority, tags=args.tags)

    if args.output_json:
        print(json.dumps(task, indent=2))
    else:
        print(f"\nTask submitted successfully!")
        print(f"  Task ID:  {task['task_id']}")
        print(f"  Project:  {task['project']}")
        print(f"  Priority: {task['priority']}")
        print(f"  Status:   {task['status']}")
        print(f"  File:     tasks/{task['task_id']}.json")
        print(f"\nThe runner will pick this up automatically.")
        print(f"Start the runner with:  python runner.py")
        print(f"Or run once:            python runner.py --once\n")


if __name__ == "__main__":
    main()
