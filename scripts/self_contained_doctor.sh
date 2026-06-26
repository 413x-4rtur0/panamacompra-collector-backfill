#!/usr/bin/env bash
# Check whether the checkout is self-contained without committing secrets/runtime state.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/env.sh
source "$SCRIPT_DIR/../lib/env.sh"
cd "$APP_ROOT"

failures=0
warnings=0
pass() { printf 'PASS: %s\n' "$*"; }
warn() { warnings=$((warnings + 1)); printf 'WARN: %s\n' "$*"; }
fail() { failures=$((failures + 1)); printf 'FAIL: %s\n' "$*"; }

ignored_or_absent() {
  local path="$1"
  if [[ ! -e "$path" ]]; then
    pass "$path absent (ok)"
  elif git check-ignore -q "$path"; then
    pass "$path is git-ignored"
  elif [[ -d "$path" ]] && [[ -z "$(git ls-files "$path" ":!:$path/.gitkeep" 2>/dev/null || true)" ]]; then
    pass "$path has no tracked runtime files beyond optional .gitkeep"
  else
    fail "$path exists but is not git-ignored or placeholder-only"
  fi
}

port_check() {
  local label="$1" port="$2"
  if command -v ss >/dev/null 2>&1; then
    if ss -ltn "sport = :$port" | tail -n +2 | grep -q .; then
      warn "$label port $port appears to be in use; change the matching *_PORT in .env"
    else
      pass "$label port $port appears free"
    fi
  else
    warn "ss not available; cannot check $label port $port"
  fi
}

printf 'PanamaCompra self-contained deployment doctor\n'
printf 'APP_ROOT=%s\nCompose project=%s\n\n' "$APP_ROOT" "${COMPOSE_PROJECT_NAME:-panamacompra-collector}"

for required in docker-compose.yml docker/Dockerfile.webhook .dockerignore .env.example lib/env.sh bin/pcc; do
  [[ -e "$required" ]] && pass "required file exists: $required" || fail "missing required file: $required"
done

ignored_or_absent .env
ignored_or_absent .webhook_token
ignored_or_absent data
ignored_or_absent records
ignored_or_absent records_test
ignored_or_absent var/data
ignored_or_absent var/records
ignored_or_absent integrations

tracked_runtime="$(git ls-files .env .webhook_token data records records_test integrations var/data var/records ':!:var/data/.gitkeep' ':!:var/records/.gitkeep' 2>/dev/null || true)"
if [[ -n "$tracked_runtime" ]]; then
  fail "secret/runtime paths are tracked by git: $tracked_runtime"
else
  pass 'secret/runtime paths are not tracked by git except placeholder .gitkeep files'
fi

if command -v docker >/dev/null 2>&1; then
  pass 'docker command available'
  if docker compose version >/dev/null 2>&1; then
    pass 'docker compose plugin available'
    if docker compose -f docker-compose.yml config >/tmp/pcc-compose-config.out 2>&1; then
      pass 'docker compose config renders successfully'
    else
      fail 'docker compose config failed; see /tmp/pcc-compose-config.out'
    fi
  else
    warn 'docker compose plugin unavailable; install it before compose deployment'
  fi
else
  warn 'docker unavailable; host-only workflows can run, compose stack cannot'
fi

port_check changedetection "${CHANGEDETECTION_PORT:-5000}"
port_check waha "${WAHA_PORT:-3000}"
port_check webhook "${PC_WEBHOOK_PORT:-8765}"

cat <<'GUIDANCE'

Self-contained policy:
  - Source, scripts, Dockerfile, compose file, docs, and default config are tracked.
  - Secrets (.env, .webhook_token) and runtime state (data, records, var/*, integrations) are excluded.
  - Docker Compose uses a project name, so creating this stack beside other Docker stacks is healthy as long as host ports do not conflict.
  - If ports conflict, set CHANGEDETECTION_PORT, WAHA_PORT, or PC_WEBHOOK_PORT in .env before `docker compose up -d`.
GUIDANCE

printf '\nResult: %d failure(s), %d warning(s).\n' "$failures" "$warnings"
[[ "$failures" -eq 0 ]]
