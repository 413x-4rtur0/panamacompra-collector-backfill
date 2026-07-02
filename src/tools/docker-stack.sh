#!/usr/bin/env bash
# Manage the changedetection + sockpuppetbrowser + WAHA + webhook Docker stack.
#
#   ./src/tools/docker-stack.sh up        pull/start (or refresh) the stack
#   ./src/tools/docker-stack.sh down      stop and remove the containers
#   ./src/tools/docker-stack.sh restart   down + up
#   ./src/tools/docker-stack.sh status    show container state
#   ./src/tools/docker-stack.sh logs      tail the stack logs
#
# Container data lives inside the self-contained state directory
# ($PC_INTEGRATIONS_DIR, default var/integrations — or the XDG state dir in
# installed mode), so the whole installation stays in one place. A legacy
# ./integrations folder at the repo root is moved there automatically (a
# compatibility symlink is left behind).
#
# Container-side settings saved from the monitors' Settings panels
# (CHANGEDETECTION_BASE_URL, WAHA_PORT, WAHA_API_KEY, WAHA_ENGINE in
# data/config/monitor_settings.env) are applied on the next `up`/`restart`,
# so the monitor can adjust changedetection and WAHA without editing .env.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
# shellcheck source=../../lib/env.sh
source "$SCRIPT_DIR/../../lib/env.sh"
cd "$APP_ROOT"

ACTION="${1:-status}"

note() { printf '[docker-stack] %s\n' "$*"; }
fail() { printf '[docker-stack] ERROR: %s\n' "$*" >&2; exit 1; }

if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE=(docker-compose)
else
  fail "Docker (with the compose plugin) is not installed. Install it and rerun: ./src/tools/docker-stack.sh $ACTION"
fi

# Monitor-saved container settings win over .env, but only when non-empty so a
# blank monitor field never wipes a value configured in .env.
MONITOR_SETTINGS="$PC_DATA_DIR/config/monitor_settings.env"
if [ -f "$MONITOR_SETTINGS" ]; then
  for key in CHANGEDETECTION_BASE_URL WAHA_PORT WAHA_API_KEY WAHA_ENGINE PC_WEBHOOK_PORT; do
    value="$(sed -n "s/^${key}=['\"]\{0,1\}\([^'\"]*\).*/\1/p" "$MONITOR_SETTINGS" | tail -n 1)"
    if [ -n "$value" ]; then
      export "$key=$value"
    fi
  done
fi

export PC_INTEGRATIONS_DIR

# One-time migration: move a legacy repo-root ./integrations folder into the
# self-contained state directory, leaving a symlink for older references.
legacy="$APP_ROOT/integrations"
if [ -d "$legacy" ] && [ ! -L "$legacy" ]; then
  if [ -z "$(ls -A "$PC_INTEGRATIONS_DIR" 2>/dev/null)" ]; then
    note "Moving legacy $legacy into $PC_INTEGRATIONS_DIR (containers are stopped first)."
    "${COMPOSE[@]}" down --remove-orphans >/dev/null 2>&1 || true
    rmdir "$PC_INTEGRATIONS_DIR" 2>/dev/null || true
    if mv "$legacy" "$PC_INTEGRATIONS_DIR"; then
      ln -s "$PC_INTEGRATIONS_DIR" "$legacy" 2>/dev/null || true
    else
      fail "Could not move $legacy to $PC_INTEGRATIONS_DIR. Move it manually, then rerun."
    fi
  else
    note "WARNING: both $legacy and $PC_INTEGRATIONS_DIR exist; the stack uses $PC_INTEGRATIONS_DIR. Merge or remove the legacy folder manually."
  fi
fi
mkdir -p "$PC_INTEGRATIONS_DIR/changedetection" "$PC_INTEGRATIONS_DIR/waha"

print_urls() {
  note "changedetection UI: ${CHANGEDETECTION_BASE_URL:-http://localhost:5000}"
  note "WAHA dashboard (pair WhatsApp by QR): http://localhost:${WAHA_PORT:-3000}"
  note "Container data: $PC_INTEGRATIONS_DIR"
}

case "$ACTION" in
  up)
    "${COMPOSE[@]}" up -d --remove-orphans || fail "docker compose up failed. Check that the Docker daemon is running and your user can access it."
    note "Stack is up."
    print_urls
    ;;
  down)
    "${COMPOSE[@]}" down --remove-orphans
    note "Stack stopped."
    ;;
  restart)
    "${COMPOSE[@]}" down --remove-orphans
    "${COMPOSE[@]}" up -d --remove-orphans || fail "docker compose up failed after restart."
    note "Stack restarted."
    print_urls
    ;;
  status)
    "${COMPOSE[@]}" ps
    print_urls
    ;;
  logs)
    shift
    "${COMPOSE[@]}" logs --tail 100 "$@"
    ;;
  *)
    fail "Unknown action: $ACTION (use up|down|restart|status|logs)"
    ;;
esac
