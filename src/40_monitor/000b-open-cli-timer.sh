#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
# shellcheck source=../../lib/env.sh
source "$SCRIPT_DIR/../../lib/env.sh"

desktop_lock="/tmp/panamacompra_cli_timer_desktop.lock"
if command -v flock >/dev/null 2>&1 && [ -e "$desktop_lock" ]; then
  if ! flock -n "$desktop_lock" -c true; then
    exit 0
  fi
fi

title="PanamaCompra CLI Timer"
wm_class="PanamaCompraCliTimer"
geometry="${PC_NEXT_RUN_CLI_GEOMETRY:-100x22}"
zoom="${PC_NEXT_RUN_CLI_ZOOM:-0.90}"
command=("$APP_ROOT/bin/pcc" timer cli --instance desktop)

# Give the timer its own desktop identity so docks use the PanamaCompra icon
# instead of grouping it under a generic terminal icon.
if command -v gnome-terminal >/dev/null 2>&1; then
  exec gnome-terminal --class="$wm_class" --title="$title" --geometry="$geometry" --zoom="$zoom" -- "${command[@]}"
elif command -v xfce4-terminal >/dev/null 2>&1; then
  exec xfce4-terminal --class="$wm_class" --title="$title" --geometry="$geometry" -x "${command[@]}"
elif command -v mate-terminal >/dev/null 2>&1; then
  exec mate-terminal --class="$wm_class" --title="$title" --geometry="$geometry" --zoom="$zoom" -- "${command[@]}"
elif command -v kitty >/dev/null 2>&1; then
  exec kitty --class "$wm_class" --title "$title" "${command[@]}"
elif command -v alacritty >/dev/null 2>&1; then
  exec alacritty --class "$wm_class,$wm_class" --title "$title" -e "${command[@]}"
elif command -v konsole >/dev/null 2>&1; then
  exec konsole --name "$wm_class" -p tabtitle="$title" -e "${command[@]}"
elif command -v xterm >/dev/null 2>&1; then
  exec xterm -class "$wm_class" -title "$title" -geometry "$geometry" -fs 9 -e "${command[@]}"
elif command -v x-terminal-emulator >/dev/null 2>&1; then
  exec x-terminal-emulator -T "$title" -e "${command[@]}"
fi

printf 'No supported terminal emulator was found. Run: %s\n' "$APP_ROOT/bin/pcc timer cli" >&2
exit 1
