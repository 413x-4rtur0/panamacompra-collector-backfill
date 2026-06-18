#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" || exit 1

mkdir -p data/logs data/queue

echo "============================================================"
echo " PanamaCompra System Review"
echo "============================================================"
echo "Started: $(date '+%Y-%m-%d %H:%M:%S')"
echo "Project: $(pwd)"
echo ""

echo "1) Active architecture"
echo "----------------------"
cat <<'TXT'
changedetection.io
  -> webhook_listener.py
  -> run_collector.sh
  -> pc_request_run_all.sh
  -> pc_run_all_worker.sh
       STEP 1: pc_index_collector.py
       STEP 2: pc_detail_downloader.py
TXT

echo ""
echo "2) Required active scripts"
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
  "pc_monitor_tk.py"
  "pc_monitor_server.py"
  "pc_open_monitor.sh"
  "pc_run_all_status.sh"
  "pc_stop_run_all.sh"
)

for f in "${required_scripts[@]}"; do
  if [ -f "$f" ]; then
    echo "OK:      $f"
  else
    echo "MISSING: $f"
  fi
done

echo ""
echo "3) Deprecated scripts (should not be present)"
echo "---------------------------------------------"
deprecated_scripts=(
  "pc_enqueue.sh"
  "pc_queue_worker.sh"
  "pc_queue_status.sh"
  "pc_requeue_running.sh"
  "pc_run_index.sh"
  "pc_run_detail.sh"
  "pc_run_sequence.sh"
)

found_deprecated=0
for f in "${deprecated_scripts[@]}"; do
  if [ -f "$f" ]; then
    echo "PRESENT: $f"
    found_deprecated=$((found_deprecated + 1))
  fi
done
[ "$found_deprecated" -eq 0 ] && echo "None present. Good."

echo ""
echo "4) Python compile check"
echo "-----------------------"
if [ -f ".venv/bin/activate" ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

python_files=(
  "pc_common.py"
  "pc_index_collector.py"
  "pc_detail_downloader.py"
  "pc_monitor_tk.py"
  "pc_monitor_server.py"
  "webhook_listener.py"
  "migrate_previous_records.py"
)

for f in "${python_files[@]}"; do
  if [ -f "$f" ]; then
    if python -m py_compile "$f" 2>/dev/null; then
      echo "COMPILE OK: $f"
    else
      echo "COMPILE FAIL: $f"
    fi
  fi
done

echo ""
echo "5) Shell syntax check"
echo "---------------------"
for f in *.sh; do
  if bash -n "$f" 2>/dev/null; then
    echo "SYNTAX OK: $f"
  else
    echo "SYNTAX FAIL: $f"
  fi
done

echo ""
echo "6) Current related processes"
echo "----------------------------"
pgrep -af "pc_run_all_worker|pc_index_collector|pc_detail_downloader|pc_monitor_window|pc_monitor_tk|pc_monitor_server|timeout .*pc_" || echo "No related active process."

echo ""
echo "7) Current run-all status"
echo "-------------------------"
if [ -x "./pc_run_all_status.sh" ]; then
  ./pc_run_all_status.sh
else
  echo "pc_run_all_status.sh missing."
fi

echo ""
echo "8) Recommended commands"
echo "-----------------------"
cat <<'TXT'
Manual small test:   ./pc_request_run_all.sh 5
Run all pending:     ./pc_request_run_all.sh
Open native monitor: ./pc_open_monitor.sh
Open web monitor:    PC_MONITOR_MODE=web ./pc_open_monitor.sh
Watch in terminal:   PC_MONITOR_MODE=terminal ./pc_open_monitor.sh
Follow logs:         ./pc_follow_run_all.sh
Check status:        ./pc_run_all_status.sh
Stop if stuck:       ./pc_stop_run_all.sh
TXT

echo ""
echo "Finished: $(date '+%Y-%m-%d %H:%M:%S')"
echo "============================================================"
