#!/usr/bin/env bash
set -euo pipefail

# Update a local PanamaCompra Collector checkout in place.
# Intended use from the target host:
#   cd ~/Apps/panamacompra-collector
#   ./update-local-copy.sh

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
# shellcheck source=lib/env.sh
source "$SCRIPT_DIR/lib/env.sh"
BASE_DIR="$APP_ROOT"
cd "$APP_ROOT"

REMOTE="${PC_UPDATE_REMOTE:-origin}"
# PC_UPDATE_BRANCH is now an OPTIONAL hard override. When empty (default), the
# updater auto-selects the branch: it tracks main if the most recently updated
# remote branch is already merged into main, otherwise it switches to that
# latest branch. Set PC_UPDATE_BRANCH to pin an exact branch instead.
BRANCH="${PC_UPDATE_BRANCH:-}"
DETAIL_LIMIT="${PC_UPDATE_TEST_DETAIL_LIMIT:-0}"
CHECKED_OUT_BRANCH=""
WEBHOOK_WAS_RUNNING=0
WEBHOOK_RESTORED=0
UPDATE_QUEUE_FLAG="$PC_QUEUE_DIR/update_monitor_requested.flag"
UPDATE_IN_PROGRESS_FLAG="$PC_QUEUE_DIR/update_monitor_in_progress.flag"
UPDATE_LOCK_FILE="/tmp/panamacompra_update_local.lock"

collector_pipeline_running() {
  pgrep -f "[r]un-worker.sh|[p]ython3? -u .*010-collect-index.py|[p]ython3? -u .*030-collect-details.py|[p]ython3? -u .*060-build-calendar.py|[p]ython3? -u .*020-notify-whatsapp.py" >/dev/null 2>&1
}

open_monitor_best_effort() {
  if [ "${PC_UPDATE_OPEN_MONITOR_WHEN_QUEUED:-1}" != "0" ] && [ -x ./src/monitor/000-open-monitor.sh ]; then
    ./src/monitor/000-open-monitor.sh >/dev/null 2>&1 || true
  fi
}

queue_update_monitor_request() {
  local reason="$1"
  {
    echo "$(date '+%Y-%m-%d %H:%M:%S') | UPDATE+MONITOR QUEUED: $reason"
  } | tee -a "$PC_LOG_DIR/update_monitor_queue.log"
  printf "REQUESTED_AT='%s'\nREASON='%s'\n" "$(date '+%Y-%m-%d %H:%M:%S')" "$reason" > "$UPDATE_QUEUE_FLAG"
  open_monitor_best_effort
  echo "Update + Monitor request queued. It will run after the active collector/update finishes."
}

webhook_listener_running() {
  pgrep -f "[s]rc/webhook/010-webhook-listener.py" >/dev/null 2>&1
}

restart_webhook_listener() {
  if [ "$WEBHOOK_RESTORED" = "1" ]; then
    return 0
  fi
  WEBHOOK_RESTORED=1

  if [ "${PC_UPDATE_RESTART_WEBHOOK:-auto}" = "0" ]; then
    echo "Skipped webhook listener restart because PC_UPDATE_RESTART_WEBHOOK=0."
    return 0
  fi

  local should_restart=0
  if [ "${PC_UPDATE_RESTART_WEBHOOK:-auto}" = "1" ]; then
    should_restart=1
  elif [ "${PC_UPDATE_RESTART_WEBHOOK:-auto}" = "auto" ]; then
    # The monitor depends on the webhook being available after Update + Monitor.
    # In auto mode, restore/start it even if it was already off before update;
    # PC_UPDATE_RESTART_WEBHOOK=0 remains the explicit opt-out.
    should_restart=1
  elif [ "$WEBHOOK_WAS_RUNNING" = "1" ]; then
    should_restart=1
  elif systemctl --user is-enabled panamacompra-webhook.service >/dev/null 2>&1; then
    should_restart=1
  fi

  if [ "$should_restart" != "1" ]; then
    echo "Webhook listener restart not requested."
    return 0
  fi

  if systemctl --user is-enabled panamacompra-webhook.service >/dev/null 2>&1; then
    echo "Restarting user systemd service: panamacompra-webhook.service"
    if systemctl --user restart panamacompra-webhook.service; then
      systemctl --user --no-pager --lines=0 status panamacompra-webhook.service || true
      return 0
    fi
    echo "WARNING: systemd restart failed; falling back to nohup listener start."
  fi

  if webhook_listener_running; then
    echo "Webhook listener is already running."
    return 0
  fi

  if ./src/webhook/020-start-listener.sh --replace-port-owner; then
    echo "Webhook listener is available after update."
  else
    echo "WARNING: webhook listener did not start; check $PC_LOG_DIR/webhook_listener.out.log."
  fi
}

restore_webhook_on_exit() {
  local code=$?
  cleanup_update_flags || true
  if [ "$code" -ne 0 ]; then
    echo ""
    echo "Update exited with status $code; restoring webhook listener before exit if it was active."
    restart_webhook_listener || true
  fi
}

install_desktop_shortcut() {
  if [ "${PC_UPDATE_INSTALL_MONITOR_SHORTCUT:-1}" = "0" ]; then
    echo "Skipped desktop shortcut install because PC_UPDATE_INSTALL_MONITOR_SHORTCUT=0."
    return 0
  fi

  if [ -x "$BASE_DIR/scripts/install-desktop-launcher.sh" ]; then
    "$BASE_DIR/scripts/install-desktop-launcher.sh" install
  else
    echo "WARNING: desktop launcher creator missing: $BASE_DIR/scripts/install-desktop-launcher.sh"
  fi
}

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "ERROR: $BASE_DIR is not a git checkout." >&2
  exit 1
fi

# Serialize manual/automatic Update + Monitor launchers. If changedetection (or
# a user) asks for another update while the collector is still processing a
# previous change, do NOT kill the active pipeline and do NOT start a second
# updater. Leave a durable queue flag; src/pipeline/100-run-worker.sh consumes it
# after the current run finishes cleanly.
exec 8>"$UPDATE_LOCK_FILE"
if ! flock -n 8; then
  queue_update_monitor_request "another update-local-copy.sh is already running"
  exit 0
fi

touch "$UPDATE_IN_PROGRESS_FLAG"
cleanup_update_flags() {
  rm -f "$UPDATE_IN_PROGRESS_FLAG"
}
trap cleanup_update_flags EXIT

if collector_pipeline_running; then
  queue_update_monitor_request "collector pipeline is still running"
  exit 0
fi
rm -f "$UPDATE_QUEUE_FLAG"
LOG_FILE="$PC_LOG_DIR/update_local_copy_$(date +%Y%m%d_%H%M%S).log"
exec > >(tee -a "$LOG_FILE") 2>&1

echo "============================================================"
echo " PanamaCompra Collector local update"
echo "============================================================"
echo "Started: $(date '+%Y-%m-%d %H:%M:%S')"
echo "Directory: $BASE_DIR"
echo "Remote: $REMOTE"
echo "Branch: ${BRANCH:-<auto-detect latest vs main>}"
echo "Log: $LOG_FILE"
echo ""

echo "1) Verify no collector pipeline is active before updating"
if collector_pipeline_running; then
  queue_update_monitor_request "collector pipeline started before updater step 1"
  exit 0
fi
if webhook_listener_running; then
  WEBHOOK_WAS_RUNNING=1
  echo "Webhook listener is currently running; it will be restarted after the update."
fi
trap restore_webhook_on_exit EXIT
# The updater used to stop any active run here. That could cut a
# changedetection-triggered collector in the middle of index/detail/calendar or
# WhatsApp processing. Active collectors are now detected before this point and
# converted into a queued Update + Monitor request instead. We only pause the
# webhook listener during the actual update window so a fresh notification is
# enqueued for after the update instead of racing code/dependency changes.
pkill -TERM -f "[s]rc/webhook/010-webhook-listener.py" 2>/dev/null || true

echo ""
echo "2) Preserve any local changes to tracked files so the update always proceeds"
# Untracked files (data/, records/, .venv.broken.*, .webhook_token, ...) never
# block an update. Local edits to TRACKED files are auto-stashed instead of
# aborting, so this checkout can always be brought up to date. The stash is
# kept (not dropped) so nothing is lost; recover it later with `git stash list`.
STASH_REF=""
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  STASH_MESSAGE="update_local_copy autostash $(date '+%Y-%m-%d %H:%M:%S')"
  echo "Local changes to tracked files detected. Auto-stashing them before updating:"
  git status --short --untracked-files=no
  if git stash push -m "$STASH_MESSAGE" >/dev/null 2>&1; then
    STASH_REF="$(git rev-parse -q --verify stash@{0} 2>/dev/null || true)"
    echo "Stashed as: $STASH_MESSAGE"
    echo "Recover later with: git stash list  /  git stash apply stash@{0}"
  else
    echo "WARNING: Could not stash local changes; continuing with a hard reset to the remote branch."
  fi
fi

echo ""
echo "3) Fetch all remotes and select the branch to update to"
git fetch --all --prune

update_main() {
  git checkout main 2>/dev/null || git checkout -B main "$REMOTE/main"
  if git pull --ff-only "$REMOTE" main; then
    echo "Fast-forwarded main to $REMOTE/main."
  else
    echo "Fast-forward of main not possible (diverged). Resetting main to $REMOTE/main."
    echo "Any diverging local commits remain reachable via the reflog (git reflog main)."
    git reset --hard "$REMOTE/main"
  fi
  CHECKED_OUT_BRANCH="main"
}

switch_to_branch() {
  local target="$1"
  git checkout "$target" 2>/dev/null || git checkout -B "$target" "$REMOTE/$target"
  git reset --hard "$REMOTE/$target"
  # Drop stray untracked files left by the previous branch, but NEVER the runtime
  # archive/db/venv. `git clean` already respects .gitignore (so data/, records/,
  # .venv, .webhook_token are kept); the explicit excludes below are a safety net
  # in case .gitignore is ever stale on the host. We deliberately do NOT pass -x.
  git clean -fd \
    -e data -e records -e records_test \
    -e .venv -e ".venv.broken.*" -e .webhook_token || true
  CHECKED_OUT_BRANCH="$target"
}

if [ -n "$BRANCH" ]; then
  # Hard override: pin the exact branch requested via PC_UPDATE_BRANCH.
  echo "PC_UPDATE_BRANCH override active; tracking $REMOTE/$BRANCH."
  switch_to_branch "$BRANCH"
else
  # Auto mode. Pick the most recently updated remote branch (ignoring HEAD).
  # `|| true` keeps `set -o pipefail` from aborting when grep filters everything
  # (e.g. a remote that only has origin/HEAD).
  LATEST_BRANCH="$(git for-each-ref --sort=-committerdate \
      --format='%(refname:short)' "refs/remotes/$REMOTE" \
    | grep -v "^$REMOTE/HEAD$" \
    | sed "s|^$REMOTE/||" \
    | head -n1 || true)"
  echo "Latest remote branch: ${LATEST_BRANCH:-<none detected>}"

  HAS_MAIN=0
  git rev-parse --verify --quiet "$REMOTE/main" >/dev/null 2>&1 && HAS_MAIN=1

  if [ "$HAS_MAIN" -eq 0 ]; then
    # No main to compare against: track the latest branch directly.
    echo "No $REMOTE/main found; tracking the latest branch directly."
    switch_to_branch "${LATEST_BRANCH:?No remote branches found to update to}"
  elif [ -z "$LATEST_BRANCH" ] || [ "$LATEST_BRANCH" = "main" ]; then
    echo "Latest remote branch is main — staying on main."
    update_main
  elif git merge-base --is-ancestor "$REMOTE/$LATEST_BRANCH" "$REMOTE/main"; then
    # The latest branch is already merged into main (its tip is an ancestor of
    # main), so the newest code lives on main: track main.
    echo "Latest branch '$LATEST_BRANCH' is already merged into main — staying on main."
    update_main
  else
    # The latest branch is NOT merged into main yet, so it holds the newest code:
    # switch to it.
    echo "Latest branch '$LATEST_BRANCH' is not merged into main — switching to it."
    switch_to_branch "$LATEST_BRANCH"
  fi
fi

echo "Now on branch: $CHECKED_OUT_BRANCH"

if [ -n "$STASH_REF" ]; then
  echo "Your previous local edits are preserved in the stash ($STASH_REF). They were"
  echo "NOT reapplied automatically to avoid conflicts during unattended updates."
fi

echo ""
echo "4) Ensure executable bits are set"
find . -maxdepth 4 \( -name "*.sh" -o -name "*.py" \) -not -path "./.venv/*" -exec chmod +x {} +
chmod +x ./bin/pcc

echo ""
echo "5) Ensure Python virtual environment and dependencies"
venv_is_healthy() {
  [ -x .venv/bin/python ] || return 1
  .venv/bin/python -c "import ensurepip; import subprocess" >/dev/null 2>&1
}

playwright_firefox_available() {
  python - <<'PY' >/dev/null 2>&1
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.firefox.launch(headless=True)
    browser.close()
PY
}

create_venv() {
  if ! python3 -m venv .venv; then
    echo "ERROR: Could not create .venv with python3 -m venv." >&2
    echo "Install the system Python venv/full packages, then retry:" >&2
    echo "  sudo apt update && sudo apt install -y python3-venv python3-full" >&2
    exit 1
  fi
}

if [ -d .venv ] && ! venv_is_healthy; then
  BROKEN_VENV=".venv.broken.$(date +%Y%m%d_%H%M%S)"
  echo "Existing .venv is broken or incomplete (for example missing _posixsubprocess)."
  echo "Moving it to $BROKEN_VENV and recreating a clean virtual environment."
  mv .venv "$BROKEN_VENV"
fi

if [ ! -d .venv ]; then
  create_venv
fi

if ! venv_is_healthy; then
  echo "ERROR: .venv was created but Python still cannot import required stdlib modules." >&2
  echo "Install/reinstall python3-venv and python3-full, remove .venv, then retry." >&2
  exit 1
fi

# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip || { echo "ERROR: Unable to upgrade pip. Check network/proxy access or retry with a reachable Python package index." >&2; exit 1; }
python -m pip install -r requirements.txt || { echo "ERROR: Unable to install Python dependencies from requirements.txt. Check network/proxy access, then rerun ./update-local-copy.sh." >&2; exit 1; }

echo ""
echo "6) Verify Playwright Firefox browser"
if playwright_firefox_available; then
  echo "Playwright Firefox is already installed and launchable."
elif [ "${PC_UPDATE_SKIP_BROWSER_INSTALL:-0}" = "1" ]; then
  echo "Skipped Playwright Firefox install because PC_UPDATE_SKIP_BROWSER_INSTALL=1."
  echo "WARNING: Playwright Firefox is not currently launchable."
else
  if [ -r /etc/os-release ] && grep -qiE 'debian|ubuntu|linuxmint' /etc/os-release; then
    python -m playwright install --with-deps firefox || {
      echo "ERROR: Unable to install Playwright Firefox and OS dependencies." >&2
      echo "Retry after fixing apt/network access, or set PC_UPDATE_SKIP_BROWSER_INSTALL=1 to skip temporarily." >&2
      exit 1
    }
  else
    python -m playwright install firefox || {
      echo "ERROR: Unable to install Playwright Firefox." >&2
      echo "If launch later reports missing libraries, run: python -m playwright install-deps firefox" >&2
      exit 1
    }
  fi
  if ! playwright_firefox_available; then
    echo "ERROR: Playwright Firefox installed but could not be launched." >&2
    echo "Install missing system browser dependencies, then retry:" >&2
    echo "  python -m playwright install --with-deps firefox" >&2
    exit 1
  fi
fi

echo ""
echo "7) Review and update archive database metadata"
python -u ./src/tools/050-maintain-database.py --apply

echo ""
echo "8) Run repository health checks"
./review-system.sh

echo ""
echo "9) Refresh already-downloaded records (optional, manual)"
echo "   A normal run only processes NEW records; it never re-pulls previously"
echo "   downloaded ones. To bring existing records up to the current parsing/ICS"
echo "   and the per-section split-table layout, run one of these manually:"
echo "     ./src/pipeline/040-build-detail-views.py --apply         # rebuild views/.ics + split tables (no browser)"
echo "     ./src/tools/080-update-day-folder.py --date <YY-MM-DD> --apply # re-download a day from the portal"
echo "   Then rebuild calendar import packages if needed:"
echo "     ./src/pipeline/060-build-calendar.py --all              # data/calendar/YY-MM-DD packages, default 10 events each"
echo "     PC_CALENDAR_PACKAGE_SIZE=5 ./src/pipeline/060-build-calendar.py --all"
echo "     ./src/pipeline/060-build-calendar.py --all --flat       # optional old parent-only package location"
echo "   To verify the current code when there are no new opportunities, run the"
echo "   testing zone (records_test/latest_5 + records_test/calendar/YY-MM-DD; monitor MODE=TEST/test_run):"
echo "     ./src/pipeline/070-test-zone.py --limit 5 --apply"

echo ""
echo "10) Install manual monitor desktop shortcut"
install_desktop_shortcut

echo ""
echo "11) Optional smoke run request"
if [ "$DETAIL_LIMIT" != "0" ]; then
  echo "Requesting smoke run with detail limit: $DETAIL_LIMIT"
  # The updater loader is responsible for opening the monitor after this script
  # exits successfully. Suppress 110a-request-run.sh's normal monitor opener so
  # an optional smoke request cannot show the monitor before steps 12/final done.
  PC_REQUEST_OPEN_MONITOR=0 ./src/pipeline/110a-request-run.sh "$DETAIL_LIMIT"
else
  echo "Skipped smoke run. Set PC_UPDATE_TEST_DETAIL_LIMIT=5 to queue one during the update without opening the monitor early."
fi

echo ""
echo "12) Restore webhook listener after update"
restart_webhook_listener
cleanup_update_flags
trap - EXIT

echo ""
echo "Finished: $(date '+%Y-%m-%d %H:%M:%S')"
echo "Local copy updated successfully."
