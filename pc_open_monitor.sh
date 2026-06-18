#!/usr/bin/env bash
set -uo pipefail

BASE_DIR="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
cd "$BASE_DIR" || exit 1

mkdir -p data/logs

OPEN_LOG="data/logs/monitor_open.log"
CMD="cd '$BASE_DIR' && mkdir -p data/logs && echo \"Monitor session started: $(date '+%Y-%m-%d %H:%M:%S')\" >> data/logs/monitor_session.log && PC_MONITOR_IDLE_CLOSE_SECONDS=3 ./pc_monitor_window.sh; code=\\$?; echo; echo \"============================================================\"; echo \" PanamaCompra monitor finished.\"; echo \" Exit code: \\$code\"; echo \" Log files:\"; echo \"   data/logs/run_all_worker.log\"; echo \"   data/logs/run_all_current.log\"; echo \"   data/logs/monitor_open.log\"; echo \"   data/logs/monitor_session.log\"; echo \"============================================================\"; echo; echo \"Press ENTER to close this window...\"; read -r _; exit \\$code"

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
