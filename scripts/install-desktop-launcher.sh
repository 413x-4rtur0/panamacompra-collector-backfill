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
Update + Monitor, Monitor Only (native Tk, no update), CLI Monitor, Next Run Timer, changedetection.io, WAHA, low-resource Integration URLs, Docker
integrations, Docker Manager (all containers on this host), Stop All / Start All,
and Dev Pause / Dev Resume. The Update +
Monitor launcher opens src/40_monitor/003-update-loader.py first; after a successful
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
monitor_only_icon_path="$icon_dir/panamacompra-monitor-only.svg"
cli_monitor_icon_path="$icon_dir/panamacompra-cli-monitor.svg"
timer_icon_path="$icon_dir/panamacompra-next-run-timer.svg"
changedetection_icon_path="$icon_dir/panamacompra-changedetection.svg"
waha_icon_path="$icon_dir/panamacompra-waha.svg"
docker_icon_path="$icon_dir/panamacompra-docker-integrations.svg"
docker_manager_icon_path="$icon_dir/panamacompra-docker-manager.svg"
urls_icon_path="$icon_dir/panamacompra-integration-urls.svg"
stop_icon_path="$icon_dir/panamacompra-stop-all.svg"
start_icon_path="$icon_dir/panamacompra-start-all.svg"
dev_pause_icon_path="$icon_dir/panamacompra-dev-pause.svg"
dev_resume_icon_path="$icon_dir/panamacompra-dev-resume.svg"
helper_dir="${XDG_DATA_HOME:-$HOME/.local/share}/panamacompra/launchers"
loader_path="$APP_ROOT/src/40_monitor/003-update-loader.py"

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
  cat > "$monitor_only_icon_path" <<'SVG'
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128">
  <rect width="128" height="128" rx="24" fill="#0f172a"/>
  <rect x="18" y="24" width="92" height="66" rx="8" fill="#111827" stroke="#22c55e" stroke-width="6"/>
  <path d="M33 68h15l10-24 14 36 10-18h13" fill="none" stroke="#38bdf8" stroke-width="7" stroke-linecap="round" stroke-linejoin="round"/>
  <circle cx="98" cy="38" r="8" fill="#4ade80"/>
  <path d="M42 106h44M64 90v16" stroke="#94a3b8" stroke-width="7" stroke-linecap="round"/>
</svg>
SVG
  cat > "$cli_monitor_icon_path" <<'SVG'
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128">
  <rect width="128" height="128" rx="24" fill="#07111f"/>
  <rect x="13" y="19" width="102" height="90" rx="12" fill="#111827" stroke="#22d3ee" stroke-width="6"/>
  <circle cx="28" cy="33" r="4" fill="#f87171"/><circle cx="41" cy="33" r="4" fill="#fbbf24"/><circle cx="54" cy="33" r="4" fill="#4ade80"/>
  <path d="M29 56l15 13-15 13M52 84h36" fill="none" stroke="#a7f3d0" stroke-width="7" stroke-linecap="round" stroke-linejoin="round"/>
  <circle cx="98" cy="85" r="9" fill="#38bdf8"/>
</svg>
SVG
  cat > "$timer_icon_path" <<'SVG'
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128">
  <rect width="128" height="128" rx="24" fill="#0f172a"/>
  <circle cx="64" cy="64" r="43" fill="#1e293b" stroke="#38bdf8" stroke-width="7"/>
  <path d="M64 35v31h25" fill="none" stroke="#fbbf24" stroke-width="8" stroke-linecap="round" stroke-linejoin="round"/>
  <circle cx="64" cy="66" r="7" fill="#22c55e"/>
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
  cat > "$stop_icon_path" <<'SVG'
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128">
  <rect width="128" height="128" rx="24" fill="#450a0a"/>
  <circle cx="64" cy="64" r="42" fill="#7f1d1d" stroke="#f87171" stroke-width="7"/>
  <rect x="46" y="46" width="36" height="36" rx="4" fill="#fecaca"/>
</svg>
SVG
  cat > "$start_icon_path" <<'SVG'
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128">
  <rect width="128" height="128" rx="24" fill="#052e16"/>
  <circle cx="64" cy="64" r="42" fill="#14532d" stroke="#4ade80" stroke-width="7"/>
  <path d="M52 42l38 22-38 22z" fill="#bbf7d0"/>
</svg>
SVG
  cat > "$dev_pause_icon_path" <<'SVG'
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128">
  <rect width="128" height="128" rx="24" fill="#451a03"/>
  <circle cx="64" cy="64" r="42" fill="#78350f" stroke="#fbbf24" stroke-width="7"/>
  <rect x="48" y="44" width="12" height="40" rx="3" fill="#fef3c7"/>
  <rect x="68" y="44" width="12" height="40" rx="3" fill="#fef3c7"/>
</svg>
SVG
  cat > "$dev_resume_icon_path" <<'SVG'
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128">
  <rect width="128" height="128" rx="24" fill="#1e1b4b"/>
  <circle cx="64" cy="64" r="42" fill="#312e81" stroke="#818cf8" stroke-width="7"/>
  <path d="M52 42l38 22-38 22z" fill="#e0e7ff"/>
</svg>
SVG
  cat > "$docker_manager_icon_path" <<'SVG'
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128">
  <rect width="128" height="128" rx="24" fill="#0c1420"/>
  <path d="M20 74h84c-4 20-20 32-46 32-18 0-30-7-38-20H6c5-4 9-8 11-12z" fill="#38bdf8"/>
  <g fill="#e0f2fe"><rect x="27" y="44" width="13" height="13" rx="2"/><rect x="44" y="44" width="13" height="13" rx="2"/><rect x="61" y="44" width="13" height="13" rx="2"/><rect x="44" y="29" width="13" height="13" rx="2"/><rect x="27" y="59" width="13" height="13" rx="2"/><rect x="44" y="59" width="13" height="13" rx="2"/><rect x="61" y="59" width="13" height="13" rx="2"/></g>
  <circle cx="96" cy="34" r="18" fill="#1e293b" stroke="#facc15" stroke-width="5"/>
  <path d="M89 26l14 14M103 26L89 40" stroke="#fef08a" stroke-width="5" stroke-linecap="round"/>
</svg>
SVG
}


write_helper_scripts() {
  mkdir -p "$helper_dir"
  cat > "$helper_dir/monitor-only.sh" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
APP_ROOT_VALUE="__APP_ROOT__"
cd "$APP_ROOT_VALUE"
# One-shot override: always open the native Tk monitor, never the updater
# loader. The operator's saved manual monitor preference is not changed.
export PC_MONITOR_MODE=tk
exec "$APP_ROOT_VALUE/src/40_monitor/000-open-monitor.sh"
SH
  cat > "$helper_dir/next-run-timer.sh" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
APP_ROOT_VALUE="__APP_ROOT__"
cd "$APP_ROOT_VALUE"
if pgrep -f "[0]02-next-run-timer.py" >/dev/null 2>&1; then
  exit 0
fi
python_bin="python3"
[[ -x "$APP_ROOT_VALUE/.venv/bin/python" ]] && python_bin="$APP_ROOT_VALUE/.venv/bin/python"
exec "$python_bin" "$APP_ROOT_VALUE/src/40_monitor/002-next-run-timer.py"
SH
  cat > "$helper_dir/cli-monitor.sh" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
APP_ROOT_VALUE="__APP_ROOT__"
cd "$APP_ROOT_VALUE"
title="PanamaCompra CLI Monitor"
wm_class="PanamaCompraCliMonitor"
command=("$APP_ROOT_VALUE/bin/pcc" watch)

# Launch a real terminal explicitly so the desktop can match the window to
# this launcher's icon instead of grouping it under a generic terminal icon.
if command -v gnome-terminal >/dev/null 2>&1; then
  exec gnome-terminal --class="$wm_class" --title="$title" -- "${command[@]}"
elif command -v xfce4-terminal >/dev/null 2>&1; then
  exec xfce4-terminal --class="$wm_class" --title="$title" -x "${command[@]}"
elif command -v mate-terminal >/dev/null 2>&1; then
  exec mate-terminal --class="$wm_class" --title="$title" -- "${command[@]}"
elif command -v kitty >/dev/null 2>&1; then
  exec kitty --class "$wm_class" --title "$title" "${command[@]}"
elif command -v alacritty >/dev/null 2>&1; then
  exec alacritty --class "$wm_class,$wm_class" --title "$title" -e "${command[@]}"
elif command -v konsole >/dev/null 2>&1; then
  exec konsole --name "$wm_class" -p tabtitle="$title" -e "${command[@]}"
elif command -v xterm >/dev/null 2>&1; then
  exec xterm -class "$wm_class" -title "$title" -e "${command[@]}"
elif command -v x-terminal-emulator >/dev/null 2>&1; then
  exec x-terminal-emulator -T "$title" -e "${command[@]}"
fi
printf 'No supported terminal emulator was found. Run: %s\n' "$APP_ROOT_VALUE/bin/pcc watch" >&2
exit 1
SH
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
# Chromeless app window when possible (no Firefox needed, no browser header);
# the helper falls back to the default browser by itself.
if [[ -x "$APP_ROOT_VALUE/src/50_tools/130-open-web-app.sh" ]]; then
  exec "$APP_ROOT_VALUE/src/50_tools/130-open-web-app.sh" changedetection
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
# Chromeless app window when possible (no Firefox needed, no browser header);
# the helper falls back to the default browser by itself.
if [[ -x "$APP_ROOT_VALUE/src/50_tools/130-open-web-app.sh" ]]; then
  exec "$APP_ROOT_VALUE/src/50_tools/130-open-web-app.sh" waha
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
./src/50_tools/010-docker-stack.sh status || true
printf '\nThese are normal local web dashboards; open them in an already-running browser to avoid launching a new heavy browser.\n'
read -r -p "Press Enter to close..." _unused || true
SH
  cat > "$helper_dir/docker-integrations.sh" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
APP_ROOT_VALUE="__APP_ROOT__"
cd "$APP_ROOT_VALUE"
echo "PanamaCompra Docker integrations (changedetection + WAHA; one safe webhook owner)"
echo "Starting/refreshing stack, then printing status..."
./src/50_tools/010-docker-stack.sh up || true
echo ""
./src/50_tools/010-docker-stack.sh status || true
echo ""
read -r -p "Press Enter to close..." _unused || true
SH
  cat > "$helper_dir/stop-all.sh" <<'SH'
#!/usr/bin/env bash
set -uo pipefail
APP_ROOT_VALUE="__APP_ROOT__"
cd "$APP_ROOT_VALUE"
echo "PanamaCompra: STOP ALL (workers, test zone, calendar builder, monitors, webhook listener, updaters)"
echo "Docker integration containers (changedetection/WAHA) are left running by design."
echo ""
./src/20_pipeline/120a-stop-everything.sh || true
echo ""
read -r -p "Press Enter to close..." _unused || true
SH
  cat > "$helper_dir/start-all.sh" <<'SH'
#!/usr/bin/env bash
set -uo pipefail
APP_ROOT_VALUE="__APP_ROOT__"
cd "$APP_ROOT_VALUE"
echo "PanamaCompra: START ALL (Docker integrations, webhook listener, monitor)"
echo "This brings background infrastructure back up; it does NOT queue a collector"
echo "run by itself -- request one from the monitor afterward."
echo ""
./src/20_pipeline/120c-start-everything.sh || true
echo ""
read -r -p "Press Enter to close..." _unused || true
SH
  cat > "$helper_dir/dev-pause.sh" <<'SH'
#!/usr/bin/env bash
set -uo pipefail
APP_ROOT_VALUE="__APP_ROOT__"
cd "$APP_ROOT_VALUE"
echo "PanamaCompra: PAUSE for development"
echo "Stops any active collection run and pauses webhook/cron auto-triggers and"
echo "the updater's autostash, so editing this repo is safe. Docker integrations"
echo "and the monitors stay running."
echo ""
./src/20_pipeline/121-dev-mode.sh pause || true
echo ""
read -r -p "Press Enter to close..." _unused || true
SH
  cat > "$helper_dir/dev-resume.sh" <<'SH'
#!/usr/bin/env bash
set -uo pipefail
APP_ROOT_VALUE="__APP_ROOT__"
cd "$APP_ROOT_VALUE"
echo "PanamaCompra: RESUME automatic collection"
echo "Restores webhook/cron auto-triggers and the updater's normal mode to what"
echo "they were before Dev Pause. Does not queue a run by itself."
echo ""
./src/20_pipeline/121-dev-mode.sh resume || true
echo ""
read -r -p "Press Enter to close..." _unused || true
SH
  cat > "$helper_dir/docker-manager.sh" <<'SH'
#!/usr/bin/env bash
set -uo pipefail
APP_ROOT_VALUE="__APP_ROOT__"
cd "$APP_ROOT_VALUE"
./src/50_tools/011-docker-manager.sh menu
SH
  for script in "$helper_dir"/*.sh; do
    sed -i "s|__APP_ROOT__|$APP_ROOT|g" "$script"
    chmod +x "$script"
  done
}

write_desktop_entry() {
  local target_app="$1" target_desktop="$2" name="$3" comment="$4" exec_value="$5" terminal="$6" categories="$7"
  local startup_wm_class="${8:-}" startup_line=""
  local selected_icon="$icon_path"
  case "$target_app" in
    *monitor-only*) selected_icon="$monitor_only_icon_path" ;;
    *cli-monitor*) selected_icon="$cli_monitor_icon_path" ;;
    *next-run-timer*) selected_icon="$timer_icon_path" ;;
    *changedetection*) selected_icon="$changedetection_icon_path" ;;
    *waha*) selected_icon="$waha_icon_path" ;;
    *docker-integrations*) selected_icon="$docker_icon_path" ;;
    *docker-manager*) selected_icon="$docker_manager_icon_path" ;;
    *integration-urls*) selected_icon="$urls_icon_path" ;;
    *stop-all*) selected_icon="$stop_icon_path" ;;
    *start-all*) selected_icon="$start_icon_path" ;;
    *dev-pause*) selected_icon="$dev_pause_icon_path" ;;
    *dev-resume*) selected_icon="$dev_resume_icon_path" ;;
  esac
  # The icon file is (re)generated on every install so no launcher is ever left
  # without one. Icon= is a plain string field in the Desktop Entry spec: it
  # must NOT be quoted — quotes become part of the path and the icon breaks.
  [[ -f "$selected_icon" ]] || write_icon
  [[ -z "$startup_wm_class" ]] || startup_line="StartupWMClass=$startup_wm_class"
  cat > "$target_app" <<DESKTOP
[Desktop Entry]
Type=Application
Name=$name
Comment=$comment
Exec=$exec_value
Icon=$selected_icon
Terminal=$terminal
Categories=$categories
StartupNotify=false
$startup_line
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
    "$app_dir/panamacompra-monitor-only.desktop" \
    "$desktop_dir/panamacompra-monitor-only.desktop" \
    "PanamaCompra Monitor Only" \
    "Open the native Tk monitor directly without updating the local copy" \
    "$(quote_desktop_value "$helper_dir/monitor-only.sh")" \
    "false" "Utility;Monitor;" "Panamacompramonitor"
  write_desktop_entry \
    "$app_dir/panamacompra-next-run-timer.desktop" \
    "$desktop_dir/panamacompra-next-run-timer.desktop" \
    "PanamaCompra Next Run Timer" \
    "Open the lightweight countdown synchronized with changedetection" \
    "$(quote_desktop_value "$helper_dir/next-run-timer.sh")" \
    "false" "Utility;Monitor;Clock;" "Panamacompratimer"
  write_desktop_entry \
    "$app_dir/panamacompra-cli-monitor.desktop" \
    "$desktop_dir/panamacompra-cli-monitor.desktop" \
    "PanamaCompra CLI Monitor" \
    "Open the lightweight interactive agent-style terminal monitor" \
    "$(quote_desktop_value "$helper_dir/cli-monitor.sh")" \
    "false" "Utility;Monitor;System;" "PanamaCompraCliMonitor"
  write_desktop_entry \
    "$app_dir/panamacompra-changedetection.desktop" \
    "$desktop_dir/panamacompra-changedetection.desktop" \
    "PanamaCompra changedetection" \
    "Open the changedetection.io watch dashboard" \
    "$(quote_desktop_value "$helper_dir/open-changedetection.sh")" \
    "false" "Utility;Monitor;Network;" "PanamaCompraChangedetection"
  write_desktop_entry \
    "$app_dir/panamacompra-waha.desktop" \
    "$desktop_dir/panamacompra-waha.desktop" \
    "PanamaCompra WAHA" \
    "Open the WAHA WhatsApp session dashboard" \
    "$(quote_desktop_value "$helper_dir/open-waha.sh")" \
    "false" "Utility;Monitor;Network;" "PanamaCompraWAHA"
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
    "Start/status changedetection and WAHA, keeping one safe webhook listener" \
    "$(quote_desktop_value "$helper_dir/docker-integrations.sh")" \
    "true" "Utility;Monitor;System;"
  write_desktop_entry \
    "$app_dir/panamacompra-stop-all.desktop" \
    "$desktop_dir/panamacompra-stop-all.desktop" \
    "PanamaCompra Stop All" \
    "Stop every PanamaCompra runner, monitor and webhook listener on this host" \
    "$(quote_desktop_value "$helper_dir/stop-all.sh")" \
    "true" "Utility;Monitor;System;"
  write_desktop_entry \
    "$app_dir/panamacompra-start-all.desktop" \
    "$desktop_dir/panamacompra-start-all.desktop" \
    "PanamaCompra Start All" \
    "Bring Docker integrations and the webhook listener back up, and open the monitor" \
    "$(quote_desktop_value "$helper_dir/start-all.sh")" \
    "true" "Utility;Monitor;System;"
  write_desktop_entry \
    "$app_dir/panamacompra-dev-pause.desktop" \
    "$desktop_dir/panamacompra-dev-pause.desktop" \
    "PanamaCompra Dev Pause" \
    "Pause automatic collection (webhook/cron triggers + updater autostash) so editing the repo is safe" \
    "$(quote_desktop_value "$helper_dir/dev-pause.sh")" \
    "true" "Utility;Monitor;System;Development;"
  write_desktop_entry \
    "$app_dir/panamacompra-dev-resume.desktop" \
    "$desktop_dir/panamacompra-dev-resume.desktop" \
    "PanamaCompra Dev Resume" \
    "Restore automatic collection settings paused by Dev Pause" \
    "$(quote_desktop_value "$helper_dir/dev-resume.sh")" \
    "true" "Utility;Monitor;System;Development;"
  write_desktop_entry \
    "$app_dir/panamacompra-docker-manager.desktop" \
    "$desktop_dir/panamacompra-docker-manager.desktop" \
    "PanamaCompra Docker Manager" \
    "Review/manage ALL Docker containers on this host (not just this repo's) — list, audit for orphaned/duplicate/stale ones, start/stop/disable/enable/remove" \
    "$(quote_desktop_value "$helper_dir/docker-manager.sh")" \
    "true" "Utility;Monitor;System;"
}

install_launcher() {
  if [[ ! -x "$loader_path" ]]; then
    log "Making updater loader executable: $loader_path"
    chmod +x "$loader_path"
  fi

  mkdir -p "$app_dir"
  # Removed in favor of panamacompra-update-monitor.desktop. Old installs left
  # this duplicate pointing at pc_update_loader.py, which no longer exists.
  rm -f "$app_dir/panamacompra-manual-monitor.desktop" "$desktop_dir/panamacompra-manual-monitor.desktop"
  write_icon

  local quoted_loader
  quoted_loader="$(quote_desktop_value "$loader_path")"
  write_desktop_entry     "$app_path" "$desktop_path"     "PanamaCompra Update + Monitor"     "Update PanamaCompra Collector, then open the monitor"     "$quoted_loader --open-monitor-after"     "false" "Utility;Monitor;" "Panamacompraupdater"
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
      "$app_dir/panamacompra-monitor-only.desktop" "$desktop_dir/panamacompra-monitor-only.desktop" \
      "$app_dir/panamacompra-cli-monitor.desktop" "$desktop_dir/panamacompra-cli-monitor.desktop" \
      "$app_dir/panamacompra-next-run-timer.desktop" "$desktop_dir/panamacompra-next-run-timer.desktop" \
      "$app_dir/panamacompra-waha.desktop" "$desktop_dir/panamacompra-waha.desktop" \
      "$app_dir/panamacompra-integration-urls.desktop" "$desktop_dir/panamacompra-integration-urls.desktop" \
      "$app_dir/panamacompra-docker-integrations.desktop" "$desktop_dir/panamacompra-docker-integrations.desktop" \
      "$app_dir/panamacompra-stop-all.desktop" "$desktop_dir/panamacompra-stop-all.desktop" \
      "$app_dir/panamacompra-start-all.desktop" "$desktop_dir/panamacompra-start-all.desktop" \
      "$app_dir/panamacompra-dev-pause.desktop" "$desktop_dir/panamacompra-dev-pause.desktop" \
      "$app_dir/panamacompra-dev-resume.desktop" "$desktop_dir/panamacompra-dev-resume.desktop" \
      "$app_dir/panamacompra-docker-manager.desktop" "$desktop_dir/panamacompra-docker-manager.desktop"
    rm -f "$app_dir/panamacompra-manual-monitor.desktop" "$desktop_dir/panamacompra-manual-monitor.desktop"
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
    printf 'Monitor Only:     %s and %s\n' "$app_dir/panamacompra-monitor-only.desktop" "$desktop_dir/panamacompra-monitor-only.desktop"
    printf 'CLI Monitor:      %s and %s\n' "$app_dir/panamacompra-cli-monitor.desktop" "$desktop_dir/panamacompra-cli-monitor.desktop"
    printf 'Next Run Timer:   %s and %s\n' "$app_dir/panamacompra-next-run-timer.desktop" "$desktop_dir/panamacompra-next-run-timer.desktop"
    printf 'WAHA:             %s and %s\n' "$app_dir/panamacompra-waha.desktop" "$desktop_dir/panamacompra-waha.desktop"
    printf 'Integration URLs: %s and %s\n' "$app_dir/panamacompra-integration-urls.desktop" "$desktop_dir/panamacompra-integration-urls.desktop"
    printf 'Docker stack:     %s and %s\n' "$app_dir/panamacompra-docker-integrations.desktop" "$desktop_dir/panamacompra-docker-integrations.desktop"
    printf 'Stop All:         %s and %s\n' "$app_dir/panamacompra-stop-all.desktop" "$desktop_dir/panamacompra-stop-all.desktop"
    printf 'Start All:        %s and %s\n' "$app_dir/panamacompra-start-all.desktop" "$desktop_dir/panamacompra-start-all.desktop"
    printf 'Dev Pause:        %s and %s\n' "$app_dir/panamacompra-dev-pause.desktop" "$desktop_dir/panamacompra-dev-pause.desktop"
    printf 'Dev Resume:       %s and %s\n' "$app_dir/panamacompra-dev-resume.desktop" "$desktop_dir/panamacompra-dev-resume.desktop"
    printf 'Docker Manager:   %s and %s\n' "$app_dir/panamacompra-docker-manager.desktop" "$desktop_dir/panamacompra-docker-manager.desktop"
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
