#!/usr/bin/env bash
set -uo pipefail

BASE_DIR="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
cd "$BASE_DIR" || exit 1

mkdir -p data/logs

OPEN_LOG="data/logs/monitor_open.log"
FALLBACK_LOG="data/logs/run_all_follow.log"
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
