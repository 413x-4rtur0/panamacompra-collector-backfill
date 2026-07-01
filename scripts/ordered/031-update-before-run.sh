#!/usr/bin/env bash
# Ordered alias: Refresh checkout before worker run.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
exec "$ROOT/pc_update_before_run.sh" "$@"
