#!/usr/bin/env bash
# Ordered alias: Review old/new entrypoint names.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
exec "$ROOT/scripts/review_entrypoint_names.sh" "$@"
