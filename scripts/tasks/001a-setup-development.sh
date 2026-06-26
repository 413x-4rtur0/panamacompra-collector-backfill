#!/usr/bin/env bash
# Phase 001a: bootstrap/update the checkout for development or portable use.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
exec "$ROOT/setup.sh" "$@"
