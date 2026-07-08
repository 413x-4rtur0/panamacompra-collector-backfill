#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
# shellcheck source=../../lib/env.sh
source "$SCRIPT_DIR/../../lib/env.sh"
cd "$APP_ROOT"

echo "Stopping all PanamaCompra runners and background processes..."

# Clear request flags first to prevent restarts
touch "$PC_QUEUE_DIR/run_all_stop_no_resume.flag"
rm -f "$PC_QUEUE_DIR/run_all_requested.flag"

# ============================================================================
# STEP 1: Stop main collector workers (run-all pipeline)
# ============================================================================
echo "1) Stopping run-all worker and collector processes..."
pkill -TERM -f "[r]un-worker.sh" 2>/dev/null || true
pkill -TERM -f "[w]atch-queue-flag.sh" 2>/dev/null || true
pkill -TERM -f "[p]ython3? -u .*010-collect-index.py" 2>/dev/null || true
pkill -TERM -f "[p]ython3? -u .*030-collect-details.py" 2>/dev/null || true
pkill -TERM -f "[t]imeout .*010-collect-index.py" 2>/dev/null || true
pkill -TERM -f "[t]imeout .*030-collect-details.py" 2>/dev/null || true

# ============================================================================
# STEP 2: Stop test zone runner (isolated sandbox testing)
# ============================================================================
echo "2) Stopping test zone runner..."
pkill -TERM -f "[p]ython3? -u .*070-test-zone.py" 2>/dev/null || true
pkill -TERM -f "[0]70-test-zone.py" 2>/dev/null || true

# ============================================================================
# STEP 3: Stop calendar builder (ICS package generation)
# ============================================================================
echo "3) Stopping calendar builder..."
pkill -TERM -f "[p]ython3? -u .*060-build-calendar.py" 2>/dev/null || true
pkill -TERM -f "[b]uild_calendar.py" 2>/dev/null || true

# ============================================================================
# STEP 4: Stop updater processes (local copy refresh)
# ============================================================================
echo "4) Stopping updater processes..."
pkill -TERM -f "[u]pdate-local-copy.sh" 2>/dev/null || true
pkill -TERM -f "[u]pdate-loader.py" 2>/dev/null || true
pkill -TERM -f "[0]00-update-before-run.sh" 2>/dev/null || true

# ============================================================================
# STEP 5: Stop monitor processes (GUI and web interfaces)
# ============================================================================
echo "5) Stopping monitor processes..."
pkill -TERM -f "[p]ython3? -u .*001a-monitor-tk.py" 2>/dev/null || true
pkill -TERM -f "[0]01a-monitor-tk.py" 2>/dev/null || true
pkill -TERM -f "[p]ython3? -u .*001b-monitor-web.py" 2>/dev/null || true
pkill -TERM -f "[0]01b-monitor-web.py" 2>/dev/null || true
pkill -TERM -f "[n]ext-run-timer.py" 2>/dev/null || true
pkill -TERM -f "[1]30c-follow-run.sh" 2>/dev/null || true

# ============================================================================
# STEP 6: Stop webhook listener (background HTTP receiver)
# ============================================================================
echo "6) Stopping webhook listener..."
pkill -TERM -f "[p]ython3? -u .*src/10_webhook/010-webhook-listener.py" 2>/dev/null || true
pkill -TERM -f "[s]rc/10_webhook/010-webhook-listener.py" 2>/dev/null || true

# Give processes a short window to terminate gracefully. Keep this snappy so the
# STOP action (and update-local-copy.sh, which relies on a fast stop) does not
# appear to hang; stubborn processes are force-killed right after.
sleep 2

# Force kill any remaining processes that didn't respond to TERM
echo "Force-killing any remaining stubborn processes..."
pkill -9 -f "[r]un-worker.sh" 2>/dev/null || true
pkill -9 -f "[w]atch-queue-flag.sh" 2>/dev/null || true
pkill -9 -f "[p]ython3? -u .*010-collect-index.py" 2>/dev/null || true
pkill -9 -f "[p]ython3? -u .*030-collect-details.py" 2>/dev/null || true
pkill -9 -f "[0]70-test-zone.py" 2>/dev/null || true
pkill -9 -f "[b]uild_calendar.py" 2>/dev/null || true
pkill -9 -f "[u]pdate-local-copy.sh" 2>/dev/null || true
pkill -9 -f "[0]01a-monitor-tk.py" 2>/dev/null || true
pkill -9 -f "[s]rc/10_webhook/010-webhook-listener.py" 2>/dev/null || true

sleep 1

# Clear in-progress flag after all workers have had time to exit
# Manual stops are intentional, not resumable abrupt exits
rm -f "$PC_QUEUE_DIR/run_all_requested.flag" "$PC_QUEUE_DIR/run_all_in_progress.flag" "$PC_QUEUE_DIR/run_all_stop_no_resume.flag"

echo "$(date '+%Y-%m-%d %H:%M:%S') | All runners stopped manually." >> "$PC_LOG_DIR/run_all_worker.log"

echo ""
echo "============================================================"
echo "All PanamaCompra processes stopped."
echo "============================================================"
echo "Remaining related processes (should be empty):"
pgrep -af "100-run-worker.sh|050-watch-queue-flag.sh|070-test-zone.py|060-build-calendar.py|monitor-tk.py|monitor-web.py|src/10_webhook/010-webhook-listener.py|update-local-copy.sh" || echo "  None found - all stopped successfully."
