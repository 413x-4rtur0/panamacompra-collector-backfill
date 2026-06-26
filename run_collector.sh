#!/usr/bin/env bash
set -euo pipefail

ROOT="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
cd "$ROOT" || exit 1
# shellcheck source=lib/env.sh
source "$ROOT/lib/env.sh"

mkdir -p "$PC_LOG_DIR"

# Changedetection/AUTO runs should not use the manual/test limit controls.
# 0 means: index every available page and download every pending detail row.
DETAIL_LIMIT="0"
INDEX_LIMIT="0"

echo "===== changedetection webhook received at $(date '+%Y-%m-%d %H:%M:%S') =====" >> "$PC_LOG_DIR/collector_triggered.log"
echo "Requesting full sequence: index + detail, index_page_cap=$INDEX_LIMIT, detail_limit=$DETAIL_LIMIT" >> "$PC_LOG_DIR/collector_triggered.log"

PC_RUN_MODE=AUTO PC_INDEX_LIMIT="$INDEX_LIMIT" "$APP_ROOT/pc_request_run_all.sh" "$DETAIL_LIMIT" AUTO "$INDEX_LIMIT" >> "$PC_LOG_DIR/collector_triggered.log" 2>&1

echo "Full sequence requested at $(date '+%Y-%m-%d %H:%M:%S')" >> "$PC_LOG_DIR/collector_triggered.log"
