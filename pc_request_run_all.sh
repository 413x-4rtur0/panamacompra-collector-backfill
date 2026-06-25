#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" || exit 1

mkdir -p data/logs data/queue

DETAIL_LIMIT="${1:-99}"
RUN_MODE="${2:-${PC_RUN_MODE:-RESTART}}"
INDEX_LIMIT="${3:-${PC_INDEX_LIMIT:-${PC_MAX_PAGES_PER_GROUP:-20}}}"
REQUEST_FLAG="data/queue/run_all_requested.flag"
IN_PROGRESS_FLAG="data/queue/run_all_in_progress.flag"
STOP_NO_RESUME_FLAG="data/queue/run_all_stop_no_resume.flag"
REQUEST_LOG="data/logs/run_all_requests.log"

echo "$(date '+%Y-%m-%d %H:%M:%S') | RUN-ALL REQUESTED index_limit=$INDEX_LIMIT detail_limit=$DETAIL_LIMIT mode=$RUN_MODE" | tee -a "$REQUEST_LOG"

if [ -f "$IN_PROGRESS_FLAG" ] && ! pgrep -f "[p]c_run_all_worker.sh" >/dev/null 2>&1; then
  echo "$(date '+%Y-%m-%d %H:%M:%S') | Stale in-progress flag found; starting worker to resume pending work." | tee -a "$REQUEST_LOG"
fi

rm -f "$STOP_NO_RESUME_FLAG"
touch "$REQUEST_FLAG"

if pgrep -f "[p]c_run_all_worker.sh" >/dev/null 2>&1; then
  echo "Worker already active. Request flag left for active worker."
else
  PC_RUN_MODE="$RUN_MODE" PC_INDEX_LIMIT="$INDEX_LIMIT" nohup ./pc_run_all_worker.sh "$DETAIL_LIMIT" "$INDEX_LIMIT" >/dev/null 2>&1 &
  echo "Worker started."
fi

if [ -x "./pc_open_monitor.sh" ]; then
  ./pc_open_monitor.sh >/dev/null 2>&1 || true
fi

echo "Run-all request submitted."
