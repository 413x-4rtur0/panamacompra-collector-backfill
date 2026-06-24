#!/usr/bin/env bash
set -uo pipefail

cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" || exit 1

PYTHON_BIN="${PYTHON_BIN:-python3}"
if [ -f ".venv/bin/activate" ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
  PYTHON_BIN="python"
fi

mkdir -p data/logs data/queue

LOCK_FILE="/tmp/panamacompra_run_all_worker.lock"
REQUEST_FLAG="data/queue/run_all_requested.flag"
IN_PROGRESS_FLAG="data/queue/run_all_in_progress.flag"
WORKER_LOG="data/logs/run_all_worker.log"
CURRENT_LOG="data/logs/run_all_current.log"
HISTORY_LOG="data/logs/run_all_history.log"
PROGRESS_FILE="data/logs/run_all_progress.env"

DETAIL_LIMIT="${1:-99}"
INDEX_LIMIT="${2:-${PC_INDEX_LIMIT:-${PC_MAX_PAGES_PER_GROUP:-20}}}"
RUN_COMPLETED=0

log() {
  echo "$(date '+%Y-%m-%d %H:%M:%S') | $*" | tee -a "$WORKER_LOG"
}

notify_waha() {
  local event="$1"
  local status="$2"
  local message="$3"
  if [ -x ./pc_waha_notify.py ]; then
    "$PYTHON_BIN" ./pc_waha_notify.py --event "$event" --status "$status" --message "$message" >> "$WORKER_LOG" 2>&1 || true
  fi
}

# Send rich WhatsApp messages after detail and calendar processing. This keeps
# downloads free of mid-stream notification side effects and lets the monitor
# show each outbound message in the dedicated MESSAGING step.
notify_new_records() {
  if [ -x ./pc_notify_new_records.py ]; then
    "$PYTHON_BIN" ./pc_notify_new_records.py "$@" >> "$WORKER_LOG" 2>&1 || true
  fi
}

quote_value() {
  printf "%s" "$1" | sed "s/'/'\\\\''/g"
}

write_progress() {
  local phase="$1"
  local status="$2"
  local percent="$3"
  local message="$4"
  local started_at="${5:-}"
  local tmp="${PROGRESS_FILE}.tmp"

  {
    echo "PHASE='$(quote_value "$phase")'"
    echo "STATUS='$(quote_value "$status")'"
    echo "PERCENT='$(quote_value "$percent")'"
    echo "MESSAGE='$(quote_value "$message")'"
    echo "INDEX_LIMIT='$(quote_value "$INDEX_LIMIT")'"
    echo "DETAIL_LIMIT='$(quote_value "$DETAIL_LIMIT")'"
    echo "STARTED_AT='$(quote_value "$started_at")'"
    echo "UPDATED_AT='$(date '+%Y-%m-%d %H:%M:%S')'"
    echo "WORKER_PID='$$'"
    echo "MODE='$(quote_value "${PC_RUN_MODE:-RESTART}")'"
    echo "STEP_CURRENT='-'"
    echo "STEP_TOTAL='-'"
    echo "ITEM_CURRENT='-'"
    echo "ITEM_TOTAL='-'"
    echo "RECORDS_FOUND='-'"
    echo "RECORDS_NEW='-'"
    echo "RECORDS_EXISTING='-'"
    echo "RECORDS_SAVED='-'"
    echo "RECORDS_FAILED='-'"
    echo "RECORDS_PENDING='-'"
    echo "RECORDS_TEST='-'"
    echo "EXTRA='-'"
  } > "$tmp"

  mv "$tmp" "$PROGRESS_FILE"
}


mark_abrupt_exit_for_resume() {
  local exit_code="$?"
  if [ "$RUN_COMPLETED" -eq 0 ]; then
    touch "$REQUEST_FLAG"
    rm -f "$IN_PROGRESS_FLAG"
    log "Worker exited before clean completion with exit=$exit_code. Request flag restored so the next start resumes pending work."
    notify_waha "resume" "FAILED" "Worker stopped before clean completion. Pending work will resume on the next run-all start."
    write_progress "RESUME_PENDING" "FAILED" "0" "Worker stopped before clean completion. Pending work will resume on the next run-all start." "$(date '+%Y-%m-%d %H:%M:%S')" || true
  else
    rm -f "$IN_PROGRESS_FLAG"
  fi
}
terminate_worker() {
  local signal="$1"
  log "Worker received $signal. Exiting and marking pending work for resume."
  exit 128
}
trap mark_abrupt_exit_for_resume EXIT
trap 'terminate_worker INT' INT
trap 'terminate_worker TERM' TERM
trap 'terminate_worker HUP' HUP

exec 9>"$LOCK_FILE"

if ! flock -n 9; then
  RUN_COMPLETED=1
  log "Worker already running. This duplicate worker exits."
  exit 0
fi

write_progress "STARTING" "RUNNING" "2" "Starting run-all worker..." "$(date '+%Y-%m-%d %H:%M:%S')"
touch "$IN_PROGRESS_FLAG"
log "RUN-ALL WORKER STARTED index_limit=$INDEX_LIMIT detail_limit=$DETAIL_LIMIT mode=${PC_RUN_MODE:-RESTART}"

ITERATION=0

while true; do
  if [ ! -f "$REQUEST_FLAG" ]; then
    sleep 3
    if [ ! -f "$REQUEST_FLAG" ]; then
      write_progress "IDLE" "DONE" "100" "No pending request. Worker finished." "$(date '+%Y-%m-%d %H:%M:%S')"
      log "No pending run-all request. Worker finished."
      break
    fi
  fi

  rm -f "$REQUEST_FLAG"
  ITERATION=$((ITERATION + 1))

  if [ "${PC_RUN_UPDATE_BEFORE_RUN:-1}" != "0" ] && [ -x ./pc_update_before_run.sh ]; then
    write_progress "UPDATE" "RUNNING" "3" "Updating local copy before run-all iteration $ITERATION..." "$(date '+%Y-%m-%d %H:%M:%S')"
    log "ITERATION $ITERATION pre-run local update started."
    ./pc_update_before_run.sh >> "$WORKER_LOG" 2>&1
    UPDATE_EXIT=$?
    if [ "$UPDATE_EXIT" -ne 0 ]; then
      # A failed pre-run update must NOT stop the collector. Previously the worker
      # skipped the whole iteration here, so any update hiccup (e.g. local
      # untracked files or no network) made the worker "do nothing". Warn and keep
      # going with the code already on disk so the run still collects data.
      write_progress "UPDATE" "RUNNING" "5" "Pre-run update failed with exit=$UPDATE_EXIT; continuing this run with the current local code." "$(date '+%Y-%m-%d %H:%M:%S')"
      log "ITERATION $ITERATION pre-run local update failed with exit=$UPDATE_EXIT; continuing with current code."
    else
      log "ITERATION $ITERATION pre-run local update completed."
    fi
  fi

  STARTED="$(date '+%Y-%m-%d %H:%M:%S')"
  export PC_RUN_STARTED_AT="$STARTED"
  export PC_WORKER_PID="$$"
  export PC_INDEX_LIMIT="$INDEX_LIMIT"
  export PC_MAX_PAGES_PER_GROUP="$INDEX_LIMIT"
  export PC_DETAIL_LIMIT="$DETAIL_LIMIT"
  {
    echo "============================================================"
    echo "RUN-ALL ITERATION $ITERATION STARTED: $STARTED"
    echo "INDEX_LIMIT: $INDEX_LIMIT"
    echo "DETAIL_LIMIT: $DETAIL_LIMIT"
    echo "PID: $$"
    echo "============================================================"
  } > "$CURRENT_LOG"

  log "ITERATION $ITERATION started."
  write_progress "INDEX" "RUNNING" "10" "Step 1/5: opening PanamaCompra and collecting Programadas + Abiertas tables, index_limit=$INDEX_LIMIT..." "$STARTED"

  {
    echo ""
    echo "-------------------- STEP 1: INDEX COLLECTOR --------------------"
    echo "Started: $(date '+%Y-%m-%d %H:%M:%S')"
    echo "Command: PC_INDEX_LIMIT=$INDEX_LIMIT timeout 1h ${PYTHON_BIN} -u ./pc_index_collector.py"
  } >> "$CURRENT_LOG"

  PC_INDEX_LIMIT="$INDEX_LIMIT" PC_MAX_PAGES_PER_GROUP="$INDEX_LIMIT" timeout 1h "$PYTHON_BIN" -u ./pc_index_collector.py >> "$CURRENT_LOG" 2>&1
  INDEX_EXIT=$?

  {
    echo ""
    echo "Index exit code: $INDEX_EXIT"
    echo "Finished: $(date '+%Y-%m-%d %H:%M:%S')"
  } >> "$CURRENT_LOG"

  if [ "$INDEX_EXIT" -ne 0 ]; then
    write_progress "INDEX" "FAILED" "50" "Index collector failed. Detail step skipped." "$STARTED"
    log "ITERATION $ITERATION index failed with exit=$INDEX_EXIT. Detail skipped."
    notify_waha "failed" "FAILED" "Iteration $ITERATION index failed with exit=$INDEX_EXIT. Detail step skipped."

    {
      echo ""
      echo "INDEX FAILED. DETAIL STEP SKIPPED."
      echo "RUN-ALL ITERATION $ITERATION FINISHED WITH ERROR."
    } >> "$CURRENT_LOG"

    cat "$CURRENT_LOG" >> "$HISTORY_LOG"
    continue
  fi

  # Records still needing detail right after the index step. Used to decide
  # whether this run is "idle" (no new records) and should run the test zone.
  PENDING_BEFORE="$("$PYTHON_BIN" - <<'PY'
from pc_common import init_db
print(init_db().execute("SELECT COUNT(*) FROM opportunities WHERE detail_status != 'saved'").fetchone()[0])
PY
)"
  [ -n "$PENDING_BEFORE" ] || PENDING_BEFORE="-1"

  write_progress "DETAIL" "RUNNING" "55" "Step 2/5: downloading pending detail pages, limit=$DETAIL_LIMIT..." "$STARTED"

  {
    echo ""
    echo "-------------------- STEP 2: DETAIL DOWNLOADER ------------------"
    echo "Started: $(date '+%Y-%m-%d %H:%M:%S')"
    echo "Command: PC_DETAIL_LIMIT=$DETAIL_LIMIT timeout 8h ${PYTHON_BIN} -u ./pc_detail_downloader.py"
  } >> "$CURRENT_LOG"

  PC_DETAIL_LIMIT="$DETAIL_LIMIT" timeout 8h "$PYTHON_BIN" -u ./pc_detail_downloader.py >> "$CURRENT_LOG" 2>&1
  DETAIL_EXIT=$?

  {
    echo ""
    echo "Detail exit code: $DETAIL_EXIT"
    echo "Finished: $(date '+%Y-%m-%d %H:%M:%S')"
  } >> "$CURRENT_LOG"

  # STEP 3: build timestamped Thunderbird/ICS import packages from new events.
  write_progress "CALENDAR" "RUNNING" "94" "Step 3/5: building timestamped calendar import packages (.ics)..." "$STARTED"
  {
    echo ""
    echo "-------------------- STEP 3: CALENDAR PACKAGES -----------------"
    echo "Started: $(date '+%Y-%m-%d %H:%M:%S')"
    echo "Command: ${PYTHON_BIN} -u ./pc_build_calendar.py"
  } >> "$CURRENT_LOG"

  "$PYTHON_BIN" -u ./pc_build_calendar.py >> "$CURRENT_LOG" 2>&1
  CALENDAR_EXIT=$?

  {
    echo "Calendar exit code: $CALENDAR_EXIT"
    echo "Finished: $(date '+%Y-%m-%d %H:%M:%S')"
    echo ""
    echo "============================================================"
    echo "RUN-ALL ITERATION $ITERATION FINISHED: $(date '+%Y-%m-%d %H:%M:%S')"
    echo "INDEX_EXIT=$INDEX_EXIT"
    echo "DETAIL_EXIT=$DETAIL_EXIT"
    echo "CALENDAR_EXIT=$CALENDAR_EXIT"
    echo "============================================================"
  } >> "$CURRENT_LOG"

  cat "$CURRENT_LOG" >> "$HISTORY_LOG"

  if [ "$DETAIL_EXIT" -eq 0 ] && [ "$CALENDAR_EXIT" -eq 0 ]; then
    write_progress "DONE" "DONE" "100" "Index, detail and calendar packages completed successfully." "$STARTED"
    log "ITERATION $ITERATION finished successfully."
  elif [ "$DETAIL_EXIT" -eq 0 ] && [ "$CALENDAR_EXIT" -ne 0 ]; then
    write_progress "CALENDAR" "FAILED" "98" "Detail finished but calendar package build failed with exit=$CALENDAR_EXIT." "$STARTED"
    notify_waha "failed" "FAILED" "Iteration $ITERATION detail finished but calendar build failed with exit=$CALENDAR_EXIT."
    log "ITERATION $ITERATION calendar step failed with exit=$CALENDAR_EXIT."
  elif [ "$DETAIL_EXIT" -eq 124 ]; then
    write_progress "DETAIL" "TIMEOUT" "90" "Detail downloader timed out." "$STARTED"
    notify_waha "timeout" "TIMEOUT" "Iteration $ITERATION detail downloader timed out."
    log "ITERATION $ITERATION detail step timed out."
  else
    write_progress "DETAIL" "FAILED" "90" "Detail downloader failed with exit=$DETAIL_EXIT." "$STARTED"
    notify_waha "failed" "FAILED" "Iteration $ITERATION detail downloader failed with exit=$DETAIL_EXIT."
    log "ITERATION $ITERATION detail failed with exit=$DETAIL_EXIT."
  fi

  # STEP 4: MESSAGING — send the rich WhatsApp messages one by one. This is a
  # visible step: pc_notify_new_records.py --announce publishes per-message
  # progress (current/total + a preview), so the monitor shows each message going
  # out. It announces new opportunities AND status changes (e.g. Programada →
  # Abierta), or sends the single "Sin nuevas entradas" status when there is
  # nothing to send.
  if [ "$DETAIL_EXIT" -eq 0 ]; then
    write_progress "MESSAGING" "RUNNING" "96" "Step 4/5: sending WhatsApp messages (new opportunities + status changes)..." "$STARTED"
    {
      echo ""
      echo "-------------------- STEP 4: WHATSAPP MESSAGING ----------------"
      echo "Started: $(date '+%Y-%m-%d %H:%M:%S')"
    } >> "$CURRENT_LOG"
    notify_new_records --announce
    FINISHED="$(date '+%Y-%m-%d %H:%M:%S')"
    SUMMARY_COUNTS="$($PYTHON_BIN - <<'PY'
from pc_common import init_db
conn = init_db()
row = conn.execute(
    "SELECT COUNT(*) total, "
    "COALESCE(SUM(CASE WHEN detail_status = 'saved' THEN 1 ELSE 0 END), 0) saved, "
    "COALESCE(SUM(CASE WHEN detail_status = 'failed' THEN 1 ELSE 0 END), 0) failed, "
    "COALESCE(SUM(CASE WHEN detail_status != 'saved' THEN 1 ELSE 0 END), 0) pending, "
    "COALESCE(SUM(CASE WHEN notified_at IS NOT NULL THEN 1 ELSE 0 END), 0) notified, "
    "COALESCE(SUM(CASE WHEN last_calendar_export_path IS NOT NULL THEN 1 ELSE 0 END), 0) calendar_exports "
    "FROM opportunities"
).fetchone()
print(
    f"Total registros: {row['total']}\n"
    f"Detalles guardados: {row['saved']}\n"
    f"Fallidos: {row['failed']}\n"
    f"Pendientes: {row['pending']}\n"
    f"Notificados: {row['notified']}\n"
    f"Archivos .ics por registro: {row['calendar_exports']}"
)
PY
)"
    notify_waha "done" "DONE" "📊 Resumen de Ejecución - Panama Compra
Inicio: $STARTED
Fin: $FINISHED
Iteración: $ITERATION
$SUMMARY_COUNTS"
    {
      echo "Finished: $FINISHED"
    } >> "$CURRENT_LOG"
  fi

  # STEP 5: OPTIONAL test zone. When this run had no new records to process, it
  # can exercise the current code on the last N records in an isolated sandbox
  # (records_test/) so a "nothing new" run still verifies code changes. This is
  # OFF by default — the autostart no longer launches the test zone on its own.
  # Opt in with PC_TEST_ZONE_AUTORUN=1 (it stays available as a manual action in
  # the monitor regardless). PC_TEST_ZONE_LIMIT still controls how many records.
  TEST_AUTORUN="${PC_TEST_ZONE_AUTORUN:-0}"
  TEST_LIMIT="${PC_TEST_ZONE_LIMIT:-5}"
  if [ "$TEST_AUTORUN" = "1" ] && printf '%s' "$TEST_LIMIT" | grep -qE '^[0-9]+$' && [ "$TEST_LIMIT" -gt 0 ] && [ "$PENDING_BEFORE" = "0" ]; then
    log "ITERATION $ITERATION had no new records — running test zone on the last $TEST_LIMIT."
    {
      echo ""
      echo "----------------- STEP 5: TEST ZONE (idle, last $TEST_LIMIT) ----"
      echo "Started: $(date '+%Y-%m-%d %H:%M:%S')"
      echo "Command: ${PYTHON_BIN} -u ./pc_test_zone.py --limit $TEST_LIMIT --apply"
    } >> "$CURRENT_LOG"
    "$PYTHON_BIN" -u ./pc_test_zone.py --limit "$TEST_LIMIT" --apply >> "$CURRENT_LOG" 2>&1
    TEST_EXIT=$?
    {
      echo "Test zone exit code: $TEST_EXIT"
      echo "Finished: $(date '+%Y-%m-%d %H:%M:%S')"
    } >> "$CURRENT_LOG"
    if [ "$TEST_EXIT" -ne 0 ]; then
      write_progress "TEST" "FAILED" "100" "Test zone failed with exit=$TEST_EXIT (real archive untouched)." "$STARTED"
      notify_waha "failed" "FAILED" "Iteration $ITERATION test zone failed with exit=$TEST_EXIT (real archive untouched)."
      log "ITERATION $ITERATION test zone failed with exit=$TEST_EXIT."
    fi
  fi

  if [ -f "$REQUEST_FLAG" ]; then
    write_progress "REPEAT" "RUNNING" "5" "Another request arrived while running. Repeating full sequence..." "$(date '+%Y-%m-%d %H:%M:%S')"
    log "Another request was received while running. Repeating full sequence."
  fi
done

RUN_COMPLETED=1
rm -f "$IN_PROGRESS_FLAG"
write_progress "IDLE" "DONE" "100" "Worker stopped. No active PanamaCompra process." "$(date '+%Y-%m-%d %H:%M:%S')"
log "RUN-ALL WORKER STOPPED"
