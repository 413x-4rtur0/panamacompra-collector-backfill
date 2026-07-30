#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
# shellcheck source=../../lib/env.sh
source "$SCRIPT_DIR/../../lib/env.sh"
cd "$APP_ROOT"

DETAIL_LIMIT="${1:-0}"
RUN_MODE="${2:-${PC_RUN_MODE:-RESTART}}"
INDEX_LIMIT="${3:-${PC_INDEX_LIMIT:-${PC_MAX_PAGES_PER_GROUP:-0}}}"
REQUEST_LOG="$PC_LOG_DIR/run_all_requests.log"
PRIORITY_RUNNER="$SCRIPT_DIR/125-run-priority.sh"
STOP_NO_RESUME_FLAG="$PC_QUEUE_DIR/run_all_stop_no_resume.flag"

echo "$(date '+%Y-%m-%d %H:%M:%S') | RUN-ALL REQUESTED index_page_cap=$INDEX_LIMIT detail_limit=$DETAIL_LIMIT mode=$RUN_MODE" | tee -a "$REQUEST_LOG"
rm -f "$STOP_NO_RESUME_FLAG"

if [ "${RUN_MODE^^}" = "AUTO" ]; then
  PRIORITY_SOURCE="${PC_RUN_SOURCE:-changedetection}"
  # Keep Cron and changedetection as separate pending jobs so a queued Cron
  # request cannot be coalesced away by an earlier changedetection request.
  PRIORITY_KEY="collector-${PRIORITY_SOURCE}"
  PRIORITY_LABEL="${PC_PRIORITY_LABEL:-${PRIORITY_SOURCE} automatic collector}"
else
  PRIORITY_SOURCE="${PC_RUN_SOURCE:-manual}"
  PRIORITY_KEY="collector-manual"
  PRIORITY_LABEL="${PC_PRIORITY_LABEL:-manual collector}"
fi

echo "$(date '+%Y-%m-%d %H:%M:%S') | priority request source=$PRIORITY_SOURCE key=$PRIORITY_KEY" | tee -a "$REQUEST_LOG"
PC_RUN_MODE="$RUN_MODE" PC_INDEX_LIMIT="$INDEX_LIMIT" PC_PRIORITY_LABEL="$PRIORITY_LABEL" \
  nohup "$PRIORITY_RUNNER" "$PRIORITY_SOURCE" "" "$PRIORITY_KEY" -- \
  env PC_RUN_MODE="$RUN_MODE" PC_RUN_SOURCE="$PRIORITY_SOURCE" PC_RUN_TRIGGER="$PRIORITY_SOURCE" PC_INDEX_LIMIT="$INDEX_LIMIT" PC_PRIORITY_START_REQUEST=1 \
  "$SCRIPT_DIR/100-run-worker.sh" "$DETAIL_LIMIT" "$INDEX_LIMIT" >/dev/null 2>&1 &
echo "Priority dispatcher submitted (source=$PRIORITY_SOURCE)."

if [ "${PC_REQUEST_OPEN_MONITOR:-1}" != "0" ] && [ -x "$APP_ROOT/src/40_monitor/000-open-monitor.sh" ]; then
  "$APP_ROOT/src/40_monitor/000-open-monitor.sh" >/dev/null 2>&1 || true
fi

echo "Run-all request submitted."
