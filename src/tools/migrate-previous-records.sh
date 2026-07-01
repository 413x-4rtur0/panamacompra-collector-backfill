#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
# shellcheck source=../../lib/env.sh
source "$SCRIPT_DIR/../../lib/env.sh"
cd "$APP_ROOT"

PYTHON_BIN="${PYTHON_BIN:-python3}"
if [ -f ".venv/bin/activate" ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
  PYTHON_BIN="python"
fi

"$PYTHON_BIN" "$SCRIPT_DIR/migrate-previous-records.py" | tee "$PC_LOG_DIR/migrate_previous_records_$(date +%Y%m%d_%H%M%S).log"
