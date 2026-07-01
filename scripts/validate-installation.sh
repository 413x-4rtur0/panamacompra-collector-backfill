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

require_file() {
  [[ -e "$1" ]] || { echo "missing required path: $1" >&2; exit 1; }
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || { echo "missing command: $1" >&2; exit 1; }
}

require_cmd python3
require_cmd bash
require_cmd find
require_cmd sed
require_cmd date
require_file requirements.txt
require_file src/pipeline/010-collect-index.py
require_file src/pipeline/collect_detail.py
require_file src/pipeline/run-worker.sh
require_file docker-compose.yml

# lib/env.sh already created $PC_DATA_DIR/config, $PC_RECORDS_DIR, $PC_RECORDS_TEST_DIR, etc.
echo "runtime directories are present"

python3 -m compileall -q src/common.py src/monitor/001b-monitor-web.py src/monitor/001a-monitor-tk.py src/monitor/next-run-timer.py src/webhook/listener.py

if [[ "$SKIP_BROWSER" != "1" ]]; then
  python3 - <<'PY'
from importlib.util import find_spec
raise SystemExit(0 if find_spec('playwright') else 'playwright is not installed; run ./setup.sh')
PY
  python3 -m playwright install --dry-run firefox >/dev/null
fi

echo "PanamaCompra Collector installation validation passed."
