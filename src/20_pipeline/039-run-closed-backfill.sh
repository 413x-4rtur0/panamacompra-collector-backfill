#!/usr/bin/env bash
set -uo pipefail

# Priority 3 (lowest): the historical Closed-opportunities backfill, run in
# explicit weekly date windows by its own systemd --user timer, not by
# changedetection — old closures do not "change", so a snapshot diff has
# nothing to trigger on. The index phase must complete every weekly window
# before enrichment is allowed to start.

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
capture_device_archive_paths
load_settings() {
  if [ -f "$MONITOR_SETTINGS" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$MONITOR_SETTINGS"
    set +a
  fi
}
load_settings
restore_device_archive_paths

load_range_settings() {
  BACKFILL_START_DATE="${PC_CLOSED_BACKFILL_START_DATE:-}"
  BACKFILL_END_DATE="${PC_CLOSED_BACKFILL_END_DATE:-}"
  WEEK_DAYS="${PC_CLOSED_BACKFILL_WEEK_DAYS:-7}"
}
load_range_settings

# Persist the next weekly window so a service restart resumes after the last
# verified window instead of replaying the configured range from its first
# day. The identity includes the configured range and window size, so changing
# the task range automatically starts a fresh cursor.
WINDOW_STATE_FILE="${PC_STATE_DIR:-$APP_ROOT/var}/closed-backfill-window.state"
WINDOW_STATE_ID="${BACKFILL_START_DATE}|${BACKFILL_END_DATE}|${WEEK_DAYS}"
load_window_resume() {
  local saved_start saved_end saved_days next identity
  if [ ! -f "$WINDOW_STATE_FILE" ]; then
    return 0
  fi
  IFS='|' read -r saved_start saved_end saved_days next < "$WINDOW_STATE_FILE" || return 0
  identity="${saved_start}|${saved_end}|${saved_days}"
  if [ "$identity" = "$WINDOW_STATE_ID" ] && date -d "$next" +%F >/dev/null 2>&1; then
    BACKFILL_START_DATE="$next"
    log "Resuming from persisted weekly window: $BACKFILL_START_DATE to $BACKFILL_END_DATE."
  fi
}
save_window_resume() {
  local next_start="$1"
  mkdir -p "$(dirname "$WINDOW_STATE_FILE")"
  printf '%s|%s\n' "$WINDOW_STATE_ID" "$next_start" > "$WINDOW_STATE_FILE"
}
# Defers to BOTH priority 1 (main worker) and priority 2 (Closed
# new-closures) — a backfill segment never competes with either for the
# browser/CPU. Skipping loses nothing: the closed_crawl_state cursor picks
# up at the same page next tick.
MAIN_LOCK_FILE="/tmp/panamacompra_run_all_worker.lock"
CLOSED_NEW_LOCK_FILE="/tmp/panamacompra_closed_new_worker.lock"
LOCK_FILE="/tmp/panamacompra_closed_backfill_worker.lock"
LOG="$PC_LOG_DIR/closed_backfill_triggered.log"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') | $1" >> "$LOG"; }
load_window_resume

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

priority_available() {
  if priority_1_busy; then
    log "Yielding: priority 1 (main worker) became active."
    return 1
  fi
  if priority_2_busy; then
    log "Yielding: priority 2 (Closed new-closures) became active."
    return 1
  fi
  return 0
}

reset_cursor() {
  "$PYTHON_BIN" -c 'import os, sqlite3
db = os.environ.get("PC_ARCHIVE_DB_PATH")
if db:
    conn = sqlite3.connect(db, timeout=30)
    conn.execute("UPDATE closed_crawl_state SET backfill_page=1, backfill_complete=0 WHERE grupo=?", ("Closed",))
    conn.commit()
    conn.close()' 2>/dev/null || true
}

index_phase() {
  local week_start week_end next_start index_out rc
  if [ -z "$BACKFILL_START_DATE" ] || [ -z "$BACKFILL_END_DATE" ]; then
    log "Weekly index phase requires PC_CLOSED_BACKFILL_START_DATE and END_DATE."
    return 1
  fi
  if ! [[ "$WEEK_DAYS" =~ ^[1-9][0-9]*$ ]]; then
    log "Invalid PC_CLOSED_BACKFILL_WEEK_DAYS=$WEEK_DAYS."
    return 1
  fi

  week_start="$BACKFILL_START_DATE"
  while [[ "$week_start" <="$BACKFILL_END_DATE" ]]; do
    if ! priority_available; then
      return 1
    fi
    week_end=$(date -d "$week_start + $((WEEK_DAYS - 1)) days" +%F) || return 1
    if [[ "$week_end" > "$BACKFILL_END_DATE" ]]; then
      week_end="$BACKFILL_END_DATE"
    fi

    reset_cursor
    log "Weekly index window started: $week_start to $week_end."
    index_out=$(timeout --foreground --kill-after=60s "${PC_CLOSED_BACKFILL_WINDOW_TIMEOUT_SECONDS:-2700}s" \
      env PC_CLOSED_MODE=backfill \
      PC_CLOSED_GROUP=Closed \
      PC_CLOSED_BACKFILL_PAGES="${PC_CLOSED_BACKFILL_PAGES:-999999}" \
      PC_CLOSED_BACKFILL_START_DATE="$week_start" \
      PC_CLOSED_BACKFILL_END_DATE="$week_end" \
      PYTHONUNBUFFERED=1 \
      "$PYTHON_BIN" "$APP_ROOT/src/20_pipeline/037-collect-closed-index.py" 2>&1)
    rc=$?
    echo "$index_out" >> "$LOG"

    if [ "$rc" -eq 124 ]; then
      log "Weekly index window timed out after ${PC_CLOSED_BACKFILL_WINDOW_TIMEOUT_SECONDS:-2700}s: $week_start to $week_end."
      return 1
    fi
    if [ "$rc" -ne 0 ]; then
      log "Weekly index window failed: $week_start to $week_end (rc=$rc)."
      return 1
    fi
    if [[ "$index_out" == *"target month not reached"* ]]; then
      log "Weekly index window was not verified: $week_start to $week_end."
      return 1
    fi
    if [[ "$index_out" != *"Backfill reached"* && "$index_out" != *"Backfill already reached"* ]]; then
      log "Weekly index window ended without a completion marker: $week_start to $week_end."
      return 1
    fi

    log "Weekly index window complete: $week_start to $week_end."
    next_start=$(date -d "$week_end + 1 day" +%F) || return 1
    save_window_resume "$next_start"
    week_start="$next_start"
  done

  rm -f "$WINDOW_STATE_FILE"
  log "All weekly index windows complete; starting detail and cotizacion enrichment."
  return 0
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

log "===== Closed backfill weekly index-first run started (${BACKFILL_START_DATE:-unset} to ${BACKFILL_END_DATE:-unset}) ====="

# Priority 4 is evaluated before the index phase. A queued date-range task
# still waits for the current Closed queues to drain, and will be picked up by
# the next timer invocation after this index-first run completes.
TASK_QUEUE_TICK=$("$PYTHON_BIN" "$APP_ROOT/src/50_tools/145-task-queue.py" tick 2>&1)
log "Task queue tick: $TASK_QUEUE_TICK"
load_settings
load_range_settings

if index_phase; then
  if [ "${PC_CLOSED_BACKFILL_ALLOW_ENRICHMENT:-0}" = "1" ]; then
    enrichment_phase || true
  else
    log "Index phase complete; enrichment held until the device archives are merged into the canonical DB."
  fi
fi

log "===== Closed backfill index-first run finished ====="
