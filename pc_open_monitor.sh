#!/usr/bin/env bash
set -uo pipefail

cd "$HOME/Apps/panamacompra-collector" || exit 1

mkdir -p data/logs

OPEN_LOG="data/logs/monitor_open.log"
CMD="cd '$HOME/Apps/panamacompra-collector' && ./pc_monitor_window.sh"

log() {
  echo "$(date '+%Y-%m-%d %H:%M:%S') | $*" >> "$OPEN_LOG"
}

if [ -z "${DISPLAY:-}" ]; then
  log "DISPLAY is empty. Cannot open GUI terminal from this environment."
  exit 0
fi

if pgrep -f "[p]c_monitor_window.sh" >/dev/null 2>&1; then
  log "Monitor already running. Not opening another window."
  exit 0
fi

if command -v gnome-terminal >/dev/null 2>&1; then
  nohup gnome-terminal --title="PanamaCompra Progress" -- bash -lc "$CMD" >/dev/null 2>&1 &
  log "Opened monitor with gnome-terminal."
  exit 0
fi

if command -v mate-terminal >/dev/null 2>&1; then
  nohup mate-terminal --title="PanamaCompra Progress" -- bash -lc "$CMD" >/dev/null 2>&1 &
  log "Opened monitor with mate-terminal."
  exit 0
fi

if command -v xfce4-terminal >/dev/null 2>&1; then
  nohup xfce4-terminal --title="PanamaCompra Progress" --command="bash -lc \"$CMD\"" >/dev/null 2>&1 &
  log "Opened monitor with xfce4-terminal."
  exit 0
fi

if command -v x-terminal-emulator >/dev/null 2>&1; then
  nohup x-terminal-emulator -T "PanamaCompra Progress" -e bash -lc "$CMD" >/dev/null 2>&1 &
  log "Opened monitor with x-terminal-emulator."
  exit 0
fi

if command -v xterm >/dev/null 2>&1; then
  nohup xterm -T "PanamaCompra Progress" -e bash -lc "$CMD" >/dev/null 2>&1 &
  log "Opened monitor with xterm."
  exit 0
fi

log "No supported terminal emulator found."
exit 0
