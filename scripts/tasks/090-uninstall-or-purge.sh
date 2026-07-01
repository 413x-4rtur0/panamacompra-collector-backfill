#!/usr/bin/env bash
# Phase 090: uninstall/shutdown services and optionally purge state/config.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
exec "$ROOT/bin/pcc" uninstall "$@"
