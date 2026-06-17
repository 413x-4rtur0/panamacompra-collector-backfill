#!/usr/bin/env bash
set -euo pipefail

cd "$HOME/Apps/panamacompra-collector" || exit 1

DETAIL_LIMIT="${1:-999999}"

mkdir -p data/queue data/logs

touch data/queue/run_all_requested.flag

echo "Starting run-all worker in this terminal..."
echo "Detail limit: $DETAIL_LIMIT"
echo ""

./pc_run_all_worker.sh "$DETAIL_LIMIT"

echo ""
echo "Finished. Last current log:"
echo "------------------------------------------------------------"
tail -120 data/logs/run_all_current.log 2>/dev/null || true
