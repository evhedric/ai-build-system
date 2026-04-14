#!/usr/bin/env bash
# AI Orchestration System — Start Script
# Usage: ./start.sh [--once] [--dashboard]

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Check for .env
if [ ! -f ".env" ]; then
    echo "ERROR: .env file not found."
    echo "Copy .env.example to .env and fill in your API keys:"
    echo "  cp .env.example .env"
    exit 1
fi

# Check for Python 3.10+
if ! command -v python3 &>/dev/null; then
    echo "ERROR: python3 not found. Please install Python 3.10+."
    exit 1
fi

PYTHON_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo "Using Python $PYTHON_VERSION"

# Install dependencies if needed
if ! python3 -c "import openai, anthropic, dotenv" &>/dev/null 2>&1; then
    echo "Installing dependencies..."
    pip install -r requirements.txt
fi

echo ""
echo "================================================"
echo "  AI Orchestration System"
echo "================================================"
echo ""

python3 runner.py "$@"
