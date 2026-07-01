#!/usr/bin/env bash
# Phase 040: stop collectors, monitors, updater, and webhook host processes.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
exec "$ROOT/bin/pcc" stop "$@"
