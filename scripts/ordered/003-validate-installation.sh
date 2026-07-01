#!/usr/bin/env bash
# Ordered alias: Validate installation prerequisites.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
exec "$ROOT/scripts/validate_installation.sh" "$@"
