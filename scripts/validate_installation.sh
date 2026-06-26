#!/usr/bin/env bash
set -euo pipefail

SKIP_BROWSER=0
if [[ "${1:-}" == "--skip-browser" ]]; then
  SKIP_BROWSER=1
fi

cd "$(dirname "${BASH_SOURCE[0]}")/.."

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
require_file pc_index_collector.py
require_file pc_detail_downloader.py
require_file pc_run_all_worker.sh
require_file docker-compose.yml

python3 - <<'PY'
from pathlib import Path
required = [Path('data/logs'), Path('data/queue'), Path('data/config'), Path('records'), Path('records_test')]
for path in required:
    path.mkdir(parents=True, exist_ok=True)
print('runtime directories are present')
PY

python3 -m compileall -q pc_common.py pc_monitor_server.py pc_monitor_tk.py pc_next_run_timer.py webhook_listener.py

if [[ "$SKIP_BROWSER" != "1" ]]; then
  python3 - <<'PY'
from importlib.util import find_spec
raise SystemExit(0 if find_spec('playwright') else 'playwright is not installed; run ./setup.sh')
PY
  python3 -m playwright install --dry-run firefox >/dev/null
fi

echo "PanamaCompra Collector installation validation passed."
