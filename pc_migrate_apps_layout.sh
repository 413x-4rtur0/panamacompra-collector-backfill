#!/usr/bin/env bash
set -euo pipefail

# Consolidate older split PanamaCompra app folders into this checkout.
# Default is DRY-RUN. Use --apply to copy/link files.

APPS_ROOT="${APPS_ROOT:-/Apps}"
COLLECTOR_DIR="${COLLECTOR_DIR:-$APPS_ROOT/panamacompra-collector}"
APPLY=0
LINK_LEGACY=0

usage() {
  cat <<USAGE
Usage: $0 [--apply] [--link-legacy] [--apps-root /Apps] [--collector /Apps/panamacompra-collector]

Consolidates legacy folders:
  /Apps/panamacompra-monitor          -> collector/docker/changedetection-data
  /Apps/panamacompra-webhook-receiver -> collector/.webhook_token (if present) and optional symlink
  /Apps/waha                          -> collector/docker/waha-sessions

Default is dry-run. --apply performs copies. --link-legacy also renames legacy
folders to *.bak-<timestamp> and creates compatibility symlinks.
USAGE
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1 ;;
    --link-legacy) LINK_LEGACY=1 ;;
    --apps-root) APPS_ROOT="$2"; shift ;;
    --collector) COLLECTOR_DIR="$2"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

CURRENT_DIR="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
STAMP="$(date +%Y%m%d_%H%M%S)"

say() { printf '%s\n' "$*"; }
do_cmd() {
  if [ "$APPLY" = "1" ]; then
    say "+ $*"
    "$@"
  else
    say "DRY-RUN: $*"
  fi
}

copy_dir_contents() {
  local src="$1"
  local dst="$2"
  if [ ! -d "$src" ]; then
    say "Skip missing source: $src"
    return 0
  fi
  do_cmd mkdir -p "$dst"
  if [ "$APPLY" = "1" ]; then
    say "+ copy contents $src -> $dst"
    cp -a "$src"/. "$dst"/
  else
    say "DRY-RUN: copy contents $src -> $dst"
  fi
}

copy_file_if_missing() {
  local src="$1"
  local dst="$2"
  if [ ! -f "$src" ]; then
    say "Skip missing file: $src"
    return 0
  fi
  if [ -e "$dst" ]; then
    say "Keep existing file: $dst"
    return 0
  fi
  do_cmd mkdir -p "$(dirname "$dst")"
  do_cmd cp -a "$src" "$dst"
}

link_legacy_dir() {
  local legacy="$1"
  local target="$2"
  [ "$LINK_LEGACY" = "1" ] || return 0
  if [ -L "$legacy" ]; then
    say "Legacy path already symlink: $legacy -> $(readlink "$legacy")"
    return 0
  fi
  if [ -e "$legacy" ]; then
    local backup="${legacy}.bak-${STAMP}"
    do_cmd mv "$legacy" "$backup"
    say "Legacy folder backed up as: $backup"
  fi
  do_cmd ln -s "$target" "$legacy"
}

say "PanamaCompra /Apps layout consolidation"
say "Current checkout: $CURRENT_DIR"
say "Target collector: $COLLECTOR_DIR"
[ "$APPLY" = "1" ] || say "Mode: DRY-RUN (add --apply to change files)"
[ "$APPLY" = "1" ] && say "Mode: APPLY"
say ""

if [ "$CURRENT_DIR" != "$COLLECTOR_DIR" ]; then
  say "This script is running from $CURRENT_DIR, not $COLLECTOR_DIR."
  say "It will still consolidate data into $COLLECTOR_DIR. Run from the target checkout for normal updates."
fi

do_cmd mkdir -p "$COLLECTOR_DIR/docker/changedetection-data" "$COLLECTOR_DIR/docker/waha-sessions" "$COLLECTOR_DIR/data/queue" "$COLLECTOR_DIR/data/logs"

MONITOR_DIR="$APPS_ROOT/panamacompra-monitor"
WEBHOOK_DIR="$APPS_ROOT/panamacompra-webhook-receiver"
WAHA_DIR="$APPS_ROOT/waha"

copy_dir_contents "$MONITOR_DIR" "$COLLECTOR_DIR/docker/changedetection-data"
copy_dir_contents "$WAHA_DIR" "$COLLECTOR_DIR/docker/waha-sessions"
copy_file_if_missing "$WEBHOOK_DIR/.webhook_token" "$COLLECTOR_DIR/.webhook_token"
copy_file_if_missing "$CURRENT_DIR/.webhook_token" "$COLLECTOR_DIR/.webhook_token"

link_legacy_dir "$MONITOR_DIR" "$COLLECTOR_DIR/docker/changedetection-data"
link_legacy_dir "$WAHA_DIR" "$COLLECTOR_DIR/docker/waha-sessions"
link_legacy_dir "$WEBHOOK_DIR" "$COLLECTOR_DIR"

say ""
say "Next recommended checks:"
say "  cd $COLLECTOR_DIR"
say "  docker compose up -d webhook changedetection waha"
say "  ./pc_start_webhook_listener.sh   # only for all-host / host.docker.internal mode"
say "  ./pc_webhook_diagnostic.sh"
say ""
say "changedetection URL guidance:"
say "  Docker Compose path: json://webhook:8765/panamacompra/<TOKEN>?method=POST"
say "  Host listener path:  json://host.docker.internal:8765/panamacompra/<TOKEN>?method=POST"
