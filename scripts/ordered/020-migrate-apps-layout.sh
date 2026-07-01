#!/usr/bin/env bash
# Ordered alias: Migrate older split app folders into this checkout.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
exec "$ROOT/pc_migrate_apps_layout.sh" "$@"
