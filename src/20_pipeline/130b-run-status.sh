#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
# shellcheck source=../../lib/env.sh
source "$SCRIPT_DIR/../../lib/env.sh"
cd "$APP_ROOT"

echo "PanamaCompra run-all status"
echo "---------------------------"

WORKER_RUNNING=0
TEST_RUNNING=0
if pgrep -f "[r]un-worker.sh" >/dev/null 2>&1; then
  WORKER_RUNNING=1
  echo "Worker: RUNNING"
else
  echo "Worker: not running"
fi

if pgrep -f "[p]ython3? -u .*070-test-zone.py" >/dev/null 2>&1; then
  TEST_RUNNING=1
fi
if [ "$WORKER_RUNNING" -eq 1 ] && [ "$TEST_RUNNING" -eq 0 ]; then
  echo "Normal run: RUNNING"
else
  echo "Normal run: not running"
fi
if [ "$TEST_RUNNING" -eq 1 ]; then
  echo "Test run: RUNNING"
else
  echo "Test run: not running"
fi

if pgrep -f "[p]ython3? -u .*010-collect-index.py" >/dev/null 2>&1; then
  echo "Index collector: RUNNING"
else
  echo "Index collector: not running"
fi

if pgrep -f "[p]ython3? -u .*030-collect-details.py" >/dev/null 2>&1; then
  echo "Detail downloader: RUNNING"
else
  echo "Detail downloader: not running"
fi

if pgrep -f "[p]ython3? -u .*060-build-calendar.py" >/dev/null 2>&1; then
  echo "Calendar packager: RUNNING"
else
  echo "Calendar packager: not running"
fi

if [ -f "$PC_QUEUE_DIR/run_all_requested.flag" ]; then
  echo "Pending request flag: YES"
else
  echo "Pending request flag: no"
fi

echo ""
echo "Progress snapshot:"
if [ -f "$PC_LOG_DIR/run_all_progress.env" ]; then
  # shellcheck disable=SC1091
  source "$PC_LOG_DIR/run_all_progress.env"
  echo "Phase: ${PHASE:-unknown}"
  echo "Status: ${STATUS:-unknown}"
  echo "Percent: ${PERCENT:-0}%"
  echo "Run type: ${RUN_TYPE:-unknown}"
  echo "Run source: ${RUN_SOURCE:-unknown}"
  echo "Run trigger: ${RUN_TRIGGER:-unknown}"
  echo "Test autorun: ${TEST_AUTORUN:-0}"
  echo "Step: ${STEP_CURRENT:--}/${STEP_TOTAL:--}"
  echo "Item: ${ITEM_CURRENT:--}/${ITEM_TOTAL:--}"
  echo "Message: ${MESSAGE:-}"
  echo "Diagnostics: found=${RECORDS_FOUND:--} new=${RECORDS_NEW:--} existing=${RECORDS_EXISTING:--} saved=${RECORDS_SAVED:--} failed=${RECORDS_FAILED:--} pending=${RECORDS_PENDING:--}"
  echo "Updated: ${UPDATED_AT:-}"
else
  echo "No progress file yet."
fi

echo ""
echo "Related processes:"
pgrep -af "100-run-worker.sh|010-collect-index.py|030-collect-details.py|060-build-calendar.py|070-test-zone.py|timeout .*src/20_pipeline" || true

echo ""
echo "Recent run-all requests:"
tail -20 "$PC_LOG_DIR/run_all_requests.log" 2>/dev/null || echo "No request log yet."

echo ""
echo "Recent worker log:"
tail -40 "$PC_LOG_DIR/run_all_worker.log" 2>/dev/null || echo "No worker log yet."

echo ""
echo "Current run log:"
tail -100 "$PC_LOG_DIR/run_all_current.log" 2>/dev/null || echo "No current log yet."
