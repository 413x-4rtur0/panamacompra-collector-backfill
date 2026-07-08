#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
# shellcheck source=../../lib/env.sh
source "$SCRIPT_DIR/../../lib/env.sh"
cd "$APP_ROOT"

REMOTE="${PC_UPLOAD_REMOTE:-${PC_UPDATE_REMOTE:-origin}}"
BRANCH="${PC_UPLOAD_BRANCH:-}"
MESSAGE="${PC_UPLOAD_COMMIT_MESSAGE:-}"
DRY_RUN=0
RUN_CHECKS="${PC_UPLOAD_RUN_CHECKS:-0}"
ALLOW_EMPTY=0

usage() {
  cat <<'USAGE'
Usage: src/50_tools/150-upload-github.sh [--message TEXT] [--remote NAME] [--branch NAME] [--dry-run] [--run-checks] [--allow-empty]

Commits local checkout changes and pushes them to GitHub (or another configured
Git remote). Intended for the opposite workflow from update-local-copy.sh:
  local edits -> commit -> git push REMOTE HEAD:BRANCH

Defaults:
  remote: PC_UPLOAD_REMOTE, then PC_UPDATE_REMOTE, then origin
  branch: PC_UPLOAD_BRANCH, then current branch
  message: PC_UPLOAD_COMMIT_MESSAGE, then an automatic timestamped message

Notes:
  - Ignored runtime folders such as data/, records/, var/, integrations/ and .venv
    stay ignored by Git and are not uploaded.
  - Use --dry-run to preview exactly what would be committed/pushed.
  - Configure credentials with src/50_tools/120-setup-git-credentials.sh when using
    an HTTPS GitHub remote.
USAGE
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --message|-m) MESSAGE="${2:-}"; shift 2 ;;
    --remote) REMOTE="${2:-origin}"; shift 2 ;;
    --branch) BRANCH="${2:-}"; shift 2 ;;
    --dry-run|-n) DRY_RUN=1; shift ;;
    --run-checks) RUN_CHECKS=1; shift ;;
    --allow-empty) ALLOW_EMPTY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "ERROR: this directory is not a Git checkout: $APP_ROOT" >&2
  exit 2
fi
REMOTE_URL="$(git remote get-url "$REMOTE" 2>/dev/null || true)"
if [ -z "$REMOTE_URL" ]; then
  if [ "$DRY_RUN" = "1" ]; then
    REMOTE_URL="(remote not configured in this checkout; real upload would fail)"
  else
    echo "ERROR: Git remote '$REMOTE' is not configured." >&2
    echo "Configured remotes:" >&2
    git remote -v >&2 || true
    exit 2
  fi
fi

CURRENT_BRANCH="$(git branch --show-current 2>/dev/null || true)"
BRANCH="${BRANCH:-$CURRENT_BRANCH}"
if [ -z "$BRANCH" ]; then
  echo "ERROR: detached HEAD; pass --branch NAME to choose the remote branch." >&2
  exit 2
fi
if [ -z "$MESSAGE" ]; then
  MESSAGE="Upload local PanamaCompra changes $(date '+%Y-%m-%d %H:%M:%S')"
fi

STATUS_BEFORE="$(git status --short)"
PENDING_COMMITS="$(git rev-list --count "$REMOTE/$BRANCH..HEAD" 2>/dev/null || echo 0)"

cat <<INFO
GitHub upload plan
------------------
Repository: $APP_ROOT
Remote:     $REMOTE ($REMOTE_URL)
Branch:     $BRANCH
Message:    $MESSAGE
Dry-run:    $DRY_RUN
Checks:     $RUN_CHECKS
INFO

echo ""
echo "Current git status:"
if [ -n "$STATUS_BEFORE" ]; then
  printf '%s\n' "$STATUS_BEFORE"
else
  echo "  clean"
fi

echo ""
echo "Commits ahead of $REMOTE/$BRANCH: $PENDING_COMMITS"

if [ "$RUN_CHECKS" = "1" ]; then
  echo ""
  echo "Running upload checks before commit/push..."
  python3 -m py_compile src/30_notify/010-waha-client.py src/40_monitor/001a-monitor-tk.py src/40_monitor/001b-monitor-web.py src/20_pipeline/020-notify-whatsapp.py src/50_tools/040-message-formats.py src/50_tools/140-full-report.py
  bash -n bin/pcc review-system.sh src/20_pipeline/100-run-worker.sh src/50_tools/010-docker-stack.sh
  git diff --check
fi

if [ "$DRY_RUN" = "1" ]; then
  echo ""
  echo "DRY RUN: would run: git add -A"
  if [ -n "$STATUS_BEFORE" ]; then
    echo "DRY RUN: would run: git commit -m '$MESSAGE'"
  elif [ "$ALLOW_EMPTY" = "1" ]; then
    echo "DRY RUN: would run: git commit --allow-empty -m '$MESSAGE'"
  else
    echo "DRY RUN: no working-tree changes to commit."
  fi
  echo "DRY RUN: would run: git push '$REMOTE' HEAD:'$BRANCH'"
  exit 0
fi

if [ -n "$STATUS_BEFORE" ]; then
  git add -A
  git commit -m "$MESSAGE"
elif [ "$ALLOW_EMPTY" = "1" ]; then
  git commit --allow-empty -m "$MESSAGE"
else
  echo "No working-tree changes to commit; pushing existing local commits only."
fi

git push "$REMOTE" "HEAD:$BRANCH"

echo "Upload complete: pushed HEAD to $REMOTE/$BRANCH"
