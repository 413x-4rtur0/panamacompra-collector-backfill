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

SETUP_LOG_FILE="${PC_SETUP_LOG_FILE:-$PC_LOG_DIR/setup_$(date +%Y%m%d_%H%M%S).log}"
mkdir -p "$(dirname "$SETUP_LOG_FILE")"
touch "$SETUP_LOG_FILE"
exec > >(tee -a "$SETUP_LOG_FILE") 2>&1
SETUP_STARTED_AT="$(date '+%Y-%m-%d %H:%M:%S')"
finish_setup_log() {
  local code=$?
  local finished_at
  finished_at="$(date '+%Y-%m-%d %H:%M:%S')"
  if [[ "$code" -eq 0 ]]; then
    printf '[setup] Finished successfully at %s. Full log: %s\n' "$finished_at" "$SETUP_LOG_FILE"
  else
    printf '[setup] FAILED with exit code %s at %s. Review full log: %s\n' "$code" "$finished_at" "$SETUP_LOG_FILE" >&2
  fi
}
trap finish_setup_log EXIT

note "Setup started at $SETUP_STARTED_AT"
note "Setup log: $SETUP_LOG_FILE"

install_monitor_launchers() {
  # Install/update desktop launchers before validation/browser steps that may be
  # skipped or fail in partial/headless setups. Launchers are useful even when the
  # collector browser still needs to be installed later.
  if [[ "${PC_SETUP_INSTALL_MONITOR_SHORTCUT:-1}" != "0" ]]; then
    if [[ -f ./scripts/install-desktop-launcher.sh ]]; then
      chmod +x ./scripts/install-desktop-launcher.sh
      note "Installing PanamaCompra desktop launchers (Update + Monitor, changedetection, WAHA, Docker). Set PC_SETUP_INSTALL_MONITOR_SHORTCUT=0 to skip."
      ./scripts/install-desktop-launcher.sh install || note "Desktop launcher install failed; retry later with: ./bin/pcc launcher install"
    else
      note "Desktop launcher installer is missing; retry later with: ./bin/pcc launcher install"
    fi
  else
    note "Desktop launcher install skipped by PC_SETUP_INSTALL_MONITOR_SHORTCUT=0."
  fi
}

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
note "Python $("$PYTHON_BIN" -c 'import sys; print(".".join(map(str, sys.version_info[:3])))') found at $(command -v "$PYTHON_BIN") — no interpreter install needed."

if [[ ! -f .env && -f .env.example ]]; then
  cp .env.example .env
  note "Created .env from .env.example. Edit it to customize settings before first run."
elif [[ -f .env ]]; then
  note "Existing .env recognized and kept (delete it to regenerate from .env.example)."
fi

if [[ -d .venv ]]; then
  note "Existing virtual environment .venv recognized — reusing it; dependencies upgrade in place. Delete .venv to force a clean rebuild."
fi
note "Creating/updating virtual environment at $APP_ROOT/.venv"
"$PYTHON_BIN" -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip || fail "Unable to upgrade pip. Check network/proxy access or retry with a reachable Python package index."
python -m pip install -r requirements.txt || fail "Unable to install Python dependencies from requirements.txt. Check network/proxy access, then rerun ./setup.sh."

# lib/env.sh already created $PC_DATA_DIR/$PC_RECORDS_DIR/$PC_RECORDS_TEST_DIR/etc above.
install_monitor_launchers

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

# Docker dependencies: changedetection (change trigger) + WAHA (WhatsApp) +
# enqueue-only webhook. Installs the Docker engine itself when missing (via
# apt), then starts the stack. Best effort — the collector works without it.
SKIP_DOCKER="${PC_SETUP_SKIP_DOCKER:-0}"
if [[ "$SKIP_DOCKER" != "1" ]]; then
  if command -v docker >/dev/null 2>&1; then
    note "Docker already installed ($(docker --version 2>/dev/null || echo version unknown)) — skipping engine install."
  fi
  if ! command -v docker >/dev/null 2>&1; then
    if [[ "$SKIP_APT" != "1" ]] && command -v apt-get >/dev/null 2>&1; then
      if command -v sudo >/dev/null 2>&1; then SUDO=sudo; else SUDO=; fi
      note "Installing the Docker engine + compose plugin with apt-get (set PC_SETUP_SKIP_DOCKER=1 to skip)."
      # Package name for the compose v2 plugin varies by distro release.
      $SUDO apt-get install -y docker.io docker-compose-v2 \
        || $SUDO apt-get install -y docker.io docker-compose-plugin \
        || $SUDO apt-get install -y docker.io docker-compose \
        || note "Could not install Docker with apt-get. Install it manually, then run: ./src/tools/010-docker-stack.sh up"
      if command -v systemctl >/dev/null 2>&1; then
        $SUDO systemctl enable --now docker >/dev/null 2>&1 || note "Could not enable the docker service automatically (systemctl enable --now docker)."
      fi
      if command -v docker >/dev/null 2>&1 && [[ -n "${USER:-}" ]] && ! id -nG "$USER" 2>/dev/null | grep -qw docker; then
        $SUDO usermod -aG docker "$USER" \
          && note "Added $USER to the 'docker' group. Log out/in (or run 'newgrp docker') to manage containers without sudo." \
          || true
      fi
    else
      note "Docker not found and apt install unavailable/skipped. Install Docker manually, then run: ./src/tools/010-docker-stack.sh up"
    fi
  fi
  if command -v docker >/dev/null 2>&1; then
    note "Starting the changedetection + WAHA + webhook Docker stack."
    if ! ./src/tools/010-docker-stack.sh up; then
      if command -v sudo >/dev/null 2>&1; then
        # A user freshly added to the docker group cannot reach the socket until
        # re-login; bootstrap the first start with sudo so setup ends complete.
        note "Retrying the Docker stack start with sudo (fresh 'docker' group membership applies after re-login)."
        sudo ./src/tools/010-docker-stack.sh up || note "Docker stack start failed — start it later with: ./src/tools/010-docker-stack.sh up"
      else
        note "Docker stack start failed (daemon not running or no permission?). Start it later with: ./src/tools/010-docker-stack.sh up"
      fi
    fi
  else
    note "Docker is still unavailable: the changedetection/WAHA containers stay off. Install Docker, then run: ./src/tools/010-docker-stack.sh up"
  fi
else
  note "Docker install/stack skipped by PC_SETUP_SKIP_DOCKER=1. Start it later with: ./src/tools/010-docker-stack.sh up"
fi

ACCESS_NOTE_FILE="${PC_INTEGRATION_CREDENTIALS_FILE:-$PC_DATA_DIR/config/integration-access.txt}"
if [[ -f "$ACCESS_NOTE_FILE" ]]; then
  note "Integration access note (WAHA API key, webhook URL/token, changedetection note): $ACCESS_NOTE_FILE"
fi
note "Setup complete. Start with: ./src/pipeline/110a-request-run.sh 5 && ./src/monitor/000-open-monitor.sh"
note "Or use the unified CLI: ./bin/pcc start 5 && ./bin/pcc monitor"
note "To stop/remove a previous or duplicate installation, run ./scripts/uninstall.sh in THAT installation's folder (interactive; data kept unless purged)."
