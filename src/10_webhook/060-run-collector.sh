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

if [ "${PC_AUTORUN_SOURCE:-changedetection}" = "cron" ]; then
  echo "===== changedetection webhook ignored at $(date '+%Y-%m-%d %H:%M:%S') because PC_AUTORUN_SOURCE=cron =====" >> "$PC_LOG_DIR/collector_triggered.log"
  exit 0
fi

# Changedetection/AUTO runs should not use the manual/test limit controls.
# 0 means: index every available page and download every pending detail row.
DETAIL_LIMIT="${PC_WEBHOOK_DETAIL_LIMIT:-0}"
INDEX_LIMIT="${PC_WEBHOOK_INDEX_LIMIT:-0}"
CHANGEDETECTION_MONITOR_MODE="${PC_CHANGEDETECTION_MONITOR_MODE:-terminal}"

echo "===== changedetection webhook received at $(date '+%Y-%m-%d %H:%M:%S') =====" >> "$PC_LOG_DIR/collector_triggered.log"
echo "Requesting full sequence: index + detail, index_page_cap=$INDEX_LIMIT, detail_limit=$DETAIL_LIMIT" >> "$PC_LOG_DIR/collector_triggered.log"

PC_MONITOR_MODE="$CHANGEDETECTION_MONITOR_MODE" PC_RUN_MODE=AUTO PC_INDEX_LIMIT="$INDEX_LIMIT" "$APP_ROOT/src/20_pipeline/110a-request-run.sh" "$DETAIL_LIMIT" AUTO "$INDEX_LIMIT" >> "$PC_LOG_DIR/collector_triggered.log" 2>&1

echo "Full sequence requested at $(date '+%Y-%m-%d %H:%M:%S')" >> "$PC_LOG_DIR/collector_triggered.log"
