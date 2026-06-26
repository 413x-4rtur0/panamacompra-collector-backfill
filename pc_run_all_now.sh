#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" || exit 1

DETAIL_LIMIT="${1:-99}"
INDEX_LIMIT="${2:-${PC_INDEX_LIMIT:-${PC_MAX_PAGES_PER_GROUP:-0}}}"
RUN_MODE="${3:-${PC_RUN_MODE:-MANUAL}}"

mkdir -p data/queue data/logs

touch data/queue/run_all_requested.flag

echo "Starting run-all worker in this terminal..."
echo "Mode: $RUN_MODE"
echo "Index page cap: $INDEX_LIMIT (0 = all pages)"
echo "Detail limit: $DETAIL_LIMIT"
echo ""

PC_RUN_MODE="$RUN_MODE" PC_INDEX_LIMIT="$INDEX_LIMIT" ./pc_run_all_worker.sh "$DETAIL_LIMIT" "$INDEX_LIMIT"

echo ""
echo "Finished. Last current log:"
echo "------------------------------------------------------------"
tail -120 data/logs/run_all_current.log 2>/dev/null || true
