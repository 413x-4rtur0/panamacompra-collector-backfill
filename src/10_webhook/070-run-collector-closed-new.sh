#!/usr/bin/env bash
set -uo pipefail

# Priority 2: catches newly-closed opportunities (the Closed equivalent of
# the main Abiertas/Programadas trigger), fired by its own changedetection.io
# watch + webhook token — completely separate from 060-run-collector.sh so a
# problem here can never touch the priority-1 pipeline. No WhatsApp: this
# downloads the closed opportunity's full detail archive (folder rename,
# tables, calendar — same per-record processing 030-collect-details.py does
# for Abiertas/Programadas, see 037b's own docstring for why that's a
# separate script) and its cuadro-de-cotizaciones price/provider data, then
# inserts everything into the database (037 forward mode + 037b + 038).

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
# shellcheck source=../../lib/env.sh
source "$SCRIPT_DIR/../../lib/env.sh"
cd "$APP_ROOT"

PYTHON_BIN="${PYTHON_BIN:-python3}"
if [ -f ".venv/bin/activate" ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
  PYTHON_BIN="python"
fi

MONITOR_SETTINGS="$PC_DATA_DIR/config/monitor_settings.env"
if [ -f "$MONITOR_SETTINGS" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$MONITOR_SETTINGS"
  set +a
fi

# Defers to priority 1 (the main worker) so the two never compete for the
# browser/CPU at the same time — this just skips the run cleanly; the next
# webhook trigger picks it back up, and no Closed data is lost by skipping.
MAIN_LOCK_FILE="/tmp/panamacompra_run_all_worker.lock"
LOCK_FILE="/tmp/panamacompra_closed_new_worker.lock"
LOG="$PC_LOG_DIR/closed_new_triggered.log"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') | $1" >> "$LOG"; }

if ! flock -n "$MAIN_LOCK_FILE" true 2>/dev/null; then
  log "Skipped: priority 1 (main Abiertas/Programadas worker) is running."
  exit 0
fi

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  log "Skipped: another Closed new-closures run is already in progress."
  exit 0
fi

log "===== Closed new-closures run started ====="
PC_CLOSED_MODE=forward "$PYTHON_BIN" "$APP_ROOT/src/20_pipeline/037-collect-closed-index.py" >> "$LOG" 2>&1
"$PYTHON_BIN" "$APP_ROOT/src/20_pipeline/037b-collect-closed-details.py" >> "$LOG" 2>&1
"$PYTHON_BIN" "$APP_ROOT/src/20_pipeline/038-collect-cotizaciones.py" >> "$LOG" 2>&1
log "===== Closed new-closures run finished ====="
