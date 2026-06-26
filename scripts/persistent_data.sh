#!/usr/bin/env bash
# Export/import persistent PanamaCompra data for reinstall/migration.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/env.sh
source "$SCRIPT_DIR/../lib/env.sh"
cd "$APP_ROOT"

usage() {
  cat <<'USAGE'
Usage: pcc data export [bundle.tar.gz]
       pcc data import <bundle.tar.gz>
       pcc data summary

Exports/imports persistent downloaded data for reinstall/migration. The bundle
contains runtime data, downloaded records, integrations state, and non-secret
config files. It intentionally excludes `.env`, `.webhook_token`, and config/env.
USAGE
}

bundle_default() {
  printf '%s/panamacompra-persistent-%s.tar.gz' "$APP_ROOT" "$(date +%Y%m%d_%H%M%S)"
}

copy_if_dir() {
  local src="$1" dst="$2"
  [[ -d "$src" ]] || return 0
  mkdir -p "$(dirname "$dst")"
  cp -a "$src" "$dst"
}

export_bundle() {
  local bundle="${1:-$(bundle_default)}"
  local tmp
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' RETURN
  mkdir -p "$tmp/panamacompra-persistent"
  copy_if_dir "$PC_DATA_DIR" "$tmp/panamacompra-persistent/data"
  copy_if_dir "$PC_RECORDS_DIR" "$tmp/panamacompra-persistent/records"
  copy_if_dir "$APP_ROOT/integrations" "$tmp/panamacompra-persistent/integrations"
  local persistent_config_dir="$PC_CONFIG_DIR"
  if [[ "$persistent_config_dir" == "$APP_ROOT/config" ]]; then
    persistent_config_dir="$PC_DATA_DIR/config"
  fi
  copy_if_dir "$persistent_config_dir" "$tmp/panamacompra-persistent/config"
  rm -f "$tmp/panamacompra-persistent/config/env" "$tmp/panamacompra-persistent/.env" "$tmp/panamacompra-persistent/.webhook_token"
  cat > "$tmp/panamacompra-persistent/MANIFEST.txt" <<EOF_MANIFEST
Created: $(date '+%Y-%m-%d %H:%M:%S')
Source APP_ROOT: $APP_ROOT
Source APP_MODE: $APP_MODE
Included: data, records, integrations, non-secret config
Excluded: .env, .webhook_token, config/env
EOF_MANIFEST
  mkdir -p "$(dirname "$bundle")"
  tar -C "$tmp" -czf "$bundle" panamacompra-persistent
  printf 'Persistent data exported: %s\n' "$bundle"
}

import_bundle() {
  local bundle="${1:-}"
  [[ -n "$bundle" ]] || { usage >&2; exit 2; }
  [[ -f "$bundle" ]] || { echo "Bundle not found: $bundle" >&2; exit 1; }
  local tmp root
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' RETURN
  tar -C "$tmp" -xzf "$bundle"
  root="$tmp/panamacompra-persistent"
  [[ -d "$root" ]] || { echo "Invalid bundle: missing panamacompra-persistent/" >&2; exit 1; }
  mkdir -p "$PC_DATA_DIR" "$PC_RECORDS_DIR" "$PC_CONFIG_DIR"
  [[ -d "$root/data" ]] && cp -a "$root/data/." "$PC_DATA_DIR/"
  [[ -d "$root/records" ]] && cp -a "$root/records/." "$PC_RECORDS_DIR/"
  if [[ -d "$root/integrations" ]]; then
    mkdir -p "$APP_ROOT/integrations"
    cp -a "$root/integrations/." "$APP_ROOT/integrations/"
  fi
  if [[ -d "$root/config" ]]; then
    cp -a "$root/config/." "$PC_CONFIG_DIR/"
    rm -f "$PC_CONFIG_DIR/env"
  fi
  printf 'Persistent data imported from: %s\n' "$bundle"
}

summary() {
  printf 'Persistent data locations:\n'
  printf '  data:         %s (%s)\n' "$PC_DATA_DIR" "$([[ -d "$PC_DATA_DIR" ]] && du -sh "$PC_DATA_DIR" 2>/dev/null | awk '{print $1}' || echo missing)"
  printf '  records:      %s (%s)\n' "$PC_RECORDS_DIR" "$([[ -d "$PC_RECORDS_DIR" ]] && du -sh "$PC_RECORDS_DIR" 2>/dev/null | awk '{print $1}' || echo missing)"
  local persistent_config_dir="$PC_CONFIG_DIR"
  if [[ "$persistent_config_dir" == "$APP_ROOT/config" ]]; then
    persistent_config_dir="$PC_DATA_DIR/config"
  fi
  printf '  config:       %s (%s)\n' "$persistent_config_dir" "$([[ -d "$persistent_config_dir" ]] && du -sh "$persistent_config_dir" 2>/dev/null | awk '{print $1}' || echo missing)"
  printf '  integrations: %s (%s)\n' "$APP_ROOT/integrations" "$([[ -d "$APP_ROOT/integrations" ]] && du -sh "$APP_ROOT/integrations" 2>/dev/null | awk '{print $1}' || echo missing)"
}

cmd="${1:-summary}"
[[ $# -gt 0 ]] && shift || true
case "$cmd" in
  export) export_bundle "$@" ;;
  import) import_bundle "$@" ;;
  summary) summary ;;
  -h|--help|help) usage ;;
  *) echo "Unknown data command: $cmd" >&2; usage >&2; exit 2 ;;
esac
