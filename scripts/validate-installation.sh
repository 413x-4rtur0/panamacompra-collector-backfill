#!/usr/bin/env bash
set -euo pipefail

SKIP_BROWSER=0
if [[ "${1:-}" == "--skip-browser" ]]; then
  SKIP_BROWSER=1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/env.sh
source "$SCRIPT_DIR/../lib/env.sh"
cd "$APP_ROOT"

if [[ -n "${PYTHON_CMD:-}" ]]; then
  PYTHON_CMD="$PYTHON_CMD"
elif [[ -x "$APP_ROOT/.venv/bin/python" ]]; then
  PYTHON_CMD="$APP_ROOT/.venv/bin/python"
else
  PYTHON_CMD="python3"
fi

require_file() {
  [[ -e "$1" ]] || { echo "missing required path: $1" >&2; exit 1; }
}

require_executable() {
  [[ -x "$1" ]] || { echo "required script is not executable: $1" >&2; exit 1; }
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || { echo "missing command: $1" >&2; exit 1; }
}

require_cmd "$PYTHON_CMD"
require_cmd bash
require_cmd find
require_cmd sed
require_cmd date
require_cmd tee
require_cmd flock
require_cmd timeout
require_cmd pgrep
require_cmd pkill

"$PYTHON_CMD" - <<'PY'
import sys
if sys.version_info < (3, 10):
    raise SystemExit(f"Python 3.10+ is required; found {sys.version.split()[0]}")
PY

require_file .env.example
require_file config/defaults.env
require_file requirements.txt
require_file docker-compose.yml
require_file docker/Dockerfile.webhook
require_file src/pipeline/010-collect-index.py
require_file src/pipeline/collect_detail.py
require_file src/pipeline/run-worker.sh
require_file src/webhook/listener.py
require_file src/monitor/open-monitor.sh
require_executable setup.sh
require_executable bin/pcc
require_executable src/pipeline/request-run-all.sh
require_executable src/pipeline/run-worker.sh
require_executable src/pipeline/stop-run-all.sh
require_executable src/monitor/open-monitor.sh
require_executable src/webhook/start-listener.sh

# lib/env.sh already created $PC_DATA_DIR/config, $PC_RECORDS_DIR, $PC_RECORDS_TEST_DIR, etc.
for dir in "$PC_DATA_DIR" "$PC_DATA_DIR/config" "$PC_RECORDS_DIR" "$PC_RECORDS_TEST_DIR" "$PC_LOG_DIR" "$PC_RUN_DIR" "$PC_QUEUE_DIR" "$PC_CONFIG_DIR"; do
  [[ -d "$dir" ]] || { echo "runtime directory was not created: $dir" >&2; exit 1; }
done
echo "runtime directories are present"

"$PYTHON_CMD" -m compileall -q src

while IFS= read -r file; do
  bash -n "$file"
done < <(find . -maxdepth 4 -type f -name '*.sh' -not -path './.git/*' -not -path './.venv/*')
bash -n bin/pcc
bash -n lib/env.sh

if [[ "$SKIP_BROWSER" != "1" ]]; then
  "$PYTHON_CMD" - <<'PY'
from importlib.util import find_spec
raise SystemExit(0 if find_spec('playwright') else 'playwright is not installed; run ./setup.sh')
PY
  "$PYTHON_CMD" -m playwright install --dry-run firefox >/dev/null
fi

# Informational (never fails validation): report whether WhatsApp notifications
# are ready to send, since enabling them takes manual steps setup.sh cannot do
# (pairing the WAHA session by QR and choosing the destination group).
MONITOR_SETTINGS="$PC_DATA_DIR/config/monitor_settings.env"
WAHA_ENABLED="${PC_WAHA_ENABLED:-}"
if [[ -z "$WAHA_ENABLED" && -f "$MONITOR_SETTINGS" ]]; then
  WAHA_ENABLED="$(sed -n "s/^PC_WAHA_ENABLED=['\"]\{0,1\}\([^'\"]*\).*/\1/p" "$MONITOR_SETTINGS" | tail -n 1)"
fi
WAHA_CHAT="${PC_WAHA_CHAT_ID:-}"
if [[ -z "$WAHA_CHAT" && -s "$PC_DATA_DIR/config/waha_chat_id.txt" ]]; then
  WAHA_CHAT="$(head -n 1 "$PC_DATA_DIR/config/waha_chat_id.txt")"
fi
case "${WAHA_ENABLED,,}" in
  1|true|yes|on)
    if [[ -n "$WAHA_CHAT" ]]; then
      echo "WhatsApp notifications: enabled, destination chat id configured."
    else
      echo "WhatsApp notifications: enabled but NO destination chat id yet. Set PC_WAHA_CHAT_ID or save it from the monitor Settings panel."
    fi
    ;;
  *)
    echo "WhatsApp notifications: disabled (default). To enable: docker compose up -d waha, pair the session (QR), then set PC_WAHA_ENABLED=1 and the chat id (see .env.example)."
    ;;
esac

echo "PanamaCompra Collector installation validation passed."
