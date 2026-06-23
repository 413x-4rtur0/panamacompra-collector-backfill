#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" || exit 1

mkdir -p data/logs data/queue

# Default detail cap per webhook-triggered run. Raise PC_WEBHOOK_DETAIL_LIMIT
# for a bigger batch, or lower it (e.g. 5 or 10) for safer testing.
DETAIL_LIMIT="${PC_WEBHOOK_DETAIL_LIMIT:-99}"

echo "===== changedetection webhook received at $(date '+%Y-%m-%d %H:%M:%S') =====" >> data/logs/collector_triggered.log
echo "Requesting full sequence: pre-run update + index + detail + calendar, detail_limit=$DETAIL_LIMIT" >> data/logs/collector_triggered.log

# Start/reuse the monitor and next-run timer immediately for webhook-triggered
# runs. pc_request_run_all.sh also opens the monitor, but doing it here makes
# changedetection-triggered runs visible even if the request is deferred during
# a local update or the worker is already active.
if [ -x ./pc_open_monitor.sh ]; then
  PC_MONITOR_LAUNCH_CONTEXT=auto ./pc_open_monitor.sh >> data/logs/collector_triggered.log 2>&1 || true
fi

if [ -f data/queue/update_in_progress.flag ]; then
  touch data/queue/run_all_requested.flag
  echo "Update in progress; deferred run-all request until update_local_copy.sh finishes." >> data/logs/collector_triggered.log
  exit 0
fi

echo "Worker pre-run update is enabled by PC_RUN_UPDATE_BEFORE_RUN=${PC_RUN_UPDATE_BEFORE_RUN:-1}." >> data/logs/collector_triggered.log
./pc_request_run_all.sh "$DETAIL_LIMIT" >> data/logs/collector_triggered.log 2>&1

echo "Full sequence requested at $(date '+%Y-%m-%d %H:%M:%S')" >> data/logs/collector_triggered.log
