#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT" || exit 1

FLAG="data/queue/run_all_requested.flag"
UPDATE_QUEUE_FLAG="data/queue/update_monitor_requested.flag"
UPDATE_IN_PROGRESS_FLAG="data/queue/update_monitor_in_progress.flag"
PROGRESS="data/logs/run_all_progress.env"
WORKER_LOG="data/logs/run_all_worker.log"
TRIGGER_LOG="data/logs/collector_triggered.log"
WEBHOOK_LOG="data/logs/webhook_listener.log"
UPDATE_QUEUE_LOG="data/logs/update_monitor_queue.log"

printf 'PanamaCompra queue/status\n'
printf 'Checkout: %s\n' "$ROOT"
printf '\n'

if [ -f "$FLAG" ]; then
  printf 'Queue flag: PENDING (%s)\n' "$(stat -c '%y' "$FLAG" 2>/dev/null || stat -f '%Sm' "$FLAG" 2>/dev/null || echo unknown)"
else
  printf 'Queue flag: none\n'
fi

if [ -f "$UPDATE_QUEUE_FLAG" ]; then
  printf 'Update + Monitor queue: PENDING (%s)\n' "$(stat -c '%y' "$UPDATE_QUEUE_FLAG" 2>/dev/null || stat -f '%Sm' "$UPDATE_QUEUE_FLAG" 2>/dev/null || echo unknown)"
  sed -n '1,20p' "$UPDATE_QUEUE_FLAG" 2>/dev/null || true
elif [ -f "$UPDATE_IN_PROGRESS_FLAG" ]; then
  printf 'Update + Monitor queue: running (%s)\n' "$(stat -c '%y' "$UPDATE_IN_PROGRESS_FLAG" 2>/dev/null || stat -f '%Sm' "$UPDATE_IN_PROGRESS_FLAG" 2>/dev/null || echo unknown)"
else
  printf 'Update + Monitor queue: none\n'
fi

if pgrep -f "[p]c_run_all_flag_watcher.sh|[p]c_run_all_worker.sh|[r]un_collector.sh" >/dev/null 2>&1; then
  printf 'Runner/worker process: running\n'
  pgrep -af "[p]c_run_all_flag_watcher.sh|[p]c_run_all_worker.sh|[r]un_collector.sh" || true
else
  printf 'Runner/worker process: not detected\n'
fi

if [ -f "$PROGRESS" ]; then
  printf '\nCurrent run progress:\n'
  sed -n '1,80p' "$PROGRESS"
else
  printf '\nCurrent run progress: no %s file\n' "$PROGRESS"
fi

for log in "$WEBHOOK_LOG" "$TRIGGER_LOG" "$WORKER_LOG" "$UPDATE_QUEUE_LOG"; do
  printf '\nRecent %s:\n' "$log"
  if [ -f "$log" ]; then
    tail -20 "$log"
  else
    printf '(missing)\n'
  fi
done
