#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../../lib/env.sh
source "$SCRIPT_DIR/../../lib/env.sh"
cd "$APP_ROOT"

FLAG="$PC_QUEUE_DIR/run_all_requested.flag"
UPDATE_QUEUE_FLAG="$PC_QUEUE_DIR/update_monitor_requested.flag"
UPDATE_IN_PROGRESS_FLAG="$PC_QUEUE_DIR/update_monitor_in_progress.flag"
PROGRESS="$PC_LOG_DIR/run_all_progress.env"
WORKER_LOG="$PC_LOG_DIR/run_all_worker.log"
TRIGGER_LOG="$PC_LOG_DIR/collector_triggered.log"
WEBHOOK_LOG="$PC_LOG_DIR/webhook_listener.log"
UPDATE_QUEUE_LOG="$PC_LOG_DIR/update_monitor_queue.log"
PRIORITY_STATE="$PC_QUEUE_DIR/priority-run.state"
PRIORITY_PENDING="$PC_QUEUE_DIR/priority-pending"
PRIORITY_LOG="$PC_LOG_DIR/priority-run.log"

printf 'PanamaCompra queue/status\n'
printf 'Checkout: %s\n' "$APP_ROOT"
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

if [ -f "$PRIORITY_STATE" ]; then
  printf '\nPriority dispatcher:\n'
  sed -n '1,20p' "$PRIORITY_STATE"
else
  printf '\nPriority dispatcher: idle\n'
fi
if [ -d "$PRIORITY_PENDING" ]; then
  printf 'Priority jobs waiting: %s\n' "$(find "$PRIORITY_PENDING" -maxdepth 1 -type f -name '*.job' | wc -l)"
  find "$PRIORITY_PENDING" -maxdepth 1 -type f -name '*.job' -printf '  %f\n' | sort | head -20
fi

if pgrep -f "[w]atch-queue-flag.sh|[r]un-worker.sh|[r]un-collector.sh" >/dev/null 2>&1; then
  printf 'Runner/worker process: running\n'
  pgrep -af "[w]atch-queue-flag.sh|[r]un-worker.sh|[r]un-collector.sh" || true
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

printf '\nRecent %s:\n' "$PRIORITY_LOG"
if [ -f "$PRIORITY_LOG" ]; then
  tail -20 "$PRIORITY_LOG"
else
  printf '(missing)\n'
fi
