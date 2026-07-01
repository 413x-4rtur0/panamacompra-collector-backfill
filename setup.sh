#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
# shellcheck source=lib/env.sh
source "$ROOT/lib/env.sh"
PYTHON_BIN="${PYTHON_BIN:-python3}"
SKIP_APT="${PC_SETUP_SKIP_APT:-0}"
SKIP_BROWSER="${PC_SETUP_SKIP_BROWSER:-0}"
IMPORT_BUNDLE="${PC_SETUP_IMPORT_BUNDLE:-}"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --import-bundle) IMPORT_BUNDLE="$2"; shift ;;
    -h|--help) echo "Usage: ./setup.sh [--import-bundle bundle.tar.gz]"; exit 0 ;;
    *) echo "Unknown setup option: $1" >&2; exit 2 ;;
  esac
  shift
done

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

if [[ -z "$IMPORT_BUNDLE" && -t 0 ]]; then
  read -r -p "Import persistent PanamaCompra data bundle now? [path or blank to skip]: " IMPORT_BUNDLE || true
fi
if [[ -n "$IMPORT_BUNDLE" ]]; then
  "$APP_ROOT/scripts/persistent_data.sh" import "$IMPORT_BUNDLE"
fi

if [[ "$SKIP_BROWSER" != "1" ]]; then
  python -m playwright install firefox
fi

if [[ "$SKIP_BROWSER" == "1" ]]; then
  "$APP_ROOT/scripts/validate_installation.sh" --skip-browser
else
  "$APP_ROOT/scripts/validate_installation.sh"
fi
echo "Setup complete. Start with: ./bin/pcc start 5 && ./bin/pcc monitor"
