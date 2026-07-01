#!/usr/bin/env bash
# Install/remove the PanamaCompra Update + Monitor desktop launcher.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/env.sh
source "$SCRIPT_DIR/../lib/env.sh"
cd "$APP_ROOT"

ACTION="install"
INSTALL_DESKTOP=1

usage() {
  cat <<'USAGE'
Usage: pcc launcher [install|remove|path] [options]

Creates the Linux desktop/application-menu launcher for "PanamaCompra Update +
Monitor". The launcher opens src/monitor/update-loader.py first; after a successful
update, the normal monitor opens with the refreshed code.

Commands:
  install              Install/update application-menu and desktop launchers
  remove               Remove the launchers created by this script
  path                 Print expected launcher paths

Options:
      --no-desktop     Only install the application-menu entry
  -h, --help           Show this help
USAGE
}

log() { printf '[LAUNCHER] %s\n' "$*"; }

quote_desktop_value() {
  local value="$1"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  value="${value//\$/\\\$}"
  value="${value//\`/\\\`}"
  printf '"%s"' "$value"
}

app_dir="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
desktop_dir="${XDG_DESKTOP_DIR:-}"
if [[ -z "$desktop_dir" ]] && command -v xdg-user-dir >/dev/null 2>&1; then
  desktop_dir="$(xdg-user-dir DESKTOP 2>/dev/null || true)"
fi
[[ -n "$desktop_dir" && "$desktop_dir" != "$HOME" ]] || desktop_dir="$HOME/Desktop"
icon_dir="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/scalable/apps"
launcher_name="panamacompra-update-monitor.desktop"
app_path="$app_dir/$launcher_name"
desktop_path="$desktop_dir/$launcher_name"
icon_path="$icon_dir/panamacompra-update-monitor.svg"
loader_path="$APP_ROOT/src/monitor/update-loader.py"

write_icon() {
  mkdir -p "$icon_dir"
  cat > "$icon_path" <<'SVG'
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128">
  <rect width="128" height="128" rx="24" fill="#0f172a"/>
  <rect x="18" y="24" width="92" height="62" rx="8" fill="#111827" stroke="#38bdf8" stroke-width="6"/>
  <path d="M34 68h16l10-24 14 36 10-18h12" fill="none" stroke="#22c55e" stroke-width="7" stroke-linecap="round" stroke-linejoin="round"/>
  <path d="M38 104h52" stroke="#38bdf8" stroke-width="8" stroke-linecap="round"/>
  <path d="M86 18v18h18" fill="none" stroke="#facc15" stroke-width="7" stroke-linecap="round" stroke-linejoin="round"/>
  <path d="M104 18v18H86" fill="none" stroke="#facc15" stroke-width="7" stroke-linecap="round" stroke-linejoin="round"/>
</svg>
SVG
}

install_launcher() {
  if [[ ! -x "$loader_path" ]]; then
    log "Making updater loader executable: $loader_path"
    chmod +x "$loader_path"
  fi

  mkdir -p "$app_dir"
  write_icon

  local quoted_loader quoted_icon
  quoted_loader="$(quote_desktop_value "$loader_path")"
  quoted_icon="$(quote_desktop_value "$icon_path")"
  cat > "$app_path" <<DESKTOP
[Desktop Entry]
Type=Application
Name=PanamaCompra Update + Monitor
Comment=Update PanamaCompra Collector, then open the monitor
Exec=$quoted_loader --open-monitor-after
Icon=$quoted_icon
Terminal=false
Categories=Utility;Monitor;
StartupNotify=false
DESKTOP
  chmod +x "$app_path"
  log "Installed application-menu launcher: $app_path"

  if [[ "$INSTALL_DESKTOP" == "1" ]]; then
    if [[ -d "$desktop_dir" ]]; then
      cp "$app_path" "$desktop_path"
      chmod +x "$desktop_path"
      if command -v gio >/dev/null 2>&1; then
        gio set "$desktop_path" metadata::trusted true >/dev/null 2>&1 || true
      fi
      log "Installed desktop launcher: $desktop_path"
      log "If your desktop asks, choose 'Allow Launching' or 'Trust and Launch' once."
    else
      log "Desktop folder not found ($desktop_dir); installed application-menu launcher only."
    fi
  fi

  if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$app_dir" >/dev/null 2>&1 || true
  fi
  if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -q "${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor" >/dev/null 2>&1 || true
  fi
}

remove_launcher() {
  rm -f "$app_path" "$desktop_path"
  log "Removed launcher files if present: $app_path $desktop_path"
  if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$app_dir" >/dev/null 2>&1 || true
  fi
}

print_paths() {
  printf 'Application menu: %s\n' "$app_path"
  printf 'Desktop:          %s\n' "$desktop_path"
  printf 'Icon:             %s\n' "$icon_path"
  printf 'Exec target:      %s --open-monitor-after\n' "$loader_path"
}

if [[ $# -gt 0 ]]; then
  case "$1" in
    install|remove|path) ACTION="$1"; shift ;;
  esac
fi
while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-desktop) INSTALL_DESKTOP=0 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

case "$ACTION" in
  install) install_launcher ;;
  remove) remove_launcher ;;
  path) print_paths ;;
  *) echo "Unknown launcher action: $ACTION" >&2; usage >&2; exit 2 ;;
esac
