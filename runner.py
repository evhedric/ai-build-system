#!/usr/bin/env python3
"""
AI Orchestration System — Main Runner

Starts a background polling loop that:
  1. Watches /tasks for new task files
  2. Drives each task through the full pipeline
  3. Logs all actions and state transitions
  4. Displays a periodic status dashboard

Usage:
    python runner.py              # Start the runner
    python runner.py --once       # Process all pending tasks once, then exit
    python runner.py --dashboard  # Show dashboard and exit
"""

import argparse
import sys
import time
import signal

from runner.config import POLL_INTERVAL, validate_config
from runner.logger import runner_log, log_execution_event
from runner.state_manager import (
    initialize_state, shutdown_state, register_task, get_pending_tasks
)
from runner.pipeline import process_task
from runner.dashboard import show_dashboard


# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------

_running = True


def _handle_signal(signum, frame):
    global _running
    runner_log.info("Shutdown signal received — finishing current task then stopping...")
    _running = False


signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


# ---------------------------------------------------------------------------
# Core loop
# ---------------------------------------------------------------------------

def run_once() -> int:
    """Process all currently pending tasks. Returns count processed."""
    tasks = get_pending_tasks()
    if not tasks:
        runner_log.info("No pending tasks found.")
        return 0

    runner_log.info("Found %d pending task(s).", len(tasks))
    for task in tasks:
        register_task(task["task_id"])
        process_task(task)

    return len(tasks)


def run_loop() -> None:
    """
    Continuously watch for new tasks and process them.
    Press Ctrl+C to stop gracefully.
    """
    runner_log.info("=" * 60)
    runner_log.info("AI Orchestration System — Runner started")
    runner_log.info("Poll interval: %ds  |  Press Ctrl+C to stop", POLL_INTERVAL)
    runner_log.info("=" * 60)

    initialize_state()
    log_execution_event("SYSTEM", "RUNNER STARTED")

    dashboard_counter = 0
    DASHBOARD_EVERY = 10  # Show dashboard every N poll cycles

    try:
        while _running:
            # Check for and process pending tasks
            tasks = get_pending_tasks()
            for task in tasks:
                if not _running:
                    break
                register_task(task["task_id"])
                process_task(task)

            # Periodic dashboard refresh
            dashboard_counter += 1
            if dashboard_counter >= DASHBOARD_EVERY:
                show_dashboard()
                dashboard_counter = 0

            if _running:
                time.sleep(POLL_INTERVAL)

    finally:
        shutdown_state()
        log_execution_event("SYSTEM", "RUNNER STOPPED")
        runner_log.info("Runner stopped cleanly.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="AI Orchestration System Runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Process all pending tasks once and exit",
    )
    parser.add_argument(
        "--dashboard",
        action="store_true",
        help="Show the current dashboard and exit",
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="Validate configuration and exit",
    )
    args = parser.parse_args()

    # Config validation
    missing = validate_config()
    if missing:
        print(f"ERROR: Missing required configuration: {', '.join(missing)}")
        print("Copy .env.example to .env and fill in your API keys.")
        if not args.check_config:
            sys.exit(1)
        else:
            sys.exit(0)

    if args.check_config:
        print("Configuration OK.")
        sys.exit(0)

    if args.dashboard:
        show_dashboard()
        sys.exit(0)

    if args.once:
        initialize_state()
        count = run_once()
        shutdown_state()
        print(f"Processed {count} task(s).")
        sys.exit(0)

    # Default: start the continuous loop
    run_loop()


if __name__ == "__main__":
    main()
