#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
# shellcheck source=lib/env.sh
source "$SCRIPT_DIR/lib/env.sh"
cd "$APP_ROOT"

PYTHON_BIN="${PYTHON_BIN:-python3}"
SKIP_APT="${PC_SETUP_SKIP_APT:-0}"
SKIP_BROWSER="${PC_SETUP_SKIP_BROWSER:-0}"

note() { printf '[setup] %s\n' "$*"; }
fail() { printf '[setup] ERROR: %s\n' "$*" >&2; exit 1; }

require_python() {
  command -v "$PYTHON_BIN" >/dev/null 2>&1 || fail "Python executable not found: $PYTHON_BIN"
  "$PYTHON_BIN" - <<'PY' || exit 1
import sys
if sys.version_info < (3, 10):
    raise SystemExit(f"Python 3.10+ is required; found {sys.version.split()[0]}")
PY
  "$PYTHON_BIN" - <<'PY' || fail "The Python venv module is unavailable. On Debian/Ubuntu/Linux Mint run: sudo apt-get install -y python3-venv python3-full"
import ensurepip  # noqa: F401
import venv  # noqa: F401
PY
}

if [[ "$SKIP_APT" != "1" ]] && command -v apt-get >/dev/null 2>&1; then
  if command -v sudo >/dev/null 2>&1; then SUDO=sudo; else SUDO=; fi
  note "Installing required system packages with apt-get. Set PC_SETUP_SKIP_APT=1 to skip this step."
  $SUDO apt-get update
  $SUDO apt-get install -y python3-venv python3-full python3-tk ca-certificates curl
fi

require_python

if [[ ! -f .env && -f .env.example ]]; then
  cp .env.example .env
  note "Created .env from .env.example. Edit it to customize settings before first run."
fi

note "Creating/updating virtual environment at $APP_ROOT/.venv"
"$PYTHON_BIN" -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip || fail "Unable to upgrade pip. Check network/proxy access or retry with a reachable Python package index."
python -m pip install -r requirements.txt || fail "Unable to install Python dependencies from requirements.txt. Check network/proxy access, then rerun ./setup.sh."

# lib/env.sh already created $PC_DATA_DIR/$PC_RECORDS_DIR/$PC_RECORDS_TEST_DIR/etc above.

if [[ "$SKIP_BROWSER" != "1" ]]; then
  if [[ "$SKIP_APT" != "1" && -r /etc/os-release ]] && grep -qiE 'debian|ubuntu|linuxmint' /etc/os-release; then
    note "Installing Playwright Firefox browser and Linux browser dependencies."
    python -m playwright install --with-deps firefox || fail "Unable to install Playwright Firefox and OS dependencies. Rerun with PC_SETUP_SKIP_BROWSER=1 to skip browser installation temporarily."
  else
    note "Installing Playwright Firefox browser. If first run reports missing system libraries, run: python -m playwright install-deps firefox"
    python -m playwright install firefox || fail "Unable to install Playwright Firefox. Rerun with PC_SETUP_SKIP_BROWSER=1 to skip browser installation temporarily."
  fi
fi

if [[ "$SKIP_BROWSER" == "1" ]]; then
  ./scripts/validate-installation.sh --skip-browser
else
  ./scripts/validate-installation.sh
fi

# Optional Docker stack: changedetection (change trigger) + WAHA (WhatsApp) +
# enqueue-only webhook. Best effort — the collector itself works without it.
SKIP_DOCKER="${PC_SETUP_SKIP_DOCKER:-0}"
if [[ "$SKIP_DOCKER" != "1" ]]; then
  if command -v docker >/dev/null 2>&1 && { docker compose version >/dev/null 2>&1 || command -v docker-compose >/dev/null 2>&1; }; then
    note "Starting the changedetection + WAHA + webhook Docker stack (set PC_SETUP_SKIP_DOCKER=1 to skip)."
    ./src/tools/docker-stack.sh up || note "Docker stack start failed (daemon not running or no permission?). Start it later with: ./src/tools/docker-stack.sh up"
  else
    note "Docker not found: skipping the changedetection/WAHA containers. Install Docker, then run: ./src/tools/docker-stack.sh up"
  fi
else
  note "Docker stack skipped by PC_SETUP_SKIP_DOCKER=1. Start it later with: ./src/tools/docker-stack.sh up"
fi

note "Setup complete. Start with: ./src/pipeline/request-run-all.sh 5 && ./src/monitor/open-monitor.sh"
note "Or use the unified CLI: ./bin/pcc start 5 && ./bin/pcc monitor"
