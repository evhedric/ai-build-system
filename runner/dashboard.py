"""
CLI Dashboard for the AI orchestration system.

Displays:
  - All tasks and their current states
  - System stats
  - Latest outputs / items needing attention

Uses the `rich` library for terminal formatting.
Fallback to plain text if rich is not available.
"""

from datetime import datetime
from pathlib import Path

from runner.schemas import load_all_tasks, load_review, load_execution_result
from runner.state_manager import get_system_state
from runner.config import REVIEWS_DIR, ARTIFACTS_DIR

try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.columns import Columns
    from rich import box
    _rich_available = True
    console = Console()
except ImportError:
    _rich_available = False


# Status → display color mapping for rich
STATUS_COLORS = {
    "pending":              "grey50",
    "planning":             "cyan",
    "ready_for_execution":  "blue",
    "executing":            "yellow",
    "in_review":            "magenta",
    "approved":             "green",
    "revise":               "orange3",
    "complete":             "bright_green",
    "failed":               "bright_red",
}

STATUS_ICONS = {
    "pending":              "⏳",
    "planning":             "🏛 ",
    "ready_for_execution":  "📋",
    "executing":            "⚙ ",
    "in_review":            "🔍",
    "approved":             "✅",
    "revise":               "🔄",
    "complete":             "✓ ",
    "failed":               "✗ ",
}


def _fmt_time(iso_str: str) -> str:
    try:
        dt = datetime.strptime(iso_str, "%Y-%m-%dT%H:%M:%SZ")
        return dt.strftime("%m/%d %H:%M")
    except Exception:
        return iso_str[:16]


def _attention_items(tasks: list[dict]) -> list[str]:
    """Return items that need human attention."""
    items = []
    for t in tasks:
        if t["status"] == "failed":
            reason = t.get("failure_reason", "unknown error")
            items.append(f"[FAILED] {t['task_id']}: {t.get('title', '')} — {reason}")
        elif t.get("revision_count", 0) >= t.get("max_revisions", 3) - 1:
            items.append(f"[MAX REVISIONS] {t['task_id']} is on revision {t['revision_count']}")
    return items


def _show_rich(tasks: list[dict], state: dict) -> None:
    console.print()

    # Header
    runner_status = state.get("runner_status", "unknown")
    status_color = "green" if runner_status == "running" else "red"
    console.print(Panel(
        f"[bold]AI Orchestration System[/bold]  "
        f"Runner: [{status_color}]{runner_status.upper()}[/{status_color}]  "
        f"Last update: {_fmt_time(state.get('last_updated', ''))}",
        style="bold blue",
    ))

    # Stats row
    stats = state.get("stats", {})
    stat_panels = [
        Panel(f"[cyan]{stats.get('total_processed', 0)}[/cyan]", title="Processed"),
        Panel(f"[green]{stats.get('total_approved', 0)}[/green]", title="Approved"),
        Panel(f"[red]{stats.get('total_failed', 0)}[/red]", title="Failed"),
        Panel(f"[yellow]{stats.get('total_revisions', 0)}[/yellow]", title="Revisions"),
    ]
    console.print(Columns(stat_panels))

    # Tasks table
    table = Table(
        title="Tasks",
        box=box.ROUNDED,
        show_header=True,
        header_style="bold",
        expand=True,
    )
    table.add_column("Task ID", style="dim", width=18)
    table.add_column("Title", min_width=30)
    table.add_column("Status", width=22)
    table.add_column("Rev", width=4, justify="center")
    table.add_column("Priority", width=8)
    table.add_column("Created", width=12)
    table.add_column("PR", width=8)

    for t in sorted(tasks, key=lambda x: x.get("created_at", ""), reverse=True):
        status = t.get("status", "unknown")
        color = STATUS_COLORS.get(status, "white")
        icon = STATUS_ICONS.get(status, " ")

        table.add_row(
            t["task_id"],
            (t.get("title") or t.get("request", ""))[:50],
            f"[{color}]{icon} {status}[/{color}]",
            str(t.get("revision_count", 0)),
            t.get("priority", "medium"),
            _fmt_time(t.get("created_at", "")),
            "✓" if t.get("pr_url") else "",
        )

    console.print(table)

    # Attention items
    attention = _attention_items(tasks)
    if attention:
        console.print()
        console.print(Panel(
            "\n".join(f"[bold red]•[/bold red] {item}" for item in attention),
            title="[bold red]Needs Attention[/bold red]",
            border_style="red",
        ))

    # Latest review for the most recent reviewed task
    reviewed = [t for t in tasks if t["status"] in ("approved", "complete", "revise")]
    if reviewed:
        latest = sorted(reviewed, key=lambda x: x.get("updated_at", ""), reverse=True)[0]
        review_path = REVIEWS_DIR / f"{latest['task_id']}.json"
        if review_path.exists():
            try:
                review = load_review(latest["task_id"])
                decision_color = "green" if review["decision"] == "approved" else "yellow"
                console.print()
                console.print(Panel(
                    f"[bold]Task:[/bold] {latest['task_id']} — {latest.get('title', '')}\n"
                    f"[bold]Decision:[/bold] [{decision_color}]{review['decision'].upper()}[/{decision_color}]  "
                    f"Score: {review.get('score', '?')}/10\n"
                    f"[bold]Reasoning:[/bold] {review.get('reasoning', '')[:200]}",
                    title="Latest Review",
                    border_style="blue",
                ))
            except Exception:
                pass

    console.print()


def _show_plain(tasks: list[dict], state: dict) -> None:
    print("\n" + "=" * 70)
    print(f"  AI ORCHESTRATION SYSTEM  |  Runner: {state.get('runner_status', 'unknown').upper()}")
    print("=" * 70)

    stats = state.get("stats", {})
    print(f"  Processed: {stats.get('total_processed', 0)}  |  "
          f"Approved: {stats.get('total_approved', 0)}  |  "
          f"Failed: {stats.get('total_failed', 0)}  |  "
          f"Revisions: {stats.get('total_revisions', 0)}")
    print("-" * 70)

    for t in sorted(tasks, key=lambda x: x.get("created_at", ""), reverse=True):
        icon = STATUS_ICONS.get(t["status"], " ")
        print(
            f"  {icon} {t['task_id']:18s}  [{t['status']:22s}]  "
            f"rev={t.get('revision_count', 0)}  "
            f"{(t.get('title') or t.get('request', ''))[:40]}"
        )

    attention = _attention_items(tasks)
    if attention:
        print("\n  NEEDS ATTENTION:")
        for item in attention:
            print(f"  ! {item}")

    print("=" * 70 + "\n")


def show_dashboard() -> None:
    """Print the current dashboard to the terminal."""
    tasks = load_all_tasks()
    state = get_system_state()

    if _rich_available:
        _show_rich(tasks, state)
    else:
        _show_plain(tasks, state)
