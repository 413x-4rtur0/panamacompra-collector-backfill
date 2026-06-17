#!/usr/bin/env bash
set -uo pipefail

cd "$HOME/Apps/panamacompra-collector" || exit 1

mkdir -p data/logs data/queue

MONITOR_LOCK="/tmp/panamacompra_monitor_window.lock"
PROGRESS_FILE="data/logs/run_all_progress.env"
IDLE_CLOSE_SECONDS="${PC_MONITOR_IDLE_CLOSE_SECONDS:-8}"
STABLE_DONE_CYCLES="${PC_MONITOR_STABLE_DONE_CYCLES:-3}"
REFRESH_SECONDS="${PC_MONITOR_REFRESH_SECONDS:-2}"

run_all_worker_running() {
  pgrep -f "[p]c_run_all_worker.sh" >/dev/null 2>&1
}

index_running() {
  pgrep -f "[p]ython -u ./pc_index_collector.py" >/dev/null 2>&1
}

detail_running() {
  pgrep -f "[p]ython -u ./pc_detail_downloader.py" >/dev/null 2>&1
}

request_pending() {
  [ -f "data/queue/run_all_requested.flag" ]
}

system_is_done() {
  run_all_worker_running && return 1
  index_running && return 1
  detail_running && return 1
  request_pending && return 1
  return 0
}

elapsed_seconds() {
  local started="$1"

  if [ -z "$started" ]; then
    echo "0"
    return
  fi

  local start_epoch
  local now_epoch

  start_epoch="$(date -d "$started" +%s 2>/dev/null || echo 0)"
  now_epoch="$(date +%s)"

  if [ "$start_epoch" -gt 0 ]; then
    echo $((now_epoch - start_epoch))
  else
    echo "0"
  fi
}

format_duration() {
  local total="$1"
  local h=$((total / 3600))
  local m=$(((total % 3600) / 60))
  local s=$((total % 60))

  if [ "$h" -gt 0 ]; then
    printf "%02d:%02d:%02d" "$h" "$m" "$s"
  else
    printf "%02d:%02d" "$m" "$s"
  fi
}

progress_bar() {
  local percent="$1"
  local width=40

  if ! [[ "$percent" =~ ^[0-9]+$ ]]; then
    percent=0
  fi

  if [ "$percent" -lt 0 ]; then percent=0; fi
  if [ "$percent" -gt 100 ]; then percent=100; fi

  local filled=$((percent * width / 100))
  local empty=$((width - filled))

  printf "["
  if [ "$filled" -gt 0 ]; then
    printf "%0.s#" $(seq 1 "$filled")
  fi
  if [ "$empty" -gt 0 ]; then
    printf "%0.s-" $(seq 1 "$empty")
  fi
  printf "] %3d%%" "$percent"
}

load_progress() {
  PHASE="IDLE"
  STATUS="DONE"
  PERCENT="100"
  MESSAGE="No active process."
  DETAIL_LIMIT="-"
  STARTED_AT=""
  UPDATED_AT="-"
  WORKER_PID="-"

  if [ -f "$PROGRESS_FILE" ]; then
    # shellcheck disable=SC1090
    source "$PROGRESS_FILE"
  fi

  # If real process is running, keep progress visually alive.
  # Estimated animation eases toward a ceiling without wrapping backward.
  if index_running && [ "$PHASE" = "INDEX" ]; then
    e="$(elapsed_seconds "$STARTED_AT")"
    animated=$((10 + 38 * e / (e + 90)))
    if [ "$animated" -gt "$PERCENT" ]; then
      PERCENT="$animated"
    fi
  fi

  if detail_running && [ "$PHASE" = "DETAIL" ]; then
    e="$(elapsed_seconds "$STARTED_AT")"
    animated=$((55 + 38 * e / (e + 120)))
    if [ "$animated" -gt "$PERCENT" ]; then
      PERCENT="$animated"
    fi
  fi
}

clear_once() {
  clear
  tput civis 2>/dev/null || true
}

restore_cursor() {
  tput cnorm 2>/dev/null || true
}

show_screen() {
  tput cup 0 0 2>/dev/null || clear
  tput ed 2>/dev/null || true

  load_progress

  elapsed="$(format_duration "$(elapsed_seconds "$STARTED_AT")")"

  echo "============================================================"
  echo " PanamaCompra Progress Monitor"
  echo "============================================================"
  echo "Time:        $(date '+%Y-%m-%d %H:%M:%S')"
  echo "Phase:       $PHASE"
  echo "Status:      $STATUS"
  echo "Progress:    $(progress_bar "$PERCENT")"
  echo "Elapsed:     $elapsed"
  echo "Detail limit:$DETAIL_LIMIT"
  echo "Updated:     $UPDATED_AT"
  echo ""
  echo "Current action:"
  echo "  $MESSAGE"
  echo ""
  echo "Processes:"
  if run_all_worker_running; then
    echo "  run-all worker:     RUNNING"
  else
    echo "  run-all worker:     not running"
  fi

  if index_running; then
    echo "  index collector:    RUNNING"
  else
    echo "  index collector:    not running"
  fi

  if detail_running; then
    echo "  detail downloader:  RUNNING"
  else
    echo "  detail downloader:  not running"
  fi

  if request_pending; then
    echo "  request flag:       YES"
  else
    echo "  request flag:       no"
  fi

  echo ""
  echo "-------------------- Recent worker log ---------------------"
  tail -12 data/logs/run_all_worker.log 2>/dev/null || echo "No run_all_worker.log yet."

  echo ""
  echo "-------------------- Current action log --------------------"
  if [ -f data/logs/run_all_current.log ]; then
    tail -25 data/logs/run_all_current.log
  else
    echo "No current run log yet."
  fi

  echo ""
  echo "============================================================"
  echo "Auto-close: when worker/index/detail are all finished."
  echo "Manual close: Ctrl+C"
  echo "============================================================"
}

exec 9>"$MONITOR_LOCK"

if ! flock -n 9; then
  echo "Another PanamaCompra monitor window is already open."
  sleep 3
  exit 0
fi

trap restore_cursor EXIT INT TERM

clear_once

done_cycles=0

while true; do
  show_screen

  if system_is_done; then
    done_cycles=$((done_cycles + 1))
  else
    done_cycles=0
  fi

  if [ "$done_cycles" -ge "$STABLE_DONE_CYCLES" ]; then
    echo ""
    echo "Process finished. Closing in ${IDLE_CLOSE_SECONDS} seconds..."
    sleep "$IDLE_CLOSE_SECONDS"
    exit 0
  fi

  sleep "$REFRESH_SECONDS"
done
