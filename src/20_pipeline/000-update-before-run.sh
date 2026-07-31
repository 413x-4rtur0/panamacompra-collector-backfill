#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
# shellcheck source=../../lib/env.sh
source "$SCRIPT_DIR/../../lib/env.sh"
cd "$APP_ROOT"

LOG_FILE="$PC_LOG_DIR/update_before_run_$(date +%Y%m%d_%H%M%S).log"
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
  git stash push -m "update-before-run autostash $(date '+%Y-%m-%d %H:%M:%S')" >/dev/null 2>&1 || true
fi

echo "Pre-run update started at $(date '+%Y-%m-%d %H:%M:%S')"
echo "Remote: $REMOTE"
echo "Branch: $BRANCH"

git fetch --prune "$REMOTE"
git checkout "$BRANCH" 2>/dev/null || git checkout -B "$BRANCH" "$REMOTE/$BRANCH"
if ! git pull --ff-only "$REMOTE" "$BRANCH"; then
  # Appliance installs must always end up on the remote code, so they hard-reset.
  # A DEVELOPMENT checkout may have local commits the operator cares about —
  # discarding them silently is how work gets lost — so development mode keeps
  # the local code and continues the run. Force the old behavior with
  # PC_UPDATE_FORCE_RESET=1.
  if [ "$APP_MODE" = "development" ] && [ "${PC_UPDATE_FORCE_RESET:-0}" != "1" ]; then
    echo "Fast-forward not possible and APP_MODE=development: keeping the local branch (no hard reset). Set PC_UPDATE_FORCE_RESET=1 to force reset to $REMOTE/$BRANCH."
  else
    echo "Fast-forward not possible; resetting $BRANCH to $REMOTE/$BRANCH."
    git reset --hard "$REMOTE/$BRANCH"
  fi
fi
find . -maxdepth 4 \( -name "*.sh" -o -name "*.py" \) -not -path "./.venv/*" -exec chmod +x {} +
chmod +x ./bin/pcc

echo "Reviewing/updating archive DB metadata after code refresh."
python -u ./src/50_tools/050-maintain-database.py --apply || echo "WARNING: DB maintenance failed; continuing pre-run update."

if [ -d .venv ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
  python -m pip install -r requirements.txt
else
  echo "No .venv directory found; dependency refresh skipped. Run ./update-local-copy.sh for full setup."
fi

echo "Pre-run update finished at $(date '+%Y-%m-%d %H:%M:%S')"
