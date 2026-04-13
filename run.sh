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

# Install build dependencies commonly required for local PJSUA2 builds.
if command -v apt-get >/dev/null 2>&1; then
    if ! dpkg -s build-essential pkg-config libasound2-dev libssl-dev libopus-dev libvpx-dev libavcodec-dev libavformat-dev libswscale-dev python3-dev >/dev/null 2>&1; then
        echo "[+] Installing system packages required for PJSUA2 builds..."
        sudo apt-get update -y
        sudo apt-get install -y build-essential pkg-config libasound2-dev libssl-dev libopus-dev libvpx-dev libavcodec-dev libavformat-dev libswscale-dev python3-dev
    fi
fi

# Install PJSUA2 if available from pip; this is required for live SIP registration tests.
if ! python -c "import pjsua2" >/dev/null 2>&1; then
    echo "[+] Installing pjsua2..."
    pip install -q pjsua2 || {
        echo "[!] pjsua2 pip build failed."
        echo "[!] This usually means a compatible wheel or full local PJSIP source/bindings are still required."
    }
fi

# Ensure audio cache directory exists
mkdir -p audio_cache

echo "[+] Starting SMS Voice Gateway..."
uvicorn app.main:app \
    --host "${HOST:-0.0.0.0}" \
    --port "${PORT:-8000}" \
    --reload \
    --log-level info
