#!/usr/bin/env bash
# Phase 002: install user systemd services for collector/webhook lifecycle.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
exec "$ROOT/bin/pcc" service install --enable "$@"
