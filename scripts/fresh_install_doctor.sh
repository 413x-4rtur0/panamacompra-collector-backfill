#!/usr/bin/env bash
# Fresh-clone readiness checks for a from-zero install/rebuild.
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

check_file() {
  local path="$1"
  [[ -f "$path" ]] && pass "file exists: $path" || fail "missing file: $path"
}
check_exec() {
  local path="$1"
  [[ -x "$path" ]] && pass "executable: $path" || fail "missing/not executable: $path"
}
check_cmd() {
  local name="$1"
  command -v "$name" >/dev/null 2>&1 && pass "command available: $name" || warn "optional/required later command not found now: $name"
}

printf 'PanamaCompra fresh install doctor\n'
printf 'APP_ROOT=%s\nAPP_MODE=%s\nPC_DATA_DIR=%s\nPC_RECORDS_DIR=%s\nPC_CONFIG_DIR=%s\n\n' "$APP_ROOT" "$APP_MODE" "$PC_DATA_DIR" "$PC_RECORDS_DIR" "$PC_CONFIG_DIR"

check_file README.md
check_file requirements.txt
check_file .env.example
check_file config/defaults.env
check_file config/script-name-map.tsv
check_file config/ordered-script-map.tsv
check_exec bin/pcc
check_exec setup.sh
check_exec update_local_copy.sh
check_exec scripts/review_entrypoint_names.sh
check_exec scripts/install_desktop_launcher.sh
check_exec scripts/install_user_services.sh
check_exec scripts/uninstall.sh

for dir in scripts/tasks scripts/ordered systemd/user var; do
  [[ -d "$dir" ]] && pass "directory exists: $dir" || fail "missing directory: $dir"
done

check_cmd python3
check_cmd git
check_cmd bash
check_cmd systemctl
if command -v docker >/dev/null 2>&1; then
  if docker compose version >/dev/null 2>&1; then
    pass 'docker compose available'
  else
    warn 'docker exists but docker compose plugin is unavailable'
  fi
else
  warn 'docker not found; compose stack (changedetection/WAHA/webhook) cannot be managed until installed'
fi

if bash -n bin/pcc lib/env.sh setup.sh run_collector.sh pc_stop_run_all.sh scripts/*.sh scripts/tasks/*.sh scripts/ordered/*.sh; then
  pass 'shell syntax checks passed'
else
  fail 'shell syntax checks failed'
fi

if python3 -m compileall -q pc_common.py pc_update_loader.py pc_monitor_tk.py pc_monitor_server.py; then
  pass 'core Python files compile'
else
  fail 'core Python compile failed'
fi

if ./bin/pcc review-names >/tmp/pcc-review-names.out 2>&1; then
  pass 'ordered entrypoint review passes'
else
  fail 'ordered entrypoint review failed; see /tmp/pcc-review-names.out'
fi

if ./bin/pcc env >/tmp/pcc-env.out 2>&1; then
  pass 'pcc env resolves runtime paths'
else
  fail 'pcc env failed; see /tmp/pcc-env.out'
fi

printf '\nFresh install next steps:\n'
printf '  1. cp .env.example .env  # optional local overrides\n'
printf '  2. PC_SETUP_SKIP_APT=1 PC_SETUP_SKIP_BROWSER=1 ./setup.sh  # smoke/no-browser mode\n'
printf '  3. ./bin/pcc launcher install --no-desktop  # or omit --no-desktop on a desktop host\n'
printf '  4. ./bin/pcc service install --enable       # optional user services\n'
printf '  5. ./bin/pcc start 5 RESTART 1              # small manual smoke run\n'
printf '  6. ./bin/pcc monitor                        # observe progress\n'
printf '  7. ./bin/pcc uninstall --dry-run --yes      # review shutdown/purge plan\n'

printf '\nResult: %d failure(s), %d warning(s).\n' "$failures" "$warnings"
[[ "$failures" -eq 0 ]]
