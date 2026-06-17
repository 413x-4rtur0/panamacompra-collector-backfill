#!/usr/bin/env bash
set -euo pipefail

cd "$HOME/Apps/panamacompra-collector" || exit 1

mkdir -p data/logs data/queue/{pending,running,done,failed,stale} deprecated_queue_scripts

echo "============================================================"
echo " PanamaCompra System Review"
echo "============================================================"
echo "Started: $(date '+%Y-%m-%d %H:%M:%S')"
echo ""

echo "1) Active recommended architecture"
echo "----------------------------------"
cat <<'TXT'
changedetection.io
  ↓
webhook_listener.py
  ↓
run_collector.sh
  ↓
pc_request_run_all.sh
  ↓
pc_run_all_worker.sh
  ↓
pc_index_collector.py
  ↓
pc_detail_downloader.py
TXT

echo ""
echo "2) Stopping old queue worker only"
echo "---------------------------------"
pkill -TERM -f "[p]c_queue_worker.sh" 2>/dev/null || true
sleep 1

if pgrep -f "[p]c_queue_worker.sh" >/dev/null 2>&1; then
  echo "Old queue worker still running."
else
  echo "Old queue worker: not running"
fi

echo ""
echo "3) Moving stale old queue running files"
echo "---------------------------------------"
if ! pgrep -f "[p]c_queue_worker.sh" >/dev/null 2>&1; then
  moved=0
  for f in data/queue/running/*.job; do
    [ -e "$f" ] || continue
    mv "$f" "data/queue/stale/stale_$(date +%Y%m%d_%H%M%S)_$(basename "$f")"
    moved=$((moved + 1))
  done
  echo "Moved stale old running queue files: $moved"
else
  echo "Skipped because old queue worker is active."
fi

echo ""
echo "4) Required active scripts"
echo "--------------------------"
required_scripts=(
  "webhook_listener.py"
  "run_collector.sh"
  "pc_request_run_all.sh"
  "pc_run_all_worker.sh"
  "pc_index_collector.py"
  "pc_detail_downloader.py"
  "pc_common.py"
  "pc_monitor_window.sh"
  "pc_open_monitor.sh"
  "pc_run_all_status.sh"
  "pc_stop_run_all.sh"
)

for f in "${required_scripts[@]}"; do
  if [ -f "$f" ]; then
    echo "OK: $f"
  else
    echo "MISSING: $f"
  fi
done

echo ""
echo "5) Deprecated old queue scripts"
echo "-------------------------------"
deprecated_scripts=(
  "pc_enqueue.sh"
  "pc_queue_worker.sh"
  "pc_queue_status.sh"
  "pc_requeue_running.sh"
  "pc_run_index.sh"
  "pc_run_detail.sh"
  "pc_run_sequence.sh"
)

for f in "${deprecated_scripts[@]}"; do
  if [ -f "$f" ]; then
    echo "Deprecated but present: $f"
  fi
done

echo ""
echo "6) Python compile check"
echo "-----------------------"
if [ -f ".venv/bin/activate" ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

python_files=(
  "pc_common.py"
  "pc_index_collector.py"
  "pc_detail_downloader.py"
  "webhook_listener.py"
)

for f in "${python_files[@]}"; do
  if [ -f "$f" ]; then
    python -m py_compile "$f"
    echo "COMPILE OK: $f"
  fi
done

if [ -f "migrate_previous_records.py" ]; then
  python -m py_compile migrate_previous_records.py
  echo "COMPILE OK: migrate_previous_records.py"
fi

echo ""
echo "7) Current related processes"
echo "----------------------------"
pgrep -af "pc_run_all_worker|pc_index_collector|pc_detail_downloader|pc_queue_worker|pc_monitor_window|timeout .*pc_" || echo "No related active process."

echo ""
echo "8) Current run-all status"
echo "-------------------------"
if [ -x "./pc_run_all_status.sh" ]; then
  ./pc_run_all_status.sh
else
  echo "pc_run_all_status.sh missing."
fi

echo ""
echo "9) Queue folder files"
echo "---------------------"
find data/queue -maxdepth 2 -type f -printf "%p\n" | sort || true

echo ""
echo "10) Recommended commands"
echo "------------------------"
cat <<'TXT'
Manual small test:
  ./pc_request_run_all.sh 5
  ./pc_open_monitor.sh

Run all pending details:
  ./pc_request_run_all.sh

Watch in terminal:
  ./pc_follow_run_all.sh

Check status:
  ./pc_run_all_status.sh

Stop if stuck:
  ./pc_stop_run_all.sh
TXT

echo ""
echo "Finished: $(date '+%Y-%m-%d %H:%M:%S')"
echo "============================================================"
