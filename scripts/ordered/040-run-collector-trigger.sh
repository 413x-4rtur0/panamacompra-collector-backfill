#!/usr/bin/env bash
# Ordered alias: Changedetection webhook trigger entrypoint.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
exec "$ROOT/run_collector.sh" "$@"
