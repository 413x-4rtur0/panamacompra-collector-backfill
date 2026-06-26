#!/usr/bin/env bash
# Phase 020: request/start the collector run-all workflow.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
exec "$ROOT/bin/pcc" start "$@"
