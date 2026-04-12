#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="${SERVICE_NAME:-sms-voice-gateway.service}"
SERVICE_USER="${SERVICE_USER:-$(id -un)}"
SERVICE_GROUP="${SERVICE_GROUP:-$(id -gn)}"
WORKING_DIRECTORY="${WORKING_DIRECTORY:-$(cd "$(dirname "$0")/.." && pwd)}"
ENV_FILE="${ENV_FILE:-$WORKING_DIRECTORY/.env}"
PYTHON_BIN="${PYTHON_BIN:-$WORKING_DIRECTORY/.venv/bin/python}"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"

if [[ ! -f "$WORKING_DIRECTORY/deploy/sms-voice-gateway.service" ]]; then
  echo "Service template not found at $WORKING_DIRECTORY/deploy/sms-voice-gateway.service" >&2
  exit 1
fi

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Python binary not found or not executable: $PYTHON_BIN" >&2
  exit 1
fi

TMP_FILE="$(mktemp)"
trap 'rm -f "$TMP_FILE"' EXIT

sed \
  -e "s|__SERVICE_USER__|$SERVICE_USER|g" \
  -e "s|__SERVICE_GROUP__|$SERVICE_GROUP|g" \
  -e "s|__WORKING_DIRECTORY__|$WORKING_DIRECTORY|g" \
  -e "s|__ENV_FILE__|$ENV_FILE|g" \
  -e "s|__PYTHON_BIN__|$PYTHON_BIN|g" \
  -e "s|__HOST__|$HOST|g" \
  -e "s|__PORT__|$PORT|g" \
  "$WORKING_DIRECTORY/deploy/sms-voice-gateway.service" > "$TMP_FILE"

sudo cp "$TMP_FILE" "/etc/systemd/system/$SERVICE_NAME"
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
sudo systemctl restart "$SERVICE_NAME"
sudo systemctl status "$SERVICE_NAME" --no-pager
