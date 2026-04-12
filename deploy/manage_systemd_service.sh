#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="${SERVICE_NAME:-sms-voice-gateway.service}"
ACTION="${1:-status}"

case "$ACTION" in
  start|stop|restart|status|enable|disable)
    sudo systemctl "$ACTION" "$SERVICE_NAME" --no-pager
    ;;
  logs)
    sudo journalctl -u "$SERVICE_NAME" -n "${LINES:-100}" --no-pager
    ;;
  *)
    echo "Usage: $0 {start|stop|restart|status|enable|disable|logs}" >&2
    exit 1
    ;;
esac
