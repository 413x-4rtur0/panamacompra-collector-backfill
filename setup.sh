#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"
PYTHON_BIN="${PYTHON_BIN:-python3}"
SKIP_APT="${PC_SETUP_SKIP_APT:-0}"
SKIP_BROWSER="${PC_SETUP_SKIP_BROWSER:-0}"

if [[ "$SKIP_APT" != "1" ]] && command -v apt-get >/dev/null 2>&1; then
  if command -v sudo >/dev/null 2>&1; then SUDO=sudo; else SUDO=; fi
  $SUDO apt-get update
  $SUDO apt-get install -y python3-venv python3-full python3-tk
fi

"$PYTHON_BIN" -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

mkdir -p data/logs data/queue data/config records records_test

if [[ "$SKIP_BROWSER" != "1" ]]; then
  python -m playwright install firefox
fi

if [[ "$SKIP_BROWSER" == "1" ]]; then
  ./scripts/validate_installation.sh --skip-browser
else
  ./scripts/validate_installation.sh
fi
echo "Setup complete. Start with: ./pc_request_run_all.sh 5 && ./pc_open_monitor.sh"
