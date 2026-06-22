#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" || exit 1

mkdir -p data/logs
LOG_FILE="data/logs/update_before_run_$(date +%Y%m%d_%H%M%S).log"
exec > >(tee -a "$LOG_FILE") 2>&1

REMOTE="${PC_UPDATE_REMOTE:-origin}"
BRANCH="${PC_UPDATE_BRANCH:-$(git branch --show-current)}"

if [ "${PC_RUN_UPDATE_BEFORE_RUN:-1}" = "0" ]; then
  echo "Pre-run update skipped because PC_RUN_UPDATE_BEFORE_RUN=0."
  exit 0
fi

if [ -z "$BRANCH" ]; then
  echo "ERROR: Could not detect current branch. Set PC_UPDATE_BRANCH explicitly." >&2
  exit 1
fi

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "ERROR: This directory is not a git checkout." >&2
  exit 1
fi

if [ -n "$(git status --porcelain)" ]; then
  echo "ERROR: Local checkout has uncommitted changes; refusing pre-run update." >&2
  git status --short
  exit 1
fi

echo "Pre-run update started at $(date '+%Y-%m-%d %H:%M:%S')"
echo "Remote: $REMOTE"
echo "Branch: $BRANCH"

git fetch --prune "$REMOTE"
git checkout "$BRANCH"
git pull --ff-only "$REMOTE" "$BRANCH"
chmod +x ./*.sh ./*.py

if [ -d .venv ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
  python -m pip install -r requirements.txt
else
  echo "No .venv directory found; dependency refresh skipped. Run ./update_local_copy.sh for full setup."
fi

echo "Pre-run update finished at $(date '+%Y-%m-%d %H:%M:%S')"
