#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
# shellcheck source=lib/env.sh
source "$SCRIPT_DIR/lib/env.sh"
cd "$APP_ROOT"

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
  -> src/webhook/listener.py
  -> src/webhook/run-collector.sh
  -> src/pipeline/request-run-all.sh
  -> src/pipeline/run-worker.sh
       STEP 0: src/pipeline/000-update-before-run.sh
       STEP 1: src/pipeline/010-collect-index.py
       STEP 2: src/pipeline/collect_detail.py
       STEP 3: src/pipeline/030-build-detail-views.py
       STEP 4: src/pipeline/040-repair-missing-deadlines.py (verify/repair)
       STEP 5: src/pipeline/build_calendar.py -> data/calendar/YY-MM-DD/*.ics
       STEP 6: src/pipeline/notify_new_records.py --announce (WhatsApp)
       STEP 7: src/pipeline/070-test-zone.py -> records_test/latest_5 + records_test/calendar/YY-MM-DD/*.ics (idle/no-new-records only)
TXT

echo ""
echo "2) Required active scripts"
echo "--------------------------"
required_scripts=(
  "src/webhook/listener.py"
  "src/webhook/run-collector.sh"
  "src/pipeline/request-run-all.sh"
  "src/pipeline/run-worker.sh"
  "src/pipeline/000-update-before-run.sh"
  "src/notify/waha_client.py"
  "src/pipeline/notify_new_records.py"
  "src/pipeline/010-collect-index.py"
  "src/pipeline/collect_detail.py"
  "src/pipeline/030-build-detail-views.py"
  "src/pipeline/040-repair-missing-deadlines.py"
  "src/pipeline/build_calendar.py"
  "src/pipeline/070-test-zone.py"
  "src/common.py"
  "src/monitor/001c-monitor-terminal.sh"
  "src/monitor/001a-monitor-tk.py"
  "src/monitor/001b-monitor-web.py"
  "src/monitor/open-monitor.sh"
  "src/monitor/next-run-timer.py"
  "src/monitor/update-loader.py"
  "src/pipeline/run-all-status.sh"
  "src/pipeline/queue-status.sh"
  "src/pipeline/stop-run-all.sh"
  "src/pipeline/stop-collectors.sh"
  "src/pipeline/follow-run-all.sh"
  "src/pipeline/run-now.sh"
  "src/webhook/start-listener.sh"
  "src/webhook/install-service.sh"
  "src/webhook/watch-queue-flag.sh"
  "src/webhook/diagnose.sh"
  "src/tools/rename-record-folders.py"
  "src/tools/maintain-database.py"
  "src/tools/update_day_folder.py"
  "src/tools/reset.py"
  "src/tools/import-selected-calendars.py"
  "src/tools/migrate-apps-layout.sh"
  "src/tools/migrate-previous-records.py"
  "src/tools/migrate-previous-records.sh"
  "src/tools/setup-git-credentials.sh"
  "setup.sh"
  "update-local-copy.sh"
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
  "pc_requeue_running.sh"
  "pc_run_index.sh"
  "pc_run_detail.sh"
  "pc_run_sequence.sh"
  "pc_common.py"
  "pc_index_collector.py"
  "pc_detail_downloader.py"
  "pc_run_all_worker.sh"
  "pc_request_run_all.sh"
  "webhook_listener.py"
  "run_collector.sh"
  "update_local_copy.sh"
  "review_panamacompra_system.sh"
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

python_files=()
while IFS= read -r -d '' f; do
  python_files+=("$f")
done < <(find src -name '*.py' -print0)

for f in "${python_files[@]}"; do
  if "$PYTHON_BIN" -m py_compile "$f" 2>/dev/null; then
    echo "COMPILE OK: $f"
  else
    echo "COMPILE FAIL: $f"
  fi
done

echo ""
echo "5) Shell syntax check"
echo "---------------------"
shell_files=()
while IFS= read -r -d '' f; do
  shell_files+=("$f")
done < <(find . -maxdepth 4 -name '*.sh' -not -path './.venv/*' -not -path './.git/*' -print0)

for f in "${shell_files[@]}"; do
  if bash -n "$f" 2>/dev/null; then
    echo "SYNTAX OK: $f"
  else
    echo "SYNTAX FAIL: $f"
  fi
done

echo ""
echo "6) Current related processes"
echo "----------------------------"
pgrep -af "run-worker.sh|010-collect-index.py|collect_detail.py|001c-monitor-terminal.sh|001a-monitor-tk.py|001b-monitor-web.py|timeout .*src/pipeline" || echo "No related active process."

echo ""
echo "7) Current run-all status"
echo "-------------------------"
if [ -x "./src/pipeline/run-all-status.sh" ]; then
  ./src/pipeline/run-all-status.sh
else
  echo "src/pipeline/run-all-status.sh missing."
fi

echo ""
echo "8) Recommended commands"
echo "-----------------------"
cat <<'TXT'
Manual small test:   ./src/pipeline/request-run-all.sh 5
Run all pending:     ./src/pipeline/request-run-all.sh
Open native monitor: ./src/monitor/open-monitor.sh
Open web monitor:    PC_MONITOR_MODE=web ./src/monitor/open-monitor.sh
Watch in terminal:   PC_MONITOR_MODE=terminal ./src/monitor/open-monitor.sh
Follow logs:         ./src/pipeline/follow-run-all.sh
Check status:        ./src/pipeline/run-all-status.sh
Stop if stuck:       ./src/pipeline/stop-run-all.sh
Or use the unified CLI: ./bin/pcc <start|stop|status|monitor|webhook|...>
TXT

echo ""
echo "Finished: $(date '+%Y-%m-%d %H:%M:%S')"
echo "============================================================"
