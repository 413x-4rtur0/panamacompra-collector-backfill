#!/usr/bin/env bash
# Ordered alias: Update checkout/dependencies before monitor.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
exec "$ROOT/update_local_copy.sh" "$@"
