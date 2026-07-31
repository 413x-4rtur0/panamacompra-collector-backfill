#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
# shellcheck source=../../lib/env.sh
source "$SCRIPT_DIR/../../lib/env.sh"
cd "$APP_ROOT"

DETAIL_LIMIT="${1:-0}"
INDEX_LIMIT="${2:-${PC_INDEX_LIMIT:-${PC_MAX_PAGES_PER_GROUP:-0}}}"
RUN_MODE="${3:-${PC_RUN_MODE:-MANUAL}}"

echo "Starting run-all worker in this terminal..."
echo "Mode: $RUN_MODE"
echo "Index page cap: $INDEX_LIMIT (0 = all pages)"
echo "Detail limit: $DETAIL_LIMIT"
echo ""

PC_RUN_MODE="$RUN_MODE" PC_INDEX_LIMIT="$INDEX_LIMIT" PC_PRIORITY_LABEL="manual collector" \
  "$SCRIPT_DIR/125-run-priority.sh" manual 60 collector-manual -- \
  env PC_RUN_MODE="$RUN_MODE" PC_RUN_SOURCE=manual PC_RUN_TRIGGER=manual PC_INDEX_LIMIT="$INDEX_LIMIT" PC_PRIORITY_START_REQUEST=1 \
  "$SCRIPT_DIR/100-run-worker.sh" "$DETAIL_LIMIT" "$INDEX_LIMIT"

echo ""
echo "Finished. Last current log:"
echo "------------------------------------------------------------"
tail -120 "$PC_LOG_DIR/run_all_current.log" 2>/dev/null || true
