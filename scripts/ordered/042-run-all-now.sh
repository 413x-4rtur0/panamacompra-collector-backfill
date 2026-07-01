#!/usr/bin/env bash
# Ordered alias: Run collector immediately.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
exec "$ROOT/pc_run_all_now.sh" "$@"
