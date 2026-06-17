#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" || exit 1

mkdir -p data/logs

echo "Following run-all logs."
echo "Press Ctrl+C to stop watching. The process will continue."
echo ""

touch data/logs/run_all_worker.log data/logs/run_all_current.log

tail -f data/logs/run_all_worker.log data/logs/run_all_current.log
