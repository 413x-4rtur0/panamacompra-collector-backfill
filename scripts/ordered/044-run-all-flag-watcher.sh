#!/usr/bin/env bash
# Ordered alias: Watch queue flags and launch host worker.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
exec "$ROOT/pc_run_all_flag_watcher.sh" "$@"
