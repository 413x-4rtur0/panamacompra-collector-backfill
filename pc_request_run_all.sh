#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" || exit 1

mkdir -p data/logs data/queue

DETAIL_LIMIT="${1:-0}"
RUN_MODE="${2:-${PC_RUN_MODE:-RESTART}}"
INDEX_LIMIT="${3:-${PC_INDEX_LIMIT:-${PC_MAX_PAGES_PER_GROUP:-0}}}"
if [ "${RUN_MODE^^}" = "AUTO" ]; then
  DETAIL_LIMIT="0"
  INDEX_LIMIT="0"
fi
REQUEST_FLAG="data/queue/run_all_requested.flag"
IN_PROGRESS_FLAG="data/queue/run_all_in_progress.flag"
REQUEST_LOG="data/logs/run_all_requests.log"
PROGRESS_FILE="data/logs/run_all_progress.env"

quote_value() {
  printf "%s" "$1" | sed "s/'/'\\'''/g"
}

write_queued_progress() {
  local tmp="${PROGRESS_FILE}.tmp"
  {
    echo "PHASE='QUEUED'"
    echo "STATUS='RUNNING'"
    echo "PERCENT='1'"
    echo "MESSAGE='$(quote_value "Run-all $RUN_MODE request queued; worker is starting...")'"
    echo "INDEX_LIMIT='$(quote_value "$INDEX_LIMIT")'"
    echo "ETA='-'"
    echo "DETAIL_LIMIT='$(quote_value "$DETAIL_LIMIT")'"
    echo "STARTED_AT='$(date '+%Y-%m-%d %H:%M:%S')'"
    echo "UPDATED_AT='$(date '+%Y-%m-%d %H:%M:%S')'"
    echo "WORKER_PID='-'"
    echo "MODE='$(quote_value "$RUN_MODE")'"
    echo "STEP_CURRENT='-'"; echo "STEP_TOTAL='-'"
    echo "ITEM_CURRENT='-'"; echo "ITEM_TOTAL='-'"
    echo "RECORDS_FOUND='-'"; echo "RECORDS_NEW='-'"; echo "RECORDS_EXISTING='-'"
    echo "RECORDS_SAVED='-'"; echo "RECORDS_FAILED='-'"; echo "RECORDS_PENDING='-'"
    echo "RECORDS_TEST='-'"; echo "EXTRA='-'"
  } > "$tmp"
  mv "$tmp" "$PROGRESS_FILE"
}

echo "$(date '+%Y-%m-%d %H:%M:%S') | RUN-ALL REQUESTED index_limit=$INDEX_LIMIT detail_limit=$DETAIL_LIMIT mode=$RUN_MODE" | tee -a "$REQUEST_LOG"

if [ -f "$IN_PROGRESS_FLAG" ] && ! pgrep -f "[p]c_run_all_worker.sh" >/dev/null 2>&1; then
  echo "$(date '+%Y-%m-%d %H:%M:%S') | Stale in-progress flag found; starting worker to resume pending work." | tee -a "$REQUEST_LOG"
fi

touch "$REQUEST_FLAG"

if pgrep -f "[p]c_run_all_worker.sh" >/dev/null 2>&1; then
  echo "Worker already active. Request flag left for active worker."
else
  write_queued_progress
  PC_RUN_MODE="$RUN_MODE" PC_INDEX_LIMIT="$INDEX_LIMIT" nohup ./pc_run_all_worker.sh "$DETAIL_LIMIT" "$INDEX_LIMIT" >/dev/null 2>&1 &
  echo "Worker started."
fi

if [ -x "./pc_open_monitor.sh" ]; then
  ./pc_open_monitor.sh >/dev/null 2>&1 || true
fi

echo "Run-all request submitted."
