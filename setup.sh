#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
# shellcheck source=lib/env.sh
source "$ROOT/lib/env.sh"
PYTHON_BIN="${PYTHON_BIN:-python3}"
SKIP_APT="${PC_SETUP_SKIP_APT:-0}"
SKIP_BROWSER="${PC_SETUP_SKIP_BROWSER:-0}"

if [[ "$SKIP_APT" != "1" ]] && command -v apt-get >/dev/null 2>&1; then
  if command -v sudo >/dev/null 2>&1; then SUDO=sudo; else SUDO=; fi
  $SUDO apt-get update
  $SUDO apt-get install -y python3-venv python3-full python3-tk
fi

"$PYTHON_BIN" -m venv "$APP_ROOT/.venv"
# shellcheck disable=SC1091
source "$APP_ROOT/.venv/bin/activate"
python -m pip install --upgrade pip
python -m pip install -r "$APP_ROOT/requirements.txt"

mkdir -p "$PC_LOG_DIR" "$PC_QUEUE_DIR" "$PC_CONFIG_DIR" "$PC_RECORDS_DIR" "${PC_RECORDS_TEST_DIR:-$PC_STATE_DIR/records_test}"

if [[ "$SKIP_BROWSER" != "1" ]]; then
  python -m playwright install firefox
fi

if [[ "$SKIP_BROWSER" == "1" ]]; then
  "$APP_ROOT/scripts/validate_installation.sh" --skip-browser
else
  "$APP_ROOT/scripts/validate_installation.sh"
fi
echo "Setup complete. Start with: ./bin/pcc start 5 && ./bin/pcc monitor"
