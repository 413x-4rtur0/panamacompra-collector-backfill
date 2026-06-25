#!/usr/bin/env python3
"""Lightweight native Tk monitor for PanamaCompra run-all progress.

This gives a real desktop window without starting Firefox or a local web server.
It uses only Python's standard library and refreshes on a timer.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import NamedTuple

BASE_DIR = Path(__file__).resolve().parent
CONFIG_DIR = BASE_DIR / "data" / "config"
PROGRESS_FILE = BASE_DIR / "data" / "logs" / "run_all_progress.env"
WORKER_LOG = BASE_DIR / "data" / "logs" / "run_all_worker.log"
CURRENT_LOG = BASE_DIR / "data" / "logs" / "run_all_current.log"
REQUEST_FLAG = BASE_DIR / "data" / "queue" / "run_all_requested.flag"
WAHA_CHAT_ID_PATH = CONFIG_DIR / "waha_chat_id.txt"
WAHA_KEYWORDS_PATH = CONFIG_DIR / "waha_keywords.txt"
MANUAL_ACTION_LOG = BASE_DIR / "data" / "logs" / "manual_actions.log"
# Archive database read (read-only) to populate the record-index selector with
# the collected opportunities (NUMERO + description + folder/link).
ARCHIVE_DB = BASE_DIR / "data" / "panamacompra_archive.db"
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
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip().strip('"').strip("'")
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


RECORDS_TEST_PARENT = BASE_DIR / "records_test"


# Manual buttons grouped by zone. Each action's `comment` is shown as a hover
# tooltip on its button (not as an always-visible label), so the grid stays
# compact and readable. Zones are ordered by how often they are used:
# run → keep code fresh → rebuild data → test → open folders. Keep the most
# common/safe action first in each zone and destructive ones clearly labelled.
MANUAL_ACTIONS = [
    # --- 1. Collector Runners: start/stop the live collection ----------------
    ManualAction("Collector Runners", "Request full collection", ("./pc_request_run_all.sh", "99", "RESTART", "20"), "Queues a manual restart run (up to 20 index pages per group and 99 detail pages) for the background worker. Safe default action."),
    ManualAction("Collector Runners", "Run collection now", ("./pc_run_all_now.sh", "99", "20", "MANUAL"), "Starts the run-all worker immediately for up to 20 index pages per group and 99 detail pages (does not wait for the queue)."),
    ManualAction("Collector Runners", "Show run status", ("./pc_run_all_status.sh",), "Writes a process/log status snapshot to the manual action log."),
    ManualAction("Collector Runners", "STOP all runners", ("./pc_stop_run_all.sh",), "DANGER: stops ALL processes — workers, test zone, calendar builder, monitors, webhook listener and updaters (this monitor closes too)."),

    # --- 2. Updater & Migration: keep code fresh, migrate old data -----------
    ManualAction("Updater & Migration", "Update local copy", ("./pc_update_loader.py", "--open-monitor-after"), "Opens the centered updater window, refreshes the checkout/dependencies (auto-picks latest branch vs main), then reopens the monitor."),
    ManualAction("Updater & Migration", "Pre-run update only", ("./pc_update_before_run.sh",), "Runs the lightweight git/dependency refresh used before worker iterations (no browser install)."),
    ManualAction("Updater & Migration", "Normalize folder names", ("./pc_rename_record_folders.py", "--apply"), "Normalizes existing record folder names using the current naming rules."),
    ManualAction("Updater & Migration", "Migrate old records", ("./migrate_previous_records.sh",), "Imports/migrates previous record archives into the current layout."),

    # --- 3. Data Tools: rebuild views/calendars and integrations -------------
    ManualAction("Data Tools", "Rebuild detail views", ("./pc_build_detail_views.py", "--apply"), "Rebuilds saved record views, ICS files and split tables from stored data (no browser)."),
    ManualAction("Data Tools", "Rebuild calendar packages", ("./pc_build_calendar.py", "--all"), "Rebuilds the calendar import packages (.ics) for all dated record folders."),
    ManualAction("Data Tools", "Import calendars to app", ("bash", "-lc", "PC_CALENDAR_AUTO_IMPORT=1 ./pc_build_calendar.py --all"), "Rebuilds all packages and opens each .ics with the desktop calendar app."),
    ManualAction("Data Tools", "Start webhook listener", ("./pc_start_webhook_listener.sh", "--replace-port-owner"), "Starts/restarts the local webhook listener in the background; use STOP all runners to halt it."),
    ManualAction("Data Tools", "Install webhook service", ("./pc_install_webhook_service.sh",), "Installs/repairs the persistent user systemd webhook service using the safe foreground starter."),
    ManualAction("Data Tools", "Open web monitor", ("bash", "-lc", "PC_MONITOR_MODE=web ./pc_open_monitor.sh"), "Starts/opens the optional browser-based monitor at the configured local URL."),

    # --- 4. Testing & Validation: sandbox runs and health checks -------------
    ManualAction("Testing & Validation", "Run test zone", ("./pc_test_zone.py", "--limit", "5", "--apply"), "Re-runs the latest 5 records in the isolated sandbox (records_test/); the real archive is left untouched.", RECORDS_TEST_PARENT),
    ManualAction("Testing & Validation", "Review system health", ("./review_panamacompra_system.sh",), "Runs the repository health checks and troubleshooting summary."),

    # --- 5. Folder Management: open data storage locations -------------------
    ManualAction("Folder Management", "Open index folder", ("bash", "-c", "xdg-open \"$(pwd)/data/index\""), "Opens the main index folder where collected records are stored."),
    ManualAction("Folder Management", "Open records folder", ("bash", "-c", "xdg-open \"$(pwd)/records\""), "Opens the records archive folder containing organized record subfolders."),
    ManualAction("Folder Management", "Open logs folder", ("bash", "-c", "xdg-open \"$(pwd)/data/logs\""), "Opens the logs folder containing worker and action logs."),
    ManualAction("Folder Management", "Open data root", ("bash", "-c", "xdg-open \"$(pwd)/data\""), "Opens the main data directory containing index, logs, queue and config."),
    ManualAction("Folder Management", "Open index parent folder", ("bash", "-c", "xdg-open \"$(dirname \"$(pwd)/data/index\")\""), "Opens the parent directory that contains the index folder."),
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
    "MODE": "LIVE",
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


def load_record_index(limit: int = 500) -> list[dict[str, str]]:
    """Read collected records (NUMERO + description + folder/link) from the
    archive DB for the monitor's record-index selector. Newest first.

    Never raises: a missing, empty or locked database simply yields an empty
    list so the monitor keeps working before the collector has ever run.
    """
    if not ARCHIVE_DB.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{ARCHIVE_DB}?mode=ro", uri=True, timeout=2)
    except sqlite3.Error:
        return []
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT numero, "
            "COALESCE(NULLIF(short_description, ''), descripcion, '') AS descripcion, "
            "COALESCE(record_folder, '') AS record_folder, "
            "COALESCE(link, '') AS link, "
            "COALESCE(detail_status, '') AS detail_status, "
            "COALESCE(detail_saved_at, '') AS detail_saved_at, "
            "COALESCE(detail_json_path, '') AS detail_json_path, "
            "COALESCE(index_downloaded_at, first_seen, '') AS index_downloaded_at, "
            "COALESCE(last_seen, '') AS last_seen, "
            "COALESCE(status_changed_at, '') AS status_changed_at, "
            "COALESCE(folder_renamed_at, '') AS folder_renamed_at, "
            "COALESCE(notified_at, '') AS notified_at, "
            "COALESCE(finish_date_guess, '') AS finish_date_guess "
            "FROM opportunities "
            "ORDER BY COALESCE(first_seen, '') DESC, numero DESC "
            "LIMIT ?",
            (limit,),
        ).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        conn.close()
    return [
        {
            "numero": str(row["numero"] or ""),
            "descripcion": str(row["descripcion"] or ""),
            "record_folder": str(row["record_folder"] or ""),
            "link": str(row["link"] or ""),
            "detail_status": str(row["detail_status"] or ""),
            "detail_saved_at": str(row["detail_saved_at"] or ""),
            "detail_json_path": str(row["detail_json_path"] or ""),
            "index_downloaded_at": str(row["index_downloaded_at"] or ""),
            "last_seen": str(row["last_seen"] or ""),
            "status_changed_at": str(row["status_changed_at"] or ""),
            "folder_renamed_at": str(row["folder_renamed_at"] or ""),
            "notified_at": str(row["notified_at"] or ""),
            "finish_date_guess": str(row["finish_date_guess"] or ""),
        }
        for row in rows
    ]


_FOLDER_COMPLETE_RE = re.compile(r"^\[[^\]\[]+\]-\[[^\]\[]+\]-\[[^\]\[]+\]$")


def _folder_name_complete(path_text: str) -> bool:
    leaf = Path(path_text or "").name
    if not _FOLDER_COMPLETE_RE.match(leaf):
        return False
    if any(ch in leaf for ch in '<>:"/\\|?*'):
        return False
    lower = leaf.lower()
    return "[]" not in leaf and "[unknown]" not in lower and "[no-finish]" not in lower and "[no-numero]" not in lower and "[no-desc]" not in lower


def _file_exists(path_text: str) -> bool:
    return bool(path_text) and Path(path_text).exists()


def _detail_files_complete(row: sqlite3.Row) -> bool:
    numero = str(row["numero"] or "")
    folder = Path(str(row["record_folder"] or ""))
    detail_json = Path(str(row["detail_json_path"] or "")) if row["detail_json_path"] else folder / "details" / f"{numero}.detail.json"
    detail_dir = detail_json.parent
    needed = [
        detail_json,
        detail_dir / f"{numero}.detail.html",
        detail_dir / f"{numero}.detail.txt",
        detail_dir / f"{numero}.calendar.ics",
    ]
    return all(path.exists() for path in needed)


def _review_issue(row: sqlite3.Row) -> str:
    issues = []
    if not _file_exists(str(row["index_json_path"] or "")):
        issues.append("missing index JSON")
    if (row["detail_status"] or "") == "saved" and not _detail_files_complete(row):
        issues.append("saved detail incomplete")
    if not _folder_name_complete(str(row["record_folder"] or "")):
        issues.append("folder name incomplete")
    if row["detail_status"] in ("pending", "failed"):
        issues.append(f"detail {row['detail_status']}")
    return ", ".join(issues)

def db_review_stats() -> dict[str, object]:
    """Aggregate counts for the Database review panel (totals, detail-queue state,
    notification state, and a per-group breakdown). Never raises; a missing/locked
    DB yields zeros so the panel renders before the collector has ever run."""
    empty = {
        "total": 0, "saved": 0, "pending": 0, "failed": 0,
        "notified": 0, "with_detail_json": 0, "index_downloaded": 0, "index_json_on_disk": 0, "detail_downloaded": 0, "details_complete": 0, "folder_name_incomplete": 0, "review_needed": 0, "review_rows": [], "possible_pending": 0, "status_changes": 0, "renamed_folders": 0, "calendar_exports": 0, "latest": [], "groups": [], "db_exists": ARCHIVE_DB.exists(),
    }
    if not ARCHIVE_DB.exists():
        return empty
    try:
        conn = sqlite3.connect(f"file:{ARCHIVE_DB}?mode=ro", uri=True, timeout=2)
    except sqlite3.Error:
        return empty
    try:
        conn.row_factory = sqlite3.Row
        columns = {r[1] for r in conn.execute("PRAGMA table_info(opportunities)").fetchall()}
        has_notified = "notified_at" in columns

        def count(where: str = "") -> int:
            sql = "SELECT COUNT(*) FROM opportunities" + (f" WHERE {where}" if where else "")
            return int(conn.execute(sql).fetchone()[0])

        rows = conn.execute("SELECT numero, detail_status, record_folder, index_json_path, detail_json_path FROM opportunities").fetchall()
        review_rows = []
        index_json_on_disk = 0
        details_complete = 0
        folder_name_incomplete = 0
        for row in rows:
            if _file_exists(str(row["index_json_path"] or "")):
                index_json_on_disk += 1
            if _detail_files_complete(row):
                details_complete += 1
            if not _folder_name_complete(str(row["record_folder"] or "")):
                folder_name_incomplete += 1
            issue = _review_issue(row)
            if issue:
                review_rows.append({"numero": str(row["numero"] or ""), "detail_status": str(row["detail_status"] or ""), "issue": issue, "folder": Path(str(row["record_folder"] or "")).name})

        stats = {
            "db_exists": True,
            "total": count(),
            "saved": count("detail_status = 'saved'"),
            "pending": count("detail_status = 'pending'"),
            "failed": count("detail_status = 'failed'"),
            "index_downloaded": count("COALESCE(index_downloaded_at, '') <> ''"),
            "index_json_on_disk": index_json_on_disk,
            "detail_downloaded": count("COALESCE(detail_saved_at, '') <> ''"),
            "details_complete": details_complete,
            "folder_name_incomplete": folder_name_incomplete,
            "review_needed": len(review_rows),
            "review_rows": review_rows[:12],
            "possible_pending": count("detail_status <> 'saved' OR COALESCE(detail_json_path, '') = ''"),
            "status_changes": count("COALESCE(pending_status_change, '') <> '' OR COALESCE(status_changed_at, '') <> ''"),
            "renamed_folders": count("COALESCE(folder_renamed_at, '') <> ''"),
            "calendar_exports": count("COALESCE(last_calendar_export_path, '') <> ''"),
            "notified": count("notified_at IS NOT NULL") if has_notified else 0,
            "with_detail_json": count("COALESCE(detail_json_path, '') <> ''"),
            "latest": [dict(r) for r in conn.execute("SELECT numero, detail_status, COALESCE(index_downloaded_at, first_seen, '') AS index_downloaded_at, COALESCE(detail_saved_at, '') AS detail_saved_at, COALESCE(last_seen, '') AS last_seen, COALESCE(status_changed_at, '') AS status_changed_at FROM opportunities ORDER BY datetime(COALESCE(detail_saved_at, index_downloaded_at, first_seen, last_seen)) DESC LIMIT 8").fetchall()],
            "groups": [
                (str(r["grupo"] or "(sin grupo)"), int(r["c"]))
                for r in conn.execute(
                    "SELECT grupo, COUNT(*) AS c FROM opportunities "
                    "GROUP BY grupo ORDER BY c DESC"
                ).fetchall()
            ],
        }
    except sqlite3.Error:
        return empty
    finally:
        conn.close()
    return stats


# How many days ahead still counts as "next to expire" (amber) instead of a calm
# "upcoming" (green). Records past their DTEND are "expired" (red).
SOON_DAYS = setting_int("PC_MONITOR_DEADLINE_SOON_DAYS", 7, minimum=1)
STATUS_COLORS = {"expired": "#fca5a5", "soon": "#fcd34d", "upcoming": "#86efac", "unknown": "#94a3b8"}
STATUS_TAGS = {"expired": "EXPIRED", "soon": "SOON", "upcoming": "ok", "unknown": "no date"}
# Friendly labels for the status selector, mapped back to the internal keys.
STATUS_FILTER_CHOICES = ("All", "Next to expire", "Expired", "Upcoming")
STATUS_FILTER_KEYS = {"Next to expire": "soon", "Expired": "expired", "Upcoming": "upcoming"}


def parse_deadline(rec: dict[str, str]) -> datetime | None:
    """The record's DTEND/deadline (finish_date_guess 'YYYY-MM-DD_HH:MM'), or None."""
    raw = (rec.get("finish_date_guess") or "").strip().replace("_", " ")
    if not raw:
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def deadline_text(rec: dict[str, str]) -> str:
    dt = parse_deadline(rec)
    return dt.strftime("%Y-%m-%d %H:%M") if dt else "—"


def downloaded_text(rec: dict[str, str]) -> str:
    raw = (rec.get("detail_saved_at") or "").strip()
    return raw[:16].replace("T", " ") if raw else "—"

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
    if running("[w]ebhook_listener.py") or running("[p]ython3? -u ./webhook_listener.py"):
        return True
    try:
        result = subprocess.run(["docker", "compose", "ps", "--status", "running", "webhook"], cwd=BASE_DIR, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=3)
        return "webhook" in result.stdout.lower()
    except Exception:
        return False

def process_snapshot() -> dict[str, bool]:
    """Detect all running PanamaCompra processes for the monitor display."""
    worker = running("[p]c_run_all_worker.sh")
    test = running("[p]ython(3)? -u ./pc_test_zone.py")
    updater = running("[u]pdate_local_copy.sh") or running("[p]c_update_loader.py")
    webhook = webhook_running()
    monitor_tk = running("[p]c_monitor_tk.py") or running("[p]ython3? -u ./pc_monitor_tk.py")
    monitor_server = running("[p]c_monitor_server.py") or running("[p]ython3? -u ./pc_monitor_server.py")
    timer = running("[p]c_next_run_timer.py")
    
    return {
        "normal_run": worker and not test,
        "test_run": test,
        "worker": worker,
        "index": running("[p]ython(3)? -u ./pc_index_collector.py"),
        "detail": running("[p]ython(3)? -u ./pc_detail_downloader.py"),
        "calendar": running("[p]ython(3)? -u ./pc_build_(detail_views|calendar).py"),
        # WhatsApp MESSAGING step: visible while the notifier sends messages.
        "messaging": running("[p]c_notify_new_records.py"),
        "request": REQUEST_FLAG.exists(),
        # Additional runners that should be stopped by pc_stop_run_all.sh
        "updater": updater,
        "webhook": webhook,
        "monitor_tk": monitor_tk,
        "monitor_server": monitor_server,
        "timer": timer,
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
    return (datetime.now() - updated).total_seconds() > int(os.environ.get("PC_MONITOR_STALE_SECONDS", "120"))


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
        "done": done,
        "auto_close_enabled": done and progress.get("MODE", "LIVE").upper() == "LIVE" and not processes.get("test_run", False),
        "refresh_seconds": IDLE_REFRESH_SECONDS if done else REFRESH_SECONDS,
        "auto_close_seconds": AUTO_CLOSE_SECONDS,
        "worker_log": tail(WORKER_LOG, 18),
        "current_log": tail(CURRENT_LOG, 28),
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "waha_chat_id": WAHA_CHAT_ID_PATH.read_text(encoding="utf-8", errors="replace").strip() if WAHA_CHAT_ID_PATH.exists() else "",
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
    # The logs pane (now row 8, after the Database review and Reset sections) is
    # the one that should absorb extra vertical space.
    content.rowconfigure(8, weight=1)

    def update_scroll_region(_event: tk.Event | None = None) -> None:
        canvas.configure(scrollregion=canvas.bbox("all"))

    def resize_content(event: tk.Event) -> None:
        canvas.itemconfigure(content_window, width=event.width)

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
    ttk.Label(header, text="Processes (off is normal when a step is idle; detail only runs during STEP 2)", style="Card.TLabel").grid(row=5, column=0, sticky="w", pady=(8, 2))
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

        def apply_hidden(is_hidden: bool) -> None:
            hidden.set(is_hidden)
            for child in content_children:
                if is_hidden:
                    child.grid_remove()
                else:
                    child.grid()
            button.configure(text="Show" if is_hidden else "Hide")

        button.configure(command=lambda: apply_hidden(not hidden.get()))
        # Sections start collapsed by default so the monitor opens compact; the
        # operator expands only the panels they need.
        if start_hidden:
            apply_hidden(True)

    # ========================================================================
    # SECTION 1: RUN CONTROLS - request a restart or test-zone run
    # ========================================================================
    controls = ttk.Frame(content, style="Card.TFrame", padding=14)
    controls.grid(row=1, column=0, sticky="ew", padx=14, pady=8)
    controls.columnconfigure(5, weight=1)
    button_status_var = tk.StringVar(value="")
    run_mode_var = tk.StringVar(value="restart")
    index_limit_var = tk.StringVar(value="20")
    detail_limit_var = tk.StringVar(value="99")
    detail_order_var = tk.StringVar(value=setting("PC_DETAIL_ORDER", "oldest"))

    def selected_limit(var: tk.StringVar, default: str) -> str:
        value = var.get().strip() or default
        return value if value.isdigit() and int(value) > 0 else default

    def request_run_now() -> None:
        index_limit = selected_limit(index_limit_var, "20")
        detail_limit = selected_limit(detail_limit_var, "99")
        mode = run_mode_var.get()
        detail_order = "newest" if detail_order_var.get().lower().startswith("new") else "oldest"
        env = monitor_env(); env["PC_DETAIL_ORDER"] = detail_order
        if mode == "test":
            subprocess.Popen([str(BASE_DIR / "pc_test_zone.py"), "--limit", detail_limit, "--order", detail_order, "--apply"], cwd=BASE_DIR, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            button_status_var.set(f"Test-zone run requested with detail limit {detail_limit}, starting from {detail_order}.")
            return
        if mode == "manual":
            subprocess.Popen([str(BASE_DIR / "pc_run_all_now.sh"), detail_limit, index_limit, "MANUAL"], cwd=BASE_DIR, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            button_status_var.set(f"Manual run started with index limit {index_limit}, detail limit {detail_limit}, starting from {detail_order}.")
            return
        subprocess.Popen([str(BASE_DIR / "pc_request_run_all.sh"), "0", "RESTART", "0"], cwd=BASE_DIR, env=monitor_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        button_status_var.set("Restart-pending run requested with normal configured limits.")

    ttk.Label(controls, text="Run controls", style="Title.TLabel").grid(row=0, column=0, columnspan=6, sticky="w", pady=(0, 8))
    # Mode is explicit: AUTO is reserved for changedetection/webhook-triggered
    # runs; manual launches are either RESTART (real pipeline) or TEST (sandbox).
    # The Limit entry is wide enough for large counts. The whole row is disabled
    # while a collection is active (e.g. a webhook-triggered run) so you cannot
    # change mode or queue a conflicting run mid-flight; it re-enables when idle.
    ttk.Label(controls, text="Mode:", style="Card.TLabel").grid(row=1, column=0, sticky="w")
    auto_radio = ttk.Radiobutton(controls, text="automatic", value="auto", variable=run_mode_var, style="Card.TRadiobutton", state="disabled")
    auto_radio.grid(row=1, column=1, sticky="w")
    live_radio = ttk.Radiobutton(controls, text="restart pending", value="restart", variable=run_mode_var, style="Card.TRadiobutton")
    live_radio.grid(row=1, column=2, sticky="w")
    manual_radio = ttk.Radiobutton(controls, text="manual run", value="manual", variable=run_mode_var, style="Card.TRadiobutton")
    manual_radio.grid(row=1, column=3, sticky="w")
    test_radio = ttk.Radiobutton(controls, text="test run", value="test", variable=run_mode_var, style="Card.TRadiobutton")
    test_radio.grid(row=1, column=4, sticky="w", padx=(0, 16))
    ttk.Label(controls, text="Index limit:", style="Card.TLabel").grid(row=2, column=0, sticky="e")
    index_limit_entry = ttk.Entry(controls, textvariable=index_limit_var, width=8)
    index_limit_entry.grid(row=2, column=1, sticky="w", padx=(6, 12))
    ttk.Label(controls, text="Detail limit:", style="Card.TLabel").grid(row=2, column=2, sticky="e")
    detail_limit_entry = ttk.Entry(controls, textvariable=detail_limit_var, width=8)
    detail_limit_entry.grid(row=2, column=3, sticky="w", padx=(6, 16))
    ttk.Label(controls, text="Start from:", style="Card.TLabel").grid(row=2, column=4, sticky="e")
    detail_order_box = ttk.Combobox(controls, textvariable=detail_order_var, values=("newest", "oldest"), width=8, state="readonly")
    detail_order_box.grid(row=2, column=5, sticky="w", padx=(6, 16))
    run_button = ttk.Button(controls, text="Request selected run", command=request_run_now, style="Accent.TButton")
    run_button.grid(row=3, column=4, sticky="w")
    ttk.Label(controls, textvariable=button_status_var, style="Card.TLabel", wraplength=520).grid(row=4, column=0, columnspan=6, sticky="w", pady=(8, 0))
    add_tooltip(auto_radio, "automatic = shown for changedetection/webhook runs; not selectable manually.")
    add_tooltip(live_radio, "restart pending = queue the normal collector pipeline (real archive).")
    add_tooltip(manual_radio, "manual run = start the worker immediately from this monitor.")
    add_tooltip(test_radio, "test run = the isolated test zone (records_test/), real archive untouched.")
    add_tooltip(index_limit_entry, "Maximum index pages per status group to collect/process.")
    add_tooltip(detail_limit_entry, "Maximum detail pages (manual) or sandbox records (test) to process this run; disabled for automatic/restart pending.")
    add_tooltip(detail_order_box, "When a limit is used, choose whether to start from newest or oldest DB records.")
    add_tooltip(run_button, "Queue the selected run with the chosen mode and limit (disabled while a run is active).")
    add_section_toggle(controls, button_column=5)

    # Keys that mean "real collection work is happening". A webhook-triggered run
    # shows up here (worker/index/detail/...), so the run controls lock while any
    # of them are active and unlock once the run is fully idle.
    run_control_widgets = (live_radio, manual_radio, test_radio, run_button)
    limit_widgets = (index_limit_entry, detail_limit_entry, detail_order_box)

    def update_run_controls(snap: dict[str, object]) -> None:
        processes = snap.get("processes", {}) or {}
        busy = any(processes.get(key) for key in WORK_PROCESS_KEYS)
        target_state = "disabled" if busy else "normal"
        for widget in run_control_widgets:
            try:
                widget.configure(state=target_state)
            except tk.TclError:
                pass
        limits_allowed = (not busy) and run_mode_var.get() in {"manual", "test"}
        for widget in limit_widgets:
            try:
                widget.configure(state=("normal" if limits_allowed else "disabled"))
            except tk.TclError:
                pass
        current_mode = str(snap.get("MODE", "")).strip().upper()
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
    # SECTION 3: SETTINGS - editable fields with defaults; leave as-is to keep
    # the defaults. Saved to data/config/monitor_settings.env and applied live.
    # (Rendered at grid row 3, below the Live diagnostics section.)
    # ========================================================================
    settings = ttk.Frame(content, style="Card.TFrame", padding=14)
    # Live diagnostics sits at row 2 (directly under Run controls); Settings moves
    # to row 3, so the live run status is visible without scrolling past Settings.
    settings.grid(row=3, column=0, sticky="ew", padx=14, pady=8)
    settings.columnconfigure(1, weight=1)
    settings.columnconfigure(3, weight=1)

    alpha_var = tk.StringVar(value=f"{runtime['alpha']:.2f}")
    autoclose_var = tk.StringVar(value=str(runtime["auto_close"]))
    refresh_var = tk.StringVar(value=str(runtime["refresh"]))
    idle_var = tk.StringVar(value=str(runtime["idle_refresh"]))
    source_var = tk.StringVar(value=setting("PC_WAHA_SOURCE", "Panamá Compra"))
    waha_var = tk.StringVar(value=(WAHA_CHAT_ID_PATH.read_text(encoding="utf-8", errors="replace").strip() if WAHA_CHAT_ID_PATH.exists() else ""))
    existing_keywords = []
    if WAHA_KEYWORDS_PATH.exists():
        existing_keywords = [k.strip() for k in WAHA_KEYWORDS_PATH.read_text(encoding="utf-8", errors="replace").splitlines() if k.strip() and not k.startswith("#")]
    keywords_var = tk.StringVar(value=", ".join(existing_keywords))
    notify_whatsapp_var = tk.BooleanVar(value=setting("PC_NOTIFY_WHATSAPP", "1") != "0")
    import_calendar_var = tk.BooleanVar(value=setting("PC_CALENDAR_AUTO_IMPORT", "0") == "1")
    records_dir_var = tk.StringVar(value=setting("PC_RECORDS_DIR", str(BASE_DIR / "records")))
    calendar_dir_var = tk.StringVar(value=setting("PC_CALENDAR_DIR", str(BASE_DIR / "data" / "calendar")))
    records_test_dir_var = tk.StringVar(value=setting("PC_RECORDS_TEST_DIR", str(BASE_DIR / "records_test")))
    index_dir_var = tk.StringVar(value=setting("PC_INDEX_DIR", str(BASE_DIR / "data" / "index")))
    config_dir_var = tk.StringVar(value=setting("PC_CONFIG_DIR", str(CONFIG_DIR)))
    soon_days_var = tk.StringVar(value=setting("PC_MONITOR_DEADLINE_SOON_DAYS", str(SOON_DAYS)))
    max_attempts_var = tk.StringVar(value=setting("PC_MAX_DETAIL_ATTEMPTS", "5"))

    def field(row: int, col: int, label: str, var: tk.StringVar, width: int, tip: str) -> None:
        ttk.Label(settings, text=label, style="Card.TLabel").grid(row=row, column=col, sticky="w", padx=(0, 6), pady=3)
        entry = ttk.Entry(settings, textvariable=var, width=width)
        entry.grid(row=row, column=col + 1, sticky="ew", pady=3, padx=(0, 12))
        add_tooltip(entry, tip)

    ttk.Label(settings, text="Settings (editable — leave a field unchanged to keep its default)", style="Title.TLabel").grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 8))
    field(1, 0, "Transparency 0.30–1.00:", alpha_var, 8, "Whole-window opacity (text shares it). Default 0.85 = lightly translucent and readable. Lower it toward 0.30 for a more see-through window; 1.00 = fully opaque. Applied live when you click Apply.")
    field(1, 2, "Auto-close seconds (0=off):", autoclose_var, 8, "Seconds to count down after a LIVE run finishes before this window closes. 0 keeps it open. Default 20.")
    field(2, 0, "Active refresh seconds:", refresh_var, 8, "How often (seconds) the monitor refreshes while a run is active. Minimum 2. Default 3.")
    field(2, 2, "Idle refresh seconds:", idle_var, 8, "How often the monitor refreshes when idle (low power). Default 15.")
    field(3, 0, "WhatsApp source label:", source_var, 8, "Text shown as '📌 Fuente:' in the WhatsApp messages (default 'Panamá Compra'). Automatic announcements are sent later in the post-detail MESSAGING step when enabled.")
    ttk.Label(settings, text="WhatsApp destination chat id (…@g.us):", style="Card.TLabel").grid(row=4, column=0, sticky="w", pady=3)
    chat_entry = ttk.Entry(settings, textvariable=waha_var)
    chat_entry.grid(row=4, column=1, columnspan=3, sticky="ew", pady=3)
    add_tooltip(chat_entry, "Destination WhatsApp group/channel id for the automated 'what is new' messages. Saved to data/config/waha_chat_id.txt.")
    ttk.Label(settings, text="WhatsApp keywords (comma separated; blank = all):", style="Card.TLabel").grid(row=5, column=0, sticky="w", pady=3)
    kw_entry = ttk.Entry(settings, textvariable=keywords_var)
    kw_entry.grid(row=5, column=1, columnspan=3, sticky="ew", pady=3)
    add_tooltip(kw_entry, "Only announce new records matching one of these keywords (title/description/entity). Blank announces every new record. Saved to data/config/waha_keywords.txt.")
    field(6, 0, "Records folder:", records_dir_var, 36, "Where normal record folders are stored. Environment key: PC_RECORDS_DIR. Relative paths are resolved from the checkout root.")
    field(7, 0, "Calendar packages folder:", calendar_dir_var, 36, "Where timestamped .ics calendar packages are written. Environment key: PC_CALENDAR_DIR.")
    field(8, 0, "Test sandbox folder:", records_test_dir_var, 36, "Where the isolated test zone stores re-downloaded records. Environment key: PC_RECORDS_TEST_DIR.")
    field(9, 0, "Index folder:", index_dir_var, 36, "Operator-facing index/data folder. Environment key: PC_INDEX_DIR.")
    field(10, 0, "Config folder:", config_dir_var, 36, "Monitor/notifier config folder. Environment key: PC_CONFIG_DIR.")
    field(11, 0, "Soon days:", soon_days_var, 8, "Deadline filter threshold for next-to-expire records. Environment key: PC_MONITOR_DEADLINE_SOON_DAYS.")
    field(11, 2, "Max detail attempts:", max_attempts_var, 8, "Retry attempts before detail rows stop retrying. Environment key: PC_MAX_DETAIL_ATTEMPTS.")
    notify_check = ttk.Checkbutton(settings, text="Notify by WhatsApp after detail/calendar", variable=notify_whatsapp_var, style="Card.TCheckbutton")
    notify_check.grid(row=12, column=0, columnspan=2, sticky="w", pady=3)
    calendar_check = ttk.Checkbutton(settings, text="Import/open generated calendar events", variable=import_calendar_var, style="Card.TCheckbutton")
    calendar_check.grid(row=12, column=2, columnspan=2, sticky="w", pady=3)
    add_tooltip(notify_check, "Turn off to skip automatic WhatsApp MESSAGING after a run. Manual selected-record notification buttons remain available.")
    add_tooltip(calendar_check, "Turn on to open generated .ics calendar packages/events after they are built.")

    def open_configured_folder(var: tk.StringVar, fallback: str) -> None:
        path = Path(var.get().strip() or fallback).expanduser()
        if not path.is_absolute():
            path = BASE_DIR / path
        path.mkdir(parents=True, exist_ok=True)
        opener = os.environ.get("PC_OPEN_FOLDER_COMMAND", "xdg-open")
        subprocess.Popen([opener, str(path)], cwd=BASE_DIR, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        button_status_var.set(f"Opened folder: {path}")

    folder_buttons = ttk.Frame(settings, style="Card.TFrame")
    folder_buttons.grid(row=13, column=1, columnspan=3, sticky="w", pady=(10, 0))
    for idx, (label, var, fallback) in enumerate((
        ("Open records", records_dir_var, str(BASE_DIR / "records")),
        ("Open calendar", calendar_dir_var, str(BASE_DIR / "data" / "calendar")),
        ("Open test", records_test_dir_var, str(BASE_DIR / "records_test")),
        ("Open index", index_dir_var, str(BASE_DIR / "data" / "index")),
        ("Open config", config_dir_var, str(CONFIG_DIR)),
    )):
        ttk.Button(folder_buttons, text=label, command=lambda v=var, f=fallback: open_configured_folder(v, f)).grid(row=0, column=idx, padx=(0, 6))

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
        keywords = [k.strip() for k in re.split(r"[,\n]", keywords_var.get()) if k.strip()]
        WAHA_KEYWORDS_PATH.write_text(("\n".join(keywords) + "\n") if keywords else "", encoding="utf-8")

        updates = {
            "PC_MONITOR_TK_ALPHA": f"{alpha:.2f}",
            "PC_MONITOR_TK_AUTO_CLOSE_SECONDS": str(runtime["auto_close"]),
            "PC_MONITOR_TK_REFRESH_SECONDS": str(runtime["refresh"]),
            "PC_MONITOR_TK_IDLE_REFRESH_SECONDS": str(runtime["idle_refresh"]),
            "PC_WAHA_SOURCE": source_var.get().strip() or "Panamá Compra",
            "PC_NOTIFY_WHATSAPP": "1" if notify_whatsapp_var.get() else "0",
            "PC_CALENDAR_AUTO_IMPORT": "1" if import_calendar_var.get() else "0",
            "PC_RECORDS_DIR": records_dir_var.get().strip() or str(BASE_DIR / "records"),
            "PC_CALENDAR_DIR": calendar_dir_var.get().strip() or str(BASE_DIR / "data" / "calendar"),
            "PC_RECORDS_TEST_DIR": records_test_dir_var.get().strip() or str(BASE_DIR / "records_test"),
            "PC_INDEX_DIR": index_dir_var.get().strip() or str(BASE_DIR / "data" / "index"),
            "PC_CONFIG_DIR": config_dir_var.get().strip() or str(CONFIG_DIR),
            "PC_MONITOR_DEADLINE_SOON_DAYS": soon_days_var.get().strip() or str(SOON_DAYS),
            "PC_MAX_DETAIL_ATTEMPTS": max_attempts_var.get().strip() or "5",
            "PC_DETAIL_ORDER": detail_order_var.get().strip() or "oldest",
        }
        merged = load_settings_file()
        merged.update(updates)
        lines = [
            "# PanamaCompra monitor settings (KEY=VALUE).",
            "# Edited from the monitor Settings panel; same-named environment",
            "# variables override these at startup.",
        ]
        lines += [f"{key}={merged[key]}" for key in sorted(merged)]
        SETTINGS_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
        _SETTINGS_FILE.clear()
        _SETTINGS_FILE.update(merged)
        button_status_var.set("Settings applied (transparency live) and saved to data/config/monitor_settings.env.")

    apply_button = ttk.Button(settings, text="Apply & save settings", command=apply_settings, style="Accent.TButton")
    apply_button.grid(row=13, column=0, sticky="w", pady=(10, 0))
    add_tooltip(apply_button, "Apply transparency immediately, persist all settings to data/config/monitor_settings.env, and save the WhatsApp destination/keywords files.")
    ttk.Label(settings, text="WhatsApp sending also requires PC_WAHA_ENABLED=1 and a WAHA server (default port 3000). Source label, destination and keywords here are read by the notifier; automatic messages are sent only in the post-detail MESSAGING step when enabled.", style="Card.TLabel", wraplength=820).grid(row=14, column=0, columnspan=4, sticky="w", pady=(8, 0))
    add_section_toggle(settings, button_column=3)

    # ========================================================================
    # SECTION 2: LIVE DIAGNOSTICS - Phase, Mode, Item, Started, etc.
    # Rendered at grid row 2 (directly under Run controls) so the live run status
    # is the third section on screen, above Settings.
    # ========================================================================
    diag = ttk.Frame(content, style="Card.TFrame", padding=14)
    diag.grid(row=2, column=0, sticky="ew", padx=14, pady=8)
    # Keep the two label columns narrow and let the two value columns absorb the
    # remaining width, so large counters and long descriptions stay readable.
    diag.columnconfigure(0, weight=0, minsize=130)
    diag.columnconfigure(1, weight=1, minsize=200)
    diag.columnconfigure(2, weight=0, minsize=130)
    diag.columnconfigure(3, weight=1, minsize=200)

    ttk.Label(diag, text="Live diagnostics", style="Title.TLabel").grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 8))

    # Fields are grouped left-to-right, top-to-bottom: lifecycle, progress,
    # timing, then record counters. "Extra" is rendered separately on its own
    # full-width row because it can hold a long human-readable note.
    fields = [
        ("Phase", "PHASE"), ("Status", "STATUS"),
        ("Mode", "MODE"), ("ETA", "ETA"), ("Index limit", "INDEX_LIMIT"), ("Detail limit", "DETAIL_LIMIT"),
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
    # SECTION 4: RECORD INDEX - pick a collected record by NUMERO + description
    # and open its archive folder or the portal page. Populated read-only from
    # data/panamacompra_archive.db; empty until the collector has run.
    # ========================================================================
    record_index = ttk.Frame(content, style="Card.TFrame", padding=14)
    record_index.grid(row=4, column=0, sticky="ew", padx=14, pady=8)
    record_index.columnconfigure(1, weight=1)

    ttk.Label(record_index, text="Record index", style="Title.TLabel").grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))

    # The folder selector is a type-to-filter box plus a dedicated, self-scrolling
    # list (with its own scrollbar) instead of a dropdown. A dropdown's popup
    # scroll fought the whole-page scroll and the long "NUMERO — description"
    # entries were impossible to separate; this list scrolls on its own (see the
    # Listbox branch in on_mousewheel) and the filter box narrows it instantly.
    index_records: list[dict[str, str]] = []
    index_filtered: list[dict[str, str]] = []
    index_filter_var = tk.StringVar(value="")
    index_status_var = tk.StringVar(value="All")
    index_mindate_var = tk.StringVar(value="")
    index_downloaded_mindate_var = tk.StringVar(value="")
    index_inserted_mindate_var = tk.StringVar(value="")
    index_lastseen_mindate_var = tk.StringVar(value="")
    index_statuschanged_mindate_var = tk.StringVar(value="")
    index_renamed_mindate_var = tk.StringVar(value="")
    index_notified_mindate_var = tk.StringVar(value="")
    index_detail_var = tk.StringVar(value="No records collected yet. Run the collector, then click Refresh list.")

    ttk.Label(record_index, text="Filter:", style="Card.TLabel").grid(row=1, column=0, sticky="w", padx=(0, 6))
    index_filter_entry = ttk.Entry(record_index, textvariable=index_filter_var)
    index_filter_entry.grid(row=1, column=1, columnspan=2, sticky="ew", pady=3)
    add_tooltip(index_filter_entry, "Type any part of a NUMERO or description to narrow the list below.")

    # Dates selector: a status filter (expired / next to expire / upcoming) plus a
    # DTEND date picker. Each row shows its downloaded date and DTEND, colored red
    # (expired), amber (next to expire) or green (upcoming) so it is obvious at a
    # glance which records are still actionable.
    dates_row = ttk.Frame(record_index, style="Card.TFrame")
    dates_row.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(0, 4))
    ttk.Label(dates_row, text="Status:", style="Card.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 6))
    index_status_box = ttk.Combobox(dates_row, textvariable=index_status_var, values=STATUS_FILTER_CHOICES, width=15, state="readonly")
    index_status_box.grid(row=0, column=1, sticky="w", padx=(0, 16))
    ttk.Label(dates_row, text="DTEND on/after:", style="Card.TLabel").grid(row=0, column=2, sticky="e", padx=(0, 6))
    index_mindate_entry = ttk.Entry(dates_row, textvariable=index_mindate_var, width=12)
    index_mindate_entry.grid(row=0, column=3, sticky="w", padx=(0, 12))
    ttk.Label(dates_row, text="Detail saved on/after:", style="Card.TLabel").grid(row=0, column=4, sticky="e", padx=(0, 6))
    index_downloaded_entry = ttk.Entry(dates_row, textvariable=index_downloaded_mindate_var, width=12)
    index_downloaded_entry.grid(row=0, column=5, sticky="w", padx=(0, 8))
    extra_dates = ttk.Frame(record_index, style="Card.TFrame")
    extra_dates.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(0, 4))
    extra_date_specs = [("Index inserted:", index_inserted_mindate_var), ("Last seen:", index_lastseen_mindate_var), ("Status changed:", index_statuschanged_mindate_var), ("Folder renamed:", index_renamed_mindate_var), ("Notified:", index_notified_mindate_var)]
    for c, (label, var) in enumerate(extra_date_specs):
        ttk.Label(extra_dates, text=label, style="Card.TLabel").grid(row=0, column=c*2, sticky="e", padx=(0, 4))
        ttk.Entry(extra_dates, textvariable=var, width=12).grid(row=0, column=c*2+1, sticky="w", padx=(0, 8))
    add_tooltip(index_status_box, "Filter by deadline: Next to expire = DTEND within the next few days, Expired = DTEND already passed, Upcoming = further out.")
    add_tooltip(index_mindate_entry, "Show only records whose DTEND (deadline) is on or after this date. Format YYYY-MM-DD; leave blank for no date limit.")
    add_tooltip(index_downloaded_entry, "Show only records downloaded into the local archive on or after this date. Format YYYY-MM-DD; leave blank for no downloaded-date limit.")

    list_frame = ttk.Frame(record_index, style="Card.TFrame")
    list_frame.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(4, 4))
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
    index_detail_text.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(6, 4))

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
        downloaded_part = downloaded.strftime("%y-%m-%d %H:%M") if downloaded else "not local"
        dt = parse_deadline(rec)
        dtend = dt.strftime("%Y-%m-%d %H:%M") if dt else "no date"
        tag = STATUS_TAGS[expiry_status(rec)]
        return f"[Downloaded {downloaded_part} | DTEND {dtend} {tag:>7}]  {index_label(rec)}"

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
            f"Detail saved: {downloaded_text(rec)}   ·   Index inserted: {(rec.get("index_downloaded_at") or "—")[:16].replace("T", " ")}   ·   Last seen: {(rec.get("last_seen") or "—")[:16].replace("T", " ")}\n"
            f"Status changed: {(rec.get("status_changed_at") or "—")[:16].replace("T", " ")}   ·   Folder renamed: {(rec.get("folder_renamed_at") or "—")[:16].replace("T", " ")}   ·   Notified: {(rec.get("notified_at") or "—")[:16].replace("T", " ")}   ·   DTEND: {deadline_text(rec)}"
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
        min_date = None
        raw_min = index_mindate_var.get().strip()
        if raw_min:
            try:
                min_date = datetime.strptime(raw_min, "%Y-%m-%d")
            except ValueError:
                min_date = None
        date_filters = []
        for key, var in (("detail_saved_at", index_downloaded_mindate_var), ("index_downloaded_at", index_inserted_mindate_var), ("last_seen", index_lastseen_mindate_var), ("status_changed_at", index_statuschanged_mindate_var), ("folder_renamed_at", index_renamed_mindate_var), ("notified_at", index_notified_mindate_var)):
            raw = var.get().strip()
            if raw:
                try:
                    date_filters.append((key, datetime.strptime(raw, "%Y-%m-%d")))
                except ValueError:
                    pass

        records = []
        for rec in index_records:
            if needle and needle not in index_label(rec).lower():
                continue
            if wanted_status and expiry_status(rec) != wanted_status:
                continue
            if min_date is not None:
                dt = parse_deadline(rec)
                if dt is None or dt < min_date:
                    continue
            blocked = False
            for key, min_dt in date_filters:
                raw = (rec.get(key) or "").strip().replace("T", " ")
                try:
                    dt = datetime.strptime(raw[:10], "%Y-%m-%d")
                except ValueError:
                    blocked = True
                    break
                if dt < min_dt:
                    blocked = True
                    break
            if blocked:
                continue
            records.append(rec)

        # Show the soonest deadlines first so "next to expire" floats to the top;
        # records without a DTEND sink to the bottom.
        records.sort(key=lambda r: (parse_deadline(r) or datetime.max))
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
        cmd = [str(BASE_DIR / "pc_notify_new_records.py"), "--force"]
        for numero in numeros:
            cmd.extend(["--record", numero])
        subprocess.Popen(cmd, cwd=BASE_DIR, env=monitor_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        button_status_var.set(f"WhatsApp notification requested for {len(numeros)} selected record(s).")

    def import_selected_calendars() -> None:
        numeros = selected_numeros()
        if not numeros:
            button_status_var.set("Select one or more records first (Ctrl/Shift-click).")
            return
        subprocess.Popen([str(BASE_DIR / "pc_import_selected_calendars.py"), "--open", *numeros], cwd=BASE_DIR, env=monitor_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        button_status_var.set(f"Calendar import requested for {len(numeros)} selected record(s).")

    def open_selected_detail_folder() -> None:
        rec = selected_record()
        if not rec:
            button_status_var.set("Select a record from the list first.")
            return
        detail_path = rec.get("detail_json_path") or ""
        folder = Path(detail_path).parent if detail_path else Path(rec["record_folder"]) / "details"
        if not folder.exists():
            button_status_var.set(f"Details folder not found on disk for {rec['numero'] or 'the selection'}.")
            return
        opener = os.environ.get("PC_OPEN_FOLDER_COMMAND", "xdg-open")
        subprocess.Popen([opener, str(folder)], cwd=BASE_DIR, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        button_status_var.set(f"Opened details folder for {rec['numero']}.")

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
    index_mindate_var.trace_add("write", apply_filter)
    for var in (index_downloaded_mindate_var, index_inserted_mindate_var, index_lastseen_mindate_var, index_statuschanged_mindate_var, index_renamed_mindate_var, index_notified_mindate_var):
        var.trace_add("write", apply_filter)

    index_buttons = ttk.Frame(record_index, style="Card.TFrame")
    index_buttons.grid(row=6, column=0, columnspan=3, sticky="w", pady=(6, 0))
    refresh_index_button = ttk.Button(index_buttons, text="Refresh list", command=refresh_index_list)
    refresh_index_button.grid(row=0, column=0, padx=(0, 8))
    open_folder_button = ttk.Button(index_buttons, text="Open record folder", command=open_selected_folder)
    open_folder_button.grid(row=0, column=1, padx=(0, 8))
    open_detail_folder_button = ttk.Button(index_buttons, text="Open details folder", command=open_selected_detail_folder)
    open_detail_folder_button.grid(row=0, column=2, padx=(0, 8))
    open_portal_button = ttk.Button(index_buttons, text="Open in portal", command=open_selected_portal)
    open_portal_button.grid(row=0, column=3, padx=(0, 8))
    notify_selected_button = ttk.Button(index_buttons, text="Notify selected WhatsApp", command=notify_selected_records)
    notify_selected_button.grid(row=0, column=4, padx=(0, 8))
    import_selected_button = ttk.Button(index_buttons, text="Import selected calendars", command=import_selected_calendars)
    import_selected_button.grid(row=0, column=5, padx=(0, 8))
    add_tooltip(refresh_index_button, "Reload the record list from the archive database (run after a new collection).")
    add_tooltip(open_folder_button, "Open the selected record's archive folder (or double-click a row).")
    add_tooltip(open_detail_folder_button, "Open the separated details/ folder for the selected record.")
    add_tooltip(open_portal_button, "Open the selected record's PanamaCompra portal page in the browser.")
    add_tooltip(notify_selected_button, "Send WhatsApp notifications for all selected records (Ctrl/Shift-click to select several).")
    add_tooltip(import_selected_button, "Export/open calendar ICS files for all selected records (Ctrl/Shift-click to select several).")

    refresh_index_list()
    add_section_toggle(record_index, button_column=2)

    # ========================================================================
    # SECTION 5: MANUAL ACTION BUTTONS - grouped by zone in a tidy 3-column grid.
    # Each button's explanation is shown as a hover tooltip (not an inline label)
    # so the grid stays compact and easy to scan.
    # ========================================================================
    actions = ttk.Frame(content, style="Card.TFrame", padding=14)
    actions.grid(row=5, column=0, sticky="ew", padx=14, pady=8)
    button_columns = 3
    for col in range(button_columns):
        actions.columnconfigure(col, weight=1, uniform="actions")
    ttk.Label(actions, text="Manual script buttons  (hover a button for what it does)", style="Title.TLabel").grid(row=0, column=0, columnspan=button_columns, sticky="w", pady=(0, 8))

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
    # SECTION 6: DATABASE REVIEW - read-only aggregate snapshot of the archive DB
    # (totals, detail-queue state, notification state, per-group breakdown) so the
    # operator can review the database state at a glance without opening sqlite.
    # ========================================================================
    db_review = ttk.Frame(content, style="Card.TFrame", padding=14)
    db_review.grid(row=6, column=0, sticky="ew", padx=14, pady=8)
    db_review.columnconfigure(0, weight=1)
    ttk.Label(db_review, text="Database review", style="Title.TLabel").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
    db_review_var = tk.StringVar(value="Loading database snapshot…")
    ttk.Label(db_review, textvariable=db_review_var, style="Card.TLabel", justify="left").grid(row=1, column=0, sticky="w")

    def refresh_db_review() -> None:
        s = db_review_stats()
        if not s.get("db_exists"):
            db_review_var.set("No database yet (data/panamacompra_archive.db). Run the collector first.")
            return
        groups = "   ·   ".join(f"{name}: {qty}" for name, qty in s["groups"]) or "—"
        review = "\n".join(f"  {r['numero']} | {r['issue']} | {r.get('folder', '')}" for r in s.get("review_rows", [])) or "  none"
        db_review_var.set(
            f"Total records: {s['total']}\n"
            f"Index   ·   inserted/downloaded: {s['index_downloaded']}   ·   JSON on disk: {s['index_json_on_disk']}/{s['total']}\n"
            f"Detail  ·   saved: {s['saved']}   ·   downloaded time set: {s['detail_downloaded']}   ·   complete files: {s['details_complete']}   ·   pending: {s['pending']}   ·   failed: {s['failed']}\n"
            f"Review needed   ·   total: {s['review_needed']}   ·   folder names incomplete: {s['folder_name_incomplete']}   ·   possible pending/incomplete: {s['possible_pending']}\n"
            f"Other   ·   Detail JSON in DB: {s['with_detail_json']}   ·   Status changes: {s['status_changes']}   ·   Calendar exports: {s['calendar_exports']}   ·   Notified (WAHA): {s['notified']}\n"
            f"Review area (first 12):\n{review}\n"
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
    # confirmed, so a stray click cannot erase the archive. All call pc_reset.py.
    # ========================================================================
    reset_zone = ttk.Frame(content, style="Card.TFrame", padding=14)
    reset_zone.grid(row=7, column=0, sticky="ew", padx=14, pady=8)
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
        command = [str(BASE_DIR / "pc_reset.py"), action]
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

    logs = ttk.Frame(content, style="Card.TFrame", padding=14)
    logs.grid(row=8, column=0, sticky="nsew", padx=14, pady=(8, 14))
    logs.columnconfigure(0, weight=1)
    logs.columnconfigure(1, weight=1)
    logs.rowconfigure(1, weight=1)
    ttk.Label(logs, text="Recent worker log").grid(row=0, column=0, sticky="w")
    ttk.Label(logs, text="Current action log").grid(row=0, column=1, sticky="w")

    # Each log is a fixed-height box WITH its own scrollbar, so the pane scrolls
    # the log itself (wheel or scrollbar) instead of moving the whole page.
    def make_log_pane(parent: tk.Widget, grid_col: int, pad: tuple[int, int]) -> tk.Text:
        frame = ttk.Frame(parent, style="TFrame")
        frame.grid(row=1, column=grid_col, sticky="nsew", padx=pad)
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        text = tk.Text(frame, height=18, bg="#020617", fg="#e5e7eb", insertbackground="#e5e7eb", wrap="word")
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
        update_run_controls(snap)

        for key, var in diag_vars.items():
            if key == "STEP":
                var.set(f"{progress.get('STEP_CURRENT', '-')} / {progress.get('STEP_TOTAL', '-')}")
            elif key == "ITEM":
                var.set(f"{progress.get('ITEM_CURRENT', '-')} / {progress.get('ITEM_TOTAL', '-')}")
            else:
                var.set(str(progress.get(key, "-")))

        if not waha_var.get():
            waha_var.set(str(snap.get("waha_chat_id", "")))

        set_text(worker_text, str(snap["worker_log"]))
        set_text(current_text, str(snap["current_log"]))

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
            done_var.set("Idle. Auto-close starts only after a live run finishes while the monitor is open.")
            overlay.place_forget()
        else:
            if done_since is None:
                done_since = time.monotonic()
            wait = runtime["auto_close"] if snap.get("auto_close_enabled") else 0
            remaining = max(0, wait - int(time.monotonic() - done_since))
            if wait:
                counting_down = True
                done_var.set(f"Live run finished. This window will close in {remaining} seconds.")
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
