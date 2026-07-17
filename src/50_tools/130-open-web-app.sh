#!/usr/bin/env bash
# Open the local web dashboards (web monitor, changedetection.io, WAHA) in a
# lightweight chromeless "app window" — independent of Firefox and without the
# browser header (no address bar, tabs or toolbars).
#
#   ./src/50_tools/130-open-web-app.sh monitor           web monitor (starts the server if needed)
#   ./src/50_tools/130-open-web-app.sh changedetection   changedetection.io dashboard
#   ./src/50_tools/130-open-web-app.sh waha              WAHA dashboard (login: see integration-access.txt)
#   ./src/50_tools/130-open-web-app.sh http://host:port/ any URL
#
# Window engine, in order:
#   1. $PC_WEB_APP_BROWSER (explicit command, tried with --app= then plain URL)
#   2. Any Chromium-family browser in --app= mode (chromeless window)
#   3. A lightweight non-Firefox browser (GNOME Web/epiphany, falkon, …)
#   4. xdg-open / sensible-browser (regular default browser) as last resort
# Set PC_WEB_APP_MODE=browser to skip the app-window attempts and always use
# the regular default browser.
#
# Remember: the NATIVE monitor (pcc monitor with PC_MONITOR_MODE=tk, the
# default) needs no browser at all, and its Settings/WhatsApp tabs edit the
# changedetection/WAHA container settings directly.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
# shellcheck source=../../lib/env.sh
source "$SCRIPT_DIR/../../lib/env.sh"
cd "$APP_ROOT"

note() { printf '[web-app] %s\n' "$*"; }

# Monitor-saved settings win over .env, but only when non-empty (same rule as
# src/50_tools/010-docker-stack.sh) so the app opens the same URLs the stack uses.
MONITOR_SETTINGS="$PC_DATA_DIR/config/monitor_settings.env"
if [ -f "$MONITOR_SETTINGS" ]; then
  for key in CHANGEDETECTION_BASE_URL WAHA_PORT PC_MONITOR_HOST PC_MONITOR_PORT PC_WEB_APP_BROWSER PC_WEB_APP_MODE; do
    value="$(sed -n "s/^${key}=['\"]\{0,1\}\([^'\"]*\).*/\1/p" "$MONITOR_SETTINGS" | tail -n 1)"
    if [ -n "$value" ]; then
      export "$key=$value"
    fi
  done
fi

MONITOR_HOST="${PC_MONITOR_HOST:-127.0.0.1}"
MONITOR_PORT="${PC_MONITOR_PORT:-8766}"

PYTHON_BIN="python3"
[ -x "$APP_ROOT/.venv/bin/python" ] && PYTHON_BIN="$APP_ROOT/.venv/bin/python"

published_monitor_value() {
  # The web monitor publishes its ACTUAL bind (it moves to the next free port
  # when the configured one is busy) to run/monitor_web.env.
  local file="$PC_RUN_DIR/monitor_web.env"
  [ -f "$file" ] || return 0
  sed -n "s/^$1='\(.*\)'\$/\1/p" "$file" | head -n1
}

monitor_local_url() {
  local published
  published="$(published_monitor_value MONITOR_LOCAL_URL)"
  echo "${published:-http://${MONITOR_HOST}:${MONITOR_PORT}/}"
}

ensure_web_monitor() {
  if "$PYTHON_BIN" -c "from urllib.request import urlopen; urlopen('$(monitor_local_url)health', timeout=1).read()" >/dev/null 2>&1; then
    return 0
  fi
  if command -v systemctl >/dev/null 2>&1 \
     && systemctl --user is-enabled --quiet panamacompra-monitor-web.service 2>/dev/null; then
    note "Starting persistent user service: panamacompra-monitor-web.service"
    if ! systemctl --user restart panamacompra-monitor-web.service; then
      note "Could not start panamacompra-monitor-web.service. Check: journalctl --user -u panamacompra-monitor-web.service -n 80 --no-pager"
      return 1
    fi
    sleep 1
    if "$PYTHON_BIN" -c "from urllib.request import urlopen; urlopen('$(monitor_local_url)health', timeout=2).read()" >/dev/null 2>&1; then
      return 0
    fi
    note "Web monitor service started but its health check failed."
    return 1
  fi
  note "Starting the web monitor server at http://${MONITOR_HOST}:${MONITOR_PORT}/ (log: $PC_LOG_DIR/monitor_server.log)"
  mkdir -p "$PC_LOG_DIR"
  PC_MONITOR_HOST="$MONITOR_HOST" PC_MONITOR_PORT="$MONITOR_PORT" \
    nohup "$PYTHON_BIN" "$APP_ROOT/src/40_monitor/001b-monitor-web.py" >> "$PC_LOG_DIR/monitor_server.log" 2>&1 &
  sleep 1
}

launch() {
  nohup "$@" >/dev/null 2>&1 &
}

open_app_window() {
  local url="$1"
  local app_class="${2:-PanamaCompra}"
  local mode="${PC_WEB_APP_MODE:-app}"

  if [ -z "${DISPLAY:-}" ] && [ -z "${WAYLAND_DISPLAY:-}" ]; then
    note "No graphical display detected. Open manually: $url"
    return 1
  fi

  # 1. Explicit operator choice.
  if [ -n "${PC_WEB_APP_BROWSER:-}" ] && command -v "${PC_WEB_APP_BROWSER%% *}" >/dev/null 2>&1; then
    # shellcheck disable=SC2086  # allow flags inside PC_WEB_APP_BROWSER
    if [ "$mode" = "app" ]; then
      # The explicit override may be a non-Chromium browser whose CLI rejects
      # --class. Operators can include their own class flag in the override.
      launch ${PC_WEB_APP_BROWSER} --app="$url" && { note "Opened app window with PC_WEB_APP_BROWSER: $url"; return 0; }
    fi
    launch ${PC_WEB_APP_BROWSER} "$url" && { note "Opened with PC_WEB_APP_BROWSER: $url"; return 0; }
  fi

  if [ "$mode" = "app" ]; then
    # 2. Chromium-family --app mode: a plain window with NO header (no address
    #    bar, tabs or menus), fully independent of Firefox.
    local candidate
    for candidate in chromium chromium-browser google-chrome google-chrome-stable brave-browser microsoft-edge microsoft-edge-stable vivaldi opera; do
      if command -v "$candidate" >/dev/null 2>&1; then
        launch "$candidate" --app="$url" --class="$app_class"
        note "Opened chromeless app window with $candidate: $url"
        return 0
      fi
    done
    # 3. Lightweight non-Firefox browsers (minimal chrome, small footprint).
    for candidate in epiphany falkon qutebrowser midori surf; do
      if command -v "$candidate" >/dev/null 2>&1; then
        launch "$candidate" "$url"
        note "Opened with lightweight browser $candidate: $url"
        return 0
      fi
    done
    note "No app-mode capable browser found (install chromium for a headerless window); falling back to the default browser."
  fi

  # 4. Regular default browser.
  if command -v xdg-open >/dev/null 2>&1; then
    launch xdg-open "$url"
    note "Opened with xdg-open (default browser): $url"
    return 0
  fi
  if command -v sensible-browser >/dev/null 2>&1; then
    launch sensible-browser "$url"
    note "Opened with sensible-browser: $url"
    return 0
  fi
  note "No browser opener found. Open manually: $url"
  return 1
}

TARGET="${1:-help}"
case "$TARGET" in
  monitor|web-monitor)
    ensure_web_monitor
    open_app_window "$(monitor_local_url)" "PanamaCompraMonitorWeb"
    ;;
  changedetection|cd)
    open_app_window "${CHANGEDETECTION_BASE_URL:-http://localhost:5000}" "PanamaCompraChangedetection"
    ;;
  waha|whatsapp)
    note "WAHA dashboard login is in $PC_DATA_DIR/config/integration-access.txt (default admin / 12345678)."
    open_app_window "http://localhost:${WAHA_PORT:-3000}" "PanamaCompraWAHA"
    ;;
  http://*|https://*)
    open_app_window "$TARGET"
    ;;
  -h|--help|help)
    sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
    ;;
  *)
    echo "Unknown target: $TARGET (use monitor|changedetection|waha|URL)" >&2
    exit 2
    ;;
esac
