#!/usr/bin/env bash
set -euo pipefail

# Host-side runner for the dockerized webhook listener.
#
# The webhook listener container runs in ENQUEUE-ONLY mode: it only writes the
# run request flag into the shared data/queue volume. This watcher runs on the
# HOST, where Playwright Firefox and the records archive live, and launches the
# real collection whenever a request appears (and no worker is already running).
#
# Run it from the checkout:
#   ./pc_run_all_flag_watcher.sh
# or install it as the systemd user service documented in the README.

cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" || exit 1

FLAG="data/queue/run_all_requested.flag"
UPDATE_QUEUE_FLAG="data/queue/update_monitor_requested.flag"
UPDATE_IN_PROGRESS_FLAG="data/queue/update_monitor_in_progress.flag"
REQUEST_LOG="data/logs/run_all_requests.log"
MONITOR_SETTINGS="data/config/monitor_settings.env"

mkdir -p data/logs data/queue

if [ -f "$MONITOR_SETTINGS" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$MONITOR_SETTINGS"
  set +a
fi

DETAIL_LIMIT="${PC_WEBHOOK_DETAIL_LIMIT:-0}"
INDEX_LIMIT="${PC_WEBHOOK_INDEX_LIMIT:-${PC_INDEX_LIMIT:-${PC_MAX_PAGES_PER_GROUP:-0}}}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
POLL_SECONDS="${PC_RUNNER_POLL_SECONDS:-5}"

log() {
  echo "$(date '+%Y-%m-%d %H:%M:%S') | flag-watcher | $1" | tee -a "$REQUEST_LOG"
}

worker_running() {
  pgrep -f "[p]c_run_all_worker.sh" >/dev/null 2>&1
}

updater_running() {
  [ -f "$UPDATE_IN_PROGRESS_FLAG" ] || pgrep -f "[u]pdate_local_copy.sh|[p]c_update_loader.py" >/dev/null 2>&1
}

launch_queued_update_monitor() {
  if [ ! -f "$UPDATE_QUEUE_FLAG" ] || worker_running || updater_running; then
    return 0
  fi
  log "queued Update + Monitor detected; launching updater"
  rm -f "$UPDATE_QUEUE_FLAG"
  if [ -x ./pc_update_loader.py ]; then
    nohup "$PYTHON_BIN" ./pc_update_loader.py --open-monitor-after >> data/logs/update_monitor_queue.log 2>&1 &
  else
    nohup ./update_local_copy.sh >> data/logs/update_monitor_queue.log 2>&1 &
  fi
}

log "started (poll ${POLL_SECONDS}s, index_page_cap ${INDEX_LIMIT}, detail_limit ${DETAIL_LIMIT}); watching $FLAG"

while true; do
  launch_queued_update_monitor
  if [ -f "$FLAG" ] && ! worker_running; then
    log "request flag detected; launching host collector"
    # pc_request_run_all.sh keeps/refreshes the flag and starts the host worker,
    # which consumes the request. Never let one failure stop the watcher.
    PC_RUN_MODE=AUTO PC_INDEX_LIMIT="$INDEX_LIMIT" ./pc_request_run_all.sh "$DETAIL_LIMIT" AUTO "$INDEX_LIMIT" >> "$REQUEST_LOG" 2>&1 || log "pc_request_run_all.sh returned non-zero"
  fi
  sleep "$POLL_SECONDS"
done
