#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

echo "========================================================"
echo "  PartHarvester Web Edition (Python + FastAPI)"
echo "========================================================"

if ! command -v python3 >/dev/null 2>&1; then
    echo "[ERROR] python3 is not installed. Please install Python 3.11+."
    exit 1
fi

if [ ! -d ".venv" ]; then
    echo "Creating virtual environment (.venv)..."
    python3 -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip >/dev/null
pip install -r requirements.txt

exec python main.py
