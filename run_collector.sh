#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" || exit 1

mkdir -p data/logs

# Default detail cap per webhook-triggered run. Raise PC_WEBHOOK_DETAIL_LIMIT
# for a bigger batch, or lower it (e.g. 5 or 10) for safer testing.
DETAIL_LIMIT="${PC_WEBHOOK_DETAIL_LIMIT:-99}"
INDEX_LIMIT="${PC_WEBHOOK_INDEX_LIMIT:-${PC_INDEX_LIMIT:-${PC_MAX_PAGES_PER_GROUP:-20}}}"

echo "===== changedetection webhook received at $(date '+%Y-%m-%d %H:%M:%S') =====" >> data/logs/collector_triggered.log
echo "Requesting full sequence: index + detail, index_limit=$INDEX_LIMIT, detail_limit=$DETAIL_LIMIT" >> data/logs/collector_triggered.log

PC_RUN_MODE=AUTO PC_INDEX_LIMIT="$INDEX_LIMIT" ./pc_request_run_all.sh "$DETAIL_LIMIT" AUTO "$INDEX_LIMIT" >> data/logs/collector_triggered.log 2>&1

echo "Full sequence requested at $(date '+%Y-%m-%d %H:%M:%S')" >> data/logs/collector_triggered.log
