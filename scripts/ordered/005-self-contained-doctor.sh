#!/usr/bin/env bash
# Ordered alias: Self-contained Docker/secret policy checks.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
exec "$ROOT/scripts/self_contained_doctor.sh" "$@"
