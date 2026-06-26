#!/usr/bin/env bash
# Ordered alias: Review repository/system health.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
exec "$ROOT/review_panamacompra_system.sh" "$@"
