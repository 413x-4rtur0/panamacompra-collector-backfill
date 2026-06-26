#!/usr/bin/env bash
# Ordered alias: Open default monitor.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
exec "$ROOT/pc_open_monitor.sh" "$@"
