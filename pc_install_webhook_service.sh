#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" || exit 1

SERVICE_DIR="$HOME/.config/systemd/user"
SERVICE_FILE="$SERVICE_DIR/panamacompra-webhook.service"
mkdir -p "$SERVICE_DIR"

cat > "$SERVICE_FILE" <<EOF_SERVICE
[Unit]
Description=PanamaCompra webhook listener
After=network-online.target

[Service]
Type=simple
WorkingDirectory=$(pwd)
Environment=PC_WEBHOOK_HOST=${PC_WEBHOOK_HOST:-0.0.0.0}
Environment=PC_WEBHOOK_PORT=${PC_WEBHOOK_PORT:-8765}
ExecStart=$(pwd)/pc_start_webhook_listener.sh --replace-port-owner --foreground
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
