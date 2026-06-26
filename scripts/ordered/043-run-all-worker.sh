#!/usr/bin/env bash
# Ordered alias: Internal run-all worker.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
exec "$ROOT/pc_run_all_worker.sh" "$@"
