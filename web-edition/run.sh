#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

echo "========================================================"
echo "  PartHarvester Web Edition (Python + FastAPI)"
echo "========================================================"

if ! command -v python3 >/dev/null 2>&1; then
    echo "[ERROR] python3 is not installed. Please install Python 3.10+."
    exit 1
fi

if ! python3 -c "import sys; assert sys.version_info >= (3, 10)" >/dev/null 2>&1; then
    echo "[ERROR] Python 3.10+ is required."
    exit 1
fi

if [ ! -d ".venv" ]; then
    echo "Creating virtual environment (.venv)..."
    if ! python3 -m venv .venv; then
        echo "[ERROR] Failed to create .venv. On Ubuntu/Debian, install: sudo apt install python3-venv"
        exit 1
    fi
fi

# shellcheck disable=SC1091
source .venv/bin/activate

if ! python -c "import fastapi, uvicorn, curl_cffi, cryptography, base58, psutil, pydantic, requests, aiofiles, websockets" >/dev/null 2>&1; then
    echo "Installing Python dependencies into .venv..."
    python -m pip install --upgrade pip >/dev/null
    pip install -r requirements.txt
fi

if ! command -v 7z >/dev/null 2>&1 && ! command -v 7zz >/dev/null 2>&1 && ! command -v unrar >/dev/null 2>&1; then
    echo "[NOTE] Neither 7-Zip (7z/7zz) nor unrar detected; Auto-Extract needs p7zip-full or unrar."
fi

exec python main.py
