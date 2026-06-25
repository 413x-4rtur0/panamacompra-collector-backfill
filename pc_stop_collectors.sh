#!/usr/bin/env bash
# Stop the active PanamaCompra COLLECTION (worker + collectors) without touching
# the monitor, the next-run timer, or the webhook listener. This is the "Stop"
# button in the monitor Run controls: it halts the current run immediately and
# prevents an automatic resume, but leaves the monitor open so the operator can
# see the stopped state and start a new run. For a full teardown that also closes
# the monitors and webhook, use pc_stop_run_all.sh instead.
set -uo pipefail

cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" || exit 1

mkdir -p data/logs data/queue

PYTHON_BIN="${PYTHON_BIN:-python3}"
if [ -f ".venv/bin/activate" ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
  PYTHON_BIN="python"
fi

echo "Stopping PanamaCompra collection (worker + collectors); monitor stays open..."

# Mark this as an intentional stop and drop any queued request so the worker's
# EXIT trap performs a clean no-resume stop instead of restoring the request.
touch data/queue/run_all_stop_no_resume.flag
rm -f data/queue/run_all_requested.flag

# Graceful TERM to the collection pipeline only (NOT monitors/timer/webhook).
pkill -TERM -f "[p]c_run_all_worker.sh" 2>/dev/null || true
pkill -TERM -f "[p]ython3? -u ./pc_index_collector.py" 2>/dev/null || true
pkill -TERM -f "[p]ython3? -u ./pc_detail_downloader.py" 2>/dev/null || true
pkill -TERM -f "[t]imeout .*pc_index_collector.py" 2>/dev/null || true
pkill -TERM -f "[t]imeout .*pc_detail_downloader.py" 2>/dev/null || true
pkill -TERM -f "[p]c_build_detail_views.py" 2>/dev/null || true
pkill -TERM -f "[p]c_build_calendar.py" 2>/dev/null || true
pkill -TERM -f "[p]c_test_zone.py" 2>/dev/null || true
pkill -TERM -f "[p]c_update_before_run.sh" 2>/dev/null || true

# Give them a short window to exit cleanly, then force-kill stragglers.
sleep 2
pkill -9 -f "[p]c_run_all_worker.sh" 2>/dev/null || true
pkill -9 -f "[p]ython3? -u ./pc_index_collector.py" 2>/dev/null || true
pkill -9 -f "[p]ython3? -u ./pc_detail_downloader.py" 2>/dev/null || true
pkill -9 -f "[p]c_build_detail_views.py" 2>/dev/null || true
pkill -9 -f "[p]c_build_calendar.py" 2>/dev/null || true
pkill -9 -f "[p]c_test_zone.py" 2>/dev/null || true
sleep 1

# If the worker handled TERM, its EXIT trap already wrote a STOPPED progress and
# cleared the flags. If only orphan collectors were running (or the worker was
# force-killed, skipping its trap), publish a STOPPED state here so the monitor
# reflects the manual stop and re-enables the run controls immediately.
if ! pgrep -f "[p]c_run_all_worker.sh" >/dev/null 2>&1; then
  "$PYTHON_BIN" - <<'PY' 2>/dev/null || true
try:
    from pc_common import write_run_progress
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
rm -f data/queue/run_all_requested.flag data/queue/run_all_in_progress.flag data/queue/run_all_stop_no_resume.flag

echo "$(date '+%Y-%m-%d %H:%M:%S') | Collection stopped manually (monitor kept open)." >> data/logs/run_all_worker.log
echo "Collection stopped. Monitor remains open."
