#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
# shellcheck source=../../lib/env.sh
source "$SCRIPT_DIR/../../lib/env.sh"
# shellcheck source=../10_webhook/015-listener-process.sh
source "$APP_ROOT/src/10_webhook/015-listener-process.sh"
cd "$APP_ROOT"

echo "Stopping all PanamaCompra runners and background processes (monitors stay open)..."

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
pkill -TERM -f "[0]50-repair-missing-deadlines.py" 2>/dev/null || true

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
# STEP 5: Keep monitor processes open
# ============================================================================
echo "5) Keeping monitors open (native, web, terminal and next-run timer)..."
# Stop All deliberately leaves every monitor alive so the operator can see the
# stopped state and request Start/Resume without reopening the dashboard.

# ============================================================================
# STEP 6: Stop webhook listener (background HTTP receiver)
# ============================================================================
echo "6) Stopping webhook listener..."
# If it's managed by the panamacompra-webhook.service systemd unit
# (Restart=on-failure), a plain pkill below looks like a crash to systemd,
# which relaunches it a few seconds later -- silently undoing this stop. Stop
# the unit itself first so it does not come back on its own; the pkill still
# runs afterward as a fallback for hosts running the listener unmanaged.
if systemctl --user is-active --quiet panamacompra-webhook.service 2>/dev/null; then
  echo "   Stopping systemd unit: panamacompra-webhook.service"
  systemctl --user stop panamacompra-webhook.service 2>/dev/null || true
fi
pc_webhook_kill_host_processes TERM

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
pkill -9 -f "[0]50-repair-missing-deadlines.py" 2>/dev/null || true
pkill -9 -f "[b]uild_calendar.py" 2>/dev/null || true
pkill -9 -f "[u]pdate-local-copy.sh" 2>/dev/null || true
pc_webhook_kill_host_processes KILL

sleep 1

# Clear in-progress flag after all workers have had time to exit
# Manual stops are intentional, not resumable abrupt exits
rm -f "$PC_QUEUE_DIR/run_all_requested.flag" "$PC_QUEUE_DIR/run_all_in_progress.flag" "$PC_QUEUE_DIR/run_all_stop_no_resume.flag"

echo "$(date '+%Y-%m-%d %H:%M:%S') | All runners stopped manually." >> "$PC_LOG_DIR/run_all_worker.log"

echo ""
echo "============================================================"
echo "PanamaCompra collectors and infrastructure stopped; monitors remain open."
echo "============================================================"
echo "Remaining collector/infrastructure processes (should be empty):"
pgrep -af "100-run-worker.sh|050-watch-queue-flag.sh|070-test-zone.py|060-build-calendar.py|src/10_webhook/010-webhook-listener.py|update-local-copy.sh" || echo "  None found - all stopped successfully."
echo "Monitors intentionally preserved:"
pgrep -af "001a-monitor-tk.py|001b-monitor-web.py|001c-monitor-terminal.sh|002-next-run-timer.py|002b-next-run-timer-cli.py" || echo "  No monitor process detected."

# This script only manages what belongs to THIS checkout. Docker integration
# containers (changedetection, sockpuppetbrowser, WAHA, webhook) are a
# separate lifecycle -- see src/50_tools/010-docker-stack.sh down/status --
# and are intentionally left running. Likewise, unrelated systemd services
# from an older/alternate setup on this host are not part of this repo and
# are left alone; surfaced here only so they are not mistaken for "stopped".
OTHER_SERVICES=""
for svc in panamacompra-runner.service panamacompra-webhook-receiver.service; do
  if systemctl --user is-active --quiet "$svc" 2>/dev/null; then
    OTHER_SERVICES="$OTHER_SERVICES $svc"
  fi
done
if [ -n "$OTHER_SERVICES" ]; then
  echo ""
  echo "NOTE: these unrelated systemd services are still running (not managed by"
  echo "this script/repo):$OTHER_SERVICES"
  echo "Stop manually if needed: systemctl --user stop <service>"
fi
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1 \
   && [ -n "$(docker compose ps --status running -q 2>/dev/null)" ]; then
  echo ""
  echo "NOTE: Docker integration containers are still running (by design; use"
  echo "./src/50_tools/010-docker-stack.sh down to stop them, or 'up' to restart)."
fi
