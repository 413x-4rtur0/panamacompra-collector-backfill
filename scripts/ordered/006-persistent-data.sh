#!/usr/bin/env bash
# Ordered alias: Export/import persistent downloaded data.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
exec "$ROOT/scripts/persistent_data.sh" "$@"
