#!/usr/bin/env bash
# Ordered alias: Fresh-clone readiness checks.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
exec "$ROOT/scripts/fresh_install_doctor.sh" "$@"
