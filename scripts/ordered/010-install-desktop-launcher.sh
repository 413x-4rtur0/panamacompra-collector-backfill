#!/usr/bin/env bash
# Ordered alias: Install/remove Update + Monitor launcher.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
exec "$ROOT/scripts/install_desktop_launcher.sh" "$@"
