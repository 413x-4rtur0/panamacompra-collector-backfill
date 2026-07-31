#!/usr/bin/env bash
# Phase 040: stop collectors, updater, and webhook host processes; keep monitors open.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
exec "$ROOT/bin/pcc" stop "$@"
