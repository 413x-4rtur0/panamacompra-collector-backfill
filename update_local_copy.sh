#!/usr/bin/env bash
set -euo pipefail

# Update a local PanamaCompra Collector checkout in place.
# Intended use from the target host:
#   cd ~/Apps/panamacompra-collector
#   ./update_local_copy.sh

BASE_DIR="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
cd "$BASE_DIR" || exit 1

REMOTE="${PC_UPDATE_REMOTE:-origin}"
# PC_UPDATE_BRANCH is now an OPTIONAL hard override. When empty (default), the
# updater auto-selects the branch: it tracks main if the most recently updated
# remote branch is already merged into main, otherwise it switches to that
# latest branch. Set PC_UPDATE_BRANCH to pin an exact branch instead.
BRANCH="${PC_UPDATE_BRANCH:-}"
DETAIL_LIMIT="${PC_UPDATE_TEST_DETAIL_LIMIT:-0}"
CHECKED_OUT_BRANCH=""

install_desktop_shortcut() {
  if [ "${PC_UPDATE_INSTALL_MONITOR_SHORTCUT:-1}" = "0" ]; then
    echo "Skipped desktop shortcut install because PC_UPDATE_INSTALL_MONITOR_SHORTCUT=0."
    return 0
  fi

  local desktop_file_name="panamacompra-manual-monitor.desktop"
  local app_dir="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
  local app_path="$app_dir/$desktop_file_name"
  local desktop_dir="${XDG_DESKTOP_DIR:-$HOME/Desktop}"
  local desktop_path="$desktop_dir/$desktop_file_name"
  local icon_path="$BASE_DIR/data/panamacompra-monitor-icon.svg"

  mkdir -p "$app_dir" "$BASE_DIR/data"
  cat > "$icon_path" <<'SVG'
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128">
  <rect width="128" height="128" rx="24" fill="#0f172a"/>
  <rect x="18" y="24" width="92" height="62" rx="8" fill="#111827" stroke="#38bdf8" stroke-width="6"/>
  <path d="M34 68h16l10-24 14 36 10-18h12" fill="none" stroke="#22c55e" stroke-width="7" stroke-linecap="round" stroke-linejoin="round"/>
  <rect x="42" y="94" width="44" height="8" rx="4" fill="#38bdf8"/>
</svg>
SVG

  cat > "$app_path" <<DESKTOP
[Desktop Entry]
Type=Application
Name=PanamaCompra Update + Monitor
Comment=Open the centered updater loader, update PanamaCompra Collector, then open the manual monitor
Exec=$BASE_DIR/pc_update_loader.py --open-monitor-after
Icon=$icon_path
Terminal=false
Categories=Utility;Monitor;
StartupNotify=false
DESKTOP
  chmod +x "$app_path"
  echo "Installed application shortcut: $app_path"
  echo "The shortcut starts pc_update_loader.py, whose Tk updater window is centered before the monitor opens."

  if [ -d "$desktop_dir" ]; then
    cp "$app_path" "$desktop_path"
    chmod +x "$desktop_path"
    echo "Installed desktop shortcut: $desktop_path"
    echo "If your desktop asks, choose 'Allow Launching' or 'Trust and Launch' once."
  else
    echo "Desktop folder not found ($desktop_dir); application-menu shortcut was installed only."
  fi

  if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$app_dir" >/dev/null 2>&1 || true
  fi
}

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "ERROR: $BASE_DIR is not a git checkout." >&2
  exit 1
fi

mkdir -p data/logs data/queue
LOG_FILE="data/logs/update_local_copy_$(date +%Y%m%d_%H%M%S).log"
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

echo "1) Stop the active collector pipeline before updating"
# IMPORTANT: do NOT call ./pc_stop_run_all.sh from here. That broad stopper also
# runs `pkill update_local_copy.sh`, `pkill pc_update_loader.py` and
# `pkill pc_monitor_tk.py` — i.e. it would terminate THIS update process, the
# loader window, and the monitor the user is watching. That self-kill is what
# made the updater appear to "freeze" or close right after step 1, and it also
# added a fixed 5s wait. Instead stop only the collector pipeline plus the
# webhook trigger so a new run cannot start mid-update, and never touch the
# updater/loader/monitor processes.
rm -f data/queue/run_all_requested.flag
pkill -TERM -f "[p]c_run_all_worker.sh" 2>/dev/null || true
pkill -TERM -f "[p]ython3? -u ./pc_index_collector.py" 2>/dev/null || true
pkill -TERM -f "[p]ython3? -u ./pc_detail_downloader.py" 2>/dev/null || true
pkill -TERM -f "[p]ython3? -u ./pc_build_calendar.py" 2>/dev/null || true
pkill -TERM -f "[w]ebhook_listener.py" 2>/dev/null || true

# Wait briefly (max ~3s) for a graceful exit, then force any straggler so the
# update never blocks for long.
for _ in 1 2 3; do
  pgrep -f "[p]c_run_all_worker.sh|[p]ython3? -u ./pc_index_collector.py|[p]ython3? -u ./pc_detail_downloader.py" >/dev/null 2>&1 || break
  sleep 1
done
pkill -9 -f "[p]c_run_all_worker.sh" 2>/dev/null || true
pkill -9 -f "[p]ython3? -u ./pc_index_collector.py" 2>/dev/null || true
pkill -9 -f "[p]ython3? -u ./pc_detail_downloader.py" 2>/dev/null || true

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
chmod +x ./*.sh ./*.py

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
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

echo ""
echo "6) Verify Playwright Firefox browser"
if playwright_firefox_available; then
  echo "Playwright Firefox is already installed and launchable."
elif [ "${PC_UPDATE_SKIP_BROWSER_INSTALL:-0}" = "1" ]; then
  echo "Skipped Playwright Firefox install because PC_UPDATE_SKIP_BROWSER_INSTALL=1."
  echo "WARNING: Playwright Firefox is not currently launchable."
else
  python -m playwright install firefox
  if ! playwright_firefox_available; then
    echo "ERROR: Playwright Firefox installed but could not be launched." >&2
    echo "Install missing system browser dependencies, then retry:" >&2
    echo "  python -m playwright install --with-deps firefox" >&2
    exit 1
  fi
fi

echo ""
echo "7) Run repository health checks"
./review_panamacompra_system.sh

echo ""
echo "8) Refresh already-downloaded records (optional, manual)"
echo "   A normal run only processes NEW records; it never re-pulls previously"
echo "   downloaded ones. To bring existing records up to the current parsing/ICS"
echo "   and the per-section split-table layout, run one of these manually:"
echo "     ./pc_build_detail_views.py --apply                  # rebuild views/.ics + split tables (no browser)"
echo "     ./pc_update_day_folder.py --date <YY-MM-DD> --apply # re-download a day from the portal"
echo "   Then rebuild calendar import packages if needed:"
echo "     ./pc_build_calendar.py --all              # data/calendar/YY-MM-DD packages, default 10 events each"
echo "     PC_CALENDAR_PACKAGE_SIZE=5 ./pc_build_calendar.py --all"
echo "     ./pc_build_calendar.py --all --flat       # optional old parent-only package location"
echo "   To verify the current code when there are no new opportunities, run the"
echo "   testing zone (records_test/latest_5 + records_test/calendar/YY-MM-DD; monitor MODE=TEST/test_run):"
echo "     ./pc_test_zone.py --limit 5 --apply"

echo ""
echo "9) Install manual monitor desktop shortcut"
install_desktop_shortcut

echo ""
echo "10) Optional smoke run request"
if [ "$DETAIL_LIMIT" != "0" ]; then
  echo "Requesting smoke run with detail limit: $DETAIL_LIMIT"
  ./pc_request_run_all.sh "$DETAIL_LIMIT"
else
  echo "Skipped smoke run. Set PC_UPDATE_TEST_DETAIL_LIMIT=5 to request one after update."
fi

echo ""
echo "Finished: $(date '+%Y-%m-%d %H:%M:%S')"
echo "Local copy updated successfully."
