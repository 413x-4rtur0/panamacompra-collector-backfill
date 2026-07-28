#!/usr/bin/env bash
# One-click "I'm editing this repo" toggle: pauses automatic collection so an
# in-progress code edit can never race a live run, an autostash, or a mid-edit
# WhatsApp send -- without tearing down the monitors, webhook listener or
# Docker integrations (changedetection/WAHA stay up; see 120a/120c for those).
#
# on:     stops any active collection run, disables the webhook auto-run
#         trigger and the cron schedule, and turns on update-local-copy.sh's
#         safe mode (PC_UPDATE_REQUIRE_CLEAN=1) so it refuses to autostash
#         uncommitted edits instead of doing it silently.
# off:    restores every setting dev mode changed, to its exact previous
#         value/schedule. Does NOT queue a new run by itself.
# status: prints whether dev mode is active and the current toggle values.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
# shellcheck source=../../lib/env.sh
source "$SCRIPT_DIR/../../lib/env.sh"
cd "$APP_ROOT"

ACTION="${1:-status}"
MONITOR_SETTINGS="$PC_DATA_DIR/config/monitor_settings.env"
BACKUP_FILE="$PC_DATA_DIR/config/dev_mode_backup.env"
MARKER_FLAG="$PC_QUEUE_DIR/dev_mode_active.flag"
CRON_MARKER="# PANAMACOMPRA-CRON-SCHEDULE"

read_setting() {
  local key="$1" default="$2" value=""
  if [ -f "$MONITOR_SETTINGS" ]; then
    value="$(sed -n "s/^${key}=[\"']*\([^\"']*\)[\"']*\$/\1/p" "$MONITOR_SETTINGS" | tail -n1)"
  fi
  printf '%s' "${value:-$default}"
}

write_setting() {
  local key="$1" value="$2"
  mkdir -p "$(dirname "$MONITOR_SETTINGS")"
  touch "$MONITOR_SETTINGS"
  if grep -q "^${key}=" "$MONITOR_SETTINGS" 2>/dev/null; then
    sed -i "s|^${key}=.*|${key}='${value}'|" "$MONITOR_SETTINGS"
  else
    echo "${key}='${value}'" >> "$MONITOR_SETTINGS"
  fi
}

dev_mode_on() {
  if [ -f "$MARKER_FLAG" ]; then
    echo "Dev mode is already ON (since $(cat "$MARKER_FLAG" 2>/dev/null))."
    return 0
  fi

  echo "Entering DEV MODE: pausing automatic collection so editing this repo is safe."
  echo "Docker integrations (changedetection/WAHA) and the monitors/webhook listener stay running."
  echo ""

  mkdir -p "$(dirname "$BACKUP_FILE")"
  local cron_lines
  cron_lines="$(crontab -l 2>/dev/null | grep -F "$CRON_MARKER" || true)"
  {
    echo "PREV_WEBHOOK_AUTO_RUN='$(read_setting PC_WEBHOOK_AUTO_RUN 1)'"
    echo "PREV_UPDATE_REQUIRE_CLEAN='$(read_setting PC_UPDATE_REQUIRE_CLEAN 0)'"
    echo "PREV_CRON_LINES_B64='$(printf '%s' "$cron_lines" | base64 -w0 2>/dev/null || printf '%s' "$cron_lines" | base64)'"
  } > "$BACKUP_FILE"

  echo "1) Stopping any active collection run (worker/index/detail/test/calendar)..."
  "$APP_ROOT/src/20_pipeline/120b-stop-collectors.sh" || true

  echo ""
  echo "2) Pausing automatic triggers: webhook auto-run OFF, cron schedule removed..."
  write_setting PC_WEBHOOK_AUTO_RUN 0
  if [ -n "$cron_lines" ]; then
    python3 "$APP_ROOT/src/50_tools/160-manage-cron-schedule.py" remove || true
  fi

  echo ""
  echo "3) Enabling update-local-copy.sh safe mode (refuses to autostash uncommitted edits)..."
  write_setting PC_UPDATE_REQUIRE_CLEAN 1

  date '+%Y-%m-%d %H:%M:%S' > "$MARKER_FLAG"

  echo ""
  echo "============================================================"
  echo "DEV MODE ON. Run '$0 off' when you're done to restore normal operation."
  echo "============================================================"
}

dev_mode_off() {
  if [ ! -f "$MARKER_FLAG" ]; then
    echo "Dev mode is not active."
    return 0
  fi
  echo "Exiting DEV MODE: restoring automatic collection settings..."

  local prev_webhook="1" prev_clean="0" prev_cron_b64=""
  if [ -f "$BACKUP_FILE" ]; then
    prev_webhook="$(sed -n "s/^PREV_WEBHOOK_AUTO_RUN='\(.*\)'\$/\1/p" "$BACKUP_FILE" | tail -n1)"
    prev_clean="$(sed -n "s/^PREV_UPDATE_REQUIRE_CLEAN='\(.*\)'\$/\1/p" "$BACKUP_FILE" | tail -n1)"
    prev_cron_b64="$(sed -n "s/^PREV_CRON_LINES_B64='\(.*\)'\$/\1/p" "$BACKUP_FILE" | tail -n1)"
  fi

  write_setting PC_WEBHOOK_AUTO_RUN "${prev_webhook:-1}"
  write_setting PC_UPDATE_REQUIRE_CLEAN "${prev_clean:-0}"

  if [ -n "$prev_cron_b64" ]; then
    local restored
    restored="$(printf '%s' "$prev_cron_b64" | base64 -d 2>/dev/null || true)"
    if [ -n "$restored" ]; then
      { crontab -l 2>/dev/null | grep -vF "$CRON_MARKER" || true; printf '%s\n' "$restored"; } | crontab -
      echo "   Restored previous cron schedule."
    fi
  fi

  rm -f "$MARKER_FLAG" "$BACKUP_FILE"

  echo ""
  echo "============================================================"
  echo "DEV MODE OFF. Automatic triggers restored to their previous settings."
  echo "This does not queue a run by itself -- request one from the monitor if"
  echo "you need fresh data now."
  echo "============================================================"
}

dev_mode_status() {
  if [ -f "$MARKER_FLAG" ]; then
    echo "DEV MODE: ON (since $(cat "$MARKER_FLAG" 2>/dev/null))"
  else
    echo "DEV MODE: OFF"
  fi
  echo "PC_WEBHOOK_AUTO_RUN: $(read_setting PC_WEBHOOK_AUTO_RUN 1)"
  echo "PC_UPDATE_REQUIRE_CLEAN: $(read_setting PC_UPDATE_REQUIRE_CLEAN 0)"
  echo "Cron schedule: $(python3 "$APP_ROOT/src/50_tools/160-manage-cron-schedule.py" show 2>&1)"
}

case "$ACTION" in
  on|pause) dev_mode_on ;;
  off|resume) dev_mode_off ;;
  status) dev_mode_status ;;
  *) echo "Usage: $0 [on|pause|off|resume|status]" >&2; exit 2 ;;
esac
