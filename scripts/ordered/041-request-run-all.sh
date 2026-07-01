#!/usr/bin/env bash
# Ordered alias: Queue a full run-all request.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
exec "$ROOT/pc_request_run_all.sh" "$@"
