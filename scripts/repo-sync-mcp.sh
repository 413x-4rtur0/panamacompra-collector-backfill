#!/usr/bin/env bash
set -euo pipefail

PULL=0
MERGE=0
REBASE=0
ADD=0
DO_COMMIT=0
PUSH=0
REINDEX=0
SNAPSHOT=0
DRY_RUN=0
COMMIT_MSG=""

usage() {
  cat <<'EOF'
Usage:
  bash scripts/repo-sync-mcp.sh [options]

Options:
  --pull                 Pull latest code. Default strategy is rebase.
  --merge                Use git pull --no-rebase.
  --rebase               Use git pull --rebase.
  --add                  Add safe repo files.
  --commit "message"     Commit staged changes.
  --push                 Push current branch.
  --reindex              Reindex local codebase-memory MCP.
  --snapshot             Create/upload frozen MCP snapshot.
  --all                  Pull rebase, add, commit, push, reindex, snapshot.
  --dry-run              Show actions without making changes.
  -h, --help             Show this help.

Examples:
  bash scripts/repo-sync-mcp.sh --all --commit "Update dashboard KPI docs"
  bash scripts/repo-sync-mcp.sh --pull --add --commit "Update monitors" --push --reindex
  bash scripts/repo-sync-mcp.sh --snapshot
EOF
}

run() {
  if [[ "$DRY_RUN" == "1" ]]; then
    printf '[DRY-RUN] %q ' "$@"
    echo
  else
    "$@"
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --pull) PULL=1; shift ;;
    --merge) MERGE=1; REBASE=0; shift ;;
    --rebase) REBASE=1; MERGE=0; shift ;;
    --add) ADD=1; shift ;;
    --commit)
      DO_COMMIT=1
      COMMIT_MSG="${2:-}"
      shift 2
      ;;
    --push) PUSH=1; shift ;;
    --reindex) REINDEX=1; shift ;;
    --snapshot) SNAPSHOT=1; shift ;;
    --all)
      PULL=1
      REBASE=1
      ADD=1
      DO_COMMIT=1
      PUSH=1
      REINDEX=1
      SNAPSHOT=1
      shift
      ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *)
      echo "Unknown option: $1"
      usage
      exit 1
      ;;
  esac
done

if ! ROOT="$(git rev-parse --show-toplevel 2>/dev/null)"; then
  echo "ERROR: not inside a Git repository."
  exit 1
fi

cd "$ROOT"

REPO_NAME="$(basename "$ROOT")"
BRANCH="$(git branch --show-current 2>/dev/null || true)"
TS="$(date +%Y-%m-%d_%H-%M-%S)"

if [[ -z "$BRANCH" ]]; then
  echo "ERROR: detached HEAD. Checkout a branch first."
  exit 1
fi

echo "Repo:   $ROOT"
echo "Branch: $BRANCH"
echo ""

ensure_gitignore() {
  touch .gitignore

  add_ignore_line() {
    local line="$1"
    if ! grep -qxF "$line" .gitignore; then
      echo "$line" >> .gitignore
    fi
  }

  if ! grep -q "Local MCP/codebase-memory index" .gitignore; then
    {
      echo ""
      echo "# Local MCP/codebase-memory index"
    } >> .gitignore
  fi

  add_ignore_line ".codebase-memory/"

  if ! grep -q "Secrets and local environment" .gitignore; then
    {
      echo ""
      echo "# Secrets and local environment"
    } >> .gitignore
  fi

  add_ignore_line ".env"
  add_ignore_line ".env.*"
  add_ignore_line "!.env.example"

  if ! grep -q "Heavy/generated local files" .gitignore; then
    {
      echo ""
      echo "# Heavy/generated local files"
    } >> .gitignore
  fi

  add_ignore_line "node_modules/"
  add_ignore_line ".venv/"
  add_ignore_line "venv/"
  add_ignore_line "__pycache__/"
  add_ignore_line "*.log"
  add_ignore_line "*.sqlite"
  add_ignore_line "*.db"
  add_ignore_line "*.zip"
  add_ignore_line "*.tar"
  add_ignore_line "*.tar.gz"
}

untrack_mcp_live_file() {
  if git ls-files --error-unmatch .codebase-memory/graph.db.zst >/dev/null 2>&1; then
    echo "Untracking live MCP graph, keeping local file..."
    run git rm --cached .codebase-memory/graph.db.zst
  fi

  if git ls-files | grep -qE '^\.codebase-memory/'; then
    echo "WARNING: more .codebase-memory files are tracked:"
    git ls-files | grep -E '^\.codebase-memory/' || true
    echo "Untracking all tracked .codebase-memory files..."
    while IFS= read -r f; do
      [[ -n "$f" ]] && run git rm --cached "$f"
    done < <(git ls-files | grep -E '^\.codebase-memory/' || true)
  fi
}

safety_check_tracked_secrets() {
  echo "Checking tracked risky files..."
  local risky
  risky="$(git ls-files | grep -Ei '(^|/)\.env($|\.)|secret|token|cookie|password|graph\.db\.zst|^\.codebase-memory/' || true)"

  if [[ -n "$risky" ]]; then
    echo "WARNING: risky tracked paths found:"
    echo "$risky"
    echo ""
    echo "If this includes real secrets, stop and clean before pushing."
  else
    echo "No obvious risky tracked paths found."
  fi
}

do_pull() {
  if [[ "$PULL" == "1" ]]; then
    echo ""
    echo "Pulling latest changes..."
    if [[ "$MERGE" == "1" ]]; then
      run git pull --no-rebase
    else
      run git pull --rebase
    fi
  fi
}

do_add() {
  if [[ "$ADD" == "1" ]]; then
    echo ""
    echo "Adding safe files..."

    ensure_gitignore
    untrack_mcp_live_file

    run git add -A

    # Always unstage unsafe files if they slipped in.
    run git restore --staged .codebase-memory/ 2>/dev/null || true
    run git restore --staged .env 2>/dev/null || true
    run git restore --staged .env.* 2>/dev/null || true
    run git restore --staged node_modules/ 2>/dev/null || true
    run git restore --staged .venv/ 2>/dev/null || true
    run git restore --staged venv/ 2>/dev/null || true

    if git diff --cached --name-only | grep -Ei '(^|/)\.env($|\.)|^\.codebase-memory/|graph\.db\.zst' >/dev/null 2>&1; then
      echo "ERROR: unsafe file staged. Refusing to continue."
      git diff --cached --name-only | grep -Ei '(^|/)\.env($|\.)|^\.codebase-memory/|graph\.db\.zst' || true
      exit 1
    fi

    echo ""
    echo "Staged files:"
    git diff --cached --name-status || true
  fi
}

do_commit() {
  if [[ "$DO_COMMIT" == "1" ]]; then
    echo ""
    if git diff --cached --quiet; then
      echo "Nothing staged to commit."
      return 0
    fi

    if [[ -z "$COMMIT_MSG" ]]; then
      COMMIT_MSG="Sync repo updates ${TS}"
    fi

    safety_check_tracked_secrets

    echo ""
    echo "Committing: $COMMIT_MSG"
    run git commit -m "$COMMIT_MSG"
  fi
}

do_push() {
  if [[ "$PUSH" == "1" ]]; then
    echo ""
    echo "Pushing branch: $BRANCH"
    run git push origin "$BRANCH"
  fi
}

do_reindex() {
  if [[ "$REINDEX" == "1" ]]; then
    echo ""
    echo "Reindexing local codebase-memory MCP..."

    if ! command -v codebase-memory-mcp >/dev/null 2>&1; then
      echo "ERROR: codebase-memory-mcp not found in PATH."
      echo "Skipping reindex."
      return 0
    fi

    run codebase-memory-mcp cli index_repository "{\"repo_path\":\"$ROOT\",\"mode\":\"full\"}"

    echo ""
    echo "MCP artifact check:"
    if [[ -f ".codebase-memory/graph.db.zst" ]]; then
      ls -lh .codebase-memory/graph.db.zst
    else
      echo "No .codebase-memory/graph.db.zst found. MCP may be using internal local DB only."
    fi
  fi
}

do_snapshot() {
  if [[ "$SNAPSHOT" == "1" ]]; then
    echo ""
    echo "Creating frozen MCP snapshot..."

    local SRC=".codebase-memory/graph.db.zst"

    if [[ ! -f "$SRC" ]]; then
      echo "No $SRC found. Skipping snapshot."
      return 0
    fi

    local WORKDIR
    WORKDIR="$(mktemp -d)"
    trap 'rm -rf "$WORKDIR"' EXIT

    local SNAPSHOT_FILE="codebase-memory-${REPO_NAME}-${TS}.db.zst"
    cp "$SRC" "$WORKDIR/$SNAPSHOT_FILE"
    sha256sum "$WORKDIR/$SNAPSHOT_FILE" > "$WORKDIR/$SNAPSHOT_FILE.sha256"

    if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
      local TAG="codebase-memory-${TS}"
      echo "Uploading GitHub Release: $TAG"

      run gh release create "$TAG" \
        "$WORKDIR/$SNAPSHOT_FILE" \
        "$WORKDIR/$SNAPSHOT_FILE.sha256" \
        --title "Codebase Memory Snapshot ${TS}" \
        --notes "Frozen codebase-memory-mcp graph snapshot for ${REPO_NAME}. Live .codebase-memory/ remains ignored in main."
    else
      local BACKUP_DIR="$HOME/Backups/codebase-memory/$REPO_NAME"
      mkdir -p "$BACKUP_DIR"
      run cp "$WORKDIR/$SNAPSHOT_FILE" "$BACKUP_DIR/"
      run cp "$WORKDIR/$SNAPSHOT_FILE.sha256" "$BACKUP_DIR/"
      echo "Saved local snapshot:"
      ls -lh "$BACKUP_DIR/$SNAPSHOT_FILE" "$BACKUP_DIR/$SNAPSHOT_FILE.sha256"
    fi
  fi
}

echo "Initial status:"
git status --short

do_pull
ensure_gitignore
untrack_mcp_live_file
do_add
do_commit
do_push
do_reindex
do_snapshot

echo ""
echo "Final status:"
git status --short

echo ""
echo "Done."
