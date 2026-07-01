#!/usr/bin/env bash
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../../lib/env.sh
source "$SCRIPT_DIR/../../lib/env.sh"
ROOT="$APP_ROOT"
cd "$ROOT" || exit 1

PORT="${PC_WEBHOOK_PORT:-8765}"
HOST="${PC_WEBHOOK_HOST:-0.0.0.0}"
LOG_DIR="$PC_LOG_DIR"
TOKEN_FILE="$ROOT/.webhook_token"

mask_token() {
  sed -E 's#(/panamacompra/)[^?[:space:]]+#\1TOKEN_HIDDEN#g; s#TOKEN=[^[:space:]]+#TOKEN=TOKEN_HIDDEN#g'
}

section() {
  printf '\n=== %s ===\n' "$1"
}

fail_tail() {
  echo "ERROR: $1"
  echo "Last listener output:"
  tail -80 "$LOG_DIR/webhook_listener.out.log" 2>/dev/null | mask_token || true
  exit 1
}

echo "============================================================"
echo " PanamaCompra webhook diagnostic"
echo "============================================================"

mkdir -p "$LOG_DIR"

section "1) Current branch/status"
git status --short --branch || true

section "2) Check token file"
if [ ! -f "$TOKEN_FILE" ]; then
  echo "ERROR: .webhook_token does not exist."
  echo "Creating a new token now..."
  python3 - <<'PY' > "$TOKEN_FILE"
import secrets
print(secrets.token_hex(24))
PY
  chmod 600 "$TOKEN_FILE"
  echo "Created .webhook_token"
else
  chmod 600 "$TOKEN_FILE"
  echo ".webhook_token exists."
  echo "Token length:"
  wc -c "$TOKEN_FILE"
fi

TOKEN="$(tr -d '\n\r' < "$TOKEN_FILE")"
if [ -z "$TOKEN" ]; then
  fail_tail ".webhook_token is empty."
fi

section "3) Check if webhook listener is running"
pgrep -af "src/webhook/listener.py" || echo "No src/webhook/listener.py process found."

section "4) Check if port $PORT is listening"
ss -ltnp | grep ":$PORT" || echo "Port $PORT is not listening."

section "5) Stop broken direct systemd webhook service and old listener if any"
# A direct ExecStart=python src/webhook/listener.py service will restart-loop when an
# older receiver owns the port. Stop it before replacing the port owner.
systemctl --user stop panamacompra-webhook.service 2>/dev/null || true
pkill -f "[p]ython.*src/webhook/listener.py" 2>/dev/null || true
sleep 1

section "6) Start webhook listener bound to $HOST:$PORT"
PC_WEBHOOK_HOST="$HOST" PC_WEBHOOK_PORT="$PORT" "$ROOT/src/webhook/start-listener.sh" --replace-port-owner
sleep 2

section "7) Confirm listener process"
pgrep -af "src/webhook/listener.py" || fail_tail "webhook listener did not start."

section "8) Confirm port $PORT is listening"
ss -ltnp | grep ":$PORT" || fail_tail "port $PORT is still not listening."

section "9) Test webhook from Linux host using 127.0.0.1"
curl -i "http://127.0.0.1:$PORT/panamacompra/$TOKEN" || {
  echo "ERROR: local curl test failed."
  exit 1
}

section "10) Detect changedetection.io Docker container"
CD_CONTAINER="$(docker ps --format '{{.Names}}' 2>/dev/null | grep -Ei 'changedetection|change-detection|changedetection.io' | head -1 || true)"

if [ -z "$CD_CONTAINER" ]; then
  echo "WARNING: Could not auto-detect changedetection.io container."
  echo "Available containers:"
  docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}' 2>/dev/null || echo "Docker is not available or not running."
else
  echo "Detected container: $CD_CONTAINER"

  section "11) Test webhook from inside changedetection.io container"
  docker exec -e TOKEN="$TOKEN" -e PORT="$PORT" "$CD_CONTAINER" python3 - <<'PY' || {
import os
import urllib.request

token = os.environ["TOKEN"]
port = os.environ.get("PORT", "8765")
url = f"http://host.docker.internal:{port}/panamacompra/{token}"

print("Testing:", url.replace(token, "TOKEN_HIDDEN"))
response = urllib.request.urlopen(url, timeout=10)
body = response.read().decode("utf-8", errors="replace")
print("HTTP status:", response.status)
print("Body:", body.strip())
PY
    echo "ERROR: Docker-to-host webhook test failed."
    exit 1
  }
fi

section "12) Recent logs"
echo
echo "--- webhook_listener.log ---"
tail -50 "$LOG_DIR/webhook_listener.log" 2>/dev/null | mask_token || true

echo
echo "--- collector_triggered.log ---"
tail -50 "$LOG_DIR/collector_triggered.log" 2>/dev/null | mask_token || true

echo
echo "--- run_all_requests.log ---"
tail -50 "$LOG_DIR/run_all_requests.log" 2>/dev/null | mask_token || true

echo
echo "============================================================"
echo " Done."
echo " If step 11 passed with HTTP 202, changedetection.io can reach the webhook."
echo " To install a persistent user service, run: ./src/webhook/install-service.sh"
echo "============================================================"
