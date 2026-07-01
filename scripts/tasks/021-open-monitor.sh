#!/usr/bin/env bash
# Phase 021: open the monitor for an already-started or queued workflow.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
exec "$ROOT/bin/pcc" monitor "$@"
