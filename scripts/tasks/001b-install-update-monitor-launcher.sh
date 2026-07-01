#!/usr/bin/env bash
# Phase 001b: create/update the desktop launcher after the checkout exists.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
exec "$ROOT/bin/pcc" launcher install "$@"
