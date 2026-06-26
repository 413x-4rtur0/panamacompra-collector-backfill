#!/usr/bin/env bash
# Ordered alias: Follow run-all logs/progress.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
exec "$ROOT/pc_follow_run_all.sh" "$@"
