#!/usr/bin/env bash
# Ordered alias: Stop all host PanamaCompra processes.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
exec "$ROOT/pc_stop_run_all.sh" "$@"
