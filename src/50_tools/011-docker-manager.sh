#!/usr/bin/env bash
# Review and manage Docker containers on this HOST, not only the ones this
# repo's own stack starts (see 010-docker-stack.sh for that narrower job).
# Useful because this host tends to accumulate leftover/duplicate stacks from
# other projects (see var/... audit notes) that quietly hold ports or disk
# space; this tool surfaces them and lets you act without hand-typing `docker`.
#
#   ./src/50_tools/011-docker-manager.sh                just list all containers
#   ./src/50_tools/011-docker-manager.sh menu            interactive numbered menu
#   ./src/50_tools/011-docker-manager.sh list
#   ./src/50_tools/011-docker-manager.sh audit           flag orphaned/duplicate/stale containers
#   ./src/50_tools/011-docker-manager.sh inspect <name>
#   ./src/50_tools/011-docker-manager.sh logs <name> [tail_lines]
#   ./src/50_tools/011-docker-manager.sh start <name>
#   ./src/50_tools/011-docker-manager.sh stop <name>
#   ./src/50_tools/011-docker-manager.sh restart <name>
#   ./src/50_tools/011-docker-manager.sh disable <name>  stop + restart-policy=no (won't come back on reboot)
#   ./src/50_tools/011-docker-manager.sh enable <name>   restart-policy=unless-stopped + start
#   ./src/50_tools/011-docker-manager.sh rm <name> [--volumes]   remove a STOPPED container (asks to stop a running one)
#   ./src/50_tools/011-docker-manager.sh prune           remove stopped containers + dangling images (confirms first)
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
# shellcheck source=../../lib/env.sh
source "$SCRIPT_DIR/../../lib/env.sh"
cd "$APP_ROOT"

LOG_FILE="$PC_LOG_DIR/docker_manager.log"
THIS_PROJECT="$(basename "$APP_ROOT")"
STALE_DAYS="${PC_DOCKER_STALE_DAYS:-14}"
# Images this repo's own stack runs — used by `audit` to spot foreign
# containers running the same service under a different compose project
# (that pattern has bitten this host before: duplicate waha/changedetection
# stacks left running from an earlier setup attempt).
OWN_IMAGES=("dgtlmoon/sockpuppetbrowser" "dgtlmoon/changedetection.io" "ghcr.io/dgtlmoon/changedetection.io" "devlikeapro/waha")

note() { printf '[docker-manager] %s\n' "$*"; }
fail() { printf '[docker-manager] ERROR: %s\n' "$*" >&2; exit 1; }
log_action() {
  mkdir -p "$PC_LOG_DIR"
  printf '%s | %s\n' "$(date '+%Y-%m-%d_%H-%M-%S')" "$*" >> "$LOG_FILE"
}

command -v docker >/dev/null 2>&1 || fail "Docker is not installed or not on PATH."
docker info >/dev/null 2>&1 || fail "Docker daemon is not reachable (permission or not running)."

# '|'-delimited rows: id, name, image, status, ports, project, workdir, restart_policy
# (NOT tab-delimited: bash's `read` treats tab as IFS-whitespace and collapses
# consecutive tabs even with IFS=$'\t', which silently shifts every field
# whenever a container has an empty value, e.g. no published Ports.)
container_rows() {
  local id name image status ports project workdir policy
  while IFS='|' read -r id name image status ports project workdir; do
    [ -n "$id" ] || continue
    policy="$(docker inspect --format '{{.HostConfig.RestartPolicy.Name}}' "$id" 2>/dev/null || echo "-")"
    [ -n "$policy" ] || policy="no"
    printf '%s|%s|%s|%s|%s|%s|%s|%s\n' \
      "$id" "$name" "$image" "$status" "$ports" "${project:-<none>}" "${workdir:-}" "$policy"
  done < <(docker ps -a --format '{{.ID}}|{{.Names}}|{{.Image}}|{{.Status}}|{{.Ports}}|{{.Label "com.docker.compose.project"}}|{{.Label "com.docker.compose.project.working_dir"}}')
}

cmd_list() {
  note "All Docker containers on this host (docker ps -a):"
  printf '%-22s %-13s %-32s %-30s %-9s %-s\n' "NAME" "STATE" "IMAGE" "STATUS" "RESTART" "PROJECT"
  while IFS='|' read -r id name image status ports project workdir policy; do
    local state="running"
    [[ "$status" == Exited* || "$status" == Created* ]] && state="stopped"
    local flag=""
    if [ -n "$workdir" ] && [ ! -d "$workdir" ]; then
      flag=" [ORPHAN: $workdir missing]"
    fi
    printf '%-22s %-13s %-32.32s %-30.30s %-9s %-s%s\n' "$name" "$state" "$image" "$status" "$policy" "$project" "$flag"
  done < <(container_rows)
}

cmd_audit() {
  note "Auditing containers for orphaned/duplicate/stale patterns..."
  local found_issue=0
  while IFS='|' read -r id name image status ports project workdir policy; do
    if [ -n "$workdir" ] && [ ! -d "$workdir" ]; then
      note "ORPHAN   $name — compose project '$project' points at $workdir, which no longer exists on disk."
      found_issue=1
    fi
    if [ "$project" != "$THIS_PROJECT" ] && [ "$project" != "<none>" ]; then
      local image_no_tag="${image%%:*}"
      for own_image in "${OWN_IMAGES[@]}"; do
        if [ "$image_no_tag" = "$own_image" ]; then
          note "DUPLICATE $name (project '$project') runs $image — this repo's own stack ('$THIS_PROJECT') also runs that image. Likely a leftover/duplicate installation."
          found_issue=1
          break
        fi
      done
    fi
    if [[ "$status" == Exited* ]]; then
      local age_days
      age_days="$(docker inspect --format '{{.State.FinishedAt}}' "$id" 2>/dev/null | xargs -I{} date -d {} +%s 2>/dev/null || echo 0)"
      if [ "$age_days" -gt 0 ]; then
        local now_epoch delta_days
        now_epoch="$(date +%s)"
        delta_days=$(( (now_epoch - age_days) / 86400 ))
        if [ "$delta_days" -ge "$STALE_DAYS" ]; then
          note "STALE    $name — exited $delta_days days ago (threshold ${STALE_DAYS}d). Candidate for 'rm'."
          found_issue=1
        fi
      fi
    fi
  done < <(container_rows)
  if [ "$found_issue" -eq 0 ]; then
    note "No orphaned, duplicate or stale containers found."
  else
    note "Review the flagged containers above, then use: stop/disable/rm <name>."
  fi
}

require_name() {
  [ -n "${1:-}" ] || fail "Missing container name. Usage: $0 $ACTION <name> [...]"
}

resolve_id() {
  docker ps -a --filter "name=^${1}\$" --format '{{.ID}}' | head -n1
}

cmd_inspect() {
  require_name "${1:-}"
  docker inspect "$1" || fail "No such container: $1"
}

cmd_logs() {
  require_name "${1:-}"
  local tail="${2:-100}"
  docker logs --tail "$tail" "$1"
}

cmd_start() {
  require_name "${1:-}"
  docker start "$1" && { note "Started $1."; log_action "start $1"; }
}

cmd_stop() {
  require_name "${1:-}"
  docker stop "$1" && { note "Stopped $1."; log_action "stop $1"; }
}

cmd_restart() {
  require_name "${1:-}"
  docker restart "$1" && { note "Restarted $1."; log_action "restart $1"; }
}

cmd_disable() {
  require_name "${1:-}"
  docker update --restart=no "$1" >/dev/null || fail "Could not change restart policy for $1."
  docker stop "$1" >/dev/null 2>&1 || true
  note "Disabled $1 (stopped, restart policy set to 'no' — won't come back on reboot or daemon restart)."
  log_action "disable $1"
}

cmd_enable() {
  require_name "${1:-}"
  local policy="${2:-unless-stopped}"
  docker update --restart="$policy" "$1" >/dev/null || fail "Could not change restart policy for $1."
  docker start "$1" || fail "Could not start $1."
  note "Enabled $1 (restart policy '$policy', started)."
  log_action "enable $1 policy=$policy"
}

cmd_rm() {
  require_name "${1:-}"
  local name="$1"; shift || true
  local extra_args=()
  for arg in "$@"; do
    [ "$arg" = "--volumes" ] && extra_args+=("-v")
  done
  local status
  status="$(docker ps -a --filter "name=^${name}\$" --format '{{.Status}}')"
  if [[ "$status" == Up* ]]; then
    if [ -t 0 ]; then
      printf '[docker-manager] %s is running. Stop it and remove? [y/N]: ' "$name"
      read -r answer
      case "$answer" in y|Y|yes|YES) docker stop "$name" >/dev/null ;; *) fail "Aborted." ;; esac
    else
      fail "$name is running; stop it first (non-interactive session, refusing to force)."
    fi
  fi
  if [ -t 0 ]; then
    printf '[docker-manager] Remove container %s permanently? [y/N]: ' "$name"
    read -r answer
    case "$answer" in y|Y|yes|YES) ;; *) fail "Aborted." ;; esac
  fi
  docker rm "${extra_args[@]}" "$name" && { note "Removed $name."; log_action "rm $name ${extra_args[*]:-}"; }
}

cmd_prune() {
  note "This will remove ALL stopped containers and dangling images on this host (not just this repo's)."
  if [ -t 0 ]; then
    printf '[docker-manager] Continue? [y/N]: '
    read -r answer
    case "$answer" in y|Y|yes|YES) ;; *) fail "Aborted." ;; esac
  fi
  docker container prune -f
  docker image prune -f
  log_action "prune"
}

print_menu() {
  echo "============================================================"
  echo " PanamaCompra Docker Manager — all containers on this host"
  echo "============================================================"
  cmd_list
  echo "------------------------------------------------------------"
  echo " 1) Audit (find orphaned/duplicate/stale containers)"
  echo " 2) Start a container"
  echo " 3) Stop a container"
  echo " 4) Restart a container"
  echo " 5) Disable a container (stop + no auto-restart)"
  echo " 6) Enable a container (restart-policy + start)"
  echo " 7) Remove a container"
  echo " 8) View logs"
  echo " 9) Prune stopped containers + dangling images"
  echo " r) Refresh list"
  echo " q) Quit"
  echo "------------------------------------------------------------"
}

cmd_menu() {
  while true; do
    print_menu
    printf 'Choice: '
    read -r choice
    case "$choice" in
      1) cmd_audit ;;
      2) printf 'Container name: '; read -r n; cmd_start "$n" ;;
      3) printf 'Container name: '; read -r n; cmd_stop "$n" ;;
      4) printf 'Container name: '; read -r n; cmd_restart "$n" ;;
      5) printf 'Container name: '; read -r n; cmd_disable "$n" ;;
      6) printf 'Container name: '; read -r n; cmd_enable "$n" ;;
      7) printf 'Container name: '; read -r n; cmd_rm "$n" ;;
      8) printf 'Container name: '; read -r n; cmd_logs "$n" ;;
      9) cmd_prune ;;
      r|R) : ;;
      q|Q) note "Bye."; exit 0 ;;
      *) note "Unknown choice: $choice" ;;
    esac
    printf '\nPress Enter to continue...'
    read -r _
  done
}

ACTION="${1:-list}"
shift || true

case "$ACTION" in
  list) cmd_list ;;
  audit) cmd_audit ;;
  inspect) cmd_inspect "$@" ;;
  logs) cmd_logs "$@" ;;
  start) cmd_start "$@" ;;
  stop) cmd_stop "$@" ;;
  restart) cmd_restart "$@" ;;
  disable) cmd_disable "$@" ;;
  enable) cmd_enable "$@" ;;
  rm|remove|delete) cmd_rm "$@" ;;
  prune) cmd_prune ;;
  menu) cmd_menu ;;
  *) fail "Unknown action: $ACTION (use list|audit|inspect|logs|start|stop|restart|disable|enable|rm|prune|menu)" ;;
esac
