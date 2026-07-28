#!/usr/bin/env bash
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
# shellcheck source=../../lib/env.sh
source "$SCRIPT_DIR/../../lib/env.sh"
BASE_DIR="$APP_ROOT"
cd "$APP_ROOT"

PYTHON_BIN="${PYTHON_BIN:-python3}"
if [ -f ".venv/bin/activate" ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
  PYTHON_BIN="python"
fi

OPEN_LOG="$PC_LOG_DIR/monitor_open.log"
FALLBACK_LOG="$PC_LOG_DIR/run_all_follow.log"
WEB_LOG="$PC_LOG_DIR/monitor_server.log"
TK_LOG="$PC_LOG_DIR/monitor_tk.log"
TIMER_LOG="$PC_LOG_DIR/next_run_timer.log"
MONITOR_SETTINGS="$PC_DATA_DIR/config/monitor_settings.env"

saved_monitor_setting() {
  local key="$1"
  [ -f "$MONITOR_SETTINGS" ] || return 0
  KEY="$key" bash -c '
    set -a
    # shellcheck disable=SC1090
    source "$1"
    printf "%s" "${!KEY-}"
  ' bash "$MONITOR_SETTINGS"
}

SAVED_MONITOR_MODE="$(saved_monitor_setting PC_MONITOR_MODE)"
SAVED_MONITOR_HOST="$(saved_monitor_setting PC_MONITOR_HOST)"
SAVED_MONITOR_PORT="$(saved_monitor_setting PC_MONITOR_PORT)"
SAVED_NEXT_RUN_TIMER="$(saved_monitor_setting PC_NEXT_RUN_TIMER)"
SAVED_TIMER_MODE="$(saved_monitor_setting PC_NEXT_RUN_TIMER_MODE)"
SAVED_AUTORUN_SOURCE="$(saved_monitor_setting PC_AUTORUN_SOURCE)"
MONITOR_MODE="${PC_MONITOR_MODE:-${SAVED_MONITOR_MODE:-tk}}"
NEXT_RUN_TIMER="${PC_NEXT_RUN_TIMER:-${SAVED_NEXT_RUN_TIMER:-1}}"
AUTORUN_SOURCE="${PC_AUTORUN_SOURCE:-${SAVED_AUTORUN_SOURCE:-changedetection}}"

# Timer front end selection. Automatic changedetection runs always use the
# lightweight terminal monitor, and the CLI countdown takes over that terminal
# when the monitor closes. The Tk timer never autostarts on this path — it is
# a separate window the operator opens manually or through the Tk monitor's
# manual-cron settings.
if [ "${PC_RUN_MODE:-}" = "AUTO" ] && [ "$AUTORUN_SOURCE" = "changedetection" ]; then
  MONITOR_MODE="terminal"
  NEXT_RUN_TIMER=1
  NEXT_RUN_TIMER_MODE="cli"
elif [ "$MONITOR_MODE" = "tk" ]; then
  NEXT_RUN_TIMER_MODE="${PC_NEXT_RUN_TIMER_MODE:-${SAVED_TIMER_MODE:-tk}}"
else
  NEXT_RUN_TIMER_MODE="${PC_NEXT_RUN_TIMER_MODE:-${SAVED_TIMER_MODE:-cli}}"
fi

# Diagnostic/test hook: print the resolved decision without touching processes.
if [ "${PC_MONITOR_RESOLVE_ONLY:-0}" = "1" ]; then
  echo "MONITOR_MODE=$MONITOR_MODE"
  echo "NEXT_RUN_TIMER=$NEXT_RUN_TIMER"
  echo "NEXT_RUN_TIMER_MODE=$NEXT_RUN_TIMER_MODE"
  exit 0
fi

MONITOR_HOST="${PC_MONITOR_HOST:-${SAVED_MONITOR_HOST:-127.0.0.1}}"
MONITOR_PORT="${PC_MONITOR_PORT:-${SAVED_MONITOR_PORT:-8766}}"
MONITOR_URL="http://${MONITOR_HOST}:${MONITOR_PORT}/"
# gnome-terminal is client/server: the new window's shell inherits the
# *server* process's environment, not this script's, so PC_RUN_MODE /
# PC_AUTORUN_SOURCE would otherwise silently vanish inside the terminal
# (001c-monitor-terminal.sh needs them to know whether to minimize itself
# when it hands off to the CLI timer). Pass them through explicitly instead.
CMD="cd $(printf '%q' "$SCRIPT_DIR") && PC_NEXT_RUN_TIMER=$(printf '%q' "$NEXT_RUN_TIMER") PC_NEXT_RUN_TIMER_MODE=$(printf '%q' "$NEXT_RUN_TIMER_MODE") PC_RUN_MODE=$(printf '%q' "${PC_RUN_MODE:-}") PC_AUTORUN_SOURCE=$(printf '%q' "$AUTORUN_SOURCE") ./001c-monitor-terminal.sh"

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
  pgrep -f "[0]01a-monitor-tk.py" >/dev/null 2>&1
}

next_run_timer_running() {
  pgrep -f "[n]ext-run-timer.py" >/dev/null 2>&1
}

start_next_run_timer() {
  if [ "$NEXT_RUN_TIMER" = "0" ]; then
    log "Next-run timer disabled by PC_NEXT_RUN_TIMER=0."
    return 0
  fi
  if [ "$NEXT_RUN_TIMER_MODE" != "tk" ]; then
    # CLI timer mode: nothing to start here. The terminal monitor itself execs
    # the CLI countdown in its own window when it closes after the run.
    # A Tk timer window left over from an earlier manual run would otherwise
    # stay open forever next to the CLI one — close it, unless a manual Tk
    # monitor session is open (then the floating timer belongs to it).
    if ! tk_monitor_running && pgrep -f "[0]02-next-run-timer\.py" >/dev/null 2>&1; then
      pkill -f "[0]02-next-run-timer\.py" 2>/dev/null || true
      log "Closed leftover Tk timer window (CLI timer mode, no Tk monitor session)."
    fi
    log "Next-run timer mode is '$NEXT_RUN_TIMER_MODE'; the CLI timer opens when the terminal monitor closes."
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
  nohup "$PYTHON_BIN" "$SCRIPT_DIR/002-next-run-timer.py" >> "$TIMER_LOG" 2>&1 &
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

  nohup "$PYTHON_BIN" "$SCRIPT_DIR/001a-monitor-tk.py" >> "$TK_LOG" 2>&1 &
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

published_monitor_value() {
  # Reads the ACTUAL bind the web monitor published (it moves to the next free
  # port when the configured one is taken by another program).
  local file="$PC_RUN_DIR/monitor_web.env"
  [ -f "$file" ] || return 0
  sed -n "s/^$1='\(.*\)'\$/\1/p" "$file" | head -n1
}

refresh_monitor_url() {
  local published
  published="$(published_monitor_value MONITOR_LOCAL_URL)"
  [ -n "$published" ] && MONITOR_URL="$published"
}

monitor_server_running() {
  refresh_monitor_url
  "$PYTHON_BIN" -c "from urllib.request import urlopen; urlopen('${MONITOR_URL}health', timeout=1).read()" >/dev/null 2>&1
}

start_web_monitor() {
  if monitor_server_running; then
    log "Web monitor already running at $MONITOR_URL."
  elif command -v systemctl >/dev/null 2>&1 \
       && systemctl --user is-enabled --quiet panamacompra-monitor-web.service 2>/dev/null; then
    log "Starting persistent user service: panamacompra-monitor-web.service."
    if systemctl --user restart panamacompra-monitor-web.service; then
      sleep 1
      refresh_monitor_url
      if monitor_server_running; then
        log "Web monitor service is healthy at $MONITOR_URL."
      else
        log "Web monitor service started but health check failed. Check journalctl --user -u panamacompra-monitor-web.service."
        return 1
      fi
    else
      log "Could not start panamacompra-monitor-web.service. Check journalctl --user -u panamacompra-monitor-web.service."
      return 1
    fi
  else
    PC_MONITOR_HOST="$MONITOR_HOST" PC_MONITOR_PORT="$MONITOR_PORT" nohup "$PYTHON_BIN" "$SCRIPT_DIR/001b-monitor-web.py" >> "$WEB_LOG" 2>&1 &
    sleep 1
    refresh_monitor_url
    log "Started web monitor at $MONITOR_URL with log $WEB_LOG."
  fi
}

open_url_if_possible() {
  if [ -z "${DISPLAY:-}" ]; then
    log "DISPLAY is empty; web monitor is available at $MONITOR_URL but browser was not opened."
    return 1
  fi

  # Prefer a chromeless app window (no browser header, no Firefox dependency);
  # the helper falls back to the default browser when no app-mode browser exists.
  if [ -x "$APP_ROOT/src/50_tools/130-open-web-app.sh" ]; then
    if "$APP_ROOT/src/50_tools/130-open-web-app.sh" "$MONITOR_URL" >> "$OPEN_LOG" 2>&1; then
      log "Opened web monitor via app-window helper: $MONITOR_URL."
      return 0
    fi
  fi

  if command -v xdg-open >/dev/null 2>&1; then
    nohup xdg-open "$MONITOR_URL" >/dev/null 2>&1 &
    log "Opened web monitor with xdg-open: $MONITOR_URL."
    return 0
  fi

  if command -v sensible-browser >/dev/null 2>&1; then
    nohup sensible-browser "$MONITOR_URL" >/dev/null 2>&1 &
    log "Opened web monitor with sensible-browser: $MONITOR_URL."
    return 0
  fi

  log "No supported browser opener found. Web monitor is available at $MONITOR_URL."
  return 1
}

start_log_follower_fallback() {
  if pgrep -f "[t]ail -f .*run_all_worker.log .*run_all_current.log" >/dev/null 2>&1; then
    log "Fallback log follower already running. Not starting another one."
    return 0
  fi

  nohup "$APP_ROOT/src/20_pipeline/130c-follow-run.sh" > "$FALLBACK_LOG" 2>&1 &
  log "No GUI monitor available. Started background log follower at $FALLBACK_LOG."
  log "Open a terminal and run: $APP_ROOT/src/20_pipeline/130c-follow-run.sh"
}

close_stale_cli_timer() {
  # 001c-monitor-terminal.sh execs into 002b-next-run-timer-cli.py when a
  # watched run finishes, so its process name stops matching the
  # "already running" pgrep above and this function opens a brand-new
  # window every cycle. The old CLI timer never notices the new run and
  # keeps sitting there holding the "terminal" instance's flock forever,
  # so every later handoff attempt fails to acquire that lock and the new
  # window's timer never appears (see 002b-next-run-timer-cli.py
  # acquire_lock). Only the "terminal" instance is closed here — a manual
  # `pcc timer cli --instance desktop` window is left alone.
  local stale_pid
  stale_pid="$(pgrep -af '[0]02b-next-run-timer-cli\.py' | grep -v -- '--instance desktop' | awk '{print $1}')"
  if [ -n "$stale_pid" ]; then
    kill $stale_pid 2>/dev/null || true
    log "Closed leftover CLI timer window (pid $stale_pid) before opening a fresh monitor."
  fi
}

open_progress_terminal() {
  if command -v gnome-terminal >/dev/null 2>&1; then
    nohup gnome-terminal --title="PanamaCompra Progress" -- bash -lc "$CMD" >/dev/null 2>&1 &
    log "Opened monitor with gnome-terminal on DISPLAY=$DISPLAY."
    return 0
  fi

  if command -v mate-terminal >/dev/null 2>&1; then
    nohup mate-terminal --title="PanamaCompra Progress" -- bash -lc "$CMD" >/dev/null 2>&1 &
    log "Opened monitor with mate-terminal on DISPLAY=$DISPLAY."
    return 0
  fi

  if command -v xfce4-terminal >/dev/null 2>&1; then
    nohup xfce4-terminal --title="PanamaCompra Progress" --command="bash -lc \"$CMD\"" >/dev/null 2>&1 &
    log "Opened monitor with xfce4-terminal on DISPLAY=$DISPLAY."
    return 0
  fi

  if command -v x-terminal-emulator >/dev/null 2>&1; then
    nohup x-terminal-emulator -T "PanamaCompra Progress" -e bash -lc "$CMD" >/dev/null 2>&1 &
    log "Opened monitor with x-terminal-emulator on DISPLAY=$DISPLAY."
    return 0
  fi

  if command -v xterm >/dev/null 2>&1; then
    nohup xterm -T "PanamaCompra Progress" -e bash -lc "$CMD" >/dev/null 2>&1 &
    log "Opened monitor with xterm on DISPLAY=$DISPLAY."
    return 0
  fi

  return 1
}

minimize_progress_window_if_auto() {
  # Unattended changedetection-triggered runs should not steal focus or pop
  # a window over whatever the operator is doing. Manual runs (MONITOR_MODE
  # picked by hand, PC_RUN_MODE != AUTO) keep popping up normally.
  [ "${PC_RUN_MODE:-}" = "AUTO" ] && [ "$AUTORUN_SOURCE" = "changedetection" ] || return 0
  command -v xdotool >/dev/null 2>&1 || return 0
  (
    for _ in $(seq 1 20); do
      win_id="$(xdotool search --name '^PanamaCompra Progress$' 2>/dev/null | head -n1)"
      if [ -n "$win_id" ]; then
        xdotool windowminimize "$win_id" 2>/dev/null || true
        log "Minimized automatic monitor window (id $win_id)."
        break
      fi
      sleep 0.25
    done
  ) &
}

prepare_gui_environment

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
  LAN_URL="$(published_monitor_value MONITOR_LAN_URL)"
  [ -n "$LAN_URL" ] && echo "LAN access from other PCs: $LAN_URL"
  exit 0
fi

# Terminal mode keeps the tiny synchronized countdown but avoids the full Tk
# dashboard. The terminal monitor itself auto-closes after the active run.
start_next_run_timer || true

if pgrep -f "[0]01c-monitor-terminal.sh" >/dev/null 2>&1; then
  log "Monitor already running. Not opening another window."
  exit 0
fi

close_stale_cli_timer

if [ -z "${DISPLAY:-}" ]; then
  log "DISPLAY is empty and no local X display was detected; cannot open GUI terminal."
  start_log_follower_fallback
  exit 0
fi

if open_progress_terminal; then
  minimize_progress_window_if_auto
  exit 0
fi

log "No supported terminal emulator found on DISPLAY=$DISPLAY."
start_log_follower_fallback
exit 0
