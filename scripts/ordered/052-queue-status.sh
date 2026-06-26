#!/usr/bin/env bash
# Ordered alias: Show queue status.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
exec "$ROOT/pc_queue_status.sh" "$@"
