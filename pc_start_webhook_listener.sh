#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" || exit 1
mkdir -p data/logs

HOST="${PC_WEBHOOK_HOST:-0.0.0.0}"
PORT="${PC_WEBHOOK_PORT:-8765}"
TOKEN_FILE=".webhook_token"
LOG_FILE="data/logs/webhook_listener.out.log"

webhook_listener_running() {
  pgrep -f "[w]ebhook_listener.py" >/dev/null 2>&1
}

if [ ! -s "$TOKEN_FILE" ]; then
  echo "ERROR: $TOKEN_FILE is missing or empty. Create it with: printf 'YOUR_SECRET_TOKEN' > $TOKEN_FILE" >&2
  exit 1
fi

if webhook_listener_running; then
  echo "Webhook listener is already running."
  pgrep -af "[w]ebhook_listener.py" || true
  exit 0
fi

PYTHON_BIN="python3"
if [ -x .venv/bin/python ]; then
  PYTHON_BIN=".venv/bin/python"
fi

nohup env PC_WEBHOOK_HOST="$HOST" PC_WEBHOOK_PORT="$PORT" \
  "$PYTHON_BIN" ./webhook_listener.py >> "$LOG_FILE" 2>&1 &

sleep 1
if webhook_listener_running; then
  echo "Webhook listener started on $HOST:$PORT. Log: $LOG_FILE"
  pgrep -af "[w]ebhook_listener.py" || true
  exit 0
fi

echo "ERROR: webhook listener did not stay running. Last log lines:" >&2
tail -40 "$LOG_FILE" >&2 2>/dev/null || true
exit 1
