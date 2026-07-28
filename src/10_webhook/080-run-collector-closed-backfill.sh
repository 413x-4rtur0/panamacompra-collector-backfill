#!/usr/bin/env bash
set -uo pipefail

# Priority 3: consume a changedetection snapshot from the dedicated historical
# Closed/Cancelled backfill watch, then drain full details and cotizaciones.
# This runner is separate from both novelty collection and the recurring 039
# browser backfill. Shared locks make a webhook snapshot safe while either
# background job is using the browser/database.

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

MAIN_LOCK_FILE="/tmp/panamacompra_run_all_worker.lock"
CLOSED_NEW_LOCK_FILE="/tmp/panamacompra_closed_new_worker.lock"
BACKFILL_LOCK_FILE="/tmp/panamacompra_closed_backfill_worker.lock"
LOG="$PC_LOG_DIR/closed_backfill_snapshot_triggered.log"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') | $1" >> "$LOG"; }

if ! flock -n "$MAIN_LOCK_FILE" true 2>/dev/null; then
  log "Skipped: priority 1 (main Abiertas/Programadas worker) is running."
  exit 0
fi
if ! flock -n "$CLOSED_NEW_LOCK_FILE" true 2>/dev/null; then
  log "Skipped: priority 2 (Closed new-closures) is running."
  exit 0
fi

exec 9>"$BACKFILL_LOCK_FILE"
if ! flock -n 9; then
  log "Skipped: recurring Closed backfill or another snapshot run is already in progress."
  exit 0
fi

log "===== Closed backfill snapshot run started ====="
"$PYTHON_BIN" "$APP_ROOT/src/20_pipeline/016-import-closed-backfill-snapshot.py" >> "$LOG" 2>&1
IMPORT_STATUS=$?
if [ "$IMPORT_STATUS" -eq 4 ]; then
  log "No usable Closed backfill snapshot; details/quotes drain skipped."
  exit 0
fi

"$PYTHON_BIN" "$APP_ROOT/src/20_pipeline/037b-collect-closed-details.py" >> "$LOG" 2>&1
"$PYTHON_BIN" "$APP_ROOT/src/20_pipeline/038-collect-cotizaciones.py" >> "$LOG" 2>&1
log "===== Closed backfill snapshot run finished (import_status=$IMPORT_STATUS) ====="
