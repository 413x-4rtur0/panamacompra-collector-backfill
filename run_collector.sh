#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" || exit 1

mkdir -p data/logs

# Default detail cap per webhook-triggered run. Raise PC_WEBHOOK_DETAIL_LIMIT
# for a bigger batch, or lower it (e.g. 5 or 10) for safer testing.
DETAIL_LIMIT="${PC_WEBHOOK_DETAIL_LIMIT:-99}"

echo "===== changedetection webhook received at $(date '+%Y-%m-%d %H:%M:%S') =====" >> data/logs/collector_triggered.log
echo "Requesting full sequence: index + detail, detail_limit=$DETAIL_LIMIT" >> data/logs/collector_triggered.log

PC_RUN_MODE=AUTO ./pc_request_run_all.sh "$DETAIL_LIMIT" AUTO >> data/logs/collector_triggered.log 2>&1

echo "Full sequence requested at $(date '+%Y-%m-%d %H:%M:%S')" >> data/logs/collector_triggered.log
