#!/usr/bin/env bash
# Ordered alias: Migrate older records into current layout.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
exec "$ROOT/migrate_previous_records.sh" "$@"
