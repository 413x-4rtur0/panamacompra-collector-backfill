#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" || exit 1

mkdir -p data/logs data/queue

DETAIL_LIMIT="${1:-99}"
RUN_MODE="${2:-${PC_RUN_MODE:-RESTART}}"
REQUEST_FLAG="data/queue/run_all_requested.flag"
IN_PROGRESS_FLAG="data/queue/run_all_in_progress.flag"
REQUEST_LOG="data/logs/run_all_requests.log"

echo "$(date '+%Y-%m-%d %H:%M:%S') | RUN-ALL REQUESTED detail_limit=$DETAIL_LIMIT mode=$RUN_MODE" | tee -a "$REQUEST_LOG"

if [ -f "$IN_PROGRESS_FLAG" ] && ! pgrep -f "[p]c_run_all_worker.sh" >/dev/null 2>&1; then
  echo "$(date '+%Y-%m-%d %H:%M:%S') | Stale in-progress flag found; starting worker to resume pending work." | tee -a "$REQUEST_LOG"
fi

touch "$REQUEST_FLAG"

if pgrep -f "[p]c_run_all_worker.sh" >/dev/null 2>&1; then
  echo "Worker already active. Request flag left for active worker."
else
  PC_RUN_MODE="$RUN_MODE" nohup ./pc_run_all_worker.sh "$DETAIL_LIMIT" >/dev/null 2>&1 &
  echo "Worker started."
fi

if [ -x "./pc_open_monitor.sh" ]; then
  ./pc_open_monitor.sh >/dev/null 2>&1 || true
fi

echo "Run-all request submitted."
