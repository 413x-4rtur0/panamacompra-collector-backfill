#!/usr/bin/env bash
set -uo pipefail

# Priority 3 (lowest): the historical Closed-opportunities backfill, run in
# bounded segments (see backfill_page_cap()/backfill_cutoff_date() in
# 037-collect-closed-index.py) by its own systemd --user timer, not by
# changedetection — old closures do not "change", so a snapshot diff has
# nothing to trigger on. No WhatsApp: downloads and inserts each record's
# full detail archive (037b) and cuadro-de-cotizaciones price/provider data
# (038), same as the priority-2 new-closures path (037 backfill mode + 037b
# + 038).
#
# Loops continuously through batches for up to PC_CLOSED_BACKFILL_MAX_SECONDS
# (default 900s, leaving a buffer before the next 20-minute tick) instead of
# doing one small batch and exiting -- a single batch left most of every
# 20-minute window idle even when priority 1/2 were free the whole time,
# which meant a large backlog (or a queued task-4 range) drained far slower
# than the system was actually capable of. Re-checks priority 1/2 before
# every batch so it yields mid-drain the instant either starts, not just at
# the next scheduled tick, and stops early once a full round finds nothing
# left to do rather than spinning no-op batches until the time cap.

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

MONITOR_SETTINGS="$PC_DATA_DIR/config/monitor_settings.env"
load_settings() {
  if [ -f "$MONITOR_SETTINGS" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$MONITOR_SETTINGS"
    set +a
  fi
}
load_settings

# Defers to BOTH priority 1 (main worker) and priority 2 (Closed
# new-closures) — a backfill segment never competes with either for the
# browser/CPU. Skipping loses nothing: the closed_crawl_state cursor picks
# up at the same page next tick.
MAIN_LOCK_FILE="/tmp/panamacompra_run_all_worker.lock"
CLOSED_NEW_LOCK_FILE="/tmp/panamacompra_closed_new_worker.lock"
LOCK_FILE="/tmp/panamacompra_closed_backfill_worker.lock"
LOG="$PC_LOG_DIR/closed_backfill_triggered.log"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') | $1" >> "$LOG"; }

priority_1_busy() { ! flock -n "$MAIN_LOCK_FILE" true 2>/dev/null; }
priority_2_busy() { ! flock -n "$CLOSED_NEW_LOCK_FILE" true 2>/dev/null; }

if priority_1_busy; then
  log "Skipped: priority 1 (main Abiertas/Programadas worker) is running."
  exit 0
fi
if priority_2_busy; then
  log "Skipped: priority 2 (Closed new-closures) is running."
  exit 0
fi

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  log "Skipped: another Closed backfill run is already in progress."
  exit 0
fi

MAX_SECONDS="${PC_CLOSED_BACKFILL_MAX_SECONDS:-900}"
START_TS=$(date +%s)
ITERATION=0

log "===== Closed backfill continuous-drain run started (cap ${MAX_SECONDS}s) ====="
while true; do
  ITERATION=$((ITERATION + 1))

  if priority_1_busy; then
    log "Yielding after $ITERATION batch(es): priority 1 (main worker) became active."
    break
  fi
  if priority_2_busy; then
    log "Yielding after $ITERATION batch(es): priority 2 (Closed new-closures) became active."
    break
  fi
  NOW_TS=$(date +%s)
  if [ $((NOW_TS - START_TS)) -ge "$MAX_SECONDS" ]; then
    log "Time cap reached after $ITERATION batch(es); yielding to the next timer tick."
    break
  fi

  # Priority 4: the persistent task queue (145-task-queue.py) for work that
  # must wait on a DB-state condition, not just "the active lock is free" —
  # e.g. a specific-date-range backfill queued to start only once the current
  # pending-detail backlog fully drains. Ticked every batch (not just once
  # per invocation) so an activation mid-drain is picked up by the very next
  # batch instead of waiting for the next 20-minute timer tick. Re-source
  # settings afterward: an activated task may have just rewritten them.
  TASK_QUEUE_TICK=$("$PYTHON_BIN" "$APP_ROOT/src/50_tools/145-task-queue.py" tick 2>&1)
  log "Task queue tick: $TASK_QUEUE_TICK"
  load_settings

  INDEX_OUT=$(PC_CLOSED_MODE=backfill "$PYTHON_BIN" "$APP_ROOT/src/20_pipeline/037-collect-closed-index.py" 2>&1)
  echo "$INDEX_OUT" >> "$LOG"
  DETAIL_OUT=$("$PYTHON_BIN" "$APP_ROOT/src/20_pipeline/037b-collect-closed-details.py" 2>&1)
  echo "$DETAIL_OUT" >> "$LOG"
  COTIZ_OUT=$("$PYTHON_BIN" "$APP_ROOT/src/20_pipeline/038-collect-cotizaciones.py" 2>&1)
  echo "$COTIZ_OUT" >> "$LOG"

  if [[ "$INDEX_OUT" == *"nothing to do"* || -z "$INDEX_OUT" ]] \
     && [[ "$DETAIL_OUT" == *"No Closed records pending a full detail fetch."* ]] \
     && [[ "$COTIZ_OUT" == *"No Closed records pending a cotizacion fetch."* ]]; then
    log "Nothing left to do after $ITERATION batch(es); stopping early."
    break
  fi
done
log "===== Closed backfill continuous-drain run finished ($ITERATION batch(es) this invocation) ====="
