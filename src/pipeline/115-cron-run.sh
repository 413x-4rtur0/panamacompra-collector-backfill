#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
# shellcheck source=../../lib/env.sh
source "$SCRIPT_DIR/../../lib/env.sh"
cd "$APP_ROOT"

MONITOR_SETTINGS="$PC_DATA_DIR/config/monitor_settings.env"
if [ -f "$MONITOR_SETTINGS" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$MONITOR_SETTINGS"
  set +a
fi

REQUEST_LOG="$PC_LOG_DIR/run_all_requests.log"
AUTORUN_SOURCE="${PC_AUTORUN_SOURCE:-changedetection}"
DETAIL_LIMIT="${PC_CRON_DETAIL_LIMIT:-0}"
INDEX_LIMIT="${PC_CRON_INDEX_LIMIT:-0}"

log() {
  echo "$(date '+%Y-%m-%d %H:%M:%S') | cron-run | $1" | tee -a "$REQUEST_LOG"
}

if [ "$AUTORUN_SOURCE" != "cron" ]; then
  log "ignored: PC_AUTORUN_SOURCE=$AUTORUN_SOURCE (changedetection/webhook is active)"
  exit 0
fi

log "requesting cron collector run index_page_cap=$INDEX_LIMIT detail_limit=$DETAIL_LIMIT"
PC_RUN_MODE=AUTO PC_INDEX_LIMIT="$INDEX_LIMIT" "$APP_ROOT/src/pipeline/110a-request-run.sh" "$DETAIL_LIMIT" AUTO "$INDEX_LIMIT"
