#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../lib/env.sh"
cd "$APP_ROOT"
exec "$APP_ROOT/pc_queue_status.sh" "$@"
