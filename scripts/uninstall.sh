#!/usr/bin/env bash
# Robust PanamaCompra Collector uninstaller / shutdown helper.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/env.sh
source "$SCRIPT_DIR/../lib/env.sh"
cd "$APP_ROOT"

ASSUME_YES=0
DRY_RUN=0
PURGE_DATA=0
PURGE_CONFIG=0
PURGE_INTEGRATIONS=0
PURGE_DOCKER_VOLUMES=0

usage() {
  cat <<'USAGE'
Usage: pcc uninstall [options]

Stops all PanamaCompra host runners, monitor/webhook services, and the Docker
Compose integration stack (changedetection.io, sockpuppetbrowser, WAHA, webhook).
By default it is conservative: it disables/removes service files and stops
processes/containers, but keeps data, records, integrations, and configuration.

Options:
  -y, --yes              Do not prompt before uninstall actions
      --dry-run          Print actions without changing anything
      --purge-data       Delete resolved runtime data/records/run/log directories
      --purge-config     Delete resolved config dir and repo-local .env/.webhook_token
      --purge-integrations
                          Delete ./integrations (changedetection + WAHA sessions)
      --docker-volumes   Pass --volumes to docker compose down
  -h, --help             Show this help
USAGE
}

log() { printf '[UNINSTALL] %s\n' "$*"; }
run() {
  if [[ "$DRY_RUN" == "1" ]]; then
    printf '[DRY-RUN]'; printf ' %q' "$@"; printf '\n'
  else
    "$@"
  fi
}

confirm() {
  [[ "$ASSUME_YES" == "1" || "$DRY_RUN" == "1" ]] && return 0
  cat <<EOF_CONFIRM
This will stop PanamaCompra host processes, disable user services, and stop the
Docker Compose stack including changedetection.io and WAHA.

Kept unless purge flags are used:
  data:         $PC_DATA_DIR
  records:      $PC_RECORDS_DIR
  config:       $PC_CONFIG_DIR
  integrations: $APP_ROOT/integrations
EOF_CONFIRM
  if [[ ! -t 0 ]]; then
    log "Refusing to prompt without a TTY; rerun with --yes or --dry-run."
    return 1
  fi
  read -r -p "Continue? [y/N] " reply
  [[ "$reply" =~ ^[Yy]$ ]]
}

systemctl_user() {
  command -v systemctl >/dev/null 2>&1 || return 0
  systemctl --user "$@" >/dev/null 2>&1 || true
}

stop_user_services() {
  log "Stopping and disabling user services."
  local services=(
    panamacompra.service
    panamacompra-webhook.service
  )
  for svc in "${services[@]}"; do
    run systemctl --user stop "$svc" 2>/dev/null || true
    run systemctl --user disable "$svc" 2>/dev/null || true
  done

  local user_service_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
  for svc in "${services[@]}"; do
    [[ -e "$user_service_dir/$svc" ]] && run rm -f "$user_service_dir/$svc"
  done
  systemctl_user daemon-reload
}

stop_host_processes() {
  log "Stopping host runners, collectors, monitors, updater, and webhook listener."
  if [[ -x "$APP_ROOT/pc_stop_run_all.sh" ]]; then
    run "$APP_ROOT/pc_stop_run_all.sh" || true
  fi

  local patterns=(
    '[p]c_run_all_flag_watcher.sh'
    '[s]cripts/run_worker.sh'
    '[p]ython3? -u ./pc_index_collector.py'
    '[p]ython3? -u ./pc_detail_downloader.py'
    '[p]ython3? -u ./pc_test_zone.py'
    '[p]ython3? -u ./pc_build_calendar.py'
    '[p]ython3? -u ./pc_monitor_tk.py'
    '[p]ython3? -u ./pc_monitor_server.py'
    '[p]c_next_run_timer.py'
    '[p]c_follow_run_all.sh'
    '[u]pdate_local_copy.sh'
    '[p]c_update_loader.py'
    '[p]c_update_before_run.sh'
    '[p]ython3? -u ./webhook_listener.py'
    '[w]ebhook_listener.py'
  )
  for pattern in "${patterns[@]}"; do
    run pkill -TERM -f "$pattern" 2>/dev/null || true
  done
  [[ "$DRY_RUN" == "1" ]] || sleep 2
  for pattern in "${patterns[@]}"; do
    run pkill -KILL -f "$pattern" 2>/dev/null || true
  done

  run mkdir -p "$PC_QUEUE_DIR" "$PC_LOG_DIR"
  run rm -f \
    "$PC_QUEUE_DIR/run_all_requested.flag" \
    "$PC_QUEUE_DIR/run_all_in_progress.flag" \
    "$PC_QUEUE_DIR/run_all_stop_no_resume.flag" \
    "$PC_QUEUE_DIR/update_monitor_requested.flag" \
    "$PC_QUEUE_DIR/update_monitor_in_progress.flag"
}

compose_cmd=()
detect_compose() {
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    compose_cmd=(docker compose)
  elif command -v docker-compose >/dev/null 2>&1; then
    compose_cmd=(docker-compose)
  else
    compose_cmd=()
  fi
}

stop_compose_stack() {
  detect_compose
  if [[ "${#compose_cmd[@]}" -eq 0 ]]; then
    log "Docker Compose not found; skipping changedetection.io/WAHA container shutdown."
    return 0
  fi
  [[ -f "$APP_ROOT/docker-compose.yml" ]] || return 0

  log "Stopping Docker Compose stack: changedetection.io, sockpuppetbrowser, WAHA, webhook."
  local down_args=(-f "$APP_ROOT/docker-compose.yml" down --remove-orphans)
  [[ "$PURGE_DOCKER_VOLUMES" == "1" ]] && down_args+=(--volumes)
  run "${compose_cmd[@]}" "${down_args[@]}" || true
}

purge_paths() {
  if [[ "$PURGE_DATA" == "1" ]]; then
    log "Purging runtime data, records, logs, and run directories."
    run rm -rf "$PC_DATA_DIR" "$PC_RECORDS_DIR" "$PC_RUN_DIR" "$APP_ROOT/var/log" "$APP_ROOT/records_test"
  fi
  if [[ "$PURGE_CONFIG" == "1" ]]; then
    log "Purging configuration and local secret files."
    run rm -rf "$PC_CONFIG_DIR"
    run rm -f "$APP_ROOT/.env" "$APP_ROOT/.webhook_token"
  fi
  if [[ "$PURGE_INTEGRATIONS" == "1" ]]; then
    log "Purging integration state, including changedetection datastore and WAHA sessions."
    run rm -rf "$APP_ROOT/integrations"
  fi
}

print_remaining() {
  log "Remaining related host processes:"
  pgrep -af 'pc_run_all|pc_index_collector|pc_detail_downloader|pc_test_zone|pc_build_calendar|pc_monitor|webhook_listener|update_local|changedetection|waha' || true
  detect_compose
  if [[ "${#compose_cmd[@]}" -gt 0 && -f "$APP_ROOT/docker-compose.yml" ]]; then
    log "Remaining compose services:"
    "${compose_cmd[@]}" -f "$APP_ROOT/docker-compose.yml" ps || true
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -y|--yes) ASSUME_YES=1 ;;
    --dry-run) DRY_RUN=1 ;;
    --purge-data) PURGE_DATA=1 ;;
    --purge-config) PURGE_CONFIG=1 ;;
    --purge-integrations) PURGE_INTEGRATIONS=1 ;;
    --docker-volumes) PURGE_DOCKER_VOLUMES=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

confirm || { log "Cancelled."; exit 1; }
stop_user_services
stop_host_processes
stop_compose_stack
purge_paths
print_remaining
log "Uninstall/shutdown complete."
