#!/usr/bin/env bash
# Ordered alias: Bootstrap dependencies and local setup.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
exec "$ROOT/setup.sh" "$@"
