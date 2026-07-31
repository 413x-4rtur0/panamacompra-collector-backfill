#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
# shellcheck source=../../lib/env.sh
source "$SCRIPT_DIR/../../lib/env.sh"
cd "$APP_ROOT"

if ! command -v systemctl >/dev/null 2>&1; then
  echo "systemctl is not available; cannot install the persistent web-monitor service." >&2
  exit 1
fi

PYTHON_BIN="$APP_ROOT/.venv/bin/python"
if [ ! -x "$PYTHON_BIN" ]; then
  PYTHON_BIN="$(command -v python3)"
fi

SERVICE_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
SERVICE_FILE="$SERVICE_DIR/panamacompra-monitor-web.service"
mkdir -p "$SERVICE_DIR"

cat > "$SERVICE_FILE" <<EOF_SERVICE
[Unit]
Description=PanamaCompra Web Monitor
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$APP_ROOT
Environment=PYTHONUNBUFFERED=1
# Keep the persistent dashboard page available after a completed run.
Environment=PC_MONITOR_WEB_AUTO_CLOSE_SECONDS=0
ExecStart=$PYTHON_BIN $APP_ROOT/src/40_monitor/001b-monitor-web.py
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=panamacompra-monitor-web

[Install]
WantedBy=default.target
EOF_SERVICE

systemctl --user daemon-reload
systemctl --user enable --now panamacompra-monitor-web.service
systemctl --user restart panamacompra-monitor-web.service

echo "Installed user service: $SERVICE_FILE"
systemctl --user --no-pager --full status panamacompra-monitor-web.service || true
echo "Use: journalctl --user -u panamacompra-monitor-web.service -n 80 --no-pager"
echo "For persistence after logout, enable once if needed: sudo loginctl enable-linger $USER"
