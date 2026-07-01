#!/usr/bin/env bash
# Ordered alias: Environment-aware queue-status wrapper.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
exec "$ROOT/scripts/queue_status.sh" "$@"
