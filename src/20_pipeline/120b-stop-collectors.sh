#!/usr/bin/env bash
# Stop the active PanamaCompra COLLECTION (worker + collectors) without touching
# the monitor, the next-run timer, or the webhook listener. This is the "Stop"
# button in the monitor Run controls: it halts the current run immediately and
# prevents an automatic resume, but leaves the monitor open so the operator can
# see the stopped state and start a new run. For a full teardown that also stops
# the webhook listener and updater/infrastructure processes, use
# src/20_pipeline/120a-stop-everything.sh instead; monitors remain open there too.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
# shellcheck source=../../lib/env.sh
source "$SCRIPT_DIR/../../lib/env.sh"
cd "$APP_ROOT"

PYTHON_BIN="${PYTHON_BIN:-python3}"
if [ -f ".venv/bin/activate" ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
  PYTHON_BIN="python"
fi

echo "Stopping PanamaCompra collection (worker + collectors); monitor stays open..."

# Mark this as an intentional stop and drop any queued request so the worker's
# EXIT trap performs a clean no-resume stop instead of restoring the request.
touch "$PC_QUEUE_DIR/run_all_stop_no_resume.flag"
rm -f "$PC_QUEUE_DIR/run_all_requested.flag"

# Graceful TERM to the collection pipeline only (NOT monitors/timer/webhook).
pkill -TERM -f "[r]un-worker.sh" 2>/dev/null || true
pkill -TERM -f "[p]ython3? -u .*010-collect-index.py" 2>/dev/null || true
pkill -TERM -f "[p]ython3? -u .*030-collect-details.py" 2>/dev/null || true
pkill -TERM -f "[t]imeout .*010-collect-index.py" 2>/dev/null || true
pkill -TERM -f "[t]imeout .*030-collect-details.py" 2>/dev/null || true
pkill -TERM -f "[0]40-build-detail-views.py" 2>/dev/null || true
pkill -TERM -f "[b]uild_calendar.py" 2>/dev/null || true
pkill -TERM -f "[0]70-test-zone.py" 2>/dev/null || true
pkill -TERM -f "[0]00-update-before-run.sh" 2>/dev/null || true

# Give them a short window to exit cleanly, then force-kill stragglers.
sleep 2
pkill -9 -f "[r]un-worker.sh" 2>/dev/null || true
pkill -9 -f "[p]ython3? -u .*010-collect-index.py" 2>/dev/null || true
pkill -9 -f "[p]ython3? -u .*030-collect-details.py" 2>/dev/null || true
pkill -9 -f "[0]40-build-detail-views.py" 2>/dev/null || true
pkill -9 -f "[b]uild_calendar.py" 2>/dev/null || true
pkill -9 -f "[0]70-test-zone.py" 2>/dev/null || true
sleep 1

# If the worker handled TERM, its EXIT trap already wrote a STOPPED progress and
# cleared the flags. If only orphan collectors were running (or the worker was
# force-killed, skipping its trap), publish a STOPPED state here so the monitor
# reflects the manual stop and re-enables the run controls immediately.
if ! pgrep -f "[r]un-worker.sh" >/dev/null 2>&1; then
  "$PYTHON_BIN" - <<'PY' 2>/dev/null || true
try:
    from common import write_run_progress
    write_run_progress(
        "STOPPED", "DONE", 100,
        "Collection stopped manually from the monitor. Monitor left open; request a new run to resume.",
        mode="IDLE",
    )
except Exception:
    pass
PY
fi

# Manual stop is intentional, not a resumable abrupt exit: clear all flags last,
# after the worker has had its chance to see the no-resume marker.
rm -f "$PC_QUEUE_DIR/run_all_requested.flag" "$PC_QUEUE_DIR/run_all_in_progress.flag" "$PC_QUEUE_DIR/run_all_stop_no_resume.flag"

echo "$(date '+%Y-%m-%d %H:%M:%S') | Collection stopped manually (monitor kept open)." >> "$PC_LOG_DIR/run_all_worker.log"
echo "Collection stopped. Monitor remains open."
