#!/usr/bin/env python3
"""Low-power local web monitor for PanamaCompra run-all progress.

The page is loaded once and then polls a small JSON endpoint. That avoids full
browser reloads every few seconds while preserving the same dashboard UI.
"""
from __future__ import annotations

import json
from collections import Counter
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import common as pc_common

BASE_DIR = pc_common.APP_ROOT
PROGRESS_FILE = pc_common.PROGRESS_PATH
WORKER_LOG = pc_common.LOG_DIR / "run_all_worker.log"
CURRENT_LOG = pc_common.LOG_DIR / "run_all_current.log"
REQUEST_FLAG = pc_common.QUEUE_DIR / "run_all_requested.flag"
UPDATE_QUEUE_FLAG = pc_common.QUEUE_DIR / "update_monitor_requested.flag"
UPDATE_IN_PROGRESS_FLAG = pc_common.QUEUE_DIR / "update_monitor_in_progress.flag"
REQUEST_LOG = pc_common.LOG_DIR / "run_all_requests.log"
UPDATE_QUEUE_LOG = pc_common.LOG_DIR / "update_monitor_queue.log"
# Work-templates helper imported as a module so the web monitor lists/saves the
# same source folder and selection the CLI and native monitor use.
import importlib.util as _importlib_util

_rt_spec = _importlib_util.spec_from_file_location(
    "record_templates", str(pc_common.APP_ROOT / "src" / "tools" / "020-record-templates.py"))
record_templates = _importlib_util.module_from_spec(_rt_spec)
_rt_spec.loader.exec_module(record_templates)

_cal_spec = _importlib_util.spec_from_file_location(
    "opportunity_calendar", str(pc_common.APP_ROOT / "src" / "tools" / "030-opportunity-calendar.py"))
opportunity_calendar = _importlib_util.module_from_spec(_cal_spec)
_cal_spec.loader.exec_module(opportunity_calendar)

_nnr_spec = _importlib_util.spec_from_file_location(
    "notify_new_records", str(pc_common.APP_ROOT / "src" / "pipeline" / "020-notify-whatsapp.py"))
notify_formats = _importlib_util.module_from_spec(_nnr_spec)
_nnr_spec.loader.exec_module(notify_formats)

WAHA_CHAT_ID_PATH = pc_common.DATA_CONFIG_DIR / "waha_chat_id.txt"
# Optional per-purpose destinations; each falls back to the default chat id.
WAHA_CHAT_ID_INDEX_PATH = pc_common.DATA_CONFIG_DIR / "waha_chat_id_index.txt"
WAHA_CHAT_ID_DETAILS_PATH = pc_common.DATA_CONFIG_DIR / "waha_chat_id_details.txt"
WAHA_CHAT_ID_STATUS_PATH = pc_common.DATA_CONFIG_DIR / "waha_chat_id_status.txt"


def read_chat_file(path) -> str:
    return path.read_text(encoding="utf-8", errors="replace").strip() if path.exists() else ""


FILTER_FILES = {
    "global": pc_common.DATA_CONFIG_DIR / "waha_keywords.txt",
    "index": pc_common.DATA_CONFIG_DIR / "waha_keywords_index.txt",
    "details": pc_common.DATA_CONFIG_DIR / "waha_keywords_details.txt",
    "status": pc_common.DATA_CONFIG_DIR / "waha_keywords_status.txt",
}


def read_filter_rules(name: str) -> str:
    path = FILTER_FILES[name]
    if not path.exists():
        return ""
    rules = [k.strip() for k in path.read_text(encoding="utf-8", errors="replace").splitlines() if k.strip() and not k.startswith("#")]
    return ", ".join(rules)
MONITOR_SETTINGS_PATH = pc_common.DATA_CONFIG_DIR / "monitor_settings.env"
MANUAL_ACTION_LOG = pc_common.LOG_DIR / "manual_actions.log"
ARCHIVE_DB = pc_common.DB_PATH
HOST = os.environ.get("PC_MONITOR_HOST", "127.0.0.1")
PORT = int(os.environ.get("PC_MONITOR_PORT", "8766"))
REFRESH_SECONDS = max(3, int(os.environ.get("PC_MONITOR_WEB_REFRESH_SECONDS", "3")))
IDLE_REFRESH_SECONDS = max(REFRESH_SECONDS, int(os.environ.get("PC_MONITOR_WEB_IDLE_REFRESH_SECONDS", "30")))
AUTO_CLOSE_SECONDS = max(0, int(os.environ.get("PC_MONITOR_WEB_AUTO_CLOSE_SECONDS", "20")))
PATH_SETTING_DEFAULTS = {
    "PC_RECORDS_DIR": str(pc_common.RECORDS_DIR),
    "PC_CALENDAR_DIR": str(pc_common.CALENDAR_DIR),
    "PC_RECORDS_TEST_DIR": str(pc_common.RECORDS_TEST_DIR),
}
# Plain string/number settings the operator can edit. PC_NOTIFY_WITHIN_DAYS is
# intentionally blank by default (blank = announce every deadline).
VALUE_SETTING_DEFAULTS = {
    "PC_WAHA_SOURCE": "Panamá Compra",
    "PC_NEXT_RUN_INTERVAL_MINUTES": "30",
    "PC_MONITOR_DEADLINE_SOON_DAYS": "7",
    "PC_WEBHOOK_INDEX_LIMIT": "0",
    "PC_WEBHOOK_DETAIL_LIMIT": "0",
    "PC_NOTIFY_WITHIN_DAYS": "",
    "PC_WAHA_RETRIES": "2",
    "PC_WAHA_BASE_URL": "http://127.0.0.1:3000",
    "PC_WAHA_SESSION": "default",
    "PC_WAHA_NOTIFY_EVENTS": "info,start,done,failed,timeout,resume,update,new,none",
    "PC_TEST_ZONE_LIMIT": "5",
    "PC_NEXT_RUN_TIMER_WIDTH": "380",
    "PC_NEXT_RUN_TIMER_HEIGHT": "360",
    "PC_NEXT_RUN_TIMER_TOP": "30",
    "PC_NEXT_RUN_TIMER_RECORDS": "20",
    "PC_NEXT_RUN_TIMER_DATA_REFRESH_SECONDS": "10",
    "PC_MONITOR_STALE_SECONDS": "120",
    # Container-side settings applied by src/tools/010-docker-stack.sh on the next
    # stack restart (non-empty values win over .env).
    "CHANGEDETECTION_BASE_URL": "http://localhost:5000",
    "PC_TEMPLATES_SRC_DIR": "",
    "WAHA_PORT": "3000",
    "WAHA_API_KEY": "",
    # WAHA dashboard login (container side). The stack seeds admin / 12345678 on
    # install/reinstall so review access always works; edit here to change it.
    "WAHA_DASHBOARD_USERNAME": "admin",
    "WAHA_DASHBOARD_PASSWORD": "12345678",
}
BOOLEAN_SETTING_DEFAULTS = {
    "PC_NOTIFY_WHATSAPP": "1",
    "PC_NOTIFY_DETAILS": "1",
    "PC_CALENDAR_AUTO_IMPORT": "0",
    "PC_WAHA_ENABLED": "0",
    "PC_NOTIFY_SKIP_EXPIRED": "0",
    "PC_TEST_ZONE_AUTORUN": "0",
    "PC_RUN_UPDATE_BEFORE_RUN": "1",
}
ALLOWED_MONITOR_SETTINGS = set(PATH_SETTING_DEFAULTS) | set(VALUE_SETTING_DEFAULTS) | set(BOOLEAN_SETTING_DEFAULTS)


class ManualAction(tuple):
    __slots__ = ()
    zone = property(lambda self: self[0])
    label = property(lambda self: self[1])
    command = property(lambda self: self[2])
    comment = property(lambda self: self[3])
    open_after = property(lambda self: self[4])

    def __new__(cls, zone: str, label: str, command: tuple[str, ...], comment: str, open_after: Path | None = None):
        return tuple.__new__(cls, (zone, label, command, comment, open_after))


RECORDS_TEST_PARENT = pc_common.RECORDS_TEST_DIR
MANUAL_ACTIONS = [
    ManualAction("Runners", "Run full collector", ("./src/pipeline/110a-request-run.sh", "99", "RESTART", "0"), "Queues a manual restart run for all available index pages and opens/reuses this monitor."),
    ManualAction("Runners", "Run collector now", ("./src/pipeline/110b-run-now.sh", "99", "0", "MANUAL"), "Starts the run-all worker immediately for all available index pages and up to 99 detail pages."),
    ManualAction("Runners", "Stop active run", ("./src/pipeline/120b-stop-collectors.sh",), "Stops the active collection (worker/index/detail/test/calendar) and prevents auto-resume. The monitor, next-run timer and webhook stay running."),
    ManualAction("Runners", "Show run status", ("./src/pipeline/130b-run-status.sh",), "Writes a process/log status snapshot to the manual action log."),
    ManualAction("Tests", "Test zone", ("./src/pipeline/070-test-zone.py", "--limit", "5", "--apply"), "Re-runs the latest five records in records_test, then opens that sandbox folder.", RECORDS_TEST_PARENT),
    ManualAction("Tests", "Review system", ("./review-system.sh",), "Runs the repository health review and troubleshooting summary."),
    ManualAction("Updater / Migration", "Update local copy", ("./src/monitor/003-update-loader.py", "--open-monitor-after"), "Opens the centered updater loader, refreshes this checkout/dependencies, then reopens the monitor."),
    ManualAction("Updater / Migration", "Pre-run update only", ("./src/pipeline/000-update-before-run.sh",), "Runs the lightweight git/dependency refresh normally used before worker iterations."),
    ManualAction("Updater / Migration", "Rename folders", ("./src/tools/070-rename-record-folders.py", "--apply"), "Normalizes existing record folder names."),
    ManualAction("Updater / Migration", "Migrate records", ("./src/tools/090a-migrate-previous-records.sh",), "Imports/migrates previous record archives."),
    ManualAction("Integrations", "Start/refresh docker stack", ("./src/tools/010-docker-stack.sh", "up"), "Pulls/starts (or refreshes) the changedetection + WAHA + webhook containers; data stays in var/integrations."),
    ManualAction("Integrations", "Docker stack status", ("./src/tools/010-docker-stack.sh", "status"), "Writes container states plus the changedetection/WAHA URLs to the manual action log."),
    ManualAction("Integrations", "Restart docker stack", ("./src/tools/010-docker-stack.sh", "restart"), "Stops and starts the containers, applying the container settings saved below (changedetection URL, WAHA port/API key)."),
    ManualAction("Integrations", "Stop docker stack", ("./src/tools/010-docker-stack.sh", "down"), "Stops and removes the changedetection/WAHA/webhook containers; their data stays in var/integrations."),
    ManualAction("Settings", "Apply work templates", ("./src/tools/020-record-templates.py", "apply", "--apply"), "Copies the selected template files into templates/ inside every saved record folder (existing files kept)."),
    ManualAction("Settings", "Build detail views", ("./src/pipeline/040-build-detail-views.py", "--apply"), "Rebuilds saved record views, ICS files, and split tables."),
    ManualAction("Settings", "Repair missing deadlines", ("./src/pipeline/050-repair-missing-deadlines.py", "--apply"), "Finds folders/rows missing DTEND, re-downloads details, and renames folders after a deadline is recovered."),
    ManualAction("Settings", "Build calendars", ("./src/pipeline/060-build-calendar.py", "--all"), "Rebuilds calendar import packages."),
    ManualAction("Settings", "Import generated calendars", ("bash", "-lc", "PC_CALENDAR_AUTO_IMPORT=1 ./src/pipeline/060-build-calendar.py --all"), "Rebuilds and opens generated ICS files."),
    ManualAction("Settings", "Webhook listener", ("./src/webhook/020-start-listener.sh", "--replace-port-owner"), "Starts/restarts the local webhook listener."),
    ManualAction("Settings", "Install webhook service", ("./src/webhook/030-install-service.sh",), "Installs/repairs the persistent user systemd webhook service."),
    ManualAction("Settings", "Open web monitor", ("./src/tools/130-open-web-app.sh", "monitor"), "Starts/opens the browser monitor (chromeless app window when available)."),
    ManualAction("Integrations", "Open changedetection app window", ("./src/tools/130-open-web-app.sh", "changedetection"), "Opens the changedetection.io dashboard in a chromeless app window on the desktop — independent of Firefox, no browser header."),
    ManualAction("Integrations", "Open WAHA app window", ("./src/tools/130-open-web-app.sh", "waha"), "Opens the WAHA dashboard in a chromeless app window on the desktop (login defaults to admin / 12345678)."),
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



def parse_settings_file() -> dict[str, str]:
    settings: dict[str, str] = {}
    if not MONITOR_SETTINGS_PATH.exists():
        return settings
    for line in MONITOR_SETTINGS_PATH.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        try:
            parsed = shlex.split(raw_value, posix=True)
            settings[key] = parsed[0] if parsed else ""
        except ValueError:
            settings[key] = raw_value.strip().strip("'").strip('"')
    return settings


def load_monitor_settings() -> dict[str, str]:
    settings = {**BOOLEAN_SETTING_DEFAULTS, **PATH_SETTING_DEFAULTS, **VALUE_SETTING_DEFAULTS}
    settings.update({key: os.environ.get(key, default) for key, default in settings.items() if key in os.environ})
    file_settings = parse_settings_file()
    for key in settings:
        if key in file_settings:
            settings[key] = file_settings[key]
    return settings


def monitor_env() -> dict[str, str]:
    env = os.environ.copy()
    env.update(parse_settings_file())
    return env


def save_monitor_setting(key: str, value: str) -> None:
    if key not in ALLOWED_MONITOR_SETTINGS:
        raise ValueError(f"unsupported setting: {key}")
    settings = parse_settings_file()
    for default_key, default_value in {**BOOLEAN_SETTING_DEFAULTS, **PATH_SETTING_DEFAULTS, **VALUE_SETTING_DEFAULTS}.items():
        settings.setdefault(default_key, os.environ.get(default_key, default_value))
    if key in BOOLEAN_SETTING_DEFAULTS:
        settings[key] = "1" if value not in {"0", "false", "False", "off", "OFF", ""} else "0"
    elif key in PATH_SETTING_DEFAULTS:
        settings[key] = value.strip() or PATH_SETTING_DEFAULTS[key]
    else:
        settings[key] = value.strip() or VALUE_SETTING_DEFAULTS[key]
    MONITOR_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    MONITOR_SETTINGS_PATH.write_text(
        "# PanamaCompra monitor settings (KEY=VALUE).\n"
        + "# Edited from the web/native monitor; same-named environment variables override these at startup.\n"
        + "\n".join(f"{name}={shlex.quote(settings[name])}" for name in sorted(settings))
        + "\n",
        encoding="utf-8",
    )

def tail(path: Path, lines: int) -> str:
    if not path.exists():
        return f"No {path.name} yet."
    content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(content[-lines:])


def finish_stamp_from_folder(record_folder: str) -> str:
    """Fallback DTEND from the record folder leaf '(YYYY-MM-DD_HH_MM)-(numero)-(desc)'
    when the finish_date_guess column is empty (older records). Returns
    'YYYY-MM-DD HH:MM' (or 'YYYY-MM-DD'), or '' when the folder carries no stamp."""
    name = os.path.basename((record_folder or "").rstrip("/"))
    # Only the first parenthesized token, so the NUMERO cannot be mistaken for it.
    if name.startswith("(") and ")" in name:
        name = name[1:name.index(")")]
    match = re.search(r"(\d{4}-\d{2}-\d{2})(?:[ _T]?(\d{2})[_:](\d{2}))?", name)
    if not match:
        return ""
    if match.group(2) and match.group(3):
        return f"{match.group(1)} {match.group(2)}:{match.group(3)}"
    return match.group(1)


def finish_stamp_from_detail_json(detail_json_path: str) -> str:
    """Fallback DTEND from saved detail JSON calendar/summary fields."""
    if not detail_json_path:
        return ""
    try:
        data = json.loads(Path(detail_json_path).read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return ""
    calendar = data.get("calendar") if isinstance(data.get("calendar"), dict) else {}
    summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
    return str(
        calendar.get("dtend")
        or data.get("finish_date_guess")
        or data.get("date_end_opportunity")
        or summary.get("date_end_opportunity")
        or ""
    )


def load_record_index(limit: int = 500) -> list[dict[str, str]]:
    """Read collected records (NUMERO + description + folder/link) from the
    archive DB for the record-index selector. Newest first; never raises."""
    if not ARCHIVE_DB.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{ARCHIVE_DB}?mode=ro", uri=True, timeout=2)
    except sqlite3.Error:
        return []
    try:
        conn.row_factory = sqlite3.Row
        column_names = {row[1] for row in conn.execute("PRAGMA table_info(opportunities)").fetchall()}
        start_expr = "COALESCE(start_date_guess, '')" if "start_date_guess" in column_names else "''"
        rows = conn.execute(
            "SELECT numero, "
            "COALESCE(NULLIF(short_description, ''), descripcion, '') AS descripcion, "
            "COALESCE(record_folder, '') AS record_folder, "
            "COALESCE(link, '') AS link, "
            "COALESCE(detail_status, '') AS detail_status, "
            "COALESCE(detail_saved_at, '') AS detail_saved_at, "
            "COALESCE(detail_json_path, '') AS detail_json_path, "
            "COALESCE(first_seen, '') AS first_seen, "
            "COALESCE(finish_date_guess, '') AS finish_date_guess, "
            f"{start_expr} AS start_date_guess "
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
            "first_seen": str(row["first_seen"] or ""),
            "detail_json_path": str(row["detail_json_path"] or ""),
            "finish_date_guess": (
                str(row["finish_date_guess"] or "")
                or finish_stamp_from_folder(str(row["record_folder"] or ""))
                or finish_stamp_from_detail_json(str(row["detail_json_path"] or ""))
            ),
            "start_date_guess": str(row["start_date_guess"] or ""),
        }
        for row in rows
    ]


def load_detail_payload_for_kpi(detail_json_path: str | None) -> tuple[list[dict], dict]:
    """Best-effort loader of a saved detail JSON for the KPI dashboards.

    Returns (items, summary); handles split item files and never raises."""
    if not detail_json_path:
        return [], {}
    path = Path(str(detail_json_path))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return [], {}
    summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
    items = data.get("items")
    if not isinstance(items, list):
        items = []
        split = data.get("_split") if isinstance(data.get("_split"), dict) else {}
        descriptor = split.get("items") if isinstance(split.get("items"), dict) else {}
        rel = descriptor.get("file")
        if rel:
            try:
                split_items = json.loads((path.parent / str(rel)).read_text(encoding="utf-8"))
                if isinstance(split_items, list):
                    items = split_items
            except Exception:
                items = []
    return [item for item in items if isinstance(item, dict)], summary


def load_detail_items_for_kpi(detail_json_path: str | None) -> list[dict]:
    """Best-effort item loader for KPI dashboards; never raises."""
    return load_detail_payload_for_kpi(detail_json_path)[0]


# Detail-summary labels that describe WHERE the opportunity is bought/delivered.
# Used to build the KPI location diagram from the saved detail pages.
LOCATION_SUMMARY_KEYS = ("Lugar", "lugar", "Provincia", "provincia", "Unidad de compra", "Dependencia")


def summarize_items_for_kpi(conn: sqlite3.Connection, limit: int = 300) -> dict[str, object]:
    """Summarize detail items for decision KPIs without scanning unbounded data."""
    rows = conn.execute(
        "SELECT numero, descripcion, detail_json_path FROM opportunities "
        "WHERE COALESCE(detail_json_path, '') <> '' "
        "ORDER BY COALESCE(detail_saved_at, first_seen, '') DESC, numero DESC LIMIT ?",
        (limit,),
    ).fetchall()
    total_items = 0
    with_items = 0
    max_items = {"numero": "", "descripcion": "", "count": 0}
    keyword_counts: Counter[str] = Counter()
    location_counts: Counter[str] = Counter()
    sample_items: list[dict[str, str]] = []
    for row in rows:
        items, summary = load_detail_payload_for_kpi(str(row["detail_json_path"] or ""))
        for key in LOCATION_SUMMARY_KEYS:
            place = str(summary.get(key) or "").strip()
            if place:
                location_counts[place[:60]] += 1
                break
        if items:
            with_items += 1
        total_items += len(items)
        if len(items) > int(max_items["count"]):
            max_items = {"numero": str(row["numero"] or ""), "descripcion": str(row["descripcion"] or ""), "count": len(items)}
        for item in items:
            text = " ".join(str(item.get(k, "")) for k in ("descripcion", "description", "nombre", "name", "codigo", "code"))
            for word in re.findall(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ0-9]{4,}", text.lower()):
                if not word.isdigit():
                    keyword_counts[word] += 1
            if len(sample_items) < 8:
                sample_items.append({
                    "numero": str(row["numero"] or ""),
                    "descripcion": str(item.get("descripcion") or item.get("description") or item.get("nombre") or item.get("name") or "")[:90],
                    "cantidad": str(item.get("cantidad") or item.get("qty") or item.get("quantity") or ""),
                })
    avg_items = round(total_items / with_items, 1) if with_items else 0
    return {
        "sampled_records": len(rows),
        "records_with_items": with_items,
        "total_items": total_items,
        "avg_items_per_record": avg_items,
        "max_items_record": max_items,
        "top_item_keywords": [{"label": k, "count": v} for k, v in keyword_counts.most_common(12)],
        "top_locations": [{"label": k, "count": v} for k, v in location_counts.most_common(10)],
        "sample_items": sample_items,
    }

def db_review_stats() -> dict[str, object]:
    """Aggregate counts for the web Database-review card (mirror of the Tk
    monitor's panel): totals, detail-queue state, notification state, and a
    per-group breakdown. Never raises; a missing/locked DB yields zeros."""
    empty = {
        "db_exists": ARCHIVE_DB.exists(), "total": 0, "saved": 0, "pending": 0,
        "failed": 0, "notified": 0, "notify_backlog": 0, "detail_notify_backlog": 0, "needs_deadline": 0,
        "with_detail_json": 0, "item_analysis": {}, "groups": [], "entities": [], "dependencias": [],
        "recent": [], "completed_recent": [], "columns": [], "status_breakdown": [], "monthly_trend": [],
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

        column_details = []
        for col in conn.execute("PRAGMA table_info(opportunities)").fetchall():
            name = col[1]
            nonempty = int(conn.execute(
                f"SELECT COUNT(*) FROM opportunities WHERE COALESCE(CAST({name} AS TEXT), '') <> ''"
            ).fetchone()[0])
            column_details.append({"name": name, "type": col[2], "nonempty": nonempty})
        status_rows = [
            {"status": str(r["detail_status"] or "(blank)"), "count": int(r["c"])}
            for r in conn.execute(
                "SELECT detail_status, COUNT(*) AS c FROM opportunities GROUP BY detail_status ORDER BY c DESC"
            ).fetchall()
        ]
        # Records the "Repair missing deadlines" action would act on: blank
        # finish_date_guess or a (NO-DATE) folder. Mirrors the predicate in
        # 050-repair-missing-deadlines.py so the count matches what that tool processes.
        deadline_predicates = ["COALESCE(finish_date_guess, '') = ''",
                               "UPPER(COALESCE(record_folder, '')) LIKE '%/(NO-DATE)%'"]
        if "record_folder_leaf" in columns:
            deadline_predicates.insert(1, "UPPER(COALESCE(record_folder_leaf, '')) LIKE '(NO-DATE)%'")
        needs_deadline_where = ("COALESCE(link, '') <> '' AND COALESCE(record_folder, '') <> '' AND ("
                                + " OR ".join(deadline_predicates) + ")")

        return {
            "db_exists": True,
            "total": count(),
            "saved": count("detail_status = 'saved'"),
            "pending": count("detail_status = 'pending'"),
            "failed": count("detail_status = 'failed'"),
            "notified": count("notified_at IS NOT NULL") if has_notified else 0,
            "notify_backlog": count("notified_at IS NULL") if has_notified else 0,
            "detail_notify_backlog": count(
                "detail_status = 'saved' AND notified_at IS NOT NULL AND detail_notified_at IS NULL"
            ) if has_notified and "detail_notified_at" in columns else 0,
            "needs_deadline": count(needs_deadline_where),
            "with_detail_json": count("COALESCE(detail_json_path, '') <> ''"),
            "item_analysis": summarize_items_for_kpi(conn),
            "recent": [
                {"numero": str(r["numero"] or ""), "descripcion": str(r["descripcion"] or r["short_description"] or ""), "detail_status": str(r["detail_status"] or "")}
                for r in conn.execute(
                    "SELECT numero, descripcion, short_description, detail_status FROM opportunities "
                    "ORDER BY COALESCE(detail_saved_at, first_seen, '') DESC, numero DESC LIMIT 12"
                ).fetchall()
            ],
            "completed_recent": [
                {"numero": str(r["numero"] or ""), "finish_date_guess": str(r["finish_date_guess"] or "") or finish_stamp_from_folder(str(r["record_folder"] or "")) or finish_stamp_from_detail_json(str(r["detail_json_path"] or "")), "descripcion": str(r["descripcion"] or r["short_description"] or "")}
                for r in conn.execute(
                    "SELECT numero, finish_date_guess, record_folder, detail_json_path, descripcion, short_description FROM opportunities "
                    "WHERE detail_status = 'saved' ORDER BY COALESCE(detail_saved_at, first_seen, '') DESC, numero DESC LIMIT 5"
                ).fetchall()
            ],
            "columns": column_details,
            "status_breakdown": status_rows,
            "monthly_trend": [
                {"label": str(r["period"] or "unknown"), "count": int(r["c"])}
                for r in conn.execute(
                    "SELECT substr(COALESCE(NULLIF(first_seen, ''), detail_saved_at, finish_date_guess, 'unknown'), 1, 7) AS period, COUNT(*) AS c "
                    "FROM opportunities GROUP BY period ORDER BY period DESC LIMIT 12"
                ).fetchall()
            ],
            "groups": [
                {"grupo": str(r["grupo"] or "(sin grupo)"), "count": int(r["c"])}
                for r in conn.execute(
                    "SELECT grupo, COUNT(*) AS c FROM opportunities "
                    "GROUP BY grupo ORDER BY c DESC"
                ).fetchall()
            ],
            # WHO buys and WHERE: contracting entities and their dependencies,
            # for the KPI location/buyer diagrams.
            "entities": [
                {"label": str(r["entidad"] or "(sin entidad)"), "count": int(r["c"])}
                for r in conn.execute(
                    "SELECT entidad, COUNT(*) AS c FROM opportunities "
                    "GROUP BY entidad ORDER BY c DESC LIMIT 12"
                ).fetchall()
            ] if "entidad" in columns else [],
            "dependencias": [
                {"label": str(r["dependencia"] or "(sin dependencia)"), "count": int(r["c"])}
                for r in conn.execute(
                    "SELECT dependencia, COUNT(*) AS c FROM opportunities "
                    "GROUP BY dependencia ORDER BY c DESC LIMIT 12"
                ).fetchall()
            ] if "dependencia" in columns else [],
        }
    except sqlite3.Error:
        return empty
    finally:
        conn.close()


# Reset actions exposed as separate buttons (per operator request). Maps the
# button action to (src/tools/110-reset.py subcommand, is_destructive). The destructive
# wipes are confirmed in the browser and run with --yes.
RESET_ACTIONS = {
    "requeue-details": False,
    "reset-notify": False,
    "wipe-db": True,
    "wipe-all": True,
}


def running(pattern: str) -> bool:
    return subprocess.run(["pgrep", "-f", pattern], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def webhook_running() -> bool:
    if running("[s]rc/webhook/010-webhook-listener.py") or running("[p]ython3? -u .*src/webhook/010-webhook-listener.py"):
        return True
    try:
        result = subprocess.run(["docker", "compose", "ps", "--status", "running", "webhook"], cwd=BASE_DIR, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=3)
        return "webhook" in result.stdout.lower()
    except Exception:
        return False

def process_snapshot() -> dict[str, bool]:
    worker = running("[r]un-worker.sh")
    test = running("[p]ython(3)? -u .*070-test-zone.py")
    webhook = webhook_running()
    return {
        "normal_run": worker and not test,
        "test_run": test,
        "worker": worker,
        "index": running("[p]ython(3)? -u .*010-collect-index.py"),
        "detail": running("[p]ython(3)? -u .*030-collect-details.py"),
        "calendar": running("[p]ython(3)? -u .*(040-build-detail-views|060-build-calendar).py"),
        "messaging": running("[0]20-notify-whatsapp.py"),
        "webhook": webhook,
        "request": REQUEST_FLAG.exists(),
    }


WORK_PROCESS_KEYS = ("worker", "index", "detail", "calendar", "messaging", "test_run", "request")


def percent_value(progress: dict[str, str]) -> int:
    try:
        return max(0, min(100, int(progress.get("PERCENT", "0"))))
    except ValueError:
        return 0


def progress_stale(processes: dict[str, bool], progress: dict[str, str]) -> bool:
    if any(processes.get(key) for key in WORK_PROCESS_KEYS):
        return False
    if progress.get("STATUS") != "RUNNING":
        return False
    try:
        updated = time.mktime(time.strptime(progress.get("UPDATED_AT", ""), "%Y-%m-%d %H:%M:%S"))
    except (TypeError, ValueError):
        return True
    return (time.time() - updated) > int(load_monitor_settings().get("PC_MONITOR_STALE_SECONDS", "120"))


def is_done(processes: dict[str, bool], progress: dict[str, str]) -> bool:
    if any(processes.get(key) for key in WORK_PROCESS_KEYS):
        return False
    return progress_stale(processes, progress) or progress.get("STATUS") in {"DONE", "FAILED", "TIMEOUT", "STALE"} or progress.get("PHASE") in {"DONE", "IDLE"}



def file_timestamp(path: Path) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(path.stat().st_mtime))
    except OSError:
        return "-"


def queue_payload() -> dict[str, str]:
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
            open_folder(action.open_after)
        threading.Thread(target=wait_then_open, daemon=True).start()

def status_payload() -> dict[str, object]:
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
        "queue": queue_payload(),
        "done": done,
        # Auto-close only the unattended automatic (changedetection/webhook) run.
        # RESTART/MANUAL/TEST are operator-initiated, so the page stays open.
        "auto_close_enabled": done and progress.get("MODE", "IDLE").upper() == "AUTO" and not processes.get("test_run", False),
        "refresh_seconds": IDLE_REFRESH_SECONDS if done else REFRESH_SECONDS,
        "auto_close_seconds": AUTO_CLOSE_SECONDS,
        "worker_log": tail(WORKER_LOG, 20),
        "current_log": tail(CURRENT_LOG, 35),
        "server_time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "waha_chat_id": read_chat_file(WAHA_CHAT_ID_PATH),
        "waha_chat_id_index": read_chat_file(WAHA_CHAT_ID_INDEX_PATH),
        "waha_chat_id_details": read_chat_file(WAHA_CHAT_ID_DETAILS_PATH),
        "waha_chat_id_status": read_chat_file(WAHA_CHAT_ID_STATUS_PATH),
        "settings": load_monitor_settings(),
    }

ACTIONS_JSON = json.dumps([
    {"zone": action.zone, "label": action.label, "comment": action.comment}
    for action in MANUAL_ACTIONS
], ensure_ascii=False)

# Honour the operator's "deadline soon" window in the web record list, matching
# the native monitor. Resolved once at startup, so a changed setting applies on
# the next monitor restart (same as PC_MONITOR_DEADLINE_SOON_DAYS elsewhere).
try:
    RECORD_SOON_DAYS = max(1, int(load_monitor_settings().get("PC_MONITOR_DEADLINE_SOON_DAYS", "7")))
except (TypeError, ValueError):
    RECORD_SOON_DAYS = 7

_startup_settings = parse_settings_file()
CHANGEDETECTION_URL = (os.environ.get("CHANGEDETECTION_BASE_URL") or _startup_settings.get("CHANGEDETECTION_BASE_URL") or "http://localhost:5000").rstrip("/")
WAHA_DASHBOARD_URL = "http://localhost:" + (os.environ.get("WAHA_PORT") or _startup_settings.get("WAHA_PORT") or "3000")

HTML = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>PanamaCompra Monitor</title>
<style>
body {{ font-family: system-ui, -apple-system, Segoe UI, sans-serif; margin: 24px; background: #0f172a; color: #e5e7eb; }}
a {{ color: #93c5fd; }}
.card {{ background: #111827; border: 1px solid #334155; border-radius: 12px; padding: 18px; margin: 0 0 16px; box-shadow: 0 8px 24px #0004; }}
h1 {{ margin-top: 0; }}
.bar {{ height: 30px; background: #334155; border-radius: 999px; overflow: hidden; border: 1px solid #64748b; }}
.fill {{ height: 100%; width: 0%; background: linear-gradient(90deg, #22c55e, #38bdf8); display: flex; align-items: center; justify-content: center; color: #020617; font-weight: 700; transition: width .4s ease; }}
table {{ border-collapse: collapse; width: 100%; }}
th, td {{ text-align: left; border-bottom: 1px solid #334155; padding: 7px 10px; vertical-align: top; }}
th {{ width: 220px; color: #93c5fd; }}
pre {{ white-space: pre-wrap; background: #020617; border: 1px solid #334155; border-radius: 8px; padding: 12px; max-height: 360px; overflow: auto; }}
/* Process status is a tidy flex grid of small chips instead of one crowded
   wrapped line: green = RUNNING, gray = off, even gaps. */
.proc-wrap {{ display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }}
.pill {{ display: inline-block; padding: 4px 10px; border-radius: 999px; font-weight: 700; font-size: .8rem; }}
.on {{ background: #14532d; color: #bbf7d0; }} .off {{ background: #1f2937; color: #9ca3af; }}
.message {{ font-size: 1.15rem; color: #fef3c7; }}
.small {{ color: #94a3b8; }}
.done {{ color: #bbf7d0; font-weight: 700; }}
button {{ background: #334155; color: #e5e7eb; border: 0; border-radius: 8px; padding: 9px 14px; font-weight: 700; cursor: pointer; margin: 0 8px 8px 0; transition: background .15s ease, transform .05s ease; }}
button:hover {{ background: #475569; }}
button:active {{ transform: translateY(1px); }}
button:disabled {{ background: #1f2937; color: #6b7280; cursor: not-allowed; transform: none; }}
button.primary {{ background: #2563eb; color: #fff; }}
button.primary:hover {{ background: #1d4ed8; }}
button.primary:disabled {{ background: #1e293b; color: #6b7280; }}
.zone {{ margin-top: 14px; padding-top: 8px; border-top: 1px solid #334155; }}
.zone h3 {{ margin: 0 0 8px; color: #fef3c7; }}
.danger {{ background: #dc2626; color: #fff; }} .danger:hover {{ background: #b91c1c; }}
textarea {{ width: 100%; min-height: 80px; border-radius: 8px; border: 1px solid #475569; background: #020617; color: #e5e7eb; padding: 10px; }}
select, input {{ border-radius: 8px; border: 1px solid #475569; background: #020617; color: #e5e7eb; padding: 6px 8px; font-size: 1rem; }}
input:disabled {{ opacity: .5; cursor: not-allowed; }}
/* Run mode as radio toggles. */
.mode-group {{ display: inline-flex; gap: 4px; vertical-align: middle; }}
.mode-group label {{ display: inline-flex; align-items: center; gap: 6px; padding: 6px 12px; border: 1px solid #475569; border-radius: 8px; background: #020617; cursor: pointer; font-weight: 700; color: #cbd5e1; }}
.mode-group input {{ accent-color: #2563eb; margin: 0; }}
.mode-group label:has(input:checked) {{ border-color: #2563eb; color: #93c5fd; background: #0b1220; }}
.mode-group input:disabled + span, .mode-group label:has(input:disabled) {{ opacity: .5; cursor: not-allowed; }}
select#record-index {{ min-width: 80%; max-width: 100%; min-height: 14rem; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .9rem; line-height: 1.35; }}
.settings-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 10px 14px; align-items: end; margin: 10px 0; }}
.settings-grid label {{ display: flex; flex-direction: column; gap: 4px; margin: 0; }}
.settings-grid input {{ width: 100%; box-sizing: border-box; }}
.section-toggle {{ float: right; margin-left: 12px; padding: 5px 10px; font-size: .8rem; }}
.card.collapsed > *:not(h1):not(h2) {{ display: none; }}
#diagnostics td {{ font-variant-numeric: tabular-nums; word-break: break-word; user-select: text; }}
.tab-nav {{ display: flex; flex-wrap: wrap; gap: 8px; margin: 12px 0 18px; position: sticky; top: 0; z-index: 5; background: #0f172acc; backdrop-filter: blur(8px); padding: 8px 0; }}
.tab-nav button.active {{ background: #2563eb; color: #fff; }}
.card[data-tab] {{ display: none; }}
.card[data-tab].tab-active {{ display: block; }}
.subsection {{ border: 1px solid #334155; border-radius: 10px; padding: 12px; margin: 10px 0; background: #0b1220; }}
.subsection h3 {{ margin: 0 0 8px; color: #bfdbfe; }}
.setting-actions {{ display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }}
.kpi-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; }}
.kpi {{ background: linear-gradient(135deg, #172554, #0f172a); border: 1px solid #38bdf8; border-radius: 12px; padding: 14px; box-shadow: 0 0 18px #0ea5e933; }}
.kpi b {{ display: block; font-size: 1.7rem; color: #67e8f9; }}
.diagram-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 14px; }}
.chart {{ background: #020617; border: 1px solid #334155; border-radius: 10px; padding: 12px; min-height: 180px; }}
.bar-row {{ display: grid; grid-template-columns: minmax(90px, 1fr) 4fr 48px; gap: 8px; align-items: center; margin: 7px 0; font-size: .9rem; }}
.bar-track {{ height: 12px; background: #1e293b; border-radius: 999px; overflow: hidden; }}
.bar-fill {{ height: 100%; background: linear-gradient(90deg, #38bdf8, #22c55e); border-radius: 999px; }}
.keyword-cloud span {{ display: inline-block; margin: 4px; padding: 5px 8px; border-radius: 999px; background: #1e293b; color: #bfdbfe; }}
/* Slim dark scrollbars for the log panes. */
pre::-webkit-scrollbar {{ width: 10px; height: 10px; }}
pre::-webkit-scrollbar-track {{ background: #0f172a; border-radius: 8px; }}
pre::-webkit-scrollbar-thumb {{ background: #334155; border-radius: 8px; }}
pre::-webkit-scrollbar-thumb:hover {{ background: #475569; }}
.record-grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }}
.record-card {{ border-radius: 10px; padding: 14px; font-weight: 700; white-space: pre-line; }}
.record-pending {{ background: #3f1d1d; color: #fecaca; }}
.record-completed {{ background: #14532d; color: #bbf7d0; }}
</style>
</head>
<body>
<div class="card">
  <h1>PanamaCompra Progress Monitor</h1>
  <p class="small"><span id="server-time">Loading...</span> · Low-power polling every <span id="refresh-label">{REFRESH_SECONDS}</span>s while running · JSON: <a href="/api/status">/api/status</a></p>
  <div class="bar"><div class="fill" id="fill">0%</div></div>
  <p class="message" id="message">Loading...</p>
  <p id="done-note" class="done" hidden></p>
  <div id="processes" class="proc-wrap"></div><p class="small">Process pills show live OS processes: detail is off except during STEP 3; webhook should stay RUNNING when the host listener is active.</p>
</div>
<div class="tab-nav"><button class="active" data-tab-button="operations" onclick="showTab('operations')">Operations</button><button data-tab-button="settings" onclick="showTab('settings')">Settings</button><button data-tab-button="whatsapp" onclick="showTab('whatsapp')">WhatsApp</button><button data-tab-button="decision" onclick="showTab('decision')">KPIs</button><button data-tab-button="records" onclick="showTab('records')">Records & Database</button></div>
<div class="card" data-tab="operations"><h2>Queue process</h2><p id="queue-summary" class="small">Loading queue…</p><pre id="queue-log"></pre></div>
<div class="card" data-tab="operations"><h2>Monitor buttons</h2><div class="subsection"><h3>Run controls</h3><p><span class="small" style="margin-right:8px">Mode</span><span class="mode-group" id="run-mode"><label><input type="radio" name="run-mode" value="auto" disabled><span>automatic</span></label><label><input type="radio" name="run-mode" value="restart" checked><span>run pending only</span></label><label><input type="radio" name="run-mode" value="manual"><span>manual run</span></label><label><input type="radio" name="run-mode" value="test"><span>test run</span></label></span> <label class="small">Index page cap <input id="index-limit" value="0" size="4"></label> <label class="small">Detail limit <input id="detail-limit" value="99" size="4"></label> <button id="run-button" class="primary" onclick="requestRun()">Request selected run</button><button class="danger" onclick="stopRun()">Stop active run</button><span id="button-status" class="small"></span></p><p class="small">Integrations: <a href="{CHANGEDETECTION_URL}" target="_blank">Open changedetection UI</a> · <a href="{WAHA_DASHBOARD_URL}" target="_blank">Open WAHA dashboard (pair by QR)</a> · container data lives in var/integrations; manage the stack from the Integrations buttons below. Container settings apply on the next stack restart.</p><p class="small" id="run-hint"><strong>Mode:</strong> automatic is shown for changedetection/webhook runs only; run pending only queues the normal collector; manual run starts the worker now; test run uses the isolated test zone. Index page cap is optional: 0 means crawl all pages until the portal has no Next page; detail limit controls detail/test records.</p></div><div class="subsection"><h3>Action buttons</h3><div id="action-zones"></div></div></div>
<div class="card" data-tab="settings"><h2>Settings</h2><details class="adv-settings" open><summary class="small">Collector, timer &amp; storage settings (apply on the next run/launch)</summary><h3>Storage paths</h3><div class="settings-grid"><label class="small">Records folder <input id="records-dir" size="42"></label> <label class="small">Calendar packages <input id="calendar-dir" size="42"></label> <label class="small">Test sandbox <input id="records-test-dir" size="42"></label> <button onclick="savePathSettings()">Save paths</button></div><h3>Run cadence</h3><div class="settings-grid"><label class="small">Next-run interval (min) <input id="set-PC_NEXT_RUN_INTERVAL_MINUTES" size="5"></label> <label class="small">Webhook index page cap <input id="set-PC_WEBHOOK_INDEX_LIMIT" size="5"></label> <label class="small">Webhook detail limit (0 = all) <input id="set-PC_WEBHOOK_DETAIL_LIMIT" size="5"></label> <label class="small">Test-zone records <input id="set-PC_TEST_ZONE_LIMIT" size="5"></label> <label class="small">Monitor stale sec <input id="set-PC_MONITOR_STALE_SECONDS" size="5"></label> <label class="small">Deadline 'soon' days <input id="set-PC_MONITOR_DEADLINE_SOON_DAYS" size="5"></label></div><h3>Timer window</h3><div class="settings-grid"><label class="small">Timer width <input id="set-PC_NEXT_RUN_TIMER_WIDTH" size="5"></label> <label class="small">Timer height <input id="set-PC_NEXT_RUN_TIMER_HEIGHT" size="5"></label> <label class="small">Timer top <input id="set-PC_NEXT_RUN_TIMER_TOP" size="5"></label> <label class="small">Timer latest records <input id="set-PC_NEXT_RUN_TIMER_RECORDS" size="5"></label> <label class="small">Timer data refresh sec <input id="set-PC_NEXT_RUN_TIMER_DATA_REFRESH_SECONDS" size="5"></label></div><h3>Integrations</h3><div class="settings-grid"><label class="small">changedetection URL <input id="set-CHANGEDETECTION_BASE_URL" size="24"></label><button onclick="saveAdvancedSettings()">Save settings</button></div><p><label class="small"><input type="checkbox" id="set-PC_TEST_ZONE_AUTORUN" onchange="saveMonitorSetting('PC_TEST_ZONE_AUTORUN', this.checked ? '1' : '0')"> Auto-run test zone when no new records</label> <label class="small"><input type="checkbox" id="set-PC_RUN_UPDATE_BEFORE_RUN" onchange="saveMonitorSetting('PC_RUN_UPDATE_BEFORE_RUN', this.checked ? '1' : '0')"> Update local copy before each run</label></p></details></div>
<div class="card" data-tab="operations"><h2>Diagnostics</h2><table id="diagnostics"></table></div>
<div class="card" data-tab="records"><h2>Records Pendings</h2><div id="records-pending" class="record-card record-pending">Records Pendings: —</div><p class="small">Use Record selector and filters → Detail status = Pending records for full selectors/open actions.</p></div>
<div class="card" data-tab="records"><h2>Records Completed</h2><div id="records-completed" class="record-card record-completed">Records Completed: —</div><p class="small">Use Record selector and filters → Detail status = Completed records for full selectors/open actions.</p></div>
<div class="card" data-tab="records"><h2>Database summary</h2><p class="small">Read-only archive database summary with counters, status breakdown, recent records and DB elements/columns.</p><pre id="records-db-summary">Database summary loading…</pre><p><button onclick="refreshDbReview('records-db-summary')">Refresh DB summary</button></p></div>
<div class="card" data-tab="whatsapp"><h2>WhatsApp destinations & toggles</h2><p class="small">Dedicated WhatsApp section: default and per-purpose destinations, notification toggles, and test send. These fields mirror the quick controls in Operations.</p><textarea id="waha-message-wa" placeholder="Default WhatsApp group/channel chat ID destination"></textarea><p class="small"><label>Index alerts <input id="waha-index-wa" size="26" placeholder="…@g.us"></label> <label>Item details <input id="waha-details-wa" size="26" placeholder="…@g.us"></label> <label>Status changes <input id="waha-status-wa" size="26" placeholder="…@g.us"></label></p><p><label class="small"><input type="checkbox" id="notify-whatsapp-wa" onchange="syncWhatsappMirror('wa'); saveMonitorSetting('PC_NOTIFY_WHATSAPP', this.checked ? '1' : '0')"> Notify by WhatsApp (index alerts)</label> <label class="small"><input type="checkbox" id="notify-details-wa" onchange="syncWhatsappMirror('wa'); saveMonitorSetting('PC_NOTIFY_DETAILS', this.checked ? '1' : '0')"> Detail follow-up WhatsApp</label> <button onclick="saveWahaFrom('wa')">Save WhatsApp destinations</button> <button onclick="sendTestWhatsapp()">Send test WhatsApp</button></p></div>

<div class="card" data-tab="whatsapp"><h2>WhatsApp advanced delivery settings</h2><p class="small">WAHA server, retry, event and deadline-notification settings live here so the Settings tab stays focused on collector/timer options.</p><div class="settings-grid"><label class="small">WhatsApp source <input id="set-PC_WAHA_SOURCE" size="16"></label> <label class="small">WhatsApp within N days <input id="set-PC_NOTIFY_WITHIN_DAYS" size="5" placeholder="all"></label> <label class="small">WAHA retries <input id="set-PC_WAHA_RETRIES" size="5"></label> <label class="small">WAHA base URL <input id="set-PC_WAHA_BASE_URL" size="24"></label> <label class="small">WAHA session <input id="set-PC_WAHA_SESSION" size="12"></label> <label class="small">WAHA events <input id="set-PC_WAHA_NOTIFY_EVENTS" size="40"></label> <label class="small">WAHA server port <input id="set-WAHA_PORT" size="6"></label> <label class="small">WAHA server API key <input id="set-WAHA_API_KEY" size="20"></label> <label class="small">WAHA dashboard user <input id="set-WAHA_DASHBOARD_USERNAME" size="12"></label> <label class="small">WAHA dashboard password (default 12345678) <input id="set-WAHA_DASHBOARD_PASSWORD" size="14"></label> <button onclick="saveAdvancedSettings()">Save WhatsApp advanced settings</button></div><p class="small">The WAHA dashboard login defaults to admin / 12345678 after every install/reinstall so the review page is always reachable; change the password here whenever you like — it applies on the next docker stack restart.</p><p><label class="small"><input type="checkbox" id="set-PC_WAHA_ENABLED" onchange="saveMonitorSetting('PC_WAHA_ENABLED', this.checked ? '1' : '0')"> Enable WAHA WhatsApp sending</label> <label class="small"><input type="checkbox" id="set-PC_NOTIFY_SKIP_EXPIRED" onchange="saveMonitorSetting('PC_NOTIFY_SKIP_EXPIRED', this.checked ? '1' : '0')"> Skip already-expired opportunities</label></p></div>
<div class="card" data-tab="whatsapp"><h2>WhatsApp filters</h2><p class="small">Per-destination rules deciding which opportunities are announced. OR between comma-separated rules · AND with '+' (<code>salud + panama</code>) · NOT with '-' (<code>-construccion</code> excludes even when another rule matches). Blank destination = the shared filter; everything blank = announce all.</p><p><label class="small">Shared <input id="flt-global" size="30"></label> <label class="small">Index alerts <input id="flt-index" size="30"></label> <label class="small">Item details <input id="flt-details" size="30"></label> <label class="small">Status changes <input id="flt-status" size="30"></label> <button onclick="saveWahaFilters()">Save filters</button></p></div><div class="card" data-tab="whatsapp"><h2>WhatsApp message formats</h2><p class="small">Customize the text of each message family with {{{{placeholder}}}} fields (unknown placeholders stay literal). <label class="small">Format <select id="fmt-kind" onchange="loadWahaFormat()"><option value="index" selected>Index alert</option><option value="details">Detail follow-up</option><option value="status">Status change</option></select></label> <button onclick="previewWahaFormat()">Preview</button> <button onclick="saveWahaFormat()">Save format</button> <button onclick="resetWahaFormat()">Reset to default</button> <span id="fmt-state" class="small"></span></p><textarea id="fmt-template" rows="8" style="width:100%; box-sizing:border-box"></textarea><p class="small" id="fmt-placeholders"></p><pre id="fmt-preview" style="max-height: 300px"></pre></div><div class="card" data-tab="records"><h2>Opportunity calendar</h2><p class="small">Collected opportunities by day, week, month or year. <label class="small">View <select id="cal-view" onchange="loadCalendar()"><option value="day">Day</option><option value="week">Week</option><option value="month" selected>Month</option><option value="year">Year</option></select></label> <label class="small">Date field <select id="cal-field" onchange="loadCalendar()"><option value="end" selected>Deadline (end)</option><option value="start">Start</option><option value="downloaded">Downloaded</option></select></label> <label class="small">Anchor <input id="cal-date" size="10" placeholder="YYYY-MM-DD"></label> <button onclick="loadCalendar(-1)">◀ Prev</button> <button onclick="loadCalendar(0)">Today</button> <button onclick="loadCalendar(1)">Next ▶</button> <button onclick="loadCalendar()">Show</button></p><div id="calendar-visual" class="chart" style="min-height:120px;margin:8px 0">Calendar visual loading…</div><pre id="calendar-text" style="max-height: 420px">Loading calendar…</pre></div><div class="card" data-tab="settings"><h2>Work templates</h2><p class="small">Reusable work files copied into <code>templates/</code> inside each record folder. Set the source folder, tick the files to use, save the selection. Records downloaded in each run receive them automatically; files already inside a record are never overwritten. Same source/selection as <code>pcc templates</code> and the native monitor.</p><p><label class="small">Source folder <input id="set-PC_TEMPLATES_SRC_DIR" size="42" placeholder="blank = var/templates"></label> <button onclick="saveTemplatesSource()">Save source</button> <button onclick="loadTemplates()">Refresh files</button> <button onclick="saveTemplatesSelection()">Save selection</button> <button onclick="runAction('Apply work templates')">Apply to all records</button></p><div id="templates-files" class="small">Loading template files…</div></div><div class="card" data-tab="records"><h2>Record selector and filters</h2><p class="small">Collected records as “[downloaded timestamp | DTEND status] NUMERO — description”; choose newest-first or oldest-first ordering. Use filters first, then Ctrl/Shift-select one or more records to notify or import calendars.</p><p><label class="small">Deadline <select id="record-status"><option value="all">All</option><option value="soon">Next to expire</option><option value="expired">Expired</option><option value="upcoming">Upcoming</option><option value="unknown">No date / needs repair</option></select></label> <label class="small">Detail status <select id="record-detail-status"><option value="all">All</option><option value="pending">Pending records</option><option value="saved">Completed records</option><option value="failed">Failed records</option></select></label> <label class="small">Order by <select id="record-order-field"><option value="downloaded">Downloaded date</option><option value="end">End date</option><option value="start">Start date</option></select></label> <label class="small"><select id="record-order"><option value="newest">Newest first</option><option value="oldest">Oldest first</option></select></label> <label class="small">DTEND on/after <input type="text" id="record-mindate" placeholder="YYYY-MM-DD [HH:MM]" size="16"></label> <label class="small">on/before <input type="text" id="record-maxdate" placeholder="YYYY-MM-DD [HH:MM]" size="16"></label> <label class="small">DTSTART on/after <input type="text" id="record-start-mindate" placeholder="YYYY-MM-DD [HH:MM]" size="16"></label> <label class="small">on/before <input type="text" id="record-start-maxdate" placeholder="YYYY-MM-DD [HH:MM]" size="16"></label> <label class="small">Downloaded on/after <input type="text" id="record-downloaded-mindate" placeholder="YYYY-MM-DD [HH:MM]" size="16"></label> <label class="small">on/before <input type="text" id="record-downloaded-maxdate" placeholder="YYYY-MM-DD [HH:MM]" size="16"></label> <span class="small">Legend: <span style="color:#86efac;font-weight:700">upcoming</span> · <span style="color:#fcd34d;font-weight:700">next to expire</span> · <span style="color:#fca5a5;font-weight:700">expired</span></span></p><p><select id="record-index" multiple size="10"></select> <button onclick="refreshRecordIndex()">Refresh list</button> <button onclick="openRecordFolder()">Open record folder</button> <button onclick="openRecordPortal()">Open in portal</button> <button onclick="notifySelectedRecords()">Notify selected WhatsApp</button> <button onclick="importSelectedCalendars()">Import selected calendars</button> <button onclick="templatesSelectedRecords()">Copy templates to selected</button></p><p id="record-detail" class="small">Loading record index…</p></div>

<div class="card" data-tab="decision"><h2>KPI Dashboard</h2><p class="small">All KPIs in one tab: index scan intake, detail download throughput, WAHA delivery, deadline repair, plus diagrams about the collected items, contracting entities and locations so the numbers point at a decision.</p><div id="decision-kpis" class="kpi-grid"></div><div class="diagram-grid"><div class="chart"><h3>Detail status mix</h3><div id="decision-status"></div></div><div class="chart"><h3>Index groups</h3><div id="decision-groups"></div></div><div class="chart"><h3>Monthly intake trend</h3><div id="decision-trend"></div></div><div class="chart"><h3>Top contracting entities</h3><div id="decision-entities"></div></div><div class="chart"><h3>Locations / buying units (from details)</h3><div id="decision-locations"></div></div><div class="chart"><h3>Detail queue pressure</h3><div id="decision-deadlines"></div></div><div class="chart"><h3>Items analysis</h3><div id="decision-items"></div></div><div class="chart"><h3>Item keywords</h3><div id="decision-item-keywords" class="keyword-cloud"></div></div></div><pre id="decision-recommendations">Loading decision signals…</pre><p><button onclick="refreshDecisionDashboard()">Refresh KPIs</button></p></div>
<div class="card" data-tab="records"><h2>Database review</h2><p class="small">Same database details in a collapsible review panel. Refresh after a run or a reset.</p><pre id="db-review">Loading database snapshot…</pre><p><button onclick="refreshDbReview()">Refresh DB snapshot</button></p></div>
<div class="card" data-tab="settings"><h2>Reset / review from zero</h2><p class="small">Separate actions, from a soft detail re-queue to a full wipe. The two destructive wipes ask for confirmation first. Each runs src/tools/110-reset.py; check the current action log and refresh the DB snapshot above to verify.</p><p><button onclick="runReset('requeue-details')">Re-queue all details</button><button onclick="runReset('reset-notify')">Reset notify / review flags</button><button class="danger" onclick="runReset('wipe-db')">Wipe database only</button><button class="danger" onclick="runReset('wipe-all')">Wipe EVERYTHING</button></p><p id="reset-status" class="small"></p></div>
<div class="card" data-tab="operations"><h2>Recent worker log</h2><pre id="worker-log"></pre></div>
<div class="card" data-tab="operations"><h2>Current action log</h2><pre id="current-log"></pre></div>
<script>
let doneSince = null;
let timer = null;
const actionZones = {ACTIONS_JSON};
const labels = [
  ['Phase', 'PHASE'], ['Status', 'STATUS'], ['Mode', 'MODE'], ['ETA', 'ETA'], ['Index page cap', 'INDEX_LIMIT'], ['Detail limit', 'DETAIL_LIMIT'],
  ['Step', 'STEP'], ['Item', 'ITEM'], ['Started', 'STARTED_AT'], ['Updated', 'UPDATED_AT'],
  ['Found rows', 'RECORDS_FOUND'], ['New records', 'RECORDS_NEW'], ['Existing records', 'RECORDS_EXISTING'],
  ['Details saved/skipped', 'RECORDS_SAVED'], ['Detail failures', 'RECORDS_FAILED'],
  ['Pending details', 'RECORDS_PENDING'], ['Test records', 'RECORDS_TEST'], ['Extra', 'EXTRA']
];
function esc(value) {{
  return String(value ?? '').replace(/[&<>"']/g, ch => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[ch]));
}}
function render(data) {{
  const p = data.progress || {{}};
  const percent = data.percent || 0;
  document.getElementById('server-time').textContent = 'Server time: ' + (data.server_time || '-');
  document.getElementById('refresh-label').textContent = data.refresh_seconds || {REFRESH_SECONDS};
  const fill = document.getElementById('fill');
  fill.style.width = percent + '%';
  fill.textContent = percent + '%';
  document.getElementById('message').textContent = p.MESSAGE || '';
  const rows = labels.map(([label, key]) => {{
    let value = p[key] ?? '-';
    if (key === 'STEP') value = `${{p.STEP_CURRENT ?? '-'}} / ${{p.STEP_TOTAL ?? '-'}}`;
    if (key === 'ITEM') value = `${{p.ITEM_CURRENT ?? '-'}} / ${{p.ITEM_TOTAL ?? '-'}}`;
    return `<tr><th>${{esc(label)}}</th><td>${{esc(value)}}</td></tr>`;
  }}).join('');
  document.getElementById('diagnostics').innerHTML = rows;
  document.getElementById('processes').innerHTML = Object.entries(data.processes || {{}}).map(([name, value]) =>
    `<span class="pill ${{value ? 'on' : 'off'}}">${{esc(name)}}: ${{value ? 'RUNNING' : 'off'}}</span>`
  ).join('');
  updateRunControls(data);
  renderQueue(data);
  renderRecordSummary(data);
  document.getElementById('worker-log').textContent = data.worker_log || '';
  document.getElementById('current-log').textContent = data.current_log || '';
  const waha = document.getElementById('waha-message');
  if (waha && document.activeElement !== waha) waha.value = data.waha_chat_id || '';
  const wahawa = document.getElementById('waha-message-wa'); if (wahawa && document.activeElement !== wahawa) wahawa.value = data.waha_chat_id || '';
  [['waha-index','waha_chat_id_index'],['waha-details','waha_chat_id_details'],['waha-status','waha_chat_id_status']].forEach(([id, key]) => {{
    const el = document.getElementById(id);
    if (el && document.activeElement !== el) el.value = data[key] || '';
    const mirror = document.getElementById(id + '-wa'); if (mirror && document.activeElement !== mirror) mirror.value = data[key] || '';
  }});
  const settings = data.settings || {{}};
  const notifyToggle = document.getElementById('notify-whatsapp');
  if (notifyToggle && document.activeElement !== notifyToggle) notifyToggle.checked = String(settings.PC_NOTIFY_WHATSAPP ?? '1') !== '0';
  const notifyToggleWa = document.getElementById('notify-whatsapp-wa'); if (notifyToggleWa) notifyToggleWa.checked = String(settings.PC_NOTIFY_WHATSAPP ?? '1') !== '0';
  const detailsToggle = document.getElementById('notify-details');
  if (detailsToggle && document.activeElement !== detailsToggle) detailsToggle.checked = String(settings.PC_NOTIFY_DETAILS ?? '1') !== '0';
  const detailsToggleWa = document.getElementById('notify-details-wa'); if (detailsToggleWa) detailsToggleWa.checked = String(settings.PC_NOTIFY_DETAILS ?? '1') !== '0';
  const calendarToggle = document.getElementById('calendar-auto-import');
  if (calendarToggle && document.activeElement !== calendarToggle) calendarToggle.checked = String(settings.PC_CALENDAR_AUTO_IMPORT ?? '0') === '1';
  [['records-dir', 'PC_RECORDS_DIR'], ['calendar-dir', 'PC_CALENDAR_DIR'], ['records-test-dir', 'PC_RECORDS_TEST_DIR']].forEach(([id, key]) => {{
    const el = document.getElementById(id);
    if (el && document.activeElement !== el) el.value = settings[key] || '';
  }});
  ['PC_WAHA_SOURCE','PC_NEXT_RUN_INTERVAL_MINUTES','PC_MONITOR_DEADLINE_SOON_DAYS','PC_WEBHOOK_INDEX_LIMIT','PC_WEBHOOK_DETAIL_LIMIT','PC_NOTIFY_WITHIN_DAYS','PC_WAHA_RETRIES','PC_WAHA_BASE_URL','PC_WAHA_SESSION','PC_WAHA_NOTIFY_EVENTS','PC_TEST_ZONE_LIMIT','PC_MONITOR_STALE_SECONDS','PC_NEXT_RUN_TIMER_WIDTH','PC_NEXT_RUN_TIMER_HEIGHT','PC_NEXT_RUN_TIMER_TOP','PC_NEXT_RUN_TIMER_RECORDS','PC_NEXT_RUN_TIMER_DATA_REFRESH_SECONDS','CHANGEDETECTION_BASE_URL','WAHA_PORT','WAHA_API_KEY','WAHA_DASHBOARD_USERNAME','WAHA_DASHBOARD_PASSWORD','PC_TEMPLATES_SRC_DIR'].forEach(key => {{ const el = document.getElementById('set-' + key); if (el && document.activeElement !== el && settings[key] !== undefined) el.value = settings[key]; }});
  [['PC_WAHA_ENABLED','0'],['PC_NOTIFY_SKIP_EXPIRED','0'],['PC_TEST_ZONE_AUTORUN','0'],['PC_RUN_UPDATE_BEFORE_RUN','1']].forEach(([key, dflt]) => {{ const el = document.getElementById('set-' + key); if (el && document.activeElement !== el) el.checked = String(settings[key] ?? dflt) === '1'; }});
  const note = document.getElementById('done-note');
  if (data.done) {{
    if (!doneSince) doneSince = Date.now();
    const wait = data.auto_close_enabled ? Number(data.auto_close_seconds || 0) : 0;
    const remaining = Math.max(0, wait - Math.floor((Date.now() - doneSince) / 1000));
    note.hidden = false;
    note.textContent = wait > 0 ? `Live run finished. This monitor will auto-close in about ${{remaining}} seconds.` : 'Run finished. Auto-close is disabled for test zone and manual desktop actions.';
    if (wait > 0 && remaining <= 0) {{
      window.close();
      document.body.innerHTML = '<div class="card"><h1>PanamaCompra monitor finished</h1><p>The run is done. You can close this tab.</p></div>';
      return;
    }}
  }} else {{
    doneSince = null;
    note.hidden = true;
  }}
}}
async function postForm(path, body) {{
  const response = await fetch(path, {{method: 'POST', headers: {{'Content-Type': 'application/x-www-form-urlencoded'}}, body}});
  const text = await response.text();
  document.getElementById('button-status').textContent = text.trim();
  poll();
}}
// Keys that mean real collection work is happening. A webhook-triggered run
// shows up here, so the mode toggle + Request button lock while any is active.
const RUN_WORK_KEYS = ['worker', 'index', 'detail', 'calendar', 'messaging', 'test_run', 'request'];
function updateRunControls(data) {{
  const procs = data.processes || {{}};
  const busy = RUN_WORK_KEYS.some(k => procs[k]);
  document.querySelectorAll('input[name="run-mode"]').forEach(el => {{ el.disabled = busy; }});
  ['index-limit', 'detail-limit'].forEach(id => {{ const el = document.getElementById(id); if (el) el.disabled = busy; }});
  const mode = String((data.progress || {{}}).MODE || '').toUpperCase();
  if (busy) {{
    const modeValue = mode === 'AUTO' ? 'auto' : mode === 'MANUAL' ? 'manual' : mode === 'TEST' ? 'test' : 'restart';
    const modeInput = document.querySelector(`input[name="run-mode"][value="${{modeValue}}"]`);
    if (modeInput) modeInput.checked = true;
  }} else {{
    const autoInput = document.querySelector('input[name="run-mode"][value="auto"]');
    const restartInput = document.querySelector('input[name="run-mode"][value="restart"]');
    if (autoInput && autoInput.checked && restartInput) restartInput.checked = true;
  }}
  document.querySelector('input[name="run-mode"][value="auto"]').disabled = true;
  const runBtn = document.getElementById('run-button');
  if (runBtn) {{ runBtn.disabled = busy; runBtn.textContent = busy ? 'Run in progress…' : 'Request selected run'; }}
}}
function requestRun() {{
  const checked = document.querySelector('input[name="run-mode"]:checked');
  const mode = encodeURIComponent(checked ? checked.value : 'restart');
  const detailLimit = encodeURIComponent(document.getElementById('detail-limit').value || '99');
  const indexLimit = encodeURIComponent(document.getElementById('index-limit').value || '0');
  postForm('/api/request-run', `mode=${{mode}}&detail_limit=${{detailLimit}}&index_limit=${{indexLimit}}`);
}}
function importCalendars() {{ postForm('/api/import-calendars', ''); }}
function stopRun() {{ postForm('/api/manual-action', 'label=' + encodeURIComponent('Stop active run')); }}
function runAction(label) {{ postForm('/api/manual-action', 'label=' + encodeURIComponent(label)); }}
function renderActionZones() {{
  const root = document.getElementById('action-zones');
  const zones = [...new Set(actionZones.map(a => a.zone))];
  root.innerHTML = zones.map(zone => `<div class="zone"><h3>${{esc(zone)}}</h3>` + actionZones.filter(a => a.zone === zone).map(a => `<button onclick="runAction('${{esc(a.label)}}')">${{esc(a.label)}}</button><span class="small">${{esc(a.comment)}}</span><br>`).join('') + `</div>`).join('');
}}
function saveWaha() {{ const val = (id, fallback) => ((document.getElementById(id) || document.getElementById(fallback) || {{value:''}}).value); const v = (id, fallback) => encodeURIComponent(val(id, fallback)); postForm('/api/waha-destination', 'chat_id=' + v('waha-message', 'waha-message-wa') + '&chat_id_index=' + v('waha-index', 'waha-index-wa') + '&chat_id_details=' + v('waha-details', 'waha-details-wa') + '&chat_id_status=' + v('waha-status', 'waha-status-wa')); }}
function syncWhatsappMirror(source) {{
  const pairs = [['waha-message','waha-message-wa'],['waha-index','waha-index-wa'],['waha-details','waha-details-wa'],['waha-status','waha-status-wa']];
  pairs.forEach(([a,b]) => {{ const from = document.getElementById(source === 'wa' ? b : a); const to = document.getElementById(source === 'wa' ? a : b); if (from && to && document.activeElement !== to) to.value = from.value; }});
  [['notify-whatsapp','notify-whatsapp-wa'],['notify-details','notify-details-wa']].forEach(([a,b]) => {{ const from = document.getElementById(source === 'wa' ? b : a); const to = document.getElementById(source === 'wa' ? a : b); if (from && to) to.checked = from.checked; }});
}}
function saveWahaFrom(source) {{ syncWhatsappMirror(source); saveWaha(); }}
function sendTestWhatsapp() {{ postForm('/api/test-whatsapp', ''); }}
function saveTemplatesSource() {{ saveMonitorSetting('PC_TEMPLATES_SRC_DIR', document.getElementById('set-PC_TEMPLATES_SRC_DIR').value); setTimeout(loadTemplates, 400); }}
async function loadTemplates() {{
  const wrap = document.getElementById('templates-files');
  try {{
    const response = await fetch('/api/templates', {{cache: 'no-store'}});
    const data = await response.json();
    if (!data.files.length) {{ wrap.textContent = 'No template files in ' + data.source + ' — drop your work files there and press Refresh files.'; return; }}
    wrap.innerHTML = data.files.map(f => '<label class="small" style="margin-right:14px; white-space:nowrap"><input type="checkbox" class="tpl-file" value="' + encodeURIComponent(f) + '"' + (data.selected.includes(f) ? ' checked' : '') + '> ' + f + '</label>').join(' ');
  }} catch (err) {{ wrap.textContent = 'Template list unavailable: ' + err; }}
}}
async function loadWahaFilters() {{
  try {{
    const response = await fetch('/api/waha-filters', {{cache: 'no-store'}});
    const data = await response.json();
    [['flt-global','global'],['flt-index','index'],['flt-details','details'],['flt-status','status']].forEach(([id, key]) => {{
      const el = document.getElementById(id);
      if (el && document.activeElement !== el) el.value = data[key] || '';
    }});
  }} catch (err) {{ /* filters card stays editable */ }}
}}
function saveWahaFilters() {{
  const v = id => encodeURIComponent((document.getElementById(id) || {{value:''}}).value);
  postForm('/api/waha-filters', 'global=' + v('flt-global') + '&index=' + v('flt-index') + '&details=' + v('flt-details') + '&status=' + v('flt-status'));
  setTimeout(loadWahaFilters, 400);
}}
async function loadWahaFormat() {{
  const kind = document.getElementById('fmt-kind').value;
  try {{
    const response = await fetch('/api/waha-format?kind=' + kind, {{cache: 'no-store'}});
    const data = await response.json();
    document.getElementById('fmt-template').value = data.template;
    document.getElementById('fmt-state').textContent = data.custom ? '(custom format active)' : '(built-in default)';
    document.getElementById('fmt-placeholders').textContent = 'Placeholders: ' + Object.keys(data.placeholders).map(k => '{{' + k + '}}').join(' ');
    document.getElementById('fmt-preview').textContent = data.preview;
  }} catch (err) {{ document.getElementById('fmt-state').textContent = 'Format unavailable: ' + err; }}
}}
async function previewWahaFormat() {{
  const kind = document.getElementById('fmt-kind').value;
  const template = document.getElementById('fmt-template').value;
  const response = await fetch('/api/waha-format', {{method: 'POST', headers: {{'Content-Type': 'application/x-www-form-urlencoded'}}, body: 'action=preview&kind=' + kind + '&template=' + encodeURIComponent(template)}});
  document.getElementById('fmt-preview').textContent = await response.text();
}}
function saveWahaFormat() {{
  const kind = document.getElementById('fmt-kind').value;
  postForm('/api/waha-format', 'action=save&kind=' + kind + '&template=' + encodeURIComponent(document.getElementById('fmt-template').value));
  setTimeout(loadWahaFormat, 400);
}}
function resetWahaFormat() {{
  postForm('/api/waha-format', 'action=reset&kind=' + document.getElementById('fmt-kind').value);
  setTimeout(loadWahaFormat, 400);
}}
let calendarAnchor = '';
async function loadCalendar(shift) {{
  const view = document.getElementById('cal-view').value;
  const field = document.getElementById('cal-field').value;
  const dateBox = document.getElementById('cal-date');
  if (shift === 0) {{ calendarAnchor = ''; dateBox.value = ''; }}
  const anchor = (dateBox.value || calendarAnchor).trim();
  let params = 'view=' + view + '&field=' + field;
  if (anchor) params += '&date=' + encodeURIComponent(anchor);
  if (shift) params += '&shift=' + shift;
  try {{
    const response = await fetch('/api/calendar?' + params, {{cache: 'no-store'}});
    const data = await response.json();
    calendarAnchor = data.anchor;
    if (document.activeElement !== dateBox) dateBox.value = data.anchor;
    document.getElementById('calendar-text').textContent = data.text;
  }} catch (err) {{ document.getElementById('calendar-text').textContent = 'Calendar unavailable: ' + err; }}
}}
function saveTemplatesSelection() {{
  const files = Array.from(document.querySelectorAll('.tpl-file:checked')).map(el => 'file=' + el.value);
  postForm('/api/templates-select', files.join('&'));
  setTimeout(loadTemplates, 400);
}}
function saveMonitorSetting(key, value) {{ postForm('/api/monitor-setting', 'key=' + encodeURIComponent(key) + '&value=' + encodeURIComponent(value)); }}
function savePathSettings() {{
  [['PC_RECORDS_DIR', 'records-dir'], ['PC_CALENDAR_DIR', 'calendar-dir'], ['PC_RECORDS_TEST_DIR', 'records-test-dir']].forEach(([key, id]) => saveMonitorSetting(key, document.getElementById(id).value));
}}
function saveAdvancedSettings() {{
  ['PC_WAHA_SOURCE','PC_NEXT_RUN_INTERVAL_MINUTES','PC_MONITOR_DEADLINE_SOON_DAYS','PC_WEBHOOK_INDEX_LIMIT','PC_WEBHOOK_DETAIL_LIMIT','PC_NOTIFY_WITHIN_DAYS','PC_WAHA_RETRIES','PC_WAHA_BASE_URL','PC_WAHA_SESSION','PC_WAHA_NOTIFY_EVENTS','PC_TEST_ZONE_LIMIT','PC_MONITOR_STALE_SECONDS','PC_NEXT_RUN_TIMER_WIDTH','PC_NEXT_RUN_TIMER_HEIGHT','PC_NEXT_RUN_TIMER_TOP','PC_NEXT_RUN_TIMER_RECORDS','PC_NEXT_RUN_TIMER_DATA_REFRESH_SECONDS','CHANGEDETECTION_BASE_URL','WAHA_PORT','WAHA_API_KEY','WAHA_DASHBOARD_USERNAME','WAHA_DASHBOARD_PASSWORD'].forEach(key => {{ const el = document.getElementById('set-' + key); if (el) saveMonitorSetting(key, el.value); }});
}}
let recordIndex = [];
let recordFiltered = [];
const RECORD_SOON_DAYS = {RECORD_SOON_DAYS};  // DTEND within this many days = "next to expire" (PC_MONITOR_DEADLINE_SOON_DAYS).
const STATUS_COLOR = {{expired: '#fca5a5', soon: '#fcd34d', upcoming: '#86efac', unknown: '#94a3b8'}};
const STATUS_TAG = {{expired: 'EXPIRED', soon: 'SOON', upcoming: 'ok', unknown: 'no date'}};
function parseDeadline(rec) {{
  const raw = (rec.finish_date_guess || '').trim().replace('T', ' ').replace('_', ' ');
  const m = raw.match(/^(\\d{{4}})-(\\d{{2}})-(\\d{{2}})(?:[ T](\\d{{2}}):(\\d{{2}}))?/);
  if (!m) return null;
  return new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]), Number(m[4] || 12), Number(m[5] || 0));
}}
function expiryStatus(rec) {{
  const dt = parseDeadline(rec);
  if (!dt) return 'unknown';
  const now = new Date();
  if (dt < now) return 'expired';
  if (dt <= new Date(now.getTime() + RECORD_SOON_DAYS * 86400000)) return 'soon';
  return 'upcoming';
}}
function deadlineText(rec) {{ return parseDeadline(rec) ? (rec.finish_date_guess || '').replace('T', ' ').replace('_', ' ') : '—'; }}
function startText(rec) {{ const raw = (rec.start_date_guess || '').trim(); return raw ? raw.slice(0, 16).replace('T', ' ').replace('_', ' ') : '—'; }}
function downloadedText(rec) {{ const raw = (rec.detail_saved_at || '').trim(); return raw ? raw.slice(0, 16).replace('T', ' ') : '—'; }}
function parseDownloaded(rec) {{
  const raw = (rec.detail_saved_at || '').trim().replace('T', ' ');
  const m = raw.match(/^(\\d{{4}})-(\\d{{2}})-(\\d{{2}})(?:[ _T](\\d{{2}}):(\\d{{2}}))?/);
  if (!m) return null;
  return new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]), Number(m[4] || 0), Number(m[5] || 0));
}}
function parseStart(rec) {{
  const raw = (rec.start_date_guess || '').trim().replace('T', ' ').replace('_', ' ');
  const m = raw.match(/^(\\d{{4}})-(\\d{{2}})-(\\d{{2}})(?:[ _T](\\d{{2}}):(\\d{{2}}))?/);
  if (!m) return null;
  return new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]), Number(m[4] || 0), Number(m[5] || 0));
}}
function parseIsoLike(raw) {{
  const text = (raw || '').trim().replace('T', ' ').replace('_', ' ');
  const m = text.match(/^(\\d{{4}})-(\\d{{2}})-(\\d{{2}})(?:[ _T](\\d{{2}}):(\\d{{2}}))?/);
  if (!m) return null;
  return new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]), Number(m[4] || 0), Number(m[5] || 0));
}}
function parseRecordOrderDate(rec, field) {{
  if (field === 'end') return parseDeadline(rec);
  if (field === 'start') return parseStart(rec);
  return parseDownloaded(rec) || parseIsoLike(rec.first_seen || '');
}}
function parseFilterBound(raw, upper) {{
  // Accept a date or a date+time; a bare date used as an upper bound covers the
  // whole day. Returns null for blank/invalid input so the bound is ignored.
  const text = (raw || '').trim().replace('T', ' ').replace('_', ' ');
  const m = text.match(/^(\\d{{4}})-(\\d{{2}})-(\\d{{2}})(?:[ ](\\d{{2}}):(\\d{{2}}))?$/);
  if (!m) return null;
  if (m[4] !== undefined) return new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]), Number(m[4]), Number(m[5]));
  return upper
    ? new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]), 23, 59, 59)
    : new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]), 0, 0, 0);
}}
const FAR_FUTURE = new Date(8640000000000000);
function selectedRecord() {{
  const sel = document.getElementById('record-index');
  const idx = sel && sel.selectedOptions.length ? Number(sel.selectedOptions[0].value) : -1;
  return (idx >= 0 && idx < recordFiltered.length) ? recordFiltered[idx] : null;
}}
function selectedRecordNumeros() {{
  const sel = document.getElementById('record-index');
  if (!sel) return [];
  return Array.from(sel.selectedOptions).map(opt => recordFiltered[Number(opt.value)]).filter(Boolean).map(rec => rec.numero).filter(Boolean);
}}
function renderRecordDetail() {{
  const rec = selectedRecord();
  const node = document.getElementById('record-detail');
  if (!rec) {{ node.textContent = recordIndex.length ? 'No records match the filter.' : 'No records collected yet. Run the collector, then Refresh list.'; return; }}
  const st = expiryStatus(rec);
  const detail = rec.detail_status ? '   ·   detail: ' + esc(rec.detail_status) : '';
  node.innerHTML = '<span style="color:' + STATUS_COLOR[st] + ';font-weight:700">' + st.toUpperCase() + '</span>  ·  NUMERO: ' + esc(rec.numero) + detail
    + '<br>' + esc(rec.descripcion || '-')
    + '<br>Downloaded: ' + esc(downloadedText(rec)) + '   ·   DTSTART: ' + esc(startText(rec)) + '   ·   DTEND (deadline): ' + esc(deadlineText(rec));
}}
function applyRecordFilter() {{
  const status = (document.getElementById('record-status') || {{}}).value || 'all';
  const detailStatus = (document.getElementById('record-detail-status') || {{}}).value || 'all';
  const order = (document.getElementById('record-order') || {{}}).value || 'newest';
  const orderField = (document.getElementById('record-order-field') || {{}}).value || 'downloaded';
  const val = id => (document.getElementById(id) || {{}}).value || '';
  const deadlineMin = parseFilterBound(val('record-mindate'), false);
  const deadlineMax = parseFilterBound(val('record-maxdate'), true);
  const startMin = parseFilterBound(val('record-start-mindate'), false);
  const startMax = parseFilterBound(val('record-start-maxdate'), true);
  const downloadedMin = parseFilterBound(val('record-downloaded-mindate'), false);
  const downloadedMax = parseFilterBound(val('record-downloaded-maxdate'), true);
  const inWindow = (value, low, high) => {{
    if (low && (!value || value < low)) return false;
    if (high && (!value || value > high)) return false;
    return true;
  }};
  recordFiltered = recordIndex.filter(r => {{
    if (status !== 'all' && expiryStatus(r) !== status) return false;
    if (detailStatus !== 'all' && String(r.detail_status || '').toLowerCase() !== detailStatus) return false;
    if ((deadlineMin || deadlineMax) && !inWindow(parseDeadline(r), deadlineMin, deadlineMax)) return false;
    if ((startMin || startMax) && !inWindow(parseStart(r), startMin, startMax)) return false;
    if ((downloadedMin || downloadedMax) && !inWindow(parseDownloaded(r), downloadedMin, downloadedMax)) return false;
    return true;
  }});
  recordFiltered.sort((a, b) => {{
    const ad = parseRecordOrderDate(a, orderField);
    const bd = parseRecordOrderDate(b, orderField);
    const av = ad ? ad.getTime() : (order === 'oldest' ? Number.MAX_SAFE_INTEGER : 0);
    const bv = bd ? bd.getTime() : (order === 'oldest' ? Number.MAX_SAFE_INTEGER : 0);
    return order === 'oldest' ? av - bv : bv - av;
  }});
  const sel = document.getElementById('record-index');
  sel.innerHTML = recordFiltered.map((r, i) => {{
    const st = expiryStatus(r);
    const tag = parseDeadline(r) ? (r.finish_date_guess || '').slice(2, 10) : 'no date';
    const dl = parseDownloaded(r);
    const downloaded = dl ? downloadedText(r) : 'not local';
    const label = '(DL ' + downloaded + ' | DTSTART ' + startText(r) + ' | DTEND ' + tag + ' ' + STATUS_TAG[st] + ') ' + (r.numero || '(sin número)') + ' — ' + (r.descripcion || '(sin descripción)');
    return `<option value="${{i}}" style="color:${{STATUS_COLOR[st]}}">${{esc(label)}}</option>`;
  }}).join('');
  renderRecordDetail();
}}
function renderCalendarVisual() {{
  const node = document.getElementById('calendar-visual');
  if (!node) return;
  const today = new Date(); today.setHours(0,0,0,0);
  const rows = [];
  for (let i = 0; i < 14; i++) {{ const d = new Date(today.getTime() + i * 86400000); rows.push({{date:d, label:d.toISOString().slice(0,10), count:0}}); }}
  recordIndex.forEach(rec => {{ const d = parseDeadline(rec); if (!d) return; d.setHours(0,0,0,0); const idx = Math.round((d - today) / 86400000); if (idx >= 0 && idx < rows.length) rows[idx].count++; }});
  const max = Math.max(1, ...rows.map(r => r.count));
  node.innerHTML = '<h3>Next 14 days deadline heatmap</h3>' + rows.map(r => `<button title="Show calendar at ${{r.label}}" onclick="document.getElementById('cal-date').value='${{r.label}}'; loadCalendar()" style="min-width:86px;margin:3px;background:${{r.count ? '#14532d' : '#1f2937'}}">${{r.label.slice(5)}}<br><b>${{r.count}}</b></button>`).join('') + '<p class="small">Click a day to jump the calendar anchor. Counts come from collected DTEND/deadline values.</p>';
}}
async function refreshRecordIndex() {{
  try {{
    const response = await fetch('/api/record-index', {{cache: 'no-store'}});
    recordIndex = await response.json();
    renderCalendarVisual();
  }} catch (err) {{ recordIndex = []; }}
  applyRecordFilter();
  renderCalendarVisual();
}}
function openRecordFolder() {{
  const rec = selectedRecord();
  if (!rec) {{ document.getElementById('button-status').textContent = 'Select a record first.'; return; }}
  postForm('/api/open-record-folder', 'numero=' + encodeURIComponent(rec.numero));
}}
function openRecordPortal() {{
  const rec = selectedRecord();
  if (!rec || !rec.link) {{ document.getElementById('button-status').textContent = 'No portal link for the selected record.'; return; }}
  window.open(rec.link, '_blank', 'noopener');
}}
function notifySelectedRecords() {{
  const numeros = selectedRecordNumeros();
  if (!numeros.length) {{ document.getElementById('button-status').textContent = 'Select one or more records first.'; return; }}
  postForm('/api/selected-record-action', 'action=notify&' + numeros.map(n => 'numero=' + encodeURIComponent(n)).join('&'));
}}
function templatesSelectedRecords() {{
  const numeros = selectedRecordNumeros();
  if (!numeros.length) return;
  postForm('/api/selected-record-action', 'action=templates&' + numeros.map(n => 'numero=' + encodeURIComponent(n)).join('&'));
}}
function importSelectedCalendars() {{
  const numeros = selectedRecordNumeros();
  if (!numeros.length) {{ document.getElementById('button-status').textContent = 'Select one or more records first.'; return; }}
  postForm('/api/selected-record-action', 'action=calendar&' + numeros.map(n => 'numero=' + encodeURIComponent(n)).join('&'));
}}

function showTab(tab) {{
  document.querySelectorAll('[data-tab-button]').forEach(btn => btn.classList.toggle('active', btn.dataset.tabButton === tab));
  document.querySelectorAll('.card[data-tab]').forEach(card => card.classList.toggle('tab-active', card.dataset.tab === tab));
  if (tab === 'decision') refreshDecisionDashboard();
}}
function bars(nodeId, rows) {{
  const node = document.getElementById(nodeId);
  const max = Math.max(1, ...rows.map(r => Number(r.count || 0)));
  node.innerHTML = rows.length ? rows.map(r => `<div class="bar-row"><span>${{esc(String(r.label || r.status || r.grupo || '—').slice(0, 28))}}</span><span class="bar-track"><span class="bar-fill" style="display:block;width:${{Math.max(4, Number(r.count || 0) / max * 100)}}%"></span></span><b>${{r.count || 0}}</b></div>`).join('') : '<span class="small">No data yet.</span>';
}}
async function refreshDecisionDashboard() {{
  const s = await (await fetch('/api/db-stats', {{cache: 'no-store'}})).json();
  const k = document.getElementById('decision-kpis');
  const closure = Number(s.total || 0) ? Math.round(Number(s.saved || 0) / Number(s.total || 1) * 100) : 0;
  const items = s.item_analysis || {{}};
  k.innerHTML = [ ['Index archive total', s.total || 0], ['Details saved', s.saved || 0], ['Details pending', s.pending || 0], ['Details failed', s.failed || 0], ['Item lines parsed', items.total_items || 0], ['Avg items / record', items.avg_items_per_record || 0], ['Deadline repairs', s.needs_deadline || 0], ['Detail closure', closure + '%'] ].map(x => `<div class="kpi"><span>${{x[0]}}</span><b>${{x[1]}}</b></div>`).join('');
  bars('decision-status', (s.status_breakdown || []).map(r => ({{label: r.status, count: r.count}})));
  bars('decision-groups', (s.groups || []).slice(0, 10).map(r => ({{label: r.grupo, count: r.count}})));
  bars('decision-trend', (s.monthly_trend || []).slice(0, 12));
  bars('decision-entities', (s.entities || []).slice(0, 10));
  bars('decision-locations', ((items.top_locations || []).length ? items.top_locations : (s.dependencias || [])).slice(0, 10));
  bars('decision-deadlines', [{{label:'Completed', count:s.saved||0}},{{label:'Pending', count:s.pending||0}},{{label:'Failed', count:s.failed||0}},{{label:'Needs repair', count:s.needs_deadline||0}},{{label:'Notify backlog', count:s.notify_backlog||0}}]);
  bars('decision-items', [{{label:'Records sampled', count:items.sampled_records||0}},{{label:'Records with items', count:items.records_with_items||0}},{{label:'Total item lines', count:items.total_items||0}},{{label:'Largest record items', count:(items.max_items_record||{{}}).count||0}}]);
  const itemWords = (items.top_item_keywords || []).map(r => r.label || '').filter(Boolean);
  document.getElementById('decision-item-keywords').innerHTML = itemWords.length ? itemWords.map((w,i) => `<span style="font-size:${{0.85 + (itemWords.length-i)/18}}rem">${{esc(w)}}</span>`).join('') : '<span class="small">No parsed item keywords yet.</span>';
  const biggest = items.max_items_record || {{}};
  const topEntity = (s.entities || [])[0] || {{}};
  const topLocation = ((items.top_locations || [])[0]) || {{}};
  const sampleLines = (items.sample_items || []).slice(0, 5).map(it => `  - ${{it.numero || 'record'}}: ${{it.descripcion || '(item without description)'}}${{it.cantidad ? ' · qty ' + it.cantidad : ''}}`).join('\n');
  document.getElementById('decision-recommendations').textContent = `Index/detail/items decision signals\n• If Details failed > 0, repair collector/detail issues before expanding index page caps.\n• If Details pending grows, prioritize detail download capacity over more index scans.\n• Item lines parsed: ${{items.total_items || 0}} across ${{items.records_with_items || 0}} records; largest record: ${{biggest.numero || '-'}} with ${{biggest.count || 0}} items.\n• Most active entity: ${{topEntity.label || '-'}} (${{topEntity.count || 0}} records)${{topLocation.label ? ' · most frequent location/unit: ' + topLocation.label + ' (' + (topLocation.count || 0) + ')' : ''}} — focus review capacity where the volume is.\n• If item keywords cluster around a buyer/product family, prioritize those folders for review and WhatsApp detail follow-up.\n• If Deadline repairs > 0, repair missing DTEND before calendar/export decisions.\n• If Notify backlog grows, verify WAHA destinations/settings before running more scans.\nRecent parsed items:\n${{sampleLines || '  - no item rows parsed yet'}}`;
}}
function initCollapsibleSections() {{
  // Every card except the live-progress header starts COLLAPSED so the monitor
  // opens compact; the operator expands only the panels they need (matches the
  // Tk monitor's default-hidden sections).
  document.querySelectorAll('.card').forEach((card, idx) => {{
    const heading = card.querySelector('h1, h2');
    if (idx === 0 || card.dataset.tab) return;
    if (!heading || heading.querySelector('.section-toggle')) return;
    card.classList.add('collapsed');
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'section-toggle';
    btn.textContent = 'Show';
    btn.title = 'Hide/show this monitor section without stopping the run.';
    btn.addEventListener('click', () => {{
      card.classList.toggle('collapsed');
      btn.textContent = card.classList.contains('collapsed') ? 'Show' : 'Hide';
    }});
    heading.appendChild(btn);
  }});
}}

function renderQueue(data) {{
  const q = data.queue || {{}};
  document.getElementById('queue-summary').textContent =
    `Collector request: ${{q.collector_state || 'none'}} (since ${{q.collector_since || '-'}}) · ` +
    `Update + Monitor: ${{q.update_state || 'none'}} (since ${{q.update_since || '-'}})`;
  document.getElementById('queue-log').textContent =
    'Recent collector queue log:\n' + (q.request_log || '(missing)') +
    '\nRecent Update + Monitor queue log:\n' + (q.update_log || '(missing)');
}}

function renderRecordSummary(data) {{
  const p = data.progress || {{}};
  const pending = document.getElementById('records-pending');
  const completed = document.getElementById('records-completed');
  if (pending) pending.textContent = `Records Pendings\n${{p.RECORDS_PENDING ?? '-'}} waiting for detail/download\nFound: ${{p.RECORDS_FOUND ?? '-'}} · New: ${{p.RECORDS_NEW ?? '-'}} · Existing: ${{p.RECORDS_EXISTING ?? '-'}}`;
  refreshDbReview('records-db-summary');
  fetch('/api/db-stats', {{cache: 'no-store'}})
    .then(r => r.json())
    .then(s => {{
      const endDates = (s.completed_recent || []).slice(0, 3).map(r => `${{r.numero}} ends ${{(r.finish_date_guess || 'no date').replace('T', ' ').replace('_', ' ')}}`).join('; ') || 'No completed end dates yet';
      if (completed) completed.textContent = `Records Completed\nSaved/skipped: ${{p.RECORDS_SAVED ?? '-'}}\nFailures needing review: ${{p.RECORDS_FAILED ?? '-'}}\nOpportunity ends: ${{endDates}}`;
    }})
    .catch(() => {{ if (completed) completed.textContent = `Records Completed\nSaved/skipped: ${{p.RECORDS_SAVED ?? '-'}}\nFailures needing review: ${{p.RECORDS_FAILED ?? '-'}}`; }});
}}

async function refreshDbReview(targetId = 'db-review') {{
  const node = document.getElementById(targetId || 'db-review');
  try {{
    const response = await fetch('/api/db-stats', {{cache: 'no-store'}});
    const s = await response.json();
    if (!s.db_exists) {{ node.textContent = 'No database yet (data/panamacompra_archive.db). Run the collector first.'; return; }}
    const groups = (s.groups || []).map(g => `${{g.grupo}}: ${{g.count}}`).join('   ·   ') || '—';
    const statuses = (s.status_breakdown || []).map(r => `${{r.status}}: ${{r.count}}`).join('   ·   ') || '—';
    const columns = (s.columns || []).map(c => `${{c.name}}[${{c.type || 'TEXT'}}]=${{c.nonempty}}`).join('   ·   ') || '—';
    const recent = (s.recent || []).map(r => `  • ${{r.numero}} [${{r.detail_status || 'unknown'}}] — ${{(r.descripcion || '').slice(0, 120)}}`).join('\n') || '  • —';
    node.textContent =
      `Total records: ${{s.total}}\n` +
      `Records Completed (saved): ${{s.saved}}   ·   Records Pendings: ${{s.pending}}   ·   Failed: ${{s.failed}}\n` +
      `Detail JSON on record: ${{s.with_detail_json}}   ·   Notified (WAHA): ${{s.notified}}\n` +
      `Awaiting WhatsApp index alert: ${{s.notify_backlog}}   ·   Awaiting item-details WhatsApp: ${{s.detail_notify_backlog}}   ·   Needs deadline repair: ${{s.needs_deadline}}\n` +
      `Detail statuses: ${{statuses}}\n` +
      `By group: ${{groups}}\n` +
      `DB elements / columns with data: ${{columns}}\n` +
      `Most recent records:\n${{recent}}`;
  }} catch (err) {{ node.textContent = 'Could not read database snapshot: ' + err; }}
}}

const RESET_CONFIRM = {{
  'wipe-db': 'Delete the tracking database (data/panamacompra_archive.db) and index CSV?\\n\\nDownloaded record folders are kept and re-linked on the next run.',
  'wipe-all': 'Delete the database AND every downloaded record folder and calendar?\\n\\nThis is irreversible — the local archive is lost. The test zone is kept.',
}};
async function runReset(action) {{
  const status = document.getElementById('reset-status');
  if (RESET_CONFIRM[action] && !window.confirm(RESET_CONFIRM[action])) {{ status.textContent = action + ': cancelled.'; return; }}
  try {{
    const response = await fetch('/api/reset', {{method: 'POST', headers: {{'Content-Type': 'application/x-www-form-urlencoded'}}, body: 'action=' + encodeURIComponent(action)}});
    status.textContent = (await response.text()).trim();
  }} catch (err) {{ status.textContent = 'Reset failed: ' + err; }}
  setTimeout(refreshDbReview, 1500);
}}
async function poll() {{
  try {{
    const response = await fetch('/api/status', {{cache: 'no-store'}});
    const data = await response.json();
    render(data);
    timer = setTimeout(poll, Math.max(3, Number(data.refresh_seconds || {REFRESH_SECONDS})) * 1000);
  }} catch (err) {{
    document.getElementById('message').textContent = 'Monitor connection error: ' + err;
    timer = setTimeout(poll, {IDLE_REFRESH_SECONDS} * 1000);
  }}
}}
window.addEventListener('beforeunload', () => {{ if (timer) clearTimeout(timer); }});
renderActionZones();
document.getElementById('record-index').addEventListener('change', renderRecordDetail);
document.getElementById('record-status').addEventListener('change', applyRecordFilter);
document.getElementById('record-detail-status').addEventListener('change', applyRecordFilter);
document.getElementById('record-order').addEventListener('change', applyRecordFilter);
document.getElementById('record-order-field').addEventListener('change', applyRecordFilter);
['record-mindate', 'record-maxdate', 'record-start-mindate', 'record-start-maxdate', 'record-downloaded-mindate', 'record-downloaded-maxdate'].forEach(id => {{
  const el = document.getElementById(id);
  if (el) {{ el.addEventListener('change', applyRecordFilter); el.addEventListener('input', applyRecordFilter); }}
}});
showTab('operations');
initCollapsibleSections();
refreshRecordIndex();
loadTemplates();
loadCalendar();
loadWahaFormat();
loadWahaFilters();
refreshDbReview();
refreshDecisionDashboard();
poll();
</script>
</body>
</html>"""


class MonitorHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: object) -> None:
        return

    def send_text(self, status: int, body: str, content_type: str) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", "0") or "0")
        form = parse_qs(self.rfile.read(length).decode("utf-8", errors="replace"))
        if path == "/api/request-run":
            raw_detail = form.get("detail_limit", ["99"])[0].strip()
            raw_index = form.get("index_limit", ["0"])[0].strip()
            detail_limit = raw_detail if raw_detail.isdigit() and int(raw_detail) > 0 else "99"
            index_limit = raw_index if raw_index.isdigit() and int(raw_index) >= 0 else "0"
            index_limit_text = "all" if index_limit == "0" else index_limit
            mode = form.get("mode", ["restart"])[0].strip().lower()
            if mode == "test":
                subprocess.Popen([str(BASE_DIR / "src/pipeline/070-test-zone.py"), "--limit", detail_limit, "--apply"], cwd=BASE_DIR, env=monitor_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.send_text(202, f"Test-zone run requested with detail limit {detail_limit}.\n", "text/plain; charset=utf-8")
                return
            if mode == "manual":
                subprocess.Popen([str(BASE_DIR / "src/pipeline/110b-run-now.sh"), detail_limit, index_limit, "MANUAL"], cwd=BASE_DIR, env=monitor_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.send_text(202, f"Manual run started with index page cap {index_limit_text}, detail limit {detail_limit}.\n", "text/plain; charset=utf-8")
                return
            subprocess.Popen([str(BASE_DIR / "src/pipeline/110a-request-run.sh"), detail_limit, "RESTART", index_limit], cwd=BASE_DIR, env=monitor_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.send_text(202, f"Restart-pending run requested with index page cap {index_limit_text}, detail limit {detail_limit}.\n", "text/plain; charset=utf-8")
            return
        if path == "/api/manual-action":
            label = form.get("label", [""])[0].strip()
            for action in MANUAL_ACTIONS:
                if action.label == label:
                    run_manual_action(action)
                    self.send_text(202, f"Started: {action.label}. Output: data/logs/manual_actions.log\n", "text/plain; charset=utf-8")
                    return
            self.send_text(404, "unknown manual action\n", "text/plain; charset=utf-8")
            return
        if path == "/api/import-calendars":
            action = next(action for action in MANUAL_ACTIONS if action.label == "Import generated calendars")
            run_manual_action(action)
            self.send_text(202, "Calendar import started. Output: data/logs/manual_actions.log\n", "text/plain; charset=utf-8")
            return
        if path == "/api/open-record-folder":
            numero = form.get("numero", [""])[0].strip()
            for record in load_record_index():
                if record["numero"] == numero:
                    folder = record["record_folder"]
                    if not folder or not Path(folder).exists():
                        self.send_text(404, f"Record folder not found on disk for {numero}.\n", "text/plain; charset=utf-8")
                        return
                    opener = os.environ.get("PC_OPEN_FOLDER_COMMAND", "xdg-open")
                    subprocess.Popen([opener, folder], cwd=BASE_DIR, env=monitor_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    self.send_text(202, f"Opened record folder for {numero}.\n", "text/plain; charset=utf-8")
                    return
            self.send_text(404, f"Unknown record: {numero}\n", "text/plain; charset=utf-8")
            return
        if path == "/api/monitor-setting":
            key = form.get("key", [""])[0].strip()
            value = form.get("value", [""])[0].strip()
            try:
                save_monitor_setting(key, value)
            except ValueError as exc:
                self.send_text(400, f"{exc}\n", "text/plain; charset=utf-8")
                return
            self.send_text(200, f"Setting saved: {key}={load_monitor_settings().get(key, '')}\n", "text/plain; charset=utf-8")
            return
        if path == "/api/selected-record-action":
            action_name = form.get("action", [""])[0].strip().lower()
            numeros = [n.strip() for n in form.get("numero", []) if n.strip()]
            known = {record["numero"] for record in load_record_index(limit=2000)}
            selected = [n for n in numeros if n in known]
            if not selected:
                self.send_text(400, "Select one or more known records first.\n", "text/plain; charset=utf-8")
                return
            if action_name == "notify":
                cmd = [str(BASE_DIR / "src/pipeline/020-notify-whatsapp.py"), "--force"]
                for numero in selected:
                    cmd.extend(["--record", numero])
                subprocess.Popen(cmd, cwd=BASE_DIR, env=monitor_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.send_text(202, f"WhatsApp notification requested for {len(selected)} selected record(s).\n", "text/plain; charset=utf-8")
                return
            if action_name == "templates":
                cmd = [str(BASE_DIR / "src/tools/020-record-templates.py"), "apply", "--apply"]
                for numero in selected:
                    cmd.extend(["--numero", numero])
                subprocess.Popen(cmd, cwd=BASE_DIR, env=monitor_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.send_text(202, f"Work templates requested for {len(selected)} selected record(s) (existing files kept).\n", "text/plain; charset=utf-8")
                return
            if action_name == "calendar":
                cmd = [str(BASE_DIR / "src/tools/060-import-selected-calendars.py"), "--open", *selected]
                subprocess.Popen(cmd, cwd=BASE_DIR, env=monitor_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.send_text(202, f"Calendar import requested for {len(selected)} selected record(s).\n", "text/plain; charset=utf-8")
                return
            self.send_text(400, "Unknown selected-record action.\n", "text/plain; charset=utf-8")
            return
        if path == "/api/reset":
            action = form.get("action", [""])[0].strip()
            if action not in RESET_ACTIONS:
                self.send_text(400, "Unknown reset action.\n", "text/plain; charset=utf-8")
                return
            command = [str(BASE_DIR / "src/tools/110-reset.py"), action]
            if RESET_ACTIONS[action]:  # destructive -> confirmed in the browser
                command.append("--yes")
            MANUAL_ACTION_LOG.parent.mkdir(parents=True, exist_ok=True)
            with MANUAL_ACTION_LOG.open("a", encoding="utf-8") as log_file:
                log_file.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} | Reset / {action} =====\n")
                subprocess.Popen(command, cwd=BASE_DIR, env=monitor_env(), stdout=log_file, stderr=subprocess.STDOUT)
            self.send_text(202, f"Started reset '{action}'. See data/logs/manual_actions.log; refresh the DB snapshot to verify.\n", "text/plain; charset=utf-8")
            return
        if path in {"/api/waha-destination", "/api/waha-message"}:
            WAHA_CHAT_ID_PATH.parent.mkdir(parents=True, exist_ok=True)
            chat_id = form.get("chat_id", form.get("message", [""]))[0].strip()
            WAHA_CHAT_ID_PATH.write_text(chat_id + "\n", encoding="utf-8")
            # Optional per-purpose destinations (blank clears → default is used).
            for field_name, purpose_path in (
                ("chat_id_index", WAHA_CHAT_ID_INDEX_PATH),
                ("chat_id_details", WAHA_CHAT_ID_DETAILS_PATH),
                ("chat_id_status", WAHA_CHAT_ID_STATUS_PATH),
            ):
                if field_name in form:
                    purpose_path.write_text(form.get(field_name, [""])[0].strip() + "\n", encoding="utf-8")
            self.send_text(200, "WhatsApp destination(s) saved.\n", "text/plain; charset=utf-8")
            return
        if path == "/api/waha-filters":
            for name, filter_path in FILTER_FILES.items():
                if name in form:
                    rules = [k.strip() for k in form.get(name, [""])[0].replace("\n", ",").split(",") if k.strip()]
                    filter_path.parent.mkdir(parents=True, exist_ok=True)
                    filter_path.write_text(("\n".join(rules) + "\n") if rules else "", encoding="utf-8")
            self.send_text(200, "WhatsApp filters saved.\n", "text/plain; charset=utf-8")
            return
        if path == "/api/waha-format":
            kind = form.get("kind", ["index"])[0].strip().lower()
            if kind not in notify_formats.FORMAT_KINDS:
                self.send_text(400, "Unknown format kind.\n", "text/plain; charset=utf-8")
                return
            action = form.get("action", ["save"])[0].strip().lower()
            template = form.get("template", [""])[0]
            if action == "preview":
                self.send_text(200, notify_formats.render_format(kind, template if template.strip() else None) + "\n", "text/plain; charset=utf-8")
                return
            if action == "reset":
                notify_formats.format_path(kind).unlink(missing_ok=True)
                self.send_text(200, f"{kind} format reset to the built-in layout.\n", "text/plain; charset=utf-8")
                return
            if not template.strip():
                self.send_text(400, "Empty template; use action=reset to restore the default.\n", "text/plain; charset=utf-8")
                return
            path_out = notify_formats.format_path(kind)
            path_out.parent.mkdir(parents=True, exist_ok=True)
            path_out.write_text(template.strip("\n") + "\n", encoding="utf-8")
            self.send_text(200, f"Custom {kind} format saved.\n", "text/plain; charset=utf-8")
            return
        if path == "/api/templates-select":
            src = record_templates.source_dir()
            available = set(record_templates.source_files(src))
            requested = [f.strip() for f in form.get("file", []) if f.strip()]
            selection = [f for f in requested if f in available]
            record_templates.save_selection(selection)
            self.send_text(200, f"Template selection saved: {len(selection)} file(s).\n", "text/plain; charset=utf-8")
            return
        if path == "/api/test-whatsapp":
            # One WAHA test message with the saved settings, so the WhatsApp
            # pipeline can be verified without waiting for a collector run.
            subprocess.Popen(
                [str(BASE_DIR / "src/notify/010-waha-client.py"), "--event", "info", "--status", "TEST",
                 "--message", "Prueba de notificación desde el monitor web PanamaCompra."],
                cwd=BASE_DIR, env=monitor_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            self.send_text(202, "WhatsApp test message requested (requires WAHA enabled + chat id).\n", "text/plain; charset=utf-8")
            return
        self.send_text(404, "not found\n", "text/plain; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        path = urlparse(self.path).path
        if path == "/health":
            self.send_text(200, "ok\n", "text/plain; charset=utf-8")
            return
        if path == "/api/status":
            self.send_text(200, json.dumps(status_payload(), ensure_ascii=False, indent=2), "application/json; charset=utf-8")
            return
        if path == "/api/waha-filters":
            payload = {name: read_filter_rules(name) for name in FILTER_FILES}
            self.send_text(200, json.dumps(payload, ensure_ascii=False), "application/json; charset=utf-8")
            return
        if path == "/api/waha-format":
            params = parse_qs(urlparse(self.path).query)
            kind = (params.get("kind", ["index"])[0] or "index").lower()
            if kind not in notify_formats.FORMAT_KINDS:
                kind = "index"
            custom = notify_formats.load_custom_format(kind)
            payload = {
                "kind": kind,
                "custom": bool(custom),
                "template": custom or notify_formats.DEFAULT_FORMATS[kind],
                "placeholders": notify_formats.PLACEHOLDERS,
                "preview": notify_formats.render_format(kind),
            }
            self.send_text(200, json.dumps(payload, ensure_ascii=False), "application/json; charset=utf-8")
            return
        if path == "/api/calendar":
            params = parse_qs(urlparse(self.path).query)
            view = (params.get("view", ["month"])[0] or "month").lower()
            if view not in opportunity_calendar.VIEWS:
                view = "month"
            field = (params.get("field", ["end"])[0] or "end").lower()
            if field not in opportunity_calendar.FIELDS:
                field = "end"
            try:
                anchor = opportunity_calendar.parse_anchor(params.get("date", [""])[0])
            except SystemExit:
                anchor = opportunity_calendar.parse_anchor("")
            try:
                shift = int(params.get("shift", ["0"])[0])
            except (TypeError, ValueError):
                shift = 0
            if shift:
                anchor = opportunity_calendar.shift_anchor(view, anchor, shift)
            try:
                conn = sqlite3.connect(f"file:{ARCHIVE_DB}?mode=ro", uri=True, timeout=2)
                conn.row_factory = sqlite3.Row
                try:
                    text = opportunity_calendar.render_view(conn, view, anchor, field)
                finally:
                    conn.close()
            except sqlite3.Error:
                text = "(archive database not available yet — run a collection first)"
            payload = {"view": view, "field": field, "anchor": anchor.isoformat(), "text": text}
            self.send_text(200, json.dumps(payload, ensure_ascii=False), "application/json; charset=utf-8")
            return
        if path == "/api/templates":
            src = record_templates.source_dir()
            payload = {
                "source": str(src),
                "files": record_templates.source_files(src),
                "selected": record_templates.load_selection(),
            }
            self.send_text(200, json.dumps(payload, ensure_ascii=False), "application/json; charset=utf-8")
            return
        if path == "/api/record-index":
            self.send_text(200, json.dumps(load_record_index(), ensure_ascii=False), "application/json; charset=utf-8")
            return
        if path == "/api/db-stats":
            self.send_text(200, json.dumps(db_review_stats(), ensure_ascii=False), "application/json; charset=utf-8")
            return
        if path in ("/", "/index.html"):
            self.send_text(200, HTML, "text/html; charset=utf-8")
            return
        self.send_text(404, "not found\n", "text/plain; charset=utf-8")


def main() -> None:
    pc_common.LOG_DIR.mkdir(parents=True, exist_ok=True)
    pc_common.QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((HOST, PORT), MonitorHandler)
    print(f"PanamaCompra web monitor: http://{HOST}:{PORT}/", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
