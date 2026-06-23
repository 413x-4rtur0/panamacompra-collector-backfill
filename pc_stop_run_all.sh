#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" || exit 1

mkdir -p data/logs data/queue

STOP_WEBHOOK="${PC_STOP_WEBHOOK:-0}"

echo "Stopping PanamaCompra collector runners and background processes..."

# Clear request flags first to prevent restarts
rm -f data/queue/run_all_requested.flag

# ============================================================================
# STEP 1: Stop main collector workers (run-all pipeline)
# ============================================================================
echo "1) Stopping run-all worker and collector processes..."
pkill -TERM -f "[p]c_run_all_worker.sh" 2>/dev/null || true
pkill -TERM -f "[p]ython3? -u ./pc_index_collector.py" 2>/dev/null || true
pkill -TERM -f "[p]ython3? -u ./pc_detail_downloader.py" 2>/dev/null || true
pkill -TERM -f "[t]imeout .*pc_index_collector.py" 2>/dev/null || true
pkill -TERM -f "[t]imeout .*pc_detail_downloader.py" 2>/dev/null || true

# ============================================================================
# STEP 2: Stop test zone runner (isolated sandbox testing)
# ============================================================================
echo "2) Stopping test zone runner..."
pkill -TERM -f "[p]ython3? -u ./pc_test_zone.py" 2>/dev/null || true
pkill -TERM -f "[p]c_test_zone.py" 2>/dev/null || true

# ============================================================================
# STEP 3: Stop calendar builder (ICS package generation)
# ============================================================================
echo "3) Stopping calendar builder..."
pkill -TERM -f "[p]ython3? -u ./pc_build_calendar.py" 2>/dev/null || true
pkill -TERM -f "[p]c_build_calendar.py" 2>/dev/null || true

# ============================================================================
# STEP 4: Stop updater processes (local copy refresh)
# ============================================================================
echo "4) Stopping updater processes..."
pkill -TERM -f "[u]pdate_local_copy.sh" 2>/dev/null || true
pkill -TERM -f "[p]c_update_loader.py" 2>/dev/null || true
pkill -TERM -f "[p]c_update_before_run.sh" 2>/dev/null || true

# ============================================================================
# STEP 5: Stop monitor processes (GUI and web interfaces)
# ============================================================================
echo "5) Stopping monitor processes..."
pkill -TERM -f "[p]ython3? -u ./pc_monitor_tk.py" 2>/dev/null || true
pkill -TERM -f "[p]c_monitor_tk.py" 2>/dev/null || true
pkill -TERM -f "[p]ython3? -u ./pc_monitor_server.py" 2>/dev/null || true
pkill -TERM -f "[p]c_monitor_server.py" 2>/dev/null || true
pkill -TERM -f "[p]c_next_run_timer.py" 2>/dev/null || true
pkill -TERM -f "[p]c_follow_run_all.sh" 2>/dev/null || true

# ============================================================================
# STEP 6: Keep webhook listener alive by default so changedetection.io autorun
# remains connected after a manual STOP. Set PC_STOP_WEBHOOK=1 only when you
# intentionally want to stop the HTTP receiver too.
# ============================================================================
if [ "$STOP_WEBHOOK" = "1" ]; then
  echo "6) Stopping webhook listener (PC_STOP_WEBHOOK=1)..."
  pkill -TERM -f "[p]ython3? -u ./webhook_listener.py" 2>/dev/null || true
  pkill -TERM -f "[w]ebhook_listener.py" 2>/dev/null || true
else
  echo "6) Keeping webhook listener running (set PC_STOP_WEBHOOK=1 to stop it)."
fi

# Give processes a short window to terminate gracefully. Keep this snappy so the
# STOP action (and update_local_copy.sh, which relies on a fast stop) does not
# appear to hang; stubborn processes are force-killed right after.
sleep 2

# Force kill any remaining processes that didn't respond to TERM
echo "Force-killing any remaining stubborn processes..."
pkill -9 -f "[p]c_run_all_worker.sh" 2>/dev/null || true
pkill -9 -f "[p]ython3? -u ./pc_index_collector.py" 2>/dev/null || true
pkill -9 -f "[p]ython3? -u ./pc_detail_downloader.py" 2>/dev/null || true
pkill -9 -f "[p]c_test_zone.py" 2>/dev/null || true
pkill -9 -f "[p]c_build_calendar.py" 2>/dev/null || true
pkill -9 -f "[u]pdate_local_copy.sh" 2>/dev/null || true
pkill -9 -f "[p]c_monitor_tk.py" 2>/dev/null || true
if [ "$STOP_WEBHOOK" = "1" ]; then
  pkill -9 -f "[w]ebhook_listener.py" 2>/dev/null || true
fi

sleep 1

# Clear in-progress flag after all workers have had time to exit
# Manual stops are intentional, not resumable abrupt exits
rm -f data/queue/run_all_requested.flag data/queue/run_all_in_progress.flag

echo "$(date '+%Y-%m-%d %H:%M:%S') | All runners stopped manually." >> data/logs/run_all_worker.log

echo ""
echo "============================================================"
if [ "$STOP_WEBHOOK" = "1" ]; then
  echo "All PanamaCompra processes stopped, including webhook listener."
else
  echo "Collector processes stopped. Webhook listener was preserved for changedetection.io autorun."
fi
echo "============================================================"
echo "Remaining related processes (should be empty):"
pgrep -af "pc_run_all|pc_test_zone|pc_build_calendar|pc_monitor|update_local" || echo "  None found - collector processes stopped successfully."
echo "Webhook listener status:"
pgrep -af "webhook_listener" || echo "  Not running (start from monitor Data Tools / Settings or run: python3 webhook_listener.py)"
