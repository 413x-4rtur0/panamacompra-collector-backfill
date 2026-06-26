#!/usr/bin/env bash
# Ordered alias: Open terminal/window monitor.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
exec "$ROOT/pc_monitor_window.sh" "$@"
