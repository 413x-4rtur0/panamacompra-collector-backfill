#!/usr/bin/env bash
# Install/remove PanamaCompra user systemd services for this checkout.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/env.sh
source "$SCRIPT_DIR/../lib/env.sh"
cd "$APP_ROOT"

ACTION="install"
ENABLE=0
START_NOW=0

usage() {
  cat <<'USAGE'
Usage: pcc service install|remove|status [options]

Installs user-level systemd services for the current checkout. Generated unit
files use the resolved APP_ROOT, so they work from portable/development paths as
well as installed paths.

Options for install:
  --enable        Enable services for login startup
  --now           Start/restart services after installing
  --webhook-only  Install only panamacompra-webhook.service
  -h, --help      Show this help
USAGE
}

WEBHOOK_ONLY=0
SERVICE_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
MAIN_SERVICE="$SERVICE_DIR/panamacompra.service"
WEBHOOK_SERVICE="$SERVICE_DIR/panamacompra-webhook.service"

need_systemctl() {
  if ! command -v systemctl >/dev/null 2>&1; then
    echo "ERROR: systemctl is required for user service management." >&2
    exit 1
  fi
}

write_main_service() {
  cat > "$MAIN_SERVICE" <<EOF_SERVICE
[Unit]
Description=PanamaCompra Collector Service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment=APP_MODE=${APP_MODE}
WorkingDirectory=${APP_ROOT}
ExecStart=${APP_ROOT}/bin/pcc start
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=panamacompra

[Install]
WantedBy=default.target
EOF_SERVICE
}

write_webhook_service() {
  cat > "$WEBHOOK_SERVICE" <<EOF_SERVICE
[Unit]
Description=PanamaCompra Webhook Listener
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment=APP_MODE=${APP_MODE}
Environment=PC_WEBHOOK_HOST=${PC_WEBHOOK_HOST:-0.0.0.0}
Environment=PC_WEBHOOK_PORT=${PC_WEBHOOK_PORT:-8765}
WorkingDirectory=${APP_ROOT}
ExecStart=${APP_ROOT}/pc_start_webhook_listener.sh --replace-port-owner --foreground
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=panamacompra-webhook

[Install]
WantedBy=default.target
EOF_SERVICE
}

install_services() {
  need_systemctl
  mkdir -p "$SERVICE_DIR"
  if [[ "$WEBHOOK_ONLY" != "1" ]]; then
    write_main_service
    echo "Installed: $MAIN_SERVICE"
  fi
  write_webhook_service
  echo "Installed: $WEBHOOK_SERVICE"
  systemctl --user daemon-reload
  if [[ "$ENABLE" == "1" ]]; then
    [[ "$WEBHOOK_ONLY" == "1" ]] || systemctl --user enable panamacompra.service
    systemctl --user enable panamacompra-webhook.service
  fi
  if [[ "$START_NOW" == "1" ]]; then
    [[ "$WEBHOOK_ONLY" == "1" ]] || systemctl --user restart panamacompra.service
    systemctl --user restart panamacompra-webhook.service
  fi
}

remove_services() {
  need_systemctl
  systemctl --user stop panamacompra.service panamacompra-webhook.service >/dev/null 2>&1 || true
  systemctl --user disable panamacompra.service panamacompra-webhook.service >/dev/null 2>&1 || true
  rm -f "$MAIN_SERVICE" "$WEBHOOK_SERVICE"
  systemctl --user daemon-reload
  echo "Removed user services from: $SERVICE_DIR"
}

status_services() {
  need_systemctl
  systemctl --user --no-pager --full status panamacompra.service panamacompra-webhook.service || true
}

if [[ $# -gt 0 ]]; then
  case "$1" in
    install|remove|status) ACTION="$1"; shift ;;
  esac
fi
while [[ $# -gt 0 ]]; do
  case "$1" in
    --enable) ENABLE=1 ;;
    --now) START_NOW=1; ENABLE=1 ;;
    --webhook-only) WEBHOOK_ONLY=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

case "$ACTION" in
  install) install_services ;;
  remove) remove_services ;;
  status) status_services ;;
  *) echo "Unknown service action: $ACTION" >&2; usage >&2; exit 2 ;;
esac
