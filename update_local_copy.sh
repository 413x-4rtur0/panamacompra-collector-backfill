#!/usr/bin/env bash
set -euo pipefail

# Update a local PanamaCompra Collector checkout in place.
# Intended use from the target host:
#   cd ~/Apps/panamacompra-collector
#   ./update_local_copy.sh

BASE_DIR="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
cd "$BASE_DIR" || exit 1

REMOTE="${PC_UPDATE_REMOTE:-origin}"
BRANCH="${PC_UPDATE_BRANCH:-$(git branch --show-current)}"
DETAIL_LIMIT="${PC_UPDATE_TEST_DETAIL_LIMIT:-0}"

if [ -z "$BRANCH" ]; then
  echo "ERROR: Could not detect the current git branch. Set PC_UPDATE_BRANCH explicitly." >&2
  exit 1
fi

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
echo "Branch: $BRANCH"
echo "Log: $LOG_FILE"
echo ""

echo "1) Stop any active collector worker before updating"
if [ -x ./pc_stop_run_all.sh ]; then
  ./pc_stop_run_all.sh || true
else
  rm -f data/queue/run_all_requested.flag
  pkill -TERM -f "[p]c_run_all_worker.sh" 2>/dev/null || true
  pkill -TERM -f "[p]ython3? -u ./pc_index_collector.py" 2>/dev/null || true
  pkill -TERM -f "[p]ython3? -u ./pc_detail_downloader.py" 2>/dev/null || true
fi

echo ""
echo "2) Verify there are no local code changes that would be overwritten"
if [ -n "$(git status --porcelain)" ]; then
  echo "ERROR: Local checkout has uncommitted changes. Review them before updating:"
  git status --short
  exit 1
fi

echo ""
echo "3) Fetch and fast-forward the current branch"
git fetch --prune "$REMOTE"
git checkout "$BRANCH"
git pull --ff-only "$REMOTE" "$BRANCH"

echo ""
echo "4) Ensure executable bits are set"
chmod +x ./*.sh ./*.py

echo ""
echo "5) Ensure Python virtual environment and dependencies"
venv_is_healthy() {
  [ -x .venv/bin/python ] || return 1
  .venv/bin/python -c "import ensurepip; import subprocess" >/dev/null 2>&1
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
echo "6) Run repository health checks"
./review_panamacompra_system.sh

echo ""
echo "7) Optional smoke run request"
if [ "$DETAIL_LIMIT" != "0" ]; then
  echo "Requesting smoke run with detail limit: $DETAIL_LIMIT"
  ./pc_request_run_all.sh "$DETAIL_LIMIT"
else
  echo "Skipped smoke run. Set PC_UPDATE_TEST_DETAIL_LIMIT=5 to request one after update."
fi

echo ""
echo "Finished: $(date '+%Y-%m-%d %H:%M:%S')"
echo "Local copy updated successfully."
