#!/usr/bin/env bash
# One-time GitHub credential setup so the desktop update launcher stops asking
# for a password / popping the Linux Mint keyring after every restart.
#
# WHAT IT DOES
#   * Points THIS repo's git at the on-disk `store` credential helper instead of
#     the GNOME keyring (libsecret), so a pull never needs the keyring unlocked.
#     The change is repo-local: your global git config and other apps are not
#     touched.
#   * If a token is available (PC_GITHUB_TOKEN env or data/config/github_token.txt)
#     it pre-seeds ~/.git-credentials so even the first pull is silent.
#
# USAGE
#   ./src/tools/setup-git-credentials.sh
#   PC_GITHUB_USER=myuser PC_GITHUB_TOKEN=ghp_xxx ./src/tools/setup-git-credentials.sh
#   # or drop the values in gitignored files first:
#   #   echo myuser  > data/config/github_user.txt
#   #   echo ghp_xxx > data/config/github_token.txt
#
# A fine-grained token with Contents: Read-only on this repo is enough for the
# updater (it only fetches/pulls).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
# shellcheck source=../../lib/env.sh
source "$SCRIPT_DIR/../../lib/env.sh"
cd "$APP_ROOT"

CONFIG_DIR="$PC_DATA_DIR/config"
USER_FILE="$CONFIG_DIR/github_user.txt"
TOKEN_FILE="$CONFIG_DIR/github_token.txt"
mkdir -p "$CONFIG_DIR"

url="$(git remote get-url origin 2>/dev/null || echo '')"
if [ -z "$url" ]; then
  echo "ERROR: no 'origin' remote found in this checkout." >&2
  exit 1
fi
echo "origin = $url"

case "$url" in
  git@*|ssh://*)
    echo ""
    echo "This repo uses an SSH remote, so the keyring prompt is an SSH key passphrase,"
    echo "not a stored GitHub password. For an unattended box, use a passphrase-less key:"
    echo "  ssh-keygen -t ed25519 -f ~/.ssh/panamacompra -N ''"
    echo "  # add ~/.ssh/panamacompra.pub as a read-only Deploy Key on the GitHub repo, then:"
    echo "  git config core.sshCommand 'ssh -i ~/.ssh/panamacompra -o IdentitiesOnly=yes'"
    echo ""
    echo "Testing current SSH auth (Ctrl-C if it hangs on a passphrase):"
    ssh -T git@github.com || true
    exit 0
    ;;
esac

# --- HTTPS remote: use the on-disk store helper for THIS repo only. -----------
# Setting an empty helper first clears any inherited (global libsecret) helper so
# git uses ONLY `store` here and never reaches for the keyring.
git config --local credential.helper ""
git config --local --add credential.helper store
# Don't let a missing/blocked credential pop a GUI dialog from the launcher; if
# something is wrong it should fail in the log, not hang waiting on the keyring.
git config --local core.askPass ""
echo "Configured: this repo now stores git credentials in ~/.git-credentials (no keyring)."

# --- Optional: pre-seed the token so the very first pull is also silent. -------
gh_user="${PC_GITHUB_USER:-}"
gh_token="${PC_GITHUB_TOKEN:-}"
[ -z "$gh_user" ]  && [ -f "$USER_FILE" ]  && gh_user="$(tr -d ' \t\r\n' < "$USER_FILE")"
[ -z "$gh_token" ] && [ -f "$TOKEN_FILE" ] && gh_token="$(tr -d ' \t\r\n' < "$TOKEN_FILE")"

host="github.com"
case "$url" in
  https://*) host="$(printf '%s' "$url" | sed -E 's#^https://([^/]+)/.*#\1#')" ;;
esac

if [ -n "$gh_token" ]; then
  # GitHub accepts the token as the password; the username can be anything
  # non-empty for a token, so default to a literal when none was supplied.
  [ -z "$gh_user" ] && gh_user="x-access-token"
  printf 'protocol=https\nhost=%s\nusername=%s\npassword=%s\n\n' \
    "$host" "$gh_user" "$gh_token" | git credential approve
  echo "Seeded ~/.git-credentials for $host as '$gh_user'. Future pulls will not prompt."
  echo "Verifying with a fetch..."
  if GIT_TERMINAL_PROMPT=0 git fetch --quiet origin; then
    echo "✅ Fetch succeeded with the stored token — restart-proof, no more prompts."
  else
    echo "⚠️  Fetch failed. Check the token value/scope (Contents: Read-only on this repo)." >&2
  fi
else
  echo ""
  echo "No token provided yet. Either:"
  echo "  • set PC_GITHUB_TOKEN (and optionally PC_GITHUB_USER) and re-run this, or"
  echo "  • write it once:  echo <TOKEN> > $TOKEN_FILE  &&  ./src/tools/setup-git-credentials.sh"
  echo "  • or just run 'git pull' once now and enter your username + token —"
  echo "    the 'store' helper will save it and it won't ask again after restart."
fi
