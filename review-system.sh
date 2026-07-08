#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
# shellcheck source=lib/env.sh
source "$SCRIPT_DIR/lib/env.sh"
cd "$APP_ROOT"

HEALTH_NOTIFY_PURPOSE="${PC_SYSTEM_HEALTH_NOTIFY_PURPOSE:-system}"
HEALTH_NOTIFY_CHAT_ID="${PC_SYSTEM_HEALTH_CHAT_ID:-}"
# Set by the data-freshness section; initialized here so the EXIT trap can
# reference it under `set -u` even when the script fails earlier.
FRESHNESS_NOTE=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --chat-id) HEALTH_NOTIFY_CHAT_ID="${2:-}"; shift 2 ;;
    --purpose) HEALTH_NOTIFY_PURPOSE="${2:-system}"; shift 2 ;;
    -h|--help)
      cat <<'USAGE'
Usage: review-system.sh [--chat-id WAHA_CHAT_ID] [--purpose default|index|details|status|system|summary]

Runs repository/system health checks. When WAHA is enabled, completion sends a
"System health" message to the selected purpose destination (default:
system). --chat-id overrides that destination for this run.
USAGE
      exit 0
      ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done
case "$HEALTH_NOTIFY_PURPOSE" in default|index|details|status|system|summary) ;; *) HEALTH_NOTIFY_PURPOSE="system" ;; esac

SETTINGS_FILE="$PC_DATA_DIR/config/monitor_settings.env"
load_monitor_settings_for_health() {
  if [ -f "$SETTINGS_FILE" ]; then
    set -a
    # shellcheck disable=SC1090
    source "$SETTINGS_FILE"
    set +a
  fi
}

send_health_notification() {
  local exit_status="$1" status_label="OK"
  [ "$exit_status" -eq 0 ] || status_label="FAILED"
  load_monitor_settings_for_health
  if [ -n "$HEALTH_NOTIFY_CHAT_ID" ]; then
    case "$HEALTH_NOTIFY_PURPOSE" in
      index) export PC_WAHA_CHAT_ID_INDEX="$HEALTH_NOTIFY_CHAT_ID" ;;
      details) export PC_WAHA_CHAT_ID_DETAILS="$HEALTH_NOTIFY_CHAT_ID" ;;
      status) export PC_WAHA_CHAT_ID_STATUS="$HEALTH_NOTIFY_CHAT_ID" ;;
      system) export PC_WAHA_CHAT_ID_SYSTEM="$HEALTH_NOTIFY_CHAT_ID" ;;
      summary) export PC_WAHA_CHAT_ID_SUMMARY="$HEALTH_NOTIFY_CHAT_ID" ;;
      default) export PC_WAHA_CHAT_ID="$HEALTH_NOTIFY_CHAT_ID" ;;
    esac
  fi
  local purpose_args=()
  [ "$HEALTH_NOTIFY_PURPOSE" = "default" ] || purpose_args=(--purpose "$HEALTH_NOTIFY_PURPOSE")
  "$APP_ROOT/src/notify/010-waha-client.py" \
    --event done \
    --status "SYSTEM HEALTH $status_label" \
    "${purpose_args[@]}" \
    --message "System health review finished with status: $status_label.${FRESHNESS_NOTE:+ $FRESHNESS_NOTE} Check data/logs/manual_actions.log or the terminal output for details." \
    >/dev/null 2>&1 || true
}
trap 'status=$?; send_health_notification "$status"; exit "$status"' EXIT

echo "============================================================"
echo " PanamaCompra System Review"
echo "============================================================"
echo "Started: $(date '+%Y-%m-%d %H:%M:%S')"
echo "Project: $(pwd)"
echo ""

echo "1) Active architecture"
echo "----------------------"
cat <<'TXT'
changedetection.io
  -> src/webhook/010-webhook-listener.py
  -> src/webhook/060-run-collector.sh (or 050-watch-queue-flag.sh for the docker listener)
  -> src/pipeline/110a-request-run.sh
  -> src/pipeline/100-run-worker.sh
       STEP 0: src/pipeline/000-update-before-run.sh
       STEP 1: src/pipeline/015-import-index-snapshot.py (AUTO) / 010-collect-index.py
       STEP 2: src/pipeline/020-notify-whatsapp.py --announce (WhatsApp index alerts)
       STEP 3: src/pipeline/030-collect-details.py (+ inline detail messages)
       STEP 4: src/pipeline/040-build-detail-views.py (+ work templates)
       STEP 5: src/pipeline/020-notify-whatsapp.py --announce-details
       STEP 6: py_compile + src/pipeline/050-repair-missing-deadlines.py (verify/repair)
       STEP 7: src/pipeline/060-build-calendar.py -> data/calendar/YY-MM-DD/*.ics
       STEP 8: src/pipeline/070-test-zone.py (opt-in, idle/no-new-records only)
See docs/ARCHITECTURE.md for the full verified flow.
TXT

echo ""
echo "2) Required active scripts"
echo "--------------------------"
required_scripts=(
  "src/webhook/010-webhook-listener.py"
  "src/webhook/060-run-collector.sh"
  "src/pipeline/110a-request-run.sh"
  "src/pipeline/100-run-worker.sh"
  "src/pipeline/000-update-before-run.sh"
  "src/notify/010-waha-client.py"
  "src/pipeline/020-notify-whatsapp.py"
  "src/pipeline/010-collect-index.py"
  "src/pipeline/030-collect-details.py"
  "src/pipeline/040-build-detail-views.py"
  "src/pipeline/050-repair-missing-deadlines.py"
  "src/pipeline/060-build-calendar.py"
  "src/pipeline/070-test-zone.py"
  "src/common.py"
  "src/monitor/001c-monitor-terminal.sh"
  "src/monitor/001a-monitor-tk.py"
  "src/monitor/001b-monitor-web.py"
  "src/monitor/000-open-monitor.sh"
  "src/monitor/002-next-run-timer.py"
  "src/monitor/003-update-loader.py"
  "src/pipeline/130b-run-status.sh"
  "src/pipeline/130a-queue-status.sh"
  "src/pipeline/120a-stop-everything.sh"
  "src/pipeline/120b-stop-collectors.sh"
  "src/pipeline/130c-follow-run.sh"
  "src/pipeline/110b-run-now.sh"
  "src/webhook/020-start-listener.sh"
  "src/webhook/030-install-service.sh"
  "src/webhook/050-watch-queue-flag.sh"
  "src/webhook/040-diagnose-webhook.sh"
  "src/tools/070-rename-record-folders.py"
  "src/tools/050-maintain-database.py"
  "src/tools/080-update-day-folder.py"
  "src/tools/110-reset.py"
  "src/tools/060-import-selected-calendars.py"
  "src/tools/100-migrate-apps-layout.sh"
  "src/tools/090b-migrate-previous-records.py"
  "src/tools/090a-migrate-previous-records.sh"
  "src/tools/120-setup-git-credentials.sh"
  "src/tools/140-full-report.py"
  "src/tools/150-upload-github.sh"
  "setup.sh"
  "update-local-copy.sh"
)

for f in "${required_scripts[@]}"; do
  if [ -f "$f" ]; then
    echo "OK:      $f"
  else
    echo "MISSING: $f"
  fi
done

echo ""
echo "3) Deprecated scripts (should not be present)"
echo "---------------------------------------------"
deprecated_scripts=(
  "pc_enqueue.sh"
  "pc_queue_worker.sh"
  "pc_requeue_running.sh"
  "pc_run_index.sh"
  "pc_run_detail.sh"
  "pc_run_sequence.sh"
  "pc_common.py"
  "pc_index_collector.py"
  "pc_detail_downloader.py"
  "pc_run_all_worker.sh"
  "pc_request_run_all.sh"
  "webhook_listener.py"
  "run_collector.sh"
  "update_local_copy.sh"
  "review_panamacompra_system.sh"
)

found_deprecated=0
for f in "${deprecated_scripts[@]}"; do
  if [ -f "$f" ]; then
    echo "PRESENT: $f"
    found_deprecated=$((found_deprecated + 1))
  fi
done
[ "$found_deprecated" -eq 0 ] && echo "None present. Good."

echo ""
echo "4) Python compile check"
echo "-----------------------"
PYTHON_BIN="${PYTHON_BIN:-python3}"
if [ -f ".venv/bin/activate" ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
  PYTHON_BIN="python"
fi

python_files=()
while IFS= read -r -d '' f; do
  python_files+=("$f")
done < <(find src -name '*.py' -print0)

for f in "${python_files[@]}"; do
  if "$PYTHON_BIN" -m py_compile "$f" 2>/dev/null; then
    echo "COMPILE OK: $f"
  else
    echo "COMPILE FAIL: $f"
  fi
done

echo ""
echo "5) Shell syntax check"
echo "---------------------"
shell_files=()
while IFS= read -r -d '' f; do
  shell_files+=("$f")
done < <(find . -maxdepth 4 -name '*.sh' -not -path './.venv/*' -not -path './.git/*' -print0)

for f in "${shell_files[@]}"; do
  if bash -n "$f" 2>/dev/null; then
    echo "SYNTAX OK: $f"
  else
    echo "SYNTAX FAIL: $f"
  fi
done

echo ""
echo "6) Current related processes"
echo "----------------------------"
pgrep -af "100-run-worker.sh|010-collect-index.py|030-collect-details.py|001c-monitor-terminal.sh|001a-monitor-tk.py|001b-monitor-web.py|timeout .*src/pipeline" || echo "No related active process."

echo ""
echo "7) Current run-all status"
echo "-------------------------"
if [ -x "./src/pipeline/130b-run-status.sh" ]; then
  ./src/pipeline/130b-run-status.sh
else
  echo "src/pipeline/130b-run-status.sh missing."
fi

echo ""
echo "8) Data freshness"
echo "-----------------"
# Warn when the last SUCCESSFUL run is older than PC_FRESHNESS_MAX_HOURS
# (default 24). Kept out of the exit status so a quiet weekend does not turn
# every health message into FAILED; the note still reaches the WAHA message.
FRESHNESS_NOTE=""
FRESHNESS_MAX_HOURS="${PC_FRESHNESS_MAX_HOURS:-24}"
LAST_SUMMARY_FILE="$PC_LOG_DIR/run_all_last_summary.env"
if [ -f "$LAST_SUMMARY_FILE" ]; then
  # shellcheck disable=SC1090
  FINISHED_AT="$(. "$LAST_SUMMARY_FILE" 2>/dev/null; printf '%s' "${FINISHED_AT:-}")"
  finished_epoch="$(date -d "$FINISHED_AT" '+%s' 2>/dev/null || echo "")"
  if [ -n "$finished_epoch" ]; then
    age_hours=$(( ( $(date '+%s') - finished_epoch ) / 3600 ))
    if [ "$age_hours" -gt "$FRESHNESS_MAX_HOURS" ]; then
      FRESHNESS_NOTE="WARNING: last successful run finished ${age_hours}h ago ($FINISHED_AT), older than ${FRESHNESS_MAX_HOURS}h — check changedetection, the webhook listener and the worker."
      echo "$FRESHNESS_NOTE"
    else
      echo "OK: last successful run finished ${age_hours}h ago ($FINISHED_AT; threshold ${FRESHNESS_MAX_HOURS}h)."
    fi
  else
    FRESHNESS_NOTE="WARNING: could not parse FINISHED_AT from $LAST_SUMMARY_FILE."
    echo "$FRESHNESS_NOTE"
  fi
else
  FRESHNESS_NOTE="WARNING: no completed run recorded yet ($LAST_SUMMARY_FILE missing)."
  echo "$FRESHNESS_NOTE"
fi

echo ""
echo "9) Recommended commands"
echo "-----------------------"
cat <<'TXT'
Manual small test:   ./src/pipeline/110a-request-run.sh 5
Run all pending:     ./src/pipeline/110a-request-run.sh
Open native monitor: ./src/monitor/000-open-monitor.sh
Open web monitor:    PC_MONITOR_MODE=web ./src/monitor/000-open-monitor.sh
Watch in terminal:   PC_MONITOR_MODE=terminal ./src/monitor/000-open-monitor.sh
Follow logs:         ./src/pipeline/130c-follow-run.sh
Check status:        ./src/pipeline/130b-run-status.sh
Stop if stuck:       ./src/pipeline/120a-stop-everything.sh
Or use the unified CLI: ./bin/pcc <start|stop|status|monitor|webhook|...>
TXT

echo ""
echo "Finished: $(date '+%Y-%m-%d %H:%M:%S')"
echo "============================================================"
