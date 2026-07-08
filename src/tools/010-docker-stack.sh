#!/usr/bin/env bash
# Manage the changedetection + sockpuppetbrowser + WAHA + webhook Docker stack.
#
#   ./src/tools/010-docker-stack.sh up        pull/start (or refresh) the stack
#   ./src/tools/010-docker-stack.sh down      stop and remove the containers
#   ./src/tools/010-docker-stack.sh restart   down + up
#   ./src/tools/010-docker-stack.sh status    show container state
#   ./src/tools/010-docker-stack.sh logs      tail the stack logs
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
  fail "Docker (with the compose plugin) is not installed. Install it and rerun: ./src/tools/010-docker-stack.sh $ACTION"
fi

# Monitor-saved container settings win over .env, but only when non-empty so a
# blank monitor field never wipes a value configured in .env.
MONITOR_SETTINGS="$PC_DATA_DIR/config/monitor_settings.env"
if [ -f "$MONITOR_SETTINGS" ]; then
  for key in CHANGEDETECTION_BASE_URL WAHA_PORT WAHA_API_KEY WAHA_ENGINE WAHA_DASHBOARD_USERNAME WAHA_DASHBOARD_PASSWORD PC_WEBHOOK_PORT; do
    value="$(sed -n "s/^${key}=['\"]\{0,1\}\([^'\"]*\).*/\1/p" "$MONITOR_SETTINGS" | tail -n 1)"
    if [ -n "$value" ]; then
      export "$key=$value"
    fi
  done
fi

export PC_INTEGRATIONS_DIR

CREDENTIALS_FILE="${PC_INTEGRATION_CREDENTIALS_FILE:-$PC_DATA_DIR/config/integration-access.txt}"

random_secret() {
  python3 - <<'PYSECRET'
import secrets
print(secrets.token_urlsafe(32))
PYSECRET
}

current_env_value() {
  local key="$1" file="${2:-.env}"
  [ -f "$file" ] || return 0
  sed -n "s/^${key}=['\"]\{0,1\}\([^'\"]*\).*/\1/p" "$file" | tail -n 1
}

set_env_value() {
  local key="$1" value="$2"
  touch .env
  if grep -qE "^${key}=" .env; then
    python3 - "$key" "$value" <<'PYSETENV'
from pathlib import Path
import sys
key, value = sys.argv[1], sys.argv[2]
path = Path('.env')
lines = path.read_text(encoding='utf-8').splitlines()
out = []
written = False
for line in lines:
    if line.startswith(f'{key}='):
        out.append(f'{key}={value}')
        written = True
    else:
        out.append(line)
if not written:
    out.append(f'{key}={value}')
path.write_text('\n'.join(out) + '\n', encoding='utf-8')
PYSETENV
  else
    printf '\n%s=%s\n' "$key" "$value" >> .env
  fi
  chmod 600 .env 2>/dev/null || true
}

ensure_access_credentials() {
  mkdir -p "$PC_DATA_DIR/config"
  local generated_any=0 webhook_token waha_key waha_dash_user waha_dash_pass

  if [ ! -s .webhook_token ]; then
    umask 077
    random_secret > .webhook_token
    chmod 600 .webhook_token 2>/dev/null || true
    generated_any=1
    note "Generated .webhook_token for changedetection/webhook requests."
  fi
  webhook_token="$(tr -d '\r\n' < .webhook_token 2>/dev/null || true)"

  waha_key="${WAHA_API_KEY:-}"
  if [ -z "$waha_key" ]; then
    waha_key="$(current_env_value WAHA_API_KEY .env)"
  fi
  if [ -z "$waha_key" ]; then
    waha_key="pc_waha_$(random_secret)"
    set_env_value WAHA_API_KEY "$waha_key"
    export WAHA_API_KEY="$waha_key"
    export PC_WAHA_API_KEY="${PC_WAHA_API_KEY:-$waha_key}"
    generated_any=1
    note "Generated WAHA_API_KEY in .env for the WAHA dashboard/API."
  fi

  # WAHA dashboard login. A fresh install/reinstall generates a RANDOM
  # password (audit Phase 5 — the previous fixed default meant every install
  # shipped a known login on the WAHA port). It is written to .env and to the
  # access note below, so the dashboard is never locked behind an unknown
  # password; change it any time from .env or the monitor Settings tab.
  waha_dash_user="${WAHA_DASHBOARD_USERNAME:-}"
  [ -n "$waha_dash_user" ] || waha_dash_user="$(current_env_value WAHA_DASHBOARD_USERNAME .env)"
  if [ -z "$waha_dash_user" ]; then
    waha_dash_user="admin"
    set_env_value WAHA_DASHBOARD_USERNAME "$waha_dash_user"
    generated_any=1
  fi
  export WAHA_DASHBOARD_USERNAME="$waha_dash_user"
  waha_dash_pass="${WAHA_DASHBOARD_PASSWORD:-}"
  [ -n "$waha_dash_pass" ] || waha_dash_pass="$(current_env_value WAHA_DASHBOARD_PASSWORD .env)"
  if [ -z "$waha_dash_pass" ]; then
    waha_dash_pass="$(random_secret | cut -c1-16)"
    set_env_value WAHA_DASHBOARD_PASSWORD "$waha_dash_pass"
    generated_any=1
    note "Generated a random WAHA dashboard password for user '$waha_dash_user' — saved in .env and in the access note."
  fi
  export WAHA_DASHBOARD_PASSWORD="$waha_dash_pass"

  {
    printf 'PanamaCompra integration access note\n'
    printf 'Generated/updated: %s\n\n' "$(date '+%Y-%m-%d %H:%M:%S')"
    printf 'changedetection.io dashboard\n'
    printf '  URL: %s\n' "${CHANGEDETECTION_BASE_URL:-http://localhost:5000}"
    printf '  Username/password: not generated by this installer; open the local dashboard and set a password in changedetection.io settings if this Docker host is reachable from another machine.\n\n'
    printf 'WAHA WhatsApp dashboard/API\n'
    printf '  URL: http://localhost:%s\n' "${WAHA_PORT:-3000}"
    printf '  Dashboard username: %s\n' "$waha_dash_user"
    printf '  Dashboard password: %s%s\n' "$waha_dash_pass" "$([ "$waha_dash_pass" = "12345678" ] && printf ' (legacy fixed default — change it in .env or the monitor Settings tab)')"
    printf '  API key (X-Api-Key header for /api requests): %s\n\n' "$waha_key"
    printf 'Webhook trigger for changedetection notifications\n'
    printf '  changedetection URL (Docker -> host, recommended): json://host.docker.internal:%s/panamacompra/%s?method=POST&format=text&overflow=truncate&rto=15&cto=10\n' "${PC_WEBHOOK_PORT:-8765}" "$webhook_token"
    printf '  Host HTTP test URL: http://host.docker.internal:%s/panamacompra/%s\n' "${PC_WEBHOOK_PORT:-8765}" "$webhook_token"
    printf '  Compose-only URL (requires changedetection to resolve webhook): json://webhook:8765/panamacompra/%s?method=POST&format=text&overflow=truncate&rto=15&cto=10\n\n' "$webhook_token"
    printf 'Files\n'
    printf '  WAHA key is stored in: %s/.env\n' "$APP_ROOT"
    printf '  Webhook token is stored in: %s/.webhook_token\n' "$APP_ROOT"
    printf '  This note is stored in: %s\n' "$CREDENTIALS_FILE"
  } > "$CREDENTIALS_FILE"
  chmod 600 "$CREDENTIALS_FILE" 2>/dev/null || true
  if [ "$generated_any" -eq 1 ]; then
    note "Integration credentials/access note written to: $CREDENTIALS_FILE"
  else
    note "Integration credentials/access note refreshed at: $CREDENTIALS_FILE"
  fi
}


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

# Recognize previous/parallel installations before starting:
#  * our own stack already running        -> just refresh it
#  * foreign containers on our host ports -> ask to stop them (tty) or warn
#  * old /Apps-style data folders         -> point to the migration helper
preflight() {
  if [ -n "$("${COMPOSE[@]}" ps -q 2>/dev/null)" ]; then
    note "This project's stack is already running — containers will be refreshed in place."
  fi

  local ports=("5000" "${WAHA_PORT:-3000}" "${PC_WEBHOOK_PORT:-8765}")
  local conflict_names=() conflict_lines=() port line name image workdir
  for port in "${ports[@]}"; do
    while IFS='|' read -r name image workdir; do
      [ -n "$name" ] || continue
      [ "$workdir" = "$APP_ROOT" ] && continue
      conflict_names+=("$name")
      conflict_lines+=("port $port is used by container '$name' ($image)${workdir:+ from $workdir}")
    done < <(docker ps --filter "publish=$port" --format '{{.Names}}|{{.Image}}|{{.Label "com.docker.compose.project.working_dir"}}' 2>/dev/null)
  done

  if [ "${#conflict_names[@]}" -gt 0 ]; then
    note "Detected containers from a previous/other installation using ports this stack needs:"
    printf '[docker-stack]   - %s\n' "${conflict_lines[@]}"
    note "Options: stop them now, keep them and change this stack's ports (WAHA server port in the monitor Settings, WAHA_PORT/PC_WEBHOOK_PORT in .env), or keep using the existing service."
    if [ -t 0 ]; then
      printf '[docker-stack] Stop the conflicting container(s) and continue? [y/N]: '
      read -r answer
      case "$answer" in
        y|Y|yes|YES|s|S|si|SI|sí)
          docker stop "${conflict_names[@]}" || fail "Could not stop the conflicting container(s)."
          note "Conflicting container(s) stopped. Their data was not touched."
          ;;
        *)
          fail "Aborted: adjust the ports or stop/uninstall the previous installation (see ./scripts/uninstall.sh in its folder), then rerun."
          ;;
      esac
    else
      note "WARNING: continuing non-interactively; services whose ports are busy will fail to start."
    fi
  fi

  for legacy_app in "${HOME:-/root}/Apps/waha" "/Apps/waha" "${HOME:-/root}/Apps/panamacompra-monitor" "/Apps/panamacompra-monitor" "${HOME:-/root}/Apps/panamacompra-webhook-receiver" "/Apps/panamacompra-webhook-receiver"; do
    if [ -d "$legacy_app" ] && [ ! -L "$legacy_app" ]; then
      note "Found previous installation data at $legacy_app — consolidate it into this checkout with ./src/tools/100-migrate-apps-layout.sh (dry-run by default)."
    fi
  done
}

case "$ACTION" in
  up)
    ensure_access_credentials
    preflight
    "${COMPOSE[@]}" up -d --remove-orphans || fail "docker compose up failed. Check that the Docker daemon is running and your user can access it."
    note "Stack is up."
    print_urls
    ;;
  down)
    "${COMPOSE[@]}" down --remove-orphans
    note "Stack stopped."
    ;;
  restart)
    ensure_access_credentials
    "${COMPOSE[@]}" down --remove-orphans
    preflight
    "${COMPOSE[@]}" up -d --remove-orphans || fail "docker compose up failed after restart."
    note "Stack restarted."
    print_urls
    ;;
  status)
    "${COMPOSE[@]}" ps
    print_urls
    [ -f "$CREDENTIALS_FILE" ] && note "Access note: $CREDENTIALS_FILE"
    ;;
  logs)
    shift
    "${COMPOSE[@]}" logs --tail 100 "$@"
    ;;
  *)
    fail "Unknown action: $ACTION (use up|down|restart|status|logs)"
    ;;
esac
