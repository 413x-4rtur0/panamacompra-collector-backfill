#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
# shellcheck source=../../lib/env.sh
source "$SCRIPT_DIR/../../lib/env.sh"
cd "$APP_ROOT"

SERVICE_DIR="$HOME/.config/systemd/user"
SERVICE_FILE="$SERVICE_DIR/panamacompra-webhook.service"
mkdir -p "$SERVICE_DIR"

cat > "$SERVICE_FILE" <<EOF_SERVICE
[Unit]
Description=PanamaCompra webhook listener
After=network-online.target

[Service]
Type=simple
WorkingDirectory=$APP_ROOT
Environment=PC_WEBHOOK_HOST=${PC_WEBHOOK_HOST:-0.0.0.0}
Environment=PC_WEBHOOK_PORT=${PC_WEBHOOK_PORT:-8765}
ExecStart=$APP_ROOT/src/10_webhook/020-start-listener.sh --replace-port-owner --foreground
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF_SERVICE

systemctl --user daemon-reload
systemctl --user enable --now panamacompra-webhook.service
systemctl --user restart panamacompra-webhook.service
systemctl --user --no-pager --full status panamacompra-webhook.service || true

echo "Installed user service: $SERVICE_FILE"
echo "Use: journalctl --user -u panamacompra-webhook.service -n 80 --no-pager"
