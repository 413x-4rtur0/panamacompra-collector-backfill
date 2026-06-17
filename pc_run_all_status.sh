#!/usr/bin/env bash
set -euo pipefail

cd "$HOME/Apps/panamacompra-collector" || exit 1

mkdir -p data/logs data/queue

echo "PanamaCompra run-all status"
echo "---------------------------"

if pgrep -f "[p]c_run_all_worker.sh" >/dev/null 2>&1; then
  echo "Worker: RUNNING"
else
  echo "Worker: not running"
fi

if pgrep -f "[p]ython -u ./pc_index_collector.py" >/dev/null 2>&1; then
  echo "Index collector: RUNNING"
else
  echo "Index collector: not running"
fi

if pgrep -f "[p]ython -u ./pc_detail_downloader.py" >/dev/null 2>&1; then
  echo "Detail downloader: RUNNING"
else
  echo "Detail downloader: not running"
fi

if [ -f data/queue/run_all_requested.flag ]; then
  echo "Pending request flag: YES"
else
  echo "Pending request flag: no"
fi

echo ""
echo "Related processes:"
pgrep -af "pc_run_all_worker|pc_index_collector|pc_detail_downloader|timeout .*pc_" || true

echo ""
echo "Recent run-all requests:"
tail -20 data/logs/run_all_requests.log 2>/dev/null || echo "No request log yet."

echo ""
echo "Recent worker log:"
tail -40 data/logs/run_all_worker.log 2>/dev/null || echo "No worker log yet."

echo ""
echo "Current run log:"
tail -100 data/logs/run_all_current.log 2>/dev/null || echo "No current log yet."
