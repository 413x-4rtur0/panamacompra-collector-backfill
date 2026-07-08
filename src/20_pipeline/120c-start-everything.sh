#!/usr/bin/env bash
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
# shellcheck source=../../lib/env.sh
source "$SCRIPT_DIR/../../lib/env.sh"
cd "$APP_ROOT"

# Counterpart to 120a-stop-everything.sh: brings back the background
# infrastructure a "Stop All" leaves down. Never fails the whole run over one
# piece being unavailable (e.g. no Docker installed) -- each step reports its
# own outcome and the script continues, matching update-local-copy.sh's
# "always make progress" style.

echo "Starting PanamaCompra background infrastructure..."

# ============================================================================
# STEP 1: Docker integration stack (changedetection, sockpuppetbrowser, WAHA,
# webhook container) -- a separate lifecycle from the host processes below.
# Skipped, not failed, when Docker/compose is not installed on this host.
# ============================================================================
echo "1) Docker integrations..."
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  "$APP_ROOT/src/50_tools/010-docker-stack.sh" up || echo "   WARNING: docker stack 'up' reported a problem; check ./src/50_tools/010-docker-stack.sh status."
  # The compose "webhook" service and the host-native systemd listener below
  # both bind PC_WEBHOOK_PORT; whichever starts last wins (020-start-listener.sh
  # --replace-port-owner kills the other). Since step 2 always (re)starts the
  # host listener when the systemd unit is enabled, the compose one is
  # guaranteed to lose and then crash-loop on Docker's own restart policy --
  # harmless but noisy. Stop just that one container up front so this run
  # ends in a clean, non-crash-looping state instead of leaving that behind.
  # Checking the container's current status first is racy: Docker's own
  # restart-policy loop can flip it between "restarting" and "running" faster
  # than this script can observe it. Just always stop it (idempotent and
  # harmless if it is already stopped/restarting) rather than trying to catch
  # it in exactly the right state.
  if systemctl --user is-enabled --quiet panamacompra-webhook.service 2>/dev/null \
     && docker compose config --services 2>/dev/null | grep -qx webhook; then
    echo "   Stopping the compose 'webhook' container: the host-native systemd listener (step 2) owns this port on this host."
    docker compose stop webhook >/dev/null 2>&1 || true
  fi
else
  echo "   Skipped: Docker (with the compose plugin) is not installed on this host."
fi

# ============================================================================
# STEP 2: Webhook listener -- prefer the systemd unit if this host has one
# installed (so it stays supervised/auto-restarting going forward), falling
# back to the plain background starter otherwise. Mirrors
# update-local-copy.sh's restart_webhook_listener().
# ============================================================================
echo ""
echo "2) Webhook listener..."
webhook_listener_running() {
  pgrep -f "[s]rc/10_webhook/010-webhook-listener.py" >/dev/null 2>&1
}
if systemctl --user is-enabled --quiet panamacompra-webhook.service 2>/dev/null; then
  echo "   Starting user systemd service: panamacompra-webhook.service"
  if systemctl --user restart panamacompra-webhook.service; then
    systemctl --user --no-pager --lines=0 status panamacompra-webhook.service || true
  else
    echo "   WARNING: systemd start failed; falling back to the plain background starter."
    ./src/10_webhook/020-start-listener.sh --replace-port-owner || echo "   WARNING: webhook listener did not start; check $PC_LOG_DIR/webhook_listener.out.log."
  fi
elif webhook_listener_running; then
  echo "   Webhook listener is already running."
else
  ./src/10_webhook/020-start-listener.sh --replace-port-owner || echo "   WARNING: webhook listener did not start; check $PC_LOG_DIR/webhook_listener.out.log."
fi

# ============================================================================
# STEP 3: Open the monitor, so the operator can see everything is back up.
# ============================================================================
echo ""
echo "3) Opening the monitor..."
if [ "${PC_START_ALL_OPEN_MONITOR:-1}" != "0" ] && [ -x "$APP_ROOT/src/40_monitor/000-open-monitor.sh" ]; then
  "$APP_ROOT/src/40_monitor/000-open-monitor.sh" >/dev/null 2>&1 &
  echo "   Requested. Set PC_START_ALL_OPEN_MONITOR=0 to skip this next time."
else
  echo "   Skipped (PC_START_ALL_OPEN_MONITOR=0)."
fi

echo ""
echo "============================================================"
echo "Start All finished. This brings background infrastructure back up; it"
echo "does not queue a collector run on its own -- request one from the"
echo "monitor or with ./src/20_pipeline/110a-request-run.sh."
echo "============================================================"
