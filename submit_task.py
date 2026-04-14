#!/usr/bin/env python3
"""
Submit a new task to the AI orchestration system.

Usage:
    python submit_task.py "Your task description here"
    python submit_task.py --priority high "Your task description here"
    python submit_task.py --file task_request.txt
    python submit_task.py  # (interactive prompt)

The task file will be written to /tasks/{task_id}.json with status=pending.
The runner will pick it up automatically.
"""

import argparse
import json
import sys
from pathlib import Path

# Ensure the project root is on the path
sys.path.insert(0, str(Path(__file__).parent))

from runner.schemas import make_task_packet, save_task
from runner.config import TASKS_DIR


def submit(request: str, priority: str = "medium", tags: list[str] | None = None) -> dict:
    """Create and save a new task packet. Returns the packet."""
    task = make_task_packet(request=request, priority=priority, tags=tags or [])
    path = save_task(task)
    return task


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Submit a task to the AI orchestration system",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "request",
        nargs="?",
        help="The task request (natural language description)",
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
        "--file",
        type=Path,
        help="Read the task request from a text file",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output the task packet as JSON",
    )
    args = parser.parse_args()

    # Get the request text
    if args.file:
        if not args.file.exists():
            print(f"ERROR: File not found: {args.file}", file=sys.stderr)
            sys.exit(1)
        request = args.file.read_text(encoding="utf-8").strip()
    elif args.request:
        request = args.request
    else:
        # Interactive prompt
        print("Enter your task request (press Enter twice when done):")
        lines = []
        while True:
            line = input()
            if line == "" and lines and lines[-1] == "":
                break
            lines.append(line)
        request = "\n".join(lines).strip()

    if not request:
        print("ERROR: No task request provided.", file=sys.stderr)
        sys.exit(1)

    task = submit(request, priority=args.priority, tags=args.tags)

    if args.json:
        print(json.dumps(task, indent=2))
    else:
        print(f"\nTask submitted successfully!")
        print(f"  Task ID:  {task['task_id']}")
        print(f"  Priority: {task['priority']}")
        print(f"  Status:   {task['status']}")
        print(f"  File:     tasks/{task['task_id']}.json")
        print(f"\nThe runner will pick this up automatically.")
        print(f"Start the runner with:  python runner.py")
        print(f"Or run once:            python runner.py --once\n")


if __name__ == "__main__":
    main()
