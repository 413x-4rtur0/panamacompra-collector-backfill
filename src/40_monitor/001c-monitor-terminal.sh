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
INTERACTIVE_TUI=0
PAUSED=0
if [[ -t 0 && -t 1 && "${TERM:-dumb}" != "dumb" ]]; then
  INTERACTIVE_TUI=1
fi

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

terminal_width() {
  local width
  width="$(tput cols 2>/dev/null || echo 100)"
  [[ "$width" =~ ^[0-9]+$ ]] || width=100
  [ "$width" -lt 72 ] && width=72
  [ "$width" -gt 140 ] && width=140
  echo "$width"
}

fit_text() {
  local text="$1" width="$2"
  text="${text//$'\r'/}"
  text="${text//$'\n'/ }"
  if [ "${#text}" -gt "$width" ]; then
    printf '%s…' "${text:0:$((width - 1))}"
  else
    printf '%s' "$text"
  fi
}

ui_rule() {
  local left="$1" fill="$2" right="$3" inner=$((UI_WIDTH - 2)) line
  printf -v line "%${inner}s" ''
  line="${line// /$fill}"
  printf '%s' "$left"
  printf '%s' "$line"
  printf '%s\n' "$right"
}

ui_line() {
  local text
  text="$(fit_text "$1" "$((UI_WIDTH - 3))")"
  # No right border: Bash printf measures UTF-8 bytes rather than terminal
  # cells, so accented WAHA names would otherwise make the box look jagged.
  printf '│ %s\n' "$text"
}

ui_section() {
  local title=" $1 " remaining line
  remaining=$((UI_WIDTH - ${#title} - 2))
  printf -v line "%${remaining}s" ''
  line="${line// /─}"
  printf '├%s' "$title"
  printf '%s' "$line"
  printf '┤\n'
}

process_word() {
  if "$1"; then printf 'RUNNING'; else printf 'idle'; fi
}

render_log_panel() {
  local title="$1" path="$2" lines="$3" line
  ui_section "$title"
  if [ -f "$path" ]; then
    while IFS= read -r line; do
      ui_line "  $(fit_text "$line" "$((UI_WIDTH - 6))")"
    done < <(tail -n "$lines" "$path" 2>/dev/null)
  else
    ui_line "  No log yet: $(basename "$path")"
  fi
}

render_screen() {
  load_progress
  UI_WIDTH="$(terminal_width)"
  elapsed="$(format_duration "$(elapsed_seconds "$STARTED_AT")")"

  ui_rule '╭' '─' '╮'
  ui_line "PANAMACOMPRA AGENT MONITOR   $(date '+%Y-%m-%d %H:%M:%S')"
  ui_line "Phase $PHASE · Status $STATUS · Elapsed $elapsed · Updated $UPDATED_AT"
  ui_line "$(progress_bar "$PERCENT")"
  ui_section "CURRENT TASK"
  ui_line "$MESSAGE"
  ui_line "Step $STEP_CURRENT/$STEP_TOTAL · Item $ITEM_CURRENT/$ITEM_TOTAL · Detail limit $DETAIL_LIMIT"

  if [ -f "$WAHA_QR_WARNING_FILE" ]; then
    SESSION="default"
    WAHA_STATUS="SCAN_QR_CODE"
    DASHBOARD_URL="http://127.0.0.1:3000"
    WAHA_WARNING_MESSAGE="WhatsApp messaging was skipped; collection continues normally."
    WAHA_WARNING_ACTION="Open $DASHBOARD_URL and verify the WAHA session."
    WAHA_WARNING_UPDATED_AT="-"
    # shellcheck disable=SC1090
    source "$WAHA_QR_WARNING_FILE"
    ui_section "WHATSAPP ATTENTION"
    ui_line "WAHA session '$SESSION' is $WAHA_STATUS — messaging skipped; collection continues."
    ui_line "$WAHA_WARNING_MESSAGE"
    ui_line "$WAHA_WARNING_ACTION"
    ui_line "Warning recorded $WAHA_WARNING_UPDATED_AT; auto-close is paused."
  fi

  ui_section "COUNTERS"
  ui_line "Found $RECORDS_FOUND · New $RECORDS_NEW · Existing $RECORDS_EXISTING · Saved/skipped $RECORDS_SAVED"
  ui_line "Failures $RECORDS_FAILED · Pending $RECORDS_PENDING · Extra $EXTRA"
  ui_section "SERVICES"
  ui_line "Worker $(process_word run_all_worker_running) · Index $(process_word index_running) · Details $(process_word detail_running) · Request $(request_pending && echo PENDING || echo none)"
  render_log_panel "RECENT WORKER EVENTS" "$PC_LOG_DIR/run_all_worker.log" 6
  render_log_panel "CURRENT ACTION EVENTS" "$PC_LOG_DIR/run_all_current.log" 9
  ui_section "CONTROLS"
  ui_line "q quit · r redraw · p pause/resume · m command menu · s search WAHA · h help"
  if [ -f "$WAHA_QR_WARNING_FILE" ]; then
    ui_line "Auto-close paused while WhatsApp attention is required."
  elif [ "$PAUSED" -eq 1 ]; then
    ui_line "Display updates PAUSED; collection is not paused. Press p to resume."
  else
    ui_line "Display updates only when state changes; normal terminal history stays clean."
  fi
  ui_rule '╰' '─' '╯'
}

show_screen() {
  local content
  content="$(render_screen)"
  if [ "$INTERACTIVE_TUI" -eq 1 ]; then
    printf '\033[H\033[2J%s\n' "$content"
  else
    printf '%s\n' "$content"
  fi
}

enter_screen() {
  [ "$INTERACTIVE_TUI" -eq 1 ] || return 0
  tput smcup 2>/dev/null || true
  tput civis 2>/dev/null || true
  printf '\033[H\033[2J'
}

restore_screen() {
  [ "$INTERACTIVE_TUI" -eq 1 ] || return 0
  tput cnorm 2>/dev/null || true
  tput rmcup 2>/dev/null || true
}

pause_for_key() {
  printf '\nPress Enter to return to the monitor...'
  IFS= read -r _answer
  tput civis 2>/dev/null || true
  last_signature=""
}

show_help() {
  tput cnorm 2>/dev/null || true
  printf '\033[H\033[2J%s\n' 'PanamaCompra terminal monitor controls'
  printf '%s\n' '  q  Close only this monitor.'
  printf '%s\n' '  r  Force a fresh screen render.'
  printf '%s\n' '  p  Pause/resume display redraws (the collector keeps running).'
  printf '%s\n' '  m  Open the command menu (run control, calendar, KPIs, Docker, reports).'
  printf '%s\n' '  s  Search WAHA contacts, groups, communities and channels.'
  printf '%s\n' '  h  Show this help.'
  pause_for_key
}

search_clients() {
  local query
  tput cnorm 2>/dev/null || true
  printf '\033[H\033[2JSearch WAHA name or chat ID (blank lists all): '
  IFS= read -r query
  printf '\n'
  "$APP_ROOT/bin/pcc" clients search "$query" --limit 30 || true
  pause_for_key
}

run_cli_page() {
  local title="$1"
  shift
  tput cnorm 2>/dev/null || true
  printf '\033[H\033[2J%s\n\n' "$title"
  "$@" || true
  pause_for_key
}

command_menu() {
  local choice limit answer
  tput cnorm 2>/dev/null || true
  printf '\033[H\033[2J%s\n' 'PanamaCompra command menu'
  printf '%s\n' \
    '  1  Queue/start collector run' \
    '  2  Stop active collector run' \
    '  3  Status and queue snapshot' \
    '  4  Opportunity calendar (month)' \
    '  5  KPI summary and terminal diagrams' \
    '  6  Search WAHA client directory' \
    '  7  Docker integrations status' \
    '  8  Generate full diagnostic report' \
    '  9  Show saved monitor settings' \
    '  0  Return to monitor'
  printf '\nChoose: '
  IFS= read -r choice
  case "$choice" in
    1)
      printf 'Detail limit (0 = all) [0]: '
      IFS= read -r limit
      limit="${limit:-0}"
      printf 'Queue collector run with detail limit %s? [y/N]: ' "$limit"
      IFS= read -r answer
      if [[ "$answer" =~ ^[Yy]$ ]]; then
        run_cli_page "Queue collector run" "$APP_ROOT/bin/pcc" start "$limit"
      else
        last_signature=""
      fi
      ;;
    2)
      printf 'Stop the active collector run (monitor stays open)? [y/N]: '
      IFS= read -r answer
      if [[ "$answer" =~ ^[Yy]$ ]]; then
        run_cli_page "Stop active collector" "$APP_ROOT/src/20_pipeline/120b-stop-collectors.sh"
      else
        last_signature=""
      fi
      ;;
    3) run_cli_page "Status and queue" "$APP_ROOT/bin/pcc" status ;;
    4) run_cli_page "Opportunity calendar — month" "$APP_ROOT/bin/pcc" calendar month ;;
    5) run_cli_page "KPI summary" "$APP_ROOT/bin/pcc" kpi ;;
    6) search_clients ;;
    7) run_cli_page "Docker integrations status" "$APP_ROOT/bin/pcc" docker status ;;
    8) run_cli_page "Full diagnostic report" "$APP_ROOT/bin/pcc" full-report ;;
    9) run_cli_page "Saved monitor settings" "$APP_ROOT/bin/pcc" get ;;
    *) last_signature="" ;;
  esac
}

exec 9>"$MONITOR_LOCK"

if ! flock -n 9; then
  echo "Another PanamaCompra monitor window is already open."
  sleep 3
  exit 0
fi

trap restore_screen EXIT
trap 'restore_screen; exit 130' INT TERM

enter_screen

IDLE_CLOSE_SECONDS="$(normalize_positive_int "$IDLE_CLOSE_SECONDS" "8")"
STABLE_DONE_CYCLES="$(normalize_positive_int "$STABLE_DONE_CYCLES" "3")"
REFRESH_SECONDS="$(normalize_positive_int "$REFRESH_SECONDS" "5")"
FORCE_REDRAW_SECONDS="$(normalize_positive_int "$FORCE_REDRAW_SECONDS" "30")"

if [ "$INTERACTIVE_TUI" -eq 0 ]; then
  show_screen
  exit 0
fi

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

  if [ "$PAUSED" -eq 0 ] && { [ "$current_signature" != "$last_signature" ] || [ $((now_epoch - last_redraw_epoch)) -ge "$FORCE_REDRAW_SECONDS" ]; }; then
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
    printf '\nProcess finished. Closing in %s seconds...\n' "$IDLE_CLOSE_SECONDS"
    sleep "$IDLE_CLOSE_SECONDS"
    exit 0
  elif [ "$saw_active" -eq 0 ] && [ "$done_cycles" -eq "$STABLE_DONE_CYCLES" ]; then
    : # The stable footer already explains idle behavior; do not append repeated lines.
  fi

  key=""
  if IFS= read -rsn1 -t "$REFRESH_SECONDS" key; then
    case "$key" in
      q|Q) exit 0 ;;
      r|R) last_signature="" ;;
      p|P)
        if [ "$PAUSED" -eq 1 ]; then PAUSED=0; else PAUSED=1; fi
        show_screen
        last_signature="$current_signature"
        last_redraw_epoch="$now_epoch"
        ;;
      m|M) command_menu ;;
      s|S|/) search_clients ;;
      h|H|'?') show_help ;;
    esac
  fi
done
