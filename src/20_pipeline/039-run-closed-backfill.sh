#!/usr/bin/env bash
set -uo pipefail

# Priority 3 (lowest): the historical Closed-opportunities backfill, run in
# bounded segments (see backfill_page_cap()/backfill_cutoff_date() in
# 037-collect-closed-index.py) by its own systemd --user timer, not by
# changedetection — old closures do not "change", so a snapshot diff has
# nothing to trigger on. No WhatsApp: downloads and inserts each record's
# full detail archive (037b) and cuadro-de-cotizaciones price/provider data
# (038), same as the priority-2 new-closures path (037 backfill mode + 037b
# + 038).
#
# Runs in two explicit phases for up to PC_CLOSED_BACKFILL_MAX_SECONDS
# (default 900s, leaving a buffer before the next 20-minute tick): first it
# crawls/commits all index pagination for the selected historical range; only
# after that index phase is complete does it drain full details (HTML, tables,
# calendar/items) and then cotizaciones (providers, items, prices). This keeps
# discovery ahead of enrichment and prevents detail work from interrupting the
# historical index walk.

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
load_settings() {
  if [ -f "$MONITOR_SETTINGS" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$MONITOR_SETTINGS"
    set +a
  fi
}
load_settings

# Defers to BOTH priority 1 (main worker) and priority 2 (Closed
# new-closures) — a backfill segment never competes with either for the
# browser/CPU. Skipping loses nothing: the closed_crawl_state cursor picks
# up at the same page next tick.
MAIN_LOCK_FILE="/tmp/panamacompra_run_all_worker.lock"
CLOSED_NEW_LOCK_FILE="/tmp/panamacompra_closed_new_worker.lock"
LOCK_FILE="/tmp/panamacompra_closed_backfill_worker.lock"
LOG="$PC_LOG_DIR/closed_backfill_triggered.log"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') | $1" >> "$LOG"; }

priority_1_busy() { ! flock -n "$MAIN_LOCK_FILE" true 2>/dev/null; }
priority_2_busy() { ! flock -n "$CLOSED_NEW_LOCK_FILE" true 2>/dev/null; }

if priority_1_busy; then
  log "Skipped: priority 1 (main Abiertas/Programadas worker) is running."
  exit 0
fi
if priority_2_busy; then
  log "Skipped: priority 2 (Closed new-closures) is running."
  exit 0
fi

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  log "Skipped: another Closed backfill run is already in progress."
  exit 0
fi

MAX_SECONDS="${PC_CLOSED_BACKFILL_MAX_SECONDS:-900}"
START_TS=$(date +%s)

within_time_cap() {
  local now
  now=$(date +%s)
  [ $((now - START_TS)) -lt "$MAX_SECONDS" ]
}

priority_available() {
  if priority_1_busy; then
    log "Yielding: priority 1 (main worker) became active."
    return 1
  fi
  if priority_2_busy; then
    log "Yielding: priority 2 (Closed new-closures) became active."
    return 1
  fi
  if ! within_time_cap; then
    log "Time cap reached; yielding to the next timer tick."
    return 1
  fi
  return 0
}

index_phase() {
  local index_out
  while priority_available; do
    index_out=$(PC_CLOSED_MODE=backfill "$PYTHON_BIN" "$APP_ROOT/src/20_pipeline/037-collect-closed-index.py" 2>&1)
    echo "$index_out" >> "$LOG"

    if [[ "$index_out" == *"Backfill reached"* || "$index_out" == *"Backfill already reached"* ]]; then
      log "Index phase complete; starting detail and cotizacion enrichment."
      return 0
    fi
    if [ -z "$index_out" ]; then
      log "Index phase returned no output; yielding before enrichment."
      return 1
    fi
  done
  return 1
}

enrichment_phase() {
  local detail_out cotiz_out
  while priority_available; do
    detail_out=$(PC_CLOSED_BACKFILL_ONLY_CLOSED=1 "$PYTHON_BIN" "$APP_ROOT/src/20_pipeline/037b-collect-closed-details.py" 2>&1)
    echo "$detail_out" >> "$LOG"
    cotiz_out=$(PC_CLOSED_BACKFILL_ONLY_CLOSED=1 "$PYTHON_BIN" "$APP_ROOT/src/20_pipeline/038-collect-cotizaciones.py" 2>&1)
    echo "$cotiz_out" >> "$LOG"

    if [[ "$detail_out" == *"No Closed records pending a full detail fetch."* ]] \
       && [[ "$cotiz_out" == *"No Closed records pending a cotizacion fetch."* ]]; then
      log "Enrichment phase complete; no pending details or cotizaciones remain."
      return 0
    fi
  done
  return 1
}

log "===== Closed backfill index-first run started (cap ${MAX_SECONDS}s) ====="

# Priority 4 is evaluated before the index phase. A queued date-range task
# still waits for the current Closed queues to drain, and will be picked up by
# the next timer invocation after this index-first run completes.
TASK_QUEUE_TICK=$("$PYTHON_BIN" "$APP_ROOT/src/50_tools/145-task-queue.py" tick 2>&1)
log "Task queue tick: $TASK_QUEUE_TICK"
load_settings

if index_phase; then
  enrichment_phase || true
fi

log "===== Closed backfill index-first run finished ====="
