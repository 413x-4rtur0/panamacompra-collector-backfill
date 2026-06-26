#!/usr/bin/env bash
# Ordered alias: Environment-aware worker wrapper.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
exec "$ROOT/scripts/run_worker.sh" "$@"
