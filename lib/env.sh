#!/usr/bin/env bash
# PanamaCompra Collector environment resolver.
#
# This file is `source`d (not executed) by nearly every script in the
# project, so it must NOT change the caller's `set -e` behavior: `source` runs
# in the calling shell, and a `set -e` here would silently force strict-abort
# semantics onto scripts that deliberately opt out of it (e.g. run-worker.sh
# and stop-collectors.sh use `set -uo pipefail` without `-e` specifically so a
# single failing step, like a pre-run git update, does not abort the whole
# script). Only `-u`/`pipefail` are safe to set unconditionally here; nothing
# below needs `-e` to behave correctly (every failure path already has an
# explicit `exit`).
set -uo pipefail

_env_source="${BASH_SOURCE[0]}"
_env_dir="$(cd "$(dirname "$_env_source")" && pwd)"
export APP_ROOT="${APP_ROOT:-$(cd "$_env_dir/.." && pwd)}"
# So any "python -" heredoc or `python -c` invoked from a bash script (not just
# the *.py files, which insert this themselves) can `import common` / `from
# common import ...` regardless of which src/<category>/ directory it runs from.
export PYTHONPATH="$APP_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

export XDG_DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
export XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
export XDG_CACHE_HOME="${XDG_CACHE_HOME:-$HOME/.cache}"

load_env_file() {
  local file="$1"
  if [[ -f "$file" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$file"
    set +a
  fi
}

detect_mode() {
  if [[ -n "${APP_MODE:-}" ]]; then
    printf '%s\n' "$APP_MODE"
  elif [[ -f "$XDG_DATA_HOME/panamacompra/.installed" ]]; then
    printf 'installed\n'
  elif [[ -f "$APP_ROOT/var/.portable" ]]; then
    printf 'portable\n'
  elif [[ -d "$APP_ROOT/.git" ]]; then
    printf 'development\n'
  else
    printf 'portable\n'
  fi
}

export APP_MODE="$(detect_mode)"

case "$APP_MODE" in
  installed)
    export PC_STATE_DIR="${PC_STATE_DIR:-$XDG_DATA_HOME/panamacompra}"
    export PC_CONFIG_DIR="${PC_CONFIG_DIR:-$XDG_CONFIG_HOME/panamacompra}"
    ;;
  portable|development)
    export PC_STATE_DIR="${PC_STATE_DIR:-$APP_ROOT/var}"
    export PC_CONFIG_DIR="${PC_CONFIG_DIR:-$APP_ROOT/config}"
    ;;
  *)
    echo "Unsupported APP_MODE: $APP_MODE" >&2
    exit 2
    ;;
esac

# Load config before deriving remaining paths, so user path overrides are honored.
# A real environment variable set by the caller (e.g. `PC_SETUP_SKIP_APT=1
# ./setup.sh`) must always win over any of these files -- matching the
# "environment variable > file > built-in default" precedence documented
# throughout this project. Snapshot every currently-exported variable before
# the cascade so it can be restored afterward; the three files may still
# freely override each other and set brand-new variables in between.
_pre_config_env="$(export -p)"
load_env_file "$APP_ROOT/config/defaults.env"
load_env_file "$APP_ROOT/.env"
load_env_file "$PC_CONFIG_DIR/env"
eval "$_pre_config_env"
unset _pre_config_env

export PC_DATA_DIR="${PC_DATA_DIR:-$PC_STATE_DIR/data}"
export PC_RECORDS_DIR="${PC_RECORDS_DIR:-$PC_STATE_DIR/records}"
export PC_LOG_DIR="${PC_LOG_DIR:-$PC_DATA_DIR/logs}"
export PC_RUN_DIR="${PC_RUN_DIR:-$PC_STATE_DIR/run}"
export PC_QUEUE_DIR="${PC_QUEUE_DIR:-$PC_DATA_DIR/queue}"
export PC_ARCHIVE_DB_PATH="${PC_ARCHIVE_DB_PATH:-$PC_DATA_DIR/panamacompra_archive.db}"
export PC_INDEX_CSV_PATH="${PC_INDEX_CSV_PATH:-$PC_DATA_DIR/panamacompra_index.csv}"
export PC_CALENDAR_DIR="${PC_CALENDAR_DIR:-$PC_DATA_DIR/calendar}"
export PC_RECORDS_TEST_DIR="${PC_RECORDS_TEST_DIR:-$PC_STATE_DIR/records_test}"
# Docker bind-mount data for the changedetection + WAHA containers, kept inside
# the self-contained state directory (see src/tools/docker-stack.sh).
export PC_INTEGRATIONS_DIR="${PC_INTEGRATIONS_DIR:-$PC_STATE_DIR/integrations}"

# The WAHA container is protected with WAHA_API_KEY (docker-compose), while the
# notifier authenticates with PC_WAHA_API_KEY. Default one from the other so a
# single .env value keeps the collector able to reach a protected WAHA server.
export PC_WAHA_API_KEY="${PC_WAHA_API_KEY:-${WAHA_API_KEY:-}}"

mkdir -p "$PC_DATA_DIR" "$PC_DATA_DIR/config" "$PC_RECORDS_DIR" "$PC_RECORDS_TEST_DIR" "$PC_LOG_DIR" "$PC_RUN_DIR" "$PC_QUEUE_DIR" "$PC_CONFIG_DIR" "$PC_INTEGRATIONS_DIR"
