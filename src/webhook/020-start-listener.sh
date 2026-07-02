#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
# shellcheck source=../../lib/env.sh
source "$SCRIPT_DIR/../../lib/env.sh"
cd "$APP_ROOT"

HOST="${PC_WEBHOOK_HOST:-0.0.0.0}"
PORT="${PC_WEBHOOK_PORT:-8765}"
TOKEN_FILE="$APP_ROOT/.webhook_token"
LOG_FILE="$PC_LOG_DIR/webhook_listener.out.log"
REPLACE_PORT_OWNER="${PC_WEBHOOK_REPLACE_PORT_OWNER:-0}"
FOREGROUND=0

usage() {
  cat <<USAGE
Usage: $0 [--replace-port-owner] [--no-replace-port-owner] [--foreground]

Starts src/webhook/010-webhook-listener.py in the background.
  --replace-port-owner     Stop the current process listening on PC_WEBHOOK_PORT first.
  --no-replace-port-owner  Never stop a non-webhook process; print diagnostics only.
  --foreground             Run listener in the foreground (for systemd services).

Environment:
  PC_WEBHOOK_HOST              Bind host (default: 0.0.0.0)
  PC_WEBHOOK_PORT              Bind port (default: 8765)
  PC_WEBHOOK_REPLACE_PORT_OWNER=1  Same as --replace-port-owner
  PC_WEBHOOK_PUBLIC_HOST              Hostname to print in the json:// URL (default: host.docker.internal)
USAGE
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --replace-port-owner|--replace) REPLACE_PORT_OWNER=1 ;;
    --no-replace-port-owner|--no-replace) REPLACE_PORT_OWNER=0 ;;
    --foreground) FOREGROUND=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

webhook_listener_running() {
  pgrep -f "[s]rc/webhook/010-webhook-listener.py" >/dev/null 2>&1
}

port_available() {
  python3 - "$HOST" "$PORT" <<'PY'
import socket
import sys
host, port = sys.argv[1], int(sys.argv[2])
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host if host != "0.0.0.0" else "", port))
    except OSError:
        raise SystemExit(1)
PY
}

port_owner_pids() {
  if command -v ss >/dev/null 2>&1; then
    ss -ltnp "sport = :$PORT" 2>/dev/null | sed -n 's/.*pid=\([0-9][0-9]*\).*/\1/p' | sort -u
    return 0
  fi
  if command -v lsof >/dev/null 2>&1; then
    lsof -nP -t -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | sort -u
    return 0
  fi
  python3 - "$PORT" <<'PY'
from pathlib import Path
import os
import sys

port_hex = f"{int(sys.argv[1]):04X}"
inodes = set()
for table in ("/proc/net/tcp", "/proc/net/tcp6"):
    try:
        lines = Path(table).read_text().splitlines()[1:]
    except OSError:
        continue
    for line in lines:
        parts = line.split()
        if len(parts) >= 10 and parts[3] == "0A" and parts[1].rsplit(":", 1)[-1].upper() == port_hex:
            inodes.add(parts[9])

pids = set()
for proc in Path("/proc").iterdir():
    if not proc.name.isdigit():
        continue
    try:
        for fd in (proc / "fd").iterdir():
            try:
                target = os.readlink(fd)
            except OSError:
                continue
            if target.startswith("socket:[") and target[8:-1] in inodes:
                pids.add(proc.name)
                break
    except OSError:
        continue

for pid in sorted(pids, key=int):
    print(pid)
PY
}

print_port_owner_hint() {
  echo "Port $PORT is already in use, but no local src/webhook/010-webhook-listener.py process was detected." >&2
  echo "This often means an old panamacompra-webhook-receiver service/container is still bound to the port." >&2
  if command -v ss >/dev/null 2>&1; then
    ss -ltnp "sport = :$PORT" >&2 || true
  elif command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >&2 || true
  fi
  echo "Run '$0 --replace-port-owner' to stop the process on this port and start the current listener, or set PC_WEBHOOK_PORT to a free port." >&2
}

print_notification_urls() {
  local token public_host query
  token="$(tr -d '\n\r' < "$TOKEN_FILE")"
  public_host="${PC_WEBHOOK_PUBLIC_HOST:-host.docker.internal}"
  query="method=POST&format=text&overflow=truncate&rto=15&cto=10"

  echo ""
  echo "Changedetection notification URL for this host listener:"
  echo "json://${public_host}:${PORT}/panamacompra/${token}?${query}"
  echo ""
  echo "If changedetection and the compose webhook service run in the same docker-compose network, use this instead:"
  echo "json://webhook:8765/panamacompra/${token}?${query}"
}

replace_port_owner() {
  local pids
  pids="$(port_owner_pids || true)"
  if [ -z "$pids" ]; then
    echo "No owning PID could be parsed for port $PORT." >&2
    return 1
  fi

  echo "Replacing process(es) currently listening on port $PORT:" >&2
  for pid in $pids; do
    ps -p "$pid" -o pid=,ppid=,comm=,args= >&2 || true
  done

  for pid in $pids; do
    kill "$pid" 2>/dev/null || true
  done
  sleep 2

  if port_available; then
    echo "Port $PORT is now free." >&2
    return 0
  fi

  echo "Port $PORT is still busy after SIGTERM; sending SIGKILL to the same PID(s)." >&2
  for pid in $pids; do
    kill -9 "$pid" 2>/dev/null || true
  done
  sleep 1
}

if [ ! -s "$TOKEN_FILE" ]; then
  echo "ERROR: $TOKEN_FILE is missing or empty. Create it with: printf 'YOUR_SECRET_TOKEN' > $TOKEN_FILE" >&2
  exit 1
fi

if webhook_listener_running; then
  echo "Webhook listener is already running."
  pgrep -af "[s]rc/webhook/010-webhook-listener.py" || true
  print_notification_urls
  exit 0
fi

if ! port_available; then
  if [ "$REPLACE_PORT_OWNER" = "1" ]; then
    replace_port_owner || true
  fi
  if ! port_available; then
    print_port_owner_hint
    exit 1
  fi
fi

PYTHON_BIN="python3"
if [ -x "$APP_ROOT/.venv/bin/python" ]; then
  PYTHON_BIN="$APP_ROOT/.venv/bin/python"
fi
LISTENER="$APP_ROOT/src/webhook/010-webhook-listener.py"

if [ "$FOREGROUND" = "1" ]; then
  echo "Webhook listener starting in foreground on $HOST:$PORT."
  print_notification_urls
  exec env PC_WEBHOOK_HOST="$HOST" PC_WEBHOOK_PORT="$PORT" "$PYTHON_BIN" "$LISTENER"
fi

nohup env PC_WEBHOOK_HOST="$HOST" PC_WEBHOOK_PORT="$PORT" \
  "$PYTHON_BIN" "$LISTENER" >> "$LOG_FILE" 2>&1 &

sleep 1
if webhook_listener_running; then
  echo "Webhook listener started on $HOST:$PORT. Log: $LOG_FILE"
  pgrep -af "[s]rc/webhook/010-webhook-listener.py" || true
  print_notification_urls
  exit 0
fi

echo "ERROR: webhook listener did not stay running. Last log lines:" >&2
tail -40 "$LOG_FILE" >&2 2>/dev/null || true
exit 1
