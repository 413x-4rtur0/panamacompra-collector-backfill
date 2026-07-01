#!/usr/bin/env bash
# Review legacy root entrypoints against ordered task wrappers.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/env.sh
source "$SCRIPT_DIR/../lib/env.sh"
cd "$APP_ROOT"

MAP_FILE="${1:-$APP_ROOT/config/script-name-map.tsv}"

printf 'PanamaCompra entrypoint naming review\n'
printf 'App root: %s\n' "$APP_ROOT"
printf 'Map:      %s\n\n' "$MAP_FILE"

if [[ ! -f "$MAP_FILE" ]]; then
  echo "ERROR: map file not found: $MAP_FILE" >&2
  exit 1
fi

status_for() {
  local path="$1"
  if [[ "$path" == "-" ]]; then
    printf 'n/a'
  elif [[ -x "$path" ]]; then
    printf 'ok executable'
  elif [[ -e "$path" ]]; then
    printf 'exists not-executable'
  else
    printf 'missing'
  fi
}

failures=0
printf '%-6s | %-34s | %-52s | %-32s\n' 'phase' 'legacy' 'ordered task' 'status'
printf '%s\n' '-------+------------------------------------+------------------------------------------------------+--------------------------------'
while IFS=$'\t' read -r phase legacy ordered canonical purpose; do
  [[ -z "${phase:-}" || "$phase" == \#* ]] && continue
  legacy_status="$(status_for "$legacy")"
  ordered_status="$(status_for "$ordered")"
  printf '%-6s | %-34s | %-52s | legacy=%s; task=%s\n' "$phase" "$legacy" "$ordered" "$legacy_status" "$ordered_status"
  if [[ "$ordered" != "-" && ! -x "$ordered" ]]; then
    failures=$((failures + 1))
  fi
done < "$MAP_FILE"

printf '\nMethodology: old root scripts stay for compatibility; ordered wrappers in scripts/tasks/ show install -> run -> stop -> uninstall hierarchy.\n'
if [[ "$failures" -gt 0 ]]; then
  printf 'Result: %d ordered task wrapper(s) missing or not executable.\n' "$failures" >&2
  exit 1
fi
printf 'Result: ordered task wrappers are present and executable.\n'

ORDERED_MAP="$APP_ROOT/config/ordered-script-map.tsv"
if [[ -f "$ORDERED_MAP" ]]; then
  printf '\nFull ordered alias map: %s\n' "$ORDERED_MAP"
  alias_failures=0
  while IFS=$'\t' read -r ordered current purpose; do
    [[ -z "${ordered:-}" || "$ordered" == \#* ]] && continue
    alias_path="scripts/ordered/$ordered"
    current_status="$(status_for "$current")"
    alias_status="$(status_for "$alias_path")"
    printf 'alias=%-42s target=%-36s target=%s; alias=%s\n' "$alias_path" "$current" "$current_status" "$alias_status"
    if [[ ! -x "$alias_path" || ! -e "$current" ]]; then
      alias_failures=$((alias_failures + 1))
    fi
  done < "$ORDERED_MAP"
  if [[ "$alias_failures" -gt 0 ]]; then
    printf 'Result: %d ordered alias issue(s) found.\n' "$alias_failures" >&2
    exit 1
  fi
  printf 'Result: full ordered aliases are present and executable.\n'
fi
