#!/usr/bin/env bash
# Phase 000b: evaluate from-zero clone/install readiness.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
exec "$ROOT/bin/pcc" doctor "$@"
