#!/usr/bin/env bash
# Ordered alias: Install legacy webhook user service.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
exec "$ROOT/pc_install_webhook_service.sh" "$@"
