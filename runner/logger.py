"""
Centralized logging setup for the AI orchestration system.

Each phase (architect, planner, executor, reviewer, runner) gets its own
log file plus a shared execution history log.
"""

import logging
import sys
from datetime import datetime
from pathlib import Path

from runner.config import LOGS_DIR


def _create_logger(name: str, log_file: Path, level: int = logging.DEBUG) -> logging.Logger:
    """Create a logger that writes to both file and stdout."""
    logger = logging.getLogger(name)
    logger.setLevel(level)

    if logger.handlers:
        return logger  # Already configured

    fmt = logging.Formatter(
        "%(asctime)s  [%(levelname)-8s]  %(name)s  —  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # File handler
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)

    # Console handler (INFO and above)
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)

    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger


# --- Public loggers ---

runner_log = _create_logger("runner", LOGS_DIR / "runner.log")
architect_log = _create_logger("architect", LOGS_DIR / "architect.log")
planner_log = _create_logger("planner", LOGS_DIR / "planner.log")
executor_log = _create_logger("executor", LOGS_DIR / "executor.log")
reviewer_log = _create_logger("reviewer", LOGS_DIR / "reviewer.log")
github_log = _create_logger("github", LOGS_DIR / "github.log")


def log_execution_event(task_id: str, event: str, details: str = "") -> None:
    """Append a structured entry to the shared execution history log."""
    history_file = LOGS_DIR / "execution_history.log"
    ts = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"{ts}  [{task_id}]  {event}"
    if details:
        line += f"  |  {details}"
    with open(history_file, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    runner_log.info("[%s] %s %s", task_id, event, f"| {details}" if details else "")


def log_error(task_id: str, phase: str, error: Exception) -> None:
    """Append a structured error entry to the shared error log."""
    error_file = LOGS_DIR / "errors.log"
    ts = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"{ts}  [{task_id}]  [{phase}]  {type(error).__name__}: {error}"
    with open(error_file, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    runner_log.error("[%s] [%s] %s: %s", task_id, phase, type(error).__name__, error)
