#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
cd "$BASE_DIR" || exit 1

mkdir -p data/logs
LOG_FILE="data/logs/webhook_ensure.log"
PYTHON_BIN="${PYTHON_BIN:-python3}"
if [ -f ".venv/bin/activate" ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
  PYTHON_BIN="python"
fi

HOST="${PC_WEBHOOK_HOST:-0.0.0.0}"
PORT="${PC_WEBHOOK_PORT:-8765}"
HEALTH_HOST="$HOST"
[ "$HEALTH_HOST" = "0.0.0.0" ] && HEALTH_HOST="127.0.0.1"
HEALTH_URL="http://${HEALTH_HOST}:${PORT}/health"

log() {
  echo "$(date '+%Y-%m-%d %H:%M:%S') | $*" | tee -a "$LOG_FILE"
}

health_ok() {
  "$PYTHON_BIN" - "$HEALTH_URL" <<'PY' >/dev/null 2>&1
import sys
from urllib.request import urlopen
url = sys.argv[1]
with urlopen(url, timeout=2) as response:
    body = response.read().decode('utf-8', errors='replace').strip()
    raise SystemExit(0 if response.status == 200 and body == 'ok' else 1)
PY
}

if health_ok; then
  log "Webhook listener already healthy at $HEALTH_URL."
  exit 0
fi

if [ ! -s .webhook_token ]; then
  log "Cannot start webhook listener: .webhook_token is missing or empty."
  exit 1
fi

if pgrep -f "[w]ebhook_listener.py" >/dev/null 2>&1; then
  log "Webhook listener process exists but health failed at $HEALTH_URL; leaving process untouched."
  exit 1
fi

log "Starting webhook listener on ${HOST}:${PORT}."
PC_WEBHOOK_HOST="$HOST" PC_WEBHOOK_PORT="$PORT" nohup "$PYTHON_BIN" ./webhook_listener.py >> data/logs/webhook_listener.log 2>&1 &

for _ in 1 2 3 4 5 6 7 8 9 10; do
  if health_ok; then
    log "Webhook listener healthy at $HEALTH_URL."
    exit 0
  fi
  sleep 0.5
done

log "Webhook listener did not become healthy at $HEALTH_URL. Check data/logs/webhook_listener.log."
exit 1
