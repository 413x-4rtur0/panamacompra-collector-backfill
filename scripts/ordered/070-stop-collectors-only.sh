#!/usr/bin/env bash
# Ordered alias: Stop active collector pipeline only.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
exec "$ROOT/pc_stop_collectors.sh" "$@"
