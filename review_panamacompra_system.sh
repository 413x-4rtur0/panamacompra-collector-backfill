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
       STEP 0: pc_update_before_run.sh
       STEP 1: pc_index_collector.py
       STEP 2: pc_detail_downloader.py
       STEP 3: pc_build_calendar.py -> data/calendar/YY-MM-DD/*.ics
       STEP 4: pc_test_zone.py -> records_test/latest_5 + records_test/calendar/YY-MM-DD/*.ics (idle/no-new-records only)
TXT

echo ""
echo "2) Required active scripts"
echo "--------------------------"
required_scripts=(
  "webhook_listener.py"
  "run_collector.sh"
  "pc_request_run_all.sh"
  "pc_run_all_worker.sh"
  "pc_update_before_run.sh"
  "pc_waha_notify.py"
  "pc_notify_new_records.py"
  "pc_index_collector.py"
  "pc_detail_downloader.py"
  "pc_build_calendar.py"
  "pc_test_zone.py"
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
PYTHON_BIN="${PYTHON_BIN:-python3}"
if [ -f ".venv/bin/activate" ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
  PYTHON_BIN="python"
fi

python_files=(
  "pc_common.py"
  "pc_index_collector.py"
  "pc_detail_downloader.py"
  "pc_monitor_tk.py"
  "pc_monitor_server.py"
  "pc_waha_notify.py"
  "pc_notify_new_records.py"
  "webhook_listener.py"
  "migrate_previous_records.py"
  "pc_rename_record_folders.py"
  "pc_build_detail_views.py"
  "pc_update_day_folder.py"
  "pc_build_calendar.py"
  "pc_test_zone.py"
)

for f in "${python_files[@]}"; do
  if [ -f "$f" ]; then
    if "$PYTHON_BIN" -m py_compile "$f" 2>/dev/null; then
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
pgrep -af "webhook_listener|pc_run_all_worker|pc_index_collector|pc_detail_downloader|pc_monitor_window|pc_monitor_tk|pc_monitor_server|timeout .*pc_" || echo "No related active process."


echo "6b) Webhook listener health"
echo "---------------------------"
WEBHOOK_HOST="${PC_WEBHOOK_HOST:-127.0.0.1}"
[ "$WEBHOOK_HOST" = "0.0.0.0" ] && WEBHOOK_HOST="127.0.0.1"
WEBHOOK_PORT="${PC_WEBHOOK_PORT:-8765}"
if pgrep -f "[w]ebhook_listener.py" >/dev/null 2>&1; then
  echo "PROCESS OK: webhook_listener.py is running."
  if command -v curl >/dev/null 2>&1; then
    if curl -fsS --max-time 2 "http://${WEBHOOK_HOST}:${WEBHOOK_PORT}/health" >/dev/null 2>&1; then
      echo "HEALTH OK: http://${WEBHOOK_HOST}:${WEBHOOK_PORT}/health"
    else
      echo "HEALTH WARN: process is running but /health did not respond at http://${WEBHOOK_HOST}:${WEBHOOK_PORT}/health"
    fi
  else
    echo "HEALTH SKIP: curl is not installed."
  fi
else
  echo "PROCESS WARN: webhook_listener.py is not running; changedetection.io cannot autorun the collector."
  echo "Start it with: python3 webhook_listener.py"
fi

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
Stop collector only: ./pc_stop_run_all.sh
Stop incl. webhook:  PC_STOP_WEBHOOK=1 ./pc_stop_run_all.sh
TXT

echo ""
echo "Finished: $(date '+%Y-%m-%d %H:%M:%S')"
echo "============================================================"
