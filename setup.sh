#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
# shellcheck source=lib/env.sh
source "$SCRIPT_DIR/lib/env.sh"
cd "$APP_ROOT"

PYTHON_BIN="${PYTHON_BIN:-python3}"
SKIP_APT="${PC_SETUP_SKIP_APT:-0}"
SKIP_BROWSER="${PC_SETUP_SKIP_BROWSER:-0}"

if [[ "$SKIP_APT" != "1" ]] && command -v apt-get >/dev/null 2>&1; then
  if command -v sudo >/dev/null 2>&1; then SUDO=sudo; else SUDO=; fi
  $SUDO apt-get update
  $SUDO apt-get install -y python3-venv python3-full python3-tk
fi

if [[ ! -f .env && -f .env.example ]]; then
  cp .env.example .env
  echo "Created .env from .env.example. Edit it to customize settings before first run."
fi

"$PYTHON_BIN" -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

# lib/env.sh already created $PC_DATA_DIR/$PC_RECORDS_DIR/$PC_RECORDS_TEST_DIR/etc above.

if [[ "$SKIP_BROWSER" != "1" ]]; then
  python -m playwright install firefox
fi

if [[ "$SKIP_BROWSER" == "1" ]]; then
  ./scripts/validate-installation.sh --skip-browser
else
  ./scripts/validate-installation.sh
fi
echo "Setup complete. Start with: ./src/pipeline/request-run-all.sh 5 && ./src/monitor/open-monitor.sh"
echo "Or use the unified CLI: ./bin/pcc start 5 && ./bin/pcc monitor"
