#!/usr/bin/env bash
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
# shellcheck source=../../lib/env.sh
source "$SCRIPT_DIR/../../lib/env.sh"
cd "$APP_ROOT"

MONITOR_LOCK="/tmp/panamacompra_monitor_window.lock"
PROGRESS_FILE="$PC_LOG_DIR/run_all_progress.env"
WAHA_QR_WARNING_FILE="$PC_RUN_DIR/waha_qr_required.env"
IDLE_CLOSE_SECONDS="${PC_MONITOR_IDLE_CLOSE_SECONDS:-8}"
STABLE_DONE_CYCLES="${PC_MONITOR_STABLE_DONE_CYCLES:-3}"
REFRESH_SECONDS="${PC_MONITOR_REFRESH_SECONDS:-5}"
FORCE_REDRAW_SECONDS="${PC_MONITOR_FORCE_REDRAW_SECONDS:-30}"

normalize_positive_int() {
  local value="$1"
  local fallback="$2"

  if [[ "$value" =~ ^[0-9]+$ ]] && [ "$value" -gt 0 ]; then
    echo "$value"
  else
    echo "$fallback"
  fi
}

file_state() {
  local path="$1"

  if [ -f "$path" ]; then
    stat -c '%Y:%s' "$path" 2>/dev/null || echo "present"
  else
    echo "missing"
  fi
}

render_signature() {
  printf 'worker=%s\n' "$(run_all_worker_running && echo running || echo stopped)"
  printf 'index=%s\n' "$(index_running && echo running || echo stopped)"
  printf 'detail=%s\n' "$(detail_running && echo running || echo stopped)"
  printf 'request=%s\n' "$(request_pending && echo pending || echo none)"
  printf 'progress=%s\n' "$(file_state "$PROGRESS_FILE")"
  printf 'worker_log=%s\n' "$(file_state "$PC_LOG_DIR/run_all_worker.log")"
  printf 'current_log=%s\n' "$(file_state "$PC_LOG_DIR/run_all_current.log")"
  printf 'waha_qr_warning=%s\n' "$(file_state "$WAHA_QR_WARNING_FILE")"
}

run_all_worker_running() {
  pgrep -f "[r]un-worker.sh" >/dev/null 2>&1
}

index_running() {
  pgrep -f "[p]ython3? -u .*010-collect-index.py" >/dev/null 2>&1
}

detail_running() {
  pgrep -f "[p]ython3? -u .*030-collect-details.py" >/dev/null 2>&1
}

request_pending() {
  [ -f "$PC_QUEUE_DIR/run_all_requested.flag" ]
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
  STEP_CURRENT="-"
  STEP_TOTAL="-"
  ITEM_CURRENT="-"
  ITEM_TOTAL="-"
  RECORDS_FOUND="-"
  RECORDS_NEW="-"
  RECORDS_EXISTING="-"
  RECORDS_SAVED="-"
  RECORDS_FAILED="-"
  RECORDS_PENDING="-"
  EXTRA="-"

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
  echo "Step:        $STEP_CURRENT / $STEP_TOTAL"
  echo "Item:        $ITEM_CURRENT / $ITEM_TOTAL"
  echo "Updated:     $UPDATED_AT"
  if [ -f "$WAHA_QR_WARNING_FILE" ]; then
    SESSION="default"
    WAHA_STATUS="SCAN_QR_CODE"
    DASHBOARD_URL="http://127.0.0.1:3000"
    WAHA_WARNING_UPDATED_AT="-"
    # shellcheck disable=SC1090
    source "$WAHA_QR_WARNING_FILE"
    echo ""
    echo "!!!!!!!!!!!!!!!!!! WHATSAPP ATTENTION REQUIRED !!!!!!!!!!!!!!!!!!"
    echo "  WAHA session '$SESSION' is $WAHA_STATUS and requires a QR scan."
    echo "  WhatsApp messaging was SKIPPED; collection continues normally."
    echo "  Open $DASHBOARD_URL and scan the QR code to pair the session."
    echo "  Warning recorded: $WAHA_WARNING_UPDATED_AT"
    echo "  It stays open until a healthy run clears it, or you press Ctrl+C."
    echo "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
  fi
  echo ""
  echo "Current action:"
  echo "  $MESSAGE"
  echo ""
  echo "Diagnostics:"
  echo "  Found rows:         $RECORDS_FOUND"
  echo "  New records:        $RECORDS_NEW"
  echo "  Existing records:   $RECORDS_EXISTING"
  echo "  Details saved/skip: $RECORDS_SAVED"
  echo "  Detail failures:    $RECORDS_FAILED"
  echo "  Pending details:    $RECORDS_PENDING"
  echo "  Extra:              $EXTRA"
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
  tail -12 "$PC_LOG_DIR/run_all_worker.log" 2>/dev/null || echo "No run_all_worker.log yet."

  echo ""
  echo "-------------------- Current action log --------------------"
  if [ -f "$PC_LOG_DIR/run_all_current.log" ]; then
    tail -25 "$PC_LOG_DIR/run_all_current.log"
  else
    echo "No current run log yet."
  fi

  echo ""
  echo "============================================================"
  if [ -f "$WAHA_QR_WARNING_FILE" ]; then
    echo "Auto-close: PAUSED while the WAHA QR warning is active."
  else
    echo "Auto-close: when worker/index/detail are all finished."
  fi
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

IDLE_CLOSE_SECONDS="$(normalize_positive_int "$IDLE_CLOSE_SECONDS" "8")"
STABLE_DONE_CYCLES="$(normalize_positive_int "$STABLE_DONE_CYCLES" "3")"
REFRESH_SECONDS="$(normalize_positive_int "$REFRESH_SECONDS" "5")"
FORCE_REDRAW_SECONDS="$(normalize_positive_int "$FORCE_REDRAW_SECONDS" "30")"

done_cycles=0
last_signature=""
last_redraw_epoch=0
# Only auto-close after this window has actually watched a run go from active
# to finished. Opening the monitor straight into a pre-existing idle/done
# state (e.g. right after an update with no run queued) must NOT start the
# countdown, otherwise the window closes before any work runs (matches the
# saw_active guard already used by 001a-monitor-tk.py).
saw_active=0

while true; do
  current_signature="$(render_signature)"
  now_epoch="$(date +%s)"

  if [ "$current_signature" != "$last_signature" ] || [ $((now_epoch - last_redraw_epoch)) -ge "$FORCE_REDRAW_SECONDS" ]; then
    show_screen
    last_signature="$current_signature"
    last_redraw_epoch="$now_epoch"
  fi

  if [ -f "$WAHA_QR_WARNING_FILE" ]; then
    done_cycles=0
  elif system_is_done; then
    done_cycles=$((done_cycles + 1))
  else
    saw_active=1
    done_cycles=0
  fi

  if [ "$saw_active" -eq 1 ] && [ "$done_cycles" -ge "$STABLE_DONE_CYCLES" ]; then
    echo ""
    echo "Process finished. Closing in ${IDLE_CLOSE_SECONDS} seconds..."
    sleep "$IDLE_CLOSE_SECONDS"
    exit 0
  elif [ "$saw_active" -eq 0 ] && [ "$done_cycles" -eq "$STABLE_DONE_CYCLES" ]; then
    echo ""
    echo "Idle. Auto-close starts only after a run finishes while this window is open. Manual close: Ctrl+C"
  fi

  sleep "$REFRESH_SECONDS"
done
