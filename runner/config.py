"""
Configuration loader for the AI orchestration system.
Reads from .env file and environment variables.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env from the project root
# override=True ensures .env values win over empty system environment variables
BASE_DIR = Path(__file__).parent.parent
load_dotenv(BASE_DIR / ".env", override=True)

# --- API Keys ---
OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
GITHUB_TOKEN: str = os.getenv("GITHUB_TOKEN", "")

# --- Model Selection ---
OPENAI_MODEL: str = os.getenv("OPENAI_MODEL", "gpt-4o")
CLAUDE_MODEL: str = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")

# --- GitHub ---
GITHUB_REPO: str = os.getenv("GITHUB_REPO", "evhedric/ai-build-system")
AUTO_CREATE_PR: bool = os.getenv("AUTO_CREATE_PR", "false").lower() == "true"

# --- Runner Behavior ---
MAX_REVISIONS: int = int(os.getenv("MAX_REVISIONS", "3"))
POLL_INTERVAL: int = int(os.getenv("POLL_INTERVAL", "3"))  # seconds between task scans

# --- Directory Paths ---
TASKS_DIR = BASE_DIR / "tasks"
PLANS_DIR = BASE_DIR / "plans"
ARTIFACTS_DIR = BASE_DIR / "artifacts"
REVIEWS_DIR = BASE_DIR / "reviews"
STATE_DIR = BASE_DIR / "state"
LOGS_DIR = BASE_DIR / "logs"
PROMPTS_DIR = BASE_DIR / "prompts"
SCHEMAS_DIR = BASE_DIR / "schemas"

# Ensure all directories exist at import time
for _dir in [TASKS_DIR, PLANS_DIR, ARTIFACTS_DIR, REVIEWS_DIR, STATE_DIR, LOGS_DIR, PROMPTS_DIR, SCHEMAS_DIR]:
    _dir.mkdir(parents=True, exist_ok=True)


def validate_config() -> list[str]:
    """Return a list of missing critical config items."""
    missing = []
    if not OPENAI_API_KEY:
        missing.append("OPENAI_API_KEY")
    if not ANTHROPIC_API_KEY:
        missing.append("ANTHROPIC_API_KEY")
    return missing
