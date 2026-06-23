#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" || exit 1

mkdir -p data/logs data/queue

# Default detail cap per webhook-triggered run. Raise PC_WEBHOOK_DETAIL_LIMIT
# for a bigger batch, or lower it (e.g. 5 or 10) for safer testing.
DETAIL_LIMIT="${PC_WEBHOOK_DETAIL_LIMIT:-99}"

echo "===== changedetection webhook received at $(date '+%Y-%m-%d %H:%M:%S') =====" >> data/logs/collector_triggered.log
echo "Requesting full sequence: index + detail, detail_limit=$DETAIL_LIMIT" >> data/logs/collector_triggered.log

if [ -f data/queue/update_in_progress.flag ]; then
  touch data/queue/run_all_requested.flag
  echo "Update in progress; deferred run-all request until update_local_copy.sh finishes." >> data/logs/collector_triggered.log
  exit 0
fi

./pc_request_run_all.sh "$DETAIL_LIMIT" >> data/logs/collector_triggered.log 2>&1

echo "Full sequence requested at $(date '+%Y-%m-%d %H:%M:%S')" >> data/logs/collector_triggered.log
