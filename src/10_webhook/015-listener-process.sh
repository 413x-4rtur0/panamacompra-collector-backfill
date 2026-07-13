#!/usr/bin/env bash
# Shared host-process helpers for the PanamaCompra webhook listener.
#
# Docker container PIDs are visible from the host PID namespace, so pgrep alone
# cannot distinguish the compose webhook from the host-native listener. Compare
# network namespaces before reporting or stopping a host listener.

pc_webhook_host_pids() {
  local host_netns pid pid_netns
  host_netns="$(readlink "/proc/$$/ns/net" 2>/dev/null || true)"
  [ -n "$host_netns" ] || return 0

  while read -r pid; do
    [ -n "$pid" ] || continue
    pid_netns="$(readlink "/proc/$pid/ns/net" 2>/dev/null || true)"
    if [ -n "$pid_netns" ] && [ "$pid_netns" = "$host_netns" ]; then
      printf '%s\n' "$pid"
    fi
  done < <(pgrep -f "[s]rc/10_webhook/010-webhook-listener.py" 2>/dev/null || true)
}

pc_webhook_host_running() {
  [ -n "$(pc_webhook_host_pids)" ]
}

pc_webhook_print_host_processes() {
  local pid found=0
  while read -r pid; do
    [ -n "$pid" ] || continue
    found=1
    ps -p "$pid" -o pid=,ppid=,comm=,args= 2>/dev/null || true
  done < <(pc_webhook_host_pids)
  [ "$found" -eq 1 ]
}

pc_webhook_kill_host_processes() {
  local signal="${1:-TERM}"
  local pid
  while read -r pid; do
    [ -n "$pid" ] || continue
    kill "-$signal" "$pid" 2>/dev/null || true
  done < <(pc_webhook_host_pids)
}
