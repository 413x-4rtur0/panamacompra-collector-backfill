#!/usr/bin/env bash
# Phase 000: review old root scripts against new ordered task wrappers.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
exec "$ROOT/scripts/review_entrypoint_names.sh" "$@"
