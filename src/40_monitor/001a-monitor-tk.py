#!/usr/bin/env python3
"""Lightweight native Tk monitor for PanamaCompra run-all progress.

This gives a real desktop window without starting Firefox or a local web server.
It uses only Python's standard library and refreshes on a timer.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import common as pc_common

# Shared KPI/data engine (audit Phase 4): the archive aggregates and detail
# helpers live in monitor_common.py so this monitor, the web monitor and
# `pcc kpi` always report the same numbers.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from monitor_common import (  # noqa: E402
    compact_dt,
    db_review_stats,
    finish_stamp_from_detail_json,
    finish_stamp_from_folder,
    load_detail_items_for_kpi,
    load_detail_payload_for_kpi,
    load_record_index,
    read_last_summary,
    summarize_items_for_kpi,
)

BASE_DIR = pc_common.APP_ROOT
CONFIG_DIR = pc_common.DATA_CONFIG_DIR

# Work-templates helper (src/50_tools/020-record-templates.py) imported as a module so
# the monitor lists/saves the same source folder and selection the CLI uses.
import importlib.util as _importlib_util

_rt_spec = _importlib_util.spec_from_file_location(
    "record_templates", str(pc_common.APP_ROOT / "src" / "50_tools" / "020-record-templates.py"))
record_templates = _importlib_util.module_from_spec(_rt_spec)
_rt_spec.loader.exec_module(record_templates)

_cal_spec = _importlib_util.spec_from_file_location(
    "opportunity_calendar", str(pc_common.APP_ROOT / "src" / "50_tools" / "030-opportunity-calendar.py"))
opportunity_calendar = _importlib_util.module_from_spec(_cal_spec)
_cal_spec.loader.exec_module(opportunity_calendar)

_nnr_spec = _importlib_util.spec_from_file_location(
    "notify_new_records", str(pc_common.APP_ROOT / "src" / "20_pipeline" / "020-notify-whatsapp.py"))
notify_formats = _importlib_util.module_from_spec(_nnr_spec)
_nnr_spec.loader.exec_module(notify_formats)
PROGRESS_FILE = pc_common.PROGRESS_PATH
WORKER_LOG = pc_common.LOG_DIR / "run_all_worker.log"
CURRENT_LOG = pc_common.LOG_DIR / "run_all_current.log"
REQUEST_FLAG = pc_common.QUEUE_DIR / "run_all_requested.flag"
UPDATE_QUEUE_FLAG = pc_common.QUEUE_DIR / "update_monitor_requested.flag"
UPDATE_IN_PROGRESS_FLAG = pc_common.QUEUE_DIR / "update_monitor_in_progress.flag"
REQUEST_LOG = pc_common.LOG_DIR / "run_all_requests.log"
UPDATE_QUEUE_LOG = pc_common.LOG_DIR / "update_monitor_queue.log"
WAHA_CHAT_ID_PATH = CONFIG_DIR / "waha_chat_id.txt"
# Optional per-purpose destinations; each falls back to the default chat id.
WAHA_CHAT_ID_INDEX_PATH = CONFIG_DIR / "waha_chat_id_index.txt"
WAHA_CHAT_ID_DETAILS_PATH = CONFIG_DIR / "waha_chat_id_details.txt"
WAHA_CHAT_ID_STATUS_PATH = CONFIG_DIR / "waha_chat_id_status.txt"
WAHA_CHAT_ID_SYSTEM_PATH = CONFIG_DIR / "waha_chat_id_system.txt"
WAHA_CHAT_ID_SUMMARY_PATH = CONFIG_DIR / "waha_chat_id_summary.txt"


def read_chat_file(path) -> str:
    return path.read_text(encoding="utf-8", errors="replace").strip() if path.exists() else ""
WAHA_KEYWORDS_PATH = CONFIG_DIR / "waha_keywords.txt"
MANUAL_ACTION_LOG = pc_common.LOG_DIR / "manual_actions.log"
# Archive database read (read-only) to populate the record-index selector with
# the collected opportunities (NUMERO + description + folder/link).
ARCHIVE_DB = pc_common.DB_PATH
# Editable settings the user can change from the monitor's Settings panel. Saved
# here as KEY=VALUE and consulted at startup (and by the WAHA notifier) so the
# choices survive restarts. Precedence everywhere is: real environment variable >
# this file > built-in default.
SETTINGS_PATH = CONFIG_DIR / "monitor_settings.env"


def load_settings_file() -> dict[str, str]:
    data: dict[str, str] = {}
    if not SETTINGS_PATH.exists():
        return data
    for line in SETTINGS_PATH.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        # Values are written shell-quoted (shlex.quote) so the worker can source
        # this file with `. file`; parse them back the same way so values with
        # spaces (e.g. the WhatsApp source label) round-trip correctly.
        try:
            parsed = shlex.split(raw_value, posix=True)
            data[key.strip()] = parsed[0] if parsed else ""
        except ValueError:
            data[key.strip()] = raw_value.strip().strip('"').strip("'")
    return data


_SETTINGS_FILE = load_settings_file()


def setting(name: str, default: str) -> str:
    """Resolve a setting: environment variable first, then the saved settings
    file, then the built-in default."""
    if name in os.environ:
        return os.environ[name]
    return _SETTINGS_FILE.get(name, default)


def monitor_env() -> dict[str, str]:
    env = os.environ.copy()
    env.update(load_settings_file())
    return env


def setting_int(name: str, default: int, minimum: int | None = None) -> int:
    try:
        value = int(float(setting(name, str(default))))
    except (TypeError, ValueError):
        value = default
    if minimum is not None:
        value = max(minimum, value)
    return value


def setting_float(name: str, default: float, low: float, high: float) -> float:
    try:
        value = float(setting(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return min(high, max(low, value))


REFRESH_SECONDS = setting_int("PC_MONITOR_TK_REFRESH_SECONDS", 3, minimum=2)
IDLE_REFRESH_SECONDS = max(REFRESH_SECONDS, setting_int("PC_MONITOR_TK_IDLE_REFRESH_SECONDS", 15))
# After a LIVE run finishes the monitor shows a centered countdown and then
# closes itself. Default is 20 seconds; set PC_MONITOR_TK_AUTO_CLOSE_SECONDS=0 to
# keep the window open until you close it manually. The countdown only applies to
# completed LIVE runs — test-zone runs and idle/manual states never auto-close
# (see auto_close_enabled in status_snapshot).
AUTO_CLOSE_SECONDS = setting_int("PC_MONITOR_TK_AUTO_CLOSE_SECONDS", 20, minimum=0)
# Window transparency. Tk only supports whole-window opacity, so text/buttons
# share it; 0.85 keeps the window clearly translucent while staying readable.
# Lower it (e.g. 0.50) from the Settings panel for a more see-through look.
# Clamped so the window can never become unreadable/invisible.
ALPHA = setting_float("PC_MONITOR_TK_ALPHA", 0.85, 0.30, 1.0)

class ManualAction(NamedTuple):
    zone: str
    label: str
    command: tuple[str, ...]
    comment: str
    open_after: Path | None = None


RECORDS_TEST_PARENT = pc_common.RECORDS_TEST_DIR


# Manual buttons grouped by zone. Each action's `comment` is shown as a hover
# tooltip on its button (not as an always-visible label), so the grid stays
# compact and readable. Zones are ordered by how often they are used:
# run → keep code fresh → rebuild data → test → open folders. Keep the most
# common/safe action first in each zone and destructive ones clearly labelled.
MANUAL_ACTIONS = [
    # --- 1. Collector Runners: start/stop the live collection ----------------
    ManualAction("Collector Runners", "Request full collection", ("./src/20_pipeline/110a-request-run.sh", "0", "RESTART", "0"), "Queues a manual restart run (all available index pages and unlimited detail pages) for the background worker. Safe default action."),
    ManualAction("Collector Runners", "Run collection now", ("./src/20_pipeline/110b-run-now.sh", "0", "0", "MANUAL"), "Starts the run-all worker immediately for all available index pages and unlimited detail pages (does not wait for the queue)."),
    ManualAction("Collector Runners", "Show run status", ("./src/20_pipeline/130b-run-status.sh",), "Writes a process/log status snapshot to the manual action log."),
    ManualAction("Collector Runners", "STOP all runners", ("./src/20_pipeline/120a-stop-everything.sh",), "DANGER: stops ALL processes — workers, test zone, calendar builder, monitors, webhook listener and updaters (this monitor closes too)."),

    # --- 2. Updater & Migration: keep code fresh, migrate old data -----------
    ManualAction("Updater & Migration", "Update local copy", ("./src/40_monitor/003-update-loader.py", "--open-monitor-after"), "Opens the centered updater window, refreshes the checkout/dependencies (auto-picks latest branch vs main), then reopens the monitor."),
    ManualAction("Updater & Migration", "Pre-run update only", ("./src/20_pipeline/000-update-before-run.sh",), "Runs the lightweight git/dependency refresh used before worker iterations (no browser install)."),
    ManualAction("Updater & Migration", "Upload local changes to GitHub", ("./bin/pcc", "upload-github"), "Commits local checkout changes and pushes the current branch to GitHub/origin. Use after local edits when you want the cloud repo updated before pulling elsewhere."),
    ManualAction("Updater & Migration", "Normalize folder names", ("./src/50_tools/070-rename-record-folders.py", "--apply"), "Normalizes existing record folder names using the current naming rules."),
    ManualAction("Updater & Migration", "Migrate old records", ("./src/50_tools/090a-migrate-previous-records.sh",), "Imports/migrates previous record archives into the current layout."),

    # --- 3. Data Tools: rebuild views/calendars and integrations -------------
    ManualAction("Data Tools", "Rebuild detail views", ("./src/20_pipeline/040-build-detail-views.py", "--apply"), "Rebuilds saved record views, ICS files and split tables from stored data (no browser)."),
    ManualAction("Data Tools", "Repair missing deadlines", ("./src/20_pipeline/050-repair-missing-deadlines.py", "--apply"), "Finds records/folders missing DTEND/deadline, force re-downloads their details, and renames folders when a deadline is recovered."),
    ManualAction("Data Tools", "Rebuild calendar packages", ("./src/20_pipeline/060-build-calendar.py", "--all"), "Rebuilds the calendar import packages (.ics) for all dated record folders."),
    ManualAction("Data Tools", "Import calendars to app", ("bash", "-lc", "PC_CALENDAR_AUTO_IMPORT=1 ./src/20_pipeline/060-build-calendar.py --all"), "Rebuilds all packages and opens each .ics with the desktop calendar app."),
    ManualAction("Data Tools", "Apply work templates", ("./src/50_tools/020-record-templates.py", "apply", "--apply"), "Copies the selected template files into templates/ inside every saved record folder. Files already present in a record are kept untouched."),
    ManualAction("Data Tools", "Start webhook listener", ("./src/10_webhook/020-start-listener.sh", "--replace-port-owner"), "Starts/restarts the local webhook listener in the background; use STOP all runners to halt it."),
    ManualAction("Data Tools", "Install webhook service", ("./src/10_webhook/030-install-service.sh",), "Installs/repairs the persistent user systemd webhook service using the safe foreground starter."),
    ManualAction("Data Tools", "Open web monitor", ("./src/50_tools/130-open-web-app.sh", "monitor"), "Starts the optional web monitor server if needed and opens it in a chromeless app window (no Firefox needed; falls back to the default browser)."),

    # --- 4. Integrations: changedetection + WAHA Docker containers -----------
    ManualAction("Integrations (Docker)", "Start/refresh docker stack", ("./src/50_tools/010-docker-stack.sh", "up"), "Pulls/starts (or refreshes) the changedetection + WAHA + webhook containers. Their data stays inside the self-contained var/integrations folder."),
    ManualAction("Integrations (Docker)", "Docker stack status", ("./src/50_tools/010-docker-stack.sh", "status"), "Writes the container states plus the changedetection/WAHA URLs to the manual action log."),
    ManualAction("Integrations (Docker)", "Restart docker stack", ("./src/50_tools/010-docker-stack.sh", "restart"), "Stops and starts the containers, applying the container settings saved from this panel (changedetection URL, WAHA port/API key)."),
    ManualAction("Integrations (Docker)", "Stop docker stack", ("./src/50_tools/010-docker-stack.sh", "down"), "Stops and removes the changedetection/WAHA/webhook containers; their data stays in var/integrations."),
    ManualAction("Integrations (Docker)", "Open changedetection UI", ("./src/50_tools/130-open-web-app.sh", "changedetection"), "Opens the changedetection.io interface in a chromeless app window (no Firefox needed; falls back to the default browser) to configure the PanamaCompra watch and its trigger/webhook URL."),
    ManualAction("Integrations (Docker)", "Print changedetection JS setup", ("./bin/pcc", "changedetection-script"), "Writes the Browser Steps Execute JS instructions/script for Programadas + Abiertas pagination to data/logs/manual_actions.log so you can copy it into changedetection."),
    ManualAction("Integrations (Docker)", "Open WAHA dashboard", ("./src/50_tools/130-open-web-app.sh", "waha"), "Opens the WAHA dashboard in a chromeless app window (no Firefox needed) to pair the WhatsApp session by QR. Login: admin + the password from data/config/integration-access.txt (see data/config/integration-access.txt)."),

    # --- 5. Testing & Validation: sandbox runs and health checks -------------
    ManualAction("Testing & Validation", "Run test zone", ("./src/20_pipeline/070-test-zone.py", "--limit", "5", "--apply"), "Re-runs the latest 5 records in the isolated sandbox (records_test/); the real archive is left untouched.", RECORDS_TEST_PARENT),
    ManualAction("Testing & Validation", "Review system health", ("./review-system.sh",), "Runs the repository health checks and troubleshooting summary; on completion WAHA sends a System health message to the system destination (override with pcc health --chat-id/--purpose)."),
    ManualAction("Testing & Validation", "Full diagnostic report", ("./bin/pcc", "full-report"), "Creates a complete Markdown diagnostic report covering paths, settings, tools, integrations, queues, database counters, processes and recent logs."),

    # --- 6. Folder Management: open data storage locations -------------------
    ManualAction("Folder Management", "Open index folder", ("bash", "-c", f"xdg-open {shlex.quote(str(pc_common.DATA_DIR / 'index'))}"), "Opens the main index folder where collected records are stored."),
    ManualAction("Folder Management", "Open records folder", ("bash", "-c", f"xdg-open {shlex.quote(str(pc_common.RECORDS_DIR))}"), "Opens the records archive folder containing organized record subfolders."),
    ManualAction("Folder Management", "Open logs folder", ("bash", "-c", f"xdg-open {shlex.quote(str(pc_common.LOG_DIR))}"), "Opens the logs folder containing worker and action logs."),
    ManualAction("Folder Management", "Open data root", ("bash", "-c", f"xdg-open {shlex.quote(str(pc_common.DATA_DIR))}"), "Opens the main data directory containing index, logs, queue and config."),
    ManualAction("Folder Management", "Open index parent folder", ("bash", "-c", f"xdg-open {shlex.quote(str(pc_common.DATA_DIR))}"), "Opens the parent directory that contains the index folder."),
]


DEFAULT_PROGRESS = {
    "PHASE": "IDLE",
    "STATUS": "DONE",
    "PERCENT": "100",
    "MESSAGE": "No active process.",
    "INDEX_LIMIT": "-",
    "ETA": "-",
    "DETAIL_LIMIT": "-",
    "STARTED_AT": "",
    "UPDATED_AT": "-",
    "WORKER_PID": "-",
    "MODE": "IDLE",
    "STEP_CURRENT": "-",
    "STEP_TOTAL": "-",
    "ITEM_CURRENT": "-",
    "ITEM_TOTAL": "-",
    "RECORDS_FOUND": "-",
    "RECORDS_NEW": "-",
    "RECORDS_EXISTING": "-",
    "RECORDS_SAVED": "-",
    "RECORDS_FAILED": "-",
    "RECORDS_PENDING": "-",
    "RECORDS_TEST": "-",
    "EXTRA": "-",
}


def parse_progress_file() -> dict[str, str]:
    data = DEFAULT_PROGRESS.copy()
    if not PROGRESS_FILE.exists():
        return data

    for line in PROGRESS_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if key not in data:
            continue
        try:
            parsed = shlex.split(raw_value, posix=True)
            data[key] = parsed[0] if parsed else ""
        except ValueError:
            data[key] = raw_value.strip().strip("'").strip('"')
    return data


def tail(path: Path, lines: int) -> str:
    if not path.exists():
        return f"No {path.name} yet."
    content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(content[-lines:])


# How many days ahead still counts as "next to expire" (amber) instead of a calm
# "upcoming" (green). Records past their DTEND are "expired" (red).
SOON_DAYS = setting_int("PC_MONITOR_DEADLINE_SOON_DAYS", 7, minimum=1)
STATUS_COLORS = {"expired": "#fca5a5", "soon": "#fcd34d", "upcoming": "#86efac", "unknown": "#94a3b8"}
STATUS_TAGS = {"expired": "EXPIRED", "soon": "SOON", "upcoming": "ok", "unknown": "no date"}
# Friendly labels for the status selector, mapped back to the internal keys.
STATUS_FILTER_CHOICES = ("All", "Next to expire", "Expired", "Upcoming", "No date / needs repair")
STATUS_FILTER_KEYS = {"Next to expire": "soon", "Expired": "expired", "Upcoming": "upcoming", "No date / needs repair": "unknown"}
DETAIL_STATUS_FILTER_CHOICES = ("All", "Pending records", "Completed records", "Failed records")
DETAIL_STATUS_FILTER_KEYS = {"Pending records": "pending", "Completed records": "saved", "Failed records": "failed"}


def parse_deadline(rec: dict[str, str]) -> datetime | None:
    """The record's DTEND/deadline (finish_date_guess 'YYYY-MM-DD_HH:MM'), or None."""
    raw = (rec.get("finish_date_guess") or "").strip().replace("T", " ").replace("_", " ")
    if not raw:
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def deadline_text(rec: dict[str, str]) -> str:
    return compact_dt(rec.get("finish_date_guess") or "") or "—"


def start_text(rec: dict[str, str]) -> str:
    return compact_dt(rec.get("start_date_guess") or "") or "—"


def downloaded_text(rec: dict[str, str]) -> str:
    return compact_dt(rec.get("detail_saved_at") or "") or "—"

def parse_downloaded(rec: dict[str, str]) -> datetime | None:
    raw = (rec.get("detail_saved_at") or "").strip().replace("T", " ")
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})(?:[ _T](\d{2}):(\d{2}))?", raw)
    if not match:
        return None
    return datetime(
        int(match.group(1)),
        int(match.group(2)),
        int(match.group(3)),
        int(match.group(4) or 0),
        int(match.group(5) or 0),
    )


def parse_record_order_date(rec: dict[str, str], field: str = "Downloaded date") -> datetime | None:
    """Timestamp used by the record selector ordering controls."""
    if field == "End date":
        return parse_deadline(rec)
    if field == "Start date":
        return parse_start(rec)
    return parse_downloaded(rec) or _parse_iso_like(rec.get("first_seen") or "")


def _parse_iso_like(raw: str) -> datetime | None:
    text = (raw or "").strip().replace("T", " ").replace("_", " ")
    for candidate in (text, text[:19], text[:16], text[:10]):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(candidate, fmt)
            except ValueError:
                pass
    return None


def parse_start(rec: dict[str, str]) -> datetime | None:
    """The record's DTSTART/start (start_date_guess), or None."""
    raw = (rec.get("start_date_guess") or "").strip().replace("T", " ").replace("_", " ")
    if not raw:
        return None
    for candidate in (raw, raw[:19], raw[:16], raw[:10]):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(candidate, fmt)
            except ValueError:
                continue
    return None


def parse_filter_bound(text: str, *, upper: bool = False) -> datetime | None:
    """Parse a user-typed date or date+time filter bound.

    Accepts 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM' (HH:MM:SS tolerated). A bare date
    used as an upper bound covers the whole day (23:59:59) so 'on/before'
    includes that day; as a lower bound it starts at 00:00. Returns None for
    blank/unparseable input so the bound simply does not constrain the list."""
    text = (text or "").strip().replace("T", " ").replace("_", " ")
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        day = datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return None
    return day.replace(hour=23, minute=59, second=59) if upper else day


def expiry_status(rec: dict[str, str], now: datetime | None = None) -> str:
    dt = parse_deadline(rec)
    if dt is None:
        return "unknown"
    now = now or datetime.now()
    if dt < now:
        return "expired"
    if dt <= now + timedelta(days=SOON_DAYS):
        return "soon"
    return "upcoming"


def running(pattern: str) -> bool:
    return subprocess.run(["pgrep", "-f", pattern], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def webhook_running() -> bool:
    if running("[s]rc/10_webhook/010-webhook-listener.py") or running("[p]ython3? -u .*src/10_webhook/010-webhook-listener.py"):
        return True
    try:
        result = subprocess.run(["docker", "compose", "ps", "--status", "running", "webhook"], cwd=BASE_DIR, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=3)
        return "webhook" in result.stdout.lower()
    except Exception:
        return False

def webhook_access_text() -> str:
    """Live view of the webhook trigger access: the token generated by setup
    (docker-stack `ensure_access_credentials` writes `.webhook_token`) plus the
    exact URLs changedetection / external tools must use. Read fresh on every
    call, so the monitor always shows the CURRENT settings even right after an
    update or a re-run of setup."""
    token_path = BASE_DIR / ".webhook_token"
    try:
        token = token_path.read_text(encoding="utf-8").strip() if token_path.exists() else ""
    except OSError:
        token = ""
    settings_file = load_settings_file()
    port = str(os.environ.get("PC_WEBHOOK_PORT") or settings_file.get("PC_WEBHOOK_PORT") or "8765")
    public_host = str(os.environ.get("PC_WEBHOOK_PUBLIC_HOST") or settings_file.get("PC_WEBHOOK_PUBLIC_HOST") or "host.docker.internal")
    shown = token or "YOUR_TOKEN"
    query = "?method=POST&format=text&overflow=truncate&rto=15&cto=10"
    return (
        f"Webhook token: {token if token else '(not generated yet — run ./setup.sh or ./src/50_tools/010-docker-stack.sh up)'}\n"
        f"Token file:    {token_path}\n"
        f"Listener:      {'RUNNING' if webhook_running() else 'off'} on port {port}\n\n"
        "changedetection notification URL (Docker → host, recommended):\n"
        f"  json://{public_host}:{port}/panamacompra/{shown}{query}\n"
        "Compose-only URL (use only if changedetection can resolve host 'webhook'):\n"
        f"  json://webhook:8765/panamacompra/{shown}{query}\n"
        "Docker container → host listener URL (plain HTTP test):\n"
        f"  http://{public_host}:{port}/panamacompra/{shown}\n"
        "Local test from this machine:\n"
        f"  http://127.0.0.1:{port}/panamacompra/{shown}\n\n"
        "The token is generated AUTOMATICALLY by setup (docker stack up creates\n"
        ".webhook_token when missing); values here are re-read live, so after an\n"
        "update or a re-run of setup this panel always shows the current settings.\n"
        f"Full access note (incl. WAHA login/API key): {CONFIG_DIR / 'integration-access.txt'}"
    )


def process_snapshot() -> dict[str, bool]:
    """Detect all running PanamaCompra processes for the monitor display."""
    worker = running("[r]un-worker.sh")
    test = running("[p]ython(3)? -u .*070-test-zone.py")
    updater = running("[u]pdate-local-copy.sh") or running("[u]pdate-loader.py")
    webhook = webhook_running()
    monitor_tk = running("[0]01a-monitor-tk.py") or running("[p]ython3? -u .*001a-monitor-tk.py")
    monitor_server = running("[0]01b-monitor-web.py") or running("[p]ython3? -u .*001b-monitor-web.py")
    timer = running("[0]02-next-run-timer.py")

    return {
        "normal_run": worker and not test,
        "test_run": test,
        "worker": worker,
        "index": running("[p]ython(3)? -u .*010-collect-index.py"),
        "detail": running("[p]ython(3)? -u .*030-collect-details.py"),
        "calendar": running("[p]ython(3)? -u .*(040-build-detail-views|060-build-calendar).py"),
        # WhatsApp MESSAGING step: visible while the notifier sends messages.
        "messaging": running("[0]20-notify-whatsapp.py"),
        "request": REQUEST_FLAG.exists(),
        # Additional runners that should be stopped by src/20_pipeline/120a-stop-everything.sh
        "updater": updater,
        "webhook": webhook,
        "monitor_tk": monitor_tk,
        "monitor_server": monitor_server,
        "timer": timer,
    }


def file_timestamp(path: Path) -> str:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d_%H-%M")
    except OSError:
        return "-"


def queue_snapshot() -> dict[str, str]:
    """Current queued collector/update requests for the monitor queue panel."""
    collector_pending = REQUEST_FLAG.exists()
    update_pending = UPDATE_QUEUE_FLAG.exists()
    update_running = UPDATE_IN_PROGRESS_FLAG.exists()
    if update_running:
        update_state = "RUNNING"
        update_since = file_timestamp(UPDATE_IN_PROGRESS_FLAG)
    elif update_pending:
        update_state = "PENDING"
        update_since = file_timestamp(UPDATE_QUEUE_FLAG)
    else:
        update_state = "none"
        update_since = "-"
    return {
        "collector_state": "PENDING" if collector_pending else "none",
        "collector_since": file_timestamp(REQUEST_FLAG) if collector_pending else "-",
        "update_state": update_state,
        "update_since": update_since,
        "request_log": tail(REQUEST_LOG, 8),
        "update_log": tail(UPDATE_QUEUE_LOG, 8),
    }


def percent_value(progress: dict[str, str]) -> int:
    try:
        return max(0, min(100, int(progress.get("PERCENT", "0"))))
    except ValueError:
        return 0


# Processes that represent actual collection/maintenance WORK. The "done" state
# (and the auto-close countdown) must only depend on these. The monitor window
# itself (monitor_tk), the optional web monitor, the always-on next-run timer and
# the passive webhook listener must NOT count — otherwise the monitor detects
# ITSELF as running and "done" is never reached, so the finish countdown never
# appears.
WORK_PROCESS_KEYS = ("worker", "index", "detail", "calendar", "messaging", "test_run", "updater", "request")


def progress_stale(processes: dict[str, bool], progress: dict[str, str]) -> bool:
    if any(processes.get(key) for key in WORK_PROCESS_KEYS):
        return False
    if progress.get("STATUS") != "RUNNING":
        return False
    try:
        updated = datetime.strptime(progress.get("UPDATED_AT", ""), "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return True
    return (datetime.now() - updated).total_seconds() > int(setting("PC_MONITOR_STALE_SECONDS", "120"))


def is_done(processes: dict[str, bool], progress: dict[str, str]) -> bool:
    if any(processes.get(key) for key in WORK_PROCESS_KEYS):
        return False
    return progress_stale(processes, progress) or progress.get("STATUS") in {"DONE", "FAILED", "TIMEOUT", "STALE"} or progress.get("PHASE") in {"DONE", "IDLE"}


def status_snapshot() -> dict[str, object]:
    progress = parse_progress_file()
    processes = process_snapshot()
    if progress_stale(processes, progress):
        progress = progress.copy()
        progress["STATUS"] = "STALE"
        progress["MESSAGE"] = "Previous run appears stopped abruptly; controls are unlocked. Request restart/manual/test to continue."
    done = is_done(processes, progress)
    return {
        "progress": progress,
        "percent": percent_value(progress),
        "processes": processes,
        "queue": queue_snapshot(),
        "done": done,
        # Auto-close only the unattended automatic (changedetection/webhook) run.
        # RESTART/MANUAL/TEST are operator-initiated, so the window stays open.
        "auto_close_enabled": done and progress.get("MODE", "IDLE").upper() == "AUTO" and not processes.get("test_run", False),
        "refresh_seconds": IDLE_REFRESH_SECONDS if done else REFRESH_SECONDS,
        "auto_close_seconds": AUTO_CLOSE_SECONDS,
        "worker_log": tail(WORKER_LOG, 10),
        "current_log": tail(CURRENT_LOG, 14),
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "waha_chat_id": read_chat_file(WAHA_CHAT_ID_PATH),
        "waha_chat_id_index": read_chat_file(WAHA_CHAT_ID_INDEX_PATH),
        "waha_chat_id_details": read_chat_file(WAHA_CHAT_ID_DETAILS_PATH),
        "waha_chat_id_status": read_chat_file(WAHA_CHAT_ID_STATUS_PATH),
        "waha_chat_id_system": read_chat_file(WAHA_CHAT_ID_SYSTEM_PATH),
        "waha_chat_id_summary": read_chat_file(WAHA_CHAT_ID_SUMMARY_PATH),
    }


def run_tk() -> int:
    try:
        import tkinter as tk
        from tkinter import messagebox, ttk
    except Exception as exc:  # pragma: no cover - depends on host packages
        print(f"ERROR: Tkinter is not available: {exc}", file=sys.stderr)
        return 2

    root = tk.Tk()
    root.title("PanamaCompra Progress")
    geometry = setting("PC_MONITOR_TK_GEOMETRY", "980x760")
    root.geometry(geometry)
    root.configure(bg="#0f172a")

    # Runtime settings the Settings panel can change live without a restart.
    runtime = {
        "alpha": ALPHA,
        "auto_close": AUTO_CLOSE_SECONDS,
        "refresh": REFRESH_SECONDS,
        "idle_refresh": IDLE_REFRESH_SECONDS,
    }

    def apply_alpha(value: float) -> None:
        try:
            root.attributes("-alpha", value)
        except tk.TclError:
            pass

    apply_alpha(runtime["alpha"])

    # Simple hover tooltip: a small borderless popup shown under a widget while
    # the pointer is over it. Used to explain every manual button on hover.
    class Tooltip:
        def __init__(self, widget: tk.Widget, text: str) -> None:
            self.widget = widget
            self.text = text
            self.tip: tk.Toplevel | None = None
            widget.bind("<Enter>", self.show, add="+")
            widget.bind("<Leave>", self.hide, add="+")
            widget.bind("<ButtonPress>", self.hide, add="+")

        def show(self, _event: tk.Event | None = None) -> None:
            if self.tip is not None or not self.text:
                return
            try:
                x = self.widget.winfo_rootx() + 18
                y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
            except tk.TclError:
                return
            self.tip = tk.Toplevel(self.widget)
            self.tip.wm_overrideredirect(True)
            self.tip.wm_geometry(f"+{x}+{y}")
            try:
                self.tip.attributes("-topmost", True)
            except tk.TclError:
                pass
            tk.Label(
                self.tip,
                text=self.text,
                justify="left",
                background="#1f2937",
                foreground="#f8fafc",
                relief="solid",
                borderwidth=1,
                wraplength=380,
                padx=8,
                pady=6,
                font=("Sans", 9),
            ).pack()

        def hide(self, _event: tk.Event | None = None) -> None:
            if self.tip is not None:
                self.tip.destroy()
                self.tip = None

    def add_tooltip(widget: tk.Widget, text: str) -> None:
        Tooltip(widget, text)

    def add_section_header(frame: ttk.Frame, name: str, desc: str, *, columnspan: int, row: int = 0,
                           detail: str = "") -> None:
        box = ttk.Frame(frame, style="Card.TFrame")
        box.grid(row=row, column=0, columnspan=columnspan, sticky="w", pady=(0, 8))
        ttk.Label(box, text=name, style="Title.TLabel").pack(anchor="w")
        if desc:
            ttk.Label(box, text=desc, style="SectionDesc.TLabel").pack(anchor="w")
        if detail:
            ttk.Label(box, text=detail, style="SectionDetail.TLabel").pack(anchor="w")

    def add_section_toggle(frame: ttk.Frame, *, button_column: int, title_row: int = 0,
                           start_hidden: bool = True) -> None:
        """Add a hide/show button that keeps the section header visible.

        The set of content widgets to collapse is captured ONCE, now, while every
        widget is still gridded. The previous version re-read ``grid_info`` inside
        the toggle, but ``grid_remove`` makes ``grid_info`` return ``{}`` for a
        hidden widget, so after the first Hide the Show pass skipped every
        (now-empty-info) child and nothing ever came back — the button looked
        dead. Capturing up front fixes that and lets sections start collapsed.
        """
        hidden = tk.BooleanVar(value=False)

        button = ttk.Button(frame, text="Hide", width=7)
        button.grid(row=title_row, column=button_column, sticky="e", padx=(8, 0), pady=(0, 8))
        add_tooltip(button, "Hide/show this monitor section without stopping the run.")

        content_children = []
        content_grid_infos = []
        for child in frame.winfo_children():
            if child is button:
                continue
            info = child.grid_info()
            if not info:
                continue
            try:
                row = int(info.get("row", 0))
            except (TypeError, ValueError):
                row = 0
            if row <= title_row:
                continue
            content_children.append(child)
            content_grid_infos.append(info)

        def apply_hidden(is_hidden: bool) -> None:
            hidden.set(is_hidden)
            for child, info in zip(content_children, content_grid_infos):
                if is_hidden:
                    child.grid_remove()
                else:
                    child.grid(**info)
            button.configure(text="Show" if is_hidden else "Hide")
            # grid_remove()/grid() change content's required size, but the canvas
            # scrollregion is only recomputed on content's <Configure> event, which
            # does not reliably fire here (row 1 has weight=1, so content keeps
            # stretching to the canvas height regardless of how much is actually
            # visible). Without this, hiding a section leaves the old, larger
            # scrollregion in place: you can keep scrolling into blank space where
            # the collapsed content used to be. Recompute explicitly, after idle so
            # geometry management has settled.
            root.after_idle(update_scroll_region)

        button.configure(command=lambda: apply_hidden(not hidden.get()))
        # Sections start collapsed by default so the monitor opens compact; the
        # operator expands only the panels they need.
        if start_hidden:
            apply_hidden(True)

    def center_window() -> None:
        root.update_idletasks()
        width = root.winfo_width()
        height = root.winfo_height()
        if width <= 1 or height <= 1:
            width, height = [int(part) for part in geometry.split("x", 1)]
        x = max(0, (root.winfo_screenwidth() - width) // 2)
        y = max(0, (root.winfo_screenheight() - height) // 2)
        root.geometry(f"{width}x{height}+{x}+{y}")

    center_window()

    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    style.configure("TFrame", background="#0f172a")
    style.configure("Card.TFrame", background="#111827", relief="solid", borderwidth=1)

    style.configure("TLabel", background="#0f172a", foreground="#e5e7eb")
    style.configure("Card.TLabel", background="#111827", foreground="#e5e7eb")
    style.configure("Title.TLabel", background="#111827", foreground="#e5e7eb", font=("Sans", 16, "bold"))
    style.configure("SectionDesc.TLabel", background="#111827", foreground="#94a3b8", font=("Sans", 9))
    style.configure("SectionDetail.TLabel", background="#111827", foreground="#64748b", font=("Sans", 8))
    style.configure("Message.TLabel", background="#111827", foreground="#fef3c7", font=("Sans", 11, "bold"))
    style.configure("Done.TLabel", background="#111827", foreground="#bbf7d0", font=("Sans", 10, "bold"))
    style.configure("Horizontal.TProgressbar", thickness=26)
    # Flat, rounded-feeling buttons with clear primary/danger variants and a
    # readable disabled state, plus matching scrollbars, radios and entries so
    # the whole window shares one cohesive dark style.
    style.configure("TButton", padding=(12, 6), relief="flat", borderwidth=0,
                    background="#334155", foreground="#e5e7eb", font=("Sans", 10))
    style.map("TButton",
              background=[("active", "#475569"), ("disabled", "#1f2937")],
              foreground=[("disabled", "#6b7280")])
    style.configure("Accent.TButton", background="#2563eb", foreground="#ffffff", font=("Sans", 10, "bold"))
    style.map("Accent.TButton",
              background=[("active", "#1d4ed8"), ("disabled", "#1e293b")],
              foreground=[("disabled", "#6b7280")])
    style.configure("Danger.TButton", background="#dc2626", foreground="#ffffff", font=("Sans", 10, "bold"))
    style.map("Danger.TButton",
              background=[("active", "#b91c1c"), ("disabled", "#3f1d1d")],
              foreground=[("disabled", "#9ca3af")])
    style.configure("Vertical.TScrollbar", background="#334155", troughcolor="#0f172a",
                    arrowcolor="#94a3b8", borderwidth=0, relief="flat")
    style.map("Vertical.TScrollbar", background=[("active", "#475569")])
    style.configure("Card.TRadiobutton", background="#111827", foreground="#e5e7eb", font=("Sans", 10))
    style.configure("Card.TCheckbutton", background="#111827", foreground="#e5e7eb", font=("Sans", 10))
    style.map("Card.TRadiobutton",
              background=[("active", "#111827")],
              foreground=[("disabled", "#6b7280"), ("selected", "#93c5fd")])
    style.map("Card.TCheckbutton",
              background=[("active", "#111827")],
              foreground=[("disabled", "#6b7280"), ("selected", "#93c5fd")])
    style.configure("TEntry", fieldbackground="#020617", foreground="#e5e7eb",
                    bordercolor="#475569", insertcolor="#e5e7eb")
    style.map("TEntry", fieldbackground=[("readonly", "#0b1220")], foreground=[("readonly", "#e5e7eb")])
    # Unified tab bar shared with the web monitor's look: dark chips, blue active.
    style.configure("TNotebook", background="#0f172a", borderwidth=0)
    style.configure("TNotebook.Tab", background="#1f2937", foreground="#cbd5e1",
                    padding=(14, 7), font=("Sans", 10, "bold"))
    style.map("TNotebook.Tab",
              background=[("selected", "#2563eb")],
              foreground=[("selected", "#ffffff")])

    root.columnconfigure(0, weight=1)
    root.rowconfigure(0, weight=1)

    canvas = tk.Canvas(root, bg="#0f172a", highlightthickness=0)
    scrollbar = ttk.Scrollbar(root, orient="vertical", command=canvas.yview)
    canvas.configure(yscrollcommand=scrollbar.set)
    canvas.grid(row=0, column=0, sticky="nsew")
    scrollbar.grid(row=0, column=1, sticky="ns")

    content = ttk.Frame(canvas, style="TFrame")
    content_window = canvas.create_window((0, 0), window=content, anchor="nw")
    content.columnconfigure(0, weight=1)
    # Row 0 holds the live header; row 1 holds the unified tab notebook, which
    # absorbs the extra vertical space.
    content.rowconfigure(1, weight=1)

    def update_scroll_region(_event: tk.Event | None = None) -> None:
        canvas.update_idletasks()
        canvas.configure(scrollregion=canvas.bbox("all"))

    def resize_content(event: tk.Event) -> None:
        canvas.itemconfigure(content_window, width=event.width)
        root.after_idle(update_scroll_region)

    def on_mousewheel(event: tk.Event) -> None:
        # X11 (Linux) delivers wheel events as Button-4 (up) / Button-5 (down)
        # with no usable event.delta, so those events never scrolled the window.
        # Windows/macOS deliver <MouseWheel> with a signed event.delta instead.
        num = getattr(event, "num", 0)
        if num == 4:
            step = -3
        elif num == 5:
            step = 3
        elif event.delta:
            step = int(-1 * (event.delta / 120)) * 3
        else:
            return
        # If the pointer is over an inner Listbox (the record browser), scroll
        # that list itself and stop — otherwise the list scroll and the whole-page
        # scroll fight each other and are impossible to separate.
        widget = getattr(event, "widget", None)
        # Over the record list or a log pane, scroll that widget itself so its own
        # scrollbar moves instead of the whole page fighting with it.
        if isinstance(widget, (tk.Listbox, tk.Text)):
            widget.yview_scroll(step, "units")
            return "break"
        canvas.yview_scroll(step, "units")

    content.bind("<Configure>", update_scroll_region)
    canvas.bind("<Configure>", resize_content)
    # Bind on all widgets so the wheel scrolls the page no matter where the
    # pointer is. <MouseWheel> covers Windows/macOS; Button-4/5 cover X11/Linux.
    canvas.bind_all("<MouseWheel>", on_mousewheel)
    canvas.bind_all("<Button-4>", on_mousewheel)
    canvas.bind_all("<Button-5>", on_mousewheel)

    _wraplabels: list[ttk.Label] = []

    def _track_wraplabel(label: ttk.Label) -> ttk.Label:
        _wraplabels.append(label)
        return label

    def _reflow_wraplabels() -> None:
        width = canvas.winfo_width()
        if width < 200:
            width = 820
        for label in _wraplabels:
            try:
                label.configure(wraplength=width - 80)
            except tk.TclError:
                pass

    root.bind("<Configure>", lambda _e: root.after_idle(update_scroll_region), add="+")
    canvas.bind("<Configure>", lambda _e: root.after_idle(_reflow_wraplabels), add="+")

    header = ttk.Frame(content, style="Card.TFrame", padding=14)
    header.grid(row=0, column=0, sticky="ew", padx=14, pady=(14, 8))
    header.columnconfigure(0, weight=1)

    title = ttk.Label(header, text="PanamaCompra Progress Monitor", style="Title.TLabel")
    title.grid(row=0, column=0, sticky="w")
    meta_var = tk.StringVar(value="Loading...")
    ttk.Label(header, textvariable=meta_var, style="Card.TLabel").grid(row=1, column=0, sticky="w", pady=(4, 8))

    progress_var = tk.IntVar(value=0)
    bar = ttk.Progressbar(header, maximum=100, variable=progress_var, style="Horizontal.TProgressbar")
    bar.grid(row=2, column=0, sticky="ew")
    message_var = tk.StringVar(value="Loading...")
    ttk.Label(header, textvariable=message_var, style="Message.TLabel", wraplength=900).grid(row=3, column=0, sticky="w", pady=(8, 4))
    done_var = tk.StringVar(value="")
    ttk.Label(header, textvariable=done_var, style="Done.TLabel").grid(row=4, column=0, sticky="w")
    # Process status used to be one long wrapped line ("normal_run: off  test_run:
    # off  …") that crowded into 2–3 dense rows. It is now a tidy grid of small
    # colored chips (green = RUNNING, gray = off) laid out in fixed columns.
    ttk.Label(header, text="Processes (off is normal when a step is idle; detail only runs during STEP 3)", style="Card.TLabel").grid(row=5, column=0, sticky="w", pady=(8, 2))
    process_frame = ttk.Frame(header, style="Card.TFrame")
    process_frame.grid(row=6, column=0, sticky="ew")
    process_chips: dict[str, tk.Label] = {}
    PROCESS_CHIP_COLUMNS = 5
    for col in range(PROCESS_CHIP_COLUMNS):
        process_frame.columnconfigure(col, weight=1, uniform="proc")

    def update_process_chips(processes: dict[str, bool]) -> None:
        for idx, (name, value) in enumerate(processes.items()):
            chip = process_chips.get(name)
            if chip is None:
                chip = tk.Label(process_frame, anchor="w", padx=8, pady=2,
                                font=("Sans", 8, "bold"), borderwidth=0)
                chip.grid(row=idx // PROCESS_CHIP_COLUMNS,
                          column=idx % PROCESS_CHIP_COLUMNS,
                          sticky="ew", padx=2, pady=2)
                process_chips[name] = chip
            chip.configure(
                text=f"{name}: {'RUNNING' if value else 'off'}",
                bg="#14532d" if value else "#1f2937",
                fg="#bbf7d0" if value else "#9ca3af",
            )

    queue_frame = ttk.Frame(header, style="Card.TFrame", padding=(0, 8, 0, 0))
    queue_frame.grid(row=7, column=0, sticky="ew")
    queue_frame.columnconfigure(1, weight=1)
    add_section_header(queue_frame, "Queue process", "Pending collector / update requests and their recent queue logs.", columnspan=2)
    queue_summary_var = tk.StringVar(value="Collector queue: none · Update + Monitor queue: none")
    _track_wraplabel(ttk.Label(queue_frame, textvariable=queue_summary_var, style="Card.TLabel", wraplength=680)).grid(row=1, column=0, columnspan=2, sticky="ew")
    queue_log_text = tk.Text(queue_frame, height=5, wrap="word", bd=0, highlightthickness=0,
                             bg="#020617", fg="#cbd5e1", insertbackground="#e5e7eb", font=("Sans", 8))
    queue_log_text.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))
    queue_log_text.configure(state="disabled")

    def update_queue_panel(queue: dict[str, str]) -> None:
        collector = queue.get("collector_state", "none")
        collector_since = queue.get("collector_since", "-")
        update = queue.get("update_state", "none")
        update_since = queue.get("update_since", "-")
        queue_summary_var.set(
            f"Collector request: {collector} (since {collector_since}) · "
            f"Update + Monitor: {update} (since {update_since})"
        )
        log_text = (
            "Recent collector queue log:\n" + (queue.get("request_log") or "(missing)") +
            "\nRecent Update + Monitor queue log:\n" + (queue.get("update_log") or "(missing)")
        )
        set_text(queue_log_text, log_text)

    # ========================================================================
    # UNIFIED TABS - Operations · Settings · WhatsApp · Scheduler · KPIs ·
    # Records & Database · Calendar. The live header (progress, process chips,
    # queue) stays visible above the tab bar.
    # ========================================================================
    tabs = ttk.Notebook(content)
    tabs.grid(row=1, column=0, sticky="nsew", padx=14, pady=8)
    ops_tab = ttk.Frame(tabs, style="TFrame")
    settings_tab = ttk.Frame(tabs, style="TFrame")
    whatsapp_tab = ttk.Frame(tabs, style="TFrame")
    scheduler_tab = ttk.Frame(tabs, style="TFrame")
    kpi_tab = ttk.Frame(tabs, style="TFrame")
    records_tab = ttk.Frame(tabs, style="TFrame")
    calendar_tab = ttk.Frame(tabs, style="TFrame")
    for tab_frame, tab_title in (
        (records_tab, "Records & Database"),
        (calendar_tab, "Calendar"),
        (ops_tab, "Operations"),
        (kpi_tab, "KPIs"),
        (scheduler_tab, "Scheduler"),
        (whatsapp_tab, "WhatsApp"),
        (settings_tab, "Settings"),
    ):
        tabs.add(tab_frame, text=tab_title)
        tab_frame.columnconfigure(0, weight=1)
    # Tab contents differ in height; recompute the page scroll range on switch.
    def on_tab_changed(_e: tk.Event) -> None:
        root.after_idle(update_scroll_region)
        root.after(100, update_scroll_region)

    tabs.bind("<<NotebookTabChanged>>", on_tab_changed)

    # ========================================================================
    # OPERATIONS TAB / RUN CONTROLS - request a restart or test-zone run
    # ========================================================================
    controls = ttk.Frame(ops_tab, style="Card.TFrame", padding=14)
    controls.grid(row=0, column=0, sticky="ew", padx=6, pady=6)
    controls.columnconfigure(5, weight=1)
    button_status_var = tk.StringVar(value="")
    run_mode_var = tk.StringVar(value="restart")
    index_limit_var = tk.StringVar(value="0")
    detail_limit_var = tk.StringVar(value="0")

    def selected_limit(var: tk.StringVar, default: str) -> str:
        value = var.get().strip() or default
        # "0" is a valid value everywhere: all index pages / unlimited details.
        return value if value.isdigit() else default

    def request_run_now() -> None:
        index_limit = selected_limit(index_limit_var, "0")
        detail_limit = selected_limit(detail_limit_var, "0")
        mode = run_mode_var.get()
        if mode == "test":
            subprocess.Popen([str(BASE_DIR / "src/20_pipeline/070-test-zone.py"), "--limit", detail_limit, "--apply"], cwd=BASE_DIR, env=monitor_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            button_status_var.set(f"Test-zone run requested with detail limit {detail_limit}.")
            return
        if mode == "manual":
            subprocess.Popen([str(BASE_DIR / "src/20_pipeline/110b-run-now.sh"), detail_limit, index_limit, "MANUAL"], cwd=BASE_DIR, env=monitor_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            button_status_var.set(f"Manual run started with index page cap {index_limit} (0 = all), detail limit {detail_limit}.")
            return
        subprocess.Popen([str(BASE_DIR / "src/20_pipeline/110a-request-run.sh"), detail_limit, "RESTART", index_limit], cwd=BASE_DIR, env=monitor_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        button_status_var.set(f"Restart-pending run requested with index page cap {index_limit} (0 = all), detail limit {detail_limit}.")

    def stop_run_now() -> None:
        # Halt the active collection but keep this monitor (and the timer/webhook)
        # running. Stays enabled while a run is active — that is when it is needed.
        subprocess.Popen([str(BASE_DIR / "src/20_pipeline/120b-stop-collectors.sh")], cwd=BASE_DIR, env=monitor_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        button_status_var.set("Stop requested: halting the active collection (worker + collectors). Monitor stays open; request a new run to resume.")

    def stop_all_now() -> None:
        # Same script as the "STOP all runners" manual action, surfaced here as a
        # direct button (with confirmation) instead of buried in that list, since
        # it is destructive enough to warrant one-click, hard-to-miss access.
        if not messagebox.askyesno(
            "Confirm Stop All",
            "This stops EVERYTHING: workers, test zone, calendar builder, "
            "monitors, webhook listener and updaters. This monitor window will "
            "close too. Continue?",
            icon="warning", default="no",
        ):
            return
        subprocess.Popen([str(BASE_DIR / "src/20_pipeline/120a-stop-everything.sh")], cwd=BASE_DIR, env=monitor_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        button_status_var.set("Stop All requested: halting every PanamaCompra process, including this monitor.")

    def start_all_now() -> None:
        # Counterpart to Stop All: brings the Docker integrations and the
        # webhook listener back up and opens the monitor. Does not queue a
        # collector run on its own -- use "Request selected run" for that.
        subprocess.Popen([str(BASE_DIR / "src/20_pipeline/120c-start-everything.sh")], cwd=BASE_DIR, env=monitor_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        button_status_var.set("Start All requested: bringing Docker integrations and the webhook listener back up.")

    def dev_pause_now() -> None:
        # Pauses automatic triggers (webhook auto-run, cron) and the updater's
        # autostash for a safe editing session, without closing the monitor,
        # webhook listener or Docker integrations. See 121-dev-mode.sh.
        subprocess.Popen([str(BASE_DIR / "src/20_pipeline/121-dev-mode.sh"), "pause"], cwd=BASE_DIR, env=monitor_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        button_status_var.set("Dev Pause requested: stopping any active run and pausing automatic triggers.")

    def dev_resume_now() -> None:
        # Restores every setting Dev Pause changed, to its exact previous value.
        subprocess.Popen([str(BASE_DIR / "src/20_pipeline/121-dev-mode.sh"), "resume"], cwd=BASE_DIR, env=monitor_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        button_status_var.set("Dev Resume requested: restoring automatic triggers to their previous settings.")

    add_section_header(controls, "Run controls", "Choose a mode, index page cap and detail limit (0 = unlimited), then request or stop a run.", columnspan=6)
    # Mode is explicit: AUTO is reserved for changedetection/webhook-triggered
    # runs; manual launches are either RESTART (real pipeline) or TEST (sandbox).
    # The Limit entry is wide enough for large counts. The whole row is disabled
    # while a collection is active (e.g. a webhook-triggered run) so you cannot
    # change mode or queue a conflicting run mid-flight; it re-enables when idle.
    ttk.Label(controls, text="Mode:", style="Card.TLabel").grid(row=1, column=0, sticky="w")
    auto_radio = ttk.Radiobutton(controls, text="automatic", value="auto", variable=run_mode_var, style="Card.TRadiobutton", state="disabled")
    auto_radio.grid(row=1, column=1, sticky="w")
    live_radio = ttk.Radiobutton(controls, text="run pending only", value="restart", variable=run_mode_var, style="Card.TRadiobutton")
    live_radio.grid(row=1, column=2, sticky="w")
    manual_radio = ttk.Radiobutton(controls, text="manual run", value="manual", variable=run_mode_var, style="Card.TRadiobutton")
    manual_radio.grid(row=1, column=3, sticky="w")
    test_radio = ttk.Radiobutton(controls, text="test run", value="test", variable=run_mode_var, style="Card.TRadiobutton")
    test_radio.grid(row=1, column=4, sticky="w", padx=(0, 16))
    ttk.Label(controls, text="Index page cap:", style="Card.TLabel").grid(row=2, column=0, sticky="e")
    index_limit_entry = ttk.Entry(controls, textvariable=index_limit_var, width=8)
    index_limit_entry.grid(row=2, column=1, sticky="w", padx=(6, 12))
    ttk.Label(controls, text="Detail limit:", style="Card.TLabel").grid(row=2, column=2, sticky="e")
    detail_limit_entry = ttk.Entry(controls, textvariable=detail_limit_var, width=8)
    detail_limit_entry.grid(row=2, column=3, sticky="w", padx=(6, 16))
    run_button = ttk.Button(controls, text="Request selected run", command=request_run_now, style="Accent.TButton")
    run_button.grid(row=2, column=4, sticky="w")
    stop_button = ttk.Button(controls, text="■ Stop run", command=stop_run_now, style="Danger.TButton")
    stop_button.grid(row=2, column=5, sticky="e")
    dev_pause_button = ttk.Button(controls, text="⏸ Dev Pause", command=dev_pause_now)
    dev_pause_button.grid(row=3, column=2, sticky="e", padx=(0, 8), pady=(6, 0))
    dev_resume_button = ttk.Button(controls, text="▶ Dev Resume", command=dev_resume_now)
    dev_resume_button.grid(row=3, column=3, sticky="e", padx=(0, 16), pady=(6, 0))
    start_all_button = ttk.Button(controls, text="▶ Start All", command=start_all_now, style="Accent.TButton")
    start_all_button.grid(row=3, column=4, sticky="e", padx=(0, 8), pady=(6, 0))
    stop_all_button = ttk.Button(controls, text="⛔ Stop All", command=stop_all_now, style="Danger.TButton")
    stop_all_button.grid(row=3, column=5, sticky="e", pady=(6, 0))
    ttk.Label(controls, textvariable=button_status_var, style="Card.TLabel", wraplength=520).grid(row=4, column=0, columnspan=6, sticky="w", pady=(8, 0))
    add_tooltip(auto_radio, "automatic = shown for changedetection/webhook runs; not selectable manually.")
    add_tooltip(live_radio, "run pending only = queue the normal collector pipeline (real archive).")
    add_tooltip(manual_radio, "manual run = start the worker immediately from this monitor.")
    add_tooltip(test_radio, "test run = the isolated test zone (records_test/), real archive untouched.")
    add_tooltip(index_limit_entry, "Optional cap for index pages per status group. 0 = all pages until the portal has no Next page.")
    add_tooltip(detail_limit_entry, "Maximum detail pages (restart/manual) or sandbox records (test) to process this run.")
    add_tooltip(run_button, "Queue the selected run with the chosen mode and limit (disabled while a run is active).")
    add_tooltip(stop_button, "Stop the active collection now (worker + index/detail/test/calendar) and prevent auto-resume. The monitor, next-run timer and webhook keep running. Stays enabled during a run, unlike the rest of this row.")
    add_tooltip(start_all_button, "Brings background infrastructure back up after Stop All: Docker integrations (changedetection/WAHA/sockpuppetbrowser), the webhook listener, and opens the monitor. Does not queue a collector run by itself.")
    add_tooltip(stop_all_button, "DANGER: stops ALL processes — workers, test zone, calendar builder, monitors, webhook listener and updaters. This monitor closes too. Asks for confirmation first.")
    add_tooltip(dev_pause_button, "Stops any active run and pauses webhook/cron auto-triggers plus the updater's autostash, so editing this repo is safe. Docker integrations, monitors and the webhook listener stay running.")
    add_tooltip(dev_resume_button, "Restores every setting Dev Pause changed, to its exact previous value. Does not queue a run by itself.")
    add_section_toggle(controls, button_column=5)

    # Keys that mean "real collection work is happening". A webhook-triggered run
    # shows up here (worker/index/detail/...), so the run controls lock while any
    # of them are active and unlock once the run is fully idle.
    run_control_widgets = (live_radio, manual_radio, test_radio, index_limit_entry, detail_limit_entry, run_button)

    def update_run_controls(snap: dict[str, object]) -> None:
        processes = snap.get("processes", {}) or {}
        busy = any(processes.get(key) for key in WORK_PROCESS_KEYS)
        target_state = "disabled" if busy else "normal"
        for widget in run_control_widgets:
            try:
                widget.configure(state=target_state)
            except tk.TclError:
                pass
        progress = snap.get("progress", {}) or {}
        current_mode = str(progress.get("MODE", "")).strip().upper()
        if busy:
            if current_mode == "AUTO":
                run_mode_var.set("auto")
            elif current_mode == "MANUAL":
                run_mode_var.set("manual")
            elif current_mode == "TEST":
                run_mode_var.set("test")
            else:
                run_mode_var.set("restart")
            run_button.configure(text="Run in progress…")
        else:
            if run_mode_var.get() == "auto":
                run_mode_var.set("restart")
            run_button.configure(text="Request selected run")

    # ========================================================================
    # SETTINGS TAB - general options in one fixed, tidy order: Monitor window →
    # Storage paths → Collector & webhook automation → Next-run timer window →
    # Integrations → Work templates. Everything WhatsApp/WAHA lives in the
    # WhatsApp tab; both tabs share the same Apply logic and persist to
    # data/config/monitor_settings.env (environment variables still win).
    # ========================================================================
    settings = ttk.Frame(settings_tab, style="Card.TFrame", padding=14)
    settings.grid(row=0, column=0, sticky="ew", padx=6, pady=6)
    settings.columnconfigure(1, weight=1)
    settings.columnconfigure(3, weight=1)

    whatsapp = ttk.Frame(whatsapp_tab, style="Card.TFrame", padding=14)
    whatsapp.grid(row=0, column=0, sticky="ew", padx=6, pady=6)
    whatsapp.columnconfigure(0, weight=1)
    whatsapp.columnconfigure(1, weight=1)
    whatsapp.columnconfigure(3, weight=1)

    alpha_var = tk.StringVar(value=f"{runtime['alpha']:.2f}")
    autoclose_var = tk.StringVar(value=str(runtime["auto_close"]))
    refresh_var = tk.StringVar(value=str(runtime["refresh"]))
    idle_var = tk.StringVar(value=str(runtime["idle_refresh"]))
    source_var = tk.StringVar(value=setting("PC_WAHA_SOURCE", "Panamá Compra"))
    waha_var = tk.StringVar(value=read_chat_file(WAHA_CHAT_ID_PATH))
    waha_index_var = tk.StringVar(value=read_chat_file(WAHA_CHAT_ID_INDEX_PATH))
    waha_details_var = tk.StringVar(value=read_chat_file(WAHA_CHAT_ID_DETAILS_PATH))
    waha_status_var = tk.StringVar(value=read_chat_file(WAHA_CHAT_ID_STATUS_PATH))
    waha_system_var = tk.StringVar(value=read_chat_file(WAHA_CHAT_ID_SYSTEM_PATH))
    waha_summary_var = tk.StringVar(value=read_chat_file(WAHA_CHAT_ID_SUMMARY_PATH))

    def read_filter_file(path: Path) -> str:
        if not path.exists():
            return ""
        rules = [k.strip() for k in path.read_text(encoding="utf-8", errors="replace").splitlines() if k.strip() and not k.startswith("#")]
        return ", ".join(rules)

    keywords_var = tk.StringVar(value=read_filter_file(WAHA_KEYWORDS_PATH))
    keywords_index_var = tk.StringVar(value=read_filter_file(CONFIG_DIR / "waha_keywords_index.txt"))
    keywords_details_var = tk.StringVar(value=read_filter_file(CONFIG_DIR / "waha_keywords_details.txt"))
    keywords_status_var = tk.StringVar(value=read_filter_file(CONFIG_DIR / "waha_keywords_status.txt"))
    notify_whatsapp_var = tk.BooleanVar(value=setting("PC_NOTIFY_WHATSAPP", "1") != "0")
    notify_details_var = tk.BooleanVar(value=setting("PC_NOTIFY_DETAILS", "1") != "0")
    notify_details_inline_var = tk.BooleanVar(value=setting("PC_NOTIFY_DETAILS_INLINE", "1") != "0")
    index_from_snapshot_var = tk.BooleanVar(value=setting("PC_INDEX_FROM_SNAPSHOT", "1") != "0")
    import_calendar_var = tk.BooleanVar(value=setting("PC_CALENDAR_AUTO_IMPORT", "0") == "1")
    records_dir_var = tk.StringVar(value=setting("PC_RECORDS_DIR", str(pc_common.RECORDS_DIR)))
    calendar_dir_var = tk.StringVar(value=setting("PC_CALENDAR_DIR", str(pc_common.CALENDAR_DIR)))
    records_test_dir_var = tk.StringVar(value=setting("PC_RECORDS_TEST_DIR", str(pc_common.RECORDS_TEST_DIR)))

    # Advanced collector / timer / WhatsApp settings. These are read by the
    # collectors, timer and notifier from monitor_settings.env at their own
    # startup, so they take effect on the next run/launch (not live like alpha).
    _truthy = {"1", "true", "yes", "on"}
    interval_var = tk.StringVar(value=setting("PC_NEXT_RUN_INTERVAL_MINUTES", "30"))
    soon_days_var = tk.StringVar(value=setting("PC_MONITOR_DEADLINE_SOON_DAYS", "7"))
    webhook_index_var = tk.StringVar(value=setting("PC_WEBHOOK_INDEX_LIMIT", setting("PC_INDEX_LIMIT", "0")))
    webhook_detail_var = tk.StringVar(value=setting("PC_WEBHOOK_DETAIL_LIMIT", "0"))
    within_days_var = tk.StringVar(value=setting("PC_NOTIFY_WITHIN_DAYS", ""))
    retries_var = tk.StringVar(value=setting("PC_WAHA_RETRIES", "2"))
    send_delay_var = tk.StringVar(value=setting("PC_WAHA_SEND_DELAY_SECONDS", "3"))
    digest_threshold_var = tk.StringVar(value=setting("PC_NOTIFY_INDEX_DIGEST_THRESHOLD", "10"))
    idle_hours_var = tk.StringVar(value=setting("PC_NOTIFY_IDLE_EVERY_HOURS", "6"))
    waha_base_var = tk.StringVar(value=setting("PC_WAHA_BASE_URL", "http://127.0.0.1:3000"))
    waha_session_var = tk.StringVar(value=setting("PC_WAHA_SESSION", "default"))
    waha_events_var = tk.StringVar(value=setting("PC_WAHA_NOTIFY_EVENTS", "info,start,done,failed,timeout,resume,update,new,none"))
    test_limit_var = tk.StringVar(value=setting("PC_TEST_ZONE_LIMIT", "5"))
    timer_width_var = tk.StringVar(value=setting("PC_NEXT_RUN_TIMER_WIDTH", "380"))
    timer_height_var = tk.StringVar(value=setting("PC_NEXT_RUN_TIMER_HEIGHT", "360"))
    timer_top_var = tk.StringVar(value=setting("PC_NEXT_RUN_TIMER_TOP", "30"))
    timer_records_var = tk.StringVar(value=setting("PC_NEXT_RUN_TIMER_RECORDS", "20"))
    timer_data_refresh_var = tk.StringVar(value=setting("PC_NEXT_RUN_TIMER_DATA_REFRESH_SECONDS", "10"))
    monitor_stale_var = tk.StringVar(value=setting("PC_MONITOR_STALE_SECONDS", "120"))
    changedetection_url_var = tk.StringVar(value=setting("CHANGEDETECTION_BASE_URL", os.environ.get("CHANGEDETECTION_BASE_URL", "http://localhost:5000")))
    webhook_port_var = tk.StringVar(value=setting("PC_WEBHOOK_PORT", os.environ.get("PC_WEBHOOK_PORT", "8765")))
    webhook_public_host_var = tk.StringVar(value=setting("PC_WEBHOOK_PUBLIC_HOST", os.environ.get("PC_WEBHOOK_PUBLIC_HOST", "host.docker.internal")))
    webhook_auto_run_var = tk.BooleanVar(value=setting("PC_WEBHOOK_AUTO_RUN", "1").lower() in _truthy)
    waha_port_var = tk.StringVar(value=setting("WAHA_PORT", os.environ.get("WAHA_PORT", "3000")))
    waha_server_key_var = tk.StringVar(value=setting("WAHA_API_KEY", ""))
    # WAHA dashboard login: setup / `pcc docker up` generates a RANDOM password
    # into .env and data/config/integration-access.txt so the review dashboard
    # is always reachable; change it here whenever desired (applied on the next
    # stack restart). Blank keeps the generated .env value.
    waha_dash_user_var = tk.StringVar(value=setting("WAHA_DASHBOARD_USERNAME", "admin"))
    waha_dash_pass_var = tk.StringVar(value=setting("WAHA_DASHBOARD_PASSWORD", ""))
    waha_enabled_var = tk.BooleanVar(value=setting("PC_WAHA_ENABLED", "0").lower() in _truthy)
    skip_expired_var = tk.BooleanVar(value=setting("PC_NOTIFY_SKIP_EXPIRED", "0").lower() in _truthy)
    test_autorun_var = tk.BooleanVar(value=setting("PC_TEST_ZONE_AUTORUN", "0").lower() in _truthy)
    update_before_var = tk.BooleanVar(value=setting("PC_RUN_UPDATE_BEFORE_RUN", "1") != "0")

    def field(frame: ttk.Frame, row: int, col: int, label: str, var: tk.StringVar, width: int, tip: str) -> None:
        ttk.Label(frame, text=label, style="Card.TLabel").grid(row=row, column=col, sticky="w", padx=(0, 6), pady=3)
        entry = ttk.Entry(frame, textvariable=var, width=width)
        entry.grid(row=row, column=col + 1, sticky="ew", pady=3, padx=(0, 12))
        add_tooltip(entry, tip)

    def group_title(frame: ttk.Frame, row: int, text: str) -> None:
        ttk.Label(frame, text=text, style="Title.TLabel").grid(row=row, column=0, columnspan=4, sticky="w", pady=(12, 6))

    # ---- Settings tab: Monitor window -------------------------------------
    add_section_header(settings, "Settings", "Steps: Monitor window → Storage paths → Collector & webhook → Timer window → Integrations → Work templates.", columnspan=4)
    field(settings, 1, 0, "Transparency 0.30–1.00:", alpha_var, 8, "Whole-window opacity (text shares it). Default 0.85 = lightly translucent and readable. Lower it toward 0.30 for a more see-through window; 1.00 = fully opaque. Applied live when you click Apply.")
    field(settings, 1, 2, "Auto-close seconds (0=off):", autoclose_var, 8, "Seconds to count down after a LIVE run finishes before this window closes. 0 keeps it open. Default 20.")
    field(settings, 2, 0, "Active refresh seconds:", refresh_var, 8, "How often (seconds) the monitor refreshes while a run is active. Minimum 2. Default 3.")
    field(settings, 2, 2, "Idle refresh seconds:", idle_var, 8, "How often the monitor refreshes when idle (low power). Default 15.")

    # ---- Settings tab: Storage paths ---------------------------------------
    group_title(settings, 3, "Storage paths")
    field(settings, 4, 0, "Records folder:", records_dir_var, 36, "Where normal record folders are stored. Environment key: PC_RECORDS_DIR. Relative paths are resolved from the checkout root.")
    field(settings, 5, 0, "Calendar packages folder:", calendar_dir_var, 36, "Where timestamped .ics calendar packages are written. Environment key: PC_CALENDAR_DIR.")
    field(settings, 6, 0, "Test sandbox folder:", records_test_dir_var, 36, "Where the isolated test zone stores re-downloaded records. Environment key: PC_RECORDS_TEST_DIR.")

    # ---- Settings tab: Collector & webhook automation ----------------------
    group_title(settings, 7, "Collector & webhook automation (apply on the next run/launch)")
    field(settings, 8, 0, "Next-run interval (min):", interval_var, 8, "Timer cadence: minutes between expected automatic runs shown by the next-run countdown. Env: PC_NEXT_RUN_INTERVAL_MINUTES.")
    field(settings, 8, 2, "Deadline 'soon' days:", soon_days_var, 8, "DTEND within this many days shows amber 'next to expire' in the record list. Env: PC_MONITOR_DEADLINE_SOON_DAYS (applies on monitor restart).")
    field(settings, 9, 0, "Webhook index page cap:", webhook_index_var, 8, "Optional index page cap for automatic runs. 0 = all pages until no Next page. Env: PC_WEBHOOK_INDEX_LIMIT.")
    field(settings, 9, 2, "Webhook detail limit:", webhook_detail_var, 8, "Detail pages per automatic (changedetection) AUTO run. 0 = every pending detail row. Env: PC_WEBHOOK_DETAIL_LIMIT.")
    field(settings, 10, 0, "Test-zone records:", test_limit_var, 8, "How many recent records the idle test zone re-runs in the sandbox. Env: PC_TEST_ZONE_LIMIT.")
    field(settings, 10, 2, "Monitor stale seconds:", monitor_stale_var, 8, "Seconds before stale RUNNING progress unlocks controls when no worker process is active. Env: PC_MONITOR_STALE_SECONDS.")
    calendar_check = ttk.Checkbutton(settings, text="Import/open generated calendar events", variable=import_calendar_var, style="Card.TCheckbutton")
    calendar_check.grid(row=11, column=0, columnspan=2, sticky="w", pady=3)
    add_tooltip(calendar_check, "Turn on to open generated .ics calendar packages/events after they are built.")
    test_autorun_check = ttk.Checkbutton(settings, text="Auto-run test zone when no new records", variable=test_autorun_var, style="Card.TCheckbutton")
    test_autorun_check.grid(row=11, column=2, columnspan=2, sticky="w", pady=3)
    add_tooltip(test_autorun_check, "When a run finds no new records, exercise the current code on the last N records in the sandbox. Env: PC_TEST_ZONE_AUTORUN.")
    update_before_check = ttk.Checkbutton(settings, text="Update local copy before each run", variable=update_before_var, style="Card.TCheckbutton")
    update_before_check.grid(row=12, column=0, columnspan=2, sticky="w", pady=3)
    add_tooltip(update_before_check, "Fast-forward the local git checkout before each worker iteration. Env: PC_RUN_UPDATE_BEFORE_RUN.")

    # ---- Settings tab: Next-run timer window -------------------------------
    group_title(settings, 13, "Next-run timer window")
    field(settings, 14, 0, "Timer width:", timer_width_var, 8, "Next-run timer window width in pixels. Env: PC_NEXT_RUN_TIMER_WIDTH.")
    field(settings, 14, 2, "Timer height:", timer_height_var, 8, "Next-run timer window height in pixels. Env: PC_NEXT_RUN_TIMER_HEIGHT.")
    field(settings, 15, 0, "Timer top offset:", timer_top_var, 8, "Pixels from top of screen for the timer window. Env: PC_NEXT_RUN_TIMER_TOP.")
    field(settings, 15, 2, "Timer latest records:", timer_records_var, 8, "How many latest records the timer window lists. Env: PC_NEXT_RUN_TIMER_RECORDS.")
    field(settings, 16, 0, "Timer data refresh sec:", timer_data_refresh_var, 8, "How often the timer refreshes git/database/queue details. Env: PC_NEXT_RUN_TIMER_DATA_REFRESH_SECONDS.")

    # ---- Settings tab: Integrations ----------------------------------------
    group_title(settings, 17, "Integrations (changedetection container; applied on the next docker stack restart)")
    field(settings, 18, 0, "changedetection URL:", changedetection_url_var, 24, "Base URL the changedetection container advertises and the 'Open changedetection UI' button uses. Env: CHANGEDETECTION_BASE_URL. Applied to the container on the next docker stack restart.")
    field(settings, 18, 2, "Webhook listener port:", webhook_port_var, 8, "Port used by the host webhook listener and the recommended changedetection json://host.docker.internal URL. Env: PC_WEBHOOK_PORT.")
    field(settings, 19, 0, "Webhook public host:", webhook_public_host_var, 24, "Hostname changedetection containers use to reach the host listener. Keep host.docker.internal for Docker Desktop/modern Linux Docker. Env: PC_WEBHOOK_PUBLIC_HOST.")
    webhook_auto_run_check = ttk.Checkbutton(settings, text="Automatic runs from changedetection (webhook)", variable=webhook_auto_run_var, style="Card.TCheckbutton")
    webhook_auto_run_check.grid(row=20, column=0, columnspan=4, sticky="w", pady=3)
    add_tooltip(webhook_auto_run_check, "ON (default) = changedetection triggers a collector run as usual. OFF = manual mode: the webhook listener keeps running (status stays accurate, no need to stop/restart it) but ignores incoming triggers instead of starting a run. Env: PC_WEBHOOK_AUTO_RUN.")

    def send_test_whatsapp() -> None:
        MANUAL_ACTION_LOG.parent.mkdir(parents=True, exist_ok=True)
        with MANUAL_ACTION_LOG.open("a", encoding="utf-8") as log_file:
            log_file.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} | WhatsApp / native test send =====\n")
            subprocess.Popen(
                [str(BASE_DIR / "src/30_notify/010-waha-client.py"), "--event", "info", "--status", "TEST",
                 "--force-send", "--purpose", "system",
                 "--message", "Prueba de notificación desde el monitor PanamaCompra."],
                cwd=BASE_DIR, env=monitor_env(), stdout=log_file, stderr=subprocess.STDOUT,
            )
        button_status_var.set("WhatsApp test message requested. Output: data/logs/manual_actions.log")

    def apply_settings() -> None:
        def as_int(var: tk.StringVar, fallback: int, low: int) -> int:
            try:
                return max(low, int(float(var.get())))
            except (TypeError, ValueError):
                return fallback

        try:
            alpha = min(1.0, max(0.30, float(alpha_var.get())))
        except (TypeError, ValueError):
            alpha = runtime["alpha"]
        runtime["alpha"] = alpha
        apply_alpha(alpha)
        runtime["auto_close"] = as_int(autoclose_var, runtime["auto_close"], 0)
        runtime["refresh"] = as_int(refresh_var, runtime["refresh"], 2)
        runtime["idle_refresh"] = max(runtime["refresh"], as_int(idle_var, runtime["idle_refresh"], 2))

        # Reflect the normalized values back into the entries.
        alpha_var.set(f"{alpha:.2f}")
        autoclose_var.set(str(runtime["auto_close"]))
        refresh_var.set(str(runtime["refresh"]))
        idle_var.set(str(runtime["idle_refresh"]))

        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        WAHA_CHAT_ID_PATH.write_text(waha_var.get().strip() + "\n", encoding="utf-8")
        WAHA_CHAT_ID_INDEX_PATH.write_text(waha_index_var.get().strip() + "\n", encoding="utf-8")
        WAHA_CHAT_ID_DETAILS_PATH.write_text(waha_details_var.get().strip() + "\n", encoding="utf-8")
        WAHA_CHAT_ID_STATUS_PATH.write_text(waha_status_var.get().strip() + "\n", encoding="utf-8")
        WAHA_CHAT_ID_SYSTEM_PATH.write_text(waha_system_var.get().strip() + "\n", encoding="utf-8")
        WAHA_CHAT_ID_SUMMARY_PATH.write_text(waha_summary_var.get().strip() + "\n", encoding="utf-8")

        def write_filter_file(path: Path, raw: str) -> None:
            rules = [k.strip() for k in re.split(r"[,\n]", raw) if k.strip()]
            path.write_text(("\n".join(rules) + "\n") if rules else "", encoding="utf-8")

        write_filter_file(WAHA_KEYWORDS_PATH, keywords_var.get())
        write_filter_file(CONFIG_DIR / "waha_keywords_index.txt", keywords_index_var.get())
        write_filter_file(CONFIG_DIR / "waha_keywords_details.txt", keywords_details_var.get())
        write_filter_file(CONFIG_DIR / "waha_keywords_status.txt", keywords_status_var.get())
        available_templates = set(record_templates.source_files(record_templates.source_dir()))
        selected_templates = [templates_listbox.get(i) for i in templates_listbox.curselection() if templates_listbox.get(i) in available_templates]
        record_templates.save_selection(selected_templates)

        updates = {
            "PC_MONITOR_TK_ALPHA": f"{alpha:.2f}",
            "PC_MONITOR_TK_AUTO_CLOSE_SECONDS": str(runtime["auto_close"]),
            "PC_MONITOR_TK_REFRESH_SECONDS": str(runtime["refresh"]),
            "PC_MONITOR_TK_IDLE_REFRESH_SECONDS": str(runtime["idle_refresh"]),
            "PC_WAHA_SOURCE": source_var.get().strip() or "Panamá Compra",
            "PC_NOTIFY_WHATSAPP": "1" if notify_whatsapp_var.get() else "0",
            "PC_NOTIFY_DETAILS": "1" if notify_details_var.get() else "0",
            "PC_NOTIFY_DETAILS_INLINE": "1" if notify_details_inline_var.get() else "0",
            "PC_INDEX_FROM_SNAPSHOT": "1" if index_from_snapshot_var.get() else "0",
            "PC_WAHA_SEND_DELAY_SECONDS": send_delay_var.get().strip() or "3",
            "PC_NOTIFY_INDEX_DIGEST_THRESHOLD": digest_threshold_var.get().strip() or "10",
            "PC_NOTIFY_IDLE_EVERY_HOURS": idle_hours_var.get().strip() or "6",
            "PC_TEMPLATES_SRC_DIR": templates_src_var.get().strip(),
            "PC_CALENDAR_AUTO_IMPORT": "1" if import_calendar_var.get() else "0",
            "PC_RECORDS_DIR": records_dir_var.get().strip() or str(pc_common.RECORDS_DIR),
            "PC_CALENDAR_DIR": calendar_dir_var.get().strip() or str(pc_common.CALENDAR_DIR),
            "PC_RECORDS_TEST_DIR": records_test_dir_var.get().strip() or str(pc_common.RECORDS_TEST_DIR),
            "PC_NEXT_RUN_INTERVAL_MINUTES": interval_var.get().strip() or "30",
            "PC_MONITOR_DEADLINE_SOON_DAYS": soon_days_var.get().strip() or "7",
            "PC_WEBHOOK_INDEX_LIMIT": webhook_index_var.get().strip() or "0",
            "PC_WEBHOOK_DETAIL_LIMIT": webhook_detail_var.get().strip() or "0",
            "PC_NOTIFY_WITHIN_DAYS": within_days_var.get().strip(),
            "PC_WAHA_RETRIES": retries_var.get().strip() or "2",
            "PC_WAHA_BASE_URL": waha_base_var.get().strip() or "http://127.0.0.1:3000",
            "PC_WAHA_SESSION": waha_session_var.get().strip() or "default",
            "PC_WAHA_NOTIFY_EVENTS": waha_events_var.get().strip() or "info,start,done,failed,timeout,resume,update,new,none",
            "PC_TEST_ZONE_LIMIT": test_limit_var.get().strip() or "5",
            "PC_NEXT_RUN_TIMER_WIDTH": timer_width_var.get().strip() or "380",
            "PC_NEXT_RUN_TIMER_HEIGHT": timer_height_var.get().strip() or "360",
            "PC_NEXT_RUN_TIMER_TOP": timer_top_var.get().strip() or "30",
            "PC_NEXT_RUN_TIMER_RECORDS": timer_records_var.get().strip() or "20",
            "PC_NEXT_RUN_TIMER_DATA_REFRESH_SECONDS": timer_data_refresh_var.get().strip() or "10",
            "PC_MONITOR_STALE_SECONDS": monitor_stale_var.get().strip() or "120",
            "CHANGEDETECTION_BASE_URL": changedetection_url_var.get().strip() or "http://localhost:5000",
            "PC_WEBHOOK_PORT": webhook_port_var.get().strip() or "8765",
            "PC_WEBHOOK_PUBLIC_HOST": webhook_public_host_var.get().strip() or "host.docker.internal",
            "PC_WEBHOOK_AUTO_RUN": "1" if webhook_auto_run_var.get() else "0",
            "WAHA_PORT": waha_port_var.get().strip() or "3000",
            "WAHA_API_KEY": waha_server_key_var.get().strip(),
            "WAHA_DASHBOARD_USERNAME": waha_dash_user_var.get().strip() or "admin",
            "WAHA_DASHBOARD_PASSWORD": waha_dash_pass_var.get().strip(),
            "PC_WAHA_ENABLED": "1" if waha_enabled_var.get() else "0",
            "PC_NOTIFY_SKIP_EXPIRED": "1" if skip_expired_var.get() else "0",
            "PC_TEST_ZONE_AUTORUN": "1" if test_autorun_var.get() else "0",
            "PC_RUN_UPDATE_BEFORE_RUN": "1" if update_before_var.get() else "0",
        }
        merged = load_settings_file()
        merged.update(updates)
        lines = [
            "# PanamaCompra monitor settings (KEY=VALUE).",
            "# Edited from the monitor Settings panel; same-named environment",
            "# variables override these at startup.",
        ]
        lines += [f"{key}={shlex.quote(str(merged[key]))}" for key in sorted(merged)]
        SETTINGS_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
        _SETTINGS_FILE.clear()
        _SETTINGS_FILE.update(merged)
        button_status_var.set("Settings applied (transparency live) and saved to data/config/monitor_settings.env.")

    apply_button = ttk.Button(settings, text="Apply & save settings", command=apply_settings, style="Accent.TButton")
    apply_button.grid(row=21, column=0, sticky="w", pady=(10, 0))
    add_tooltip(apply_button, "Apply transparency immediately and persist EVERY setting from the Settings and WhatsApp tabs to data/config/monitor_settings.env (shell-quoted so the worker can source them).")
    _track_wraplabel(ttk.Label(settings, text="Collector/timer settings apply on the next run or monitor launch; container settings (changedetection URL — and the WAHA server values in the WhatsApp tab) apply when the docker stack is restarted from the Integrations buttons in Operations.", style="Card.TLabel", wraplength=820)).grid(row=24, column=0, columnspan=4, sticky="w", pady=(8, 0))

    # ---- Settings tab: Work templates ---------------------------------------
    group_title(settings, 22, "Work templates (copied into templates/ inside each record folder)")
    templates_src_var = tk.StringVar(value=setting("PC_TEMPLATES_SRC_DIR", ""))
    field(settings, 23, 0, "Templates source folder:", templates_src_var, 36, "Folder holding your reusable work files (bid forms, checklists, ...). Blank = var/templates. Env: PC_TEMPLATES_SRC_DIR. Click Apply, then Refresh template files to re-scan.")
    templates_listbox = tk.Listbox(settings, selectmode="multiple", height=5, activestyle="none", exportselection=False)
    templates_listbox.grid(row=24, column=1, columnspan=3, sticky="ew", pady=3)
    ttk.Label(settings, text="Template files (Ctrl-click = multi-select):", style="Card.TLabel").grid(row=24, column=0, sticky="nw", pady=3)
    add_tooltip(templates_listbox, "Tick the template files to copy into each record's templates/ folder. Selection is saved on Apply to data/config/templates_selected.txt (shared with pcc templates). New downloads receive them automatically; files already inside a record are never overwritten.")

    def refresh_templates_list() -> None:
        templates_listbox.delete(0, "end")
        src = record_templates.source_dir()
        files = record_templates.source_files(src)
        selected = set(record_templates.load_selection())
        for position, rel_name in enumerate(files):
            templates_listbox.insert("end", rel_name)
            if rel_name in selected:
                templates_listbox.selection_set(position)
        if not files:
            templates_listbox.insert("end", f"(no files in {src} — drop templates there and refresh)")

    refresh_templates_button = ttk.Button(settings, text="Refresh template files", command=refresh_templates_list)
    refresh_templates_button.grid(row=22, column=2, sticky="w", pady=3)
    add_tooltip(refresh_templates_button, "Re-scan the template source folder (after Apply when the folder path changed).")
    refresh_templates_list()
    add_section_toggle(settings, button_column=3)

    # ========================================================================
    # WHATSAPP TAB - four card sections: WhatsApp Settings, Client Profiles,
    # Client Search, and Message Formats. Saved by the same Apply logic as the
    # Settings tab.
    # ========================================================================

    # ---- WhatsApp: Settings (toggles, destinations, filters, delivery, server, readability) ----
    whatsapp_settings = ttk.Frame(whatsapp, style="Card.TFrame", padding=14)
    whatsapp_settings.grid(row=0, column=0, sticky="ew", padx=0, pady=(0, 8))
    whatsapp_settings.columnconfigure(1, weight=1)
    whatsapp_settings.columnconfigure(3, weight=1)

    add_section_header(whatsapp_settings, "WhatsApp Settings", "Toggle WAHA, configure destinations, filters, delivery, and server settings.", columnspan=4,
                       detail="Apply saves everything to data/config/monitor_settings.env. Container settings need a docker stack restart.")

    waha_enabled_check = ttk.Checkbutton(whatsapp_settings, text="Enable WAHA WhatsApp sending", variable=waha_enabled_var, style="Card.TCheckbutton")
    waha_enabled_check.grid(row=1, column=0, columnspan=2, sticky="w", pady=3)
    add_tooltip(waha_enabled_check, "Master switch for WAHA WhatsApp sending. Env: PC_WAHA_ENABLED. Still needs a reachable WAHA server and a destination chat id.")
    notify_check = ttk.Checkbutton(whatsapp_settings, text="Notify by WhatsApp (index alerts right after scan)", variable=notify_whatsapp_var, style="Card.TCheckbutton")
    notify_check.grid(row=1, column=2, columnspan=2, sticky="w", pady=3)
    add_tooltip(notify_check, "Master switch for automatic WhatsApp MESSAGING. The index alert is sent right after the index scan, before downloads. Manual selected-record notification buttons remain available.")
    notify_details_check = ttk.Checkbutton(whatsapp_settings, text="Follow-up WhatsApp with item details after download", variable=notify_details_var, style="Card.TCheckbutton")
    notify_details_check.grid(row=2, column=0, columnspan=2, sticky="w", pady=3)
    add_tooltip(notify_details_check, "Second notifier phase: after each announced record's detail page downloads, send the '📥 Detalles Completos' message with the real items, location and full date range. Env: PC_NOTIFY_DETAILS. Off = items are absorbed silently (no duplicate messages).")
    skip_expired_check = ttk.Checkbutton(whatsapp_settings, text="Skip already-expired opportunities", variable=skip_expired_var, style="Card.TCheckbutton")
    skip_expired_check.grid(row=2, column=2, columnspan=2, sticky="w", pady=3)
    add_tooltip(skip_expired_check, "Do not announce opportunities whose deadline already passed. Env: PC_NOTIFY_SKIP_EXPIRED.")

    group_title(whatsapp_settings, 3, "Destinations")
    field(whatsapp_settings, 4, 0, "WhatsApp source label:", source_var, 8, "Text shown as '📌 Fuente:' in the WhatsApp messages (default 'Panamá Compra'). Automatic index alerts are sent right after the index scan; the item-details follow-up goes out after the downloads, when enabled.")
    ttk.Label(whatsapp_settings, text="Default destination chat id (…@g.us):", style="Card.TLabel").grid(row=5, column=0, sticky="w", pady=3)
    chat_entry = ttk.Entry(whatsapp_settings, textvariable=waha_var)
    chat_entry.grid(row=5, column=1, columnspan=3, sticky="ew", pady=3)
    add_tooltip(chat_entry, "Destination WhatsApp group/channel id for the automated 'what is new' messages. Saved to data/config/waha_chat_id.txt.")
    ttk.Label(whatsapp_settings, text="Optional per-purpose chat ids (blank = default; if default is blank and exactly one purpose is filled, it becomes the one group):", style="Card.TLabel").grid(row=6, column=0, columnspan=4, sticky="w", pady=(8, 3))
    for _row, (_label, _var, _tip) in enumerate((
        ("Index alerts:", waha_index_var, "Group/channel that receives the immediate index alerts (new opportunities + 'Sin nuevas entradas'). Env: PC_WAHA_CHAT_ID_INDEX. Blank = default destination."),
        ("Item details:", waha_details_var, "Group/channel that receives the '📥 Detalles Completos' follow-up with the downloaded items. Env: PC_WAHA_CHAT_ID_DETAILS. Blank = default destination."),
        ("Status changes:", waha_status_var, "Group/channel that receives status-change, cancellation and item-update messages. Env: PC_WAHA_CHAT_ID_STATUS. Blank = default destination."),
        ("System health:", waha_system_var, "Group/channel that receives review-system, worker start/failure and test messages. Env: PC_WAHA_CHAT_ID_SYSTEM. Blank = default destination."),
        ("Final summary per round:", waha_summary_var, "Group/channel that receives the one final run summary after each collector round. Env: PC_WAHA_CHAT_ID_SUMMARY. Blank = default destination."),
    ), start=7):
        ttk.Label(whatsapp_settings, text=_label, style="Card.TLabel").grid(row=_row, column=0, sticky="w", pady=3)
        _entry = ttk.Entry(whatsapp_settings, textvariable=_var)
        _entry.grid(row=_row, column=1, columnspan=3, sticky="ew", pady=3)
        add_tooltip(_entry, _tip)

    group_title(whatsapp_settings, 12, "Filters (OR with commas, AND with '+', NOT with '-'; blank = announce all)")
    ttk.Label(whatsapp_settings, text="Shared keywords (all destinations):", style="Card.TLabel").grid(row=13, column=0, sticky="w", pady=3)
    kw_entry = ttk.Entry(whatsapp_settings, textvariable=keywords_var)
    kw_entry.grid(row=13, column=1, columnspan=3, sticky="ew", pady=3)
    add_tooltip(kw_entry, "Shared filter for every WhatsApp destination without its own rules. OR between comma-separated rules; AND with '+' (salud + panama); NOT with '-' (-construccion excludes even when another rule matches). Blank announces every record. Saved to data/config/waha_keywords.txt.")
    field(whatsapp_settings, 14, 0, "Index alerts filter:", keywords_index_var, 30, "Rules for the index-alert destination only. Example: salud + panama, medicinas, -construccion. Blank = shared filter. Saved to data/config/waha_keywords_index.txt.")
    field(whatsapp_settings, 14, 2, "Item-details filter:", keywords_details_var, 30, "Rules for the detail follow-up destination only. Blank = shared filter. Saved to data/config/waha_keywords_details.txt.")
    field(whatsapp_settings, 15, 0, "Status-changes filter:", keywords_status_var, 30, "Rules for the status-change destination only. Blank = shared filter. Saved to data/config/waha_keywords_status.txt.")

    group_title(whatsapp_settings, 16, "Delivery")
    field(whatsapp_settings, 17, 0, "Within N days (blank=all):", within_days_var, 8, "Only announce opportunities whose deadline is within this many days; blank announces all. Env: PC_NOTIFY_WITHIN_DAYS.")
    field(whatsapp_settings, 17, 2, "Send retries:", retries_var, 8, "Extra WAHA send retries with short backoff before giving up. Env: PC_WAHA_RETRIES.")
    ttk.Label(whatsapp_settings, text="WAHA events (comma separated):", style="Card.TLabel").grid(row=18, column=0, sticky="w", pady=3)
    events_entry = ttk.Entry(whatsapp_settings, textvariable=waha_events_var)
    events_entry.grid(row=18, column=1, columnspan=3, sticky="ew", pady=3)
    add_tooltip(events_entry, "Which events are sent: info,start,done,failed,timeout,resume,update,new,none (or 'all'). Env: PC_WAHA_NOTIFY_EVENTS.")
    test_whatsapp_button = ttk.Button(whatsapp_settings, text="Send test WhatsApp", command=send_test_whatsapp)
    test_whatsapp_button.grid(row=19, column=0, sticky="w", pady=3)
    add_tooltip(test_whatsapp_button, "Send one WAHA test message to the configured destination using the saved settings, so you can verify the WhatsApp pipeline without waiting for a run. Requires 'Enable WAHA WhatsApp sending' and a chat id.")

    group_title(whatsapp_settings, 20, "WAHA server (container; applied on the next docker stack restart)")
    field(whatsapp_settings, 21, 0, "WAHA base URL:", waha_base_var, 24, "Base URL of the self-hosted WAHA HTTP API. Env: PC_WAHA_BASE_URL.")
    field(whatsapp_settings, 21, 2, "WAHA session:", waha_session_var, 16, "WAHA session name used when sending. Env: PC_WAHA_SESSION.")
    field(whatsapp_settings, 22, 0, "WAHA server port:", waha_port_var, 8, "Host port for the WAHA container (dashboard + API). Env: WAHA_PORT. Applied on the next docker stack restart; keep PC_WAHA_BASE_URL in sync.")
    field(whatsapp_settings, 22, 2, "WAHA server API key:", waha_server_key_var, 24, "Optional API key the WAHA container requires (X-Api-Key). Env: WAHA_API_KEY; the notifier's PC_WAHA_API_KEY defaults to it. Blank keeps the value from .env. Applied on the next docker stack restart.")
    field(whatsapp_settings, 23, 0, "Dashboard username:", waha_dash_user_var, 16, "Login user for the WAHA review dashboard (http://localhost:WAHA_PORT). Env: WAHA_DASHBOARD_USERNAME. Default admin.")
    field(whatsapp_settings, 23, 2, "Dashboard password:", waha_dash_pass_var, 16, "Login password for the WAHA review dashboard. Setup generates a random one (saved in .env and data/config/integration-access.txt); blank here keeps that value. Env: WAHA_DASHBOARD_PASSWORD. Applied on the next docker stack restart.")
    _track_wraplabel(ttk.Label(whatsapp_settings, text="Dashboard login: user admin with a RANDOM password generated by setup — see data/config/integration-access.txt. To change it, set the password above and click Apply, then restart the docker stack (Operations → Integrations).", style="Card.TLabel", wraplength=820)).grid(row=24, column=0, columnspan=4, sticky="w", pady=(4, 0))

    whatsapp_apply_button = ttk.Button(whatsapp_settings, text="Apply & save settings", command=apply_settings, style="Accent.TButton")
    whatsapp_apply_button.grid(row=25, column=0, sticky="w", pady=(10, 0))
    add_tooltip(whatsapp_apply_button, "Same as the Settings tab Apply: persists every setting from both tabs and saves the WhatsApp destination/keywords files.")
    _track_wraplabel(ttk.Label(whatsapp_settings, text="WhatsApp sending requires 'Enable WAHA sending' (PC_WAHA_ENABLED) and a reachable WAHA server. Source label, destination and keywords are read by the notifier; index alerts sent right after the index scan and item-details follow-up after downloads, when enabled.", style="Card.TLabel", wraplength=820)).grid(row=26, column=0, columnspan=4, sticky="w", pady=(8, 0))

    group_title(whatsapp_settings, 40, "Readability & index source")
    field(whatsapp_settings, 41, 0, "Delay between sends (s):", send_delay_var, 8, "Seconds to pause between consecutive WhatsApp sends so a batch arrives as separate readable messages instead of one burst. 0 disables pacing. Env: PC_WAHA_SEND_DELAY_SECONDS.")
    field(whatsapp_settings, 41, 2, "Digest above N new:", digest_threshold_var, 8, "When one run finds more new records than this, the index alerts collapse into compact digest message(s) (20 records per message); each record still gets its own detail follow-up. 0 = always one message per record. Env: PC_NOTIFY_INDEX_DIGEST_THRESHOLD.")
    field(whatsapp_settings, 42, 0, "Idle status every N hours:", idle_hours_var, 8, "Minimum hours between '⚪ Sin nuevas entradas' idle messages so frequent webhook runs do not repeat it. 0 = send on every idle run. Env: PC_NOTIFY_IDLE_EVERY_HOURS.")
    inline_details_check = ttk.Checkbutton(whatsapp_settings, text="Send each detail message right after its download", variable=notify_details_inline_var, style="Card.TCheckbutton")
    inline_details_check.grid(row=42, column=2, columnspan=2, sticky="w", pady=3)
    add_tooltip(inline_details_check, "Send each record's '📥 Detalles Completos' follow-up inline, right after ITS detail page downloads, so follow-ups arrive naturally spaced across the download phase instead of as one batch at the end. Env: PC_NOTIFY_DETAILS_INLINE. The later MESSAGING step stays as the idempotent catch-up.")
    snapshot_index_check = ttk.Checkbutton(whatsapp_settings, text="AUTO runs import index from changedetection snapshot", variable=index_from_snapshot_var, style="Card.TCheckbutton")
    snapshot_index_check.grid(row=43, column=0, columnspan=4, sticky="w", pady=3)
    add_tooltip(snapshot_index_check, "Webhook (AUTO) runs import the index from the latest changedetection datastore snapshot instead of re-crawling it with Firefox; partial snapshots crawl only the missing group, and any snapshot problem falls back to the full crawler. Manual/restart runs always crawl. Env: PC_INDEX_FROM_SNAPSHOT.")

    add_section_toggle(whatsapp_settings, button_column=3)

    # ---- WhatsApp: Client Profiles ------------------------------------------
    whatsapp_clients = ttk.Frame(whatsapp, style="Card.TFrame", padding=14)
    whatsapp_clients.grid(row=1, column=0, sticky="ew", padx=0, pady=(0, 8))
    whatsapp_clients.columnconfigure(1, weight=1)

    add_section_header(whatsapp_clients, "Client Profiles",
                       "Each client fans a message out to its own WhatsApp destination when the message's phase matches.",
                       columnspan=4, row=0,
                       detail="Fields: name, chat_id (destination), purposes (index/details/status/system/summary or \"all\"), "
                              "filters (keyword rules like the shared ones above, comma-separated), enabled.")
    clients_text = tk.Text(whatsapp_clients, height=12, wrap="word")
    clients_text.grid(row=2, column=0, columnspan=4, sticky="ew", pady=3)
    add_tooltip(
        clients_text,
        'JSON list, e.g. [{"name":"Client A","chat_id":"12036...@g.us","purposes":["index","details"],'
        '"filters":"salud + insumos, -construccion","enabled":true}]',
    )

    def load_clients() -> None:
        path = notify_formats.CLIENTS_PATH
        text = path.read_text(encoding="utf-8", errors="replace").strip() if path.exists() else "[]"
        clients_text.delete("1.0", "end")
        clients_text.insert("1.0", text or "[]")
        button_status_var.set("Loaded WhatsApp client profiles.")

    def save_clients() -> None:
        raw = clients_text.get("1.0", "end").strip("\n") or "[]"
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            button_status_var.set(f"Client profiles NOT saved: invalid JSON ({exc}).")
            return
        if not isinstance(parsed, list):
            button_status_var.set("Client profiles NOT saved: must be a JSON list.")
            return
        normalized = []
        for item in parsed:
            if not isinstance(item, dict):
                button_status_var.set("Client profiles NOT saved: each entry must be an object.")
                return
            chat_id = str(item.get("chat_id") or "").strip()
            if not chat_id:
                continue
            purposes = item.get("purposes") or ["index", "details", "status"]
            if isinstance(purposes, str):
                purposes = [p.strip() for p in purposes.split(",") if p.strip()]
            if not isinstance(purposes, list):
                button_status_var.set("Client profiles NOT saved: purposes must be a list or comma-separated string.")
                return
            normalized.append({
                "name": str(item.get("name") or chat_id).strip(),
                "chat_id": chat_id,
                "purposes": [str(p).strip().lower() for p in purposes if str(p).strip()] or ["index", "details", "status"],
                "filters": str(item.get("filters") or "").strip(),
                "enabled": bool(item.get("enabled", True)),
            })
        path = notify_formats.CLIENTS_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        load_clients()
        button_status_var.set(f"Saved {len(normalized)} WhatsApp client profile(s) to {path.name}.")

    clients_controls = ttk.Frame(whatsapp_clients, style="Card.TFrame")
    clients_controls.grid(row=3, column=0, columnspan=4, sticky="w", pady=3)
    ttk.Button(clients_controls, text="Load", command=load_clients).grid(row=0, column=0, padx=(0, 6))
    ttk.Button(clients_controls, text="Save client profiles", command=save_clients).grid(row=0, column=1)
    load_clients()

    add_section_toggle(whatsapp_clients, button_column=1)

    # ---- WhatsApp: Client Search (contact/group lookup on the WAHA server) ----
    whatsapp_client_search = ttk.Frame(whatsapp, style="Card.TFrame", padding=14)
    whatsapp_client_search.grid(row=2, column=0, sticky="ew", padx=0, pady=(0, 8))
    whatsapp_client_search.columnconfigure(1, weight=1)

    add_section_header(whatsapp_client_search, "Client Search",
                       "Search contacts, groups, channels and communities on WAHA by name or ID (partial names match).",
                       columnspan=5,
                       detail="Leave search empty and click Search to list every known contact/group, or use the Export button to save all as JSON.")

    cs_query_var = tk.StringVar(value="")

    ttk.Label(whatsapp_client_search, text="Name or ID:", style="Card.TLabel").grid(row=2, column=0, sticky="w", padx=(0, 6), pady=3)
    cs_query_entry = ttk.Entry(whatsapp_client_search, textvariable=cs_query_var, width=30)
    cs_query_entry.grid(row=2, column=1, sticky="ew", pady=3)

    cs_matches_cache: list[dict] = []

    def _waha_fetch(base_url: str, api_key: str) -> list[dict]:
        """Fetch all contacts/groups/chats from WAHA, return structured matches."""
        import urllib.request  # noqa: PLC0415 - local WAHA endpoint
        import urllib.error  # noqa: PLC0415

        headers = {"Accept": "application/json"}
        if api_key:
            headers["X-Api-Key"] = api_key

        def _get(path: str, timeout: float = 8.0):
            req = urllib.request.Request(f"{base_url}{path}", headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - local WAHA
                return json.loads(resp.read().decode("utf-8", "replace"))

        sessions = _get("/api/sessions?all=true")
        if not isinstance(sessions, list):
            sessions = []
        matches: list[dict] = []
        seen_ids: set[str] = set()

        for s in sessions:
            if not isinstance(s, dict):
                continue
            sname = str(s.get("name") or "default")
            status = str(s.get("status") or "").upper()
            if status and status not in {"WORKING", "RUNNING", "STARTING"}:
                continue

            # Groups
            try:
                for item in _get(f"/api/{sname}/groups"):
                    if not isinstance(item, dict):
                        continue
                    name = str(item.get("name") or item.get("subject") or "").strip()
                    raw_id = item.get("id")
                    chat_id = str(raw_id.get("_serialized") if isinstance(raw_id, dict) else raw_id or "")
                    if not chat_id or chat_id in seen_ids:
                        continue
                    seen_ids.add(chat_id)
                    matches.append({"name": name or chat_id, "chat_id": chat_id, "session": sname, "kind": "group"})
            except Exception:
                pass

            # Contacts
            try:
                for item in _get(f"/api/contacts/all?session={sname}"):
                    if not isinstance(item, dict):
                        continue
                    name = str(item.get("name") or item.get("pushname") or "").strip()
                    raw_id = item.get("id")
                    chat_id = str(raw_id.get("_serialized") if isinstance(raw_id, dict) else raw_id or "")
                    if not chat_id or chat_id in seen_ids:
                        continue
                    seen_ids.add(chat_id)
                    if not name:
                        continue
                    matches.append({"name": name, "chat_id": chat_id, "session": sname, "kind": "contact"})
            except Exception:
                pass

            # Chats endpoint — catches anything groups/contacts miss
            try:
                for item in _get(f"/api/{sname}/chats"):
                    if not isinstance(item, dict):
                        continue
                    name = str(item.get("name") or "").strip()
                    raw_id = item.get("id")
                    chat_id = str(raw_id.get("_serialized") if isinstance(raw_id, dict) else raw_id or "")
                    if not chat_id or chat_id in seen_ids:
                        continue
                    seen_ids.add(chat_id)
                    kind_label = "group" if "@g.us" in chat_id else "contact" if "@c.us" in chat_id \
                        else "channel" if "@s.whatsapp.net" in chat_id else "community" if "@lid" in chat_id \
                        else "broadcast" if "@newsletter" in chat_id else "chat"
                    matches.append({"name": name or chat_id, "chat_id": chat_id, "session": sname, "kind": kind_label})
            except Exception:
                pass

        matches.sort(key=lambda m: (m["kind"] != "group", pc_common.strip_accents(m["name"]).lower()))
        return matches

    def _format_match(m: dict) -> str:
        return f"{m['name']}  [{m['chat_id']}]  ({m['session']} · {m['kind']})"

    def _on_cs_select(_event: object = None) -> None:
        sel = cs_results_list.curselection()
        if not sel:
            return
        idx = sel[0]
        if idx < len(cs_matches_cache):
            chat_id = cs_matches_cache[idx]["chat_id"]
            cs_results_list.clipboard_clear()
            cs_results_list.clipboard_append(chat_id)
            button_status_var.set(f"Copied: {chat_id}")

    def run_client_search() -> None:
        q = cs_query_var.get().strip()
        base_url = (waha_base_var.get().strip().rstrip("/") or "http://127.0.0.1:3000")
        api_key = waha_server_key_var.get().strip()
        try:
            all_matches = _waha_fetch(base_url, api_key)
        except Exception as exc:
            cs_results_list.delete(0, "end")
            cs_results_list.insert("end", f"WAHA unreachable: {exc}")
            cs_matches_cache.clear()
            return

        if not q:
            filtered = all_matches
        else:
            wanted = pc_common.strip_accents(q).lower()
            filtered = [m for m in all_matches if wanted in pc_common.strip_accents(m["name"]).lower()]

        cs_matches_cache.clear()
        cs_results_list.delete(0, "end")
        if not filtered:
            cs_results_list.insert("end", "No matches found.")
        else:
            for m in filtered:
                cs_results_list.insert("end", _format_match(m))
                cs_matches_cache.append(m)

    def run_client_export() -> None:
        base_url = (waha_base_var.get().strip().rstrip("/") or "http://127.0.0.1:3000")
        api_key = waha_server_key_var.get().strip()
        try:
            all_matches = _waha_fetch(base_url, api_key)
        except Exception as exc:
            cs_results_list.delete(0, "end")
            cs_results_list.insert("end", f"WAHA unreachable for export: {exc}")
            cs_matches_cache.clear()
            return
        formatted = "\n".join(_format_match(m) for m in all_matches)
        path = CONFIG_DIR / "waha_contacts_export.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(formatted + "\n", encoding="utf-8")
        count = len(all_matches)
        run_client_search()
        button_status_var.set(f"Exported {count} entries to {path.relative_to(BASE_DIR)}")

    cs_search_button = ttk.Button(whatsapp_client_search, text="Search", command=run_client_search)
    cs_search_button.grid(row=2, column=2, sticky="w", padx=(6, 0), pady=3)
    cs_query_entry.bind("<Return>", lambda _e: run_client_search())

    cs_export_button = ttk.Button(whatsapp_client_search, text="Export all as TXT", command=run_client_export)
    cs_export_button.grid(row=2, column=3, sticky="w", padx=(6, 0), pady=3)
    add_tooltip(cs_export_button, "Fetches all contacts/groups/channels/communities from WAHA and writes them to data/config/waha_contacts_export.txt.")

    cs_results_list = tk.Listbox(whatsapp_client_search, height=12, exportselection=False)
    cs_results_list.grid(row=3, column=0, columnspan=5, sticky="ew", pady=3)
    cs_results_list.bind("<<ListboxSelect>>", _on_cs_select)
    cs_scrollbar = ttk.Scrollbar(whatsapp_client_search, orient="vertical", command=cs_results_list.yview)
    cs_scrollbar.grid(row=3, column=5, sticky="ns", pady=3)
    cs_results_list.configure(yscrollcommand=cs_scrollbar.set)
    add_tooltip(cs_results_list, "Click an entry to copy its chat ID to the clipboard. Format: Name [chat_id] (session · kind). Empty search = every known entry.")

    add_section_toggle(whatsapp_client_search, button_column=4)

    # ---- WhatsApp: Message Formats ------------------------------------------
    whatsapp_formats = ttk.Frame(whatsapp, style="Card.TFrame", padding=14)
    whatsapp_formats.grid(row=3, column=0, sticky="ew", padx=0, pady=0)
    whatsapp_formats.columnconfigure(1, weight=1)
    whatsapp_formats.columnconfigure(3, weight=1)

    group_title(whatsapp_formats, 0, "Message formats ({placeholder} fields; unknown placeholders stay literal)")
    format_kind_var = tk.StringVar(value="index")
    format_controls = ttk.Frame(whatsapp_formats, style="Card.TFrame")
    format_controls.grid(row=1, column=0, columnspan=4, sticky="w", pady=3)
    ttk.Label(format_controls, text="Format:", style="Card.TLabel").grid(row=0, column=0, padx=(0, 4))
    format_kind_combo = ttk.Combobox(format_controls, textvariable=format_kind_var, values=notify_formats.FORMAT_KINDS, width=12, state="readonly")
    format_kind_combo.grid(row=0, column=1, padx=(0, 10))
    add_tooltip(format_kind_combo, "index = 🔔 alert right after the scan; details = 📥 follow-up with items; status = cambios/cancelaciones/items; system = health/worker/test messages; summary = final run summary per round.")

    format_text = tk.Text(whatsapp_formats, height=12, wrap="word")
    format_text.grid(row=2, column=0, columnspan=4, sticky="ew", pady=3)
    add_tooltip(format_text, "Template with {placeholder} fields: " + " ".join("{" + name + "}" for name in notify_formats.PLACEHOLDERS))
    format_preview = tk.Text(whatsapp_formats, height=12, wrap="word", state="disabled")
    format_preview.grid(row=3, column=0, columnspan=4, sticky="ew", pady=3)

    def _set_preview(text: str) -> None:
        format_preview.configure(state="normal")
        format_preview.delete("1.0", "end")
        format_preview.insert("1.0", text)
        format_preview.configure(state="disabled")

    def load_format(*_a) -> None:
        kind = format_kind_var.get()
        custom = notify_formats.load_custom_format(kind)
        format_text.delete("1.0", "end")
        format_text.insert("1.0", custom or notify_formats.DEFAULT_FORMATS[kind])
        _set_preview(notify_formats.render_format(kind))
        button_status_var.set(f"Loaded {kind} format ({'custom' if custom else 'built-in default'}).")

    def preview_format() -> None:
        _set_preview(notify_formats.render_format(format_kind_var.get(), format_text.get("1.0", "end").strip("\n")))

    def save_format() -> None:
        kind = format_kind_var.get()
        template = format_text.get("1.0", "end").strip("\n")
        if not template.strip():
            button_status_var.set("Template is empty — use Reset to restore the default.")
            return
        path = notify_formats.format_path(kind)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(template + "\n", encoding="utf-8")
        preview_format()
        button_status_var.set(f"Custom {kind} WhatsApp format saved to {path.name}.")

    def reset_format() -> None:
        kind = format_kind_var.get()
        notify_formats.format_path(kind).unlink(missing_ok=True)
        load_format()
        button_status_var.set(f"{kind} WhatsApp format reset to the built-in layout.")

    ttk.Button(format_controls, text="Load", command=load_format).grid(row=0, column=2, padx=(0, 6))
    ttk.Button(format_controls, text="Preview", command=preview_format).grid(row=0, column=3, padx=(0, 6))
    ttk.Button(format_controls, text="Save format", command=save_format).grid(row=0, column=4, padx=(0, 6))
    ttk.Button(format_controls, text="Reset to default", command=reset_format).grid(row=0, column=5)
    format_kind_combo.bind("<<ComboboxSelected>>", load_format)
    load_format()

    add_section_toggle(whatsapp_formats, button_column=3)

    # ========================================================================
    # SCHEDULER TAB - cron-based automatic run scheduling (day pattern + time
    # window + repeat interval), installed as a real crontab entry via
    # src/50_tools/160-manage-cron-schedule.py so no manual `crontab -e` step
    # is needed. Enabling it sets Auto-run source to cron (exclusive with the
    # changedetection webhook, same rule as the existing Settings tab select).
    # ========================================================================
    scheduler = ttk.Frame(scheduler_tab, style="Card.TFrame", padding=14)
    scheduler.grid(row=0, column=0, sticky="ew", padx=6, pady=6)
    scheduler.columnconfigure(1, weight=1)
    scheduler.columnconfigure(3, weight=1)

    add_section_header(scheduler, "Automatic scheduler (cron)", "Install or remove the crontab entry: pick days, time window and repeat interval, then apply.", columnspan=4)
    _track_wraplabel(ttk.Label(
        scheduler,
        text="Runs the collector on a repeating schedule instead of the changedetection webhook trigger. "
             "Enabling this sets Auto-run source to cron and installs a crontab entry; disabling it removes "
             "that entry and switches Auto-run source back to changedetection.",
        style="Card.TLabel", wraplength=820,
    )).grid(row=1, column=0, columnspan=4, sticky="w", pady=(0, 8))

    cron_enabled_var = tk.BooleanVar(value=setting("PC_AUTORUN_SOURCE", "changedetection") == "cron")
    cron_days_var = tk.StringVar(value=setting("PC_CRON_DAYS", "daily"))
    cron_custom_days_var = tk.StringVar(value=setting("PC_CRON_CUSTOM_DAYS", ""))
    cron_start_var = tk.StringVar(value=setting("PC_CRON_START_TIME", "08:00"))
    cron_end_var = tk.StringVar(value=setting("PC_CRON_END_TIME", "18:00"))
    cron_interval_var = tk.StringVar(value=setting("PC_CRON_INTERVAL_MINUTES", "30"))

    cron_enabled_check = ttk.Checkbutton(scheduler, text="Enable scheduled automatic runs", variable=cron_enabled_var, style="Card.TCheckbutton")
    cron_enabled_check.grid(row=2, column=0, columnspan=4, sticky="w", pady=3)

    ttk.Label(scheduler, text="Days:", style="Card.TLabel").grid(row=3, column=0, sticky="nw", pady=3)
    days_frame = ttk.Frame(scheduler, style="Card.TFrame")
    days_frame.grid(row=3, column=1, columnspan=3, sticky="w", pady=3)
    for value, label in (("daily", "Daily"), ("weekdays", "Weekdays (Mon-Fri)"), ("weekends", "Weekends (Sat-Sun)"), ("custom", "Custom")):
        ttk.Radiobutton(days_frame, text=label, value=value, variable=cron_days_var, style="Card.TRadiobutton").pack(side="left", padx=(0, 12))

    ttk.Label(scheduler, text="Custom days (0=Sun..6=Sat):", style="Card.TLabel").grid(row=4, column=0, sticky="w", pady=3)
    custom_days_entry = ttk.Entry(scheduler, textvariable=cron_custom_days_var, width=20)
    custom_days_entry.grid(row=4, column=1, sticky="w", pady=3)
    add_tooltip(custom_days_entry, "Comma-separated day numbers, cron convention: 0=Sunday, 1=Monday, ... 6=Saturday. Only used when Days=Custom.")

    ttk.Label(scheduler, text="Start time (HH:MM):", style="Card.TLabel").grid(row=5, column=0, sticky="w", pady=3)
    start_entry = ttk.Entry(scheduler, textvariable=cron_start_var, width=10)
    start_entry.grid(row=5, column=1, sticky="w", pady=3)
    ttk.Label(scheduler, text="End time (HH:MM):", style="Card.TLabel").grid(row=5, column=2, sticky="e", pady=3)
    end_entry = ttk.Entry(scheduler, textvariable=cron_end_var, width=10)
    end_entry.grid(row=5, column=3, sticky="w", pady=3)

    ttk.Label(scheduler, text="Repeat every (minutes):", style="Card.TLabel").grid(row=6, column=0, sticky="w", pady=3)
    interval_entry = ttk.Entry(scheduler, textvariable=cron_interval_var, width=10)
    interval_entry.grid(row=6, column=1, sticky="w", pady=3)
    add_tooltip(interval_entry, "e.g. 30 = every 30 minutes within the window. Values of 60 or more must be a whole number of hours (60, 120, ...).")

    scheduler_status_var = tk.StringVar(value="")

    def apply_scheduler() -> None:
        updates = {
            "PC_CRON_DAYS": cron_days_var.get(),
            "PC_CRON_CUSTOM_DAYS": cron_custom_days_var.get().strip(),
            "PC_CRON_START_TIME": cron_start_var.get().strip() or "08:00",
            "PC_CRON_END_TIME": cron_end_var.get().strip() or "18:00",
            "PC_CRON_INTERVAL_MINUTES": cron_interval_var.get().strip() or "30",
        }
        merged = load_settings_file()
        merged.update(updates)
        if cron_enabled_var.get():
            merged["PC_AUTORUN_SOURCE"] = "cron"
        elif merged.get("PC_AUTORUN_SOURCE") == "cron":
            merged["PC_AUTORUN_SOURCE"] = "changedetection"
        lines = [
            "# PanamaCompra monitor settings (KEY=VALUE).",
            "# Edited from the monitor Settings panel; same-named environment",
            "# variables override these at startup.",
        ]
        lines += [f"{key}={shlex.quote(str(merged[key]))}" for key in sorted(merged)]
        SETTINGS_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
        _SETTINGS_FILE.clear()
        _SETTINGS_FILE.update(merged)

        script = str(BASE_DIR / "src/50_tools/160-manage-cron-schedule.py")
        if not cron_enabled_var.get():
            result = subprocess.run([script, "remove"], cwd=BASE_DIR, env=monitor_env(), capture_output=True, text=True)
            scheduler_status_var.set(result.stdout.strip() or result.stderr.strip() or "Schedule disabled.")
            return

        args = [script, "install", "--days", cron_days_var.get(), "--custom-days", cron_custom_days_var.get().strip(),
                "--start", cron_start_var.get().strip() or "08:00", "--end", cron_end_var.get().strip() or "18:00",
                "--interval", cron_interval_var.get().strip() or "30"]
        result = subprocess.run(args, cwd=BASE_DIR, env=monitor_env(), capture_output=True, text=True)
        if result.returncode != 0:
            scheduler_status_var.set(f"NOT applied: {result.stderr.strip() or result.stdout.strip()}")
        else:
            scheduler_status_var.set(result.stdout.strip())

    def refresh_scheduler_status() -> None:
        script = str(BASE_DIR / "src/50_tools/160-manage-cron-schedule.py")
        result = subprocess.run([script, "show"], cwd=BASE_DIR, env=monitor_env(), capture_output=True, text=True)
        scheduler_status_var.set(f"Currently installed: {result.stdout.strip()}")

    apply_scheduler_button = ttk.Button(scheduler, text="Save & Apply schedule", command=apply_scheduler, style="Accent.TButton")
    apply_scheduler_button.grid(row=7, column=0, sticky="w", pady=(8, 0))
    ttk.Button(scheduler, text="Refresh status", command=refresh_scheduler_status).grid(row=7, column=1, sticky="w", pady=(8, 0))
    ttk.Label(scheduler, textvariable=scheduler_status_var, style="Card.TLabel", wraplength=820).grid(row=8, column=0, columnspan=4, sticky="w", pady=(8, 0))
    refresh_scheduler_status()
    add_section_toggle(scheduler, button_column=3)

    # ========================================================================
    # OPERATIONS TAB / LIVE DIAGNOSTICS - Phase, Mode, Item, Started, etc.
    # ========================================================================
    diag = ttk.Frame(ops_tab, style="Card.TFrame", padding=14)
    diag.grid(row=1, column=0, sticky="ew", padx=6, pady=6)
    # Keep the two label columns narrow and let the two value columns absorb the
    # remaining width, so large counters and long descriptions stay readable.
    diag.columnconfigure(0, weight=0, minsize=130)
    diag.columnconfigure(1, weight=1, minsize=200)
    diag.columnconfigure(2, weight=0, minsize=130)
    diag.columnconfigure(3, weight=1, minsize=200)

    add_section_header(diag, "Live diagnostics", "Process health and the most recent worker / webhook log lines.", columnspan=4)

    # Fields are grouped left-to-right, top-to-bottom: lifecycle, progress,
    # timing, then record counters. "Extra" is rendered separately on its own
    # full-width row because it can hold a long human-readable note.
    fields = [
        ("Phase", "PHASE"), ("Status", "STATUS"),
        ("Mode", "MODE"), ("ETA", "ETA"), ("Index page cap", "INDEX_LIMIT"), ("Detail limit", "DETAIL_LIMIT"),
        ("Step", "STEP"), ("Item", "ITEM"),
        ("Started", "STARTED_AT"), ("Updated", "UPDATED_AT"),
        ("Found", "RECORDS_FOUND"), ("New", "RECORDS_NEW"),
        ("Existing", "RECORDS_EXISTING"), ("Saved/skipped", "RECORDS_SAVED"),
        ("Failures", "RECORDS_FAILED"), ("Pending", "RECORDS_PENDING"),
        ("Test", "RECORDS_TEST"),
    ]
    # Values are read-only Entry widgets (not Labels) so the operator can select
    # and copy any phase/count/timestamp for further actions; readonly keeps them
    # uneditable while still selectable. Tighter pady reduces the old line crowd.
    diag_vars: dict[str, tk.StringVar] = {}
    for idx, (label, key) in enumerate(fields):
        row = idx // 2 + 1  # row 0 holds the section title
        col = (idx % 2) * 2
        ttk.Label(diag, text=f"{label}:", style="Card.TLabel").grid(row=row, column=col, sticky="w", padx=(0, 6), pady=1)
        var = tk.StringVar(value="-")
        diag_vars[key] = var
        ttk.Entry(diag, textvariable=var, state="readonly").grid(row=row, column=col + 1, sticky="ew", pady=1, padx=(0, 8))

    extra_row = len(fields) // 2 + 2
    ttk.Label(diag, text="Extra:", style="Card.TLabel").grid(row=extra_row, column=0, sticky="w", padx=(0, 6), pady=1)
    extra_var = tk.StringVar(value="-")
    diag_vars["EXTRA"] = extra_var
    ttk.Entry(diag, textvariable=extra_var, state="readonly").grid(row=extra_row, column=1, columnspan=3, sticky="ew", pady=1, padx=(0, 8))
    add_section_toggle(diag, button_column=3)

    # ========================================================================
    # RECORDS TAB / RECORDS PENDING / COMPLETED - readable run counters plus the
    # previous database snapshot directly after the live records-completed view.
    # ========================================================================
    records_overview = ttk.Frame(records_tab, style="Card.TFrame", padding=14)
    records_overview.grid(row=0, column=0, sticky="ew", padx=6, pady=6)
    for col in range(2):
        records_overview.columnconfigure(col, weight=1, uniform="record_overview")
    add_section_header(records_overview, "Records summary counters", "Archive totals by detail status and deadline window.", columnspan=2)

    pending_var = tk.StringVar(value="Pending records: —")
    completed_var = tk.StringVar(value="Completed records: —")
    # Stacked full-width for readability: the red Pending card sits directly above
    # the green Completed card (one after another), each stretched across the row.
    pending_card = tk.Label(records_overview, textvariable=pending_var, anchor="nw", justify="left",
                            bg="#3f1d1d", fg="#fecaca", padx=12, pady=10, font=("Sans", 11, "bold"))
    pending_card.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 8))
    completed_card = tk.Label(records_overview, textvariable=completed_var, anchor="nw", justify="left",
                              bg="#14532d", fg="#bbf7d0", padx=12, pady=10, font=("Sans", 11, "bold"))
    completed_card.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(0, 8))
    previous_db_text = tk.Text(records_overview, height=7, wrap="word", bd=0, highlightthickness=0,
                               bg="#0b1220", fg="#e5e7eb", insertbackground="#e5e7eb", font=("Sans", 9))
    previous_db_text.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(0, 8))

    def set_previous_db_text(value: str) -> None:
        previous_db_text.configure(state="normal")
        previous_db_text.delete("1.0", "end")
        previous_db_text.insert("1.0", value)
        previous_db_text.configure(state="disabled")

    def update_records_overview(progress: dict[str, str]) -> None:
        pending = progress.get("RECORDS_PENDING", "-")
        saved = progress.get("RECORDS_SAVED", "-")
        failed = progress.get("RECORDS_FAILED", "-")
        found = progress.get("RECORDS_FOUND", "-")
        new = progress.get("RECORDS_NEW", "-")
        existing = progress.get("RECORDS_EXISTING", "-")
        pending_var.set(f"Records Pendings\n{pending} waiting for detail/download\nFound: {found} · New: {new} · Existing: {existing}")
        s = db_review_stats()
        end_dates = "; ".join(
            f"{rec['numero']} ends {compact_dt(rec['finish_date_guess']) or 'no date'}"
            for rec in s.get("completed_recent", [])[:3]
        ) or "No completed end dates yet"
        completed_var.set(f"Records Completed\nSaved/skipped: {saved}\nFailures needing review: {failed}\nOpportunity ends: {end_dates}")
        if not s.get("db_exists"):
            set_previous_db_text("Previous database data: no archive database yet.")
            return
        recent = "\n".join(
            f"  • {rec['numero']} [{rec['detail_status'] or 'unknown'}] — {rec['descripcion'][:90]}"
            for rec in s.get("recent", [])[:8]
        ) or "  • —"
        groups = ", ".join(f"{row['grupo']}: {row['count']}" for row in s.get("groups", [])) or "—"
        statuses = ", ".join(f"{row['status']}: {row['count']}" for row in s.get("status_breakdown", [])) or "—"
        columns = ", ".join(f"{col['name']}[{col['type'] or 'TEXT'}]={col['nonempty']}" for col in s.get("columns", [])[:24]) or "—"
        set_previous_db_text(
            "Previous database data / all records summary\n"
            f"Total: {s['total']} · Completed(saved): {s['saved']} · Pending: {s['pending']} · Failed: {s['failed']}\n"
            f"Pending snapshot: {s['new_records']} · Completed snapshot: {s['existing_records']} · Detail JSON: {s['with_detail_json']} · Notified: {s['notified']}\n"
            f"Awaiting WhatsApp index alert: {s['notify_backlog']} · Awaiting item-details WhatsApp: {s['detail_notify_backlog']} · Needs deadline repair: {s['needs_deadline']}\n"
            f"Detail statuses: {statuses}\n"
            f"Groups: {groups}\n"
            f"DB elements/columns with data: {columns}\n"
            f"Most recent records:\n{recent}"
        )

    add_section_toggle(records_overview, button_column=1)

    # ========================================================================
    # KPIS TAB - every KPI in one dedicated tab: the decision cards, an items
    # analysis line, trend/status text, plus drawn diagrams about the collected
    # groups, contracting entities, locations and monthly intake so the numbers
    # point at a decision. Mirrors the web monitor's KPIs tab.
    # ========================================================================
    kpi_frame = ttk.Frame(kpi_tab, style="Card.TFrame", padding=14)
    kpi_frame.grid(row=0, column=0, sticky="ew", padx=6, pady=6)
    for col in range(3):
        kpi_frame.columnconfigure(col, weight=1, uniform="kpi")
    add_section_header(kpi_frame, "KPI dashboard", "Key indicators from the archive database; use the filters to narrow the period and group.", columnspan=3)

    # Dashboard filters: every card, line and diagram below answers for the
    # same slice (time window, index group, contracting entity).
    kpi_filters = ttk.Frame(kpi_frame, style="Card.TFrame")
    kpi_filters.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(0, 8))
    KPI_DAY_CHOICES = {"All time": 0, "Last 7 days": 7, "Last 30 days": 30, "Last 90 days": 90, "Last year": 365}
    kpi_days_var = tk.StringVar(value="All time")
    kpi_grupo_var = tk.StringVar(value="")
    kpi_entidad_var = tk.StringVar(value="")
    ttk.Label(kpi_filters, text="Window:", style="Card.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 4))
    kpi_days_box = ttk.Combobox(kpi_filters, textvariable=kpi_days_var, values=tuple(KPI_DAY_CHOICES), width=12, state="readonly")
    kpi_days_box.grid(row=0, column=1, sticky="w", padx=(0, 10))
    ttk.Label(kpi_filters, text="Group:", style="Card.TLabel").grid(row=0, column=2, sticky="w", padx=(0, 4))
    kpi_grupo_box = ttk.Combobox(kpi_filters, textvariable=kpi_grupo_var, values=(), width=14)
    kpi_grupo_box.grid(row=0, column=3, sticky="w", padx=(0, 10))
    ttk.Label(kpi_filters, text="Entity:", style="Card.TLabel").grid(row=0, column=4, sticky="w", padx=(0, 4))
    kpi_entidad_box = ttk.Combobox(kpi_filters, textvariable=kpi_entidad_var, values=(), width=28)
    kpi_entidad_box.grid(row=0, column=5, sticky="w", padx=(0, 10))
    add_tooltip(kpi_days_box, "Restrict every KPI card and diagram to records first seen inside this window.")
    add_tooltip(kpi_grupo_box, "Restrict the dashboard to one index group (pick from the list or type; blank = all groups).")
    add_tooltip(kpi_entidad_box, "Restrict the dashboard to one contracting entity (pick or type; blank = all entities).")

    def kpi_filter_args() -> dict[str, object]:
        return {
            "days": KPI_DAY_CHOICES.get(kpi_days_var.get(), 0),
            "grupo": kpi_grupo_var.get().strip(),
            "entidad": kpi_entidad_var.get().strip(),
        }

    def apply_kpi_filters() -> None:
        update_kpi_dashboard(parse_progress_file())

    def reset_kpi_filters() -> None:
        kpi_days_var.set("All time")
        kpi_grupo_var.set("")
        kpi_entidad_var.set("")
        apply_kpi_filters()

    kpi_apply_button = ttk.Button(kpi_filters, text="Apply filters", command=apply_kpi_filters, style="Accent.TButton")
    kpi_apply_button.grid(row=0, column=6, sticky="w", padx=(0, 6))
    ttk.Button(kpi_filters, text="Reset", command=reset_kpi_filters).grid(row=0, column=7, sticky="w")
    kpi_days_box.bind("<<ComboboxSelected>>", lambda _e: apply_kpi_filters())
    kpi_filter_state_var = tk.StringVar(value="Showing all records")
    ttk.Label(kpi_filters, textvariable=kpi_filter_state_var, style="Card.TLabel").grid(row=0, column=8, sticky="w", padx=(10, 0))

    kpi_guide_text = (
        "How to use this tab: 1) Fix failed detail downloads first. "
        "2) If pending details grows, prioritize detail capacity over more index pages. "
        "3) Repair missing deadlines before calendar/export decisions. "
        "4) If WAHA backlog grows, verify destinations and WAHA status before scanning more."
    )
    kpi_vars = {
        "index": tk.StringVar(value="Index scan: —"),
        "details": tk.StringVar(value="Detail queue: —"),
        "whatsapp": tk.StringVar(value="WhatsApp: —"),
        "items": tk.StringVar(value="Items analysis: —"),
        "trend": tk.StringVar(value="Trend: —"),
        "lastrun": tk.StringVar(value="Last run: no completed run recorded yet."),
        "decision": tk.StringVar(value="Decision signals loading…"),
    }
    kpi_colors = {"index": ("#172554", "#bfdbfe"), "details": ("#064e3b", "#bbf7d0"), "whatsapp": ("#3b0764", "#e9d5ff")}
    for idx, key in enumerate(("index", "details", "whatsapp")):
        bg, fg = kpi_colors[key]
        tk.Label(kpi_frame, textvariable=kpi_vars[key], anchor="nw", justify="left", bg=bg, fg=fg,
                 padx=12, pady=10, font=("Sans", 10, "bold")).grid(row=2, column=idx, sticky="nsew", padx=4, pady=(0, 8))
    ttk.Label(kpi_frame, textvariable=kpi_vars["items"], style="Card.TLabel", justify="left", wraplength=900).grid(row=3, column=0, columnspan=3, sticky="w", pady=(0, 4))
    ttk.Label(kpi_frame, textvariable=kpi_vars["trend"], style="Card.TLabel", justify="left").grid(row=4, column=0, columnspan=3, sticky="w", pady=(0, 4))
    ttk.Label(kpi_frame, textvariable=kpi_vars["lastrun"], style="Card.TLabel", justify="left", wraplength=900).grid(row=5, column=0, columnspan=3, sticky="w", pady=(0, 4))
    ttk.Label(kpi_frame, textvariable=kpi_vars["decision"], style="Card.TLabel", justify="left", wraplength=900).grid(row=6, column=0, columnspan=3, sticky="w")
    ttk.Label(kpi_frame, text=kpi_guide_text, style="Card.TLabel", justify="left", wraplength=900).grid(row=7, column=0, columnspan=3, sticky="w", pady=(6, 0))
    add_section_toggle(kpi_frame, button_column=2)

    # KPI diagrams: horizontal bar charts drawn on plain Tk canvases about the
    # collected data — index groups, contracting entities, locations parsed
    # from the detail pages, and the monthly intake trend.
    kpi_charts = ttk.Frame(kpi_tab, style="Card.TFrame", padding=14)
    kpi_charts.grid(row=1, column=0, sticky="ew", padx=6, pady=6)
    for col in range(2):
        kpi_charts.columnconfigure(col, weight=1, uniform="kpi_chart")
    add_section_header(kpi_charts, "KPI diagrams", "Groups · entities · locations · monthly trend.", columnspan=2)

    CHART_HEIGHT = 176

    def make_kpi_chart(title: str, grid_row: int, grid_col: int) -> tk.Canvas:
        holder = ttk.Frame(kpi_charts, style="Card.TFrame")
        holder.grid(row=grid_row, column=grid_col, sticky="nsew", padx=4, pady=4)
        holder.columnconfigure(0, weight=1)
        ttk.Label(holder, text=title, style="Message.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 2))
        chart = tk.Canvas(holder, height=CHART_HEIGHT, bg="#020617", highlightthickness=1,
                          highlightbackground="#334155")
        chart.grid(row=1, column=0, sticky="ew")
        return chart

    groups_chart = make_kpi_chart("Index groups", 1, 0)
    entities_chart = make_kpi_chart("Top contracting entities", 1, 1)
    locations_chart = make_kpi_chart("Locations / buying units (from details)", 2, 0)
    trend_chart = make_kpi_chart("Monthly intake trend", 2, 1)
    daily_chart = make_kpi_chart("Daily intake (last 14 days)", 3, 0)
    top_items_chart = make_kpi_chart("Most frequent items", 3, 1)
    ttk.Label(kpi_charts, text="Latest parsed items", style="Message.TLabel").grid(row=4, column=0, columnspan=2, sticky="w", pady=(8, 2))
    latest_items_box = tk.Text(kpi_charts, height=7, wrap="none", bd=0, highlightthickness=1,
                               highlightbackground="#334155", bg="#020617", fg="#cbd5e1",
                               insertbackground="#e5e7eb", font=("monospace", 8))
    latest_items_box.grid(row=5, column=0, columnspan=2, sticky="ew", padx=4)
    latest_items_box.configure(state="disabled")
    add_section_toggle(kpi_charts, button_column=1)

    def draw_bars(chart: tk.Canvas, rows: list[tuple[str, int]], color: str = "#38bdf8") -> None:
        chart.delete("all")
        rows = [(str(label), int(count)) for label, count in rows if str(label).strip()][:8]
        if not rows:
            chart.create_text(10, 16, anchor="w", fill="#94a3b8", font=("Sans", 9),
                              text="No data yet — run a collection first.")
            return
        width = chart.winfo_width()
        if width <= 1:
            width = 430
        label_width = 170
        bar_area = max(60, width - label_width - 56)
        max_count = max(count for _, count in rows) or 1
        y = 8
        for label, count in rows:
            chart.create_text(8, y + 7, anchor="w", fill="#e5e7eb", font=("Sans", 8), text=label[:30])
            bar_width = max(3, int(bar_area * count / max_count))
            chart.create_rectangle(label_width, y, label_width + bar_width, y + 14, fill=color, width=0)
            chart.create_text(label_width + bar_width + 6, y + 7, anchor="w", fill="#94a3b8",
                              font=("Sans", 8), text=str(count))
            y += 20

    def update_kpi_charts(s: dict[str, object]) -> None:
        items = s.get("item_analysis", {}) if isinstance(s.get("item_analysis"), dict) else {}
        draw_bars(groups_chart, [(row.get("grupo", ""), row.get("count", 0)) for row in s.get("groups", [])])
        draw_bars(entities_chart, [(row.get("label", ""), row.get("count", 0)) for row in s.get("entities", [])], color="#22c55e")
        locations = items.get("top_locations") or []
        if not locations:
            # Before any detail pages carry a Lugar/Provincia field, fall back to
            # the index "dependencia" column so the location chart still informs.
            locations = s.get("dependencias", [])
        draw_bars(locations_chart, [(row.get("label", ""), row.get("count", 0)) for row in locations], color="#facc15")
        # Oldest→newest so the trend reads left/top to bottom chronologically.
        draw_bars(trend_chart, [(row.get("label", ""), row.get("count", 0)) for row in reversed(list(s.get("monthly_trend", [])))], color="#a78bfa")
        draw_bars(daily_chart, [(row.get("label", ""), row.get("count", 0)) for row in s.get("daily_intake", [])], color="#38bdf8")
        draw_bars(top_items_chart, [(row.get("label", ""), row.get("count", 0)) for row in items.get("top_items", [])], color="#f472b6")
        latest = items.get("sample_items", []) or []
        latest_lines = [
            f"{row.get('numero', '?'):<22} {row.get('saved_at', ''):<17} qty {row.get('cantidad') or '-':<8} {row.get('descripcion') or '(item without description)'}"
            for row in latest
        ] or ["(no parsed items yet — run a collection with detail downloads)"]
        latest_items_box.configure(state="normal")
        latest_items_box.delete("1.0", "end")
        latest_items_box.insert("1.0", "\n".join(latest_lines))
        latest_items_box.configure(state="disabled")

    def update_kpi_dashboard(progress: dict[str, str]) -> None:
        filters = kpi_filter_args()
        s = db_review_stats(**filters)
        # Keep the selector suggestion lists in sync with the current slice so
        # drilling down (pick group → see its entities) stays easy.
        kpi_grupo_box["values"] = tuple(row.get("grupo", "") for row in s.get("groups", []))
        kpi_entidad_box["values"] = tuple(row.get("label", "") for row in s.get("entities", []))
        active_parts = []
        if filters["days"]:
            active_parts.append(f"last {filters['days']} days")
        if filters["grupo"]:
            active_parts.append(f"group {filters['grupo']}")
        if filters["entidad"]:
            active_parts.append(f"entity {filters['entidad']}")
        kpi_filter_state_var.set(("Filtered: " + " · ".join(active_parts)) if active_parts else "Showing all records")
        total = int(s.get("total") or 0)
        saved = int(s.get("saved") or 0)
        pending = int(s.get("pending") or 0)
        failed = int(s.get("failed") or 0)
        detail_json = int(s.get("with_detail_json") or 0)
        closure = round((saved / total) * 100) if total else 0
        kpi_vars["index"].set(
            "Index scan KPIs\n"
            f"Found now: {progress.get('RECORDS_FOUND', '-')} · New: {progress.get('RECORDS_NEW', '-')} · Existing: {progress.get('RECORDS_EXISTING', '-')}\n"
            f"Archive total: {total} · New today: {s.get('new_today', 0)} · Notify backlog: {s.get('notify_backlog', 0)}\n"
            f"Abiertas: {s.get('abiertas', 0)} · Programadas: {s.get('programadas', 0)} · Closing ≤{s.get('soon_days', 7)}d: {s.get('closing_soon', 0)}"
        )
        kpi_vars["details"].set(
            "Detail download KPIs\n"
            f"Pending: {pending} · Saved: {saved} · Failed: {failed}\n"
            f"Detail JSON coverage: {detail_json}/{total or 0} · Closure: {closure}%"
        )
        kpi_vars["whatsapp"].set(
            "Decision delivery KPIs\n"
            f"Index alerts sent: {s.get('notified', 0)} · Failed alerts: {s.get('alerts_failed', 0)}\n"
            f"Details follow-up backlog: {s.get('detail_notify_backlog', 0)} · Needs deadline repair: {s.get('needs_deadline', 0)}"
        )
        items = s.get("item_analysis", {}) if isinstance(s.get("item_analysis"), dict) else {}
        max_item_record = items.get("max_items_record", {}) if isinstance(items.get("max_items_record"), dict) else {}
        item_keywords = " · ".join(f"{row.get('label')}: {row.get('count')}" for row in items.get("top_item_keywords", [])[:6]) or "no item keywords yet"
        kpi_vars["items"].set(
            "Items analysis KPIs\n"
            f"Parsed item lines: {items.get('total_items', 0)} · Records with items: {items.get('records_with_items', 0)}/{items.get('sampled_records', 0)} · Avg items/record: {items.get('avg_items_per_record', 0)}\n"
            f"Largest record: {max_item_record.get('numero', '-')} ({max_item_record.get('count', 0)} items) · Top item keywords: {item_keywords}"
        )
        trend = " · ".join(f"{row['label']}: {row['count']}" for row in s.get("monthly_trend", [])[:6]) or "no monthly trend yet"
        statuses = " · ".join(f"{row['status']}: {row['count']}" for row in s.get("status_breakdown", [])) or "no status data"
        kpi_vars["trend"].set(f"Trend windows: {trend}\nDetail status mix: {statuses}")
        last = read_last_summary()
        if last.get("FINISHED_AT"):
            stage_parts = " · ".join(
                f"{label} {last.get(key)}s" for label, key in (
                    ("update", "UPDATE_SECONDS"), ("index", "INDEX_SECONDS"), ("messaging", "MESSAGING_SECONDS"),
                    ("details", "DETAIL_SECONDS"), ("views", "VIEW_SECONDS"), ("verify", "VERIFY_SECONDS"),
                    ("calendar", "CALENDAR_SECONDS"),
                ) if str(last.get(key) or "").strip() not in {"", "0"}
            )
            kpi_vars["lastrun"].set(
                f"Last run: finished {compact_dt(last.get('FINISHED_AT') or '')} · duration {last.get('TOTAL_TEXT') or last.get('TOTAL_SECONDS', '?') + 's'}"
                f" · index source: {last.get('INDEX_SOURCE') or 'crawler'}"
                + (f"\nStages: {stage_parts}" if stage_parts else "")
            )
        else:
            kpi_vars["lastrun"].set("Last run: no completed run recorded yet — stage durations appear after the first clean run.")
        entities = s.get("entities", []) or []
        top_entity = entities[0] if entities else {}
        decision_extra = (
            f" Most active entity: {top_entity.get('label', '-')} ({top_entity.get('count', 0)} records) — focus review capacity where the volume is."
            if top_entity else ""
        )
        kpi_vars["decision"].set(
            "Decision focus: clear failed detail downloads first; repair missing deadlines before calendar/export decisions; "
            "if index notify backlog grows, verify WAHA/settings before running more scans; if item keywords cluster around specific products/buyers, prioritize those folders for review and detail follow-up; if pending details grows, prioritize detail worker capacity over more index pages."
            + decision_extra
        )
        update_kpi_charts(s)


    def make_status_browser(title: str, detail_status: str, row: int) -> None:
        frame = ttk.Frame(records_tab, style="Card.TFrame", padding=14)
        frame.grid(row=row, column=0, sticky="ew", padx=6, pady=6)
        frame.columnconfigure(1, weight=1)
        ttk.Label(frame, text=title, style="Title.TLabel").grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 8))
        filter_var = tk.StringVar(value="")
        order_var = tk.StringVar(value="Newest first")
        ttk.Label(frame, text="Search:", style="Card.TLabel").grid(row=1, column=0, sticky="w", padx=(0, 6))
        filter_entry = ttk.Entry(frame, textvariable=filter_var)
        filter_entry.grid(row=1, column=1, sticky="ew", pady=3)
        ttk.Label(frame, text="Order:", style="Card.TLabel").grid(row=1, column=2, sticky="e", padx=(8, 6))
        order_box = ttk.Combobox(frame, textvariable=order_var, values=("Newest first", "Oldest first"), width=13, state="readonly")
        order_box.grid(row=1, column=3, sticky="w", pady=3)
        listbox = tk.Listbox(frame, height=6, activestyle="none", exportselection=False,
                             bg="#020617", fg="#e5e7eb", selectbackground="#2563eb",
                             selectforeground="#ffffff", highlightthickness=0, borderwidth=0, font=("Sans", 9))
        listbox.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(4, 6))
        records: list[dict[str, str]] = []

        def label(rec: dict[str, str]) -> str:
            return f"{rec.get('numero') or '(sin número)'} — starts {start_text(rec)} — ends {deadline_text(rec)} — {rec.get('descripcion') or '(sin descripción)'}"

        def selected() -> dict[str, str] | None:
            sel = listbox.curselection()
            return records[sel[0]] if sel else None

        def refresh_list(*_args: object) -> None:
            nonlocal records
            needle = filter_var.get().strip().lower()
            records = [
                rec for rec in load_record_index(limit=1000)
                if (rec.get("detail_status") or "").lower() == detail_status
                and (not needle or needle in label(rec).lower())
            ]
            newest_first = order_var.get() != "Oldest first"
            records.sort(key=lambda rec: parse_record_order_date(rec) or datetime.min, reverse=newest_first)
            listbox.delete(0, "end")
            for rec in records:
                listbox.insert("end", label(rec))
            if records:
                listbox.selection_set(0)

        def open_folder_for_selection() -> None:
            rec = selected()
            if not rec:
                button_status_var.set(f"Select a record in {title} first.")
                return
            folder = rec.get("record_folder") or ""
            if not folder or not Path(folder).exists():
                button_status_var.set(f"Record folder not found for {rec.get('numero') or 'selection'}.")
                return
            subprocess.Popen([os.environ.get("PC_OPEN_FOLDER_COMMAND", "xdg-open"), folder], cwd=BASE_DIR, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        def open_portal_for_selection() -> None:
            rec = selected()
            if rec and rec.get("link"):
                subprocess.Popen(["xdg-open", rec["link"]], cwd=BASE_DIR, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                button_status_var.set(f"No portal link for the selected {title} record.")

        refresh_btn = ttk.Button(frame, text="Refresh", command=refresh_list)
        refresh_btn.grid(row=3, column=0, sticky="w", padx=(0, 8))
        folder_btn = ttk.Button(frame, text="Open folder", command=open_folder_for_selection)
        folder_btn.grid(row=3, column=1, sticky="w", padx=(0, 8))
        portal_btn = ttk.Button(frame, text="Open portal", command=open_portal_for_selection)
        portal_btn.grid(row=3, column=2, sticky="w", padx=(0, 8))
        add_tooltip(filter_entry, f"Filter records in {title} by NUMERO, deadline or description.")
        add_tooltip(order_box, f"Order {title} by when the record was first seen/downloaded locally.")
        filter_var.trace_add("write", refresh_list)
        order_var.trace_add("write", refresh_list)
        refresh_list()
        add_section_toggle(frame, button_column=3)

    make_status_browser("Records Pendings", "pending", 1)
    make_status_browser("Records Completed", "saved", 2)

    # ========================================================================
    # RECORDS TAB / RECORD INDEX - pick a collected record by NUMERO +
    # description and open its archive folder or the portal page. Populated
    # read-only from data/panamacompra_archive.db; empty until the collector runs.
    # ========================================================================
    record_index = ttk.Frame(records_tab, style="Card.TFrame", padding=14)
    record_index.grid(row=3, column=0, sticky="ew", padx=6, pady=6)
    record_index.columnconfigure(1, weight=1)

    add_section_header(record_index, "Record selector and filters", "Filter first, then Ctrl/Shift-select records to notify, import calendars or copy templates.", columnspan=3)

    # The folder selector is a type-to-filter box plus a dedicated, self-scrolling
    # list (with its own scrollbar) instead of a dropdown. A dropdown's popup
    # scroll fought the whole-page scroll and the long "NUMERO — description"
    # entries were impossible to separate; this list scrolls on its own (see the
    # Listbox branch in on_mousewheel) and the filter box narrows it instantly.
    index_records: list[dict[str, str]] = []
    index_filtered: list[dict[str, str]] = []
    index_filter_var = tk.StringVar(value="")
    index_status_var = tk.StringVar(value="All")
    index_detail_status_var = tk.StringVar(value="All")
    index_order_var = tk.StringVar(value="Newest first")
    index_order_field_var = tk.StringVar(value="Downloaded date")
    index_mindate_var = tk.StringVar(value="")
    index_maxdate_var = tk.StringVar(value="")
    index_start_mindate_var = tk.StringVar(value="")
    index_start_maxdate_var = tk.StringVar(value="")
    index_downloaded_mindate_var = tk.StringVar(value="")
    index_downloaded_maxdate_var = tk.StringVar(value="")
    index_detail_var = tk.StringVar(value="No records collected yet. Run the collector, then click Refresh list.")

    ttk.Label(record_index, text="Search NUMERO / description:", style="Card.TLabel").grid(row=1, column=0, sticky="w", padx=(0, 6))
    index_filter_entry = ttk.Entry(record_index, textvariable=index_filter_var)
    index_filter_entry.grid(row=1, column=1, columnspan=2, sticky="ew", pady=3)
    add_tooltip(index_filter_entry, "Type any part of a NUMERO or description to narrow the list below.")

    # Dates selector: a deadline-status filter plus from/to pickers for the
    # deadline (DTEND), the start date (DTSTART) and the local download date. Each
    # accepts a plain date (YYYY-MM-DD) or a date and time (YYYY-MM-DD HH:MM); a
    # bare date used as an upper bound covers the whole day. Each list row shows
    # its downloaded date, DTSTART and DTEND, colored red (expired), amber (next
    # to expire) or green (upcoming) so it is obvious which records are actionable.
    dates_row = ttk.Frame(record_index, style="Card.TFrame")
    dates_row.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(0, 4))
    ttk.Label(dates_row, text="Deadline:", style="Card.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 6))
    index_status_box = ttk.Combobox(dates_row, textvariable=index_status_var, values=STATUS_FILTER_CHOICES, width=22, state="readonly")
    index_status_box.grid(row=0, column=1, sticky="w", padx=(0, 12))
    ttk.Label(dates_row, text="Detail status:", style="Card.TLabel").grid(row=0, column=2, sticky="e", padx=(0, 6))
    index_detail_status_box = ttk.Combobox(dates_row, textvariable=index_detail_status_var, values=DETAIL_STATUS_FILTER_CHOICES, width=18, state="readonly")
    index_detail_status_box.grid(row=0, column=3, sticky="w", padx=(0, 12))
    ttk.Label(dates_row, text="Order by:", style="Card.TLabel").grid(row=0, column=4, sticky="e", padx=(0, 6))
    index_order_field_box = ttk.Combobox(dates_row, textvariable=index_order_field_var, values=("Downloaded date", "End date", "Start date"), width=15, state="readonly")
    index_order_field_box.grid(row=0, column=5, sticky="w", padx=(0, 8))
    index_order_box = ttk.Combobox(dates_row, textvariable=index_order_var, values=("Newest first", "Oldest first"), width=13, state="readonly")
    index_order_box.grid(row=0, column=6, sticky="w", padx=(0, 8))

    ttk.Label(dates_row, text="DTEND on/after:", style="Card.TLabel").grid(row=1, column=0, sticky="e", padx=(0, 6))
    index_mindate_entry = ttk.Entry(dates_row, textvariable=index_mindate_var, width=16)
    index_mindate_entry.grid(row=1, column=1, sticky="w", padx=(0, 12))
    ttk.Label(dates_row, text="DTEND on/before:", style="Card.TLabel").grid(row=1, column=2, sticky="e", padx=(0, 6))
    index_maxdate_entry = ttk.Entry(dates_row, textvariable=index_maxdate_var, width=16)
    index_maxdate_entry.grid(row=1, column=3, sticky="w", padx=(0, 8))

    ttk.Label(dates_row, text="DTSTART on/after:", style="Card.TLabel").grid(row=2, column=0, sticky="e", padx=(0, 6))
    index_start_mindate_entry = ttk.Entry(dates_row, textvariable=index_start_mindate_var, width=16)
    index_start_mindate_entry.grid(row=2, column=1, sticky="w", padx=(0, 12))
    ttk.Label(dates_row, text="DTSTART on/before:", style="Card.TLabel").grid(row=2, column=2, sticky="e", padx=(0, 6))
    index_start_maxdate_entry = ttk.Entry(dates_row, textvariable=index_start_maxdate_var, width=16)
    index_start_maxdate_entry.grid(row=2, column=3, sticky="w", padx=(0, 8))

    ttk.Label(dates_row, text="Downloaded on/after:", style="Card.TLabel").grid(row=3, column=0, sticky="e", padx=(0, 6))
    index_downloaded_entry = ttk.Entry(dates_row, textvariable=index_downloaded_mindate_var, width=16)
    index_downloaded_entry.grid(row=3, column=1, sticky="w", padx=(0, 12))
    ttk.Label(dates_row, text="Downloaded on/before:", style="Card.TLabel").grid(row=3, column=2, sticky="e", padx=(0, 6))
    index_downloaded_maxdate_entry = ttk.Entry(dates_row, textvariable=index_downloaded_maxdate_var, width=16)
    index_downloaded_maxdate_entry.grid(row=3, column=3, sticky="w", padx=(0, 8))

    _date_hint = " Format YYYY-MM-DD or YYYY-MM-DD HH:MM; leave blank for no limit."
    add_tooltip(index_status_box, "Filter by deadline: Next to expire = DTEND within the next few days, Expired = DTEND already passed, Upcoming = further out, No date / needs repair = no DTEND found (what 'Repair missing deadlines' targets).")
    add_tooltip(index_detail_status_box, "Filter the selector between pending records, completed/saved records, failed records or all records.")
    add_tooltip(index_order_field_box, "Choose whether newest/oldest ordering uses downloaded date, end/deadline date, or start date.")
    add_tooltip(index_order_box, "Choose newest first or oldest first for the selected order-by date.")
    add_tooltip(index_mindate_entry, "Show only records whose DTEND (deadline) is on or after this date/time." + _date_hint)
    add_tooltip(index_maxdate_entry, "Show only records whose DTEND (deadline) is on or before this date/time (a bare date covers the whole day)." + _date_hint)
    add_tooltip(index_start_mindate_entry, "Show only records whose DTSTART (start) is on or after this date/time." + _date_hint)
    add_tooltip(index_start_maxdate_entry, "Show only records whose DTSTART (start) is on or before this date/time (a bare date covers the whole day)." + _date_hint)
    add_tooltip(index_downloaded_entry, "Show only records downloaded into the local archive on or after this date/time." + _date_hint)
    add_tooltip(index_downloaded_maxdate_entry, "Show only records downloaded on or before this date/time (a bare date covers the whole day)." + _date_hint)

    list_frame = ttk.Frame(record_index, style="Card.TFrame")
    list_frame.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(4, 4))
    list_frame.columnconfigure(0, weight=1)
    index_listbox = tk.Listbox(
        list_frame, height=10, activestyle="none", exportselection=False, selectmode="extended",
        bg="#020617", fg="#e5e7eb", selectbackground="#2563eb", selectforeground="#ffffff",
        highlightthickness=0, borderwidth=0, font=("Sans", 9),
    )
    index_scroll = ttk.Scrollbar(list_frame, orient="vertical", command=index_listbox.yview)
    index_listbox.configure(yscrollcommand=index_scroll.set)
    index_listbox.grid(row=0, column=0, sticky="ew")
    index_scroll.grid(row=0, column=1, sticky="ns")

    # Read-only, selectable Text so the NUMERO/description/dates can be copied for
    # further actions (search, paste into the portal, etc.).
    index_detail_text = tk.Text(
        record_index, height=3, wrap="word", bd=0, highlightthickness=0,
        bg="#0b1220", fg="#e5e7eb", insertbackground="#e5e7eb", font=("Sans", 9),
    )
    index_detail_text.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(6, 4))

    def set_index_detail(text: str) -> None:
        index_detail_text.configure(state="normal")
        index_detail_text.delete("1.0", "end")
        index_detail_text.insert("1.0", text)
        index_detail_text.configure(state="disabled")

    set_index_detail(index_detail_var.get())

    def index_label(rec: dict[str, str]) -> str:
        numero = rec["numero"] or "(sin número)"
        desc = rec["descripcion"] or "(sin descripción)"
        return f"{numero} — {desc}"

    def index_row_text(rec: dict[str, str]) -> str:
        """List row prefixed with local download timestamp, DTEND and status."""
        downloaded = parse_downloaded(rec)
        downloaded_part = downloaded.strftime("%Y-%m-%d_%H-%M") if downloaded else "not local"
        dt = parse_deadline(rec)
        dtend = dt.strftime("%Y-%m-%d") if dt else "no date"
        tag = STATUS_TAGS[expiry_status(rec)]
        return f"(DL {downloaded_part} | DTSTART {start_text(rec)} | DTEND {dtend} {tag:>7})  {index_label(rec)}"

    def selected_records() -> list[dict[str, str]]:
        records: list[dict[str, str]] = []
        for idx in index_listbox.curselection():
            if 0 <= idx < len(index_filtered):
                records.append(index_filtered[idx])
        return records

    def selected_record() -> dict[str, str] | None:
        records = selected_records()
        return records[0] if records else None

    def show_selected_detail(_event: object = None) -> None:
        rec = selected_record()
        if not rec:
            return
        status = f"   ·   detail: {rec['detail_status']}" if rec["detail_status"] else ""
        set_index_detail(
            f"NUMERO: {rec['numero']}   ·   {expiry_status(rec).upper()}{status}\n"
            f"Descripción: {rec['descripcion'] or '-'}\n"
            f"Downloaded: {downloaded_text(rec)}   ·   DTSTART: {start_text(rec)}   ·   DTEND (deadline): {deadline_text(rec)}"
        )

    def populate_listbox(records: list[dict[str, str]]) -> None:
        nonlocal index_filtered
        index_filtered = records
        index_listbox.delete(0, "end")
        for idx, rec in enumerate(records):
            index_listbox.insert("end", index_row_text(rec))
            index_listbox.itemconfig(idx, foreground=STATUS_COLORS[expiry_status(rec)])
        if records:
            index_listbox.selection_clear(0, "end")
            index_listbox.selection_set(0)
            index_listbox.see(0)
            show_selected_detail()

    def apply_filter(*_args: object) -> None:
        needle = index_filter_var.get().strip().lower()
        wanted_status = STATUS_FILTER_KEYS.get(index_status_var.get())
        wanted_detail_status = DETAIL_STATUS_FILTER_KEYS.get(index_detail_status_var.get())
        # Deadline (DTEND), start (DTSTART) and downloaded date windows. Each
        # bound accepts a date or a date+time; a bare date upper bound covers the
        # whole day. parse_filter_bound returns None for blank/invalid input, so
        # an unparseable value simply does not constrain the list.
        deadline_min = parse_filter_bound(index_mindate_var.get())
        deadline_max = parse_filter_bound(index_maxdate_var.get(), upper=True)
        start_min = parse_filter_bound(index_start_mindate_var.get())
        start_max = parse_filter_bound(index_start_maxdate_var.get(), upper=True)
        downloaded_min = parse_filter_bound(index_downloaded_mindate_var.get())
        downloaded_max = parse_filter_bound(index_downloaded_maxdate_var.get(), upper=True)

        def in_window(value: datetime | None, low: datetime | None, high: datetime | None) -> bool:
            if low is not None and (value is None or value < low):
                return False
            if high is not None and (value is None or value > high):
                return False
            return True

        records = []
        for rec in index_records:
            if needle and needle not in index_label(rec).lower():
                continue
            if wanted_status and expiry_status(rec) != wanted_status:
                continue
            if wanted_detail_status and (rec.get("detail_status") or "").lower() != wanted_detail_status:
                continue
            if (deadline_min or deadline_max) and not in_window(parse_deadline(rec), deadline_min, deadline_max):
                continue
            if (start_min or start_max) and not in_window(parse_start(rec), start_min, start_max):
                continue
            if (downloaded_min or downloaded_max) and not in_window(parse_downloaded(rec), downloaded_min, downloaded_max):
                continue
            records.append(rec)

        newest_first = index_order_var.get() != "Oldest first"
        order_field = index_order_field_var.get()
        if newest_first:
            records.sort(key=lambda rec: parse_record_order_date(rec, order_field) or datetime.min, reverse=True)
        else:
            records.sort(key=lambda rec: parse_record_order_date(rec, order_field) or datetime.max)
        populate_listbox(records)
        if not records:
            set_index_detail("No records match the filter." if index_records else
                             "No records collected yet (data/panamacompra_archive.db is missing or empty). Run the collector, then Refresh list.")

    def refresh_index_list() -> None:
        nonlocal index_records
        index_records = load_record_index()
        apply_filter()
        if index_records:
            button_status_var.set(f"Loaded {len(index_records)} record(s) into the index list.")

    def open_selected_folder() -> None:
        rec = selected_record()
        if not rec:
            button_status_var.set("Select a record from the list first.")
            return
        folder = rec["record_folder"]
        if not folder or not Path(folder).exists():
            button_status_var.set(f"Record folder not found on disk for {rec['numero'] or 'the selection'}.")
            return
        opener = os.environ.get("PC_OPEN_FOLDER_COMMAND", "xdg-open")
        subprocess.Popen([opener, folder], cwd=BASE_DIR, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        button_status_var.set(f"Opened record folder for {rec['numero']}.")


    def selected_numeros() -> list[str]:
        return [rec["numero"] for rec in selected_records() if rec.get("numero")]

    def notify_selected_records() -> None:
        numeros = selected_numeros()
        if not numeros:
            button_status_var.set("Select one or more records first (Ctrl/Shift-click).")
            return
        cmd = [str(BASE_DIR / "src/20_pipeline/020-notify-whatsapp.py"), "--force"]
        for numero in numeros:
            cmd.extend(["--record", numero])
        subprocess.Popen(cmd, cwd=BASE_DIR, env=monitor_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        button_status_var.set(f"WhatsApp notification requested for {len(numeros)} selected record(s).")

    def copy_templates_to_selected() -> None:
        numeros = selected_numeros()
        if not numeros:
            button_status_var.set("Select one or more records first (Ctrl/Shift-click).")
            return
        cmd = [str(BASE_DIR / "src/50_tools/020-record-templates.py"), "apply", "--apply"]
        for numero in numeros:
            cmd.extend(["--numero", numero])
        subprocess.Popen(cmd, cwd=BASE_DIR, env=monitor_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        button_status_var.set(f"Work templates requested for {len(numeros)} selected record(s) (existing files kept).")

    def import_selected_calendars() -> None:
        numeros = selected_numeros()
        if not numeros:
            button_status_var.set("Select one or more records first (Ctrl/Shift-click).")
            return
        subprocess.Popen([str(BASE_DIR / "src/50_tools/060-import-selected-calendars.py"), "--open", *numeros], cwd=BASE_DIR, env=monitor_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        button_status_var.set(f"Calendar import requested for {len(numeros)} selected record(s).")

    def open_selected_portal() -> None:
        rec = selected_record()
        if not rec:
            button_status_var.set("Select a record from the list first.")
            return
        if not rec["link"]:
            button_status_var.set(f"No portal link stored for {rec['numero'] or 'the selection'}.")
            return
        subprocess.Popen(["xdg-open", rec["link"]], cwd=BASE_DIR, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        button_status_var.set(f"Opening portal page for {rec['numero']}.")

    index_listbox.bind("<<ListboxSelect>>", show_selected_detail)
    index_listbox.bind("<Double-Button-1>", lambda _e: open_selected_folder())
    index_filter_var.trace_add("write", apply_filter)
    index_status_var.trace_add("write", apply_filter)
    index_detail_status_var.trace_add("write", apply_filter)
    index_order_var.trace_add("write", apply_filter)
    index_order_field_var.trace_add("write", apply_filter)
    index_mindate_var.trace_add("write", apply_filter)
    index_maxdate_var.trace_add("write", apply_filter)
    index_start_mindate_var.trace_add("write", apply_filter)
    index_start_maxdate_var.trace_add("write", apply_filter)
    index_downloaded_mindate_var.trace_add("write", apply_filter)
    index_downloaded_maxdate_var.trace_add("write", apply_filter)

    index_buttons = ttk.Frame(record_index, style="Card.TFrame")
    index_buttons.grid(row=5, column=0, columnspan=3, sticky="w", pady=(6, 0))
    refresh_index_button = ttk.Button(index_buttons, text="Refresh list", command=refresh_index_list)
    refresh_index_button.grid(row=0, column=0, padx=(0, 8))
    open_folder_button = ttk.Button(index_buttons, text="Open record folder", command=open_selected_folder)
    open_folder_button.grid(row=0, column=1, padx=(0, 8))
    open_portal_button = ttk.Button(index_buttons, text="Open in portal", command=open_selected_portal)
    open_portal_button.grid(row=0, column=2, padx=(0, 8))
    notify_selected_button = ttk.Button(index_buttons, text="Notify selected WhatsApp", command=notify_selected_records)
    notify_selected_button.grid(row=0, column=3, padx=(0, 8))
    import_selected_button = ttk.Button(index_buttons, text="Import selected calendars", command=import_selected_calendars)
    import_selected_button.grid(row=0, column=4, padx=(0, 8))
    templates_selected_button = ttk.Button(index_buttons, text="Copy templates to selected", command=copy_templates_to_selected)
    templates_selected_button.grid(row=0, column=5, padx=(0, 8))
    add_tooltip(templates_selected_button, "Copy the selected work templates into templates/ inside each chosen record folder (files already there are kept).")
    add_tooltip(refresh_index_button, "Reload the record list from the archive database (run after a new collection).")
    add_tooltip(open_folder_button, "Open the selected record's archive folder (or double-click a row).")
    add_tooltip(open_portal_button, "Open the selected record's PanamaCompra portal page in the browser.")
    add_tooltip(notify_selected_button, "Send WhatsApp notifications for all selected records (Ctrl/Shift-click to select several).")
    add_tooltip(import_selected_button, "Export/open calendar ICS files for all selected records (Ctrl/Shift-click to select several).")

    refresh_index_list()
    add_section_toggle(record_index, button_column=2)

    # ========================================================================
    # Opportunity calendar: the collected opportunities by day/week/month/year,
    # driven by deadline/start/downloaded dates. Shares its renderer with
    # `pcc calendar` and the web monitor's Calendar tab.
    calendar_card = ttk.Frame(calendar_tab, style="Card.TFrame", padding=14)
    calendar_card.grid(row=0, column=0, sticky="ew", padx=6, pady=6)
    calendar_card.columnconfigure(6, weight=1)
    add_section_header(calendar_card, "Opportunity calendar", "Opportunities by day, week, month or year — click a grid cell to drill in.", columnspan=6)

    calendar_view_var = tk.StringVar(value="month")
    calendar_field_var = tk.StringVar(value="end")
    calendar_anchor_var = tk.StringVar(value="")

    # Graphical calendar: a drawn grid whose LAYOUT follows the selected view —
    # single wide cell (day), one 7-column row (week), the classic month grid,
    # or 12 month boxes (year). Today gets an amber outline and every cell is
    # clickable (click = drill into that day/month).
    calendar_day_cells: list[tuple[float, float, float, float, str, str]] = []

    def _grid_cell(x0, y0, x1, y1, iso, count, max_count, label, *, today=False, drill="day"):
        hot = count and count >= max_count * 0.7
        fill = "#713f12" if hot else ("#14532d" if count else "#0b1220")
        outline = "#facc15" if today else "#334155"
        calendar_canvas.create_rectangle(x0, y0, x1, y1, fill=fill, outline=outline, width=2 if today else 1)
        calendar_canvas.create_text(x0 + 6, y0 + 10, anchor="w", fill="#94a3b8", font=("Sans", 8), text=label)
        if count:
            calendar_canvas.create_text((x0 + x1) / 2, (y0 + y1) / 2 + 6, fill="#fde68a" if hot else "#bbf7d0",
                                        font=("Sans", 10, "bold"), text=str(count))
        calendar_day_cells.append((x0, y0, x1, y1, iso, drill))

    def draw_calendar_grid(view: str, anchor, counts: dict[str, int]) -> None:
        calendar_day_cells.clear()
        calendar_canvas.delete("all")
        width = calendar_canvas.winfo_width()
        if width <= 1:
            width = 920
        header_h = 20
        today_iso = datetime.now().strftime("%Y-%m-%d")
        max_count = max(list(counts.values()) + [1])

        if view == "year":
            month_totals: dict[str, int] = {}
            for iso, qty in counts.items():
                month_totals[iso[:7]] = month_totals.get(iso[:7], 0) + int(qty)
            peak = max(list(month_totals.values()) + [1])
            cols = 4
            cell_w = max(120, (width - 12) / cols)
            cell_h = 54
            calendar_canvas.configure(height=3 * cell_h + 10)
            for month in range(1, 13):
                key = f"{anchor.year}-{month:02d}"
                row, col = divmod(month - 1, cols)
                x0 = 6 + col * cell_w
                y0 = 6 + row * cell_h
                _grid_cell(x0, y0, x0 + cell_w - 4, y0 + cell_h - 4, f"{key}-01",
                           month_totals.get(key, 0), peak,
                           date(anchor.year, month, 1).strftime("%b %Y"),
                           today=key == today_iso[:7], drill="month")
            return

        if view == "day":
            cell_h = 64
            calendar_canvas.configure(height=cell_h + 10)
            iso = anchor.isoformat()
            _grid_cell(6, 6, width - 6, cell_h, iso, int(counts.get(iso, 0)), max_count,
                       f"{iso} ({anchor.strftime('%A')})", today=iso == today_iso)
            return

        if view == "week":
            start, _ = opportunity_calendar.view_range("week", anchor)
            cell_w = max(60, (width - 12) / 7)
            cell_h = 64
            calendar_canvas.configure(height=header_h + cell_h + 10)
            for col, day_name in enumerate(("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")):
                calendar_canvas.create_text(6 + col * cell_w + cell_w / 2, header_h / 2, fill="#93c5fd",
                                            font=("Sans", 8, "bold"), text=day_name)
            for offset in range(7):
                day = start + timedelta(days=offset)
                iso = day.isoformat()
                x0 = 6 + offset * cell_w
                _grid_cell(x0, header_h, x0 + cell_w - 4, header_h + cell_h - 4, iso,
                           int(counts.get(iso, 0)), max_count, iso[5:], today=iso == today_iso)
            return

        month_start, month_end = opportunity_calendar.view_range("month", anchor)
        cell_w = max(60, (width - 12) / 7)
        cell_h = 44
        weeks = (month_start.weekday() + month_end.day + 6) // 7
        calendar_canvas.configure(height=header_h + weeks * cell_h + 8)
        for col, day_name in enumerate(("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")):
            calendar_canvas.create_text(6 + col * cell_w + cell_w / 2, header_h / 2, fill="#93c5fd",
                                        font=("Sans", 8, "bold"), text=day_name)
        for day_number in range(1, month_end.day + 1):
            iso = f"{month_start.isoformat()[:8]}{day_number:02d}"
            slot = month_start.weekday() + day_number - 1
            row, col = divmod(slot, 7)
            x0 = 6 + col * cell_w
            y0 = header_h + row * cell_h
            _grid_cell(x0, y0, x0 + cell_w - 4, y0 + cell_h - 4, iso,
                       int(counts.get(iso, 0)), max_count, str(day_number), today=iso == today_iso)

    def on_calendar_grid_click(event: object) -> None:
        for x0, y0, x1, y1, iso, drill in calendar_day_cells:
            if x0 <= event.x <= x1 and y0 <= event.y <= y1:
                calendar_anchor_var.set(iso)
                calendar_view_var.set(drill)
                render_calendar(None)
                return

    def render_calendar(shift: int | None = None) -> None:
        view = calendar_view_var.get()
        try:
            anchor = opportunity_calendar.parse_anchor(calendar_anchor_var.get())
        except SystemExit:
            anchor = opportunity_calendar.parse_anchor("")
        if shift == 0:
            anchor = opportunity_calendar.parse_anchor("")
        elif shift:
            anchor = opportunity_calendar.shift_anchor(view, anchor, shift)
        calendar_anchor_var.set(anchor.isoformat())
        grid_counts: dict[str, int] = {}
        try:
            conn = sqlite3.connect(f"file:{ARCHIVE_DB}?mode=ro", uri=True, timeout=2)
            conn.row_factory = sqlite3.Row
            try:
                text = opportunity_calendar.render_view(conn, view, anchor, calendar_field_var.get())
                # Fetch counts for the SAME range the grid will draw, so week
                # and day views (which can cross month edges) are always right.
                grid_start, grid_end = opportunity_calendar.view_range(view, anchor)
                events = opportunity_calendar.fetch_events(conn, calendar_field_var.get(), grid_start, grid_end)
                grid_counts = {day: len(rows) for day, rows in events.items()}
            finally:
                conn.close()
        except sqlite3.Error:
            text = "(archive database not available yet — run a collection first)"
        draw_calendar_grid(view, anchor, grid_counts)
        calendar_text.configure(state="normal")
        calendar_text.delete("1.0", "end")
        calendar_text.insert("1.0", text)
        calendar_text.configure(state="disabled")

    ttk.Label(calendar_card, text="View:", style="Card.TLabel").grid(row=1, column=0, sticky="w", padx=(0, 4))
    calendar_view_combo = ttk.Combobox(calendar_card, textvariable=calendar_view_var, values=("day", "week", "month", "year"), width=7, state="readonly")
    calendar_view_combo.grid(row=1, column=1, sticky="w", padx=(0, 10))
    ttk.Label(calendar_card, text="Date field:", style="Card.TLabel").grid(row=1, column=2, sticky="w", padx=(0, 4))
    calendar_field_combo = ttk.Combobox(calendar_card, textvariable=calendar_field_var, values=("end", "start", "downloaded"), width=11, state="readonly")
    calendar_field_combo.grid(row=1, column=3, sticky="w", padx=(0, 10))
    calendar_anchor_entry = ttk.Entry(calendar_card, textvariable=calendar_anchor_var, width=12)
    calendar_anchor_entry.grid(row=1, column=4, sticky="w", padx=(0, 10))
    add_tooltip(calendar_anchor_entry, "Anchor date: YYYY, YYYY-MM or YYYY-MM-DD (blank = today). Press Show.")
    add_tooltip(calendar_view_combo, "Calendar granularity: one day, the week, a month grid with per-day counts, or a whole year with per-month totals.")
    add_tooltip(calendar_field_combo, "Which date drives the view: end = deadline (default), start = opportunity start, downloaded = when the detail was saved locally.")

    calendar_buttons = ttk.Frame(calendar_card, style="Card.TFrame")
    calendar_buttons.grid(row=1, column=5, sticky="w")
    ttk.Button(calendar_buttons, text="◀ Prev", command=lambda: render_calendar(-1)).grid(row=0, column=0, padx=(0, 6))
    ttk.Button(calendar_buttons, text="Today", command=lambda: render_calendar(0)).grid(row=0, column=1, padx=(0, 6))
    ttk.Button(calendar_buttons, text="Next ▶", command=lambda: render_calendar(1)).grid(row=0, column=2, padx=(0, 6))
    ttk.Button(calendar_buttons, text="Show", command=lambda: render_calendar(None)).grid(row=0, column=3)

    calendar_canvas = tk.Canvas(calendar_card, height=220, bg="#020617", highlightthickness=1,
                                highlightbackground="#334155")
    calendar_canvas.grid(row=2, column=0, columnspan=7, sticky="ew", pady=(8, 0))
    calendar_canvas.bind("<Button-1>", on_calendar_grid_click)
    add_tooltip(calendar_canvas, "Graphical month calendar for the selected date field. Click a day to open its detail below; amber outline = today, amber cell = busiest days.")
    calendar_text = tk.Text(calendar_card, height=14, wrap="none", state="disabled", font=("monospace", 9))
    calendar_text.grid(row=3, column=0, columnspan=7, sticky="ew", pady=(8, 0))
    calendar_view_combo.bind("<<ComboboxSelected>>", lambda _e: render_calendar(None))
    calendar_field_combo.bind("<<ComboboxSelected>>", lambda _e: render_calendar(None))
    render_calendar(0)
    add_section_toggle(calendar_card, button_column=6)

    # ========================================================================
    # OPERATIONS TAB / MANUAL ACTION BUTTONS - grouped by zone in a tidy
    # 3-column grid. Each button's explanation is shown as a hover tooltip (not
    # an inline label) so the grid stays compact and easy to scan.
    # ========================================================================
    actions = ttk.Frame(ops_tab, style="Card.TFrame", padding=14)
    actions.grid(row=2, column=0, sticky="ew", padx=6, pady=6)
    button_columns = 3
    for col in range(button_columns):
        actions.columnconfigure(col, weight=1, uniform="actions")
    add_section_header(actions, "Manual script buttons", "Grouped by zone; hover a button for what it does.", columnspan=button_columns)

    def open_folder(path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        opener = os.environ.get("PC_OPEN_FOLDER_COMMAND", "xdg-open")
        subprocess.Popen([opener, str(path)], cwd=BASE_DIR, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def run_manual_action(action: ManualAction) -> None:
        MANUAL_ACTION_LOG.parent.mkdir(parents=True, exist_ok=True)
        with MANUAL_ACTION_LOG.open("a", encoding="utf-8") as log_file:
            log_file.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} | {action.zone} / {action.label} =====\n")
            log_file.write("Command: " + " ".join(shlex.quote(part) for part in action.command) + "\n")
            proc = subprocess.Popen(action.command, cwd=BASE_DIR, env=monitor_env(), stdout=log_file, stderr=subprocess.STDOUT)

        if action.open_after is not None:
            def wait_then_open() -> None:
                proc.wait()
                root.after(0, open_folder, action.open_after)
            import threading
            threading.Thread(target=wait_then_open, daemon=True).start()
        button_status_var.set(f"Started: {action.label}. Output: {MANUAL_ACTION_LOG.relative_to(BASE_DIR)}")

    grid_row = 1
    for zone in dict.fromkeys(action.zone for action in MANUAL_ACTIONS):
        ttk.Label(actions, text=zone, style="Message.TLabel").grid(row=grid_row, column=0, columnspan=button_columns, sticky="w", pady=(10, 4))
        grid_row += 1
        zone_actions = [action for action in MANUAL_ACTIONS if action.zone == zone]
        for offset, action in enumerate(zone_actions):
            col = offset % button_columns
            if offset and col == 0:
                grid_row += 1
            button_style = "Danger.TButton" if "STOP" in action.label.upper() else "TButton"
            button = ttk.Button(actions, text=action.label, command=lambda selected=action: run_manual_action(selected), style=button_style)
            button.grid(row=grid_row, column=col, sticky="ew", padx=4, pady=4)
            add_tooltip(button, action.comment)
        grid_row += 1
    add_section_toggle(actions, button_column=button_columns - 1)

    # ========================================================================
    # KPIS TAB / DATABASE REVIEW - read-only aggregate snapshot of the archive DB
    # (totals, detail-queue state, notification state, per-group breakdown) so the
    # operator can review the database state at a glance without opening sqlite.
    # ========================================================================
    db_review = ttk.Frame(kpi_tab, style="Card.TFrame", padding=14)
    db_review.grid(row=2, column=0, sticky="ew", padx=6, pady=6)
    db_review.columnconfigure(0, weight=1)
    add_section_header(db_review, "Database review", "Inspect archive rows and run maintenance fixes.", columnspan=2)
    db_review_var = tk.StringVar(value="Loading database snapshot…")
    ttk.Label(db_review, textvariable=db_review_var, style="Card.TLabel", justify="left").grid(row=1, column=0, sticky="w")

    def refresh_db_review() -> None:
        s = db_review_stats()
        if not s.get("db_exists"):
            db_review_var.set("No database yet (data/panamacompra_archive.db). Run the collector first.")
            return
        groups = "   ·   ".join(f"{row['grupo']}: {row['count']}" for row in s["groups"]) or "—"
        db_review_var.set(
            f"Total records: {s['total']}\n"
            f"Detail status   ·   saved: {s['saved']}   ·   pending: {s['pending']}   ·   failed: {s['failed']}\n"
            f"Detail JSON on record: {s['with_detail_json']}   ·   Notified (WAHA): {s['notified']}\n"
            f"Awaiting WhatsApp index alert: {s['notify_backlog']}   ·   Awaiting item-details WhatsApp: {s['detail_notify_backlog']}   ·   Needs deadline repair: {s['needs_deadline']}\n"
            f"By group   ·   {groups}"
        )

    refresh_db_review()
    db_review_refresh_button = ttk.Button(db_review, text="Refresh DB snapshot", command=refresh_db_review)
    db_review_refresh_button.grid(row=2, column=0, sticky="w", pady=(8, 0))
    add_tooltip(db_review_refresh_button, "Re-read the archive database and refresh these review counts.")
    add_section_toggle(db_review, button_column=1)

    # ========================================================================
    # SECTION 7: RESET / REVIEW FROM ZERO - separate buttons (per the operator's
    # request) for each reset depth, from a soft detail re-queue to a full wipe.
    # The two destructive wipes pop a confirmation dialog and pass --yes only when
    # confirmed, so a stray click cannot erase the archive. All call src/50_tools/110-reset.py.
    # ========================================================================
    reset_zone = ttk.Frame(settings_tab, style="Card.TFrame", padding=14)
    reset_zone.grid(row=1, column=0, sticky="ew", padx=6, pady=6)
    for col in range(2):
        reset_zone.columnconfigure(col, weight=1, uniform="reset")
    ttk.Label(reset_zone, text="Reset / review from zero", style="Title.TLabel").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
    reset_status_var = tk.StringVar(value="")
    ttk.Label(reset_zone, textvariable=reset_status_var, style="Card.TLabel", wraplength=620).grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))

    def run_reset(action: str, *, destructive: bool, confirm_text: str) -> None:
        if destructive:
            if not messagebox.askyesno("Confirm reset", confirm_text, icon="warning", default="no"):
                reset_status_var.set(f"{action}: cancelled.")
                return
        command = [str(BASE_DIR / "src/50_tools/110-reset.py"), action]
        if destructive:
            command.append("--yes")
        MANUAL_ACTION_LOG.parent.mkdir(parents=True, exist_ok=True)
        with MANUAL_ACTION_LOG.open("a", encoding="utf-8") as log_file:
            log_file.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} | Reset / {action} =====\n")
            subprocess.Popen(command, cwd=BASE_DIR, env=monitor_env(), stdout=log_file, stderr=subprocess.STDOUT)
        reset_status_var.set(f"Started reset '{action}'. See {MANUAL_ACTION_LOG.relative_to(BASE_DIR)}; refresh the DB snapshot above to verify.")

    requeue_btn = ttk.Button(reset_zone, text="Re-queue all details",
                             command=lambda: run_reset("requeue-details", destructive=False, confirm_text=""))
    requeue_btn.grid(row=1, column=0, sticky="ew", padx=4, pady=4)
    notify_btn = ttk.Button(reset_zone, text="Reset notify / review flags",
                            command=lambda: run_reset("reset-notify", destructive=False, confirm_text=""))
    notify_btn.grid(row=1, column=1, sticky="ew", padx=4, pady=4)
    wipe_db_btn = ttk.Button(reset_zone, text="Wipe database only", style="Danger.TButton",
                             command=lambda: run_reset("wipe-db", destructive=True,
                                                       confirm_text="Delete the tracking database (data/panamacompra_archive.db) and index CSV?\n\nDownloaded record folders are kept and re-linked on the next run."))
    wipe_db_btn.grid(row=2, column=0, sticky="ew", padx=4, pady=4)
    wipe_all_btn = ttk.Button(reset_zone, text="Wipe EVERYTHING", style="Danger.TButton",
                              command=lambda: run_reset("wipe-all", destructive=True,
                                                        confirm_text="Delete the database AND every downloaded record folder and calendar?\n\nThis is irreversible — the local archive is lost. The test zone is kept."))
    wipe_all_btn.grid(row=2, column=1, sticky="ew", padx=4, pady=4)
    add_tooltip(requeue_btn, "Set every record's detail back to pending (attempts=0) so the next run re-downloads all detail pages. Keeps all data.")
    add_tooltip(notify_btn, "Clear WAHA notification/review flags so every record can be announced again from zero. Keeps all data.")
    add_tooltip(wipe_db_btn, "Delete the tracking DB + index CSV (keeps record files on disk). Destructive — asks for confirmation.")
    add_tooltip(wipe_all_btn, "Delete the DB AND all downloaded records/calendars for a true from-scratch re-collection. Irreversible — asks for confirmation.")
    add_section_toggle(reset_zone, button_column=1)

    # ========================================================================
    # SETTINGS TAB / WEBHOOK TRIGGER ACCESS - the token generated by setup
    # plus the exact changedetection/docker/local URLs, re-read live on Refresh
    # so the panel always reflects the CURRENT settings after an update.
    # ========================================================================
    webhook_access = ttk.Frame(settings_tab, style="Card.TFrame", padding=14)
    webhook_access.grid(row=2, column=0, sticky="ew", padx=6, pady=6)
    webhook_access.columnconfigure(0, weight=1)
    ttk.Label(webhook_access, text="Webhook trigger access (token + URLs from setup)", style="Title.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 8))
    webhook_access_box = tk.Text(webhook_access, height=13, wrap="none", bd=0, highlightthickness=0,
                                 bg="#020617", fg="#e5e7eb", insertbackground="#e5e7eb", font=("monospace", 9))
    webhook_access_box.grid(row=1, column=0, sticky="ew")

    def refresh_webhook_access() -> None:
        webhook_access_box.configure(state="normal")
        webhook_access_box.delete("1.0", "end")
        webhook_access_box.insert("1.0", webhook_access_text())
        webhook_access_box.configure(state="disabled")

    webhook_access_refresh = ttk.Button(webhook_access, text="Refresh webhook access", command=refresh_webhook_access)
    webhook_access_refresh.grid(row=2, column=0, sticky="w", pady=(8, 0))
    add_tooltip(webhook_access_refresh, "Re-read .webhook_token and the port settings so the URLs reflect the current setup (e.g. right after running setup or the docker stack).")
    add_tooltip(webhook_access_box, "Copyable: token + the changedetection json:// URL, the host.docker.internal URL and the local test URL. Paste the recommended json://host.docker.internal URL into changedetection; use json://webhook only in the same compose network.")
    refresh_webhook_access()
    add_section_toggle(webhook_access, button_column=0)

    logs = ttk.Frame(ops_tab, style="Card.TFrame", padding=14)
    logs.grid(row=3, column=0, sticky="nsew", padx=6, pady=(6, 10))
    ops_tab.rowconfigure(3, weight=1)
    logs.columnconfigure(0, weight=1)
    logs.columnconfigure(1, weight=1)
    logs.rowconfigure(1, weight=1)
    ttk.Label(logs, text="Recent worker log", style="Title.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 8))
    ttk.Label(logs, text="Current action log", style="Title.TLabel").grid(row=0, column=1, sticky="w", pady=(0, 8))

    # Each log is a fixed-height box WITH its own scrollbar, so the pane scrolls
    # the log itself (wheel or scrollbar) instead of moving the whole page.
    def make_log_pane(parent: tk.Widget, grid_col: int, pad: tuple[int, int]) -> tk.Text:
        frame = ttk.Frame(parent, style="Card.TFrame")
        frame.grid(row=1, column=grid_col, sticky="nsew", padx=pad)
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        text = tk.Text(frame, height=8, bg="#020617", fg="#e5e7eb", insertbackground="#e5e7eb", wrap="word")
        bar = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=bar.set)
        text.grid(row=0, column=0, sticky="nsew")
        bar.grid(row=0, column=1, sticky="ns")
        return text

    worker_text = make_log_pane(logs, 0, (0, 7))
    current_text = make_log_pane(logs, 1, (7, 0))
    add_section_toggle(logs, button_column=2)

    # Centered auto-close countdown overlay. It is placed in the exact middle of
    # the window (relx/rely 0.5, anchor center) only while a finished LIVE run is
    # counting down, and removed otherwise. Using place() keeps it on top of the
    # gridded canvas without disturbing the scrollable layout.
    overlay_var = tk.StringVar(value="")
    overlay = tk.Label(
        root,
        textvariable=overlay_var,
        bg="#020617",
        fg="#bbf7d0",
        font=("Sans", 26, "bold"),
        justify="center",
        padx=44,
        pady=30,
        bd=2,
        relief="solid",
        highlightbackground="#22c55e",
        highlightthickness=2,
    )

    done_since: float | None = None
    # Only auto-close after this monitor session has actually watched a run go
    # from active to finished. Opening the monitor straight into a pre-existing
    # idle/done state (e.g. right after an update with no run queued) must NOT
    # start the countdown, otherwise the window would close before any work runs.
    saw_active = False

    def set_text(widget: tk.Text, value: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", value)
        widget.configure(state="disabled")

    def refresh() -> None:
        nonlocal done_since, saw_active
        snap = status_snapshot()
        progress = snap["progress"]
        percent = int(snap["percent"])
        progress_var.set(percent)
        # Refresh cadence comes from the live runtime settings (editable via the
        # Settings panel), not the static snapshot values.
        active_delay = runtime["idle_refresh"] if snap["done"] else runtime["refresh"]
        meta_var.set(f"Time: {snap['time']} · Transparency: {runtime['alpha']:.2f} · Refresh: {active_delay}s · Progress: {percent}%")
        message_var.set(str(progress.get("MESSAGE", "")))
        update_process_chips(snap["processes"])
        update_queue_panel(snap.get("queue", {}) or {})
        update_run_controls(snap)
        update_records_overview(progress)
        update_kpi_dashboard(progress)

        for key, var in diag_vars.items():
            if key == "STEP":
                var.set(f"{progress.get('STEP_CURRENT', '-')} / {progress.get('STEP_TOTAL', '-')}")
            elif key == "ITEM":
                var.set(f"{progress.get('ITEM_CURRENT', '-')} / {progress.get('ITEM_TOTAL', '-')}")
            else:
                var.set(str(progress.get(key, "-")))

        if not waha_var.get():
            waha_var.set(str(snap.get("waha_chat_id", "")))
        if not waha_index_var.get():
            waha_index_var.set(str(snap.get("waha_chat_id_index", "")))
        if not waha_details_var.get():
            waha_details_var.set(str(snap.get("waha_chat_id_details", "")))
        if not waha_status_var.get():
            waha_status_var.set(str(snap.get("waha_chat_id_status", "")))
        if not waha_system_var.get():
            waha_system_var.set(str(snap.get("waha_chat_id_system", "")))
        if not waha_summary_var.get():
            waha_summary_var.set(str(snap.get("waha_chat_id_summary", "")))

        set_text(worker_text, str(snap.get("worker_log") or "(no recent worker log lines)"))
        set_text(current_text, str(snap.get("current_log") or "(no current action log lines)"))

        counting_down = False
        if not snap["done"]:
            # A run is active (or starting): remember it so the countdown is
            # allowed once it finishes, and clear any previous countdown state.
            saw_active = True
            done_since = None
            done_var.set("")
            overlay.place_forget()
        elif not saw_active:
            # Opened into a pre-existing idle/done state: show status, no countdown.
            done_since = None
            done_var.set("Idle. Auto-close starts only after an automatic (changedetection) run finishes while the monitor is open.")
            overlay.place_forget()
        else:
            if done_since is None:
                done_since = time.monotonic()
            wait = runtime["auto_close"] if snap.get("auto_close_enabled") else 0
            remaining = max(0, wait - int(time.monotonic() - done_since))
            if wait:
                counting_down = True
                done_var.set(f"Automatic run finished. This window will close in {remaining} seconds.")
                overlay_var.set(f"✅ Run finished\n\nClosing in {remaining} s")
                overlay.place(relx=0.5, rely=0.5, anchor="center")
            else:
                done_var.set("Run finished. Auto-close is disabled for test zone and manual desktop actions.")
                overlay.place_forget()
            if wait and remaining <= 0:
                root.destroy()
                return

        # Tick once per second while the countdown is visible so it updates
        # smoothly; otherwise use the normal (slower, low-power) refresh cadence.
        next_delay_ms = 1000 if counting_down else active_delay * 1000
        root.after(next_delay_ms, refresh)

    # Re-apply transparency once the window is actually mapped. On many X11
    # window managers `-alpha` set before the window is visible is silently
    # ignored, so the early apply_alpha() above is not enough on its own. Wait for
    # visibility, then re-apply, and re-apply again shortly after in case a
    # compositor finishes initializing late.
    root.update_idletasks()
    try:
        root.wait_visibility(root)
    except tk.TclError:
        pass
    apply_alpha(runtime["alpha"])
    root.after(300, lambda: apply_alpha(runtime["alpha"]))

    refresh()
    root.mainloop()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="PanamaCompra native Tk progress monitor")
    parser.add_argument("--snapshot", action="store_true", help="print one JSON status snapshot and exit")
    args = parser.parse_args()

    if args.snapshot:
        print(json.dumps(status_snapshot(), ensure_ascii=False, indent=2))
        return 0

    return run_tk()


if __name__ == "__main__":
    raise SystemExit(main())
