#!/usr/bin/env bash
# Ordered alias: Configure Git credentials for updater/desktop usage.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
exec "$ROOT/pc_setup_git_credentials.sh" "$@"
