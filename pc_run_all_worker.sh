#!/usr/bin/env bash
set -uo pipefail

cd "$HOME/Apps/panamacompra-collector" || exit 1

if [ -f ".venv/bin/activate" ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

mkdir -p data/logs data/queue

LOCK_FILE="/tmp/panamacompra_run_all_worker.lock"
REQUEST_FLAG="data/queue/run_all_requested.flag"
WORKER_LOG="data/logs/run_all_worker.log"
CURRENT_LOG="data/logs/run_all_current.log"
HISTORY_LOG="data/logs/run_all_history.log"
PROGRESS_FILE="data/logs/run_all_progress.env"

DETAIL_LIMIT="${1:-999999}"

log() {
  echo "$(date '+%Y-%m-%d %H:%M:%S') | $*" | tee -a "$WORKER_LOG"
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
    echo "DETAIL_LIMIT='$(quote_value "$DETAIL_LIMIT")'"
    echo "STARTED_AT='$(quote_value "$started_at")'"
    echo "UPDATED_AT='$(date '+%Y-%m-%d %H:%M:%S')'"
    echo "WORKER_PID='$$'"
  } > "$tmp"

  mv "$tmp" "$PROGRESS_FILE"
}

exec 9>"$LOCK_FILE"

if ! flock -n 9; then
  log "Worker already running. This duplicate worker exits."
  exit 0
fi

write_progress "STARTING" "RUNNING" "2" "Starting run-all worker..." "$(date '+%Y-%m-%d %H:%M:%S')"
log "RUN-ALL WORKER STARTED detail_limit=$DETAIL_LIMIT"

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
  STARTED="$(date '+%Y-%m-%d %H:%M:%S')"

  {
    echo "============================================================"
    echo "RUN-ALL ITERATION $ITERATION STARTED: $STARTED"
    echo "DETAIL_LIMIT: $DETAIL_LIMIT"
    echo "PID: $$"
    echo "============================================================"
  } > "$CURRENT_LOG"

  log "ITERATION $ITERATION started."

  write_progress "INDEX" "RUNNING" "10" "Step 1/2: opening PanamaCompra and collecting Programadas + Abiertas tables..." "$STARTED"

  {
    echo ""
    echo "-------------------- STEP 1: INDEX COLLECTOR --------------------"
    echo "Started: $(date '+%Y-%m-%d %H:%M:%S')"
    echo "Command: timeout 1h python -u ./pc_index_collector.py"
  } >> "$CURRENT_LOG"

  timeout 1h python -u ./pc_index_collector.py >> "$CURRENT_LOG" 2>&1
  INDEX_EXIT=$?

  {
    echo ""
    echo "Index exit code: $INDEX_EXIT"
    echo "Finished: $(date '+%Y-%m-%d %H:%M:%S')"
  } >> "$CURRENT_LOG"

  if [ "$INDEX_EXIT" -ne 0 ]; then
    write_progress "INDEX" "FAILED" "50" "Index collector failed. Detail step skipped." "$STARTED"
    log "ITERATION $ITERATION index failed with exit=$INDEX_EXIT. Detail skipped."

    {
      echo ""
      echo "INDEX FAILED. DETAIL STEP SKIPPED."
      echo "RUN-ALL ITERATION $ITERATION FINISHED WITH ERROR."
    } >> "$CURRENT_LOG"

    cat "$CURRENT_LOG" >> "$HISTORY_LOG"
    continue
  fi

  write_progress "DETAIL" "RUNNING" "55" "Step 2/2: downloading pending detail pages, limit=$DETAIL_LIMIT..." "$STARTED"

  {
    echo ""
    echo "-------------------- STEP 2: DETAIL DOWNLOADER ------------------"
    echo "Started: $(date '+%Y-%m-%d %H:%M:%S')"
    echo "Command: PC_DETAIL_LIMIT=$DETAIL_LIMIT timeout 8h python -u ./pc_detail_downloader.py"
  } >> "$CURRENT_LOG"

  PC_DETAIL_LIMIT="$DETAIL_LIMIT" timeout 8h python -u ./pc_detail_downloader.py >> "$CURRENT_LOG" 2>&1
  DETAIL_EXIT=$?

  {
    echo ""
    echo "Detail exit code: $DETAIL_EXIT"
    echo "Finished: $(date '+%Y-%m-%d %H:%M:%S')"
    echo ""
    echo "============================================================"
    echo "RUN-ALL ITERATION $ITERATION FINISHED: $(date '+%Y-%m-%d %H:%M:%S')"
    echo "INDEX_EXIT=$INDEX_EXIT"
    echo "DETAIL_EXIT=$DETAIL_EXIT"
    echo "============================================================"
  } >> "$CURRENT_LOG"

  cat "$CURRENT_LOG" >> "$HISTORY_LOG"

  if [ "$DETAIL_EXIT" -eq 0 ]; then
    write_progress "DONE" "DONE" "100" "Index and detail process completed successfully." "$STARTED"
    log "ITERATION $ITERATION finished successfully."
  elif [ "$DETAIL_EXIT" -eq 124 ]; then
    write_progress "DETAIL" "TIMEOUT" "90" "Detail downloader timed out." "$STARTED"
    log "ITERATION $ITERATION detail step timed out."
  else
    write_progress "DETAIL" "FAILED" "90" "Detail downloader failed with exit=$DETAIL_EXIT." "$STARTED"
    log "ITERATION $ITERATION detail failed with exit=$DETAIL_EXIT."
  fi

  if [ -f "$REQUEST_FLAG" ]; then
    write_progress "REPEAT" "RUNNING" "5" "Another request arrived while running. Repeating full sequence..." "$(date '+%Y-%m-%d %H:%M:%S')"
    log "Another request was received while running. Repeating full sequence."
  fi
done

write_progress "IDLE" "DONE" "100" "Worker stopped. No active PanamaCompra process." "$(date '+%Y-%m-%d %H:%M:%S')"
log "RUN-ALL WORKER STOPPED"
