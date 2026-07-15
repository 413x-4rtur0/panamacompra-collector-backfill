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
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import common as pc_common

# Shared KPI/data engine (audit Phase 4): the archive aggregates and detail
# helpers live in monitor_common.py so this monitor, the Tk monitor and
# `pcc kpi` always report the same numbers.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from monitor_common import (  # noqa: E402
    db_review_stats,
    finish_stamp_from_detail_json,
    finish_stamp_from_folder,
    load_detail_items_for_kpi,
    load_detail_payload_for_kpi,
    load_record_index,
    monitor_connectivity_status,
    read_last_summary,
    setting,
    stats_to_csv,
    summarize_items_for_kpi,
    waha_fetch_all,
    waha_filter_matches,
)

BASE_DIR = pc_common.APP_ROOT
PROGRESS_FILE = pc_common.PROGRESS_PATH
WORKER_LOG = pc_common.LOG_DIR / "run_all_worker.log"
CURRENT_LOG = pc_common.LOG_DIR / "run_all_current.log"
REQUEST_FLAG = pc_common.QUEUE_DIR / "run_all_requested.flag"
UPDATE_QUEUE_FLAG = pc_common.QUEUE_DIR / "update_monitor_requested.flag"
UPDATE_IN_PROGRESS_FLAG = pc_common.QUEUE_DIR / "update_monitor_in_progress.flag"
REQUEST_LOG = pc_common.LOG_DIR / "run_all_requests.log"
UPDATE_QUEUE_LOG = pc_common.LOG_DIR / "update_monitor_queue.log"
CHANGEDETECTION_BROWSER_STEPS_JS = pc_common.APP_ROOT / "config" / "changedetection-browser-steps.js"
# Work-templates helper imported as a module so the web monitor lists/saves the
# same source folder and selection the CLI and native monitor use.
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

WAHA_CHAT_ID_PATH = pc_common.DATA_CONFIG_DIR / "waha_chat_id.txt"
# Optional per-purpose destinations; each falls back to the default chat id.
WAHA_CHAT_ID_INDEX_PATH = pc_common.DATA_CONFIG_DIR / "waha_chat_id_index.txt"
WAHA_CHAT_ID_DETAILS_PATH = pc_common.DATA_CONFIG_DIR / "waha_chat_id_details.txt"
WAHA_CHAT_ID_STATUS_PATH = pc_common.DATA_CONFIG_DIR / "waha_chat_id_status.txt"
WAHA_CHAT_ID_OPEN_NOW_PATH = pc_common.DATA_CONFIG_DIR / "waha_chat_id_open_now.txt"
WAHA_CHAT_ID_SYSTEM_PATH = pc_common.DATA_CONFIG_DIR / "waha_chat_id_system.txt"
WAHA_CHAT_ID_SUMMARY_PATH = pc_common.DATA_CONFIG_DIR / "waha_chat_id_summary.txt"
WAHA_CLIENTS_PATH = pc_common.DATA_CONFIG_DIR / "waha_clients.json"


def read_chat_file(path) -> str:
    return path.read_text(encoding="utf-8", errors="replace").strip() if path.exists() else ""


FILTER_FILES = {
    "global": pc_common.DATA_CONFIG_DIR / "waha_keywords.txt",
    "index": pc_common.DATA_CONFIG_DIR / "waha_keywords_index.txt",
    "details": pc_common.DATA_CONFIG_DIR / "waha_keywords_details.txt",
    "status": pc_common.DATA_CONFIG_DIR / "waha_keywords_status.txt",
    "open_now": pc_common.DATA_CONFIG_DIR / "waha_keywords_open_now.txt",
}


def read_filter_rules(name: str) -> str:
    path = FILTER_FILES[name]
    if not path.exists():
        return ""
    rules = [k.strip() for k in path.read_text(encoding="utf-8", errors="replace").splitlines() if k.strip() and not k.startswith("#")]
    return ", ".join(rules)


def read_waha_clients_text() -> str:
    if not WAHA_CLIENTS_PATH.exists():
        return "[]"
    return WAHA_CLIENTS_PATH.read_text(encoding="utf-8", errors="replace").strip() or "[]"


WAHA_CLIENTS_LOCK = threading.Lock()


def _normalize_client_profile(item: dict) -> dict | None:
    """Shared normalization for one client profile entry.

    Used by both save_waha_clients_text() (operator-edited textarea, a full
    list replace) and upsert_client_profile() (self-service, one profile at a
    time from the Android app), so the two paths can never drift out of sync
    on which fields exist or how they're cleaned up. Returns None for an
    entry with no chat_id (dropped, same as before).
    """
    if not isinstance(item, dict):
        raise ValueError("each client profile must be an object")
    chat_id = str(item.get("chat_id") or "").strip()
    if not chat_id:
        return None
    purposes = item.get("purposes") or ["index", "details", "status"]
    if isinstance(purposes, str):
        purposes = [p.strip() for p in purposes.split(",") if p.strip()]
    if not isinstance(purposes, list):
        raise ValueError("client purposes must be a list or comma-separated string")
    return {
        "name": str(item.get("name") or chat_id).strip(),
        "chat_id": chat_id,
        "purposes": [str(p).strip().lower() for p in purposes if str(p).strip()] or ["index", "details", "status"],
        "filters": str(item.get("filters") or "").strip(),
        "enabled": bool(item.get("enabled", True)),
        # Code a client types into the Android app at setup (unrelated to
        # chat_id) so /api/client-notifications can look up which chat_id's
        # app_notifications rows belong to them. Superseded by firebase_uid
        # for anyone who signs up through the app, but still supported for
        # profiles the operator created manually.
        "app_code": str(item.get("app_code") or "").strip(),
        # Self-service profile fields (Android app login/settings screens).
        "firebase_uid": str(item.get("firebase_uid") or "").strip(),
        "email": str(item.get("email") or "").strip(),
        "phone": str(item.get("phone") or "").strip(),
        "profession": str(item.get("profession") or "").strip(),
        "location": str(item.get("location") or "").strip(),
        "institution": str(item.get("institution") or "").strip(),
        "calendar_visible": bool(item.get("calendar_visible", True)),
    }


def save_waha_clients_text(text: str) -> None:
    try:
        parsed = json.loads(text or "[]")
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid client JSON: {exc}") from exc
    if not isinstance(parsed, list):
        raise ValueError("client profiles must be a JSON list")
    normalized = [p for p in (_normalize_client_profile(item) for item in parsed) if p is not None]
    with WAHA_CLIENTS_LOCK:
        WAHA_CLIENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        WAHA_CLIENTS_PATH.write_text(json.dumps(normalized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def find_client_profile(*, firebase_uid: str = "", app_code: str = "") -> dict | None:
    """Look up one client profile by firebase_uid (preferred) or legacy app_code."""
    try:
        profiles = json.loads(read_waha_clients_text())
    except json.JSONDecodeError:
        return None
    if not isinstance(profiles, list):
        return None
    for item in profiles:
        if not isinstance(item, dict) or not item.get("enabled", True):
            continue
        if firebase_uid and str(item.get("firebase_uid") or "").strip() == firebase_uid:
            return item
        if app_code and str(item.get("app_code") or "").strip().lower() == app_code.lower():
            return item
    return None


def upsert_client_profile(update: dict) -> dict:
    """Self-service create/update for one client profile, keyed by firebase_uid.

    Concurrency-safe (WAHA_CLIENTS_LOCK guards the read-modify-write) since
    this can be called by any signed-in client's app at any time.
    """
    uid = str(update.get("firebase_uid") or "").strip()
    if not uid:
        raise ValueError("firebase_uid is required")
    with WAHA_CLIENTS_LOCK:
        try:
            existing = json.loads(read_waha_clients_text())
        except json.JSONDecodeError:
            existing = []
        if not isinstance(existing, list):
            existing = []
        match_index = next(
            (i for i, item in enumerate(existing)
             if isinstance(item, dict) and str(item.get("firebase_uid") or "").strip() == uid),
            None,
        )
        merged = dict(existing[match_index]) if match_index is not None else {}
        merged.update(update)
        merged["firebase_uid"] = uid
        if not str(merged.get("chat_id") or "").strip():
            # App-only signup, no WhatsApp group linked yet. A stable
            # non-WhatsApp chat_id still lets /api/client-notifications and
            # app_notifications scope rows to this client — see the
            # "app:" handling added to notify_whatsapp.send_text().
            merged["chat_id"] = f"app:{uid}"
        normalized = _normalize_client_profile(merged)
        if normalized is None:
            raise ValueError("could not normalize client profile")
        if match_index is not None:
            existing[match_index] = normalized
        else:
            existing.append(normalized)
        WAHA_CLIENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        WAHA_CLIENTS_PATH.write_text(json.dumps(existing, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return normalized


def client_filter_fn(profile: dict):
    """Row predicate for a client's own filters, for opportunity_calendar.fetch_events().

    Reuses notify_whatsapp's own parse_filter_rules()/evaluate_filter() so the
    client-scoped calendar always agrees with what that client's WhatsApp
    group / app notifications actually receive — one filter implementation,
    not two that could drift apart.
    """
    includes, excludes = notify_formats.parse_filter_rules(profile.get("filters", ""))

    def _matches(row) -> bool:
        haystack = notify_formats.row_filter_haystack(row, {})
        return notify_formats.evaluate_filter(haystack, includes, excludes) is not None

    return _matches


def calendar_keyword_filter_fn(query: str):
    """Row predicate for the operator-facing calendar's free-text filter box.

    Case- and accent-insensitive partial match (e.g. "construccion" matches
    "Construcción"), against numero/descripcion/entidad/dependencia/modalidad/
    grupo — only rows that partially or fully match stay visible. Returns
    None when the query is blank so callers can skip filtering entirely.
    """
    needle = pc_common.strip_accents(query).strip().lower()
    if not needle:
        return None

    def _matches(row) -> bool:
        haystack = pc_common.strip_accents(" ".join(
            str(row[col]) for col in (
                "numero", "descripcion", "short_description", "estado",
                "grupo", "entidad", "dependencia", "modalidad",
            )
            if row[col]
        )).lower()
        return needle in haystack

    return _matches


def calendar_grid_payload(view: str, field: str, date_param: str, *, filter_fn=None, shift: int = 0) -> dict:
    """Shared JSON payload builder behind /api/calendar-grid and
    /api/client-calendar-grid (the client version passes filter_fn so it only
    sees opportunities matching its own profile's filters). ``shift`` moves
    the anchor by that many view-units (prev/next), server-side, so callers
    with no separate text endpoint to lean on (the client calendar page) can
    still page through months/weeks/etc. in one round trip."""
    if view not in opportunity_calendar.VIEWS:
        view = "month"
    if field not in opportunity_calendar.FIELDS:
        field = "end"
    try:
        anchor = opportunity_calendar.parse_anchor(date_param)
    except SystemExit:
        anchor = opportunity_calendar.parse_anchor("")
    if shift:
        anchor = opportunity_calendar.shift_anchor(view, anchor, shift)
    start, end = opportunity_calendar.view_range(view, anchor)
    grouped_events: dict[str, list[dict[str, str]]] = {}
    month_counts: dict[str, int] = {}
    try:
        conn = sqlite3.connect(f"file:{ARCHIVE_DB}?mode=ro", uri=True, timeout=2)
        conn.row_factory = sqlite3.Row
        try:
            events = opportunity_calendar.fetch_events(conn, field, start, end, filter_fn=filter_fn)
            for day_key, rows in events.items():
                grouped_events[day_key] = []
                month_counts[day_key[:7]] = month_counts.get(day_key[:7], 0) + len(rows)
                for row in rows:
                    value = opportunity_calendar.normalize_value(row["event_date"])
                    desc = (row["descripcion"] or row["short_description"] or "").strip()
                    grouped_events[day_key].append({
                        "numero": str(row["numero"] or ""),
                        "description": desc[:96],
                        "status": str(row["estado"] or row["grupo"] or ""),
                        "date": value,
                        "clock": value[11:16] if len(value) >= 16 else "--:--",
                    })
        finally:
            conn.close()
    except sqlite3.Error:
        grouped_events = {}
        month_counts = {}
    days = [
        {
            "iso": (start + timedelta(days=offset)).isoformat(),
            "label": str((start + timedelta(days=offset)).day),
            "today": time.strftime("%Y-%m-%d"),
        }
        for offset in range((end - start).days + 1)
    ]
    months = [
        {"value": f"{anchor.year}-{month:02d}", "label": date(anchor.year, month, 1).strftime("%b %Y")}
        for month in range(1, 13)
    ] if view == "year" else []
    return {
        "view": view,
        "field": field,
        "anchor": anchor.isoformat(),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "label": opportunity_calendar.FIELDS[field][1],
        "first_weekday": start.weekday(),
        "today": time.strftime("%Y-%m-%d"),
        "days": days,
        "events": grouped_events,
        "total": sum(len(rows) for rows in grouped_events.values()),
        "months": months,
        "month_counts": month_counts,
    }


_WAHA_DIRECTORY_LOCK = threading.Lock()
_WAHA_DIRECTORY_CACHE: dict[str, object] = {"loaded_at": 0.0, "matches": []}


def _waha_directory(*, refresh: bool = False) -> tuple[list[dict], bool]:
    """Return a complete short-lived directory cache shared by web searches."""
    try:
        ttl = max(15.0, float(setting("PC_WAHA_DIRECTORY_CACHE_SECONDS", "120") or 120))
    except ValueError:
        ttl = 120.0
    now = time.monotonic()
    with _WAHA_DIRECTORY_LOCK:
        loaded_at = float(_WAHA_DIRECTORY_CACHE.get("loaded_at") or 0.0)
        cached = _WAHA_DIRECTORY_CACHE.get("matches")
        if not refresh and loaded_at and now - loaded_at < ttl and isinstance(cached, list):
            return list(cached), True
        matches = waha_fetch_all(
            "", include_chats=True, raise_on_connection_error=True, limit=None
        )
        _WAHA_DIRECTORY_CACHE["loaded_at"] = time.monotonic()
        _WAHA_DIRECTORY_CACHE["matches"] = list(matches)
        return matches, False


def waha_search(query: str, *, refresh: bool = False) -> dict:
    """Search WAHA sessions by full/partial display name or chat ID.

    Returns {'sessions': [...], 'matches': [...]} where each match carries the
    session it was found in, the chat id to paste into a client profile, the
    display name and whether it is a group or contact. Empty query lists every
    group (the useful default for building client profiles)."""
    try:
        directory, cached = _waha_directory(refresh=refresh)
    except Exception as exc:  # noqa: BLE001 - return a useful monitor diagnosis
        health = monitor_connectivity_status()
        return {"sessions": [], "matches": [], "health": health,
                "error": f"WAHA directory unavailable: {exc}"}
    matches = waha_filter_matches(directory, query, limit=100)
    public_matches = [
        {key: match.get(key, "") for key in ("session", "id", "name", "kind")}
        for match in matches
    ]
    sessions: list[dict] = []
    seen = set()
    for m in directory:
        sname = m.get("session", "")
        if sname not in seen:
            seen.add(sname)
            sessions.append({"name": sname, "status": "WORKING"})
    return {"sessions": sessions, "matches": public_matches, "directory_count": len(directory),
            "cached": cached}
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
    "PC_AUTORUN_SOURCE": "changedetection",
    "PC_CRON_DETAIL_LIMIT": "0",
    "PC_CRON_INDEX_LIMIT": "0",
    "PC_CRON_DAYS": "daily",
    "PC_CRON_CUSTOM_DAYS": "",
    "PC_CRON_START_TIME": "08:00",
    "PC_CRON_END_TIME": "18:00",
    "PC_CRON_INTERVAL_MINUTES": "30",
    "PC_MONITOR_HOST": "127.0.0.1",
    "PC_MONITOR_DEADLINE_SOON_DAYS": "7",
    "PC_WEBHOOK_INDEX_LIMIT": "0",
    "PC_WEBHOOK_DETAIL_LIMIT": "0",
    "PC_NOTIFY_WITHIN_DAYS": "",
    "PC_WAHA_RETRIES": "2",
    "PC_WAHA_SEND_DELAY_SECONDS": "3",
    "PC_NOTIFY_INDEX_DIGEST_THRESHOLD": "10",
    "PC_NOTIFY_IDLE_EVERY_HOURS": "6",
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
    # Container-side settings applied by src/50_tools/010-docker-stack.sh on the next
    # stack restart (non-empty values win over .env).
    "CHANGEDETECTION_BASE_URL": "http://localhost:5000",
    "PC_TEMPLATES_SRC_DIR": "",
    "WAHA_PORT": "3000",
    "WAHA_API_KEY": "",
    "PC_WEBHOOK_PORT": "8765",
    "PC_WEBHOOK_PUBLIC_HOST": "host.docker.internal",
    # WAHA dashboard login (container side). Setup generates a RANDOM password
    # into .env and data/config/integration-access.txt so review access always
    # works; edit here to change it. Blank keeps the generated .env value.
    "WAHA_DASHBOARD_USERNAME": "admin",
    "WAHA_DASHBOARD_PASSWORD": "",
}
BOOLEAN_SETTING_DEFAULTS = {
    "PC_NOTIFY_WHATSAPP": "1",
    "PC_NOTIFY_DETAILS": "1",
    "PC_NOTIFY_DETAILS_INLINE": "1",
    "PC_INDEX_FROM_SNAPSHOT": "1",
    "PC_CALENDAR_AUTO_IMPORT": "0",
    "PC_WAHA_ENABLED": "0",
    "PC_NOTIFY_SKIP_EXPIRED": "0",
    "PC_TEST_ZONE_AUTORUN": "0",
    "PC_RUN_UPDATE_BEFORE_RUN": "1",
    "PC_WEBHOOK_AUTO_RUN": "1",
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
    ManualAction("Runners", "Run full collector", ("./src/20_pipeline/110a-request-run.sh", "0", "RESTART", "0"), "Queues a manual restart run for all available index pages and opens/reuses this monitor."),
    ManualAction("Runners", "Run collector now", ("./src/20_pipeline/110b-run-now.sh", "0", "0", "MANUAL"), "Starts the run-all worker immediately for all available index pages and unlimited detail pages."),
    ManualAction("Runners", "Stop active run", ("./src/20_pipeline/120b-stop-collectors.sh",), "Stops the active collection (worker/index/detail/test/calendar) and prevents auto-resume. The monitor, next-run timer and webhook stay running."),
    ManualAction("Runners", "STOP all runners", ("./src/20_pipeline/120a-stop-everything.sh",), "DANGER: stops ALL processes — workers, test zone, calendar builder, monitors, webhook listener and updaters (this monitor closes too)."),
    ManualAction("Runners", "START all infrastructure", ("./src/20_pipeline/120c-start-everything.sh",), "Counterpart to STOP all runners: brings Docker integrations (changedetection/WAHA/sockpuppetbrowser) and the webhook listener back up, and opens the monitor. Does not queue a collector run by itself."),
    ManualAction("Runners", "Pause for development", ("./src/20_pipeline/121-dev-mode.sh", "pause"), "Stops any active run and pauses webhook/cron auto-triggers plus the updater's autostash, so editing this repo is safe. Docker integrations and the monitors stay running."),
    ManualAction("Runners", "Resume automatic collection", ("./src/20_pipeline/121-dev-mode.sh", "resume"), "Restores every setting 'Pause for development' changed, to its exact previous value. Does not queue a run by itself."),
    ManualAction("Runners", "Show run status", ("./src/20_pipeline/130b-run-status.sh",), "Writes a process/log status snapshot to the manual action log."),
    ManualAction("Tests", "Test zone", ("./src/20_pipeline/070-test-zone.py", "--limit", "5", "--apply"), "Re-runs the latest five records in records_test, then opens that sandbox folder.", RECORDS_TEST_PARENT),
    ManualAction("Tests", "Review system", ("./review-system.sh",), "Runs the repository health review and troubleshooting summary; on completion WAHA sends a System health message to the system destination (override with pcc health --chat-id/--purpose)."),
    ManualAction("Tests", "Full diagnostic report", ("./bin/pcc", "full-report"), "Creates a complete Markdown diagnostic report covering paths, settings, tools, integrations, queues, database counters, processes and recent logs."),
    ManualAction("Updater / Migration", "Update local copy", ("./src/40_monitor/003-update-loader.py", "--open-monitor-after"), "Opens the centered updater loader, refreshes this checkout/dependencies, then reopens the monitor."),
    ManualAction("Updater / Migration", "Pre-run update only", ("./src/20_pipeline/000-update-before-run.sh",), "Runs the lightweight git/dependency refresh normally used before worker iterations."),
    ManualAction("Updater / Migration", "Upload local changes to GitHub", ("./bin/pcc", "upload-github"), "Commits local checkout changes and pushes the current branch to GitHub/origin before other machines update."),
    ManualAction("Updater / Migration", "Rename folders", ("./src/50_tools/070-rename-record-folders.py", "--apply"), "Normalizes existing record folder names."),
    ManualAction("Updater / Migration", "Migrate records", ("./src/50_tools/090a-migrate-previous-records.sh",), "Imports/migrates previous record archives."),
    ManualAction("Integrations", "Start/refresh docker stack", ("./src/50_tools/010-docker-stack.sh", "up"), "Pulls/starts (or refreshes) the changedetection + WAHA + webhook containers; data stays in var/integrations."),
    ManualAction("Integrations", "Docker stack status", ("./src/50_tools/010-docker-stack.sh", "status"), "Writes container states plus the changedetection/WAHA URLs to the manual action log."),
    ManualAction("Integrations", "Restart docker stack", ("./src/50_tools/010-docker-stack.sh", "restart"), "Stops and starts the containers, applying the container settings saved below (changedetection URL, WAHA port/API key)."),
    ManualAction("Integrations", "Stop docker stack", ("./src/50_tools/010-docker-stack.sh", "down"), "Stops and removes the changedetection/WAHA/webhook containers; their data stays in var/integrations."),
    ManualAction("Settings", "Apply work templates", ("./src/50_tools/020-record-templates.py", "apply", "--apply"), "Copies the selected template files into templates/ inside every saved record folder (existing files kept)."),
    ManualAction("Settings", "Build detail views", ("./src/20_pipeline/040-build-detail-views.py", "--apply"), "Rebuilds saved record views, ICS files, and split tables."),
    ManualAction("Settings", "Repair missing deadlines", ("./src/20_pipeline/050-repair-missing-deadlines.py", "--apply"), "Finds folders/rows missing DTEND, re-downloads details, and renames folders after a deadline is recovered."),
    ManualAction("Settings", "Build calendars", ("./src/20_pipeline/060-build-calendar.py", "--all"), "Rebuilds calendar import packages."),
    ManualAction("Settings", "Import generated calendars", ("bash", "-lc", "PC_CALENDAR_AUTO_IMPORT=1 ./src/20_pipeline/060-build-calendar.py --all"), "Rebuilds and opens generated ICS files."),
    ManualAction("Settings", "Webhook listener", ("./src/10_webhook/020-start-listener.sh", "--replace-port-owner"), "Starts/restarts the local webhook listener."),
    ManualAction("Settings", "Install webhook service", ("./src/10_webhook/030-install-service.sh",), "Installs/repairs the persistent user systemd webhook service."),
    ManualAction("Settings", "Open web monitor", ("./src/50_tools/130-open-web-app.sh", "monitor"), "Starts/opens the browser monitor (chromeless app window when available)."),
    ManualAction("Integrations", "Open changedetection app window", ("./src/50_tools/130-open-web-app.sh", "changedetection"), "Opens the changedetection.io dashboard in a chromeless app window on the desktop — independent of Firefox, no browser header."),
    ManualAction("Integrations", "Print changedetection JS setup", ("./bin/pcc", "changedetection-script"), "Writes the Browser Steps Execute JS instructions/script for Programadas + Abiertas pagination to the manual action log."),
    ManualAction("Integrations", "Open WAHA app window", ("./src/50_tools/130-open-web-app.sh", "waha"), "Opens the WAHA dashboard in a chromeless app window on the desktop (login: admin + the password from data/config/integration-access.txt)."),
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


# Reset actions exposed as separate buttons (per operator request). Maps the
# button action to (src/50_tools/110-reset.py subcommand, is_destructive). The destructive
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
    if running("[s]rc/10_webhook/010-webhook-listener.py") or running("[p]ython3? -u .*src/10_webhook/010-webhook-listener.py"):
        return True
    try:
        result = subprocess.run(["docker", "compose", "ps", "--status", "running", "webhook"], cwd=BASE_DIR, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=3)
        return "webhook" in result.stdout.lower()
    except Exception:
        return False

def webhook_access_payload() -> dict[str, object]:
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
    settings_file = parse_settings_file()
    port = str(os.environ.get("PC_WEBHOOK_PORT") or settings_file.get("PC_WEBHOOK_PORT") or "8765")
    public_host = str(os.environ.get("PC_WEBHOOK_PUBLIC_HOST") or settings_file.get("PC_WEBHOOK_PUBLIC_HOST") or "host.docker.internal")
    shown = token or "YOUR_TOKEN"
    query = "?method=POST&format=text&overflow=truncate&rto=15&cto=10"
    return {
        "token": token,
        "token_exists": bool(token),
        "token_file": str(token_path),
        "port": port,
        "listener_running": webhook_running(),
        # Primary changedetection notification URL: works when changedetection is
        # in Docker but the durable listener is running on the host.
        "changedetection_url": f"json://{public_host}:{port}/panamacompra/{shown}{query}",
        # Compose-network URL only works when changedetection and webhook are in
        # this same docker-compose project/network; old/standalone containers will
        # fail DNS lookup for host 'webhook'.
        "compose_url": f"json://webhook:8765/panamacompra/{shown}{query}",
        # Docker container -> host-run listener as plain HTTP (for curl/tests).
        "docker_to_host_url": f"http://{public_host}:{port}/panamacompra/{shown}",
        # Local test from the host itself.
        "local_url": f"http://127.0.0.1:{port}/panamacompra/{shown}",
        "access_note": str(pc_common.DATA_CONFIG_DIR / "integration-access.txt"),
        "generated_by": "setup.sh → docker stack up (auto-generates .webhook_token when missing); also src/10_webhook/040-diagnose-webhook.sh",
    }


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
        return time.strftime("%Y-%m-%d_%H-%M", time.localtime(path.stat().st_mtime))
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
        "worker_log": tail(WORKER_LOG, 10),
        "current_log": tail(CURRENT_LOG, 14),
        "server_time": time.strftime("%Y-%m-%d_%H-%M"),
        "waha_chat_id": read_chat_file(WAHA_CHAT_ID_PATH),
        "waha_chat_id_index": read_chat_file(WAHA_CHAT_ID_INDEX_PATH),
        "waha_chat_id_details": read_chat_file(WAHA_CHAT_ID_DETAILS_PATH),
        "waha_chat_id_status": read_chat_file(WAHA_CHAT_ID_STATUS_PATH),
        "waha_chat_id_open_now": read_chat_file(WAHA_CHAT_ID_OPEN_NOW_PATH),
        "waha_chat_id_system": read_chat_file(WAHA_CHAT_ID_SYSTEM_PATH),
        "waha_chat_id_summary": read_chat_file(WAHA_CHAT_ID_SUMMARY_PATH),
        "waha_clients": read_waha_clients_text(),
        "settings": load_monitor_settings(),
        "last_summary": read_last_summary(),
    }

_TIMER_CORE = None


def _timer_core():
    """Shared scheduling/data core of the next-run timer (002-next-run-timer.py).

    Loaded lazily: importing the core reads settings and never needs tkinter,
    so the web monitor can serve the same countdown the Tk/CLI timers show."""
    global _TIMER_CORE
    if _TIMER_CORE is None:
        spec = _importlib_util.spec_from_file_location(
            "panamacompra_next_run_timer_core", Path(__file__).resolve().with_name("002-next-run-timer.py")
        )
        assert spec and spec.loader
        module = _importlib_util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        _TIMER_CORE = module
    return _TIMER_CORE


def web_timer_payload() -> dict[str, object]:
    """JSON payload for the web monitor's next-run countdown strip."""
    try:
        core = _timer_core()
        return core.timer_json_payload(core.timer_snapshot(5))
    except Exception as exc:  # noqa: BLE001 - the strip degrades, the page must not
        return {"target": "", "countdown": "—", "error": str(exc)}


def changedetection_schedule_payload() -> dict[str, object]:
    """Read-only "what is active / scheduled" snapshot for the Scheduler tab.

    Combines changedetection's own watch list (via the timer core, which
    already knows how to talk to its API) with the local automatic-run gate,
    so the operator can see in one place whether messages *should* be going
    out right now."""
    settings = load_monitor_settings()
    auto_run = str(settings.get("PC_WEBHOOK_AUTO_RUN", "1")).strip().lower() not in {"0", "false", "no", "off"}
    dev_mode_active = (pc_common.QUEUE_DIR / "dev_mode_active.flag").exists()
    try:
        core = _timer_core()
        watch_status = core.changedetection_watch_status()
    except Exception as exc:  # noqa: BLE001 - the panel degrades, the page must not
        watch_status = {"source": "", "configured": False, "watches": [], "schedule_note": "", "error": str(exc)}
    watch_status = dict(watch_status)
    watch_status["webhook_auto_run"] = auto_run
    watch_status["dev_mode_active"] = dev_mode_active
    return watch_status


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

# Standalone page for the Android client apps' Calendar tab (loaded in a
# WebView pointed at /client-calendar?uid=<firebase_uid>). Deliberately a
# self-contained copy of just the calendar CSS/JS from the admin page's
# calendar card below, not a shared include — the admin page's HTML is one
# big f-string and factoring a shared fragment out of it isn't worth the risk
# of a blind edit to an already-verified 600+ line string. If you change the
# calendar's appearance (CSS in the "Ubuntu-style opportunity calendar" block
# below, or the renderCalendar* functions), mirror it here too.
CLIENT_CALENDAR_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PanamaCompra Calendar</title>
<style>
body { font-family: system-ui, -apple-system, Segoe UI, sans-serif; margin: 10px; background: #0f172a; color: #e5e7eb; }
.small { color: #94a3b8; font-size: .85rem; }
.controls { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-bottom: 10px; }
select, input, button { border-radius: 8px; border: 1px solid #475569; background: #020617; color: #e5e7eb; padding: 6px 8px; font-size: .9rem; }
button { cursor: pointer; }
.calendar-board { background: #020617; border: 1px solid #334155; border-radius: 10px; overflow: hidden; }
.calendar-title { display: flex; justify-content: space-between; gap: 10px; align-items: center; padding: 10px 12px; background: #0b1220; border-bottom: 1px solid #334155; }
.calendar-title h3 { margin: 0; color: #bfdbfe; font-size: 1rem; }
.calgrid { display: grid; grid-template-columns: repeat(7, minmax(0, 1fr)); }
.calgrid .dow { text-align: center; color: #93c5fd; font-weight: 700; font-size: .8rem; padding: 7px 4px; border-bottom: 1px solid #1e293b; background: #0f172a; }
.calcell { min-height: 100px; border-right: 1px solid #1e293b; border-bottom: 1px solid #1e293b; padding: 5px; cursor: pointer; background: #020617; overflow: hidden; }
.calcell.blank { background: #02061799; cursor: default; }
.calcell.today { box-shadow: inset 0 0 0 2px #facc15; }
.calcell .num { color: #cbd5e1; font-size: .85rem; font-weight: 700; display: flex; justify-content: space-between; margin-bottom: 4px; }
.calcell .count { color: #94a3b8; font-size: .72rem; font-weight: 400; }
.calevent { display: block; margin: 3px 0; padding: 3px 5px; border-radius: 6px; border-left: 3px solid #38bdf8; background: #172554; color: #dbeafe; font-size: .74rem; line-height: 1.3; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.calevent.soon { border-left-color: #facc15; background: #422006; color: #fde68a; }
.calevent.expired { border-left-color: #f87171; background: #450a0a; color: #fecaca; }
.calevent.more { border-left-color: #64748b; background: #1e293b; color: #cbd5e1; }
.timeline-scroll { max-height: 70vh; overflow-y: auto; border-top: 1px solid #1e293b; }
.timeline { display: grid; grid-template-columns: 56px 1fr; }
.week-timeline { display: grid; grid-template-columns: 56px repeat(7, minmax(0, 1fr)); }
.hour-label { color: #93c5fd; font-weight: 700; font-size: .74rem; padding: 5px 6px; text-align: right; border-bottom: 1px solid #1e293b; border-right: 1px solid #1e293b; background: #0f172a; }
.hour-lane { min-height: 30px; padding: 3px 6px; display: flex; flex-direction: column; gap: 3px; border-bottom: 1px solid #1e293b; }
.week-timeline .hour-lane { padding: 2px; gap: 2px; border-right: 1px solid #1e293b; }
.timeline-notime .hour-label, .timeline-notime .hour-lane, .wk-notime { background: #0b1220; border-bottom: 2px solid #334155; }
.wk-head { padding: 6px 4px; text-align: center; font-weight: 700; color: #93c5fd; font-size: .76rem; border-bottom: 1px solid #1e293b; background: #0f172a; position: sticky; top: 0; z-index: 1; }
.wk-corner { background: #0f172a; border-bottom: 1px solid #1e293b; position: sticky; top: 0; z-index: 1; }
.year-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 8px; padding: 10px; }
.month-box { border: 1px solid #334155; border-radius: 8px; padding: 10px; background: #0b1220; cursor: pointer; }
.month-box b { color: #bfdbfe; }
.bar-track { height: 10px; background: #1e293b; border-radius: 999px; overflow: hidden; margin-top: 6px; }
.bar-fill { height: 100%; background: linear-gradient(90deg, #38bdf8, #22c55e); border-radius: 999px; }
</style>
</head>
<body>
<div class="controls">
  <select id="cal-view" onchange="loadCalendar()">
    <option value="day">Day</option><option value="week">Week</option>
    <option value="month" selected>Month</option><option value="year">Year</option>
  </select>
  <select id="cal-field" onchange="loadCalendar()">
    <option value="end" selected>Deadline</option><option value="start">Start</option><option value="downloaded">Downloaded</option>
  </select>
  <input id="cal-date" size="10" placeholder="YYYY-MM-DD">
  <button onclick="loadCalendar(-1)">&#9664;</button>
  <button onclick="loadCalendar(0)">Today</button>
  <button onclick="loadCalendar(1)">&#9654;</button>
</div>
<div id="calendar-visual" class="small">Loading calendar&hellip;</div>
<script>
const params = new URLSearchParams(location.search);
const uid = params.get('uid') || '';
const WEEKDAY_LABELS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
let calendarAnchor = '';
function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function calendarEventClass(ev) {
  const status = (ev.status || '').toLowerCase();
  if (status.includes('venc') || status.includes('cerrad') || status.includes('expir')) return 'expired';
  if (status.includes('pront') || status.includes('soon')) return 'soon';
  return '';
}
function renderCalendarEvent(ev) {
  const title = (ev.numero ? ev.numero + ' · ' : '') + (ev.description || '(sin descripcion)');
  const clock = ev.clock && ev.clock !== '--:--' ? ev.clock + ' ' : '';
  return `<span class="calevent ${calendarEventClass(ev)}" title="${esc(title)}">${esc(clock + title)}</span>`;
}
function renderCalendarDayCell(day, events, blank, maxShown) {
  if (blank) return '<div class="calcell blank"></div>';
  const limit = maxShown || 4;
  const shown = (events || []).slice(0, limit).map(renderCalendarEvent).join('');
  const more = (events || []).length > limit ? `<span class="calevent more">+${events.length - limit} more</span>` : '';
  const classes = ['calcell'];
  if (day.iso === day.today) classes.push('today');
  return `<div class="${classes.join(' ')}" onclick="document.getElementById('cal-date').value='${day.iso}'; document.getElementById('cal-view').value='day'; loadCalendar()"><div class="num"><span>${day.label}</span><span class="count">${events.length || ''}</span></div>${shown}${more}</div>`;
}
async function loadCalendar(shift) {
  const node = document.getElementById('calendar-visual');
  const view = document.getElementById('cal-view').value || 'month';
  const field = document.getElementById('cal-field').value || 'end';
  const dateBox = document.getElementById('cal-date');
  if (shift === 0) { calendarAnchor = ''; dateBox.value = ''; }
  const anchor = (dateBox.value || calendarAnchor).trim();
  let qs = 'uid=' + encodeURIComponent(uid) + '&view=' + encodeURIComponent(view) + '&field=' + encodeURIComponent(field);
  if (anchor) qs += '&date=' + encodeURIComponent(anchor);
  if (shift) qs += '&shift=' + shift;
  try {
    const g = await (await fetch('/api/client-calendar-grid?' + qs, {cache: 'no-store'})).json();
    if (g.hidden) { node.textContent = 'Calendar hidden for this profile.'; return; }
    calendarAnchor = g.anchor;
    dateBox.value = g.anchor;
    const grouped = g.events || {};
    const title = `${g.label || view} · ${g.start} to ${g.end} · ${g.total || 0} opportunities`;
    if (view === 'year') {
      const peak = Math.max(1, ...Object.values(g.month_counts || {}).map(Number));
      const boxes = (g.months || []).map(m => {
        const count = Number((g.month_counts || {})[m.value] || 0);
        const width = Math.round(100 * count / peak);
        return `<div class="month-box" onclick="document.getElementById('cal-date').value='${m.value}-01'; document.getElementById('cal-view').value='month'; loadCalendar()"><b>${esc(m.label)}</b><div class="small">${count} opportunities</div><div class="bar-track"><div class="bar-fill" style="width:${width}%"></div></div></div>`;
      }).join('');
      node.innerHTML = `<div class="calendar-board"><div class="calendar-title"><h3>${esc(title)}</h3></div><div class="year-grid">${boxes}</div></div>`;
      return;
    }
    const days = g.days || [];
    const hasTime = ev => ev.clock && ev.clock !== '--:--';
    const hourOf = ev => parseInt(ev.clock.slice(0, 2), 10) || 0;
    if (view === 'day') {
      const day = days[0] || {};
      const evs = grouped[day.iso] || [];
      const notime = evs.filter(ev => !hasTime(ev));
      const byHour = Array.from({length: 24}, () => []);
      evs.forEach(ev => { if (hasTime(ev)) byHour[hourOf(ev)].push(ev); });
      const notimeRow = notime.length
        ? `<div class="hour-label timeline-notime">No time</div><div class="hour-lane timeline-notime">${notime.map(renderCalendarEvent).join('')}</div>` : '';
      const hourRows = byHour.map((evsAtHour, h) => `<div class="hour-label">${String(h).padStart(2, '0')}:00</div><div class="hour-lane">${evsAtHour.map(renderCalendarEvent).join('')}</div>`).join('');
      node.innerHTML = `<div class="calendar-board"><div class="calendar-title"><h3>${esc(title)}</h3></div><div class="timeline-scroll"><div class="timeline">${notimeRow}${hourRows}</div></div></div>`;
      return;
    }
    if (view === 'week') {
      const cols = days.map((day, i) => {
        const evs = grouped[day.iso] || [];
        const byHour = Array.from({length: 24}, () => []);
        evs.forEach(ev => { if (hasTime(ev)) byHour[hourOf(ev)].push(ev); });
        return {i, iso: day.iso, notime: evs.filter(ev => !hasTime(ev)), byHour};
      });
      const head = '<div class="wk-corner"></div>' + cols.map(c => `<div class="wk-head">${WEEKDAY_LABELS[c.i] || ''} ${esc((c.iso || '').slice(5))}</div>`).join('');
      const anyNotime = cols.some(c => c.notime.length);
      const notimeRow = anyNotime
        ? '<div class="hour-label wk-notime">No time</div>' + cols.map(c => `<div class="hour-lane wk-notime">${c.notime.map(renderCalendarEvent).join('')}</div>`).join('') : '';
      let hourRows = '';
      for (let h = 0; h < 24; h++) {
        hourRows += `<div class="hour-label">${String(h).padStart(2, '0')}:00</div>`;
        hourRows += cols.map(c => `<div class="hour-lane">${c.byHour[h].map(renderCalendarEvent).join('')}</div>`).join('');
      }
      node.innerHTML = `<div class="calendar-board"><div class="calendar-title"><h3>${esc(title)}</h3></div><div class="timeline-scroll"><div class="week-timeline">${head}${notimeRow}${hourRows}</div></div></div>`;
      return;
    }
    let cells = WEEKDAY_LABELS.map(d => `<div class="dow">${d}</div>`).join('');
    for (let i = 0; i < (g.first_weekday || 0); i++) cells += renderCalendarDayCell(null, [], true);
    cells += days.map(day => renderCalendarDayCell(day, grouped[day.iso] || [], false)).join('');
    node.innerHTML = `<div class="calendar-board"><div class="calendar-title"><h3>${esc(title)}</h3></div><div class="calgrid">${cells}</div></div>`;
  } catch (err) {
    node.textContent = 'Calendar unavailable: ' + err;
  }
}
if (!uid) {
  document.getElementById('calendar-visual').textContent = 'Missing uid.';
} else {
  loadCalendar(0);
}
</script>
</body>
</html>
"""

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
pre.log-pane {{ max-height: 180px; min-height: 2.8rem; }}
/* Process status is a tidy flex grid of small chips instead of one crowded
   wrapped line: green = RUNNING, gray = off, even gaps. */
.proc-wrap {{ display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }}
.pill {{ display: inline-block; padding: 4px 10px; border-radius: 999px; font-weight: 700; font-size: .8rem; }}
.on {{ background: #14532d; color: #bbf7d0; }} .off {{ background: #1f2937; color: #9ca3af; }}
.message {{ font-size: 1.15rem; color: #fef3c7; }}
.small {{ color: #94a3b8; font-size: 0.90rem; }}
.xs {{ color: #94a3b8; font-size: 0.78rem; }}
.done {{ color: #bbf7d0; font-weight: 700; }}
button {{ background: #334155; color: #e5e7eb; border: 0; border-radius: 8px; padding: 9px 14px; font-weight: 700; cursor: pointer; margin: 0 8px 8px 0; transition: background .15s ease, transform .05s ease; }}
button:hover {{ background: #475569; }}
button:active {{ transform: translateY(1px); }}
button:disabled {{ background: #1f2937; color: #6b7280; cursor: not-allowed; transform: none; }}
button.primary {{ background: #2563eb; color: #fff; }}
button.primary:hover {{ background: #1d4ed8; }}
button.primary:disabled {{ background: #1e293b; color: #6b7280; }}
.zone {{ margin-top: 14px; padding-top: 8px; border-top: 1px solid #334155; }}
.zone h3 {{ margin: 0 0 2px; color: #fef3c7; }}
.zone-desc {{ margin: 0 0 8px; color: #94a3b8; }}
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
.destination-grid {{ display: grid; grid-template-columns: minmax(180px, 240px) minmax(280px, 1fr); gap: 8px 12px; align-items: center; margin: 10px 0; }}
.destination-grid label {{ color: #94a3b8; }}
.destination-grid input, .destination-grid textarea {{ width: 100%; box-sizing: border-box; }}
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
/* Sci-fi board: faint blueprint grid behind every chart, neon hover glow, and
   a pulsing LIVE beacon next to the last-refresh stamp. */
.chart {{ background: #020617 linear-gradient(#38bdf80d 1px, transparent 1px) 0 0 / 100% 22px, #020617 linear-gradient(90deg, #38bdf808 1px, transparent 1px) 0 0 / 22px 100%; border: 1px solid #334155; border-radius: 10px; padding: 12px; min-height: 180px; transition: border-color .2s ease, box-shadow .2s ease; }}
.chart:hover {{ border-color: #38bdf8; box-shadow: 0 0 16px #0ea5e955, inset 0 0 24px #0ea5e911; }}
.kpi-live {{ color: #22d3ee; font-weight: 700; text-shadow: 0 0 8px #22d3ee; }}
.kpi-live::before {{ content: '●'; margin-right: 5px; animation: kpiPulse 1.6s ease-in-out infinite; }}
@keyframes kpiPulse {{ 0%, 100% {{ opacity: 1; }} 50% {{ opacity: .25; }} }}
.kpi-filter-bar {{ display: flex; flex-wrap: wrap; gap: 10px; align-items: center; margin: 8px 0 12px; padding: 8px 10px; border: 1px solid #164e63; border-radius: 10px; background: #0b1220; }}
.item-line {{ border-left: 3px solid #38bdf8; padding: 3px 8px; margin: 4px 0; font-size: .85rem; color: #cbd5e1; }}
.item-line b {{ color: #67e8f9; }}
/* Ubuntu-style opportunity calendar with boxed events inside each date cell. */
.calendar-board {{ background: #020617; border: 1px solid #334155; border-radius: 10px; overflow: hidden; }}
.calendar-title {{ display: flex; justify-content: space-between; gap: 10px; align-items: center; padding: 10px 12px; background: #0b1220; border-bottom: 1px solid #334155; }}
.calendar-title h3 {{ margin: 0; color: #bfdbfe; }}
.calgrid {{ display: grid; grid-template-columns: repeat(7, minmax(0, 1fr)); }}
.calgrid .dow {{ text-align: center; color: #93c5fd; font-weight: 700; font-size: .88rem; padding: 7px 4px; border-bottom: 1px solid #1e293b; background: #0f172a; }}
.calcell {{ min-height: 142px; border-right: 1px solid #1e293b; border-bottom: 1px solid #1e293b; padding: 6px; cursor: pointer; background: #020617; transition: border-color .15s ease, box-shadow .15s ease, background .15s ease; overflow: hidden; }}
.calcell:hover {{ background: #0b1220; box-shadow: inset 0 0 0 1px #38bdf8; }}
.calcell.blank {{ background: #02061799; cursor: default; }}
.calcell.today {{ box-shadow: inset 0 0 0 2px #facc15; }}
.calcell .num {{ color: #cbd5e1; font-size: .95rem; font-weight: 700; display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px; }}
.calcell .count {{ color: #94a3b8; font-size: .82rem; font-weight: 400; }}
.calevent {{ display: block; margin: 4px 0; padding: 4px 6px; border-radius: 6px; border-left: 3px solid #38bdf8; background: #172554; color: #dbeafe; font-size: .85rem; line-height: 1.4; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
.calevent.soon {{ border-left-color: #facc15; background: #422006; color: #fde68a; }}
.calevent.expired {{ border-left-color: #f87171; background: #450a0a; color: #fecaca; }}
.calevent.more {{ border-left-color: #64748b; background: #1e293b; color: #cbd5e1; }}
/* Day/week views: hourly timeline (hour rows, events placed at their hour)
   instead of a flat list or a day-chip grid. */
.timeline-scroll {{ max-height: 640px; overflow-y: auto; border-top: 1px solid #1e293b; }}
.timeline {{ display: grid; grid-template-columns: 64px 1fr; }}
.week-timeline {{ display: grid; grid-template-columns: 64px repeat(7, minmax(0, 1fr)); }}
.hour-label {{ color: #93c5fd; font-weight: 700; font-size: .78rem; padding: 6px 8px; text-align: right; border-bottom: 1px solid #1e293b; border-right: 1px solid #1e293b; background: #0f172a; }}
.hour-lane {{ min-height: 34px; padding: 4px 8px; display: flex; flex-direction: column; gap: 4px; border-bottom: 1px solid #1e293b; }}
.week-timeline .hour-lane {{ padding: 3px; gap: 3px; border-right: 1px solid #1e293b; }}
.timeline-notime .hour-label, .timeline-notime .hour-lane, .wk-notime {{ background: #0b1220; border-bottom: 2px solid #334155; }}
.wk-head {{ padding: 7px 6px; text-align: center; font-weight: 700; color: #93c5fd; font-size: .82rem; border-bottom: 1px solid #1e293b; background: #0f172a; position: sticky; top: 0; z-index: 1; }}
.wk-corner {{ background: #0f172a; border-bottom: 1px solid #1e293b; position: sticky; top: 0; z-index: 1; }}
.agenda-empty {{ padding: 16px; }}
.year-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 8px; padding: 10px; }}
.month-box {{ border: 1px solid #334155; border-radius: 8px; padding: 10px; background: #0b1220; cursor: pointer; }}
.month-box:hover {{ border-color: #38bdf8; }}
.month-box b {{ color: #bfdbfe; }}
.month-box .bar-track {{ margin-top: 8px; }}
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
.waha-alert {{ background: #451a03; border: 2px solid #f59e0b; border-radius: 12px; padding: 12px 16px; margin: 0 0 16px; color: #fef3c7; }}
.waha-alert button {{ margin-left: 10px; }}
.waha-alert img {{ display: block; margin-top: 10px; background: #fff; padding: 8px; border-radius: 8px; }}
</style>
</head>
<body>
<div class="card">
  <h1>PanamaCompra Progress Monitor</h1>
  <p class="small"><span id="server-time">Loading...</span> · Next run in <b id="web-timer-countdown">…</b> · Low-power polling every <span id="refresh-label">{REFRESH_SECONDS}</span>s while running · JSON: <a href="/api/status">/api/status</a></p>
  <div class="bar"><div class="fill" id="fill">0%</div></div>
  <p class="message" id="message">Loading...</p>
  <p id="done-note" class="done" hidden></p>
  <div id="processes" class="proc-wrap"></div><p class="small">Process pills show live OS processes: detail is off except during STEP 3; webhook should stay RUNNING when the host listener is active.</p>
</div>
<div id="waha-alert-banner" class="waha-alert" hidden="">
  <strong>⚠️ WAHA WhatsApp session needs attention</strong> — <span id="waha-alert-text"></span>
  <button onclick="checkWahaSession()">Retry check</button>
  <button onclick="runWahaRecovery()" id="waha-recovery-action" hidden=""></button>
  <button onclick="toggleWahaQr()" id="waha-qr-toggle">Show QR to scan</button>
  <div id="waha-alert-qr" hidden="">
    <img id="waha-qr-img" alt="WAHA pairing QR code" width="220" height="220">
    <p class="small">Open WhatsApp on your phone → Linked Devices → Link a Device, and scan. The code refreshes automatically while shown.</p>
  </div>
</div>
<div class="tab-nav"><button class="active" data-tab-button="overview" onclick="showTab('overview')">Overview</button><button data-tab-button="operations" onclick="showTab('operations')">Operations</button><button data-tab-button="records" onclick="showTab('records')">Opportunities</button><button data-tab-button="calendar" onclick="showTab('calendar')">Calendar</button><button data-tab-button="decision" onclick="showTab('decision')">KPIs</button><button data-tab-button="whatsapp" onclick="showTab('whatsapp')">WhatsApp</button><button data-tab-button="scheduler" onclick="showTab('scheduler')">Scheduler</button><button data-tab-button="integrations" onclick="showTab('integrations')">Integrations</button><button data-tab-button="settings" onclick="showTab('settings')">Settings</button></div>
<div class="card" data-tab="records"><h2>Records Pendings</h2><div id="records-pending" class="record-card record-pending">Records Pendings: —</div><p class="small">Use Record selector and filters → Detail status = Pending records for full selectors/open actions.</p></div>
<div class="card" data-tab="records"><h2>Records Completed</h2><div id="records-completed" class="record-card record-completed">Records Completed: —</div><p class="small">Use Record selector and filters → Detail status = Completed records for full selectors/open actions.</p></div>
<div class="card" data-tab="records"><h2>Database summary</h2><p class="small">Read-only archive database summary with counters, status breakdown, recent records and DB elements/columns.</p><pre id="records-db-summary">Database summary loading…</pre><p><button onclick="refreshDbReview('records-db-summary')">Refresh DB summary</button></p></div>
<div class="card" data-tab="records"><h2>Database review</h2><p class="small">Same database details in a collapsible review panel. Refresh after a run or a reset.</p><pre id="db-review">Loading database snapshot…</pre><p><button onclick="refreshDbReview()">Refresh DB snapshot</button></p></div>
<div class="card" data-tab="records"><h2>Record selector and filters</h2><p class="small">Collected records as “[downloaded timestamp | DTEND status] NUMERO — description”; choose newest-first or oldest-first ordering. Use filters first, then Ctrl/Shift-select one or more records to notify or import calendars.</p><p><label class="small">Deadline <select id="record-status"><option value="all">All</option><option value="soon">Next to expire</option><option value="expired">Expired</option><option value="upcoming">Upcoming</option><option value="unknown">No date / needs repair</option></select></label> <label class="small">Detail status <select id="record-detail-status"><option value="all">All</option><option value="pending">Pending records</option><option value="saved">Completed records</option><option value="failed">Failed records</option></select></label> <label class="small">Order by <select id="record-order-field"><option value="downloaded">Downloaded date</option><option value="end">End date</option><option value="start">Start date</option></select></label> <label class="small"><select id="record-order"><option value="newest">Newest first</option><option value="oldest">Oldest first</option></select></label> <label class="small">DTEND on/after <input type="text" id="record-mindate" placeholder="YYYY-MM-DD [HH:MM]" size="16"></label> <label class="small">on/before <input type="text" id="record-maxdate" placeholder="YYYY-MM-DD [HH:MM]" size="16"></label> <label class="small">DTSTART on/after <input type="text" id="record-start-mindate" placeholder="YYYY-MM-DD [HH:MM]" size="16"></label> <label class="small">on/before <input type="text" id="record-start-maxdate" placeholder="YYYY-MM-DD [HH:MM]" size="16"></label> <label class="small">Downloaded on/after <input type="text" id="record-downloaded-mindate" placeholder="YYYY-MM-DD [HH:MM]" size="16"></label> <label class="small">on/before <input type="text" id="record-downloaded-maxdate" placeholder="YYYY-MM-DD [HH:MM]" size="16"></label> <span class="small">Legend: <span style="color:#86efac;font-weight:700">upcoming</span> · <span style="color:#fcd34d;font-weight:700">next to expire</span> · <span style="color:#fca5a5;font-weight:700">expired</span></span></p><p><select id="record-index" multiple size="10"></select> <button onclick="refreshRecordIndex()">Refresh list</button> <button onclick="openRecordFolder()">Open record folder</button> <button onclick="openRecordPortal()">Open in portal</button> <button onclick="notifySelectedRecords()">Notify selected WhatsApp</button> <button onclick="importSelectedCalendars()">Import selected calendars</button> <button onclick="templatesSelectedRecords()">Copy templates to selected</button></p><p id="record-detail" class="small">Loading record index…</p></div>
<div class="card" data-tab="overview"><h2>System health <span class="kpi-live" id="overview-live-stamp">LIVE</span></h2><p class="small">Snapshot of the last completed run, current intake and service reachability. Full analysis lives in the KPIs tab; run controls in Operations.</p><div id="overview-kpis" class="kpi-grid">Loading overview…</div></div>
<div class="card" data-tab="overview"><h2>Last run stages</h2><p class="small" id="overview-last-run">No completed run recorded yet.</p><div id="overview-stages" class="chart"></div></div>
<div class="card" data-tab="overview"><h2>Services</h2><div id="overview-services" class="small">Loading services…</div><p class="small">Webhook access details and the changedetection script live in the Integrations tab.</p></div>
<div class="card" data-tab="operations"><h2>Queue process</h2><p id="queue-summary" class="small">Loading queue…</p><pre id="queue-log"></pre></div>
<div class="card" data-tab="operations"><h2>Monitor buttons</h2><div class="subsection"><h3>Run controls</h3><p><span class="small" style="margin-right:8px">Mode</span><span class="mode-group" id="run-mode"><label><input type="radio" name="run-mode" value="auto" disabled><span>automatic</span></label><label><input type="radio" name="run-mode" value="restart" checked><span>run pending only</span></label><label><input type="radio" name="run-mode" value="manual"><span>manual run</span></label><label><input type="radio" name="run-mode" value="test"><span>test run</span></label></span> <label class="small">Index page cap <input id="index-limit" value="0" size="4"></label> <label class="small">Detail limit <input id="detail-limit" value="0" size="4"></label> <button id="run-button" class="primary" onclick="requestRun()">Request selected run</button><button class="danger" onclick="stopRun()">Stop active run</button><button class="primary" onclick="startAll()" title="Brings Docker integrations and the webhook listener back up, and opens the monitor">▶ Start All</button><button class="danger" onclick="stopAll()" title="DANGER: stops ALL processes, including this monitor">⛔ Stop All</button><button onclick="devPause()" title="Stops any active run and pauses webhook/cron auto-triggers plus the updater's autostash, so editing this repo is safe">⏸ Dev Pause</button><button onclick="devResume()" title="Restores everything Dev Pause changed">▶ Dev Resume</button><span id="button-status" class="small"></span></p><p class="small">Integrations: <a href="{CHANGEDETECTION_URL}" target="_blank">Open changedetection UI</a> · <a href="{WAHA_DASHBOARD_URL}" target="_blank">Open WAHA dashboard (pair by QR)</a> · container data lives in var/integrations; manage the stack from the Integrations buttons below. Container settings apply on the next stack restart.</p><p class="small" id="run-hint"><strong>Mode:</strong> automatic is shown for changedetection/webhook runs only; run pending only queues the normal collector; manual run starts the worker now; test run uses the isolated test zone. Index page cap is optional: 0 means crawl all pages until the portal has no Next page; detail limit controls detail/test records (0 = unlimited: download until no pending entries remain).</p></div><div class="subsection"><h3>Action buttons</h3><div id="action-zones"></div></div></div>
<div class="card" data-tab="operations"><h2>Diagnostics</h2><table id="diagnostics"></table></div>
<div class="card" data-tab="operations"><h2>Recent worker log</h2><pre id="worker-log" class="log-pane"></pre></div>
<div class="card" data-tab="operations"><h2>Current action log</h2><pre id="current-log" class="log-pane"></pre></div>
<div class="card" data-tab="decision"><h2>KPI Dashboard <span class="kpi-live" id="kpi-live-stamp">LIVE</span></h2><p class="small">All KPIs in one tab: index scan intake, detail download throughput, WAHA delivery, deadline repair, plus diagrams about the collected items, contracting entities and locations so the numbers point at a decision. Use the filters to slice every card and diagram to a time window, a group or an entity.</p><div class="kpi-filter-bar"><label class="small">Window <select id="kpi-days" onchange="refreshDecisionDashboard()"><option value="0" selected>All time</option><option value="7">Last 7 days</option><option value="30">Last 30 days</option><option value="90">Last 90 days</option><option value="365">Last year</option></select></label> <label class="small">Group <input id="kpi-grupo" list="kpi-grupo-list" size="14" placeholder="all groups"></label><datalist id="kpi-grupo-list"></datalist> <label class="small">Entity <input id="kpi-entidad" list="kpi-entidad-list" size="26" placeholder="all entities"></label><datalist id="kpi-entidad-list"></datalist> <button class="primary" onclick="refreshDecisionDashboard()">Apply filters</button> <button onclick="resetKpiFilters()">Reset</button> <button onclick="window.location = '/api/kpi-export?' + kpiFilterParams()">Export CSV</button> <span id="kpi-filter-state" class="small"></span></div><div id="decision-kpis" class="kpi-grid"></div><div class="diagram-grid"><div class="chart"><h3>Detail status mix</h3><div id="decision-status"></div></div><div class="chart"><h3>Index groups</h3><div id="decision-groups"></div></div><div class="chart"><h3>Daily intake (last 14 days)</h3><div id="decision-daily"></div></div><div class="chart"><h3>Monthly intake trend</h3><div id="decision-trend"></div></div><div class="chart"><h3>Top contracting entities</h3><div id="decision-entities"></div></div><div class="chart"><h3>Locations / buying units (from details)</h3><div id="decision-locations"></div></div><div class="chart"><h3>Most frequent items</h3><div id="decision-top-items"></div></div><div class="chart"><h3>Latest parsed items</h3><div id="decision-latest-items"></div></div><div class="chart"><h3>Detail queue pressure</h3><div id="decision-deadlines"></div></div><div class="chart"><h3>Items analysis</h3><div id="decision-items"></div></div><div class="chart"><h3>Item keywords</h3><div id="decision-item-keywords" class="keyword-cloud"></div></div></div><pre id="decision-recommendations">Loading decision signals…</pre><p><button onclick="refreshDecisionDashboard()">Refresh KPIs</button></p></div>
<div class="card" data-tab="records"><h2>Opportunity calendar</h2><p class="small">Collected opportunities by day, week, month or year. <label class="small">View <select id="cal-view" onchange="loadCalendar()"><option value="day">Day</option><option value="week">Week</option><option value="month" selected>Month</option><option value="year">Year</option></select></label> <label class="small">Date field <select id="cal-field" onchange="loadCalendar()"><option value="end" selected>Deadline (end)</option><option value="start">Start</option><option value="downloaded">Downloaded</option></select></label> <label class="small">Anchor <input id="cal-date" size="10" placeholder="YYYY-MM-DD"></label> <label class="small">Keyword <input id="cal-filter" size="16" placeholder="filter text" onchange="loadCalendar()"></label> <button onclick="loadCalendar(-1)">◀ Prev</button> <button onclick="loadCalendar(0)">Today</button> <button onclick="loadCalendar(1)">Next ▶</button> <button onclick="loadCalendar()">Show</button></p><div id="calendar-visual" class="chart" style="min-height:120px;margin:8px 0">Calendar visual loading…</div><pre id="calendar-text" style="max-height: 420px">Loading calendar…</pre></div>
<div class="card" data-tab="scheduler"><h2>changedetection schedule <span class="small">(read-only)</span></h2><p class="small">What changedetection itself has active and scheduled right now — this panel only reads changedetection's API/datastore, it never changes anything there. Control which trigger actually starts a run below (webhook vs cron) and the "Automatic runs from changedetection" toggle in Settings.</p><div id="cd-schedule-banner" class="small"></div><div id="cd-schedule-summary" class="small">Loading changedetection schedule…</div><table id="cd-schedule-table" class="small" style="width:100%;border-collapse:collapse"></table><p><button onclick="refreshChangedetectionSchedule()">Refresh changedetection schedule</button></p></div>
<div class="card" data-tab="scheduler"><h2>Automatic scheduler (cron)</h2><p class="small">Runs the collector on a repeating schedule instead of the changedetection webhook trigger. Enabling this sets Auto-run source to cron and installs a crontab entry (via <code>src/50_tools/160-manage-cron-schedule.py</code>, no manual <code>crontab -e</code> needed); disabling it removes that entry and switches Auto-run source back to changedetection.</p><p><label class="small"><input type="checkbox" id="cron-enabled"> Enable scheduled automatic runs</label></p><p class="xs">Days <label><input type="radio" name="cron-days" value="daily" checked> Daily</label> <label><input type="radio" name="cron-days" value="weekdays"> Weekdays (Mon-Fri)</label> <label><input type="radio" name="cron-days" value="weekends"> Weekends (Sat-Sun)</label> <label><input type="radio" name="cron-days" value="custom"> Custom</label></p><p><label class="small">Custom days (0=Sun..6=Sat) <input id="cron-custom-days" size="20" placeholder="e.g. 1,3,5"></label></p><p><label class="small">Start time (HH:MM) <input id="cron-start" size="8" value="08:00"></label> <label class="small">End time (HH:MM) <input id="cron-end" size="8" value="18:00"></label> <label class="small">Repeat every (minutes) <input id="cron-interval" size="6" value="30"></label></p><p><button class="primary" onclick="applyCronSchedule()">Save &amp; Apply schedule</button> <button onclick="refreshCronScheduleStatus()">Refresh status</button></p><p class="small" id="cron-schedule-status"></p></div>
<div class="card" data-tab="integrations"><h2>changedetection Browser Steps JS</h2><p class="small">Paste this into <strong>ChangeDetection → Watch → Browser Steps → Execute JS</strong>. Keep CSS filter <code>#pc-monitor-output</code>, and leave Visual Filter, Remove elements and Triggers empty/disabled. It crawls all Programadas pages first, then all Abiertas pages.</p><p><button onclick="loadChangedetectionScript()">Load script</button> <button onclick="copyChangedetectionScript()">Copy script</button> <span id="cd-script-state" class="small"></span></p><textarea id="changedetection-script" rows="16" style="width:100%; box-sizing:border-box" placeholder="Press Load script"></textarea></div>
<div class="card" data-tab="whatsapp"><h2>WhatsApp settings</h2><p class="small">All WhatsApp options in one place: destinations, delivery settings, WAHA server connection, toggles and per-destination content filters.</p><div class="subsection"><h3>Destinations & toggles</h3><div class="destination-grid"><label>Default / one group</label><textarea id="waha-message-wa" rows="2" placeholder="12036...@g.us (used when a purpose-specific group is blank)"></textarea><label>Index alerts</label><input id="waha-index-wa" size="32" placeholder="blank = default group"><label>Item details</label><input id="waha-details-wa" size="32" placeholder="blank = default group"><label>Status changes</label><input id="waha-status-wa" size="32" placeholder="blank = default group"><label>Open Now Opportunities</label><input id="waha-open-now-wa" size="32" placeholder="blank = Index alerts / default group"><label>System health</label><input id="waha-system-wa" size="32" placeholder="blank = default group"><label>Final summary per round</label><input id="waha-summary-wa" size="32" placeholder="blank = default group"></div><p><label class="small"><input type="checkbox" id="notify-whatsapp-wa" onchange="syncWhatsappMirror('wa'); saveMonitorSetting('PC_NOTIFY_WHATSAPP', this.checked ? '1' : '0')"> Notify by WhatsApp (index alerts)</label><br><label class="small"><input type="checkbox" id="notify-details-wa" onchange="syncWhatsappMirror('wa'); saveMonitorSetting('PC_NOTIFY_DETAILS', this.checked ? '1' : '0')"> Detail follow-up WhatsApp</label></p><p><button onclick="saveWahaFrom('wa')">Save WhatsApp destinations</button> <button onclick="sendTestWhatsapp()">Send test WhatsApp</button></p></div><div class="subsection"><h3>Delivery & server settings</h3><div class="settings-grid"><label class="small">WhatsApp source <input id="set-PC_WAHA_SOURCE" size="16"></label> <label class="small">WhatsApp within N days <input id="set-PC_NOTIFY_WITHIN_DAYS" size="5" placeholder="all"></label> <label class="small">WAHA retries <input id="set-PC_WAHA_RETRIES" size="5"></label> <label class="small">Delay between sends (s) <input id="set-PC_WAHA_SEND_DELAY_SECONDS" size="5"></label> <label class="small">Digest above N new records <input id="set-PC_NOTIFY_INDEX_DIGEST_THRESHOLD" size="5"></label> <label class="small">Idle status every N hours <input id="set-PC_NOTIFY_IDLE_EVERY_HOURS" size="5"></label> <label class="small">WAHA base URL <input id="set-PC_WAHA_BASE_URL" size="24"></label> <label class="small">WAHA session <input id="set-PC_WAHA_SESSION" size="12"></label> <label class="small">WAHA events <input id="set-PC_WAHA_NOTIFY_EVENTS" size="40"></label> <label class="small">WAHA server port <input id="set-WAHA_PORT" size="6"></label> <label class="small">WAHA server API key <input id="set-WAHA_API_KEY" size="20"></label> <label class="small">WAHA dashboard user <input id="set-WAHA_DASHBOARD_USERNAME" size="12"></label> <label class="small">WAHA dashboard password (generated by setup) <input id="set-WAHA_DASHBOARD_PASSWORD" size="14"></label></div><p><button onclick="saveAdvancedSettings()">Save WhatsApp advanced settings</button></p><p class="xs">The WAHA dashboard login is user admin with a RANDOM password generated by setup — see data/config/integration-access.txt. Change it here whenever you like — it applies on the next docker stack restart.</p><p><label class="small"><input type="checkbox" id="set-PC_WAHA_ENABLED" onchange="saveMonitorSetting('PC_WAHA_ENABLED', this.checked ? '1' : '0')"> Enable WAHA WhatsApp sending</label> <label class="small"><input type="checkbox" id="set-PC_NOTIFY_SKIP_EXPIRED" onchange="saveMonitorSetting('PC_NOTIFY_SKIP_EXPIRED', this.checked ? '1' : '0')"> Skip already-expired opportunities</label> <label class="small"><input type="checkbox" id="set-PC_NOTIFY_DETAILS_INLINE" onchange="saveMonitorSetting('PC_NOTIFY_DETAILS_INLINE', this.checked ? '1' : '0')"> Send each detail message right after its download</label> <label class="small"><input type="checkbox" id="set-PC_INDEX_FROM_SNAPSHOT" onchange="saveMonitorSetting('PC_INDEX_FROM_SNAPSHOT', this.checked ? '1' : '0')"> AUTO runs import index from changedetection snapshot</label></p></div><div class="subsection"><h3>Content filters</h3><p><label class="small">Shared <input id="flt-global" size="30"></label> <label class="small">Index alerts <input id="flt-index" size="30"></label> <label class="small">Item details <input id="flt-details" size="30"></label> <label class="small">Status changes <input id="flt-status" size="30"></label> <label class="small">Open Now Opportunities <input id="flt-open-now" size="30"></label> <button onclick="saveWahaFilters()">Save filters</button></p></div></div>
<div class="card" data-tab="whatsapp"><h2>WhatsApp client profiles</h2><div class="subsection"><h3>Add / update a client</h3><p class="small">Pick any destination returned by WAHA or type a custom chat ID; the filter accepts custom expressions (OR with commas, AND with '+', NOT with '-').</p><p><label class="small">Client name <input id="client-name" size="18"></label> <label class="small">Destination <select id="client-group-select"><option value="">— search first —</option></select></label> <label class="small">or custom chat ID <input id="client-chat-custom" size="22" placeholder="12036...@g.us"></label></p><p><span class="small">Purposes</span> <label class="small"><input type="checkbox" id="client-purpose-index" checked> index</label> <label class="small"><input type="checkbox" id="client-purpose-details" checked> details</label> <label class="small"><input type="checkbox" id="client-purpose-status" checked> status</label> <label class="small">Filter expression <input id="client-filters" size="30" placeholder="salud + insumos, -construccion"></label> <button onclick="addClientProfile()">Add to profiles</button></p></div><div class="subsection"><h3>Profiles (JSON)</h3><p class="small">Full list, editable by hand. Purposes: index, details, status, or all.</p><textarea id="waha-clients" rows="10" placeholder='[{{"name":"Client A","chat_id":"12036...@g.us","purposes":["index","details"],"filters":"salud + insumos, -construccion","enabled":true}}]'></textarea><p><button onclick="saveWahaClients()">Save client profiles</button></p></div></div><div class="card" data-tab="whatsapp"><h2>WAHA Directory Search</h2><p class="small">Search the complete WAHA directory by one or more words from a name or chat ID. Results filter live from a short-lived local cache, so typing does not repeatedly download contacts, groups, communities and channels.</p><p><label class="small">Name or ID <input id="waha-search-q" size="40" placeholder="e.g. Chiriquí contratistas, 12036, @g.us" oninput="scheduleWahaSearch()" onkeydown="if (event.key === 'Enter') {{ event.preventDefault(); wahaSearch(); }}"></label> <button onclick="wahaSearch()">Search</button> <button onclick="wahaSearch(true)">Refresh directory</button> <span id="waha-search-state" class="small"></span></p><div id="waha-search-results" class="small"></div></div>
<div class="card" data-tab="whatsapp"><h2>WhatsApp message formats</h2><p class="small">Customize the text of each message family, including system health / worker messages with {{{{placeholder}}}} fields (unknown placeholders stay literal). <label class="small">Format <select id="fmt-kind" onchange="loadWahaFormat()"><option value="index" selected>Index alert</option><option value="details">Detail follow-up</option><option value="status">Status change</option><option value="system">System / health</option><option value="summary">Final summary</option></select></label> <button onclick="previewWahaFormat()">Preview</button> <button onclick="saveWahaFormat()">Save format</button> <button onclick="resetWahaFormat()">Reset to default</button> <span id="fmt-state" class="small"></span></p><textarea id="fmt-template" rows="8" style="width:100%; box-sizing:border-box"></textarea><p class="small" id="fmt-placeholders"></p><pre id="fmt-preview" style="max-height: 300px"></pre></div>
<div class="card" data-tab="settings"><h2>Settings</h2><details class="adv-settings" open><summary class="small">Collector, timer &amp; storage settings (apply on the next run/launch)</summary><h3>Storage paths</h3><div class="settings-grid"><label class="small">Records folder <input id="records-dir" size="42"></label> <label class="small">Calendar packages <input id="calendar-dir" size="42"></label> <label class="small">Test sandbox <input id="records-test-dir" size="42"></label> <button onclick="savePathSettings()">Save paths</button></div><h3>Run cadence</h3><div class="settings-grid"><label class="small">Auto-run source <select id="set-PC_AUTORUN_SOURCE"><option value="changedetection">changedetection webhook</option><option value="cron">manual cron</option></select></label><label class="small">Next-run interval (min) <input id="set-PC_NEXT_RUN_INTERVAL_MINUTES" size="5"></label> <label class="small">Cron index page cap <input id="set-PC_CRON_INDEX_LIMIT" size="5"></label> <label class="small">Cron detail limit (0 = all) <input id="set-PC_CRON_DETAIL_LIMIT" size="5"></label> <label class="small">Webhook index page cap <input id="set-PC_WEBHOOK_INDEX_LIMIT" size="5"></label> <label class="small">Webhook detail limit (0 = all) <input id="set-PC_WEBHOOK_DETAIL_LIMIT" size="5"></label> <label class="small">Test-zone records <input id="set-PC_TEST_ZONE_LIMIT" size="5"></label> <label class="small">Monitor stale sec <input id="set-PC_MONITOR_STALE_SECONDS" size="5"></label> <label class="small">Deadline 'soon' days <input id="set-PC_MONITOR_DEADLINE_SOON_DAYS" size="5"></label></div><p class="small"><label><input type="checkbox" id="source-changedetection-active" disabled> changedetection/webhook active</label> <label><input type="checkbox" id="source-cron-active" disabled> cron active</label> <span id="autorun-source-note"></span></p><h3>Timer window</h3><div class="settings-grid"><label class="small">Timer width <input id="set-PC_NEXT_RUN_TIMER_WIDTH" size="5"></label> <label class="small">Timer height <input id="set-PC_NEXT_RUN_TIMER_HEIGHT" size="5"></label> <label class="small">Timer top <input id="set-PC_NEXT_RUN_TIMER_TOP" size="5"></label> <label class="small">Timer latest records <input id="set-PC_NEXT_RUN_TIMER_RECORDS" size="5"></label> <label class="small">Timer data refresh sec <input id="set-PC_NEXT_RUN_TIMER_DATA_REFRESH_SECONDS" size="5"></label></div><h3>Integrations</h3><div class="settings-grid"><label class="small">Monitor bind host <input id="set-PC_MONITOR_HOST" size="16" placeholder="127.0.0.1 or 0.0.0.0"></label><label class="small">changedetection URL <input id="set-CHANGEDETECTION_BASE_URL" size="24"></label> <label class="small">Webhook listener port <input id="set-PC_WEBHOOK_PORT" size="6"></label> <label class="small">Webhook public host <input id="set-PC_WEBHOOK_PUBLIC_HOST" size="22"></label><button onclick="saveAdvancedSettings()">Save settings</button></div><p class="xs">Auto-run source is exclusive: cron active disables webhook collection; changedetection active disables cron collection. Use <code>src/20_pipeline/115-cron-run.sh</code> from crontab.</p><p><label class="small"><input type="checkbox" id="set-PC_TEST_ZONE_AUTORUN" onchange="saveMonitorSetting('PC_TEST_ZONE_AUTORUN', this.checked ? '1' : '0')"> Auto-run test zone when no new records</label> <label class="small"><input type="checkbox" id="set-PC_RUN_UPDATE_BEFORE_RUN" onchange="saveMonitorSetting('PC_RUN_UPDATE_BEFORE_RUN', this.checked ? '1' : '0')"> Update local copy before each run</label> <label class="small" title="OFF = manual mode: the webhook listener keeps running but ignores incoming changedetection triggers instead of starting a run."><input type="checkbox" id="set-PC_WEBHOOK_AUTO_RUN" onchange="saveMonitorSetting('PC_WEBHOOK_AUTO_RUN', this.checked ? '1' : '0')"> Automatic runs from changedetection (webhook)</label></p></details></div>
<div class="card" data-tab="settings"><h2>Work templates</h2><p class="small">Reusable work files copied into <code>templates/</code> inside each record folder. Set the source folder, tick the files to use, save the selection. Records downloaded in each run receive them automatically; files already inside a record are never overwritten. Same source/selection as <code>pcc templates</code> and the native monitor.</p><p><label class="small">Source folder <input id="set-PC_TEMPLATES_SRC_DIR" size="42" placeholder="blank = var/templates"></label> <button onclick="saveTemplatesSource()">Save source</button> <button onclick="loadTemplates()">Refresh files</button> <button onclick="saveTemplatesSelection()">Save selection</button> <button onclick="runAction('Apply work templates')">Apply to all records</button></p><div id="templates-files" class="small">Loading template files…</div></div>
<div class="card" data-tab="settings"><h2>Webhook trigger access</h2><p class="small">The trigger token is generated automatically by setup (<code>docker stack up</code> writes <code>.webhook_token</code> when missing) and read here LIVE, so after an update or a re-run of setup this panel always shows the current values. Paste the Docker-to-host <code>json://host.docker.internal</code> URL into changedetection. Use <code>json://webhook</code> only when changedetection and webhook are in this same compose stack/network.</p><pre id="webhook-access">Loading webhook access…</pre><p><button onclick="loadWebhookAccess()">Refresh webhook access</button> <button onclick="runAction('Docker stack status')">Docker stack status</button></p></div>
<div class="card" data-tab="settings"><h2>Reset / review from zero</h2><p class="small">Separate actions, from a soft detail re-queue to a full wipe. The two destructive wipes ask for confirmation first. Each runs src/50_tools/110-reset.py; check the current action log and refresh the DB snapshot above to verify.</p><p><button onclick="runReset('requeue-details')">Re-queue all details</button><button onclick="runReset('reset-notify')">Reset notify / review flags</button><button class="danger" onclick="runReset('wipe-db')">Wipe database only</button><button class="danger" onclick="runReset('wipe-all')">Wipe EVERYTHING</button></p><p id="reset-status" class="small"></p></div>
<script>
let doneSince = null;
let sawActive = false;
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
  document.getElementById('worker-log').textContent = data.worker_log || '(no recent worker log lines)';
  document.getElementById('current-log').textContent = data.current_log || '(no current action log lines)';
  const waha = document.getElementById('waha-message');
  if (waha && document.activeElement !== waha) waha.value = data.waha_chat_id || '';
  const wahawa = document.getElementById('waha-message-wa'); if (wahawa && document.activeElement !== wahawa) wahawa.value = data.waha_chat_id || '';
  const clientsBox = document.getElementById('waha-clients');
  if (clientsBox && document.activeElement !== clientsBox) clientsBox.value = data.waha_clients || '[]';
  [['waha-index','waha_chat_id_index'],['waha-details','waha_chat_id_details'],['waha-status','waha_chat_id_status'],['waha-open-now','waha_chat_id_open_now'],['waha-system','waha_chat_id_system'],['waha-summary','waha_chat_id_summary']].forEach(([id, key]) => {{
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
  ['PC_WAHA_SOURCE','PC_AUTORUN_SOURCE','PC_CRON_INDEX_LIMIT','PC_CRON_DETAIL_LIMIT','PC_MONITOR_HOST','PC_NEXT_RUN_INTERVAL_MINUTES','PC_MONITOR_DEADLINE_SOON_DAYS','PC_WEBHOOK_INDEX_LIMIT','PC_WEBHOOK_DETAIL_LIMIT','PC_NOTIFY_WITHIN_DAYS','PC_WAHA_RETRIES','PC_WAHA_SEND_DELAY_SECONDS','PC_NOTIFY_INDEX_DIGEST_THRESHOLD','PC_NOTIFY_IDLE_EVERY_HOURS','PC_WAHA_BASE_URL','PC_WAHA_SESSION','PC_WAHA_NOTIFY_EVENTS','PC_TEST_ZONE_LIMIT','PC_MONITOR_STALE_SECONDS','PC_NEXT_RUN_TIMER_WIDTH','PC_NEXT_RUN_TIMER_HEIGHT','PC_NEXT_RUN_TIMER_TOP','PC_NEXT_RUN_TIMER_RECORDS','PC_NEXT_RUN_TIMER_DATA_REFRESH_SECONDS','CHANGEDETECTION_BASE_URL','PC_WEBHOOK_PORT','PC_WEBHOOK_PUBLIC_HOST','WAHA_PORT','WAHA_API_KEY','WAHA_DASHBOARD_USERNAME','WAHA_DASHBOARD_PASSWORD','PC_TEMPLATES_SRC_DIR'].forEach(key => {{ const el = document.getElementById('set-' + key); if (el && document.activeElement !== el && settings[key] !== undefined) el.value = settings[key]; }});
  updateAutorunSourceIndicators(settings);
  [['PC_WAHA_ENABLED','0'],['PC_NOTIFY_SKIP_EXPIRED','0'],['PC_NOTIFY_DETAILS_INLINE','1'],['PC_INDEX_FROM_SNAPSHOT','1'],['PC_TEST_ZONE_AUTORUN','0'],['PC_RUN_UPDATE_BEFORE_RUN','1'],['PC_WEBHOOK_AUTO_RUN','1']].forEach(([key, dflt]) => {{ const el = document.getElementById('set-' + key); if (el && document.activeElement !== el) el.checked = String(settings[key] ?? dflt) === '1'; }});
  const cronEnabledEl = document.getElementById('cron-enabled');
  if (cronEnabledEl && document.activeElement !== cronEnabledEl) cronEnabledEl.checked = String(settings.PC_AUTORUN_SOURCE ?? 'changedetection') === 'cron';
  const cronDaysRadio = document.querySelector(`input[name="cron-days"][value="${{settings.PC_CRON_DAYS || 'daily'}}"]`);
  if (cronDaysRadio && document.activeElement?.name !== 'cron-days') cronDaysRadio.checked = true;
  [['cron-custom-days', 'PC_CRON_CUSTOM_DAYS'], ['cron-start', 'PC_CRON_START_TIME'], ['cron-end', 'PC_CRON_END_TIME'], ['cron-interval', 'PC_CRON_INTERVAL_MINUTES']].forEach(([id, key]) => {{
    const el = document.getElementById(id);
    if (el && document.activeElement !== el && settings[key] !== undefined) el.value = settings[key];
  }});
  const note = document.getElementById('done-note');
  if (data.done) {{
    if (!sawActive) {{
      // Opened straight into a pre-existing idle/done state (e.g. right after
      // an update with no run queued): show status, but never start the
      // countdown, or the tab would close before any work runs. Mirrors the
      // saw_active guard in 001a-monitor-tk.py / 001c-monitor-terminal.sh.
      note.hidden = false;
      note.textContent = 'Idle. Auto-close starts only after a run finishes while this monitor is open.';
      return;
    }}
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
    sawActive = true;
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
  const detailLimit = encodeURIComponent(document.getElementById('detail-limit').value || '0');
  const indexLimit = encodeURIComponent(document.getElementById('index-limit').value || '0');
  postForm('/api/request-run', `mode=${{mode}}&detail_limit=${{detailLimit}}&index_limit=${{indexLimit}}`);
}}
function importCalendars() {{ postForm('/api/import-calendars', ''); }}
function stopRun() {{ postForm('/api/manual-action', 'label=' + encodeURIComponent('Stop active run')); }}
function stopAll() {{
  if (!window.confirm('This stops EVERYTHING: workers, test zone, calendar builder, monitors, webhook listener and updaters. This monitor closes too. Continue?')) return;
  postForm('/api/manual-action', 'label=' + encodeURIComponent('STOP all runners'));
}}
function startAll() {{ postForm('/api/manual-action', 'label=' + encodeURIComponent('START all infrastructure')); }}
function devPause() {{ postForm('/api/manual-action', 'label=' + encodeURIComponent('Pause for development')); }}
function devResume() {{ postForm('/api/manual-action', 'label=' + encodeURIComponent('Resume automatic collection')); }}
async function applyCronSchedule() {{
  const status = document.getElementById('cron-schedule-status');
  const enabled = document.getElementById('cron-enabled').checked;
  const days = document.querySelector('input[name="cron-days"]:checked').value;
  const customDays = document.getElementById('cron-custom-days').value.trim();
  const start = document.getElementById('cron-start').value.trim() || '08:00';
  const end = document.getElementById('cron-end').value.trim() || '18:00';
  const interval = document.getElementById('cron-interval').value.trim() || '30';
  status.textContent = 'Applying...';
  const body = `enabled=${{enabled ? '1' : '0'}}&days=${{encodeURIComponent(days)}}&custom_days=${{encodeURIComponent(customDays)}}&start=${{encodeURIComponent(start)}}&end=${{encodeURIComponent(end)}}&interval=${{encodeURIComponent(interval)}}`;
  try {{
    const res = await fetch('/api/cron-schedule', {{method: 'POST', headers: {{'Content-Type': 'application/x-www-form-urlencoded'}}, body}});
    status.textContent = await res.text();
  }} catch (e) {{ status.textContent = 'Request failed: ' + e; }}
}}
async function refreshCronScheduleStatus() {{
  const status = document.getElementById('cron-schedule-status');
  try {{
    const res = await fetch('/api/cron-schedule?action=show', {{cache: 'no-store'}});
    status.textContent = await res.text();
  }} catch (e) {{ status.textContent = 'Request failed: ' + e; }}
}}
async function refreshChangedetectionSchedule() {{
  const banner = document.getElementById('cd-schedule-banner');
  const summary = document.getElementById('cd-schedule-summary');
  const table = document.getElementById('cd-schedule-table');
  try {{
    const res = await fetch('/api/changedetection-schedule', {{cache: 'no-store'}});
    const data = await res.json();
    const bits = [];
    if (data.dev_mode_active) bits.push('<span style="color:#fca5a5;font-weight:700">⏸ Dev mode is ON — all automatic triggers are paused.</span>');
    if (!data.webhook_auto_run) bits.push('<span style="color:#fca5a5;font-weight:700">⚠ Automatic runs from changedetection (webhook) is OFF — new changedetection changes will NOT start a run or send WhatsApp messages.</span>');
    else bits.push('<span style="color:#86efac">✓ Automatic runs from changedetection (webhook) is ON.</span>');
    banner.innerHTML = bits.join(' ');
    if (data.error) {{
      summary.textContent = data.error;
      table.innerHTML = '';
      return;
    }}
    summary.textContent = `Source: ${{data.source || '—'}} · ${{(data.watches || []).length}} watch(es) · ${{data.schedule_note || 'no schedule window set (checks any time)'}}`;
    const rows = (data.watches || []).map(w => `<tr><td>${{esc(w.title || w.uuid || '')}}</td><td>${{w.paused ? '⏸ paused' : '● active'}}</td><td>${{esc(w.last_checked || '—')}}</td><td>${{Math.round((w.interval_seconds || 0) / 60)}} min${{w.uses_default_schedule ? '' : ' (override)'}}</td><td>${{esc(w.next_check || '—')}}</td></tr>`).join('');
    table.innerHTML = `<tr><th>Watch</th><th>Status</th><th>Last checked</th><th>Interval</th><th>Next check</th></tr>${{rows || '<tr><td colspan="5">No watches found.</td></tr>'}}`;
  }} catch (e) {{ summary.textContent = 'Request failed: ' + e; }}
}}
function runAction(label) {{ postForm('/api/manual-action', 'label=' + encodeURIComponent(label)); }}
const ZONE_DESCRIPTIONS = {{
  'Runners': 'Start, queue or stop collection runs.',
  'Integrations': 'Docker stack, changedetection and WAHA dashboards.',
  'Tests': 'Diagnostics, reports and sandbox runs.',
  'Settings': 'Maintenance, repair and configuration helpers.',
}};
function renderActionZones() {{
  const root = document.getElementById('action-zones');
  const zones = [...new Set(actionZones.map(a => a.zone))];
  root.innerHTML = zones.map(zone => `<div class="zone"><h3>${{esc(zone)}}</h3><div class="zone-desc small">${{esc(ZONE_DESCRIPTIONS[zone] || '')}}</div>` + actionZones.filter(a => a.zone === zone).map(a => `<button onclick="runAction('${{esc(a.label)}}')">${{esc(a.label)}}</button><span class="small">${{esc(a.comment)}}</span><br>`).join('') + `</div>`).join('');
}}
function saveWaha() {{ const val = (id, fallback) => ((document.getElementById(id) || document.getElementById(fallback) || {{value:''}}).value); const v = (id, fallback) => encodeURIComponent(val(id, fallback)); postForm('/api/waha-destination', 'chat_id=' + v('waha-message', 'waha-message-wa') + '&chat_id_index=' + v('waha-index', 'waha-index-wa') + '&chat_id_details=' + v('waha-details', 'waha-details-wa') + '&chat_id_status=' + v('waha-status', 'waha-status-wa') + '&chat_id_open_now=' + v('waha-open-now', 'waha-open-now-wa') + '&chat_id_system=' + v('waha-system', 'waha-system-wa') + '&chat_id_summary=' + v('waha-summary', 'waha-summary-wa')); }}
function syncWhatsappMirror(source) {{
  const pairs = [['waha-message','waha-message-wa'],['waha-index','waha-index-wa'],['waha-details','waha-details-wa'],['waha-status','waha-status-wa'],['waha-open-now','waha-open-now-wa'],['waha-system','waha-system-wa'],['waha-summary','waha-summary-wa']];
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
    [['flt-global','global'],['flt-index','index'],['flt-details','details'],['flt-status','status'],['flt-open-now','open_now']].forEach(([id, key]) => {{
      const el = document.getElementById(id);
      if (el && document.activeElement !== el) el.value = data[key] || '';
    }});
  }} catch (err) {{ /* filters card stays editable */ }}
}}
function saveWahaFilters() {{
  const v = id => encodeURIComponent((document.getElementById(id) || {{value:''}}).value);
  postForm('/api/waha-filters', 'global=' + v('flt-global') + '&index=' + v('flt-index') + '&details=' + v('flt-details') + '&status=' + v('flt-status') + '&open_now=' + v('flt-open-now'));
  setTimeout(loadWahaFilters, 400);
}}
function saveWahaClients() {{
  const box = document.getElementById('waha-clients');
  postForm('/api/waha-clients', 'clients=' + encodeURIComponent(box ? box.value : '[]'));
}}
let wahaSearchTimer = null;
let wahaLastMatches = [];
function scheduleWahaSearch() {{
  if (wahaSearchTimer) window.clearTimeout(wahaSearchTimer);
  wahaSearchTimer = window.setTimeout(() => wahaSearch(false), 300);
}}
async function wahaSearch(refresh = false) {{
  const q = (document.getElementById('waha-search-q').value || '').trim();
  const state = document.getElementById('waha-search-state');
  const box = document.getElementById('waha-search-results');
  state.textContent = refresh ? 'Refreshing the complete WAHA directory…' : 'Searching WAHA…';
  try {{
    const url = '/api/waha-search?q=' + encodeURIComponent(q) + (refresh ? '&refresh=1' : '');
    const response = await fetch(url, {{cache: 'no-store'}});
    if (!response.ok) throw new Error(`HTTP ${{response.status}}`);
    const data = await response.json();
    if (data.error) {{ state.textContent = data.error; box.innerHTML = ''; return; }}
    const sess = (data.sessions || []).map(s => `${{esc(s.name)}} (${{esc(s.status || '?')}})`).join(', ') || 'none';
    wahaLastMatches = data.matches || [];
    state.textContent = `Sessions: ${{sess}} · ${{wahaLastMatches.length}} match(es) from ${{data.directory_count || 0}} destinations${{data.cached ? ' (cached)' : ''}}`;
    box.innerHTML = wahaLastMatches.map((m, index) =>
      `<div>[${{esc(m.kind)}} · session ${{esc(m.session)}}] <b>${{esc(m.name)}}</b> — <code>${{esc(m.id)}}</code> ` +
      `<button onclick="useWahaMatchIndex(${{index}})">Use</button></div>`
    ).join('') || '<div>No matches.</div>';
    const select = document.getElementById('client-group-select');
    select.replaceChildren(new Option('— pick a destination —', ''));
    wahaLastMatches.forEach(m => select.add(new Option(`[${{m.kind}}] ${{m.name}} (${{m.id}})`, m.id)));
  }} catch (err) {{ state.textContent = 'Search failed: ' + err; }}
}}
function useWahaMatchIndex(index) {{
  const match = wahaLastMatches[index];
  if (match) useWahaMatch(match.id, match.name);
}}
function useWahaMatch(id, name) {{
  const custom = document.getElementById('client-chat-custom');
  custom.value = id;
  const nameBox = document.getElementById('client-name');
  if (!nameBox.value.trim()) nameBox.value = name;
  document.getElementById('waha-search-state').textContent = 'Chat ID copied into the client form: ' + id;
}}
function addClientProfile() {{
  const chatId = (document.getElementById('client-chat-custom').value || '').trim() ||
                 (document.getElementById('client-group-select').value || '').trim();
  if (!chatId) {{ document.getElementById('waha-search-state').textContent = 'Pick a group or type a chat ID first.'; return; }}
  const purposes = ['index', 'details', 'status'].filter(p => document.getElementById('client-purpose-' + p).checked);
  const profile = {{
    name: (document.getElementById('client-name').value || '').trim() || chatId,
    chat_id: chatId,
    purposes: purposes.length ? purposes : ['index', 'details', 'status'],
    filters: (document.getElementById('client-filters').value || '').trim(),
    enabled: true,
  }};
  const box = document.getElementById('waha-clients');
  let list = [];
  try {{ list = JSON.parse(box.value || '[]'); }} catch (err) {{ list = []; }}
  if (!Array.isArray(list)) list = [];
  const existing = list.findIndex(c => c && c.chat_id === chatId);
  if (existing >= 0) list[existing] = profile; else list.push(profile);
  box.value = JSON.stringify(list, null, 2);
  saveWahaClients();
  document.getElementById('waha-search-state').textContent = `Client "${{profile.name}}" saved (${{chatId}}).`;
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
  const keyword = (document.getElementById('cal-filter') || {{}}).value || '';
  let params = 'view=' + view + '&field=' + field;
  if (anchor) params += '&date=' + encodeURIComponent(anchor);
  if (shift) params += '&shift=' + shift;
  if (keyword.trim()) params += '&filter=' + encodeURIComponent(keyword.trim());
  try {{
    const response = await fetch('/api/calendar?' + params, {{cache: 'no-store'}});
    const data = await response.json();
    calendarAnchor = data.anchor;
    if (document.activeElement !== dateBox) dateBox.value = data.anchor;
    document.getElementById('calendar-text').textContent = data.text;
    renderCalendarVisual();
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
  ['PC_WAHA_SOURCE','PC_AUTORUN_SOURCE','PC_CRON_INDEX_LIMIT','PC_CRON_DETAIL_LIMIT','PC_MONITOR_HOST','PC_NEXT_RUN_INTERVAL_MINUTES','PC_MONITOR_DEADLINE_SOON_DAYS','PC_WEBHOOK_INDEX_LIMIT','PC_WEBHOOK_DETAIL_LIMIT','PC_NOTIFY_WITHIN_DAYS','PC_WAHA_RETRIES','PC_WAHA_SEND_DELAY_SECONDS','PC_NOTIFY_INDEX_DIGEST_THRESHOLD','PC_NOTIFY_IDLE_EVERY_HOURS','PC_WAHA_BASE_URL','PC_WAHA_SESSION','PC_WAHA_NOTIFY_EVENTS','PC_TEST_ZONE_LIMIT','PC_MONITOR_STALE_SECONDS','PC_NEXT_RUN_TIMER_WIDTH','PC_NEXT_RUN_TIMER_HEIGHT','PC_NEXT_RUN_TIMER_TOP','PC_NEXT_RUN_TIMER_RECORDS','PC_NEXT_RUN_TIMER_DATA_REFRESH_SECONDS','CHANGEDETECTION_BASE_URL','PC_WEBHOOK_PORT','PC_WEBHOOK_PUBLIC_HOST','WAHA_PORT','WAHA_API_KEY','WAHA_DASHBOARD_USERNAME','WAHA_DASHBOARD_PASSWORD'].forEach(key => {{ const el = document.getElementById('set-' + key); if (el) saveMonitorSetting(key, el.value); }});
}}
function updateAutorunSourceIndicators(settings) {{
  const source = String((settings || {{}}).PC_AUTORUN_SOURCE || 'changedetection').toLowerCase();
  const changedetection = source !== 'cron';
  const cdBox = document.getElementById('source-changedetection-active');
  const cronBox = document.getElementById('source-cron-active');
  if (cdBox) cdBox.checked = changedetection;
  if (cronBox) cronBox.checked = !changedetection;
  const note = document.getElementById('autorun-source-note');
  if (note) note.textContent = changedetection
    ? 'changedetection/webhook is the automatic collector source; cron collection is ignored.'
    : 'cron is the automatic collector source; changedetection/webhook collection is ignored.';
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
function fmtCompactDate(raw) {{
  let text = (raw || '').trim().replace('T', ' ').replace('_', ' ');
  if (!text) return '—';
  // Convert any 12h 'HH:MM AM/PM' fragment to 24h so display is uniform.
  text = text.replace(/(\\d{{1,2}}):(\\d{{2}})(?::\\d{{2}})?\\s*([AaPp])\\.?\\s*[Mm]\\.?/, (s, h, mn, ap) => {{
    let hh = parseInt(h, 10);
    if (ap.toLowerCase() === 'p' && hh !== 12) hh += 12;
    if (ap.toLowerCase() === 'a' && hh === 12) hh = 0;
    return `${{String(hh % 24).padStart(2, '0')}}:${{mn}}`;
  }});
  const m = text.match(/^(\\d{{4}})-(\\d{{2}})-(\\d{{2}})(?:[ _T](\\d{{2}}):(\\d{{2}}))?/);
  if (!m) return text;
  return (m[4] && m[5]) ? `${{m[1]}}-${{m[2]}}-${{m[3]}}_${{m[4]}}-${{m[5]}}` : `${{m[1]}}-${{m[2]}}-${{m[3]}}`;
}}
function deadlineText(rec) {{ return parseDeadline(rec) ? fmtCompactDate(rec.finish_date_guess) : '—'; }}
function startText(rec) {{ const raw = (rec.start_date_guess || '').trim(); return raw ? fmtCompactDate(raw) : '—'; }}
function downloadedText(rec) {{ const raw = (rec.detail_saved_at || '').trim(); return raw ? fmtCompactDate(raw) : '—'; }}
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
const WEEKDAY_LABELS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
function calendarEventClass(ev) {{
  const st = String(ev.status || '').toLowerCase();
  if (st.includes('venc') || st.includes('expired')) return 'expired';
  const when = parseIsoLike(ev.date || '');
  if (when) {{
    const now = new Date();
    if (when < now) return 'expired';
    if (when <= new Date(now.getTime() + RECORD_SOON_DAYS * 86400000)) return 'soon';
  }}
  return '';
}}
function renderCalendarEvent(ev) {{
  const title = (ev.numero ? ev.numero + ' · ' : '') + (ev.description || '(sin descripcion)');
  const clock = ev.clock && ev.clock !== '--:--' ? ev.clock + ' ' : '';
  return `<span class="calevent ${{calendarEventClass(ev)}}" title="${{esc(title)}}">${{esc(clock + title)}}</span>`;
}}
function renderCalendarDayCell(day, events, blank, maxShown) {{
  if (blank) return '<div class="calcell blank"></div>';
  const limit = maxShown || 4;
  const shown = (events || []).slice(0, limit).map(renderCalendarEvent).join('');
  const more = (events || []).length > limit ? `<span class="calevent more">+${{events.length - limit}} more</span>` : '';
  const classes = ['calcell'];
  if (day.iso === day.today) classes.push('today');
  return `<div class="${{classes.join(' ')}}" onclick="document.getElementById('cal-date').value='${{day.iso}}'; document.getElementById('cal-view').value='day'; loadCalendar()"><div class="num"><span>${{day.label}}</span><span class="count">${{events.length || ''}}</span></div>${{shown}}${{more}}</div>`;
}}
async function renderCalendarVisual() {{
  const node = document.getElementById('calendar-visual');
  if (!node) return;
  const field = (document.getElementById('cal-field') || {{}}).value || 'end';
  const view = (document.getElementById('cal-view') || {{}}).value || 'month';
  const anchor = ((document.getElementById('cal-date') || {{}}).value || calendarAnchor || '').trim();
  const keyword = ((document.getElementById('cal-filter') || {{}}).value || '').trim();
  let params = 'field=' + encodeURIComponent(field) + '&view=' + encodeURIComponent(view);
  if (anchor) params += '&date=' + encodeURIComponent(anchor);
  if (keyword) params += '&filter=' + encodeURIComponent(keyword);
  try {{
    const g = await (await fetch('/api/calendar-grid?' + params, {{cache: 'no-store'}})).json();
    const grouped = g.events || {{}};
    const title = `${{g.label || view}} · ${{g.start}} to ${{g.end}} · ${{g.total || 0}} opportunities`;
    if (view === 'year') {{
      const peak = Math.max(1, ...Object.values(g.month_counts || {{}}).map(Number));
      const boxes = (g.months || []).map(m => {{
        const count = Number((g.month_counts || {{}})[m.value] || 0);
        const width = Math.round(100 * count / peak);
        return `<div class="month-box" onclick="document.getElementById('cal-date').value='${{m.value}}-01'; document.getElementById('cal-view').value='month'; loadCalendar()"><b>${{esc(m.label)}}</b><div class="small">${{count}} opportunities</div><div class="bar-track"><div class="bar-fill" style="width:${{width}}%"></div></div></div>`;
      }}).join('');
      node.innerHTML = `<div class="calendar-board"><div class="calendar-title"><h3>${{esc(title)}}</h3><span class="small">Click a month to open it.</span></div><div class="year-grid">${{boxes}}</div></div>`;
      return;
    }}
    const days = g.days || [];
    const hasTime = ev => ev.clock && ev.clock !== '--:--';
    const hourOf = ev => parseInt(ev.clock.slice(0, 2), 10) || 0;
    if (view === 'day') {{
      // Hourly timeline: one row per hour (00:00-23:00) with events placed in
      // their hour's lane; events without a known time get a "No time" row.
      const day = days[0] || {{}};
      const evs = grouped[day.iso] || [];
      const notime = evs.filter(ev => !hasTime(ev));
      const byHour = Array.from({{length: 24}}, () => []);
      evs.forEach(ev => {{ if (hasTime(ev)) byHour[hourOf(ev)].push(ev); }});
      const notimeRow = notime.length
        ? `<div class="hour-label timeline-notime">No time</div><div class="hour-lane timeline-notime">${{notime.map(renderCalendarEvent).join('')}}</div>` : '';
      const hourRows = byHour.map((evsAtHour, h) => `<div class="hour-label">${{String(h).padStart(2, '0')}}:00</div><div class="hour-lane">${{evsAtHour.map(renderCalendarEvent).join('')}}</div>`).join('');
      const note = evs.length ? 'Hourly agenda.' : 'No opportunities on this day.';
      node.innerHTML = `<div class="calendar-board day-agenda"><div class="calendar-title"><h3>${{esc(title)}}</h3><span class="small">${{note}}</span></div><div class="timeline-scroll"><div class="timeline">${{notimeRow}}${{hourRows}}</div></div></div>`;
      return;
    }}
    if (view === 'week') {{
      // Hourly timeline with one column per day: a time-label column plus 7
      // day columns, each split into 24 hour lanes so events line up by time.
      const cols = days.map((day, i) => {{
        const evs = grouped[day.iso] || [];
        const byHour = Array.from({{length: 24}}, () => []);
        evs.forEach(ev => {{ if (hasTime(ev)) byHour[hourOf(ev)].push(ev); }});
        return {{ i, iso: day.iso, notime: evs.filter(ev => !hasTime(ev)), byHour }};
      }});
      const head = '<div class="wk-corner"></div>' + cols.map(c => `<div class="wk-head">${{WEEKDAY_LABELS[c.i] || ''}} ${{esc((c.iso || '').slice(5))}}</div>`).join('');
      const anyNotime = cols.some(c => c.notime.length);
      const notimeRow = anyNotime
        ? '<div class="hour-label wk-notime">No time</div>' + cols.map(c => `<div class="hour-lane wk-notime">${{c.notime.map(renderCalendarEvent).join('')}}</div>`).join('') : '';
      let hourRows = '';
      for (let h = 0; h < 24; h++) {{
        hourRows += `<div class="hour-label">${{String(h).padStart(2, '0')}}:00</div>`;
        hourRows += cols.map(c => `<div class="hour-lane">${{c.byHour[h].map(renderCalendarEvent).join('')}}</div>`).join('');
      }}
      node.innerHTML = `<div class="calendar-board week-agenda"><div class="calendar-title"><h3>${{esc(title)}}</h3><span class="small">Click a date to open the day view.</span></div><div class="timeline-scroll"><div class="week-timeline">${{head}}${{notimeRow}}${{hourRows}}</div></div></div>`;
      return;
    }}
    let cells = WEEKDAY_LABELS.map(d => `<div class="dow">${{d}}</div>`).join('');
    for (let i = 0; i < (g.first_weekday || 0); i++) cells += renderCalendarDayCell(null, [], true);
    cells += days.map(day => renderCalendarDayCell(day, grouped[day.iso] || [], false)).join('');
    node.innerHTML = `<div class="calendar-board"><div class="calendar-title"><h3>${{esc(title)}}</h3><span class="small">Click a date to open the day view.</span></div><div class="calgrid">${{cells}}</div></div>`;
  }} catch (err) {{ node.textContent = 'Graphical calendar unavailable: ' + err; }}
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
  if (tab === 'overview') refreshOverview();
  if (tab === 'records') refreshRecordIndex();
  if (tab === 'calendar') loadCalendar();
  if (tab === 'scheduler') {{ refreshCronScheduleStatus(); refreshChangedetectionSchedule(); }}
}}

async function refreshOverview() {{
  try {{
    const [st, db] = await Promise.all([
      (await fetch('/api/status', {{cache: 'no-store'}})).json(),
      (await fetch('/api/db-stats', {{cache: 'no-store'}})).json(),
    ]);
    const stamp = document.getElementById('overview-live-stamp');
    if (stamp) stamp.textContent = 'LIVE · ' + new Date().toLocaleTimeString();
    const last = st.last_summary || {{}};
    const progress = st.progress || {{}};
    const running = !st.done;
    const k = document.getElementById('overview-kpis');
    if (k) k.innerHTML = [
      ['Run state', running ? (progress.PHASE || 'RUNNING') : (progress.STATUS || 'IDLE')],
      ['Last completed', last.FINISHED_AT ? fmtCompactDate(last.FINISHED_AT) : '—'],
      ['Duration', last.TOTAL_TEXT || '—'],
      ['Index source', last.INDEX_SOURCE || '—'],
      ['New today', db.new_today || 0],
      ['Closing ≤' + (db.soon_days || 7) + 'd', db.closing_soon || 0],
      ['Abiertas', db.abiertas || 0],
      ['Alerts sent', db.notified || 0],
      ['Failed alerts', db.alerts_failed || 0],
    ].map(x => `<div class="kpi"><span>${{esc(String(x[0]))}}</span><b>${{esc(String(x[1]))}}</b></div>`).join('');
    const lastLine = document.getElementById('overview-last-run');
    if (lastLine) lastLine.textContent = last.FINISHED_AT
      ? `Started ${{last.STARTED_AT ? fmtCompactDate(last.STARTED_AT) : '?'}} · finished ${{fmtCompactDate(last.FINISHED_AT)}} · total ${{last.TOTAL_TEXT || last.TOTAL_SECONDS + 's'}} · index source: ${{last.INDEX_SOURCE || 'crawler'}}`
      : 'No completed run recorded yet — stage durations appear after the first clean run.';
    const stageRows = [
      ['Update', last.UPDATE_SECONDS], ['Index', last.INDEX_SECONDS], ['Messaging', last.MESSAGING_SECONDS],
      ['Details', last.DETAIL_SECONDS], ['Views', last.VIEW_SECONDS], ['Verify', last.VERIFY_SECONDS], ['Calendar', last.CALENDAR_SECONDS],
    ].map(r => ({{label: r[0], count: Number(r[1] || 0)}})).filter(r => r.count > 0);
    bars('overview-stages', stageRows);
    const p = st.processes || {{}};
    const q = st.queue || {{}};
    const settings = st.settings || {{}};
    const chip = (ok, onText, offText) => `<span style="color:${{ok ? '#86efac' : '#fca5a5'}};font-weight:700">${{ok ? onText : offText}}</span>`;
    const services = document.getElementById('overview-services');
    if (services) services.innerHTML =
      `Worker: ${{chip(p.worker, 'RUNNING', 'idle')}} · ` +
      `Webhook listener: ${{chip(p.webhook, 'RUNNING', 'OFF')}} · ` +
      `WhatsApp (WAHA): ${{chip(String(settings.PC_WAHA_ENABLED || '0') === '1', 'enabled', 'disabled')}} · ` +
      `Queue: ${{esc(q.collector_state || 'none')}}${{q.collector_state === 'PENDING' ? ' since ' + esc(q.collector_since || '?') : ''}} · ` +
      `Details pending: <b>${{db.pending || 0}}</b> · failed: <b>${{db.failed || 0}}</b>`;
  }} catch (err) {{
    const k = document.getElementById('overview-kpis');
    if (k) k.textContent = 'Overview unavailable: ' + err;
  }}
}}
function bars(nodeId, rows) {{
  const node = document.getElementById(nodeId);
  const max = Math.max(1, ...rows.map(r => Number(r.count || 0)));
  node.innerHTML = rows.length ? rows.map(r => `<div class="bar-row"><span>${{esc(String(r.label || r.status || r.grupo || '—').slice(0, 28))}}</span><span class="bar-track"><span class="bar-fill" style="display:block;width:${{Math.max(4, Number(r.count || 0) / max * 100)}}%"></span></span><b>${{r.count || 0}}</b></div>`).join('') : '<span class="small">No data yet.</span>';
}}
function kpiFilterParams() {{
  const days = (document.getElementById('kpi-days') || {{}}).value || '0';
  const grupo = ((document.getElementById('kpi-grupo') || {{}}).value || '').trim();
  const entidad = ((document.getElementById('kpi-entidad') || {{}}).value || '').trim();
  return 'days=' + encodeURIComponent(days) + '&grupo=' + encodeURIComponent(grupo) + '&entidad=' + encodeURIComponent(entidad);
}}
function resetKpiFilters() {{
  const days = document.getElementById('kpi-days'); if (days) days.value = '0';
  ['kpi-grupo', 'kpi-entidad'].forEach(id => {{ const el = document.getElementById(id); if (el) el.value = ''; }});
  refreshDecisionDashboard();
}}

async function loadChangedetectionScript() {{
  const box = document.getElementById('changedetection-script');
  const state = document.getElementById('cd-script-state');
  if (!box || (box.value && box.dataset.loaded === '1')) return;
  try {{
    const data = await (await fetch('/api/changedetection-script', {{cache: 'no-store'}})).text();
    box.value = data;
    box.dataset.loaded = '1';
    if (state) state.textContent = 'Loaded from config/changedetection-browser-steps.js';
  }} catch (err) {{
    if (state) state.textContent = 'Could not load script: ' + err;
  }}
}}
async function copyChangedetectionScript() {{
  await loadChangedetectionScript();
  const box = document.getElementById('changedetection-script');
  const state = document.getElementById('cd-script-state');
  if (!box) return;
  box.select();
  try {{
    await navigator.clipboard.writeText(box.value);
    if (state) state.textContent = 'Copied script to clipboard.';
  }} catch (err) {{
    document.execCommand('copy');
    if (state) state.textContent = 'Selected script; press Ctrl+C if it did not copy automatically.';
  }}
}}

async function loadWebhookAccess() {{
  const node = document.getElementById('webhook-access');
  if (!node) return;
  try {{
    const w = await (await fetch('/api/webhook-access', {{cache: 'no-store'}})).json();
    node.textContent =
      `Webhook token: ${{w.token_exists ? w.token : '(not generated yet — run ./setup.sh or ./src/50_tools/010-docker-stack.sh up)'}}\n` +
      `Token file:    ${{w.token_file}}\n` +
      `Listener:      ${{w.listener_running ? 'RUNNING' : 'off'}} on port ${{w.port}}\n\n` +
      `changedetection notification URL (Docker → host, recommended):\n  ${{w.changedetection_url}}\n` +
      `Compose-only URL (use only if changedetection can resolve host 'webhook'):\n  ${{w.compose_url}}\n` +
      `Docker container → host listener URL (plain HTTP test):\n  ${{w.docker_to_host_url}}\n` +
      `Local test from this machine:\n  ${{w.local_url}}\n\n` +
      `Generated by: ${{w.generated_by}}\n` +
      `Full access note (incl. WAHA login/API key): ${{w.access_note}}`;
  }} catch (err) {{ node.textContent = 'Webhook access unavailable: ' + err; }}
}}
async function refreshDecisionDashboard() {{
  const s = await (await fetch('/api/db-stats?' + kpiFilterParams(), {{cache: 'no-store'}})).json();
  const stamp = document.getElementById('kpi-live-stamp');
  if (stamp) stamp.textContent = 'LIVE · ' + new Date().toLocaleTimeString();
  const fstate = document.getElementById('kpi-filter-state');
  if (fstate) {{
    const f = s.filters || {{}};
    const parts = [];
    if (Number(f.days || 0) > 0) parts.push('last ' + f.days + ' days');
    if (f.grupo) parts.push('group ' + f.grupo);
    if (f.entidad) parts.push('entity ' + f.entidad);
    fstate.textContent = parts.length ? 'Filtered: ' + parts.join(' · ') : 'Showing all records';
  }}
  // Selector suggestions come from the CURRENT slice so drilling down stays easy.
  const grupoList = document.getElementById('kpi-grupo-list');
  if (grupoList) grupoList.innerHTML = (s.groups || []).map(g => `<option value="${{esc(g.grupo)}}">`).join('');
  const entidadList = document.getElementById('kpi-entidad-list');
  if (entidadList) entidadList.innerHTML = (s.entities || []).map(e => `<option value="${{esc(e.label)}}">`).join('');
  const k = document.getElementById('decision-kpis');
  const closure = Number(s.total || 0) ? Math.round(Number(s.saved || 0) / Number(s.total || 1) * 100) : 0;
  const items = s.item_analysis || {{}};
  k.innerHTML = [ ['Index archive total', s.total || 0], ['New today', s.new_today || 0], ['Closing ≤' + (s.soon_days || 7) + 'd', s.closing_soon || 0], ['Abiertas', s.abiertas || 0], ['Programadas', s.programadas || 0], ['Alerts sent', s.notified || 0], ['Failed alerts', s.alerts_failed || 0], ['Details saved', s.saved || 0], ['Details pending', s.pending || 0], ['Details failed', s.failed || 0], ['Item lines parsed', items.total_items || 0], ['Avg items / record', items.avg_items_per_record || 0], ['Deadline repairs', s.needs_deadline || 0], ['Detail closure', closure + '%'] ].map(x => `<div class="kpi"><span>${{x[0]}}</span><b>${{x[1]}}</b></div>`).join('');
  bars('decision-status', (s.status_breakdown || []).map(r => ({{label: r.status, count: r.count}})));
  bars('decision-groups', (s.groups || []).slice(0, 10).map(r => ({{label: r.grupo, count: r.count}})));
  bars('decision-daily', (s.daily_intake || []).slice(0, 14));
  bars('decision-trend', (s.monthly_trend || []).slice(0, 12));
  bars('decision-entities', (s.entities || []).slice(0, 10));
  bars('decision-locations', ((items.top_locations || []).length ? items.top_locations : (s.dependencias || [])).slice(0, 10));
  bars('decision-top-items', (items.top_items || []).slice(0, 10));
  const latestNode = document.getElementById('decision-latest-items');
  if (latestNode) {{
    const latest = items.sample_items || [];
    latestNode.innerHTML = latest.length
      ? latest.map(it => `<div class="item-line"><b>${{esc(it.numero || '?')}}</b>${{it.saved_at ? ' · ' + esc(it.saved_at) : ''}}${{it.cantidad ? ' · qty ' + esc(it.cantidad) : ''}}<br>${{esc(it.descripcion || '(item without description)')}}</div>`).join('')
      : '<span class="small">No parsed items yet — run a collection with detail downloads.</span>';
  }}
  bars('decision-deadlines', [{{label:'Completed', count:s.saved||0}},{{label:'Pending', count:s.pending||0}},{{label:'Failed', count:s.failed||0}},{{label:'Needs repair', count:s.needs_deadline||0}},{{label:'Notify backlog', count:s.notify_backlog||0}}]);
  bars('decision-items', [{{label:'Records sampled', count:items.sampled_records||0}},{{label:'Records with items', count:items.records_with_items||0}},{{label:'Total item lines', count:items.total_items||0}},{{label:'Largest record items', count:(items.max_items_record||{{}}).count||0}}]);
  const itemWords = (items.top_item_keywords || []).map(r => r.label || '').filter(Boolean);
  document.getElementById('decision-item-keywords').innerHTML = itemWords.length ? itemWords.map((w,i) => `<span style="font-size:${{0.85 + (itemWords.length-i)/18}}rem">${{esc(w)}}</span>`).join('') : '<span class="small">No parsed item keywords yet.</span>';
  const biggest = items.max_items_record || {{}};
  const topEntity = (s.entities || [])[0] || {{}};
  const topLocation = ((items.top_locations || [])[0]) || {{}};
  const sampleLines = (items.sample_items || []).slice(0, 5).map(it => `  - ${{it.numero || 'record'}}: ${{it.descripcion || '(item without description)'}}${{it.cantidad ? ' · qty ' + it.cantidad : ''}}`).join('\\n');
  document.getElementById('decision-recommendations').textContent = `Index/detail/items decision signals\n• If Details failed > 0, repair collector/detail issues before expanding index page caps.\n• If Details pending grows, prioritize detail download capacity over more index scans.\n• Item lines parsed: ${{items.total_items || 0}} across ${{items.records_with_items || 0}} records; largest record: ${{biggest.numero || '-'}} with ${{biggest.count || 0}} items.\n• Most active entity: ${{topEntity.label || '-'}} (${{topEntity.count || 0}} records)${{topLocation.label ? ' · most frequent location/unit: ' + topLocation.label + ' (' + (topLocation.count || 0) + ')' : ''}} — focus review capacity where the volume is.\n• If item keywords cluster around a buyer/product family, prioritize those folders for review and WhatsApp detail follow-up.\n• If Deadline repairs > 0, repair missing DTEND before calendar/export decisions.\n• If Notify backlog grows, verify WAHA destinations/settings before running more scans.\nRecent parsed items:\n${{sampleLines || '  - no item rows parsed yet'}}`;
}}
function initCollapsibleSections() {{
  // Every card except the live-progress header starts COLLAPSED so the monitor
  // opens compact; the operator expands only the panels they need (matches the
  // Tk monitor's default-hidden sections). Tab cards are also collapsed when
  // their tab is active, leaving only the section title and Show button visible.
  document.querySelectorAll('.card').forEach((card, idx) => {{
    const heading = card.querySelector('h1, h2');
    if (idx === 0) return;
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

function assignDedicatedTabs() {{
  document.querySelectorAll('.card[data-tab]').forEach(card => {{
    const heading = (card.querySelector('h2') || {{}}).textContent || '';
    if (heading.trim() === 'Opportunity calendar') card.dataset.tab = 'calendar';
  }});
}}

function renderQueue(data) {{
  const q = data.queue || {{}};
  document.getElementById('queue-summary').textContent =
    `Collector request: ${{q.collector_state || 'none'}} (since ${{q.collector_since || '-'}}) · ` +
    `Update + Monitor: ${{q.update_state || 'none'}} (since ${{q.update_since || '-'}})`;
  document.getElementById('queue-log').textContent =
    'Recent collector queue log:\\n' + (q.request_log || '(missing)') +
    '\\nRecent Update + Monitor queue log:\\n' + (q.update_log || '(missing)');
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
      const endDates = (s.completed_recent || []).slice(0, 3).map(r => `${{r.numero}} ends ${{r.finish_date_guess ? fmtCompactDate(r.finish_date_guess) : 'no date'}}`).join('; ') || 'No completed end dates yet';
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
    const recent = (s.recent || []).map(r => `  • ${{r.numero}} [${{r.detail_status || 'unknown'}}] — ${{(r.descripcion || '').slice(0, 120)}}`).join('\\n') || '  • —';
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
let webTimerTarget = null;
function tickWebTimer(text) {{
  const el = document.getElementById('web-timer-countdown');
  if (!el) return;
  if (text) {{ el.textContent = text; return; }}
  if (!webTimerTarget) return;
  const target = new Date(String(webTimerTarget).replace(' ', 'T'));
  if (isNaN(target)) return;
  const total = Math.max(0, Math.floor((target - new Date()) / 1000));
  const h = Math.floor(total / 3600), m = Math.floor((total % 3600) / 60), s = total % 60;
  el.textContent = (h ? h + 'h ' : '') + String(m).padStart(2, '0') + 'm ' + String(s).padStart(2, '0') + 's';
}}
async function refreshWebTimer() {{
  // Same schedule the Tk/CLI timers show (changedetection API or interval
  // fallback); between fetches the countdown ticks locally every second.
  try {{
    const t = await (await fetch('/api/web-timer', {{cache: 'no-store'}})).json();
    webTimerTarget = t.target || null;
    tickWebTimer(t.countdown || '');
  }} catch (err) {{ /* keep the last countdown ticking */ }}
}}
setInterval(() => tickWebTimer(''), 1000);
setInterval(refreshWebTimer, 60000);
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
let wahaQrTimer = null;
let wahaQrShown = false;
let wahaRecoveryAction = '';
function refreshWahaQr() {{
  const img = document.getElementById('waha-qr-img');
  if (img) img.src = '/api/waha-qr?t=' + Date.now();
}}
function toggleWahaQr() {{
  wahaQrShown = !wahaQrShown;
  document.getElementById('waha-alert-qr').hidden = !wahaQrShown;
  document.getElementById('waha-qr-toggle').textContent = wahaQrShown ? 'Hide QR' : 'Show QR to scan';
  if (wahaQrShown) {{
    refreshWahaQr();
    if (wahaQrTimer) clearInterval(wahaQrTimer);
    wahaQrTimer = setInterval(refreshWahaQr, 20000);
  }} else if (wahaQrTimer) {{
    clearInterval(wahaQrTimer);
    wahaQrTimer = null;
  }}
}}
async function checkWahaSession() {{
  try {{
    const data = await (await fetch('/api/waha-session-status', {{cache: 'no-store'}})).json();
    const banner = document.getElementById('waha-alert-banner');
    const recovery = document.getElementById('waha-recovery-action');
    wahaRecoveryAction = data.recommended_action || '';
    recovery.hidden = !wahaRecoveryAction;
    recovery.textContent = wahaRecoveryAction || '';
    if (data.connected) {{
      banner.hidden = true;
      if (wahaQrShown) toggleWahaQr();
    }} else {{
      banner.hidden = false;
      document.getElementById('waha-alert-text').textContent = data.message || ('status: ' + data.status);
      // A QR only exists while the session is actually waiting to be scanned;
      // otherwise (e.g. WAHA unreachable) hide the button so it doesn't 503.
      document.getElementById('waha-qr-toggle').hidden = !data.needs_qr;
      if (!data.needs_qr && wahaQrShown) toggleWahaQr();
    }}
  }} catch (err) {{ /* monitor's own connection problem; the main poll() already reports it */ }}
}}
function runWahaRecovery() {{
  if (!wahaRecoveryAction) return;
  runAction(wahaRecoveryAction);
  document.getElementById('waha-alert-text').textContent =
    'Recovery requested: ' + wahaRecoveryAction + '. Retrying shortly…';
  setTimeout(checkWahaSession, 6000);
}}
checkWahaSession();
setInterval(checkWahaSession, 20000);
window.addEventListener('beforeunload', () => {{ if (timer) clearTimeout(timer); if (wahaQrTimer) clearInterval(wahaQrTimer); }});
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
assignDedicatedTabs();
showTab('overview');
initCollapsibleSections();
refreshRecordIndex();
loadTemplates();
loadCalendar();
loadWahaFormat();
loadWahaFilters();
refreshDbReview();
refreshDecisionDashboard();
loadWebhookAccess();
refreshWebTimer();
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
            raw_detail = form.get("detail_limit", ["0"])[0].strip()
            raw_index = form.get("index_limit", ["0"])[0].strip()
            detail_limit = raw_detail if raw_detail.isdigit() else "0"
            index_limit = raw_index if raw_index.isdigit() and int(raw_index) >= 0 else "0"
            index_limit_text = "all" if index_limit == "0" else index_limit
            mode = form.get("mode", ["restart"])[0].strip().lower()
            if mode == "test":
                subprocess.Popen([str(BASE_DIR / "src/20_pipeline/070-test-zone.py"), "--limit", detail_limit, "--apply"], cwd=BASE_DIR, env=monitor_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.send_text(202, f"Test-zone run requested with detail limit {detail_limit}.\n", "text/plain; charset=utf-8")
                return
            if mode == "manual":
                subprocess.Popen([str(BASE_DIR / "src/20_pipeline/110b-run-now.sh"), detail_limit, index_limit, "MANUAL"], cwd=BASE_DIR, env=monitor_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.send_text(202, f"Manual run started with index page cap {index_limit_text}, detail limit {detail_limit}.\n", "text/plain; charset=utf-8")
                return
            subprocess.Popen([str(BASE_DIR / "src/20_pipeline/110a-request-run.sh"), detail_limit, "RESTART", index_limit], cwd=BASE_DIR, env=monitor_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
        if path == "/api/cron-schedule":
            enabled = form.get("enabled", ["0"])[0] == "1"
            days = form.get("days", ["daily"])[0].strip() or "daily"
            custom_days = form.get("custom_days", [""])[0].strip()
            start = form.get("start", ["08:00"])[0].strip() or "08:00"
            end = form.get("end", ["18:00"])[0].strip() or "18:00"
            interval = form.get("interval", ["30"])[0].strip() or "30"
            for key, value in (
                ("PC_CRON_DAYS", days), ("PC_CRON_CUSTOM_DAYS", custom_days),
                ("PC_CRON_START_TIME", start), ("PC_CRON_END_TIME", end),
                ("PC_CRON_INTERVAL_MINUTES", interval),
            ):
                save_monitor_setting(key, value)
            save_monitor_setting("PC_AUTORUN_SOURCE", "cron" if enabled else "changedetection")

            script = str(BASE_DIR / "src/50_tools/160-manage-cron-schedule.py")
            if not enabled:
                result = subprocess.run([script, "remove"], cwd=BASE_DIR, env=monitor_env(), capture_output=True, text=True)
                self.send_text(200, (result.stdout.strip() or result.stderr.strip() or "Schedule disabled.") + "\n", "text/plain; charset=utf-8")
                return
            args = [script, "install", "--days", days, "--custom-days", custom_days, "--start", start, "--end", end, "--interval", interval]
            result = subprocess.run(args, cwd=BASE_DIR, env=monitor_env(), capture_output=True, text=True)
            if result.returncode != 0:
                self.send_text(400, f"NOT applied: {result.stderr.strip() or result.stdout.strip()}\n", "text/plain; charset=utf-8")
            else:
                self.send_text(200, result.stdout.strip() + "\n", "text/plain; charset=utf-8")
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
                cmd = [str(BASE_DIR / "src/20_pipeline/020-notify-whatsapp.py"), "--force"]
                for numero in selected:
                    cmd.extend(["--record", numero])
                subprocess.Popen(cmd, cwd=BASE_DIR, env=monitor_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.send_text(202, f"WhatsApp notification requested for {len(selected)} selected record(s).\n", "text/plain; charset=utf-8")
                return
            if action_name == "templates":
                cmd = [str(BASE_DIR / "src/50_tools/020-record-templates.py"), "apply", "--apply"]
                for numero in selected:
                    cmd.extend(["--numero", numero])
                subprocess.Popen(cmd, cwd=BASE_DIR, env=monitor_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.send_text(202, f"Work templates requested for {len(selected)} selected record(s) (existing files kept).\n", "text/plain; charset=utf-8")
                return
            if action_name == "calendar":
                cmd = [str(BASE_DIR / "src/50_tools/060-import-selected-calendars.py"), "--open", *selected]
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
            command = [str(BASE_DIR / "src/50_tools/110-reset.py"), action]
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
                ("chat_id_open_now", WAHA_CHAT_ID_OPEN_NOW_PATH),
                ("chat_id_system", WAHA_CHAT_ID_SYSTEM_PATH),
                ("chat_id_summary", WAHA_CHAT_ID_SUMMARY_PATH),
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
        if path == "/api/waha-clients":
            try:
                save_waha_clients_text(form.get("clients", ["[]"])[0])
            except ValueError as exc:
                self.send_text(400, f"{exc}\n", "text/plain; charset=utf-8")
                return
            self.send_text(200, "WhatsApp client profiles saved.\n", "text/plain; charset=utf-8")
            return
        if path == "/api/client-profile":
            # Self-service profile upsert from the Android app: form field
            # "profile" is a JSON object string (same "JSON inside a form
            # field" convention as /api/waha-clients), e.g.
            # {"firebase_uid":"abc","email":"a@b.com","phone":"+507...",
            #  "purposes":["index","details"],"filters":"salud + insumos",
            #  "profession":"...","location":"...","institution":"...",
            #  "calendar_visible":true}
            try:
                update = json.loads(form.get("profile", ["{}"])[0])
            except json.JSONDecodeError as exc:
                self.send_text(400, f"invalid profile JSON: {exc}\n", "text/plain; charset=utf-8")
                return
            if not isinstance(update, dict):
                self.send_text(400, "profile must be a JSON object\n", "text/plain; charset=utf-8")
                return
            try:
                saved = upsert_client_profile(update)
            except ValueError as exc:
                self.send_text(400, f"{exc}\n", "text/plain; charset=utf-8")
                return
            self.send_text(200, json.dumps(saved, ensure_ascii=False), "application/json; charset=utf-8")
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
            MANUAL_ACTION_LOG.parent.mkdir(parents=True, exist_ok=True)
            with MANUAL_ACTION_LOG.open("a", encoding="utf-8") as log_file:
                log_file.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} | WhatsApp / test send =====\n")
                subprocess.Popen(
                    [str(BASE_DIR / "src/30_notify/010-waha-client.py"), "--event", "info", "--status", "TEST",
                     "--force-send", "--purpose", "system",
                     "--message", "Prueba de notificación desde el monitor web PanamaCompra."],
                    cwd=BASE_DIR, env=monitor_env(), stdout=log_file, stderr=subprocess.STDOUT,
                )
            self.send_text(202, "WhatsApp test message requested. Output: data/logs/manual_actions.log\n", "text/plain; charset=utf-8")
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
        if path == "/api/web-timer":
            self.send_text(200, json.dumps(web_timer_payload(), ensure_ascii=False, indent=2), "application/json; charset=utf-8")
            return
        if path == "/api/changedetection-schedule":
            self.send_text(200, json.dumps(changedetection_schedule_payload(), ensure_ascii=False, indent=2), "application/json; charset=utf-8")
            return
        if path == "/api/actions":
            # Same MANUAL_ACTIONS list the page's own buttons render from, as
            # plain JSON — lets other clients (e.g. the Android app) drive
            # /api/manual-action without duplicating this list.
            self.send_text(200, ACTIONS_JSON, "application/json; charset=utf-8")
            return
        if path == "/api/notifications":
            # Incremental feed over app_notifications (see
            # common.log_app_notification), the local mirror of every
            # outbound WAHA/WhatsApp send. ?since=<last seen id> returns only
            # newer rows so a polling client (the Android app) can dedupe.
            params = parse_qs(urlparse(self.path).query)
            try:
                since_id = int(params.get("since", ["0"])[0])
            except ValueError:
                since_id = 0
            rows_out: list[dict[str, object]] = []
            try:
                conn = sqlite3.connect(f"file:{ARCHIVE_DB}?mode=ro", uri=True, timeout=2)
                conn.row_factory = sqlite3.Row
                try:
                    rows = conn.execute(
                        "SELECT id, created_at, purpose, chat_id, text FROM app_notifications "
                        "WHERE id > ? ORDER BY id LIMIT 200",
                        (since_id,),
                    ).fetchall()
                    rows_out = [dict(row) for row in rows]
                finally:
                    conn.close()
            except sqlite3.Error:
                rows_out = []
            self.send_text(200, json.dumps(rows_out, ensure_ascii=False), "application/json; charset=utf-8")
            return
        if path == "/api/client-notifications":
            # Scoped feed for the client-facing Android app: ?uid=<firebase_uid>
            # (signed-in clients) or the legacy ?code=<app_code> resolves to a
            # data/config/waha_clients.json profile's chat_id, then returns
            # only app_notifications rows sent to that exact chat_id — i.e.
            # exactly what that client's WhatsApp group already receives, no
            # more. No chat_id in the response (clients don't need to see
            # WhatsApp internals).
            params = parse_qs(urlparse(self.path).query)
            uid = (params.get("uid", [""])[0] or "").strip()
            code = (params.get("code", [""])[0] or "").strip()
            try:
                since_id = int(params.get("since", ["0"])[0])
            except ValueError:
                since_id = 0
            latest_first = (params.get("latest", [""])[0] or "").strip().lower() in {"1", "true", "yes"}
            if not uid and not code:
                self.send_text(400, "missing ?uid= or ?code=\n", "text/plain; charset=utf-8")
                return
            profile = find_client_profile(firebase_uid=uid, app_code=code)
            if profile is None:
                self.send_text(404, "unknown or disabled client\n", "text/plain; charset=utf-8")
                return
            chat_id = str(profile.get("chat_id") or "").strip()
            rows_out = []
            try:
                conn = sqlite3.connect(f"file:{ARCHIVE_DB}?mode=ro", uri=True, timeout=2)
                conn.row_factory = sqlite3.Row
                try:
                    order = "DESC" if latest_first else "ASC"
                    rows = conn.execute(
                        "SELECT id, created_at, purpose, text FROM app_notifications "
                        f"WHERE chat_id = ? AND id > ? ORDER BY id {order} LIMIT 200",
                        (chat_id, since_id),
                    ).fetchall()
                    rows_out = [dict(row) for row in rows]
                finally:
                    conn.close()
            except sqlite3.Error:
                rows_out = []
            self.send_text(200, json.dumps(rows_out, ensure_ascii=False), "application/json; charset=utf-8")
            return
        if path == "/api/cron-schedule":
            result = subprocess.run(
                [str(BASE_DIR / "src/50_tools/160-manage-cron-schedule.py"), "show"],
                cwd=BASE_DIR, env=monitor_env(), capture_output=True, text=True,
            )
            self.send_text(200, f"Currently installed: {result.stdout.strip() or result.stderr.strip()}\n", "text/plain; charset=utf-8")
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
            keyword_filter = calendar_keyword_filter_fn(params.get("filter", [""])[0])
            try:
                conn = sqlite3.connect(f"file:{ARCHIVE_DB}?mode=ro", uri=True, timeout=2)
                conn.row_factory = sqlite3.Row
                try:
                    text = opportunity_calendar.render_view(conn, view, anchor, field, filter_fn=keyword_filter)
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
            params = parse_qs(urlparse(self.path).query)
            try:
                days = max(0, int(params.get("days", ["0"])[0] or 0))
            except (TypeError, ValueError):
                days = 0
            grupo = params.get("grupo", [""])[0].strip()
            entidad = params.get("entidad", [""])[0].strip()
            self.send_text(200, json.dumps(db_review_stats(days=days, grupo=grupo, entidad=entidad), ensure_ascii=False), "application/json; charset=utf-8")
            return
        if path == "/api/kpi-export":
            # CSV of the current filtered KPI slice (audit Phase 3) — same
            # payload as /api/db-stats, flattened by the shared engine.
            params = parse_qs(urlparse(self.path).query)
            try:
                days = max(0, int(params.get("days", ["0"])[0] or 0))
            except (TypeError, ValueError):
                days = 0
            grupo = params.get("grupo", [""])[0].strip()
            entidad = params.get("entidad", [""])[0].strip()
            csv_text = stats_to_csv(db_review_stats(days=days, grupo=grupo, entidad=entidad))
            encoded = csv_text.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="panamacompra_kpis.csv"')
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(encoded)
            return
        if path == "/api/webhook-access":
            self.send_text(200, json.dumps(webhook_access_payload(), ensure_ascii=False), "application/json; charset=utf-8")
            return
        if path == "/api/changedetection-script":
            try:
                script_text = CHANGEDETECTION_BROWSER_STEPS_JS.read_text(encoding="utf-8")
            except OSError as exc:
                self.send_text(404, f"changedetection script not found: {exc}\n", "text/plain; charset=utf-8")
                return
            self.send_text(200, script_text, "text/javascript; charset=utf-8")
            return
        if path == "/api/waha-session-status":
            self.send_text(200, json.dumps(monitor_connectivity_status(), ensure_ascii=False), "application/json; charset=utf-8")
            return
        if path == "/api/waha-qr":
            session_name = setting("PC_WAHA_SESSION", "default")
            try:
                import urllib.request
                base_url = os.environ.get("PC_WAHA_BASE_URL", "http://127.0.0.1:3000").rstrip("/")
                api_key = (os.environ.get("PC_WAHA_API_KEY") or os.environ.get("WAHA_API_KEY", "")).strip()
                headers = {"Accept": "image/png"}
                if api_key:
                    headers["X-Api-Key"] = api_key
                request = urllib.request.Request(f"{base_url}/api/{session_name}/auth/qr?format=image", headers=headers)
                with urllib.request.urlopen(request, timeout=8) as response:  # noqa: S310 - local WAHA endpoint
                    png_bytes = response.read()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(png_bytes)))
                self.end_headers()
                self.wfile.write(png_bytes)
            except Exception as exc:  # noqa: BLE001 - QR not available is a normal state
                self.send_text(503, f"QR unavailable: {exc}\n", "text/plain; charset=utf-8")
            return
        if path == "/api/waha-search":
            params = parse_qs(urlparse(self.path).query)
            refresh = (params.get("refresh", [""])[0] or "").strip().lower() in {"1", "true", "yes"}
            payload = waha_search(params.get("q", [""])[0], refresh=refresh)
            self.send_text(200, json.dumps(payload, ensure_ascii=False), "application/json; charset=utf-8")
            return
        if path == "/api/calendar-grid":
            # Lightweight event data for the graphical day/week/month/year
            # calendar. The text renderer remains the source for CLI parity;
            # this endpoint is only for browser layout.
            params = parse_qs(urlparse(self.path).query)
            try:
                shift = int(params.get("shift", ["0"])[0])
            except ValueError:
                shift = 0
            keyword_filter = calendar_keyword_filter_fn(params.get("filter", [""])[0])
            payload = calendar_grid_payload(
                (params.get("view", ["month"])[0] or "month").lower(),
                (params.get("field", ["end"])[0] or "end").lower(),
                params.get("date", [""])[0],
                filter_fn=keyword_filter,
                shift=shift,
            )
            self.send_text(200, json.dumps(payload, ensure_ascii=False), "application/json; charset=utf-8")
            return
        if path == "/api/client-calendar-grid":
            # Same shape as /api/calendar-grid, scoped to one client's own
            # filters — the Android app's Calendar tab (WebView, see
            # /client-calendar) points here instead.
            params = parse_qs(urlparse(self.path).query)
            uid = (params.get("uid", [""])[0] or "").strip()
            code = (params.get("code", [""])[0] or "").strip()
            if not uid and not code:
                self.send_text(400, "missing ?uid= or ?code=\n", "text/plain; charset=utf-8")
                return
            profile = find_client_profile(firebase_uid=uid, app_code=code)
            if profile is None:
                self.send_text(404, "unknown or disabled client\n", "text/plain; charset=utf-8")
                return
            if not profile.get("calendar_visible", True):
                self.send_text(200, json.dumps({"hidden": True}, ensure_ascii=False), "application/json; charset=utf-8")
                return
            try:
                shift = int(params.get("shift", ["0"])[0])
            except ValueError:
                shift = 0
            payload = calendar_grid_payload(
                (params.get("view", ["month"])[0] or "month").lower(),
                (params.get("field", ["end"])[0] or "end").lower(),
                params.get("date", [""])[0],
                filter_fn=client_filter_fn(profile),
                shift=shift,
            )
            self.send_text(200, json.dumps(payload, ensure_ascii=False), "application/json; charset=utf-8")
            return
        if path == "/client-calendar":
            self.send_text(200, CLIENT_CALENDAR_HTML, "text/html; charset=utf-8")
            return
        if path == "/api/client-profile":
            params = parse_qs(urlparse(self.path).query)
            uid = (params.get("uid", [""])[0] or "").strip()
            if not uid:
                self.send_text(400, "missing ?uid=\n", "text/plain; charset=utf-8")
                return
            profile = find_client_profile(firebase_uid=uid)
            if profile is None:
                self.send_text(404, "no profile for this uid yet\n", "text/plain; charset=utf-8")
                return
            self.send_text(200, json.dumps(profile, ensure_ascii=False), "application/json; charset=utf-8")
            return
        if path == "/api/filter-suggestions":
            # Suggested institution/location/profession values, derived from
            # what's actually in the archive (not a hand-maintained list) so
            # it stays current as new opportunities come in. "profession" has
            # no dedicated column — grupo (the index-scrape category) is the
            # closest existing proxy.
            suggestions = {"institutions": [], "locations": [], "professions": []}
            try:
                conn = sqlite3.connect(f"file:{ARCHIVE_DB}?mode=ro", uri=True, timeout=2)
                conn.row_factory = sqlite3.Row
                try:
                    for key, column in (("institutions", "entidad"), ("locations", "dependencia"), ("professions", "grupo")):
                        rows = conn.execute(
                            f"SELECT {column} AS value, COUNT(*) AS n FROM opportunities "
                            f"WHERE {column} IS NOT NULL AND TRIM({column}) != '' "
                            f"GROUP BY {column} ORDER BY n DESC LIMIT 40"
                        ).fetchall()
                        suggestions[key] = [row["value"] for row in rows]
                finally:
                    conn.close()
            except sqlite3.Error:
                pass
            self.send_text(200, json.dumps(suggestions, ensure_ascii=False), "application/json; charset=utf-8")
            return
        if path in ("/", "/index.html"):
            self.send_text(200, HTML, "text/html; charset=utf-8")
            return
        self.send_text(404, "not found\n", "text/plain; charset=utf-8")


MONITOR_URL_ENV_PATH = pc_common.RUN_DIR / "monitor_web.env"
# When the configured port is taken by another program, scan forward this many
# ports for a free one instead of crashing. The actual bind is published to
# run/monitor_web.env so the opener and launchers always find the real port.
PORT_SCAN_RANGE = max(1, int(os.environ.get("PC_MONITOR_PORT_SCAN", "20")))


def lan_ip() -> str:
    """This PC's LAN address (for the URL other machines use), '' if unknown."""
    import socket
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("192.0.2.1", 80))  # UDP connect sends no packets
            return probe.getsockname()[0]
        finally:
            probe.close()
    except OSError:
        return ""


def bind_monitor_server(host: str, port: int):
    """Bind the monitor, moving to the next free port on a conflict."""
    last_error = None
    for candidate in range(port, port + PORT_SCAN_RANGE):
        try:
            return ThreadingHTTPServer((host, candidate), MonitorHandler), candidate
        except OSError as exc:
            last_error = exc
            print(f"Port {candidate} unavailable ({exc.strerror or exc}); trying {candidate + 1}...", flush=True)
    raise SystemExit(f"No free monitor port in {port}-{port + PORT_SCAN_RANGE - 1}: {last_error}")


def publish_monitor_url(host: str, port: int) -> None:
    """Write the ACTUAL bind to run/monitor_web.env (atomic).

    MONITOR_LOCAL_URL always works from this PC; MONITOR_LAN_URL is what other
    machines on the network use (only set when the bind allows them in)."""
    open_to_lan = host in ("0.0.0.0", "::", "")
    local_host = "127.0.0.1" if open_to_lan else host
    lan = lan_ip() if open_to_lan else (host if not host.startswith("127.") else "")
    fields = {
        "MONITOR_HOST": host,
        "MONITOR_PORT": port,
        "MONITOR_LOCAL_URL": f"http://{local_host}:{port}/",
        "MONITOR_LAN_URL": f"http://{lan}:{port}/" if lan else "",
        "MONITOR_PID": os.getpid(),
        "MONITOR_STARTED_AT": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        pc_common.RUN_DIR.mkdir(parents=True, exist_ok=True)
        tmp = MONITOR_URL_ENV_PATH.with_suffix(".env.tmp")
        tmp.write_text("".join(f"{key}='{value}'\n" for key, value in fields.items()), encoding="utf-8")
        tmp.replace(MONITOR_URL_ENV_PATH)
    except OSError as exc:
        print(f"Could not publish monitor URL file: {exc}", flush=True)


def main() -> None:
    pc_common.LOG_DIR.mkdir(parents=True, exist_ok=True)
    pc_common.QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    settings = load_monitor_settings()
    host = os.environ.get("PC_MONITOR_HOST") or settings.get("PC_MONITOR_HOST") or HOST
    port = int(os.environ.get("PC_MONITOR_PORT", str(PORT)))
    server, bound_port = bind_monitor_server(host, port)
    publish_monitor_url(host, bound_port)
    local_host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    print(f"PanamaCompra web monitor: http://{local_host}:{bound_port}/", flush=True)
    if host in ("0.0.0.0", "::"):
        lan = lan_ip()
        if lan:
            print(f"LAN access: http://{lan}:{bound_port}/", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
