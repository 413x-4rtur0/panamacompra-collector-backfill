#!/usr/bin/env bash
set -uo pipefail

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

LOCK_FILE="/tmp/panamacompra_run_all_worker.lock"
REQUEST_FLAG="$PC_QUEUE_DIR/run_all_requested.flag"
IN_PROGRESS_FLAG="$PC_QUEUE_DIR/run_all_in_progress.flag"
STOP_NO_RESUME_FLAG="$PC_QUEUE_DIR/run_all_stop_no_resume.flag"
WORKER_LOG="$PC_LOG_DIR/run_all_worker.log"
CURRENT_LOG="$PC_LOG_DIR/run_all_current.log"
HISTORY_LOG="$PC_LOG_DIR/run_all_history.log"
PROGRESS_FILE="$PC_LOG_DIR/run_all_progress.env"
LAST_SUMMARY_FILE="$PC_LOG_DIR/run_all_last_summary.env"
UPDATE_QUEUE_FLAG="$PC_QUEUE_DIR/update_monitor_requested.flag"
UPDATE_QUEUE_LOG="$PC_LOG_DIR/update_monitor_queue.log"
PIPELINE_DIR="$APP_ROOT/src/pipeline"

DETAIL_LIMIT="${1:-99}"
INDEX_LIMIT="${2:-${PC_INDEX_LIMIT:-${PC_MAX_PAGES_PER_GROUP:-0}}}"
RUN_COMPLETED=0

log() {
  echo "$(date '+%Y-%m-%d %H:%M:%S') | $*" | tee -a "$WORKER_LOG"
}

notify_waha() {
  local event="$1"
  local status="$2"
  local message="$3"
  if [ -x "$APP_ROOT/src/notify/waha_client.py" ]; then
    "$PYTHON_BIN" "$APP_ROOT/src/notify/waha_client.py" --event "$event" --status "$status" --message "$message" >> "$WORKER_LOG" 2>&1 || true
  fi
}

# Send rich WhatsApp messages after detail and calendar processing. This keeps
# downloads free of mid-stream notification side effects and lets the monitor
# show each outbound message in the dedicated MESSAGING step.
launch_queued_update_monitor() {
  if [ ! -f "$UPDATE_QUEUE_FLAG" ]; then
    return 0
  fi
  log "Queued Update + Monitor request found after collector finished; launching updater."
  echo "$(date '+%Y-%m-%d %H:%M:%S') | UPDATE+MONITOR STARTING after collector finished" >> "$UPDATE_QUEUE_LOG"
  rm -f "$UPDATE_QUEUE_FLAG"
  if [ -x "$APP_ROOT/src/monitor/update-loader.py" ]; then
    nohup "$PYTHON_BIN" "$APP_ROOT/src/monitor/update-loader.py" --open-monitor-after >> "$UPDATE_QUEUE_LOG" 2>&1 &
  else
    nohup "$APP_ROOT/update-local-copy.sh" >> "$UPDATE_QUEUE_LOG" 2>&1 &
  fi
}

notify_new_records() {
  if [ -x "$PIPELINE_DIR/notify_new_records.py" ]; then
    "$PYTHON_BIN" "$PIPELINE_DIR/notify_new_records.py" "$@" >> "$WORKER_LOG" 2>&1 || true
  fi
}

quote_value() {
  printf "%s" "$1" | sed "s/'/'\\\\''/g"
}

format_eta() {
  local seconds="$1"
  if [ -z "$seconds" ] || [ "$seconds" -lt 0 ] 2>/dev/null; then
    echo "-"
    return
  fi
  local hours=$((seconds / 3600))
  local minutes=$(((seconds % 3600) / 60))
  local secs=$((seconds % 60))
  if [ "$hours" -gt 0 ]; then
    printf "%dh %02dm" "$hours" "$minutes"
  else
    printf "%dm %02ds" "$minutes" "$secs"
  fi
}

historical_eta_seconds() {
  local percent="$1"
  if [ ! -f "$LAST_SUMMARY_FILE" ] || ! printf '%s' "$percent" | grep -qE '^[0-9]+$'; then
    echo ""
    return
  fi
  # shellcheck disable=SC1090
  . "$LAST_SUMMARY_FILE" 2>/dev/null || true
  local total="${TOTAL_SECONDS:-}"
  if ! printf '%s' "$total" | grep -qE '^[0-9]+$' || [ "$total" -le 0 ]; then
    echo ""
    return
  fi
  echo $((total * (100 - percent) / 100))
}

write_progress() {
  local phase="$1"
  local status="$2"
  local percent="$3"
  local message="$4"
  local started_at="${5:-}"
  local tmp="${PROGRESS_FILE}.tmp"
  local eta="-"
  if [ "$status" = "RUNNING" ] && printf '%s' "$percent" | grep -qE '^[0-9]+$' && [ "$percent" -gt 0 ] && [ "$percent" -lt 100 ] && [ -n "$started_at" ]; then
    historical_eta="$(historical_eta_seconds "$percent")"
    if [ -n "$historical_eta" ]; then
      eta="$(format_eta "$historical_eta") (based on previous run)"
    else
      start_epoch="$(date -d "$started_at" '+%s' 2>/dev/null || true)"
      now_epoch="$(date '+%s')"
      if [ -n "$start_epoch" ] && [ "$now_epoch" -gt "$start_epoch" ]; then
        elapsed=$((now_epoch - start_epoch))
        total_est=$((elapsed * 100 / percent))
        eta="$(format_eta $((total_est - elapsed)))"
      fi
    fi
  fi

  {
    echo "PHASE='$(quote_value "$phase")'"
    echo "STATUS='$(quote_value "$status")'"
    echo "PERCENT='$(quote_value "$percent")'"
    echo "MESSAGE='$(quote_value "$message")'"
    echo "INDEX_LIMIT='$(quote_value "$INDEX_LIMIT")'"
    echo "ETA='$(quote_value "$eta")'"
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
  if [ -f "$STOP_NO_RESUME_FLAG" ]; then
    rm -f "$REQUEST_FLAG" "$IN_PROGRESS_FLAG" "$STOP_NO_RESUME_FLAG"
    log "Worker stopped by updater/manual stop with no-resume marker. Pending/recover request was NOT restored."
    write_progress "STOPPED" "DONE" "100" "Worker stopped intentionally by updater/manual launcher; no pending/recover restart was queued." "$(date '+%Y-%m-%d %H:%M:%S')" || true
    return
  fi
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
log "RUN-ALL WORKER STARTED index_page_cap=$INDEX_LIMIT detail_limit=$DETAIL_LIMIT mode=${PC_RUN_MODE:-RESTART}"

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
  UPDATE_SECONDS=0

  if [ "${PC_RUN_UPDATE_BEFORE_RUN:-1}" != "0" ] && [ -x "$PIPELINE_DIR/000-update-before-run.sh" ]; then
    write_progress "UPDATE" "RUNNING" "3" "Updating local copy before run-all iteration $ITERATION..." "$(date '+%Y-%m-%d %H:%M:%S')"
    log "ITERATION $ITERATION pre-run local update started."
    UPDATE_START_EPOCH="$(date '+%s')"
    "$PIPELINE_DIR/000-update-before-run.sh" >> "$WORKER_LOG" 2>&1
    UPDATE_EXIT=$?
    UPDATE_SECONDS=$(( $(date '+%s') - UPDATE_START_EPOCH ))
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
  RUN_START_EPOCH="$(date '+%s')"
  INDEX_SECONDS=0
  DETAIL_SECONDS=0
  VIEW_SECONDS=0
  CALENDAR_SECONDS=0
  MESSAGING_SECONDS=0
  VERIFY_SECONDS=0
  export PC_RUN_STARTED_AT="$STARTED"
  export PC_WORKER_PID="$$"
  export PC_INDEX_LIMIT="$INDEX_LIMIT"
  export PC_MAX_PAGES_PER_GROUP="$INDEX_LIMIT"
  export PC_DETAIL_LIMIT="$DETAIL_LIMIT"
  {
    echo "============================================================"
    echo "RUN-ALL ITERATION $ITERATION STARTED: $STARTED"
    echo "INDEX_PAGE_CAP: $INDEX_LIMIT (0 = all pages)"
    echo "DETAIL_LIMIT: $DETAIL_LIMIT"
    echo "PID: $$"
    echo "============================================================"
  } > "$CURRENT_LOG"

  log "ITERATION $ITERATION started."
  write_progress "INDEX" "RUNNING" "10" "Step 1/6: opening PanamaCompra and collecting Programadas + Abiertas tables, index_page_cap=$INDEX_LIMIT..." "$STARTED"

  {
    echo ""
    echo "-------------------- STEP 1: INDEX COLLECTOR --------------------"
    echo "Started: $(date '+%Y-%m-%d %H:%M:%S')"
    echo "Command: PC_INDEX_LIMIT=$INDEX_LIMIT timeout 1h ${PYTHON_BIN} -u $PIPELINE_DIR/010-collect-index.py"
  } >> "$CURRENT_LOG"

  INDEX_START_EPOCH="$(date '+%s')"
  PC_INDEX_LIMIT="$INDEX_LIMIT" PC_MAX_PAGES_PER_GROUP="$INDEX_LIMIT" timeout 1h "$PYTHON_BIN" -u "$PIPELINE_DIR/010-collect-index.py" >> "$CURRENT_LOG" 2>&1
  INDEX_EXIT=$?
  INDEX_SECONDS=$(( $(date '+%s') - INDEX_START_EPOCH ))

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
from common import init_db
print(init_db().execute("SELECT COUNT(*) FROM opportunities WHERE detail_status != 'saved'").fetchone()[0])
PY
)"
  [ -n "$PENDING_BEFORE" ] || PENDING_BEFORE="-1"

  write_progress "DETAIL" "RUNNING" "55" "Step 2/6: downloading pending detail pages, limit=$DETAIL_LIMIT..." "$STARTED"

  {
    echo ""
    echo "-------------------- STEP 2: DETAIL DOWNLOADER ------------------"
    echo "Started: $(date '+%Y-%m-%d %H:%M:%S')"
    echo "Command: PC_DETAIL_LIMIT=$DETAIL_LIMIT timeout 8h ${PYTHON_BIN} -u $PIPELINE_DIR/collect_detail.py"
  } >> "$CURRENT_LOG"

  DETAIL_START_EPOCH="$(date '+%s')"
  PC_DETAIL_LIMIT="$DETAIL_LIMIT" timeout 8h "$PYTHON_BIN" -u "$PIPELINE_DIR/collect_detail.py" >> "$CURRENT_LOG" 2>&1
  DETAIL_EXIT=$?
  DETAIL_SECONDS=$(( $(date '+%s') - DETAIL_START_EPOCH ))

  {
    echo ""
    echo "Detail exit code: $DETAIL_EXIT"
    echo "Finished: $(date '+%Y-%m-%d %H:%M:%S')"
  } >> "$CURRENT_LOG"

  VIEW_EXIT=0
  VERIFY_EXIT=0
  COMPILE_EXIT=0
  REPAIR_EXIT=0
  CALENDAR_EXIT=0

  if [ "$DETAIL_EXIT" -eq 0 ]; then
    # STEP 3: normalize detail outputs after all detail downloads finish. This
    # rebuilds the structured summary/items/calendar views in each detail JSON
    # and rewrites per-record .calendar.ics files before packages or WAHA.
    if [ "${PC_REBUILD_DETAIL_VIEWS_AFTER_DETAIL:-1}" != "0" ] && [ -x "$PIPELINE_DIR/030-build-detail-views.py" ]; then
      write_progress "CALENDAR" "RUNNING" "76" "Step 3/6: creating per-record calendar/detail views from saved detail.json files..." "$STARTED"
      {
        echo ""
        echo "-------------------- STEP 3: DETAIL VIEWS + RECORD ICS --------"
        echo "Started: $(date '+%Y-%m-%d %H:%M:%S')"
        echo "Command: ${PYTHON_BIN} -u $PIPELINE_DIR/030-build-detail-views.py --apply --since $STARTED"
      } >> "$CURRENT_LOG"
      VIEW_START_EPOCH="$(date '+%s')"
      "$PYTHON_BIN" -u "$PIPELINE_DIR/030-build-detail-views.py" --apply --since "$STARTED" >> "$CURRENT_LOG" 2>&1
      VIEW_EXIT=$?
      VIEW_SECONDS=$(( $(date '+%s') - VIEW_START_EPOCH ))
      {
        echo "Detail views exit code: $VIEW_EXIT"
        echo "Finished: $(date '+%Y-%m-%d %H:%M:%S')"
      } >> "$CURRENT_LOG"
    else
      write_progress "CALENDAR" "RUNNING" "76" "Step 3/6: per-record calendar/detail view rebuild disabled; continuing to package calendars." "$STARTED"
      log "ITERATION $ITERATION detail view rebuild skipped by PC_REBUILD_DETAIL_VIEWS_AFTER_DETAIL=0."
    fi

    # STEP 4: verification and repair. Compile the core Python entrypoints, then
    # retry failed rows and folders/DB rows missing DTEND before calendar packages
    # are built, so any repaired records are included in the packages/messaging.
    if [ "$VIEW_EXIT" -eq 0 ]; then
      write_progress "VERIFY" "RUNNING" "84" "Step 4/6: compiling collector scripts and checking failed/missing-deadline records..." "$STARTED"
      {
        echo ""
        echo "-------------------- STEP 4: VERIFY + REPAIR -------------------"
        echo "Started: $(date '+%Y-%m-%d %H:%M:%S')"
        echo "Command: ${PYTHON_BIN} -m py_compile core collector scripts"
      } >> "$CURRENT_LOG"

      VERIFY_START_EPOCH="$(date '+%s')"
      "$PYTHON_BIN" -m py_compile \
        "$APP_ROOT/src/common.py" "$PIPELINE_DIR/010-collect-index.py" "$PIPELINE_DIR/collect_detail.py" \
        "$PIPELINE_DIR/030-build-detail-views.py" "$PIPELINE_DIR/build_calendar.py" "$PIPELINE_DIR/notify_new_records.py" \
        "$PIPELINE_DIR/040-repair-missing-deadlines.py" >> "$CURRENT_LOG" 2>&1
      COMPILE_EXIT=$?
      {
        echo "Compile verification exit code: $COMPILE_EXIT"
        echo "Finished compile: $(date '+%Y-%m-%d %H:%M:%S')"
      } >> "$CURRENT_LOG"

      if [ "$COMPILE_EXIT" -eq 0 ] && [ "${PC_REPAIR_FAILED_AND_MISSING_DEADLINES:-1}" != "0" ] && [ -x "$PIPELINE_DIR/040-repair-missing-deadlines.py" ]; then
        REPAIR_LIMIT="${PC_MISSING_DEADLINE_REPAIR_LIMIT:-0}"
        {
          echo ""
          echo "Verification repair command: ${PYTHON_BIN} -u $PIPELINE_DIR/040-repair-missing-deadlines.py --include-failed --apply --limit $REPAIR_LIMIT"
          echo "PC_MISSING_DEADLINE_REPAIR_LIMIT=$REPAIR_LIMIT (0 = all matching failed/missing-deadline rows)"
        } >> "$CURRENT_LOG"
        "$PYTHON_BIN" -u "$PIPELINE_DIR/040-repair-missing-deadlines.py" --include-failed --apply --limit "$REPAIR_LIMIT" >> "$CURRENT_LOG" 2>&1
        REPAIR_EXIT=$?
        echo "Failed/missing-deadline repair exit code: $REPAIR_EXIT" >> "$CURRENT_LOG"
        if [ "$REPAIR_EXIT" -ne 0 ]; then
          log "ITERATION $ITERATION verification repair returned exit=$REPAIR_EXIT; continuing calendar packaging with current records."
        fi
      else
        echo "Failed/missing-deadline repair skipped (compile_exit=$COMPILE_EXIT, enabled=${PC_REPAIR_FAILED_AND_MISSING_DEADLINES:-1})." >> "$CURRENT_LOG"
      fi
      VERIFY_SECONDS=$(( $(date '+%s') - VERIFY_START_EPOCH ))
      VERIFY_EXIT=$COMPILE_EXIT
    fi

    # STEP 5: build timestamped Thunderbird/ICS import packages from the
    # per-record calendars after detail views/verification have completed.
    if [ "$VIEW_EXIT" -eq 0 ] && [ "$VERIFY_EXIT" -eq 0 ]; then
      write_progress "CALENDAR" "RUNNING" "90" "Step 5/6: creating timestamped calendar import packages (.ics)..." "$STARTED"
      {
        echo ""
        echo "-------------------- STEP 5: CALENDAR PACKAGES -----------------"
        echo "Started: $(date '+%Y-%m-%d %H:%M:%S')"
        echo "Command: ${PYTHON_BIN} -u $PIPELINE_DIR/build_calendar.py"
      } >> "$CURRENT_LOG"

      CALENDAR_START_EPOCH="$(date '+%s')"
      "$PYTHON_BIN" -u "$PIPELINE_DIR/build_calendar.py" >> "$CURRENT_LOG" 2>&1
      CALENDAR_EXIT=$?
      CALENDAR_SECONDS=$(( $(date '+%s') - CALENDAR_START_EPOCH ))

      {
        echo "Calendar package exit code: $CALENDAR_EXIT"
        echo "Finished: $(date '+%Y-%m-%d %H:%M:%S')"
      } >> "$CURRENT_LOG"
    fi
  fi

  if [ "$DETAIL_EXIT" -eq 124 ]; then
    write_progress "DETAIL" "TIMEOUT" "90" "Detail downloader timed out. Calendar/package/WhatsApp steps skipped." "$STARTED"
    notify_waha "timeout" "TIMEOUT" "Iteration $ITERATION detail downloader timed out. Calendar/package/WhatsApp steps skipped."
    log "ITERATION $ITERATION detail step timed out."
  elif [ "$DETAIL_EXIT" -ne 0 ]; then
    write_progress "DETAIL" "FAILED" "90" "Detail downloader failed with exit=$DETAIL_EXIT. Calendar/package/WhatsApp steps skipped." "$STARTED"
    notify_waha "failed" "FAILED" "Iteration $ITERATION detail downloader failed with exit=$DETAIL_EXIT. Calendar/package/WhatsApp steps skipped."
    log "ITERATION $ITERATION detail failed with exit=$DETAIL_EXIT."
  elif [ "$VIEW_EXIT" -ne 0 ]; then
    write_progress "CALENDAR" "FAILED" "88" "Detail finished but per-record calendar/detail view build failed with exit=$VIEW_EXIT. Packages/WhatsApp skipped." "$STARTED"
    notify_waha "failed" "FAILED" "Iteration $ITERATION detail view/calendar build failed with exit=$VIEW_EXIT."
    log "ITERATION $ITERATION detail view/calendar step failed with exit=$VIEW_EXIT."
  elif [ "$VERIFY_EXIT" -ne 0 ]; then
    write_progress "VERIFY" "FAILED" "86" "Verification compile failed with exit=$VERIFY_EXIT. Calendar/package/WhatsApp steps skipped." "$STARTED"
    notify_waha "failed" "FAILED" "Iteration $ITERATION verification compile failed with exit=$VERIFY_EXIT."
    log "ITERATION $ITERATION verification compile failed with exit=$VERIFY_EXIT."
  elif [ "$CALENDAR_EXIT" -ne 0 ]; then
    write_progress "CALENDAR" "FAILED" "94" "Per-record calendars finished but calendar package build failed with exit=$CALENDAR_EXIT. WhatsApp skipped." "$STARTED"
    notify_waha "failed" "FAILED" "Iteration $ITERATION calendar package build failed with exit=$CALENDAR_EXIT."
    log "ITERATION $ITERATION calendar package step failed with exit=$CALENDAR_EXIT."
  fi

  # STEP 6: MESSAGING — send the rich WhatsApp messages one by one. This is a
  # visible step: notify_new_records.py --announce publishes per-message
  # progress (current/total + a preview), so the monitor shows each message going
  # out. It announces new opportunities AND status changes (e.g. Programada →
  # Abierta), or sends the single "Sin nuevas entradas" status when there is
  # nothing to send. It runs only after index, all details, per-record calendars,
  # and calendar packages succeed.
  if [ "$DETAIL_EXIT" -eq 0 ] && [ "$VIEW_EXIT" -eq 0 ] && [ "$VERIFY_EXIT" -eq 0 ] && [ "$CALENDAR_EXIT" -eq 0 ]; then
    NOTIFY_WHATSAPP="${PC_NOTIFY_WHATSAPP:-}"
    if [ -z "$NOTIFY_WHATSAPP" ] && [ -f "$MONITOR_SETTINGS" ]; then
      NOTIFY_WHATSAPP="$($PYTHON_BIN - "$MONITOR_SETTINGS" <<'PY'
import sys
from pathlib import Path
for line in Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace").splitlines():
    if line.startswith("PC_NOTIFY_WHATSAPP="):
        print(line.split("=", 1)[1].strip().strip("\"").strip("'"))
        break
PY
)"
    fi
    NOTIFY_WHATSAPP="${NOTIFY_WHATSAPP:-1}"
    if [ "$NOTIFY_WHATSAPP" != "0" ]; then
      write_progress "MESSAGING" "RUNNING" "96" "Step 6/6: sending WhatsApp messages (new opportunities + status changes)..." "$STARTED"
      {
        echo ""
        echo "-------------------- STEP 6: WHATSAPP MESSAGING ----------------"
        echo "Started: $(date '+%Y-%m-%d %H:%M:%S')"
      } >> "$CURRENT_LOG"
      MESSAGING_START_EPOCH="$(date '+%s')"
      PC_MSG_STEP_CURRENT=6 PC_MSG_STEP_TOTAL=6 notify_new_records --announce
      MESSAGING_SECONDS=$(( $(date '+%s') - MESSAGING_START_EPOCH ))
    else
      write_progress "MESSAGING" "DONE" "96" "Step 6/6: WhatsApp notifications disabled by monitor setting." "$STARTED"
      log "ITERATION $ITERATION WhatsApp notifications skipped by PC_NOTIFY_WHATSAPP=0."
    fi
    FINISHED="$(date '+%Y-%m-%d %H:%M:%S')"
    SUMMARY_COUNTS="$($PYTHON_BIN - <<'PY'
from common import init_db
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
    TOTAL_SECONDS=$(( $(date '+%s') - RUN_START_EPOCH ))
    {
      echo "STARTED_AT='$(quote_value "$STARTED")'"
      echo "FINISHED_AT='$(quote_value "$FINISHED")'"
      echo "TOTAL_SECONDS='$TOTAL_SECONDS'"
      echo "UPDATE_SECONDS='$UPDATE_SECONDS'"
      echo "INDEX_SECONDS='$INDEX_SECONDS'"
      echo "DETAIL_SECONDS='$DETAIL_SECONDS'"
      echo "VIEW_SECONDS='$VIEW_SECONDS'"
      echo "VERIFY_SECONDS='$VERIFY_SECONDS'"
      echo "CALENDAR_SECONDS='$CALENDAR_SECONDS'"
      echo "MESSAGING_SECONDS='$MESSAGING_SECONDS'"
      echo "TOTAL_TEXT='$(quote_value "$(format_eta "$TOTAL_SECONDS")")'"
    } > "$LAST_SUMMARY_FILE"

    notify_waha "done" "DONE" "📊 Resumen de Ejecución - Panama Compra
Inicio: $STARTED
Fin: $FINISHED
Duración total: $(format_eta "$TOTAL_SECONDS")
Etapas: index $(format_eta "$INDEX_SECONDS"), detail/download $(format_eta "$DETAIL_SECONDS"), store/views $(format_eta "$VIEW_SECONDS"), verification $(format_eta "$VERIFY_SECONDS"), calendar $(format_eta "$CALENDAR_SECONDS"), messaging $(format_eta "$MESSAGING_SECONDS")
Iteración: $ITERATION
$SUMMARY_COUNTS"
    {
      echo "Finished: $FINISHED"
    } >> "$CURRENT_LOG"
    write_progress "DONE" "DONE" "100" "Index, details, verification/repair, per-record calendars, calendar packages and WhatsApp messaging completed." "$STARTED"
    log "ITERATION $ITERATION finished successfully."
  fi

  {
    echo ""
    echo "============================================================"
    echo "RUN-ALL ITERATION $ITERATION FINISHED: $(date '+%Y-%m-%d %H:%M:%S')"
    echo "INDEX_EXIT=$INDEX_EXIT"
    echo "DETAIL_EXIT=$DETAIL_EXIT"
    echo "VIEW_EXIT=$VIEW_EXIT"
    echo "VERIFY_EXIT=$VERIFY_EXIT"
    echo "COMPILE_EXIT=$COMPILE_EXIT"
    echo "REPAIR_EXIT=$REPAIR_EXIT"
    echo "CALENDAR_EXIT=$CALENDAR_EXIT"
    echo "============================================================"
  } >> "$CURRENT_LOG"
  cat "$CURRENT_LOG" >> "$HISTORY_LOG"

  # STEP 7: OPTIONAL test zone. When this run had no new records to process, it
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
      echo "----------------- STEP 7: TEST ZONE (idle, last $TEST_LIMIT) ----"
      echo "Started: $(date '+%Y-%m-%d %H:%M:%S')"
      echo "Command: ${PYTHON_BIN} -u $PIPELINE_DIR/070-test-zone.py --limit $TEST_LIMIT --apply"
    } >> "$CURRENT_LOG"
    "$PYTHON_BIN" -u "$PIPELINE_DIR/070-test-zone.py" --limit "$TEST_LIMIT" --apply >> "$CURRENT_LOG" 2>&1
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
launch_queued_update_monitor
