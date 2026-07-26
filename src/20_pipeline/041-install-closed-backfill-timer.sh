#!/usr/bin/env bash
set -euo pipefail

# Installs the priority-3 Closed-opportunities historical backfill as a
# systemd --user timer, matching the pattern
# src/40_monitor/030-install-web-service.sh uses for the monitor-web service.
# A timer (not changedetection) drives this one on purpose: old closures do
# not "change", so there is nothing for a snapshot diff to trigger on — see
# 039-run-closed-backfill.sh.

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
# shellcheck source=../../lib/env.sh
source "$SCRIPT_DIR/../../lib/env.sh"
cd "$APP_ROOT"

if ! command -v systemctl >/dev/null 2>&1; then
  echo "systemctl is not available; cannot install the Closed backfill timer." >&2
  exit 1
fi

SERVICE_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
SERVICE_FILE="$SERVICE_DIR/panamacompra-closed-backfill.service"
TIMER_FILE="$SERVICE_DIR/panamacompra-closed-backfill.timer"
mkdir -p "$SERVICE_DIR"

cat > "$SERVICE_FILE" <<EOF_SERVICE
[Unit]
Description=PanamaCompra Closed-opportunities historical backfill (priority 3, segmented)
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$APP_ROOT
ExecStart=$APP_ROOT/src/20_pipeline/039-run-closed-backfill.sh
StandardOutput=journal
StandardError=journal
SyslogIdentifier=panamacompra-closed-backfill
EOF_SERVICE

# Every 20 minutes by default (override PC_CLOSED_BACKFILL_INTERVAL, e.g.
# "10min" or "1h", and re-run this installer to apply it) — frequent enough
# to make steady progress toward PC_CLOSED_BACKFILL_DAYS without a single
# giant run, but each tick defers cleanly to priority 1/2 if either is busy
# (see 039-run-closed-backfill.sh), so a shorter interval is still safe.
BACKFILL_INTERVAL="${PC_CLOSED_BACKFILL_INTERVAL:-20min}"

cat > "$TIMER_FILE" <<EOF_TIMER
[Unit]
Description=Run the PanamaCompra Closed-opportunities historical backfill on a recurring segment schedule

[Timer]
OnBootSec=5min
OnUnitActiveSec=$BACKFILL_INTERVAL
Persistent=true

[Install]
WantedBy=timers.target
EOF_TIMER

systemctl --user daemon-reload
systemctl --user enable --now panamacompra-closed-backfill.timer

echo "Installed user timer: $TIMER_FILE (every $BACKFILL_INTERVAL)"
echo "Installed user service: $SERVICE_FILE"
systemctl --user --no-pager --full status panamacompra-closed-backfill.timer || true
echo "Use: journalctl --user -u panamacompra-closed-backfill.service -n 80 --no-pager"
echo "Or:  tail -f \"\$PC_LOG_DIR/closed_backfill_triggered.log\""
