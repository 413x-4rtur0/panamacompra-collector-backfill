#!/usr/bin/env bash
set -uo pipefail

# Priority 3 (lowest): the historical Cerradas backfill, run in bounded
# segments (see backfill_page_cap()/backfill_cutoff_date() in
# 037-collect-cerradas-index.py) by its own systemd --user timer, not by
# changedetection — old closures do not "change", so a snapshot diff has
# nothing to trigger on; the segmented cursor already handles chunking a
# large archive over many runs. No WhatsApp: downloads and inserts each
# record's full detail archive (037b) and cuadro-de-cotizaciones price/
# provider data (038), same as the priority-2 new-closures path (037
# backfill mode + 037b + 038).

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

# Defers to BOTH priority 1 (main worker) and priority 2 (Cerradas
# new-closures) — a backfill segment never competes with either for the
# browser/CPU. Skipping loses nothing: the cerradas_crawl_state cursor picks
# up at the same page next tick.
MAIN_LOCK_FILE="/tmp/panamacompra_run_all_worker.lock"
CERRADAS_NEW_LOCK_FILE="/tmp/panamacompra_cerradas_new_worker.lock"
LOCK_FILE="/tmp/panamacompra_cerradas_backfill_worker.lock"
LOG="$PC_LOG_DIR/cerradas_backfill_triggered.log"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') | $1" >> "$LOG"; }

if ! flock -n "$MAIN_LOCK_FILE" true 2>/dev/null; then
  log "Skipped: priority 1 (main Abiertas/Programadas worker) is running."
  exit 0
fi
if ! flock -n "$CERRADAS_NEW_LOCK_FILE" true 2>/dev/null; then
  log "Skipped: priority 2 (Cerradas new-closures) is running."
  exit 0
fi

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  log "Skipped: another Cerradas backfill run is already in progress."
  exit 0
fi

log "===== Cerradas backfill segment started ====="
PC_CERRADAS_MODE=backfill "$PYTHON_BIN" "$APP_ROOT/src/20_pipeline/037-collect-cerradas-index.py" >> "$LOG" 2>&1
"$PYTHON_BIN" "$APP_ROOT/src/20_pipeline/037b-collect-cerradas-details.py" >> "$LOG" 2>&1
"$PYTHON_BIN" "$APP_ROOT/src/20_pipeline/038-collect-cotizaciones.py" >> "$LOG" 2>&1
log "===== Cerradas backfill segment finished ====="
