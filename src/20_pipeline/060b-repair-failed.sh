#!/usr/bin/env bash
set -uo pipefail

# Repair mode (F11): re-download every record stuck at detail_status='failed'.
# Triggered manually from the monitor's "Repair" button via
# 125-run-priority.sh (priority 90) -- that layer already serializes against
# update/test-zone/build-calendar/etc (see 125-run-priority.sh's
# external_busy() pgrep list). It does NOT know about the Closed-lane flock
# locks though, so this script defers to them itself, the same
# check-don't-kill way 039-run-closed-backfill.sh already does: a repair pass
# touches the same shared detail/browser resources as priority 1 (main
# worker), priority 2 (Closed new-closures) and priority 3 (Closed backfill),
# and failed records can span every group, so it waits on all three rather
# than assuming 125-run-priority.sh's generic queue already covers them.

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
CLOSED_BACKFILL_LOCK_FILE="/tmp/panamacompra_closed_backfill_worker.lock"
LOCK_FILE="/tmp/panamacompra_repair_worker.lock"
LOG="$PC_LOG_DIR/repair_triggered.log"

REQUESTED_FLAG="$PC_QUEUE_DIR/repair_requested.flag"
IN_PROGRESS_FLAG="$PC_QUEUE_DIR/repair_in_progress.flag"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') | $1" >> "$LOG"; }

if ! flock -n "$MAIN_LOCK_FILE" true 2>/dev/null; then
  log "Skipped: priority 1 (main Abiertas/Programadas worker) is running."
  exit 0
fi
if ! flock -n "$CLOSED_NEW_LOCK_FILE" true 2>/dev/null; then
  log "Skipped: priority 2 (Closed new-closures) is running."
  exit 0
fi
if ! flock -n "$CLOSED_BACKFILL_LOCK_FILE" true 2>/dev/null; then
  log "Skipped: priority 3 (Closed backfill) is running."
  exit 0
fi

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  log "Skipped: another repair run is already in progress."
  exit 0
fi

rm -f "$REQUESTED_FLAG"
touch "$IN_PROGRESS_FLAG"
cleanup() { rm -f "$IN_PROGRESS_FLAG"; }
trap cleanup EXIT

log "===== Repair run started ====="
"$PYTHON_BIN" "$APP_ROOT/src/20_pipeline/060-repair-failed.py" >> "$LOG" 2>&1
log "===== Repair run finished ====="
