#!/usr/bin/env bash
# Ordered alias: Uninstall/shutdown and optionally purge.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
exec "$ROOT/scripts/uninstall.sh" "$@"
