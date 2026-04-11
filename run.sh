#!/usr/bin/env bash
# Quick start for development (without Docker).
# Requires Python 3.10+ and a running Redis instance.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Copy env if not present
if [ ! -f .env ]; then
    cp .env.example .env
    echo "[!] Created .env from .env.example – please edit it before running."
    exit 1
fi

# Create virtual environment if missing
if [ ! -d .venv ]; then
    python3 -m venv .venv
    echo "[+] Created virtualenv"
fi

source .venv/bin/activate
pip install -q --upgrade pip
pip install -q -r requirements.txt

# Ensure audio cache directory exists
mkdir -p audio_cache

echo "[+] Starting SMS Voice Gateway..."
uvicorn app.main:app \
    --host "${HOST:-0.0.0.0}" \
    --port "${PORT:-8000}" \
    --reload \
    --log-level info
