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
#   ./src/10_webhook/050-watch-queue-flag.sh
# or install it as the systemd user service documented in the README.

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
# shellcheck source=../../lib/env.sh
source "$SCRIPT_DIR/../../lib/env.sh"
cd "$APP_ROOT"

FLAG="$PC_QUEUE_DIR/run_all_requested.flag"
UPDATE_QUEUE_FLAG="$PC_QUEUE_DIR/update_monitor_requested.flag"
UPDATE_IN_PROGRESS_FLAG="$PC_QUEUE_DIR/update_monitor_in_progress.flag"
REQUEST_LOG="$PC_LOG_DIR/run_all_requests.log"
MONITOR_SETTINGS="$PC_DATA_DIR/config/monitor_settings.env"

if [ -f "$MONITOR_SETTINGS" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$MONITOR_SETTINGS"
  set +a
fi

DETAIL_LIMIT="${PC_WEBHOOK_DETAIL_LIMIT:-0}"
INDEX_LIMIT="${PC_WEBHOOK_INDEX_LIMIT:-${PC_INDEX_LIMIT:-${PC_MAX_PAGES_PER_GROUP:-0}}}"
AUTORUN_SOURCE="${PC_AUTORUN_SOURCE:-changedetection}"
CHANGEDETECTION_MONITOR_MODE="${PC_CHANGEDETECTION_MONITOR_MODE:-terminal}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
POLL_SECONDS="${PC_RUNNER_POLL_SECONDS:-5}"

log() {
  echo "$(date '+%Y-%m-%d %H:%M:%S') | flag-watcher | $1" | tee -a "$REQUEST_LOG"
}

worker_running() {
  pgrep -f "[r]un-worker.sh" >/dev/null 2>&1
}

updater_running() {
  [ -f "$UPDATE_IN_PROGRESS_FLAG" ] || pgrep -f "[u]pdate-local-copy.sh|[u]pdate-loader.py" >/dev/null 2>&1
}

launch_queued_update_monitor() {
  if [ ! -f "$UPDATE_QUEUE_FLAG" ] || worker_running || updater_running; then
    return 0
  fi
  log "queued Update + Monitor detected; launching updater"
  rm -f "$UPDATE_QUEUE_FLAG"
  if [ -x "$APP_ROOT/src/40_monitor/003-update-loader.py" ]; then
    nohup "$PYTHON_BIN" "$APP_ROOT/src/40_monitor/003-update-loader.py" --open-monitor-after >> "$PC_LOG_DIR/update_monitor_queue.log" 2>&1 &
  else
    nohup "$APP_ROOT/update-local-copy.sh" >> "$PC_LOG_DIR/update_monitor_queue.log" 2>&1 &
  fi
}

log "started (poll ${POLL_SECONDS}s, source ${AUTORUN_SOURCE}, index_page_cap ${INDEX_LIMIT}, detail_limit ${DETAIL_LIMIT}); watching $FLAG"

while true; do
  if [ -f "$MONITOR_SETTINGS" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$MONITOR_SETTINGS"
    set +a
    AUTORUN_SOURCE="${PC_AUTORUN_SOURCE:-changedetection}"
    CHANGEDETECTION_MONITOR_MODE="${PC_CHANGEDETECTION_MONITOR_MODE:-terminal}"
  fi
  launch_queued_update_monitor
  if [ -f "$FLAG" ] && ! worker_running; then
    if [ "$AUTORUN_SOURCE" = "cron" ]; then
      log "request flag ignored because PC_AUTORUN_SOURCE=cron; remove flag and leave cron as the only automatic runner"
      rm -f "$FLAG"
      sleep "$POLL_SECONDS"
      continue
    fi
    log "request flag detected; launching host collector"
    # 110a-request-run.sh keeps/refreshes the flag and starts the host worker,
    # which consumes the request. Never let one failure stop the watcher.
    PC_MONITOR_MODE="$CHANGEDETECTION_MONITOR_MODE" PC_RUN_MODE=AUTO PC_INDEX_LIMIT="$INDEX_LIMIT" "$APP_ROOT/src/20_pipeline/110a-request-run.sh" "$DETAIL_LIMIT" AUTO "$INDEX_LIMIT" >> "$REQUEST_LOG" 2>&1 || log "110a-request-run.sh returned non-zero"
  fi
  sleep "$POLL_SECONDS"
done
