#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" || exit 1

mkdir -p data/logs

# Changedetection/AUTO runs are intentionally unlimited. Manual/test runs are the only places that may pass index/detail caps.
DETAIL_LIMIT="0"
INDEX_LIMIT="0"

echo "===== changedetection webhook received at $(date '+%Y-%m-%d %H:%M:%S') =====" >> data/logs/collector_triggered.log
echo "Requesting full sequence: index + detail, index_limit=$INDEX_LIMIT, detail_limit=$DETAIL_LIMIT" >> data/logs/collector_triggered.log

PC_RUN_MODE=AUTO PC_INDEX_LIMIT="$INDEX_LIMIT" ./pc_request_run_all.sh "$DETAIL_LIMIT" AUTO "$INDEX_LIMIT" >> data/logs/collector_triggered.log 2>&1

echo "Full sequence requested at $(date '+%Y-%m-%d %H:%M:%S')" >> data/logs/collector_triggered.log
