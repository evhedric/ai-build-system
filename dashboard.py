#!/usr/bin/env python3
"""
Standalone dashboard viewer for the AI orchestration system.

Shows all tasks, their current states, stats, and any items needing attention.

Usage:
    python dashboard.py           # Show once and exit
    python dashboard.py --watch   # Refresh every N seconds
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from runner.dashboard import show_dashboard


def main() -> None:
    parser = argparse.ArgumentParser(description="AI Orchestration Dashboard")
    parser.add_argument(
        "--watch",
        type=int,
        metavar="SECONDS",
        nargs="?",
        const=5,
        help="Refresh interval in seconds (default 5 when --watch is passed)",
    )
    args = parser.parse_args()

    if args.watch:
        try:
            while True:
                # Clear screen (cross-platform)
                print("\033[H\033[J", end="")
                show_dashboard()
                print(f"  [Refreshing every {args.watch}s — Ctrl+C to exit]")
                time.sleep(args.watch)
        except KeyboardInterrupt:
            print("\nDashboard stopped.")
    else:
        show_dashboard()


if __name__ == "__main__":
    main()
