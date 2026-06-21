#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" || exit 1

mkdir -p data/logs data/queue

echo "Stopping PanamaCompra run-all process..."

rm -f data/queue/run_all_requested.flag

pkill -TERM -f "[p]ython3? -u ./pc_index_collector.py" 2>/dev/null || true
pkill -TERM -f "[p]ython3? -u ./pc_detail_downloader.py" 2>/dev/null || true
pkill -TERM -f "[t]imeout .*pc_index_collector.py" 2>/dev/null || true
pkill -TERM -f "[t]imeout .*pc_detail_downloader.py" 2>/dev/null || true

sleep 5

pkill -TERM -f "[p]c_run_all_worker.sh" 2>/dev/null || true

sleep 2

# Manual stops are intentional, not resumable abrupt exits. The worker's
# shutdown trap may briefly restore the request flag, so clear both markers
# after the worker has had time to exit.
rm -f data/queue/run_all_requested.flag data/queue/run_all_in_progress.flag

echo "$(date '+%Y-%m-%d %H:%M:%S') | Manual stop requested." >> data/logs/run_all_worker.log

echo "Remaining related processes:"
pgrep -af "pc_run_all_worker|pc_index_collector|pc_detail_downloader" || true
