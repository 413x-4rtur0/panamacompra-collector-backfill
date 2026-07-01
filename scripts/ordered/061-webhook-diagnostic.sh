#!/usr/bin/env bash
# Ordered alias: Diagnose webhook setup.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
exec "$ROOT/pc_webhook_diagnostic.sh" "$@"
