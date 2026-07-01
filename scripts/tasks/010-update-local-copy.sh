#!/usr/bin/env bash
# Phase 010: update the local checkout before opening the monitor.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
exec "$ROOT/update-local-copy.sh" "$@"
