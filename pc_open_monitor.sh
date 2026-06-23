#!/usr/bin/env bash
set -uo pipefail

BASE_DIR="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
cd "$BASE_DIR" || exit 1

mkdir -p data/logs

PYTHON_BIN="${PYTHON_BIN:-python3}"
if [ -f ".venv/bin/activate" ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
  PYTHON_BIN="python"
fi

OPEN_LOG="data/logs/monitor_open.log"
FALLBACK_LOG="data/logs/run_all_follow.log"
WEB_LOG="data/logs/monitor_server.log"
TK_LOG="data/logs/monitor_tk.log"
TIMER_LOG="data/logs/next_run_timer.log"
MONITOR_MODE="${PC_MONITOR_MODE:-tk}"
MONITOR_HOST="${PC_MONITOR_HOST:-127.0.0.1}"
MONITOR_PORT="${PC_MONITOR_PORT:-8766}"
MONITOR_LAUNCH_CONTEXT="${PC_MONITOR_LAUNCH_CONTEXT:-manual}"
MONITOR_URL="http://${MONITOR_HOST}:${MONITOR_PORT}/"
CMD="cd $(printf '%q' "$BASE_DIR") && ./pc_monitor_window.sh"

log() {
  echo "$(date '+%Y-%m-%d %H:%M:%S') | $*" >> "$OPEN_LOG"
}

prepare_gui_environment() {
  local uid
  uid="$(id -u)"

  # Webhook listeners are often started from changedetection.io, cron, or a
  # background service where DISPLAY/DBUS are missing even though the user has
  # an active desktop session. Try the common single-seat Linux desktop values.
  if [ -z "${DISPLAY:-}" ] && [ -S /tmp/.X11-unix/X0 ]; then
    export DISPLAY=":0"
    log "DISPLAY was empty; using detected X display :0."
  fi

  if [ -z "${DBUS_SESSION_BUS_ADDRESS:-}" ] && [ -S "/run/user/$uid/bus" ]; then
    export DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus"
    log "DBUS_SESSION_BUS_ADDRESS was empty; using /run/user/$uid/bus."
  fi

  if [ -z "${XAUTHORITY:-}" ] && [ -f "$HOME/.Xauthority" ]; then
    export XAUTHORITY="$HOME/.Xauthority"
    log "XAUTHORITY was empty; using $HOME/.Xauthority."
  fi
}

tk_monitor_running() {
  pgrep -f "[p]c_monitor_tk.py" >/dev/null 2>&1
}

next_run_timer_running() {
  pgrep -f "[p]c_next_run_timer.py" >/dev/null 2>&1
}

ensure_webhook_listener() {
  if [ "${PC_WEBHOOK_AUTO_START:-1}" = "0" ]; then
    log "Webhook auto-start disabled by PC_WEBHOOK_AUTO_START=0."
    return 0
  fi
  if [ -x ./pc_ensure_webhook_listener.sh ]; then
    ./pc_ensure_webhook_listener.sh >> "$OPEN_LOG" 2>&1 || log "Webhook listener ensure failed; changedetection may show connection refused."
  fi
}

start_next_run_timer() {
  if [ "${PC_NEXT_RUN_TIMER:-1}" = "0" ]; then
    log "Next-run timer disabled by PC_NEXT_RUN_TIMER=0."
    return 0
  fi
  if next_run_timer_running; then
    log "Next-run timer already running."
    return 0
  fi
  if [ -z "${DISPLAY:-}" ]; then
    log "DISPLAY is empty; cannot open next-run timer."
    return 1
  fi
  nohup "$PYTHON_BIN" ./pc_next_run_timer.py >> "$TIMER_LOG" 2>&1 &
  log "Started next-run timer with log $TIMER_LOG."
}

start_tk_monitor() {
  if tk_monitor_running; then
    log "Native Tk monitor already running. Not starting another one."
    echo "PanamaCompra native monitor is already running."
    return 0
  fi

  if [ -z "${DISPLAY:-}" ]; then
    log "DISPLAY is empty; cannot open native Tk monitor."
    return 1
  fi

  PC_MONITOR_LAUNCH_CONTEXT="$MONITOR_LAUNCH_CONTEXT" nohup "$PYTHON_BIN" ./pc_monitor_tk.py >> "$TK_LOG" 2>&1 &
  local tk_pid=$!
  sleep 1

  if kill -0 "$tk_pid" 2>/dev/null || tk_monitor_running; then
    log "Started native Tk monitor pid=$tk_pid with log $TK_LOG."
    echo "PanamaCompra native monitor started."
    return 0
  fi

  log "Native Tk monitor exited immediately. Check $TK_LOG for details."
  return 1
}

monitor_server_running() {
  "$PYTHON_BIN" -c "from urllib.request import urlopen; urlopen('http://${MONITOR_HOST}:${MONITOR_PORT}/health', timeout=1).read()" >/dev/null 2>&1
}

start_web_monitor() {
  if monitor_server_running; then
    log "Web monitor already running at $MONITOR_URL."
  else
    PC_MONITOR_HOST="$MONITOR_HOST" PC_MONITOR_PORT="$MONITOR_PORT" PC_MONITOR_LAUNCH_CONTEXT="$MONITOR_LAUNCH_CONTEXT" nohup "$PYTHON_BIN" ./pc_monitor_server.py >> "$WEB_LOG" 2>&1 &
    log "Started web monitor at $MONITOR_URL with log $WEB_LOG."
    sleep 1
  fi
}

open_url_if_possible() {
  if [ -z "${DISPLAY:-}" ]; then
    log "DISPLAY is empty; web monitor is available at $MONITOR_URL but browser was not opened."
    return 1
  fi

  if command -v xdg-open >/dev/null 2>&1; then
    local open_url="$MONITOR_URL"
    if [ "$MONITOR_LAUNCH_CONTEXT" = "auto" ]; then
      open_url="${MONITOR_URL}?auto_close=1"
    fi
    nohup xdg-open "$open_url" >/dev/null 2>&1 &
    log "Opened web monitor with xdg-open: $open_url."
    return 0
  fi

  if command -v sensible-browser >/dev/null 2>&1; then
    local open_url="$MONITOR_URL"
    if [ "$MONITOR_LAUNCH_CONTEXT" = "auto" ]; then
      open_url="${MONITOR_URL}?auto_close=1"
    fi
    nohup sensible-browser "$open_url" >/dev/null 2>&1 &
    log "Opened web monitor with sensible-browser: $open_url."
    return 0
  fi

  log "No supported browser opener found. Web monitor is available at $MONITOR_URL."
  return 1
}

start_log_follower_fallback() {
  if pgrep -f "[t]ail -f data/logs/run_all_worker.log data/logs/run_all_current.log" >/dev/null 2>&1; then
    log "Fallback log follower already running. Not starting another one."
    return 0
  fi

  nohup ./pc_follow_run_all.sh > "$FALLBACK_LOG" 2>&1 &
  log "No GUI monitor available. Started background log follower at $FALLBACK_LOG."
  log "Open a terminal and run: cd $(printf '%q' "$BASE_DIR") && ./pc_follow_run_all.sh"
}

prepare_gui_environment
ensure_webhook_listener

if [ "$MONITOR_MODE" = "tk" ]; then
  start_next_run_timer || true
  if start_tk_monitor; then
    exit 0
  fi
  log "Native Tk monitor could not be opened; starting text log follower fallback."
  start_log_follower_fallback
  exit 0
fi

if [ "$MONITOR_MODE" = "web" ]; then
  start_web_monitor
  open_url_if_possible || true
  echo "PanamaCompra web monitor: $MONITOR_URL"
  exit 0
fi

if pgrep -f "[p]c_monitor_window.sh" >/dev/null 2>&1; then
  log "Monitor already running. Not opening another window."
  exit 0
fi

if [ -z "${DISPLAY:-}" ]; then
  log "DISPLAY is empty and no local X display was detected; cannot open GUI terminal."
  start_log_follower_fallback
  exit 0
fi

if command -v gnome-terminal >/dev/null 2>&1; then
  nohup gnome-terminal --title="PanamaCompra Progress" -- bash -lc "$CMD" >/dev/null 2>&1 &
  log "Opened monitor with gnome-terminal on DISPLAY=$DISPLAY."
  exit 0
fi

if command -v mate-terminal >/dev/null 2>&1; then
  nohup mate-terminal --title="PanamaCompra Progress" -- bash -lc "$CMD" >/dev/null 2>&1 &
  log "Opened monitor with mate-terminal on DISPLAY=$DISPLAY."
  exit 0
fi

if command -v xfce4-terminal >/dev/null 2>&1; then
  nohup xfce4-terminal --title="PanamaCompra Progress" --command="bash -lc \"$CMD\"" >/dev/null 2>&1 &
  log "Opened monitor with xfce4-terminal on DISPLAY=$DISPLAY."
  exit 0
fi

if command -v x-terminal-emulator >/dev/null 2>&1; then
  nohup x-terminal-emulator -T "PanamaCompra Progress" -e bash -lc "$CMD" >/dev/null 2>&1 &
  log "Opened monitor with x-terminal-emulator on DISPLAY=$DISPLAY."
  exit 0
fi

if command -v xterm >/dev/null 2>&1; then
  nohup xterm -T "PanamaCompra Progress" -e bash -lc "$CMD" >/dev/null 2>&1 &
  log "Opened monitor with xterm on DISPLAY=$DISPLAY."
  exit 0
fi

log "No supported terminal emulator found on DISPLAY=$DISPLAY."
start_log_follower_fallback
exit 0
