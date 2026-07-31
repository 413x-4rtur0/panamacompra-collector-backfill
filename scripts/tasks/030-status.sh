#!/usr/bin/env bash
# Phase 030: inspect queues, progress, logs, and process state.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
exec "$ROOT/bin/pcc" status "$@"
