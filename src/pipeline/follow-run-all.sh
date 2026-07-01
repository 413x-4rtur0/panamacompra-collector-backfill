#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
# shellcheck source=../../lib/env.sh
source "$SCRIPT_DIR/../../lib/env.sh"
cd "$APP_ROOT"

echo "Following run-all logs."
echo "Press Ctrl+C to stop watching. The process will continue."
echo ""

touch "$PC_LOG_DIR/run_all_worker.log" "$PC_LOG_DIR/run_all_current.log"

tail -f "$PC_LOG_DIR/run_all_worker.log" "$PC_LOG_DIR/run_all_current.log"
