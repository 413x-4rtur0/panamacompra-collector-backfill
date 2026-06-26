# Implementation Strategy Report
## Self-Hosted Portable Application Framework Adoption for PanamaCompra Collector

**Report Date:** 26 June 2026  
**Repository:** PanamaCompra Collector  
**Target Platform:** Linux Mint 21.x (Ubuntu 22.04 LTS)  
**Framework Reference:** Self-Hosted Portable Application Framework v1.0

---

## Executive Summary

This report provides a comprehensive analysis and implementation roadmap for transforming the existing PanamaCompra Collector repository into a compliant Self-Hosted Portable Application Framework template. The current repository demonstrates strong operational maturity but requires systematic reorganization to achieve full Twelve-Factor App compliance, XDG specification adherence, and true path-agnostic execution.

**Current State Assessment:** Partial compliance (~45%) with target framework specifications  
**Estimated Effort:** 3-4 sprints (6-8 weeks) for full migration  
**Risk Level:** Medium - requires careful handling of existing user installations

---

## 1. Current Architecture Analysis

### 1.1 Existing Directory Structure

```
/workspace/
├─ .env.example                    ✓ Present (Docker-focused)
├─ .gitignore                      ✓ Present
├─ setup.sh                        △ Partial (missing mode detection)
├─ scripts/
│  └─ validate_installation.sh     ✓ Present
├─ docker/
│  └─ Dockerfile.webhook           ✓ Present
├─ docker-compose.yml              ✓ Present
├─ docs/
│  └─ AGILE_PROCESS.md             ✓ Present
├─ *.py (17 files)                 ✗ Root-level scattering
├─ *.sh (20+ files)                ✗ Root-level scattering
└─ Missing directories:
   ├─ bin/                         ✗ Absent
   ├─ lib/                         ✗ Absent
   ├─ app/                         ✗ Absent
   ├─ config/                      ✗ Absent
   ├─ systemd/user/                ✗ Absent
   ├─ share/docs/                  △ Partial (docs/ exists)
   └─ var/{data,log,run}/          △ Partial (data/ exists ad-hoc)
```

### 1.2 Critical Gaps Identified

| Requirement | Current State | Gap Severity |
|-------------|---------------|--------------|
| Path independence via `APP_ROOT` | Uses `BASE_DIR = Path(__file__).resolve().parent` in `pc_common.py` | **HIGH** |
| Unified CLI entrypoint | Multiple `pc_*.sh` scripts scattered in root | **HIGH** |
| XDG Base Directory compliance | Hardcoded `data/`, `records/` relative paths | **HIGH** |
| Mode detection (clone/portable/install) | No mode detection logic | **HIGH** |
| Process naming convention | Inconsistent (`pc_run_all_worker.sh`, `webhook_listener.py`) | **MEDIUM** |
| Environment configuration externalization | `.env` used only for Docker Compose | **MEDIUM** |
| Build-Release-Run separation | No build artifacts, direct script execution | **MEDIUM** |
| Systemd service templates | Ad-hoc generation in `pc_install_webhook_service.sh` | **MEDIUM** |
| Dependency isolation | `.venv/` created but not mode-aware | **LOW** |
| Semantic versioning | Tags exist but no SBOM generation | **LOW** |

---

## 2. Proposed Implementation Hierarchy

### 2.1 Phase 1: Foundation Layer (Week 1-2)

#### 2.1.1 Create Core Infrastructure Files

**File: `lib/env.sh`** (NEW - Critical Path Component)
```bash
#!/usr/bin/env bash
# Universal environment resolver - computes APP_ROOT and runtime mode
set -euo pipefail

# Compute APP_ROOT dynamically (Factor I: Codebase)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export APP_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Detect execution mode
detect_mode() {
  local install_marker="$XDG_DATA_HOME/panamacompra/.installed"
  local portable_marker="$APP_ROOT/var/.portable"
  
  if [[ -f "$install_marker" ]]; then
    echo "installed"
  elif [[ -f "$portable_marker" ]]; then
    echo "portable"
  elif [[ "$APP_ROOT" =~ ^($HOME/dev|$HOME/src|$HOME/git) ]]; then
    echo "development"
  else
    echo "portable"
  fi
}

export APP_MODE="${APP_MODE:-$(detect_mode)}"

# XDG Base Directory resolution (XDG Spec compliance)
export XDG_DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
export XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
export XDG_CACHE_HOME="${XDG_CACHE_HOME:-$HOME/.cache}"

# Mode-specific state locations
case "$APP_MODE" in
  installed)
    export PC_DATA_DIR="${PC_DATA_DIR:-$XDG_DATA_HOME/panamacompra/data}"
    export PC_RECORDS_DIR="${PC_RECORDS_DIR:-$XDG_DATA_HOME/panamacompra/records}"
    export PC_CONFIG_DIR="${PC_CONFIG_DIR:-$XDG_CONFIG_HOME/panamacompra}"
    ;;
  portable|development)
    export PC_DATA_DIR="${PC_DATA_DIR:-$APP_ROOT/var/data}"
    export PC_RECORDS_DIR="${PC_RECORDS_DIR:-$APP_ROOT/var/records}"
    export PC_CONFIG_DIR="${PC_CONFIG_DIR:-$APP_ROOT/config}"
    ;;
esac

# Load configuration hierarchy
load_config() {
  local config_file="${PC_CONFIG_DIR}/env"
  [[ -f "$config_file" ]] && source "$config_file"
}

load_config
```

**File: `bin/pcc`** (NEW - Primary CLI Entrypoint)
```bash
#!/usr/bin/env bash
# PanamaCompra Collector - Unified CLI Controller
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../lib/env.sh"

usage() {
  cat <<EOF
Usage: pcc <command> [options]

Commands:
  start           Start the collector worker (alias: run)
  stop            Stop running collectors gracefully
  status          Show collector and queue status
  dev             Start development mode with auto-reload
  install         Install system-wide or user-mode (see: install --help)
  migrate         Run database migrations
  webhook         Manage webhook listener service
  
Options:
  -h, --help      Show this help message
  -v, --version   Show version information

Examples:
  pcc start                    # Start collector worker
  pcc install --user           # Install to user directory
  pcc migrate                  # Run pending migrations
EOF
}

cmd_start() {
  exec "$APP_ROOT/scripts/run_worker.sh" "$@"
}

cmd_stop() {
  exec "$APP_ROOT/scripts/stop_collectors.sh" "$@"
}

cmd_status() {
  exec "$APP_ROOT/scripts/queue_status.sh" "$@"
}

cmd_dev() {
  echo "Starting development mode..."
  exec "$APP_ROOT/scripts/dev.sh" "$@"
}

cmd_install() {
  exec "$APP_ROOT/scripts/install.sh" "$@"
}

cmd_migrate() {
  exec "$APP_ROOT/scripts/migrate.sh" "$@"
}

cmd_webhook() {
  exec "$APP_ROOT/scripts/webhook_ctl.sh" "$@"
}

# Command dispatch
main() {
  local cmd="${1:-}"
  shift || true
  
  case "$cmd" in
    start|run)     cmd_start "$@" ;;
    stop)          cmd_stop "$@" ;;
    status)        cmd_status "$@" ;;
    dev)           cmd_dev "$@" ;;
    install)       cmd_install "$@" ;;
    migrate)       cmd_migrate "$@" ;;
    webhook)       cmd_webhook "$@" ;;
    -h|--help)     usage; exit 0 ;;
    -v|--version)  echo "panamacompra-collector v$(cat "$APP_ROOT/VERSION" 2>/dev/null || echo 'unknown')"; exit 0 ;;
    *)             echo "Error: Unknown command '$cmd'" >&2; usage >&2; exit 1 ;;
  esac
}

main "$@"
```

#### 2.1.2 Directory Reorganization Plan

```bash
# Migration script to be executed once
mkdir -p bin lib app config scripts systemd/user share/docs var/{data,log,run}

# Move application code to app/
mv *.py app/ 2>/dev/null || true

# Move scripts to appropriate locations
mv setup.sh scripts/setup.sh
mv pc_*.sh bin/ 2>/dev/null || true  # Legacy scripts preserved during transition

# Create new canonical scripts
touch scripts/{setup.sh,install.sh,dev.sh,migrate.sh,run_worker.sh,webhook_ctl.sh}
chmod +x scripts/*.sh bin/*

# Create systemd template
touch systemd/user/panamacompra.service
```

### 2.2 Phase 2: Configuration Management (Week 2-3)

#### 2.2.1 Enhanced Configuration Hierarchy

**File: `config/defaults.env`** (NEW)
```bash
# PanamaCompra Collector - Default Configuration
# DO NOT EDIT - Override in $XDG_CONFIG_HOME/panamacompra/env or .env

# Application identity
APP_NAME="panamacompra-collector"
APP_VERSION="1.0.0"

# Network configuration
PC_WEBHOOK_HOST="0.0.0.0"
PC_WEBHOOK_PORT="8765"
CHANGEDETECTION_BASE_URL="http://localhost:5000"

# Collection limits
PC_WEBHOOK_DETAIL_LIMIT="99"
PC_INDEX_LIMIT="0"

# WAHA integration
WAHA_API_KEY=""
WAHA_ENGINE="WEBJS"
WAHA_PORT="3000"

# Runtime behavior
PC_RUN_MODE="RESTART"
PC_REQUEST_OPEN_MONITOR="1"
PC_SETUP_SKIP_APT="0"
PC_SETUP_SKIP_BROWSER="0"

# Paths (resolved by lib/env.sh, included for documentation)
# PC_DATA_DIR → $XDG_DATA_HOME/panamacompra/data (installed) or ./var/data (portable)
# PC_RECORDS_DIR → $XDG_DATA_HOME/panamacompra/records (installed) or ./var/records (portable)
# PC_CONFIG_DIR → $XDG_CONFIG_HOME/panamacompra (installed) or ./config (portable)
```

**File: `.env.example`** (ENHANCED - Replace existing)
```bash
# PanamaCompra Collector - Environment Configuration
# Copy to $XDG_CONFIG_HOME/panamacompra/env (installed) or .env (portable/dev)
# See: https://12factor.net/config

# =============================================================================
# NETWORK CONFIGURATION
# =============================================================================

# Public URL changedetection.io advertises in its UI and notification links
CHANGEDETECTION_BASE_URL=http://localhost:5000

# Webhook listener host and port
PC_WEBHOOK_HOST=0.0.0.0
PC_WEBHOOK_PORT=8765

# =============================================================================
# COLLECTION CONFIGURATION
# =============================================================================

# Detail pages processed per triggered run
PC_WEBHOOK_DETAIL_LIMIT=99

# Maximum index pages per group (0 = unlimited)
PC_INDEX_LIMIT=0

# Run mode: RESTART, RESUME, or ONCE
PC_RUN_MODE=RESTART

# =============================================================================
# WAHA WHATSAPP INTEGRATION
# =============================================================================

# Optional WAHA API key for authenticated notifications
WAHA_API_KEY=

# WAHA WhatsApp engine (WEBJS, WHATSAPP-BUSINESS, etc.)
WAHA_ENGINE=WEBJS

# WAHA container port mapping
WAHA_PORT=3000

# =============================================================================
# RUNTIME BEHAVIOR
# =============================================================================

# Automatically open monitor after run request (1=yes, 0=no)
PC_REQUEST_OPEN_MONITOR=1

# Skip apt package installation during setup (1=skip)
PC_SETUP_SKIP_APT=0

# Skip Playwright browser installation (1=skip)
PC_SETUP_SKIP_BROWSER=0

# =============================================================================
# PATH OVERRIDES (Advanced)
# =============================================================================

# Override default data directory (normally managed by lib/env.sh)
# PC_DATA_DIR=/custom/path/to/data

# Override default records directory
# PC_RECORDS_DIR=/custom/path/to/records

# Override database path
# PC_ARCHIVE_DB_PATH=/custom/path/to/panamacompra_archive.db
```

### 2.3 Phase 3: Script Modernization (Week 3-4)

#### 2.3.1 Script Naming Convention Migration

| Legacy Script | New Location | New Usage Pattern |
|---------------|--------------|-------------------|
| `setup.sh` | `scripts/setup.sh` | `./scripts/setup.sh` or `pcc install --dev` |
| `pc_request_run_all.sh` | `bin/pcc start` | `pcc start` |
| `pc_stop_run_all.sh` | `bin/pcc stop` | `pcc stop` |
| `pc_queue_status.sh` | `bin/pcc status` | `pcc status` |
| `pc_install_webhook_service.sh` | `scripts/install.sh --webhook` | `pcc install --webhook` |
| `pc_migrate_apps_layout.sh` | `scripts/migrate.sh --layout` | `pcc migrate --layout` |
| `pc_open_monitor.sh` | `bin/pcc monitor` | `pcc monitor` |
| `pc_webhook_diagnostic.sh` | `bin/pcc webhook diag` | `pcc webhook diag` |
| `pc_run_all_worker.sh` | `scripts/run_worker.sh` | Internal use only |
| `run_collector.sh` | `scripts/run_collector.sh` | Internal use only |

#### 2.3.2 Enhanced Setup Script

**File: `scripts/setup.sh`** (REWRITE)
```bash
#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../lib/env.sh"

PYTHON_BIN="${PYTHON_BIN:-python3}"
SKIP_APT="${PC_SETUP_SKIP_APT:-0}"
SKIP_BROWSER="${PC_SETUP_SKIP_BROWSER:-0}"
INTERACTIVE="${1:-}"

log() { echo "[SETUP] $(date '+%H:%M:%S') $*"; }

# Detect package manager
if [[ "$SKIP_APT" != "1" ]] && command -v apt-get >/dev/null 2>&1; then
  if command -v sudo >/dev/null 2>&1; then SUDO=sudo; else SUDO=; fi
  
  log "Updating package lists..."
  $SUDO apt-get update
  
  log "Installing system dependencies..."
  $SUDO apt-get install -y python3-venv python3-full python3-tk jq
fi

# Create virtual environment
log "Creating virtual environment in $APP_ROOT/.venv..."
"$PYTHON_BIN" -m venv "$APP_ROOT/.venv"
source "$APP_ROOT/.venv/bin/activate"

# Upgrade pip and install dependencies
log "Upgrading pip..."
python -m pip install --upgrade pip

log "Installing Python dependencies..."
python -m pip install -r "$APP_ROOT/requirements.txt"

# Initialize runtime directories based on mode
log "Initializing runtime directories for mode: $APP_MODE"
case "$APP_MODE" in
  installed)
    mkdir -p "$XDG_DATA_HOME/panamacompra"/{data,records}
    mkdir -p "$XDG_CONFIG_HOME/panamacompra"
    # Create marker file
    touch "$XDG_DATA_HOME/panamacompra/.installed"
    ;;
  portable|development)
    mkdir -p "$APP_ROOT"/var/{data,log,run}
    mkdir -p "$APP_ROOT"/var/records
    touch "$APP_ROOT/var/.portable"
    ;;
esac

# Initialize configuration from template
if [[ ! -f "$PC_CONFIG_DIR/env" ]] && [[ -f "$APP_ROOT/.env.example" ]]; then
  log "Creating initial configuration..."
  mkdir -p "$PC_CONFIG_DIR"
  cp "$APP_ROOT/.env.example" "$PC_CONFIG_DIR/env"
  log "Configuration written to: $PC_CONFIG_DIR/env"
fi

# Install Playwright browsers
if [[ "$SKIP_BROWSER" != "1" ]]; then
  log "Installing Playwright Firefox browser..."
  python -m playwright install firefox
else
  log "Skipping browser installation (PC_SETUP_SKIP_BROWSER=1)"
fi

# Run validation
log "Running installation validation..."
if [[ "$SKIP_BROWSER" == "1" ]]; then
  "$APP_ROOT/scripts/validate_installation.sh" --skip-browser
else
  "$APP_ROOT/scripts/validate_installation.sh"
fi

echo ""
echo "=============================================="
echo "Setup complete!"
echo "=============================================="
echo ""
echo "Next steps:"
echo "  1. Edit configuration: nano $PC_CONFIG_DIR/env"
echo "  2. Start collector:    pcc start"
echo "  3. Check status:       pcc status"
echo ""
echo "For installation options:"
echo "  User mode:  ./scripts/install.sh --user"
echo "  System:     sudo ./scripts/install.sh --system"
echo ""
```

### 2.4 Phase 4: Systemd Integration (Week 4)

#### 2.4.1 Service Template

**File: `systemd/user/panamacompra.service`** (NEW)
```ini
[Unit]
Description=PanamaCompra Collector Service
Documentation=https://github.com/<ORG>/panamacompra-collector/tree/main/share/docs
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment=APP_MODE=installed
Environment=XDG_DATA_HOME=%h/.local/share
Environment=XDG_CONFIG_HOME=%h/.config
Environment=XDG_CACHE_HOME=%h/.cache

# Dynamic path resolution via wrapper script
ExecStartPre=-/usr/bin/mkdir -p %h/.local/share/panamacompra/data
ExecStartPre=-/usr/bin/mkdir -p %h/.local/share/panamacompra/records
ExecStartPre=-/usr/bin/mkdir -p %h/.config/panamacompra

WorkingDirectory=%h/.local/share/panamacompra
ExecStart=%h/.local/share/panamacompra/bin/pcc start
Restart=on-failure
RestartSec=5
TimeoutStopSec=30

# Security hardening
NoNewPrivileges=true
ProtectHome=read-only
ProtectSystem=strict
ReadWritePaths=%h/.local/share/panamacompra
PrivateTmp=true

# Logging (Factor XI: Logs)
StandardOutput=journal
StandardError=journal
SyslogIdentifier=panamacompra

[Install]
WantedBy=default.target
```

**File: `systemd/user/panamacompra-webhook.service`** (NEW)
```ini
[Unit]
Description=PanamaCompra Webhook Listener
After=network-online.target panamacompra.service
Wants=network-online.target

[Service]
Type=simple
Environment=APP_MODE=installed
Environment=PC_WEBHOOK_HOST=0.0.0.0
Environment=PC_WEBHOOK_PORT=8765
Environment=PC_WEBHOOK_ENQUEUE_ONLY=1

WorkingDirectory=%h/.local/share/panamacompra
ExecStart=%h/.local/share/panamacompra/app/webhook_listener.py --foreground
Restart=on-failure
RestartSec=5

# Security
NoNewPrivileges=true
ProtectHome=read-only
ProtectSystem=strict
ReadWritePaths=%h/.local/share/panamacompra/data

# Logging
StandardOutput=journal
StandardError=journal
SyslogIdentifier=panamacompra-webhook

[Install]
WantedBy=default.target
```

#### 2.4.2 Installation Script

**File: `scripts/install.sh`** (NEW)
```bash
#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../lib/env.sh"

INSTALL_MODE="user"
INSTALL_PREFIX=""
CREATE_SYMLINKS=true
INSTALL_SERVICES=true

usage() {
  cat <<EOF
Usage: $0 [OPTIONS]

Installation modes:
  --user        Install to user directory (default: \$XDG_DATA_HOME/panamacompra)
  --system      Install to /opt/panamacompra (requires sudo)
  --prefix DIR  Custom installation prefix

Options:
  --no-symlinks   Skip creating /usr/local/bin symlinks
  --no-services   Skip systemd service installation
  --dry-run       Show what would be done without making changes
  -h, --help      Show this help message

Examples:
  $0 --user                    # User-mode installation
  sudo $0 --system             # System-wide installation
  $0 --prefix /custom/path     # Custom location
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --user) INSTALL_MODE="user" ;;
    --system) INSTALL_MODE="system" ;;
    --prefix) INSTALL_PREFIX="$2"; shift ;;
    --no-symlinks) CREATE_SYMLINKS=false ;;
    --no-services) INSTALL_SERVICES=false ;;
    --dry-run) DRY_RUN=true; echo "[DRY-RUN MODE]" ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 1 ;;
  esac
  shift
done

# Determine installation paths
case "$INSTALL_MODE" in
  user)
    INSTALL_DIR="${INSTALL_PREFIX:-$XDG_DATA_HOME/panamacompra}"
    SYSTEMD_DIR="$XDG_CONFIG_HOME/systemd/user"
    ;;
  system)
    INSTALL_DIR="${INSTALL_PREFIX:-/opt/panamacompra}"
    SYSTEMD_DIR="/etc/systemd/system"
    ;;
esac

log() { echo "[INSTALL] $*"; }
do_cmd() {
  if [[ "${DRY_RUN:-false}" == "true" ]]; then
    echo "[DRY-RUN] $*"
  else
    "$@"
  fi
}

log "Installing PanamaCompra Collector ($INSTALL_MODE mode)"
log "Target directory: $INSTALL_DIR"

# Create installation directory
do_cmd mkdir -p "$INSTALL_DIR"

# Copy application files (content-addressable rsync)
log "Copying application files..."
do_cmd rsync -a --delete \
  --exclude='.git' \
  --exclude='.venv' \
  --exclude='data/' \
  --exclude='records/' \
  --exclude='records_test/' \
  --exclude='*.log' \
  "$APP_ROOT/" "$INSTALL_DIR/"

# Create mode marker
do_cmd touch "$INSTALL_DIR/var/.installed"

# Update shebangs in scripts if needed
if [[ "$INSTALL_DIR" != "$APP_ROOT" ]]; then
  log "Updating script paths..."
  find "$INSTALL_DIR/bin" "$INSTALL_DIR/scripts" -type f -name "*.sh" -exec \
    sed -i "s|$(echo "$APP_ROOT" | sed 's/[\/&]/\\&/g')|$INSTALL_DIR|g" {} \;
fi

# Install systemd services
if [[ "$INSTALL_SERVICES" == "true" ]]; then
  log "Installing systemd services..."
  do_cmd mkdir -p "$SYSTEMD_DIR"
  do_cmd cp "$INSTALL_DIR/systemd/user/"*.service "$SYSTEMD_DIR/"
  
  if [[ "${DRY_RUN:-false}" != "true" ]]; then
    systemctl --${INSTALL_MODE} daemon-reload
    systemctl --${INSTALL_MODE} enable panamacompra.service
    systemctl --${INSTALL_MODE} enable panamacompra-webhook.service
  fi
fi

# Create global symlinks
if [[ "$CREATE_SYMLINKS" == "true" ]]; then
  log "Creating command symlinks..."
  if [[ "$INSTALL_MODE" == "system" ]] || command -v sudo >/dev/null 2>&1; then
    do_cmd ln -sf "$INSTALL_DIR/bin/pcc" /usr/local/bin/pcc
  else
    mkdir -p "$HOME/.local/bin"
    ln -sf "$INSTALL_DIR/bin/pcc" "$HOME/.local/bin/pcc"
    echo "Added $HOME/.local/bin to PATH in your shell profile to use 'pcc' command"
  fi
fi

echo ""
echo "=============================================="
echo "Installation complete!"
echo "=============================================="
echo ""
echo "Installation directory: $INSTALL_DIR"
echo "Configuration directory: $XDG_CONFIG_HOME/panamacompra"
echo "Data directory: $XDG_DATA_HOME/panamacompra/data"
echo ""
echo "Manage services:"
echo "  systemctl --${INSTALL_MODE} start panamacompra"
echo "  systemctl --${INSTALL_MODE} status panamacompra"
echo "  journalctl --${INSTALL_MODE} -u panamacompra -f"
echo ""
```

### 2.5 Phase 5: Python Code Refactoring (Week 5)

#### 2.5.1 Update pc_common.py for Path Independence

**Changes required in `app/pc_common.py`:**

```python
# CURRENT CODE (Line 10-11):
# BASE_DIR = Path(__file__).resolve().parent

# REPLACEMENT CODE:
import os
from pathlib import Path

def resolve_app_root() -> Path:
    """
    Resolve application root using environment-aware logic.
    Supports: git clone, portable copy, system install.
    """
    # Priority 1: Explicit environment variable
    if os.environ.get('APP_ROOT'):
        return Path(os.environ['APP_ROOT']).resolve()
    
    # Priority 2: Installed mode marker
    xdg_data = Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local' / 'share'))
    installed_marker = xdg_data / 'panamacompra' / '.installed'
    if installed_marker.exists():
        return xdg_data / 'panamacompra'
    
    # Priority 3: Portable mode marker
    script_dir = Path(__file__).resolve().parent
    if (script_dir.parent / 'var' / '.portable').exists():
        return script_dir.parent
    
    # Priority 4: Development mode (running from git checkout)
    return script_dir.parent

BASE_DIR = resolve_app_root()

# Update all path resolutions to use environment variables with fallbacks
def env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    path = Path(raw).expanduser()
    return path if path.is_absolute() else BASE_DIR / path

# Existing function continues...
```

#### 2.5.2 Additional Python Files Requiring Updates

| File | Required Changes |
|------|------------------|
| `app/webhook_listener.py` | Add `lib/env.sh` sourcing wrapper or Python equivalent |
| `app/pc_monitor_tk.py` | Use `PC_CONFIG_DIR` for settings file location |
| `app/pc_monitor_server.py` | Same as above |
| `app/pc_next_run_timer.py` | Ensure path independence |
| All `app/pc_*.py` files | Import from centralized config module |

**Recommended: Create `app/config.py` module**
```python
"""
Centralized configuration management for PanamaCompra Collector.
Implements Twelve-Factor App Factor III: Config
"""
import os
from pathlib import Path
from typing import Optional

class Config:
    """Application configuration loaded from environment with sensible defaults."""
    
    def __init__(self):
        # Application identity
        self.app_name = os.environ.get('APP_NAME', 'panamacompra-collector')
        self.app_mode = os.environ.get('APP_MODE', 'development')
        
        # Base directories
        self.app_root = self._resolve_app_root()
        self.xdg_data_home = Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local' / 'share'))
        self.xdg_config_home = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config'))
        self.xdg_cache_home = Path(os.environ.get('XDG_CACHE_HOME', Path.home() / '.cache'))
        
        # Data directories (mode-aware)
        if self.app_mode == 'installed':
            self.data_dir = self._path_from_env('PC_DATA_DIR', self.xdg_data_home / 'panamacompra' / 'data')
            self.records_dir = self._path_from_env('PC_RECORDS_DIR', self.xdg_data_home / 'panamacompra' / 'records')
            self.config_dir = self._path_from_env('PC_CONFIG_DIR', self.xdg_config_home / 'panamacompra')
        else:
            self.data_dir = self._path_from_env('PC_DATA_DIR', self.app_root / 'var' / 'data')
            self.records_dir = self._path_from_env('PC_RECORDS_DIR', self.app_root / 'var' / 'records')
            self.config_dir = self._path_from_env('PC_CONFIG_DIR', self.app_root / 'config')
        
        # Database and logs
        self.db_path = self._path_from_env('PC_ARCHIVE_DB_PATH', self.data_dir / 'panamacompra_archive.db')
        self.log_dir = self.data_dir / 'logs'
        
        # Network configuration
        self.webhook_host = os.environ.get('PC_WEBHOOK_HOST', '0.0.0.0')
        self.webhook_port = int(os.environ.get('PC_WEBHOOK_PORT', '8765'))
        self.changedetection_base_url = os.environ.get('CHANGEDETECTION_BASE_URL', 'http://localhost:5000')
        
        # Collection limits
        self.detail_limit = int(os.environ.get('PC_WEBHOOK_DETAIL_LIMIT', '99'))
        self.index_limit = int(os.environ.get('PC_INDEX_LIMIT', '0'))
        
        # WAHA integration
        self.waha_api_key = os.environ.get('WAHA_API_KEY', '')
        self.waha_engine = os.environ.get('WAHA_ENGINE', 'WEBJS')
        self.waha_port = int(os.environ.get('WAHA_PORT', '3000'))
        
        # Runtime behavior
        self.run_mode = os.environ.get('PC_RUN_MODE', 'RESTART')
        self.request_open_monitor = os.environ.get('PC_REQUEST_OPEN_MONITOR', '1') == '1'
    
    def _resolve_app_root(self) -> Path:
        """Resolve application root directory."""
        if os.environ.get('APP_ROOT'):
            return Path(os.environ['APP_ROOT']).resolve()
        
        script_dir = Path(__file__).resolve().parent
        return script_dir.parent
    
    def _path_from_env(self, name: str, default: Path) -> Path:
        """Resolve path from environment variable or return default."""
        raw = os.environ.get(name, '').strip()
        if not raw:
            return default
        path = Path(raw).expanduser()
        return path if path.is_absolute() else self.app_root / path
    
    def ensure_directories(self):
        """Create all required directories if they don't exist."""
        for dir_path in [self.data_dir, self.records_dir, self.config_dir, self.log_dir]:
            dir_path.mkdir(parents=True, exist_ok=True)

# Global configuration instance
config = Config()
config.ensure_directories()
```

---

## 3. Implementation Sequence Diagram

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                    MIGRATION SEQUENCE (6-8 Weeks)                           │
└─────────────────────────────────────────────────────────────────────────────┘

Week 1-2: Foundation Layer
┌──────────────────────────────────────────────────────────────────────────┐
│ Sprint 1: Core Infrastructure                                            │
│                                                                          │
│ Day 1-2:  Create lib/env.sh with mode detection                          │
│           ├── Implement APP_ROOT resolution                              │
│           ├── Add XDG Base Directory support                             │
│           └── Test mode detection logic                                  │
│                                                                          │
│ Day 3-4:  Create bin/pcc unified CLI                                     │
│           ├── Implement command dispatcher                               │
│           ├── Add subcommands: start, stop, status, install, migrate     │
│           └── Integrate with legacy scripts (backward compat)            │
│                                                                          │
│ Day 5-7:  Directory reorganization                                       │
│           ├── Create canonical tree structure                            │
│           ├── Move *.py → app/                                           │
│           ├── Move *.sh → bin/ or scripts/                               │
│           └── Update all internal path references                        │
└──────────────────────────────────────────────────────────────────────────┘

Week 2-3: Configuration Management
┌──────────────────────────────────────────────────────────────────────────┐
│ Sprint 2: Configuration & Environment                                    │
│                                                                          │
│ Day 1-3:  Enhance .env.example                                           │
│           ├── Document all configuration options                         │
│           ├── Add hierarchical comments                                  │
│           └── Include path override examples                             │
│                                                                          │
│ Day 4-5:  Create config/defaults.env                                     │
│           ├── Define factory defaults                                    │
│           └── Document override hierarchy                                │
│                                                                          │
│ Day 6-10: Create app/config.py module                                    │
│           ├── Implement Config class                                     │
│           ├── Add mode-aware path resolution                             │
│           ├── Integrate with all Python modules                          │
│           └── Write unit tests                                           │
└──────────────────────────────────────────────────────────────────────────┘

Week 3-4: Script Modernization
┌──────────────────────────────────────────────────────────────────────────┐
│ Sprint 3: Scripts & Automation                                           │
│                                                                          │
│ Day 1-3:  Rewrite scripts/setup.sh                                       │
│           ├── Source lib/env.sh                                          │
│           ├── Add mode-aware directory initialization                    │
│           └── Improve logging and error handling                         │
│                                                                          │
│ Day 4-7:  Create scripts/install.sh                                      │
│           ├── Support --user and --system modes                          │
│           ├── Implement content-addressable rsync                        │
│           ├── Add systemd service installation                           │
│           └── Create PATH symlinks                                       │
│                                                                          │
│ Day 8-10: Migrate legacy scripts                                         │
│           ├── pc_request_run_all.sh → bin/pcc start                      │
│           ├── pc_stop_run_all.sh → bin/pcc stop                          │
│           ├── pc_queue_status.sh → bin/pcc status                        │
│           └── Maintain backward compatibility symlinks                   │
└──────────────────────────────────────────────────────────────────────────┘

Week 4: Systemd Integration
┌──────────────────────────────────────────────────────────────────────────┐
│ Sprint 4: Service Management                                             │
│                                                                          │
│ Day 1-3:  Create systemd/user/panamacompra.service                       │
│           ├── Configure Type=simple                                      │
│           ├── Add security hardening directives                          │
│           └── Set up journald logging                                    │
│                                                                          │
│ Day 4-5:  Create systemd/user/panamacompra-webhook.service               │
│           ├── Configure After=panamacompra.service                       │
│           └── Isolate webhook listener                                   │
│                                                                          │
│ Day 6-10: Test service lifecycle                                         │
│           ├── Enable/start/stop/restart                                  │
│           ├── Verify log aggregation                                     │
│           └── Test failure recovery                                      │
└──────────────────────────────────────────────────────────────────────────┘

Week 5: Python Refactoring
┌──────────────────────────────────────────────────────────────────────────┐
│ Sprint 5: Code Modernization                                             │
│                                                                          │
│ Day 1-3:  Refactor app/pc_common.py                                      │
│           ├── Replace BASE_DIR with resolve_app_root()                   │
│           ├── Update all path resolutions                                │
│           └── Test in all three modes                                    │
│                                                                          │
│ Day 4-7:  Update remaining Python modules                                │
│           ├── Import from app/config.py                                  │
│           ├── Remove hardcoded paths                                     │
│           └── Add environment variable overrides                         │
│                                                                          │
│ Day 8-10: Testing & validation                                           │
│           ├── Unit tests for config module                               │
│           ├── Integration tests for path resolution                      │
│           └── End-to-end testing in all modes                            │
└──────────────────────────────────────────────────────────────────────────┘

Week 6: Documentation & Release
┌──────────────────────────────────────────────────────────────────────────┐
│ Sprint 6: Documentation & v1.0 Release                                   │
│                                                                          │
│ Day 1-3:  Update README.md                                               │
│           ├── Document new CLI usage                                     │
│           ├── Add installation guide                                     │
│           └── Include troubleshooting section                            │
│                                                                          │
│ Day 4-5:  Create share/docs/                                             │
│           ├── TEMPLATE.md for future projects                            │
│           ├── OPERATIONS.md for administrators                           │
│           └── MIGRATION.md for existing users                            │
│                                                                          │
│ Day 6-8:  Final validation                                               │
│           ├── Clean VM testing                                           │
│           ├── Backward compatibility verification                        │
│           └── Performance benchmarking                                   │
│                                                                          │
│ Day 9-10: Release v1.0.0                                                 │
│           ├── Generate SBOM                                              │
│           ├── Create release tag                                         │
│           └── Publish release notes                                      │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## 4. Compliance Verification Matrix (Post-Implementation)

| Requirement | Implementation Artifact | Standard | Status |
|-------------|------------------------|----------|--------|
| **Factor I: Codebase** | Single repo, git-native | 12FA | ✅ Planned |
| **Factor II: Dependencies** | `.venv/` isolated, requirements.txt pinned | 12FA | ✅ Existing |
| **Factor III: Config** | `lib/env.sh`, `app/config.py`, XDG paths | 12FA | ✅ Planned |
| **Factor IV: Backing Services** | DB path via env, externalized | 12FA | ✅ Planned |
| **Factor V: Build/Release/Run** | `scripts/install.sh` separates stages | 12FA | ✅ Planned |
| **Factor VI: Processes** | Stateless workers, state in backing services | 12FA | ✅ Existing |
| **Factor VII: Port Binding** | `$PC_WEBHOOK_PORT` from env | 12FA | ✅ Existing |
| **Factor VIII: Concurrency** | Worker pool via systemd templates | 12FA | ✅ Planned |
| **Factor IX: Disposability** | Fast startup (<2s target), graceful shutdown | 12FA | ⚠️ Needs work |
| **Factor X: Dev/Prod Parity** | Identical codebase across modes | 12FA | ✅ Planned |
| **Factor XI: Logs** | stdout/stderr → journald | 12FA | ✅ Planned |
| **Factor XII: Admin Processes** | `pcc migrate`, versioned scripts | 12FA | ✅ Planned |
| **XDG Base Directory** | `$XDG_*_HOME` resolution in `lib/env.sh` | XDG | ✅ Planned |
| **POSIX Shell Portability** | `#!/usr/bin/env bash`, `set -euo pipefail` | POSIX | ✅ Existing |
| **FHS 3.0 (System Mode)** | `/opt/panamacompra`, `/etc/systemd/system` | FHS | ✅ Planned |
| **Semantic Versioning** | `vMAJOR.MINOR.PATCH` tags, VERSION file | SemVer | ⚠️ Partial |
| **GitOps** | Declarative systemd units, idempotent install | GitOps | ✅ Planned |
| **DevSecOps** | SBOM generation, dependency scanning | DevSecOps | ⚠️ Needs work |

---

## 5. Risk Assessment & Mitigation Strategies

### 5.1 High-Risk Areas

| Risk | Impact | Probability | Mitigation |
|------|--------|-------------|------------|
| **Breaking existing installations** | Users lose configured paths | Medium | Provide `scripts/migrate.sh --layout` with automatic backup and rollback |
| **Path resolution failures** | Application cannot find data | Medium | Extensive testing in all three modes before release |
| **Systemd service conflicts** | Service fails to start | Low | Use unique service names, test on clean VM |
| **Configuration migration errors** | Lost custom settings | Medium | Auto-migrate `.env` to `$XDG_CONFIG_HOME/panamacompra/env` |
| **Python import breaks** | Modules fail to load | Low | Maintain `sys.path` adjustments during transition |

### 5.2 Backward Compatibility Strategy

**Phase 1 (v0.9.x - Transition Release):**
- Keep legacy scripts in root with deprecation warnings
- Add `lib/env.sh` sourcing to all existing scripts
- Create symlinks: `bin/pcc` → `scripts/pcc_wrapper.sh`

**Phase 2 (v1.0.0 - Breaking Release):**
- Remove legacy root-level scripts
- Provide `scripts/migrate.sh` for one-time migration
- Document migration path in `MIGRATION.md`

**Phase 3 (v1.1.0 - Stabilization):**
- Remove migration scripts
- Only support new directory structure

---

## 6. Testing Strategy

### 6.1 Test Matrix

| Test Scenario | Clone Mode | Portable Mode | Install Mode |
|---------------|------------|---------------|--------------|
| Fresh setup | ✅ Required | ✅ Required | ✅ Required |
| Configuration loading | ✅ Required | ✅ Required | ✅ Required |
| Data directory creation | ✅ Required | ✅ Required | ✅ Required |
| Webhook listener start | ✅ Required | ✅ Required | ✅ Required |
| Collector worker execution | ✅ Required | ✅ Required | ✅ Required |
| Systemd service lifecycle | ❌ N/A | ❌ N/A | ✅ Required |
| Upgrade from v0.x | ✅ Required | ✅ Required | ✅ Required |
| Path relocation (portable) | ❌ N/A | ✅ Required | ❌ N/A |

### 6.2 Validation Commands

```bash
# Test 1: Git clone and run
cd /tmp && git clone <repo> && cd panamacompra-collector
./scripts/setup.sh
./bin/pcc status

# Test 2: Portable mode
cp -r /tmp/panamacompra-collector /mnt/usb/test-app
cd /mnt/usb/test-app
touch var/.portable
./bin/pcc start

# Test 3: User installation
./scripts/install.sh --user
systemctl --user start panamacompra
systemctl --user status panamacompra

# Test 4: System installation (requires VM)
sudo ./scripts/install.sh --system
sudo systemctl start panamacompra
sudo systemctl status panamacompra
```

---

## 7. Recommendations for Future Repositories

### 7.1 Template Instantiation Procedure

To reuse this framework for a new project `<NEWAPP>`:

```bash
# 1. Clone template repository
git clone https://github.com/<ORG>/panamacompra-collector.git <NEWAPP>
cd <NEWAPP>

# 2. Run parameterization script
./scripts/instantiate_template.sh \
  --org "<ORG>" \
  --repo "<NEWAPP>" \
  --app "<NEWAPP>" \
  --app-ctl "<NEWAPP>ctl"

# 3. Review changes
git diff

# 4. Commit instantiation
git add -A
git commit -m "Instantiate template for <NEWAPP>"
```

### 7.2 Parameterization Script (Future Enhancement)

**File: `scripts/instantiate_template.sh`** (TO BE CREATED)
```bash
#!/usr/bin/env bash
# Template instantiation script for new projects

set -euo pipefail

ORG=""
REPO=""
APP=""
APP_CTL=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --org) ORG="$2"; shift ;;
    --repo) REPO="$2"; shift ;;
    --app) APP="$2"; shift ;;
    --app-ctl) APP_CTL="$2"; shift ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
  shift
done

# Validate required parameters
[[ -z "$ORG" || -z "$REPO" || -z "$APP" ]] && {
  echo "Error: --org, --repo, and --app are required" >&2
  exit 1
}

# Default APP_CTL to APP if not specified
APP_CTL="${APP_CTL:-$APP}"

echo "Instantiating template:"
echo "  ORG: $ORG"
echo "  REPO: $REPO"
echo "  APP: $APP"
echo "  APP_CTL: $APP_CTL"

# Perform replacements
find . -type f \( -name "*.sh" -o -name "*.py" -o -name "*.md" -o -name "*.service" -o -name "*.env*" \) \
  -not -path "./.git/*" \
  -exec sed -i \
    -e "s/panamacompra-collector/$REPO/g" \
    -e "s/panamacompra/$APP/g" \
    -e "s/pcc/$APP_CTL/g" \
    {} \;

# Rename files
[[ -f "bin/pcc" ]] && mv "bin/pcc" "bin/$APP_CTL"
[[ -f "systemd/user/panamacompra.service" ]] && \
  mv "systemd/user/panamacompra.service" "systemd/user/$APP.service"

echo "Template instantiation complete!"
echo "Review changes with: git diff"
```

---

## 8. Adjustments Specific to PanamaCompra Collector

### 8.1 Docker Compose Integration

The existing `docker-compose.yml` requires updates to align with the new structure:

**Changes needed:**
```yaml
# Update volumes to use new paths
services:
  webhook:
    volumes:
      - ./var/data/queue:/app/data/queue  # Changed from ./data/queue
      
  changedetection:
    volumes:
      - ./var/integrations/changedetection:/datastore  # Changed from ./integrations/changedetection
  
  waha:
    volumes:
      - ./var/integrations/waha:/app/.sessions  # Changed from ./integrations/waha
```

### 8.2 Monitor Application Considerations

The Tkinter monitor (`pc_monitor_tk.py`) and web monitor (`pc_monitor_server.py`) require special attention:

**Issues:**
- Currently read settings from `data/config/monitor_settings.env`
- Need to migrate to `$XDG_CONFIG_HOME/panamacompra/monitor.env`

**Solution:**
```python
# In app/pc_monitor_tk.py
from pathlib import Path
import os

# OLD:
# SETTINGS_FILE = Path(__file__).parent / "data" / "config" / "monitor_settings.env"

# NEW:
config_dir = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'panamacompra'
SETTINGS_FILE = config_dir / 'monitor.env'

# Fallback to old location for migration
if not SETTINGS_FILE.exists():
    legacy = Path(__file__).parent / "data" / "config" / "monitor_settings.env"
    if legacy.exists():
        SETTINGS_FILE = legacy
```

### 8.3 Queue-Based Architecture Preservation

The existing flag-based queue system (`data/queue/*.flag`) is well-designed and should be preserved:

**Recommendation:** Maintain exact semantics, only relocate to:
- Installed mode: `$XDG_DATA_HOME/panamacompra/data/queue/`
- Portable/Dev mode: `$APP_ROOT/var/data/queue/`

---

## 9. Conclusion

The PanamaCompra Collector repository is an excellent candidate for transformation into a Self-Hosted Portable Application Framework template. The existing codebase demonstrates strong operational maturity, comprehensive error handling, and production-ready patterns.

**Key Strengths to Preserve:**
- Flag-based queue architecture
- Comprehensive logging
- Defensive programming practices
- Modular script design
- Docker Compose integration

**Critical Changes Required:**
1. Implement `lib/env.sh` for path resolution and mode detection
2. Create unified CLI (`bin/pcc`)
3. Reorganize directory structure to canonical tree
4. Externalize all configuration via XDG specifications
5. Add systemd service templates
6. Refactor Python code for path independence

**Timeline:** 6-8 weeks with dedicated development effort  
**Risk:** Medium, manageable with phased rollout and backward compatibility layer  
**Benefit:** Reusable template for future projects, improved maintainability, Twelve-Factor compliance

---

## Appendix A: Quick Reference - Before/After Comparison

| Operation | Before (Current) | After (Target) |
|-----------|------------------|----------------|
| Initial setup | `./setup.sh` | `./scripts/setup.sh` or `pcc install --dev` |
| Start collector | `./pc_request_run_all.sh 5` | `pcc start` |
| Stop collector | `./pc_stop_run_all.sh` | `pcc stop` |
| Check status | `./pc_queue_status.sh` | `pcc status` |
| Install webhook service | `./pc_install_webhook_service.sh` | `pcc install --webhook` |
| Run migrations | `./pc_migrate_apps_layout.sh --apply` | `pcc migrate --layout` |
| Open monitor | `./pc_open_monitor.sh` | `pcc monitor` |
| View logs | `journalctl --user -u panamacompra-webhook` | `pcc logs --follow` |
| Systemd start | Manual service creation | `systemctl --user start panamacompra` |

---

## Appendix B: File Inventory - Complete Restructure List

### Files to Create (NEW)
1. `lib/env.sh` - Environment resolver
2. `bin/pcc` - Unified CLI
3. `bin/pccd` - Daemon process (optional, wrapper for worker)
4. `config/defaults.env` - Factory defaults
5. `scripts/install.sh` - Installation script
6. `scripts/dev.sh` - Development mode runner
7. `scripts/migrate.sh` - Migration orchestrator
8. `scripts/run_worker.sh` - Worker launcher (refactored from pc_run_all_worker.sh)
9. `scripts/webhook_ctl.sh` - Webhook service controller
10. `systemd/user/panamacompra.service` - Main service
11. `systemd/user/panamacompra-webhook.service` - Webhook service
12. `share/docs/TEMPLATE.md` - Template documentation
13. `share/docs/OPERATIONS.md` - Operations manual
14. `share/docs/MIGRATION.md` - Migration guide
15. `app/config.py` - Python configuration module
16. `VERSION` - Version file
17. `Makefile` - Build automation
18. `scripts/instantiate_template.sh` - Template parameterization

### Files to Move
1. `*.py` (17 files) → `app/`
2. `setup.sh` → `scripts/setup.sh` (rewrite)
3. `pc_*.sh` (20+ files) → `bin/` (legacy, deprecated) or `scripts/` (internal)
4. `docker/Dockerfile.webhook` → `docker/webhook/Dockerfile`
5. `docs/AGILE_PROCESS.md` → `share/docs/AGILE_PROCESS.md`

### Files to Modify
1. `.env.example` - Complete rewrite with comprehensive documentation
2. `.gitignore` - Add new paths, remove old
3. `README.md` - Update usage examples, add installation section
4. `docker-compose.yml` - Update volume paths
5. `requirements.txt` - Add new dependencies (if any)
6. `app/pc_common.py` - Path resolution refactor
7. `app/webhook_listener.py` - Add config import
8. `app/pc_monitor_tk.py` - Config path update
9. `app/pc_monitor_server.py` - Config path update
10. All `app/pc_*.py` - Import from `app/config.py`

### Files to Deprecate (Remove in v1.0)
1. Root-level `pc_*.sh` scripts (replaced by `bin/pcc`)
2. `pc_install_webhook_service.sh` (replaced by `scripts/install.sh --webhook`)
3. `pc_migrate_apps_layout.sh` (replaced by `scripts/migrate.sh --layout`)

---

**Document Prepared By:** Systems Architecture Analysis  
**Review Status:** Ready for Implementation Planning  
**Next Action:** Sprint planning meeting to prioritize Phase 1 tasks
