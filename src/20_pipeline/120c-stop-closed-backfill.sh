#!/usr/bin/env bash
# Stop an in-progress Closed-opportunities backfill segment (priority 3) only
# — the monitor's other collectors (main worker, Closed new-closures) and the
# backfill timer itself are left untouched; the timer will simply start a
# fresh segment on its next tick (or stay idle if paused).
#
# 037b-collect-closed-details.py and 038-collect-cotizaciones.py are shared
# with 070-run-collector-closed-new.sh (priority 2), so this deliberately
# does NOT pkill by script name — that could hit a Closed new-closures run
# using the same scripts. Instead it targets exactly the process tree that
# holds the backfill's own flock (see LOCK_FILE in
# 039-run-closed-backfill.sh), which only 039 ever holds.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
# shellcheck source=../../lib/env.sh
source "$SCRIPT_DIR/../../lib/env.sh"
cd "$APP_ROOT"

LOCK_FILE="/tmp/panamacompra_closed_backfill_worker.lock"
LOG="$PC_LOG_DIR/closed_backfill_triggered.log"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') | $1" >> "$LOG"; }

if [ ! -e "$LOCK_FILE" ]; then
  echo "No Closed backfill lock file found; nothing is running."
  exit 0
fi

PIDS="$(fuser "$LOCK_FILE" 2>/dev/null)"
if [ -z "${PIDS// /}" ]; then
  echo "Closed backfill is not currently running."
  exit 0
fi

echo "Stopping active Closed backfill run (PIDs:$PIDS)..."
log "Stopping active Closed backfill run manually from the monitor (PIDs:$PIDS)"

# TERM first (script + direct children, e.g. the playwright driver), then a
# short grace window, then force-kill anything still standing.
for pid in $PIDS; do
  pkill -TERM -P "$pid" 2>/dev/null || true
  kill -TERM "$pid" 2>/dev/null || true
done
sleep 2
for pid in $PIDS; do
  pkill -9 -P "$pid" 2>/dev/null || true
  kill -9 "$pid" 2>/dev/null || true
done

log "Closed backfill stopped manually from the monitor."
echo "Closed backfill stopped."
