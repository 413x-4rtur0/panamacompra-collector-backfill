#!/usr/bin/env bash
# Ordered alias: Show run-all status.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
exec "$ROOT/pc_run_all_status.sh" "$@"
