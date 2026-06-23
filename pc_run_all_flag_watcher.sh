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
REQUEST_LOG="data/logs/run_all_requests.log"
DETAIL_LIMIT="${PC_WEBHOOK_DETAIL_LIMIT:-99}"
POLL_SECONDS="${PC_RUNNER_POLL_SECONDS:-5}"

mkdir -p data/logs data/queue

log() {
  echo "$(date '+%Y-%m-%d %H:%M:%S') | flag-watcher | $1" | tee -a "$REQUEST_LOG"
}

worker_running() {
  pgrep -f "[p]c_run_all_worker.sh" >/dev/null 2>&1
}

log "started (poll ${POLL_SECONDS}s, detail_limit ${DETAIL_LIMIT}); watching $FLAG"

while true; do
  if [ -f "$FLAG" ] && ! worker_running; then
    log "request flag detected; launching host collector"
    # pc_request_run_all.sh keeps/refreshes the flag and starts the host worker,
    # which consumes the request. Never let one failure stop the watcher.
    ./pc_request_run_all.sh "$DETAIL_LIMIT" >> "$REQUEST_LOG" 2>&1 || log "pc_request_run_all.sh returned non-zero"
  fi
  sleep "$POLL_SECONDS"
done
