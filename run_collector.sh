#!/usr/bin/env bash
set -euo pipefail

cd "$HOME/Apps/panamacompra-collector" || exit 1

mkdir -p data/logs

# By default this means "process all pending details".
# For safer testing, change 999999 to 5 or 10.
DETAIL_LIMIT="${PC_WEBHOOK_DETAIL_LIMIT:-999999}"

echo "===== changedetection webhook received at $(date '+%Y-%m-%d %H:%M:%S') =====" >> data/logs/collector_triggered.log
echo "Requesting full sequence: index + detail, detail_limit=$DETAIL_LIMIT" >> data/logs/collector_triggered.log

./pc_request_run_all.sh "$DETAIL_LIMIT" >> data/logs/collector_triggered.log 2>&1

echo "Full sequence requested at $(date '+%Y-%m-%d %H:%M:%S')" >> data/logs/collector_triggered.log
