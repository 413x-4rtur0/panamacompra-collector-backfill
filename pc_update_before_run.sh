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

# Untracked runtime files (data/, records/, .webhook_token, ...) never block a
# pre-run update. Only local edits to TRACKED files are stashed out of the way so
# the worker can always fast-forward before a run; the stash is kept for recovery.
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "Local changes to tracked files detected; auto-stashing them before the pre-run update."
  git stash push -m "pc_update_before_run autostash $(date '+%Y-%m-%d %H:%M:%S')" >/dev/null 2>&1 || true
fi

echo "Pre-run update started at $(date '+%Y-%m-%d %H:%M:%S')"
echo "Remote: $REMOTE"
echo "Branch: $BRANCH"

git fetch --prune "$REMOTE"
git checkout "$BRANCH" 2>/dev/null || git checkout -B "$BRANCH" "$REMOTE/$BRANCH"
if ! git pull --ff-only "$REMOTE" "$BRANCH"; then
  echo "Fast-forward not possible; resetting $BRANCH to $REMOTE/$BRANCH."
  git reset --hard "$REMOTE/$BRANCH"
fi
chmod +x ./*.sh ./*.py

if [ -d .venv ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
  python -m pip install -r requirements.txt
else
  echo "No .venv directory found; dependency refresh skipped. Run ./update_local_copy.sh for full setup."
fi

echo "Pre-run update finished at $(date '+%Y-%m-%d %H:%M:%S')"
