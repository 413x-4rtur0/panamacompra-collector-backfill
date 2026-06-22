#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" || exit 1

mkdir -p data/logs data/queue

echo "Stopping all PanamaCompra runners and background processes..."

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
# STEP 6: Stop webhook listener (background HTTP receiver)
# ============================================================================
echo "6) Stopping webhook listener..."
pkill -TERM -f "[p]ython3? -u ./webhook_listener.py" 2>/dev/null || true
pkill -TERM -f "[w]ebhook_listener.py" 2>/dev/null || true

# Give processes time to terminate gracefully
sleep 3

# Force kill any remaining processes that didn't respond to TERM
echo "Force-killing any remaining stubborn processes..."
pkill -9 -f "[p]c_run_all_worker.sh" 2>/dev/null || true
pkill -9 -f "[p]ython3? -u ./pc_index_collector.py" 2>/dev/null || true
pkill -9 -f "[p]ython3? -u ./pc_detail_downloader.py" 2>/dev/null || true
pkill -9 -f "[p]c_test_zone.py" 2>/dev/null || true
pkill -9 -f "[p]c_build_calendar.py" 2>/dev/null || true
pkill -9 -f "[u]pdate_local_copy.sh" 2>/dev/null || true
pkill -9 -f "[p]c_monitor_tk.py" 2>/dev/null || true
pkill -9 -f "[w]ebhook_listener.py" 2>/dev/null || true

sleep 2

# Clear in-progress flag after all workers have had time to exit
# Manual stops are intentional, not resumable abrupt exits
rm -f data/queue/run_all_requested.flag data/queue/run_all_in_progress.flag

echo "$(date '+%Y-%m-%d %H:%M:%S') | All runners stopped manually." >> data/logs/run_all_worker.log

echo ""
echo "============================================================"
echo "All PanamaCompra processes stopped."
echo "============================================================"
echo "Remaining related processes (should be empty):"
pgrep -af "pc_run_all|pc_test_zone|pc_build_calendar|pc_monitor|webhook_listener|update_local" || echo "  None found - all stopped successfully."
