#!/usr/bin/env bash
# Ordered alias: Install/remove user systemd services.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
exec "$ROOT/scripts/install_user_services.sh" "$@"
