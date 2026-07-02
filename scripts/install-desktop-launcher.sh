#!/usr/bin/env bash
# Install/remove PanamaCompra desktop launchers.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/env.sh
source "$SCRIPT_DIR/../lib/env.sh"
cd "$APP_ROOT"

ACTION="install"
INSTALL_DESKTOP=1
MONITOR_ONLY=0

usage() {
  cat <<'USAGE'
Usage: pcc launcher [install|remove|path] [options]

Creates Linux desktop/application-menu launchers for PanamaCompra operator tools:
Update + Monitor, changedetection.io, WAHA, low-resource Integration URLs, and Docker integrations. The Update +
Monitor launcher opens src/monitor/003-update-loader.py first; after a successful
update, the normal monitor opens with the refreshed code.

Commands:
  install              Install/update application-menu and desktop launchers
  remove               Remove the launchers created by this script
  path                 Print expected launcher paths

Options:
      --no-desktop     Only install application-menu entries
      --monitor-only   Install/remove only the Update + Monitor launcher
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
changedetection_icon_path="$icon_dir/panamacompra-changedetection.svg"
waha_icon_path="$icon_dir/panamacompra-waha.svg"
docker_icon_path="$icon_dir/panamacompra-docker-integrations.svg"
urls_icon_path="$icon_dir/panamacompra-integration-urls.svg"
helper_dir="${XDG_DATA_HOME:-$HOME/.local/share}/panamacompra/launchers"
loader_path="$APP_ROOT/src/monitor/003-update-loader.py"

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
  cat > "$changedetection_icon_path" <<'SVG'
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128">
  <rect width="128" height="128" rx="24" fill="#052e16"/>
  <circle cx="64" cy="64" r="42" fill="#064e3b" stroke="#22c55e" stroke-width="7"/>
  <path d="M35 66c16-22 42-22 58 0-16 22-42 22-58 0z" fill="#bbf7d0"/>
  <circle cx="64" cy="66" r="13" fill="#16a34a"/>
  <path d="M88 28l12 12M100 28L88 40" stroke="#facc15" stroke-width="7" stroke-linecap="round"/>
</svg>
SVG
  cat > "$waha_icon_path" <<'SVG'
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128">
  <rect width="128" height="128" rx="24" fill="#022c22"/>
  <path d="M32 30h64a14 14 0 0114 14v34a14 14 0 01-14 14H58l-24 18 7-18h-9a14 14 0 01-14-14V44a14 14 0 0114-14z" fill="#14532d" stroke="#22c55e" stroke-width="6"/>
  <path d="M45 57c6 18 20 28 38 31l9-11-14-9-7 7c-9-4-15-10-18-18l7-7-9-14-11 8z" fill="#dcfce7"/>
</svg>
SVG
  cat > "$docker_icon_path" <<'SVG'
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128">
  <rect width="128" height="128" rx="24" fill="#082f49"/>
  <path d="M24 70h78c-4 22-20 34-46 34-18 0-30-7-38-20H8c6-4 10-8 12-14z" fill="#38bdf8"/>
  <g fill="#e0f2fe"><rect x="31" y="38" width="14" height="14" rx="2"/><rect x="49" y="38" width="14" height="14" rx="2"/><rect x="67" y="38" width="14" height="14" rx="2"/><rect x="49" y="22" width="14" height="14" rx="2"/><rect x="31" y="54" width="14" height="14" rx="2"/><rect x="49" y="54" width="14" height="14" rx="2"/><rect x="67" y="54" width="14" height="14" rx="2"/></g>
  <path d="M94 55c8 0 15-5 18-12 4 8 1 18-7 23" fill="none" stroke="#bae6fd" stroke-width="6" stroke-linecap="round"/>
</svg>
SVG
  cat > "$urls_icon_path" <<'SVG'
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128">
  <rect width="128" height="128" rx="24" fill="#1e1b4b"/>
  <path d="M49 47l-8 8a18 18 0 1025 25l8-8" fill="none" stroke="#a5b4fc" stroke-width="9" stroke-linecap="round"/>
  <path d="M79 81l8-8a18 18 0 10-25-25l-8 8" fill="none" stroke="#67e8f9" stroke-width="9" stroke-linecap="round"/>
  <path d="M52 76l24-24" stroke="#fef3c7" stroke-width="8" stroke-linecap="round"/>
</svg>
SVG
}


write_helper_scripts() {
  mkdir -p "$helper_dir"
  cat > "$helper_dir/open-changedetection.sh" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
APP_ROOT_VALUE="__APP_ROOT__"
cd "$APP_ROOT_VALUE"
# shellcheck source=lib/env.sh
source "$APP_ROOT_VALUE/lib/env.sh"
settings="$PC_DATA_DIR/config/monitor_settings.env"
if [[ -f "$settings" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$settings"
  set +a
fi
url="${CHANGEDETECTION_BASE_URL:-http://localhost:5000}"
if command -v xdg-open >/dev/null 2>&1; then exec xdg-open "$url"; fi
if command -v sensible-browser >/dev/null 2>&1; then exec sensible-browser "$url"; fi
printf 'Open changedetection.io at: %s\n' "$url"
SH
  cat > "$helper_dir/open-waha.sh" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
APP_ROOT_VALUE="__APP_ROOT__"
cd "$APP_ROOT_VALUE"
# shellcheck source=lib/env.sh
source "$APP_ROOT_VALUE/lib/env.sh"
settings="$PC_DATA_DIR/config/monitor_settings.env"
if [[ -f "$settings" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$settings"
  set +a
fi
url="http://localhost:${WAHA_PORT:-3000}"
if command -v xdg-open >/dev/null 2>&1; then exec xdg-open "$url"; fi
if command -v sensible-browser >/dev/null 2>&1; then exec sensible-browser "$url"; fi
printf 'Open WAHA at: %s\n' "$url"
SH
  cat > "$helper_dir/integration-urls.sh" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
APP_ROOT_VALUE="__APP_ROOT__"
cd "$APP_ROOT_VALUE"
# shellcheck source=lib/env.sh
source "$APP_ROOT_VALUE/lib/env.sh"
settings="$PC_DATA_DIR/config/monitor_settings.env"
if [[ -f "$settings" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$settings"
  set +a
fi
printf 'PanamaCompra low-resource integration access\n'
printf 'changedetection: %s\n' "${CHANGEDETECTION_BASE_URL:-http://localhost:5000}"
printf 'WAHA dashboard:  http://localhost:%s\n' "${WAHA_PORT:-3000}"
printf '\nDocker status (if docker is available):\n'
./src/tools/010-docker-stack.sh status || true
printf '\nThese are normal local web dashboards; open them in an already-running browser to avoid launching a new heavy browser.\n'
read -r -p "Press Enter to close..." _unused || true
SH
  cat > "$helper_dir/docker-integrations.sh" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
APP_ROOT_VALUE="__APP_ROOT__"
cd "$APP_ROOT_VALUE"
echo "PanamaCompra Docker integrations (changedetection + WAHA + webhook)"
echo "Starting/refreshing stack, then printing status..."
./src/tools/010-docker-stack.sh up || true
echo ""
./src/tools/010-docker-stack.sh status || true
echo ""
read -r -p "Press Enter to close..." _unused || true
SH
  for script in "$helper_dir"/*.sh; do
    sed -i "s|__APP_ROOT__|$APP_ROOT|g" "$script"
    chmod +x "$script"
  done
}

write_desktop_entry() {
  local target_app="$1" target_desktop="$2" name="$3" comment="$4" exec_value="$5" terminal="$6" categories="$7"
  local selected_icon="$icon_path" quoted_icon
  case "$target_app" in
    *changedetection*) selected_icon="$changedetection_icon_path" ;;
    *waha*) selected_icon="$waha_icon_path" ;;
    *docker-integrations*) selected_icon="$docker_icon_path" ;;
    *integration-urls*) selected_icon="$urls_icon_path" ;;
  esac
  quoted_icon="$(quote_desktop_value "$selected_icon")"
  cat > "$target_app" <<DESKTOP
[Desktop Entry]
Type=Application
Name=$name
Comment=$comment
Exec=$exec_value
Icon=$quoted_icon
Terminal=$terminal
Categories=$categories
StartupNotify=false
DESKTOP
  chmod +x "$target_app"
  log "Installed application-menu launcher: $target_app"
  if [[ "$INSTALL_DESKTOP" == "1" ]]; then
    mkdir -p "$desktop_dir"
    cp "$target_app" "$target_desktop"
    chmod +x "$target_desktop"
    if command -v gio >/dev/null 2>&1; then
      gio set "$target_desktop" metadata::trusted true >/dev/null 2>&1 || true
    fi
    log "Installed desktop launcher: $target_desktop"
  fi
}

install_integration_launchers() {
  [[ "$MONITOR_ONLY" == "1" ]] && return 0
  write_helper_scripts
  write_desktop_entry \
    "$app_dir/panamacompra-changedetection.desktop" \
    "$desktop_dir/panamacompra-changedetection.desktop" \
    "PanamaCompra changedetection" \
    "Open the changedetection.io watch dashboard" \
    "$(quote_desktop_value "$helper_dir/open-changedetection.sh")" \
    "false" "Utility;Monitor;Network;"
  write_desktop_entry \
    "$app_dir/panamacompra-waha.desktop" \
    "$desktop_dir/panamacompra-waha.desktop" \
    "PanamaCompra WAHA" \
    "Open the WAHA WhatsApp session dashboard" \
    "$(quote_desktop_value "$helper_dir/open-waha.sh")" \
    "false" "Utility;Monitor;Network;"
  write_desktop_entry \
    "$app_dir/panamacompra-integration-urls.desktop" \
    "$desktop_dir/panamacompra-integration-urls.desktop" \
    "PanamaCompra Integration URLs" \
    "Low-resource terminal view of changedetection and WAHA URLs" \
    "$(quote_desktop_value "$helper_dir/integration-urls.sh")" \
    "true" "Utility;Monitor;Network;"
  write_desktop_entry \
    "$app_dir/panamacompra-docker-integrations.desktop" \
    "$desktop_dir/panamacompra-docker-integrations.desktop" \
    "PanamaCompra Docker Integrations" \
    "Start/status changedetection, WAHA and webhook containers" \
    "$(quote_desktop_value "$helper_dir/docker-integrations.sh")" \
    "true" "Utility;Monitor;System;"
}

install_launcher() {
  if [[ ! -x "$loader_path" ]]; then
    log "Making updater loader executable: $loader_path"
    chmod +x "$loader_path"
  fi

  mkdir -p "$app_dir"
  write_icon

  local quoted_loader
  quoted_loader="$(quote_desktop_value "$loader_path")"
  write_desktop_entry     "$app_path" "$desktop_path"     "PanamaCompra Update + Monitor"     "Update PanamaCompra Collector, then open the monitor"     "$quoted_loader --open-monitor-after"     "false" "Utility;Monitor;"
  install_integration_launchers
  if [[ "$INSTALL_DESKTOP" == "1" ]]; then
    log "If your desktop asks, choose 'Allow Launching' or 'Trust and Launch' once."
  fi

  if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$app_dir" >/dev/null 2>&1 || true
  fi
  if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -q "${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor" >/dev/null 2>&1 || true
  fi
}

remove_launcher() {
  if [[ "$MONITOR_ONLY" == "1" ]]; then
    rm -f "$app_path" "$desktop_path"
  else
    rm -f "$app_path" "$desktop_path" \
      "$app_dir/panamacompra-changedetection.desktop" "$desktop_dir/panamacompra-changedetection.desktop" \
      "$app_dir/panamacompra-waha.desktop" "$desktop_dir/panamacompra-waha.desktop" \
      "$app_dir/panamacompra-integration-urls.desktop" "$desktop_dir/panamacompra-integration-urls.desktop" \
      "$app_dir/panamacompra-docker-integrations.desktop" "$desktop_dir/panamacompra-docker-integrations.desktop"
    rm -rf "$helper_dir"
  fi
  log "Removed PanamaCompra launcher files if present."
  if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$app_dir" >/dev/null 2>&1 || true
  fi
}

print_paths() {
  printf 'Application menu: %s\n' "$app_path"
  printf 'Desktop:          %s\n' "$desktop_path"
  printf 'Icon:             %s\n' "$icon_path"
  printf 'Exec target:      %s --open-monitor-after\n' "$loader_path"
  if [[ "$MONITOR_ONLY" != "1" ]]; then
    printf 'changedetection:  %s and %s\n' "$app_dir/panamacompra-changedetection.desktop" "$desktop_dir/panamacompra-changedetection.desktop"
    printf 'WAHA:             %s and %s\n' "$app_dir/panamacompra-waha.desktop" "$desktop_dir/panamacompra-waha.desktop"
    printf 'Integration URLs: %s and %s\n' "$app_dir/panamacompra-integration-urls.desktop" "$desktop_dir/panamacompra-integration-urls.desktop"
    printf 'Docker stack:     %s and %s\n' "$app_dir/panamacompra-docker-integrations.desktop" "$desktop_dir/panamacompra-docker-integrations.desktop"
    printf 'Helper scripts:   %s\n' "$helper_dir"
  fi
}
if [[ $# -gt 0 ]]; then
  case "$1" in
    install|remove|path) ACTION="$1"; shift ;;
  esac
fi
while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-desktop) INSTALL_DESKTOP=0 ;;
    --monitor-only) MONITOR_ONLY=1 ;;
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
