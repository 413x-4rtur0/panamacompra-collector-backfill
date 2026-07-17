#!/usr/bin/env python3
"""Low-power local web monitor for PanamaCompra run-all progress.

The page is loaded once and then polls a small JSON endpoint. That avoids full
browser reloads every few seconds while preserving the same dashboard UI.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections import Counter
import os
import re
import secrets
import shlex
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
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
    waha_api_key,
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


# row_filter_haystack() reads each record's detail-items JSON from disk; on a
# busy month that is thousands of file reads per calendar request (~15s
# measured). Cache the built haystack per record, invalidated by the detail
# file's mtime, so only the first filtered request pays the I/O. Matching
# semantics (accent/case-insensitive, includes detail items text) unchanged —
# and the WhatsApp sender path is untouched.
_HAYSTACK_CACHE: dict[str, tuple[float, str]] = {}
_HAYSTACK_CACHE_MAX = 30000


def _row_haystack_cached(row) -> str:
    """The record's ALREADY-NORMALIZED (accent-stripped, lowercased) filter
    haystack, cached per numero and invalidated by the detail file's mtime."""
    numero = str(row["numero"] or "")
    detail_path = row["detail_json_path"] or ""
    mtime = 0.0
    if detail_path:
        try:
            mtime = os.stat(detail_path).st_mtime
        except OSError:
            mtime = 0.0
    cached = _HAYSTACK_CACHE.get(numero)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    haystack = pc_common.strip_accents(notify_formats.row_filter_haystack(row, {})).lower()
    if len(_HAYSTACK_CACHE) >= _HAYSTACK_CACHE_MAX:
        _HAYSTACK_CACHE.clear()
    _HAYSTACK_CACHE[numero] = (mtime, haystack)
    return haystack


def client_filter_fn(profile: dict):
    """Row predicate for a client's own filters, for opportunity_calendar.fetch_events().

    Same rules and semantics as notify_whatsapp (parse_filter_rules + the
    accent/case-insensitive substring match of evaluate_filter; operators:
    comma = OR, '+' = AND, leading '-' = NOT), evaluated over pre-normalized
    cached haystacks so a filtered month view answers in well under a second
    instead of ~15s. If evaluate_filter's semantics ever change, mirror the
    change here.
    """
    includes, excludes = notify_formats.parse_filter_rules(profile.get("filters", ""))
    includes_n = [[pc_common.strip_accents(t).lower() for t in rule] for rule in includes]
    excludes_n = [[pc_common.strip_accents(t).lower() for t in rule] for rule in excludes]

    def _matches(row) -> bool:
        normalized = _row_haystack_cached(row)
        if any(all(term in normalized for term in rule) for rule in excludes_n):
            return False
        if not includes_n:
            return True
        return any(all(term in normalized for term in rule) for rule in includes_n)

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
                    link = str(row["link"] or "")
                    grouped_events[day_key].append({
                        "numero": str(row["numero"] or ""),
                        "description": desc[:96],
                        "status": str(row["estado"] or row["grupo"] or ""),
                        "date": value,
                        "clock": value[11:16] if len(value) >= 16 else "--:--",
                        "url": link if link.startswith("http") else "",
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
    weeks = []
    if view == "month":
        week_start = start - timedelta(days=start.weekday())
        while week_start <= end:
            iso = week_start.isocalendar()
            weeks.append({
                "start": week_start.isoformat(),
                "number": iso.week,
                "year": iso.year,
            })
            week_start += timedelta(days=7)
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
        "weeks": weeks,
        "events": grouped_events,
        "total": sum(len(rows) for rows in grouped_events.values()),
        "months": months,
        "month_counts": month_counts,
    }


# Location fields shown, in order, in the calendar hover tooltip. Keys match the
# detail JSON's "summary" block; entidad/dependencia fall back to the archive DB.
_LOCATION_KEYS = (
    "provincia",
    "direccion",
    "provincia_de_entrega",
    "forma_de_entrega",
    "dias_de_entrega",
    "dia_y_hora_de_entrega",
)


def opportunity_location(numero: str) -> dict:
    """All delivery/location data for one opportunity, for the calendar hover
    tooltip. entidad/dependencia come straight from the archive DB; the richer
    fields (provincia, direccion, ...) are read on demand from that record's
    detail JSON summary so the calendar grid payload stays lightweight."""
    out: dict[str, str] = {"numero": numero}
    row = None
    try:
        conn = sqlite3.connect(f"file:{ARCHIVE_DB}?mode=ro", uri=True, timeout=2)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT descripcion, short_description, entidad, dependencia, detail_json_path "
                "FROM opportunities WHERE numero = ? LIMIT 1",
                (numero,),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        row = None
    if row is None:
        return out
    out["descripcion"] = (row["short_description"] or row["descripcion"] or "").strip()
    out["entidad"] = (row["entidad"] or "").strip()
    out["dependencia"] = (row["dependencia"] or "").strip()
    path = (row["detail_json_path"] or "").strip()
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as handle:
                summary = (json.load(handle) or {}).get("summary") or {}
        except (OSError, ValueError):
            summary = {}
        for key in ("entidad", "dependencia", *_LOCATION_KEYS):
            value = summary.get(key)
            if value:
                out[key] = str(value).strip()
    return out


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
    # Firebase *web* app config for the browser client dashboard
    # (/client-calendar). Same Firebase project as the Android app; add a Web
    # app in Firebase console → Project settings → Your apps and copy the
    # values here. Blank = client sign-in disabled (page explains how to
    # enable it). These are public identifiers, not secrets.
    "PC_FIREBASE_WEB_API_KEY": "",
    "PC_FIREBASE_WEB_AUTH_DOMAIN": "",
    "PC_FIREBASE_WEB_PROJECT_ID": "",
    "PC_FIREBASE_WEB_APP_ID": "",
    # Local manager login for the admin monitor (front-page "Manager" form).
    # Works with no Firebase/internet at all. Blank = manager login disabled.
    # Like WAHA_DASHBOARD_PASSWORD above, the value lives in
    # var/data/config/monitor_settings.env on this PC only — never commit it.
    "PC_ADMIN_USERNAME": "",
    "PC_ADMIN_PASSWORD": "",
    # Emails allowed into the ADMIN monitor after Firebase sign-in (comma or
    # space separated, case-insensitive). Everyone else who signs in is a
    # client and lands on /client-calendar. Requests from this PC itself
    # (127.0.0.1) always get admin access, so you can never lock yourself out.
    "PC_ADMIN_EMAILS": "",
    # Optional shared token for non-browser admin clients (the Android admin
    # app): requests carrying it as an X-PC-Admin-Token header or
    # ?admin_token= query parameter bypass the session cookie. Blank = off.
    "PC_ADMIN_API_TOKEN": "",
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
    ManualAction("Runners", "STOP all runners", ("./src/20_pipeline/120a-stop-everything.sh",), "DANGER: stops workers, test zone, calendar builder, webhook listener and updaters. All monitors stay open so you can resume from here."),
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


# Credential-bearing settings a blank save must never erase: an empty value
# for one of these keeps the stored value instead of overwriting it (this
# once wiped WAHA_API_KEY and the manager login in one bad Save click). To
# intentionally clear one, edit data/config/monitor_settings.env by hand.
PROTECTED_NONBLANK_SETTINGS = {"WAHA_API_KEY", "PC_ADMIN_USERNAME", "PC_ADMIN_PASSWORD", "PC_ADMIN_EMAILS", "PC_ADMIN_API_TOKEN",
                               "WAHA_DASHBOARD_PASSWORD", "PC_FIREBASE_WEB_API_KEY", "PC_FIREBASE_WEB_AUTH_DOMAIN",
                               "PC_FIREBASE_WEB_PROJECT_ID", "PC_FIREBASE_WEB_APP_ID"}


def save_monitor_setting(key: str, value: str) -> None:
    if key not in ALLOWED_MONITOR_SETTINGS:
        raise ValueError(f"unsupported setting: {key}")
    settings = parse_settings_file()
    if key in PROTECTED_NONBLANK_SETTINGS and not value.strip() and settings.get(key, "").strip():
        return
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


# ---- Admin sign-in sessions -------------------------------------------------
# The front page (/) is a Firebase login; the admin dashboard and every
# admin API require one of: a request from this PC itself (127.0.0.1), a
# valid signed session cookie whose email is in PC_ADMIN_EMAILS, or the
# PC_ADMIN_API_TOKEN shared token (non-browser clients). Client-facing
# endpoints (the uid-scoped /api/client-* family) stay open as before.
SESSION_COOKIE_NAME = "pc_admin_session"
SESSION_TTL_SECONDS = 7 * 24 * 3600
SESSION_SECRET_PATH = pc_common.DATA_CONFIG_DIR / "monitor_web_secret.txt"
_SESSION_SECRET_LOCK = threading.Lock()
_SESSION_SECRET: bytes | None = None


def _session_secret() -> bytes:
    """Persistent HMAC key for session cookies (so restarts keep sessions)."""
    global _SESSION_SECRET
    with _SESSION_SECRET_LOCK:
        if _SESSION_SECRET is None:
            try:
                text = SESSION_SECRET_PATH.read_text(encoding="utf-8").strip()
            except OSError:
                text = ""
            if len(text) < 32:
                text = secrets.token_hex(32)
                SESSION_SECRET_PATH.parent.mkdir(parents=True, exist_ok=True)
                SESSION_SECRET_PATH.write_text(text + "\n", encoding="utf-8")
                try:
                    SESSION_SECRET_PATH.chmod(0o600)
                except OSError:
                    pass
            _SESSION_SECRET = text.encode("utf-8")
        return _SESSION_SECRET


def _sign_session(payload_b64: str) -> str:
    return hmac.new(_session_secret(), payload_b64.encode("ascii"), hashlib.sha256).hexdigest()


def make_session_token(uid: str, email: str, role: str, tabs: list[str] | None = None) -> str:
    payload = json.dumps({"uid": uid, "email": email, "role": role, "tabs": tabs or [], "exp": int(time.time()) + SESSION_TTL_SECONDS})
    payload_b64 = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")
    return payload_b64 + "." + _sign_session(payload_b64)


def parse_session_token(token: str) -> dict | None:
    """The session dict if the token is genuine and unexpired, else None."""
    if not token or "." not in token:
        return None
    payload_b64, _, signature = token.rpartition(".")
    if not hmac.compare_digest(signature, _sign_session(payload_b64)):
        return None
    try:
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        session = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(session, dict) or int(session.get("exp", 0)) < time.time():
        return None
    return session


def admin_emails() -> set[str]:
    raw = load_monitor_settings().get("PC_ADMIN_EMAILS", "")
    return {part.strip().lower() for part in re.split(r"[\s,;]+", raw) if part.strip()}


def verify_firebase_id_token(id_token: str) -> dict | None:
    """Server-side check of a Firebase ID token via the identitytoolkit
    accounts:lookup REST endpoint (needs internet, login-time only). Returns
    {"uid": ..., "email": ...} when Google confirms the token, else None.
    Chosen over local JWT verification so the monitor needs no extra
    dependencies (stdlib has no RS256)."""
    api_key = load_monitor_settings().get("PC_FIREBASE_WEB_API_KEY", "").strip()
    if not api_key or not id_token:
        return None
    request = urllib.request.Request(
        f"https://identitytoolkit.googleapis.com/v1/accounts:lookup?key={api_key}",
        data=json.dumps({"idToken": id_token}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 - fixed Google endpoint
            data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, ValueError, OSError):
        return None
    users = data.get("users") or []
    if not users or not isinstance(users[0], dict):
        return None
    return {"uid": str(users[0].get("localId") or ""), "email": str(users[0].get("email") or "").strip().lower()}


# ---- Manager-created monitor users (per-tab access) -------------------------
# The manager can add named users who sign in with the same front-page
# "Manager" form but only see (and can only drive) the tabs granted to them.
# Stored in data/config/monitor_users.json, managed from Settings → "Monitor
# users & tab access". The main PC_ADMIN_USERNAME account always has all tabs.
MONITOR_USERS_PATH = pc_common.DATA_CONFIG_DIR / "monitor_users.json"
MONITOR_USERS_LOCK = threading.Lock()
VALID_MONITOR_TABS = ("overview", "calendar", "decision", "records", "operations", "whatsapp", "scheduler", "integrations", "settings")


def _normalize_monitor_user(item: dict) -> dict | None:
    if not isinstance(item, dict):
        raise ValueError("each monitor user must be an object")
    username = str(item.get("username") or "").strip()
    if not username:
        return None
    tabs = item.get("tabs") or []
    if isinstance(tabs, str):
        tabs = [t.strip() for t in tabs.split(",")]
    if not isinstance(tabs, list):
        raise ValueError("tabs must be a list or comma-separated string")
    tabs = [t for t in (str(t).strip().lower() for t in tabs) if t in VALID_MONITOR_TABS]
    return {
        "username": username,
        "password": str(item.get("password") or ""),
        "tabs": tabs or ["overview", "calendar"],
        "enabled": bool(item.get("enabled", True)),
    }


def read_monitor_users() -> list[dict]:
    try:
        parsed = json.loads(MONITOR_USERS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(parsed, list):
        return []
    out = []
    for item in parsed:
        try:
            user = _normalize_monitor_user(item)
        except ValueError:
            continue
        if user is not None:
            out.append(user)
    return out


def save_monitor_users_text(text: str) -> list[dict]:
    try:
        parsed = json.loads(text or "[]")
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid users JSON: {exc}") from exc
    if not isinstance(parsed, list):
        raise ValueError("monitor users must be a JSON list")
    normalized = [u for u in (_normalize_monitor_user(item) for item in parsed) if u is not None]
    with MONITOR_USERS_LOCK:
        MONITOR_USERS_PATH.parent.mkdir(parents=True, exist_ok=True)
        MONITOR_USERS_PATH.write_text(json.dumps(normalized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return normalized


def find_monitor_user(username: str, password: str) -> dict | None:
    """Constant-time-ish credential check against monitor_users.json."""
    for user in read_monitor_users():
        if not user.get("enabled", True) or not user.get("password"):
            continue
        if hmac.compare_digest(username, user["username"]) and hmac.compare_digest(password, user["password"]):
            return user
    return None


# Settings keys whose values never leave the server for staff sessions —
# staff /api/status responses carry a redacted settings map.
SENSITIVE_SETTING_KEYS = {"WAHA_API_KEY", "WAHA_DASHBOARD_PASSWORD", "PC_ADMIN_PASSWORD", "PC_ADMIN_API_TOKEN", "PC_ADMIN_USERNAME", "PC_FIREBASE_WEB_API_KEY"}

# POST endpoints a staff session may call, mapped to the tab that grants
# them. Anything not listed here stays full-admin-only.
STAFF_POST_TAB_MAP = {
    "/api/request-run": "operations",
    "/api/manual-action": "operations",
    "/api/import-calendars": "records",
    "/api/selected-record-action": "records",
    "/api/open-record-folder": "records",
    "/api/test-whatsapp": "whatsapp",
    "/api/waha-filters": "whatsapp",
    "/api/waha-clients": "whatsapp",
    "/api/waha-format": "whatsapp",
    "/api/cron-schedule": "scheduler",
}

# Paths a request may hit WITHOUT admin access: the client dashboard, the
# uid-scoped client APIs the Android client app already relies on, and the
# session login/logout handshake itself. Everything else is admin-only.
OPEN_GET_PATHS = {
    "/health",
    "/client-calendar",
    "/client",
    "/api/client-auth-config",
    "/api/client-calendar-grid",
    "/api/client-notifications",
    "/api/client-profile",
    "/api/opportunity-location",
    "/api/filter-suggestions",
}
OPEN_POST_PATHS = {
    "/api/client-profile",
    "/api/session-login",
    "/api/session-logout",
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

# Calendar hover tooltip: on mouseover of a calendar event, fetch that
# opportunity's location data once (cached) and show it near the cursor.
# Injected verbatim into the admin page's <script> (single-brace JS, so it is
# referenced as a plain {_LOC_TOOLTIP_JS} field in the f-string below).
_LOC_TOOLTIP_JS = r"""
const _locCache = {};
let _locTipEl = null;
function _locTip() {
  if (!_locTipEl) {
    _locTipEl = document.createElement('div');
    _locTipEl.className = 'loc-tooltip';
    document.body.appendChild(_locTipEl);
  }
  return _locTipEl;
}
function _locHide() { if (_locTipEl) _locTipEl.style.display = 'none'; }
function _locPos(t, x, y) {
  const w = t.offsetWidth || 300, h = t.offsetHeight || 130;
  let left = x + 14, top = y + 16;
  if (left + w > window.innerWidth) left = x - w - 14;
  if (top + h > window.innerHeight) top = y - h - 16;
  t.style.left = Math.max(4, left) + 'px';
  t.style.top = Math.max(4, top) + 'px';
}
function _locFmt(d) {
  const rows = [];
  const add = (label, v) => { if (v) rows.push('<div><b>' + esc(label) + ':</b> ' + esc(v) + '</div>'); };
  if (d.numero) rows.push('<div class="loc-head">' + esc(d.numero) + '</div>');
  if (d.descripcion) rows.push('<div class="loc-desc">' + esc(d.descripcion) + '</div>');
  add('Entidad', d.entidad);
  add('Dependencia', d.dependencia);
  add('Provincia', d.provincia);
  add('Direccion', d.direccion);
  add('Provincia de entrega', d.provincia_de_entrega);
  add('Forma de entrega', d.forma_de_entrega);
  add('Dias de entrega', d.dias_de_entrega);
  add('Dia y hora de entrega', d.dia_y_hora_de_entrega);
  return rows.join('') || '<div class="loc-desc">Sin datos de ubicacion</div>';
}
async function _locShow(numero, x, y) {
  const t = _locTip();
  let data = _locCache[numero];
  if (data === undefined) {
    t.innerHTML = '<div class="loc-desc">Cargando ubicacion...</div>';
    _locPos(t, x, y);
    t.style.display = 'block';
    try {
      const resp = await fetch('/api/opportunity-location?numero=' + encodeURIComponent(numero), {cache: 'no-store'});
      data = await resp.json();
    } catch (e) {
      data = {};
    }
    _locCache[numero] = data;
  }
  t.innerHTML = _locFmt(data);
  _locPos(t, x, y);
  t.style.display = 'block';
}
function _locTarget(ev) {
  return (ev.target && ev.target.closest) ? ev.target.closest('.calevent[data-numero]') : null;
}
document.addEventListener('mouseover', ev => {
  const el = _locTarget(ev);
  if (el) _locShow(el.getAttribute('data-numero'), ev.clientX, ev.clientY);
});
document.addEventListener('mousemove', ev => {
  if (!_locTipEl || _locTipEl.style.display !== 'block') return;
  const el = _locTarget(ev);
  if (el) _locPos(_locTipEl, ev.clientX, ev.clientY);
  else _locHide();
});
document.addEventListener('mouseout', ev => {
  if (_locTarget(ev)) _locHide();
});
"""

# Standalone client dashboard at /client-calendar (alias /client): the only
# page a client ever needs — sign-in plus their scoped calendar, nothing else
# from the admin monitor. Two access modes:
#   * Browser: Firebase Auth login (email/password or Google; config comes
#     from /api/client-auth-config, i.e. the PC_FIREBASE_WEB_* settings). On
#     first sign-in it asks for email + phone and upserts the client profile.
#   * Android WebView (legacy): ?uid=<firebase_uid> skips the login UI.
# Deliberately a self-contained copy of just the calendar CSS/JS from the
# admin page's calendar card below, not a shared include — the admin page's
# HTML is one big f-string and factoring a shared fragment out of it isn't
# worth the risk of a blind edit to an already-verified 600+ line string.
# The location hover tooltip (_LOC_TOOLTIP_JS above) IS shared verbatim. If
# you change the calendar's appearance (CSS in the "Ubuntu-style opportunity
# calendar" block below, or the renderCalendar* functions), mirror it here.
CLIENT_CALENDAR_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PanamaCompra Calendar</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Archivo:wght@600;700;800&family=IBM+Plex+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>
/* ARL-89 (HP-23 Design System) tokens — hi-vis amber, charcoal ink, warm
   concrete neutrals, blueprint accent. Source: claude.ai/design project
   129dfa45 tokens/colors.css + typography.css + spacing.css. */
:root {
  --amber-50: #FFF6E0; --amber-100: #FFE9B3; --amber-200: #FFD773; --amber-300: #FFC53D;
  --amber-400: #FFB400; --amber-500: #F5A300; --amber-600: #D98A00; --amber-700: #B06E00; --amber-800: #855200;
  --ink-900: #121417; --ink-800: #1A1D21; --ink-700: #23262B; --ink-600: #2E3338;
  --concrete-0: #FFFFFF; --concrete-50: #F6F5F2; --concrete-100: #ECEAE5; --concrete-200: #DEDBD4;
  --concrete-300: #C8C4BB; --concrete-400: #A6A199; --concrete-500: #7D7872; --concrete-600: #585450;
  --blueprint-400: #3D7DFF; --blueprint-500: #2D6CDF; --blueprint-600: #1F54B5;
  --green-500: #2F9E5B; --green-600: #247A47; --red-500: #D63B26; --red-600: #AE2D1C;
  --blue-tint: #EDF3FF; --green-tint: #E6F4EC; --red-tint: #FBE9E6;
  --font-display: 'Archivo', 'Helvetica Neue', Arial, sans-serif;
  --font-sans: 'IBM Plex Sans', 'Helvetica Neue', Arial, sans-serif;
  --font-mono: 'IBM Plex Mono', ui-monospace, 'SFMono-Regular', Menlo, monospace;
  --radius-sm: 4px; --radius-md: 6px; --radius-lg: 10px; --radius-pill: 999px;
  --shadow-sm: 0 1px 2px rgba(18,20,23,.10), 0 1px 1px rgba(18,20,23,.05);
  --shadow-md: 0 4px 12px rgba(18,20,23,.10);
  --shadow-lg: 0 12px 30px rgba(18,20,23,.16);
  --focus-ring: 0 0 0 3px rgba(45,108,223,.35);
}
body { font-family: var(--font-sans); margin: 10px; background: var(--concrete-50); color: var(--ink-700); }
.small { color: var(--concrete-500); font-size: .85rem; }
.err { color: var(--red-600); font-size: .85rem; min-height: 1.2em; }
.controls { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-bottom: 10px; }
select, input, button { border-radius: var(--radius-md); border: 1px solid var(--concrete-300); background: var(--concrete-0); color: var(--ink-800); padding: 6px 8px; font-size: .9rem; font-family: var(--font-sans); }
select:focus, input:focus { outline: none; border-color: var(--blueprint-500); box-shadow: var(--focus-ring); }
button { cursor: pointer; font-weight: 600; color: var(--ink-900); }
button:hover { background: var(--concrete-100); }
button.primary { background: var(--amber-500); border-color: var(--amber-600); color: var(--ink-900); font-weight: 700; }
button.primary:hover { background: var(--amber-600); }
.topbar { display: flex; justify-content: space-between; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 10px; padding: 10px 12px; background: var(--ink-900); border-radius: var(--radius-lg); border-bottom: 2px solid var(--amber-500); }
.topbar h1 { margin: 0; font-size: 1.1rem; color: var(--concrete-0); font-family: var(--font-display); letter-spacing: -0.015em; }
.topbar .small { color: var(--concrete-300); }
.topbar a { color: var(--amber-300); }
.auth-card { max-width: 380px; margin: 8vh auto 0; background: var(--concrete-0); border: 1px solid var(--concrete-200); border-top: 4px solid var(--amber-500); border-radius: var(--radius-lg); padding: 20px; display: flex; flex-direction: column; gap: 10px; box-shadow: var(--shadow-md); }
.auth-card h1 { margin: 0 0 4px; font-size: 1.15rem; color: var(--ink-900); font-family: var(--font-display); letter-spacing: -0.015em; }
.auth-card input { width: 100%; box-sizing: border-box; }
.auth-sep { text-align: center; color: var(--concrete-400); font-size: .8rem; font-family: var(--font-mono); text-transform: uppercase; letter-spacing: 0.14em; }
.calendar-board { background: var(--concrete-0); border: 1px solid var(--concrete-200); border-radius: var(--radius-lg); overflow: hidden; box-shadow: var(--shadow-sm); }
.calendar-title { display: flex; justify-content: space-between; gap: 10px; align-items: center; padding: 10px 12px; background: var(--ink-900); border-bottom: 2px solid var(--amber-500); }
.calendar-title h3 { margin: 0; color: var(--concrete-0); font-size: 1rem; font-family: var(--font-display); }
.calendar-title .small { color: var(--concrete-300); }
.calgrid { display: grid; grid-template-columns: repeat(7, minmax(0, 1fr)); }
.calgrid .dow { text-align: center; color: var(--concrete-600); font-family: var(--font-mono); font-weight: 600; text-transform: uppercase; letter-spacing: 0.06em; font-size: .74rem; padding: 7px 4px; border-bottom: 1px solid var(--concrete-200); background: var(--concrete-50); }
.calcell { min-height: 100px; border-right: 1px solid var(--concrete-100); border-bottom: 1px solid var(--concrete-100); padding: 5px; cursor: pointer; background: var(--concrete-0); overflow: hidden; }
.calcell:hover { background: var(--amber-50); box-shadow: inset 0 0 0 1px var(--amber-500); }
.calcell.blank { background: var(--concrete-50); cursor: default; }
.calcell.today { box-shadow: inset 0 0 0 2px var(--amber-500); }
.calcell .num { color: var(--ink-900); font-size: .85rem; font-weight: 700; display: flex; justify-content: space-between; margin-bottom: 4px; }
.calcell .count { color: var(--concrete-500); font-size: .72rem; font-weight: 400; font-family: var(--font-mono); }
.calevent { display: block; margin: 3px 0; padding: 3px 5px; border-radius: var(--radius-sm); border-left: 3px solid var(--blueprint-500); background: var(--blue-tint); color: var(--ink-800); font-size: .74rem; line-height: 1.3; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
a.calevent { text-decoration: none; cursor: pointer; }
a.calevent:hover { filter: brightness(.94); }
.calevent.soon { border-left-color: var(--amber-500); background: var(--amber-100); color: var(--amber-800); }
.calevent.expired { border-left-color: var(--red-500); background: var(--red-tint); color: var(--red-600); }
.calevent.more { border-left-color: var(--concrete-400); background: var(--concrete-100); color: var(--concrete-600); }
.timeline-scroll { max-height: 70vh; overflow-y: auto; border-top: 1px solid var(--concrete-200); }
.timeline { display: grid; grid-template-columns: 56px 1fr; }
.week-timeline { display: grid; grid-template-columns: 56px repeat(7, minmax(0, 1fr)); }
.hour-label { color: var(--concrete-500); font-family: var(--font-mono); font-weight: 600; font-size: .7rem; padding: 5px 6px; text-align: right; border-bottom: 1px solid var(--concrete-100); border-right: 1px solid var(--concrete-200); background: var(--concrete-50); }
.hour-lane { min-height: 30px; padding: 3px 6px; display: flex; flex-direction: column; gap: 3px; border-bottom: 1px solid var(--concrete-100); }
.week-timeline .hour-lane { padding: 2px; gap: 2px; border-right: 1px solid var(--concrete-100); }
.timeline-notime .hour-label, .timeline-notime .hour-lane, .wk-notime { background: var(--concrete-100); border-bottom: 2px solid var(--concrete-300); }
.wk-head { padding: 6px 4px; text-align: center; font-family: var(--font-mono); font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; color: var(--concrete-600); font-size: .72rem; border-bottom: 1px solid var(--concrete-200); background: var(--concrete-50); position: sticky; top: 0; z-index: 1; }
.wk-head.zoomable { cursor: pointer; }
.wk-head.zoomable:hover { background: var(--amber-100); color: var(--ink-900); }
.wk-corner { background: var(--concrete-50); border-bottom: 1px solid var(--concrete-200); position: sticky; top: 0; z-index: 1; }
.year-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 8px; padding: 10px; }
.month-box { border: 1px solid var(--concrete-200); border-radius: var(--radius-md); padding: 10px; background: var(--concrete-0); cursor: pointer; }
.month-box:hover { border-color: var(--amber-500); background: var(--amber-50); }
.month-box b { color: var(--ink-900); font-family: var(--font-display); }
.bar-track { height: 10px; background: var(--concrete-200); border-radius: var(--radius-pill); overflow: hidden; margin-top: 6px; }
.bar-fill { height: 100%; background: linear-gradient(90deg, var(--amber-400), var(--amber-600)); border-radius: var(--radius-pill); }
.loc-tooltip { position: fixed; z-index: 9999; max-width: 340px; background: var(--concrete-0); color: var(--ink-800); border: 2px solid var(--ink-900); border-radius: var(--radius-md); padding: 8px 10px; font-size: .8rem; line-height: 1.35; box-shadow: var(--shadow-lg); pointer-events: none; display: none; }
.loc-tooltip .loc-head { font-weight: 700; color: var(--blueprint-600); margin-bottom: 2px; font-family: var(--font-mono); }
.loc-tooltip .loc-desc { color: var(--concrete-600); margin-bottom: 6px; white-space: normal; }
.loc-tooltip b { color: var(--ink-900); }
</style>
</head>
<body>
<div id="boot-msg" class="small">Loading&hellip;</div>
<div id="auth-screen" class="auth-card" hidden>
  <h1>PanamaCompra &mdash; client access</h1>
  <p class="small">Sign in to see your opportunity calendar.</p>
  <input id="auth-email" type="email" placeholder="Email" autocomplete="username">
  <input id="auth-password" type="password" placeholder="Password" autocomplete="current-password">
  <div class="err" id="auth-error"></div>
  <button class="primary" onclick="emailSignIn()">Sign in</button>
  <button onclick="emailSignUp()">Create account</button>
  <div class="auth-sep">&mdash; or &mdash;</div>
  <button onclick="googleSignIn()">Sign in with Google</button>
</div>
<div id="profile-screen" class="auth-card" hidden>
  <h1>Your contact details</h1>
  <p class="small">Confirm the email and phone number where we can reach you.</p>
  <input id="profile-email" type="email" placeholder="Email" autocomplete="email">
  <input id="profile-phone" type="tel" placeholder="Phone (e.g. +507 6000-0000)" autocomplete="tel">
  <div class="err" id="profile-error"></div>
  <button class="primary" onclick="saveProfile()">Save and continue</button>
  <button onclick="doSignOut()">Sign out</button>
</div>
<div id="app-screen" hidden>
<div class="topbar"><h1>Opportunity calendar</h1><span class="small" id="user-box"></span></div>
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
</div>
<script>
function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
""" + _LOC_TOOLTIP_JS + """
const params = new URLSearchParams(location.search);
let uid = params.get('uid') || '';
const legacyMode = !!uid;  // Android WebView passes ?uid= and skips the login UI.
const WEEKDAY_LABELS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
let calendarAnchor = '';
let calendarEventsInteractive = false;
let fbUser = null;
let existingProfile = null;

function show(id) {
  for (const s of ['boot-msg', 'auth-screen', 'profile-screen', 'app-screen']) {
    document.getElementById(s).hidden = (s !== id);
  }
}
function bootText(text) { show('boot-msg'); document.getElementById('boot-msg').textContent = text; }

// Progressive zoom: year -> month -> week -> day. Events only link to the
// opportunity page (and show the location hover tooltip) in the day view;
// in month/week a click zooms in instead.
function calendarZoomTo(iso, view) {
  document.getElementById('cal-date').value = iso;
  document.getElementById('cal-view').value = view;
  loadCalendar();
}
function calendarEventClass(ev) {
  const status = (ev.status || '').toLowerCase();
  if (status.includes('venc') || status.includes('cerrad') || status.includes('expir')) return 'expired';
  if (status.includes('pront') || status.includes('soon')) return 'soon';
  return '';
}
function renderCalendarEvent(ev) {
  const title = (ev.numero ? ev.numero + ' · ' : '') + (ev.description || '(sin descripcion)');
  const clock = ev.clock && ev.clock !== '--:--' ? ev.clock + ' ' : '';
  const cls = calendarEventClass(ev);
  const label = esc(clock + title);
  if (!calendarEventsInteractive) {
    return `<span class="calevent ${cls}">${label}</span>`;
  }
  if (ev.url) {
    return `<a class="calevent ${cls}" data-numero="${esc(ev.numero)}" href="${esc(ev.url)}" target="_blank" rel="noopener" title="${esc(title)}">${label}</a>`;
  }
  return `<span class="calevent ${cls}" data-numero="${esc(ev.numero)}" title="${esc(title)}">${label}</span>`;
}
function renderCalendarDayCell(day, events, blank, maxShown) {
  if (blank) return '<div class="calcell blank"></div>';
  const limit = maxShown || 4;
  const shown = (events || []).slice(0, limit).map(renderCalendarEvent).join('');
  const more = (events || []).length > limit ? `<span class="calevent more">+${events.length - limit} more</span>` : '';
  const classes = ['calcell'];
  if (day.iso === day.today) classes.push('today');
  return `<div class="${classes.join(' ')}" onclick="calendarZoomTo('${day.iso}', 'week')"><div class="num"><span>${day.label}</span><span class="count">${events.length || ''}</span></div>${shown}${more}</div>`;
}
async function loadCalendar(shift) {
  const node = document.getElementById('calendar-visual');
  const view = document.getElementById('cal-view').value || 'month';
  const field = document.getElementById('cal-field').value || 'end';
  const dateBox = document.getElementById('cal-date');
  calendarEventsInteractive = view === 'day';
  if (shift === 0) { calendarAnchor = ''; dateBox.value = ''; }
  const anchor = (dateBox.value || calendarAnchor).trim();
  let qs = 'uid=' + encodeURIComponent(uid) + '&view=' + encodeURIComponent(view) + '&field=' + encodeURIComponent(field);
  if (anchor) qs += '&date=' + encodeURIComponent(anchor);
  if (shift) qs += '&shift=' + shift;
  try {
    const resp = await fetch('/api/client-calendar-grid?' + qs, {cache: 'no-store'});
    if (resp.status === 404) { node.textContent = 'Your account is not enabled yet — please contact the administrator.'; return; }
    if (!resp.ok) { node.textContent = 'Calendar unavailable (' + resp.status + ').'; return; }
    const g = await resp.json();
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
        return `<div class="month-box" onclick="calendarZoomTo('${m.value}-01', 'month')"><b>${esc(m.label)}</b><div class="small">${count} opportunities</div><div class="bar-track"><div class="bar-fill" style="width:${width}%"></div></div></div>`;
      }).join('');
      node.innerHTML = `<div class="calendar-board"><div class="calendar-title"><h3>${esc(title)}</h3><span class="small">Click a month to open it.</span></div><div class="year-grid">${boxes}</div></div>`;
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
      const note = evs.length ? 'Click an event to open the opportunity.' : 'No opportunities on this day.';
      node.innerHTML = `<div class="calendar-board"><div class="calendar-title"><h3>${esc(title)}</h3><span class="small">${note}</span></div><div class="timeline-scroll"><div class="timeline">${notimeRow}${hourRows}</div></div></div>`;
      return;
    }
    if (view === 'week') {
      const cols = days.map((day, i) => {
        const evs = grouped[day.iso] || [];
        const byHour = Array.from({length: 24}, () => []);
        evs.forEach(ev => { if (hasTime(ev)) byHour[hourOf(ev)].push(ev); });
        return {i, iso: day.iso, notime: evs.filter(ev => !hasTime(ev)), byHour};
      });
      const head = '<div class="wk-corner"></div>' + cols.map(c => `<div class="wk-head zoomable" onclick="calendarZoomTo('${c.iso}', 'day')">${WEEKDAY_LABELS[c.i] || ''} ${esc((c.iso || '').slice(5))}</div>`).join('');
      const anyNotime = cols.some(c => c.notime.length);
      const notimeRow = anyNotime
        ? '<div class="hour-label wk-notime">No time</div>' + cols.map(c => `<div class="hour-lane wk-notime">${c.notime.map(renderCalendarEvent).join('')}</div>`).join('') : '';
      let hourRows = '';
      for (let h = 0; h < 24; h++) {
        hourRows += `<div class="hour-label">${String(h).padStart(2, '0')}:00</div>`;
        hourRows += cols.map(c => `<div class="hour-lane">${c.byHour[h].map(renderCalendarEvent).join('')}</div>`).join('');
      }
      node.innerHTML = `<div class="calendar-board"><div class="calendar-title"><h3>${esc(title)}</h3><span class="small">Click a day header to zoom to that day.</span></div><div class="timeline-scroll"><div class="week-timeline">${head}${notimeRow}${hourRows}</div></div></div>`;
      return;
    }
    let cells = WEEKDAY_LABELS.map(d => `<div class="dow">${d}</div>`).join('');
    for (let i = 0; i < (g.first_weekday || 0); i++) cells += renderCalendarDayCell(null, [], true);
    cells += days.map(day => renderCalendarDayCell(day, grouped[day.iso] || [], false)).join('');
    node.innerHTML = `<div class="calendar-board"><div class="calendar-title"><h3>${esc(title)}</h3><span class="small">Click a day to zoom to its week.</span></div><div class="calgrid">${cells}</div></div>`;
  } catch (err) {
    node.textContent = 'Calendar unavailable: ' + err;
  }
}

// ---- Sign-in (Firebase Auth: email/password + Google) ----
function loadScript(src) {
  return new Promise((resolve, reject) => {
    const s = document.createElement('script');
    s.src = src;
    s.onload = resolve;
    s.onerror = () => reject(new Error('could not load ' + src));
    document.head.appendChild(s);
  });
}
function authError(e) { document.getElementById('auth-error').textContent = (e && e.message) || String(e); }
function emailSignIn() {
  const email = document.getElementById('auth-email').value.trim();
  const password = document.getElementById('auth-password').value;
  if (!email || !password) { authError('Enter your email and password.'); return; }
  firebase.auth().signInWithEmailAndPassword(email, password).catch(authError);
}
function emailSignUp() {
  const email = document.getElementById('auth-email').value.trim();
  const password = document.getElementById('auth-password').value;
  if (!email || !password) { authError('Enter an email and a password (6+ characters).'); return; }
  firebase.auth().createUserWithEmailAndPassword(email, password).catch(authError);
}
function googleSignIn() {
  firebase.auth().signInWithPopup(new firebase.auth.GoogleAuthProvider()).catch(authError);
}
function doSignOut() {
  if (window.firebase && firebase.auth) firebase.auth().signOut();
}
async function afterSignIn(user) {
  existingProfile = null;
  try {
    const resp = await fetch('/api/client-profile?uid=' + encodeURIComponent(user.uid), {cache: 'no-store'});
    if (resp.ok) existingProfile = await resp.json();
  } catch (e) { /* offline profile check: fall through to the form */ }
  const email = existingProfile && (existingProfile.email || '').trim();
  const phone = existingProfile && (existingProfile.phone || '').trim();
  if (!email || !phone) {
    document.getElementById('profile-email').value = email || user.email || '';
    document.getElementById('profile-phone').value = phone || user.phoneNumber || '';
    show('profile-screen');
    return;
  }
  enterApp(user);
}
function enterApp(user) {
  document.getElementById('user-box').innerHTML =
    esc(user.email || user.displayName || user.uid) + ' &middot; <a href="#" onclick="doSignOut(); return false;">Sign out</a>';
  show('app-screen');
  loadCalendar(0);
}
async function saveProfile() {
  const errBox = document.getElementById('profile-error');
  const email = document.getElementById('profile-email').value.trim();
  const phone = document.getElementById('profile-phone').value.trim();
  if (!email) { errBox.textContent = 'Email is required.'; return; }
  if (!phone) { errBox.textContent = 'Phone number is required.'; return; }
  const payload = {firebase_uid: uid, email: email, phone: phone};
  if (!existingProfile) payload.name = (fbUser && fbUser.displayName) || email.split('@')[0];
  try {
    const resp = await fetch('/api/client-profile', {
      method: 'POST',
      headers: {'Content-Type': 'application/x-www-form-urlencoded'},
      body: 'profile=' + encodeURIComponent(JSON.stringify(payload)),
    });
    if (!resp.ok) { errBox.textContent = await resp.text(); return; }
  } catch (e) { errBox.textContent = 'Could not save: ' + e; return; }
  enterApp(fbUser);
}
async function boot() {
  if (legacyMode) { show('app-screen'); loadCalendar(0); return; }
  let cfg;
  try {
    cfg = await (await fetch('/api/client-auth-config', {cache: 'no-store'})).json();
  } catch (e) { bootText('Server unavailable: ' + e); return; }
  if (!cfg.configured) {
    bootText('Client sign-in is not configured yet. Ask the administrator to fill in the Firebase web app settings on the monitor Settings tab.');
    return;
  }
  try {
    await loadScript('https://www.gstatic.com/firebasejs/10.12.2/firebase-app-compat.js');
    await loadScript('https://www.gstatic.com/firebasejs/10.12.2/firebase-auth-compat.js');
  } catch (e) { bootText('Could not load the sign-in library — internet access is needed to log in.'); return; }
  firebase.initializeApp({apiKey: cfg.apiKey, authDomain: cfg.authDomain, projectId: cfg.projectId, appId: cfg.appId});
  firebase.auth().onAuthStateChanged(user => {
    fbUser = user;
    if (!user) { show('auth-screen'); return; }
    uid = user.uid;
    afterSignIn(user);
  });
}
boot();
</script>
</body>
</html>
"""

# Front-page login (/) shown to any non-localhost visitor without an admin
# session. One Firebase sign-in for everyone: emails in PC_ADMIN_EMAILS get
# the admin dashboard, anyone else is sent to /client-calendar. Requests from
# 127.0.0.1 never see this page (they go straight to the admin dashboard).
LOGIN_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PanamaCompra Monitor — Sign in</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Archivo:wght@600;700;800&family=IBM+Plex+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>
/* ARL-89 (HP-23 Design System) tokens — hi-vis amber, charcoal ink, warm
   concrete neutrals, blueprint accent. Source: claude.ai/design project
   129dfa45 tokens/colors.css + typography.css + spacing.css. */
:root {
  --amber-50: #FFF6E0; --amber-100: #FFE9B3; --amber-200: #FFD773; --amber-300: #FFC53D;
  --amber-400: #FFB400; --amber-500: #F5A300; --amber-600: #D98A00; --amber-700: #B06E00; --amber-800: #855200;
  --ink-900: #121417; --ink-800: #1A1D21; --ink-700: #23262B; --ink-600: #2E3338;
  --concrete-0: #FFFFFF; --concrete-50: #F6F5F2; --concrete-100: #ECEAE5; --concrete-200: #DEDBD4;
  --concrete-300: #C8C4BB; --concrete-400: #A6A199; --concrete-500: #7D7872; --concrete-600: #585450;
  --blueprint-400: #3D7DFF; --blueprint-500: #2D6CDF; --blueprint-600: #1F54B5;
  --green-500: #2F9E5B; --green-600: #247A47; --red-500: #D63B26; --red-600: #AE2D1C;
  --blue-tint: #EDF3FF; --green-tint: #E6F4EC; --red-tint: #FBE9E6;
  --font-display: 'Archivo', 'Helvetica Neue', Arial, sans-serif;
  --font-sans: 'IBM Plex Sans', 'Helvetica Neue', Arial, sans-serif;
  --font-mono: 'IBM Plex Mono', ui-monospace, 'SFMono-Regular', Menlo, monospace;
  --radius-sm: 4px; --radius-md: 6px; --radius-lg: 10px; --radius-pill: 999px;
  --shadow-sm: 0 1px 2px rgba(18,20,23,.10), 0 1px 1px rgba(18,20,23,.05);
  --shadow-md: 0 4px 12px rgba(18,20,23,.10);
  --shadow-lg: 0 12px 30px rgba(18,20,23,.16);
  --focus-ring: 0 0 0 3px rgba(45,108,223,.35);
}
body { font-family: var(--font-sans); margin: 10px; background: var(--concrete-50); color: var(--ink-700); }
.small { color: var(--concrete-500); font-size: .85rem; }
.err { color: var(--red-600); font-size: .85rem; min-height: 1.2em; }
input, button { border-radius: var(--radius-md); border: 1px solid var(--concrete-300); background: var(--concrete-0); color: var(--ink-800); padding: 8px 10px; font-size: .95rem; font-family: var(--font-sans); }
input:focus { outline: none; border-color: var(--blueprint-500); box-shadow: var(--focus-ring); }
button { cursor: pointer; font-weight: 600; color: var(--ink-900); }
button:hover { background: var(--concrete-100); }
button.primary { background: var(--amber-500); border-color: var(--amber-600); color: var(--ink-900); font-weight: 700; }
button.primary:hover { background: var(--amber-600); }
.auth-card { max-width: 380px; margin: 10vh auto 0; background: var(--concrete-0); border: 1px solid var(--concrete-200); border-top: 4px solid var(--amber-500); border-radius: var(--radius-lg); padding: 22px; display: flex; flex-direction: column; gap: 10px; box-shadow: var(--shadow-md); }
.auth-card h1 { margin: 0 0 4px; font-size: 1.2rem; color: var(--ink-900); font-family: var(--font-display); letter-spacing: -0.015em; }
.auth-card input { width: 100%; box-sizing: border-box; }
.auth-sep { text-align: center; color: var(--concrete-400); font-size: .8rem; font-family: var(--font-mono); text-transform: uppercase; letter-spacing: 0.14em; }
</style>
</head>
<body>
<div id="login-card" class="auth-card">
  <h1>PanamaCompra Monitor</h1>
  <p class="small" id="login-note">Sign in to continue. Managers open the admin monitor; clients open their opportunity calendar.</p>
  <div id="manager-form">
    <div class="auth-sep" style="margin-bottom:8px">Manager</div>
    <input id="admin-user" placeholder="Username" autocomplete="username" style="margin-bottom:10px" onkeydown="if (event.key === 'Enter') managerSignIn()">
    <input id="admin-password" type="password" placeholder="Password" autocomplete="current-password" style="margin-bottom:10px" onkeydown="if (event.key === 'Enter') managerSignIn()">
    <div class="err" id="admin-error"></div>
    <button class="primary" style="width:100%" onclick="managerSignIn()">Manager sign in</button>
  </div>
  <div id="login-form" hidden>
    <div class="auth-sep" style="margin:8px 0">Client</div>
    <input id="auth-email" type="email" placeholder="Email" autocomplete="email" style="margin-bottom:10px">
    <input id="auth-password" type="password" placeholder="Password" autocomplete="current-password" style="margin-bottom:10px">
    <div class="err" id="auth-error"></div>
    <button class="primary" style="width:100%;margin-bottom:8px" onclick="emailSignIn()">Sign in</button>
    <button style="width:100%;margin-bottom:8px" onclick="emailSignUp()">Create account</button>
    <div class="auth-sep" style="margin-bottom:8px">&mdash; or &mdash;</div>
    <button style="width:100%" onclick="googleSignIn()">Sign in with Google</button>
  </div>
</div>
<script>
function note(text) { document.getElementById('login-note').textContent = text; }
function authError(e) { document.getElementById('auth-error').textContent = (e && e.message) || String(e); }
async function managerSignIn() {
  const errBox = document.getElementById('admin-error');
  const user = document.getElementById('admin-user').value.trim();
  const password = document.getElementById('admin-password').value;
  if (!user || !password) { errBox.textContent = 'Enter the manager username and password.'; return; }
  let resp;
  try {
    resp = await fetch('/api/session-login', {
      method: 'POST',
      headers: {'Content-Type': 'application/x-www-form-urlencoded'},
      body: 'user=' + encodeURIComponent(user) + '&password=' + encodeURIComponent(password),
    });
  } catch (e) { errBox.textContent = String(e); return; }
  if (!resp.ok) { errBox.textContent = (await resp.text()).trim(); return; }
  location.replace('/');
}
function loadScript(src) {
  return new Promise((resolve, reject) => {
    const s = document.createElement('script');
    s.src = src;
    s.onload = resolve;
    s.onerror = () => reject(new Error('could not load ' + src));
    document.head.appendChild(s);
  });
}
async function submitToken(user) {
  // Cookie-refused loop guard: don't auto-retry the handshake forever.
  const attempts = Number(sessionStorage.getItem('pc_login_attempts') || '0');
  if (attempts > 2) { note('Signed in, but the session cookie is not being kept. Enable cookies for this site and reload.'); return; }
  sessionStorage.setItem('pc_login_attempts', String(attempts + 1));
  let idToken;
  try { idToken = await user.getIdToken(); } catch (e) { authError(e); return; }
  let resp;
  try {
    resp = await fetch('/api/session-login', {
      method: 'POST',
      headers: {'Content-Type': 'application/x-www-form-urlencoded'},
      body: 'idToken=' + encodeURIComponent(idToken),
    });
  } catch (e) { authError(e); return; }
  if (!resp.ok) { authError(await resp.text()); return; }
  const data = await resp.json();
  if (data.role === 'admin') { location.replace('/'); return; }
  sessionStorage.removeItem('pc_login_attempts');
  location.replace('/client-calendar');
}
function emailSignIn() {
  const email = document.getElementById('auth-email').value.trim();
  const password = document.getElementById('auth-password').value;
  if (!email || !password) { authError('Enter your email and password.'); return; }
  sessionStorage.removeItem('pc_login_attempts');
  firebase.auth().signInWithEmailAndPassword(email, password).catch(authError);
}
function emailSignUp() {
  const email = document.getElementById('auth-email').value.trim();
  const password = document.getElementById('auth-password').value;
  if (!email || !password) { authError('Enter an email and a password (6+ characters).'); return; }
  sessionStorage.removeItem('pc_login_attempts');
  firebase.auth().createUserWithEmailAndPassword(email, password).catch(authError);
}
function googleSignIn() {
  sessionStorage.removeItem('pc_login_attempts');
  firebase.auth().signInWithPopup(new firebase.auth.GoogleAuthProvider()).catch(authError);
}
async function boot() {
  const signedOut = new URLSearchParams(location.search).get('signedout') === '1';
  let cfg;
  try {
    cfg = await (await fetch('/api/client-auth-config', {cache: 'no-store'})).json();
  } catch (e) { note('Server unavailable: ' + e); return; }
  if (!cfg.configured) {
    note('Client sign-in is not configured yet (Firebase web app settings are blank) — managers can still sign in above.');
    return;
  }
  try {
    await loadScript('https://www.gstatic.com/firebasejs/10.12.2/firebase-app-compat.js');
    await loadScript('https://www.gstatic.com/firebasejs/10.12.2/firebase-auth-compat.js');
  } catch (e) { note('Could not load the sign-in library — internet access is needed to log in.'); return; }
  firebase.initializeApp({apiKey: cfg.apiKey, authDomain: cfg.authDomain, projectId: cfg.projectId, appId: cfg.appId});
  document.getElementById('login-form').hidden = false;
  if (signedOut) {
    history.replaceState(null, '', '/');
    sessionStorage.removeItem('pc_login_attempts');
    try { await firebase.auth().signOut(); } catch (e) { /* already signed out */ }
  }
  firebase.auth().onAuthStateChanged(user => { if (user) submitToken(user); });
}
boot();
</script>
</body>
</html>
"""

HTML = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PanamaCompra Monitor</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Archivo:wght@600;700;800&family=IBM+Plex+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>
/* ARL-89 (HP-23 Design System) tokens — hi-vis amber, charcoal ink, warm
   concrete neutrals, blueprint accent. Source: claude.ai/design project
   129dfa45 tokens/colors.css + typography.css + spacing.css. */
:root {{
  --amber-50: #FFF6E0; --amber-100: #FFE9B3; --amber-200: #FFD773; --amber-300: #FFC53D;
  --amber-400: #FFB400; --amber-500: #F5A300; --amber-600: #D98A00; --amber-700: #B06E00; --amber-800: #855200;
  --ink-900: #121417; --ink-800: #1A1D21; --ink-700: #23262B; --ink-600: #2E3338;
  --concrete-0: #FFFFFF; --concrete-50: #F6F5F2; --concrete-100: #ECEAE5; --concrete-200: #DEDBD4;
  --concrete-300: #C8C4BB; --concrete-400: #A6A199; --concrete-500: #7D7872; --concrete-600: #585450;
  --blueprint-400: #3D7DFF; --blueprint-500: #2D6CDF; --blueprint-600: #1F54B5;
  --green-500: #2F9E5B; --green-600: #247A47; --red-500: #D63B26; --red-600: #AE2D1C;
  --blue-tint: #EDF3FF; --green-tint: #E6F4EC; --red-tint: #FBE9E6;
  --font-display: 'Archivo', 'Helvetica Neue', Arial, sans-serif;
  --font-sans: 'IBM Plex Sans', 'Helvetica Neue', Arial, sans-serif;
  --font-mono: 'IBM Plex Mono', ui-monospace, 'SFMono-Regular', Menlo, monospace;
  --radius-sm: 4px; --radius-md: 6px; --radius-lg: 10px; --radius-pill: 999px;
  --shadow-sm: 0 1px 2px rgba(18,20,23,.10), 0 1px 1px rgba(18,20,23,.05);
  --shadow-md: 0 4px 12px rgba(18,20,23,.10);
  --shadow-lg: 0 12px 30px rgba(18,20,23,.16);
  --focus-ring: 0 0 0 3px rgba(45,108,223,.35);
}}
body {{ font-family: var(--font-sans); margin: 24px; background: var(--concrete-50); color: var(--ink-700); }}
a {{ color: var(--blueprint-600); }}
h1, h2, h3 {{ font-family: var(--font-display); color: var(--ink-900); letter-spacing: -0.015em; }}
.card {{ background: var(--concrete-0); border: 1px solid var(--concrete-200); border-radius: var(--radius-lg); padding: 18px; margin: 0 0 16px; box-shadow: var(--shadow-sm); overflow-x: auto; }}
.subsection {{ overflow-x: auto; }}
h1 {{ margin-top: 0; }}
/* Uniform vertical rhythm inside cards/subsections. */
.card p, .subsection p {{ margin: 8px 0 12px; }}
.bar {{ height: 30px; background: var(--concrete-100); border-radius: var(--radius-pill); overflow: hidden; border: 1px solid var(--concrete-300); }}
.fill {{ height: 100%; width: 0%; background: linear-gradient(90deg, var(--amber-400), var(--amber-500)); display: flex; align-items: center; justify-content: center; color: var(--ink-900); font-weight: 700; transition: width .4s ease; }}
table {{ border-collapse: collapse; width: 100%; }}
th, td {{ text-align: left; border-bottom: 1px solid var(--concrete-100); padding: 7px 10px; vertical-align: top; }}
th {{ width: 220px; color: var(--concrete-500); font-family: var(--font-mono); font-size: .78rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.08em; }}
pre {{ white-space: pre-wrap; font-family: var(--font-mono); background: var(--concrete-100); color: var(--ink-800); border: 1px solid var(--concrete-200); border-radius: var(--radius-md); padding: 12px; max-height: 360px; overflow: auto; }}
pre.log-pane {{ max-height: 180px; min-height: 2.8rem; }}
/* Process status is a tidy flex grid of small chips instead of one crowded
   wrapped line: green = RUNNING, gray = off, even gaps. */
.proc-wrap {{ display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }}
.pill {{ display: inline-block; padding: 4px 10px; border-radius: var(--radius-pill); font-weight: 600; font-size: .8rem; font-family: var(--font-mono); }}
.on {{ background: var(--green-tint); color: var(--green-600); border: 1px solid var(--green-500); }} .off {{ background: var(--concrete-100); color: var(--concrete-500); border: 1px solid var(--concrete-200); }}
.message {{ font-size: 1.15rem; color: var(--ink-900); font-weight: 600; }}
.small {{ color: var(--concrete-500); font-size: 0.90rem; }}
.xs {{ color: var(--concrete-500); font-size: 0.78rem; }}
.done {{ color: var(--green-600); font-weight: 700; }}
button {{ background: var(--concrete-0); color: var(--ink-900); border: 1px solid var(--concrete-300); border-radius: var(--radius-md); padding: 9px 14px; font-weight: 600; font-family: var(--font-sans); cursor: pointer; margin: 0 10px 10px 0; transition: background .15s ease, transform .05s ease, border-color .15s ease; }}
/* Inline label+input pairs align on one baseline (settings-grid labels keep
   their own column layout — this only targets labels inside paragraphs). */
p > label.small {{ display: inline-flex; align-items: center; gap: 6px; max-width: 100%; vertical-align: middle; margin: 2px 0; }}
button:hover {{ background: var(--concrete-100); border-color: var(--concrete-400); }}
button:active {{ transform: translateY(1px); }}
button:disabled {{ background: var(--concrete-100); color: var(--concrete-400); border-color: var(--concrete-200); cursor: not-allowed; transform: none; }}
button.primary {{ background: var(--amber-500); border-color: var(--amber-600); color: var(--ink-900); font-weight: 700; }}
button.primary:hover {{ background: var(--amber-600); }}
button.primary:disabled {{ background: var(--amber-100); color: var(--concrete-400); }}
.zone {{ margin-top: 14px; padding-top: 8px; border-top: 1px solid var(--concrete-200); }}
.zone h3 {{ margin: 0 0 2px; color: var(--ink-900); }}
.zone-desc {{ margin: 0 0 8px; color: var(--concrete-500); }}
.danger {{ background: var(--red-500); border-color: var(--red-600); color: #fff; }} .danger:hover {{ background: var(--red-600); }}
textarea {{ width: 100%; min-height: 80px; border-radius: var(--radius-md); border: 1px solid var(--concrete-300); background: var(--concrete-0); color: var(--ink-800); padding: 10px; font-family: var(--font-mono); }}
select, input {{ border-radius: var(--radius-md); border: 1px solid var(--concrete-300); background: var(--concrete-0); color: var(--ink-800); padding: 6px 8px; font-size: 1rem; font-family: var(--font-sans); }}
select:focus, input:focus, textarea:focus {{ outline: none; border-color: var(--blueprint-500); box-shadow: var(--focus-ring); }}
input:disabled {{ opacity: .5; cursor: not-allowed; }}
/* Grouped control rows (Run controls and friends): a label chip on the left,
   controls flowing after it, action buttons pushed together sensibly. */
.control-row {{ display: flex; flex-wrap: wrap; align-items: center; gap: 10px; margin: 10px 0; padding: 9px 12px; background: var(--concrete-50); border: 1px solid var(--concrete-100); border-radius: var(--radius-md); }}
.control-row .control-label {{ font-family: var(--font-mono); font-size: .72rem; font-weight: 600; text-transform: uppercase; letter-spacing: .08em; color: var(--concrete-500); min-width: 58px; flex: 0 0 auto; }}
.control-row button {{ margin: 0; }}
.control-row label.small {{ display: inline-flex; align-items: center; gap: 6px; margin: 0; }}
.control-sep {{ flex: 1 1 12px; }}
/* Run mode as radio toggles. */
.mode-group {{ display: inline-flex; flex-wrap: wrap; gap: 4px; vertical-align: middle; max-width: 100%; }}
.mode-group label {{ display: inline-flex; align-items: center; gap: 6px; padding: 6px 12px; border: 1px solid var(--concrete-300); border-radius: var(--radius-md); background: var(--concrete-0); cursor: pointer; font-weight: 600; color: var(--ink-700); }}
.mode-group input {{ accent-color: var(--amber-600); margin: 0; }}
.mode-group label:has(input:checked) {{ border-color: var(--amber-600); color: var(--ink-900); background: var(--amber-50); }}
.mode-group input:disabled + span, .mode-group label:has(input:disabled) {{ opacity: .5; cursor: not-allowed; }}
select#record-index {{ min-width: 80%; max-width: 100%; min-height: 14rem; font-family: var(--font-mono); font-size: .9rem; line-height: 1.35; }}
.settings-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 10px 14px; align-items: end; margin: 10px 0; }}
.settings-grid label {{ display: flex; flex-direction: column; gap: 4px; margin: 0; }}
.destination-grid {{ display: grid; grid-template-columns: minmax(180px, 240px) minmax(280px, 1fr); gap: 8px 12px; align-items: center; margin: 10px 0; }}
.destination-grid label {{ color: var(--concrete-500); }}
.destination-grid input, .destination-grid textarea {{ width: 100%; box-sizing: border-box; }}
.settings-grid input {{ width: 100%; box-sizing: border-box; }}
/* Card/section headings are flex rows so the Show/Hide toggle (and LIVE
   badges) stay aligned INSIDE the card instead of floating out of it. */
.card > h1, .card > h2, .subsection > h3 {{ display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }}
.section-toggle {{ float: none; margin: 0 0 0 auto; padding: 5px 10px; font-size: .8rem; }}
.card.collapsed > *:not(h1):not(h2) {{ display: none; }}
#diagnostics td {{ font-variant-numeric: tabular-nums; word-break: break-word; user-select: text; font-family: var(--font-mono); font-size: .88rem; }}
.tab-nav {{ display: flex; flex-wrap: wrap; gap: 8px; margin: 12px 0 18px; position: sticky; top: 0; z-index: 5; background: #F6F5F2E6; backdrop-filter: blur(8px); padding: 8px 0; border-bottom: 2px solid var(--ink-900); }}
.tab-nav button.active {{ background: var(--ink-900); border-color: var(--ink-900); color: var(--amber-400); }}
.card[data-tab] {{ display: none; }}
.card[data-tab].tab-active {{ display: block; }}
.subsection {{ border: 1px solid var(--concrete-200); border-radius: var(--radius-lg); padding: 12px; margin: 10px 0; background: var(--concrete-50); }}
.subsection h3 {{ margin: 0 0 8px; color: var(--ink-900); }}
.setting-actions {{ display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }}
.kpi-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; }}
.kpi {{ background: var(--concrete-0); border: 2px solid var(--ink-900); border-radius: var(--radius-lg); padding: 14px; box-shadow: var(--shadow-sm); }}
.kpi b {{ display: block; font-size: 1.7rem; color: var(--ink-900); font-family: var(--font-display); }}
.diagram-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 14px; }}
/* Blueprint board: faint grid behind every chart (drafting-paper feel) and
   a pulsing LIVE beacon next to the last-refresh stamp. */
.chart {{ background: var(--concrete-0) linear-gradient(rgba(45,108,223,.06) 1px, transparent 1px) 0 0 / 100% 22px, var(--concrete-0) linear-gradient(90deg, rgba(45,108,223,.05) 1px, transparent 1px) 0 0 / 22px 100%; border: 1px solid var(--concrete-200); border-radius: var(--radius-lg); padding: 12px; min-height: 180px; transition: border-color .2s ease, box-shadow .2s ease; }}
.chart:hover {{ border-color: var(--blueprint-500); box-shadow: var(--shadow-md); }}
.kpi-live {{ color: var(--blueprint-500); font-weight: 700; }}
.kpi-live::before {{ content: '●'; margin-right: 5px; animation: kpiPulse 1.6s ease-in-out infinite; }}
@keyframes kpiPulse {{ 0%, 100% {{ opacity: 1; }} 50% {{ opacity: .25; }} }}
.kpi-filter-bar {{ display: flex; flex-wrap: wrap; gap: 10px; align-items: center; margin: 8px 0 12px; padding: 8px 10px; border: 1px solid var(--concrete-200); border-radius: var(--radius-lg); background: var(--concrete-50); }}
.item-line {{ border-left: 3px solid var(--blueprint-500); padding: 3px 8px; margin: 4px 0; font-size: .85rem; color: var(--ink-700); }}
.item-line b {{ color: var(--ink-900); }}
/* Ubuntu-style opportunity calendar with boxed events inside each date cell. */
.calendar-board {{ background: var(--concrete-0); border: 1px solid var(--concrete-200); border-radius: var(--radius-lg); overflow: hidden; box-shadow: var(--shadow-sm); }}
.calendar-title {{ display: flex; justify-content: space-between; gap: 10px; align-items: center; padding: 10px 12px; background: var(--ink-900); border-bottom: 2px solid var(--amber-500); }}
.calendar-title h3 {{ margin: 0; color: var(--concrete-0); }}
.calendar-title .small {{ color: var(--concrete-300); }}
.cal-controls-row {{ display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin: 0 0 4px; }}
.cal-controls-row label {{ white-space: nowrap; }}
.cal-controls-row input#cal-date {{ width: 118px; box-sizing: border-box; }}
.calgrid {{ display: grid; grid-template-columns: 48px repeat(7, minmax(0, 1fr)); }}
.week-number-header {{ color: var(--concrete-500); font-family: var(--font-mono); font-size: .74rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.06em; padding: 7px 4px; text-align: center; border-bottom: 1px solid var(--concrete-200); background: var(--concrete-50); }}
.week-number {{ min-height: 142px; border: 0; border-right: 1px solid var(--concrete-200); border-bottom: 1px solid var(--concrete-200); padding: 6px 3px; background: var(--concrete-50); color: var(--concrete-500); font-family: var(--font-mono); font-size: .78rem; font-weight: 600; cursor: pointer; }}
.week-number:hover {{ background: var(--amber-50); color: var(--ink-900); box-shadow: inset 0 0 0 1px var(--amber-500); }}
.calgrid .dow {{ text-align: center; color: var(--concrete-600); font-family: var(--font-mono); font-size: .78rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.06em; padding: 7px 4px; border-bottom: 1px solid var(--concrete-200); background: var(--concrete-50); }}
.calcell {{ min-height: 142px; border-right: 1px solid var(--concrete-100); border-bottom: 1px solid var(--concrete-100); padding: 6px; cursor: pointer; background: var(--concrete-0); transition: border-color .15s ease, box-shadow .15s ease, background .15s ease; overflow: hidden; }}
.calcell:hover {{ background: var(--amber-50); box-shadow: inset 0 0 0 1px var(--amber-500); }}
.calcell.blank {{ background: var(--concrete-50); cursor: default; }}
.calcell.today {{ box-shadow: inset 0 0 0 2px var(--amber-500); }}
.calcell .num {{ color: var(--ink-900); font-size: .95rem; font-weight: 700; display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px; }}
.calcell .count {{ color: var(--concrete-500); font-size: .82rem; font-weight: 400; font-family: var(--font-mono); }}
.calevent {{ display: -webkit-box; margin: 4px 0; padding: 4px 6px; border-radius: var(--radius-sm); border-left: 3px solid var(--blueprint-500); background: var(--blue-tint); color: var(--ink-800); font-size: .85rem; line-height: 1.25; white-space: normal; overflow: hidden; text-overflow: ellipsis; -webkit-box-orient: vertical; -webkit-line-clamp: 2; line-clamp: 2; max-height: 2.5em; }}
a.calevent {{ text-decoration: none; cursor: pointer; }}
a.calevent:hover {{ filter: brightness(.94); }}
.loc-tooltip {{ position: fixed; z-index: 9999; max-width: 340px; background: var(--concrete-0); color: var(--ink-800); border: 2px solid var(--ink-900); border-radius: var(--radius-md); padding: 8px 10px; font-size: .8rem; line-height: 1.35; box-shadow: var(--shadow-lg); pointer-events: none; display: none; }}
.loc-tooltip .loc-head {{ font-weight: 700; color: var(--blueprint-600); margin-bottom: 2px; font-family: var(--font-mono); }}
.loc-tooltip .loc-desc {{ color: var(--concrete-600); margin-bottom: 6px; white-space: normal; }}
.loc-tooltip b {{ color: var(--ink-900); }}
.calevent.soon {{ border-left-color: var(--amber-500); background: var(--amber-100); color: var(--amber-800); }}
.calevent.expired {{ border-left-color: var(--red-500); background: var(--red-tint); color: var(--red-600); }}
.calevent.more {{ border-left-color: var(--concrete-400); background: var(--concrete-100); color: var(--concrete-600); }}
/* Day/week views: hourly timeline (hour rows, events placed at their hour)
   instead of a flat list or a day-chip grid. */
.timeline-scroll {{ max-height: 640px; overflow-y: auto; border-top: 1px solid var(--concrete-200); }}
.timeline {{ display: grid; grid-template-columns: 64px 1fr; }}
.week-timeline {{ display: grid; grid-template-columns: 64px repeat(7, minmax(0, 1fr)); }}
.hour-label {{ color: var(--concrete-500); font-family: var(--font-mono); font-weight: 600; font-size: .74rem; padding: 6px 8px; text-align: right; border-bottom: 1px solid var(--concrete-100); border-right: 1px solid var(--concrete-200); background: var(--concrete-50); }}
.hour-lane {{ min-height: 34px; padding: 4px 8px; display: flex; flex-direction: column; gap: 4px; border-bottom: 1px solid var(--concrete-100); }}
.week-timeline .hour-lane {{ padding: 3px; gap: 3px; border-right: 1px solid var(--concrete-100); }}
.timeline-notime .hour-label, .timeline-notime .hour-lane, .wk-notime {{ background: var(--concrete-100); border-bottom: 2px solid var(--concrete-300); }}
.timeline-collapsed .hour-label, .timeline-collapsed .hour-lane, .wk-collapsed {{ background: var(--concrete-100); border-bottom: 2px solid var(--concrete-300); }}
.wk-head {{ padding: 7px 6px; text-align: center; font-family: var(--font-mono); font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; color: var(--concrete-600); font-size: .76rem; border-bottom: 1px solid var(--concrete-200); background: var(--concrete-50); position: sticky; top: 0; z-index: 1; }}
.wk-head.zoomable {{ cursor: pointer; }}
.wk-head.zoomable:hover {{ background: var(--amber-100); color: var(--ink-900); }}
/* Keyword filter gets its own full row (hi-vis amber) under the calendar
   controls so the active filter is always visible at a glance. */
.cal-filter-row {{ display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin: 6px 0 2px; padding: 8px 10px; background: var(--amber-50); border: 1px solid var(--amber-300); border-radius: var(--radius-md); }}
.cal-filter-row > label {{ display: flex; flex: 1 1 320px; min-width: 0; align-items: center; gap: 6px; }}
.cal-filter-row > label b {{ flex: 0 0 auto; color: var(--ink-900); }}
.cal-filter-row input {{ flex: 1 1 180px; min-width: 0; width: auto; max-width: 280px; box-sizing: border-box; background: var(--concrete-0); color: var(--ink-800); border: 1px solid var(--amber-300); border-radius: var(--radius-md); padding: 6px 8px; }}
.cal-filter-row button {{ margin: 0; padding: 6px 12px; }}
.cal-filter-row .xs {{ flex: 1 1 220px; min-width: 0; color: var(--amber-800); }}
.cal-display-row {{ display: flex; flex-wrap: wrap; gap: 12px; align-items: center; margin: 6px 0 2px; padding: 6px 10px; background: var(--concrete-50); border: 1px solid var(--concrete-200); border-radius: var(--radius-md); }}
.cal-display-row label {{ white-space: nowrap; }}
.calendar-warning {{ margin: 8px 10px 0; padding: 7px 10px; border: 1px solid var(--amber-500); border-radius: var(--radius-md); background: var(--amber-50); color: var(--amber-800); font-size: .82rem; line-height: 1.35; }}
.wk-corner {{ background: var(--concrete-50); border-bottom: 1px solid var(--concrete-200); position: sticky; top: 0; z-index: 1; }}
.agenda-empty {{ padding: 16px; }}
.year-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 8px; padding: 10px; }}
.month-box {{ border: 1px solid var(--concrete-200); border-radius: var(--radius-md); padding: 10px; background: var(--concrete-0); cursor: pointer; }}
.month-box:hover {{ border-color: var(--amber-500); background: var(--amber-50); }}
.month-box b {{ color: var(--ink-900); font-family: var(--font-display); }}
.month-box .bar-track {{ margin-top: 8px; }}
.bar-row {{ display: grid; grid-template-columns: minmax(90px, 1fr) 4fr 48px; gap: 8px; align-items: center; margin: 7px 0; font-size: .9rem; }}
.bar-track {{ height: 12px; background: var(--concrete-200); border-radius: var(--radius-pill); overflow: hidden; }}
.bar-fill {{ height: 100%; background: linear-gradient(90deg, var(--amber-400), var(--amber-600)); border-radius: var(--radius-pill); }}
.keyword-cloud span {{ display: inline-block; margin: 4px; padding: 5px 8px; border-radius: var(--radius-pill); background: var(--concrete-100); color: var(--ink-700); border: 1px solid var(--concrete-200); }}
/* Slim scrollbars for the log panes. */
pre::-webkit-scrollbar {{ width: 10px; height: 10px; }}
pre::-webkit-scrollbar-track {{ background: var(--concrete-100); border-radius: var(--radius-md); }}
pre::-webkit-scrollbar-thumb {{ background: var(--concrete-300); border-radius: var(--radius-md); }}
pre::-webkit-scrollbar-thumb:hover {{ background: var(--concrete-400); }}
.record-grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }}
.record-card {{ border-radius: var(--radius-lg); padding: 14px; font-weight: 600; white-space: pre-line; }}
.record-pending {{ background: var(--red-tint); color: var(--red-600); border: 1px solid var(--red-500); }}
.record-completed {{ background: var(--green-tint); color: var(--green-600); border: 1px solid var(--green-500); }}
.waha-alert {{ background: var(--amber-50); border: 2px solid var(--amber-500); border-radius: var(--radius-lg); padding: 12px 16px; margin: 0 0 16px; color: var(--ink-900); }}
.waha-alert button {{ margin-left: 10px; }}
.waha-alert img {{ display: block; margin-top: 10px; background: #fff; padding: 8px; border-radius: var(--radius-md); border: 1px solid var(--concrete-200); }}
/* App header: ink bar with the ARL-89 mark, sister-site links, the progress
   toggle and sign-out — brand pairing is amber on ink. */
.app-header {{ display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; background: var(--ink-900); border-radius: var(--radius-lg); padding: 10px 16px; margin: 0 0 14px; border-bottom: 3px solid var(--amber-500); }}
.app-header .brand {{ display: flex; align-items: center; gap: 10px; }}
.app-header .brand svg {{ display: block; border-radius: 4px; }}
.app-header .brand-name {{ font-family: var(--font-display); color: var(--concrete-0); font-size: 1.15rem; letter-spacing: -0.015em; }}
.app-header .brand-name b {{ color: var(--amber-400); }}
.header-actions {{ display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }}
.header-actions a {{ color: var(--concrete-200); text-decoration: none; font-size: .9rem; padding: 6px 10px; border-radius: var(--radius-sm); }}
.header-actions a:hover {{ color: var(--amber-300); background: var(--ink-700); }}
.header-actions button {{ margin: 0; background: var(--ink-700); color: var(--concrete-100); border-color: var(--ink-600); }}
.header-actions button:hover {{ background: var(--ink-600); }}
.header-actions button.signout {{ background: var(--amber-500); color: var(--ink-900); border-color: var(--amber-600); font-weight: 700; }}
.header-actions button.signout:hover {{ background: var(--amber-600); }}
/* Calendar controls: selector cluster left, Prev|Today|Next as one segmented
   group center, Show on the right. */
.cal-controls-row {{ justify-content: space-between; }}
.cal-cluster {{ display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }}
.cal-nav {{ display: inline-flex; }}
.cal-nav button {{ margin: 0; border-radius: 0; border-right-width: 0; }}
.cal-nav button:first-child {{ border-radius: var(--radius-md) 0 0 var(--radius-md); }}
.cal-nav button:last-child {{ border-radius: 0 var(--radius-md) var(--radius-md) 0; border-right-width: 1px; }}
.cal-nav button.today {{ background: var(--amber-500); border: 1px solid var(--amber-600); color: var(--ink-900); font-weight: 700; }}
.cal-nav button.today:hover {{ background: var(--amber-600); }}
/* Text-summary pane: counts table instead of the old ASCII grid. */
.cal-summary-wrap {{ background: var(--concrete-0); border: 1px solid var(--concrete-200); border-radius: var(--radius-lg); padding: 10px 12px; margin: 8px 0; }}
.cal-summary-table {{ border-collapse: collapse; width: 100%; }}
.cal-summary-table th {{ width: auto; text-align: center; background: var(--concrete-50); border: 1px solid var(--concrete-200); padding: 7px 6px; }}
.cal-summary-table td {{ border: 1px solid var(--concrete-100); padding: 8px 10px; vertical-align: top; }}
.cal-summary-table tbody tr:hover td, .cal-summary-table td:hover {{ background: var(--amber-50); cursor: pointer; }}
.cal-summary-table td.blank {{ background: var(--concrete-50); cursor: default; }}
.cal-summary-table td.blank:hover {{ background: var(--concrete-50); }}
.cal-summary-table td.today-cell {{ box-shadow: inset 0 0 0 2px var(--amber-500); }}
.cal-summary-month td {{ text-align: center; width: 14.28%; }}
.cal-summary-month td b {{ display: block; color: var(--ink-900); margin-bottom: 3px; }}
.cal-summary-table .cnt {{ display: inline-block; background: var(--blue-tint); color: var(--blueprint-600); border-radius: var(--radius-pill); padding: 1px 9px; font-family: var(--font-mono); font-weight: 600; font-size: .8rem; }}
.cal-summary-table .cnt.zero {{ background: transparent; color: var(--concrete-300); }}
.cal-summary-table .num-cell {{ font-family: var(--font-mono); font-weight: 600; text-align: center; width: 130px; }}
.cal-summary-table .trend-cell {{ width: 40%; }}
/* Opportunities-list table: only dates/links are clickable, so plain rows
   don't get the amber click affordance. */
.cal-list-table tbody tr:hover td {{ background: var(--concrete-50); cursor: default; }}
.cal-list-table td.date-cell {{ font-family: var(--font-mono); font-weight: 600; white-space: nowrap; cursor: pointer; }}
.cal-list-table tbody tr:hover td.date-cell {{ background: var(--amber-50); }}
.cal-list-table td.mono-cell {{ font-family: var(--font-mono); font-size: .85rem; white-space: nowrap; }}
.cal-list-table td.desc-cell {{ max-width: 480px; }}
.cal-list-table tr.list-expired td {{ color: var(--red-600); }}
.cal-list-table tr.list-soon td {{ color: var(--amber-800); }}
.cal-list-table .num-cell {{ width: 70px; }}
/* Client list rows */
.client-table td {{ cursor: default; }}
.client-table tbody tr:hover td {{ background: var(--concrete-50); }}
.client-table .client-actions {{ white-space: nowrap; }}
.client-table .client-actions button {{ margin: 0 4px 0 0; padding: 4px 9px; font-size: .78rem; }}
.client-table tr.client-off td {{ color: var(--concrete-400); }}
.cal-subtabs {{ display: flex; gap: 0; margin: 10px 0 0; border-bottom: 2px solid var(--ink-900); }}
.cal-subtabs button {{ margin: 0; border-radius: var(--radius-sm) var(--radius-sm) 0 0; border: 1px solid var(--concrete-300); border-bottom: 0; background: var(--concrete-100); color: var(--concrete-600); padding: 7px 14px; }}
.cal-subtabs button.active {{ background: var(--ink-900); color: var(--amber-400); border-color: var(--ink-900); }}
/* Phone layout: single-column grids, edge-to-edge cards, stacked key/value
   tables, and horizontally scrollable tab bar so nothing overflows the screen. */
@media (max-width: 640px) {{
  body {{ margin: 10px; }}
  .app-header {{ padding: 8px 10px; }}
  .app-header .brand-name {{ font-size: 1rem; }}
  h1 {{ font-size: 1.5rem; }}
  .card {{ padding: 12px; border-radius: var(--radius-lg); }}
  .tab-nav {{ flex-wrap: nowrap; overflow-x: auto; -webkit-overflow-scrolling: touch; }}
  .tab-nav button {{ flex: 0 0 auto; }}
  button {{ padding: 10px 14px; }}
  th {{ width: auto; }}
  /* Stack key/value rows so the 220px label column doesn't squeeze values. */
  #diagnostics table, #diagnostics tbody, #diagnostics tr, #diagnostics th, #diagnostics td {{ display: block; width: auto; }}
  #diagnostics th {{ border-bottom: 0; padding-bottom: 0; }}
  #diagnostics td {{ padding-top: 2px; }}
  .settings-grid, .destination-grid, .record-grid {{ grid-template-columns: 1fr; }}
  select#record-index {{ min-width: 100%; min-height: 10rem; }}
  .bar-row {{ grid-template-columns: 1fr; gap: 2px; }}
  .calcell, .week-number {{ min-height: 84px; }}
  .calgrid {{ grid-template-columns: 34px repeat(7, minmax(0, 1fr)); }}
  .calevent {{ font-size: .72rem; }}
  .timeline {{ grid-template-columns: 44px 1fr; }}
  .week-timeline {{ grid-template-columns: 40px repeat(7, minmax(0, 1fr)); }}
  pre {{ font-size: .8rem; }}
}}
</style>
</head>
<body>
<header class="app-header">
  <div class="brand">
    <svg width="34" height="34" viewBox="0 0 128 128" fill="none" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="ARL-89 mark"><rect width="128" height="128" rx="8" fill="#F5A300"></rect><path d="M0 0H44L0 44V0Z" fill="#121417"></path><path d="M0 52V32L32 0H52L0 52Z" fill="#F5A300"></path><path d="M0 32V20L20 0H32L0 32Z" fill="#121417"></path><text x="70" y="90" font-family="Archivo, 'Arial Black', system-ui, sans-serif" font-size="60" font-weight="800" fill="#121417" text-anchor="middle" letter-spacing="-2">89</text></svg>
    <span class="brand-name">PanamaCompra <b>MONITOR</b></span>
  </div>
  <nav class="header-actions">
    <a id="link-home-site" href="#" title="ARL-89 home site (WordPress)">ARL-89 Home</a>
    <a id="link-tools-site" href="#" title="Tools portal (Homepage dashboard)">Tools</a>
    <button id="progress-toggle" onclick="toggleProgressCard()" title="Hide or show the progress monitor card">Hide progress</button>
    <button class="signout" onclick="adminSignOut()" title="End the admin session and go to the ARL-89 home site">Sign out</button>
  </nav>
</header>
<div class="card" id="progress-card">
  <h2>Progress monitor</h2>
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
<div class="tab-nav"><button class="active" data-tab-button="overview" onclick="showTab('overview')">Overview</button><button data-tab-button="calendar" onclick="showTab('calendar')">Calendar</button><button data-tab-button="decision" onclick="showTab('decision')">KPIs</button><button data-tab-button="records" onclick="showTab('records')">Opportunities</button><button data-tab-button="operations" onclick="showTab('operations')">Operations</button><button data-tab-button="whatsapp" onclick="showTab('whatsapp')">WhatsApp</button><button data-tab-button="scheduler" onclick="showTab('scheduler')">Scheduler</button><button data-tab-button="integrations" onclick="showTab('integrations')">Integrations</button><button data-tab-button="settings" onclick="showTab('settings')">Settings</button></div>
<div class="card" data-tab="records"><h2>Record selector and filters</h2><p class="small">Collected records as “[downloaded timestamp | DTEND status] NUMERO — description”; choose newest-first or oldest-first ordering. Use filters first, then Ctrl/Shift-select one or more records to notify or import calendars.</p><p><label class="small">Deadline <select id="record-status"><option value="all">All</option><option value="soon">Next to expire</option><option value="expired">Expired</option><option value="upcoming">Upcoming</option><option value="unknown">No date / needs repair</option></select></label> <label class="small">Detail status <select id="record-detail-status"><option value="all">All</option><option value="pending">Pending records</option><option value="saved">Completed records</option><option value="failed">Failed records</option></select></label> <label class="small">Order by <select id="record-order-field"><option value="downloaded">Downloaded date</option><option value="end">End date</option><option value="start">Start date</option></select></label> <label class="small"><select id="record-order"><option value="newest">Newest first</option><option value="oldest">Oldest first</option></select></label> <label class="small">DTEND on/after <input type="text" id="record-mindate" placeholder="YYYY-MM-DD [HH:MM]" size="16"></label> <label class="small">on/before <input type="text" id="record-maxdate" placeholder="YYYY-MM-DD [HH:MM]" size="16"></label> <label class="small">DTSTART on/after <input type="text" id="record-start-mindate" placeholder="YYYY-MM-DD [HH:MM]" size="16"></label> <label class="small">on/before <input type="text" id="record-start-maxdate" placeholder="YYYY-MM-DD [HH:MM]" size="16"></label> <label class="small">Downloaded on/after <input type="text" id="record-downloaded-mindate" placeholder="YYYY-MM-DD [HH:MM]" size="16"></label> <label class="small">on/before <input type="text" id="record-downloaded-maxdate" placeholder="YYYY-MM-DD [HH:MM]" size="16"></label> <span class="small">Legend: <span style="color:#247A47;font-weight:700">upcoming</span> · <span style="color:#B06E00;font-weight:700">next to expire</span> · <span style="color:#AE2D1C;font-weight:700">expired</span></span></p><p><select id="record-index" multiple size="10"></select> <button onclick="refreshRecordIndex()">Refresh list</button> <button onclick="openRecordFolder()">Open record folder</button> <button onclick="openRecordPortal()">Open in portal</button> <button onclick="notifySelectedRecords()">Notify selected WhatsApp</button> <button onclick="importSelectedCalendars()">Import selected calendars</button> <button onclick="templatesSelectedRecords()">Copy templates to selected</button></p><p id="record-detail" class="small">Loading record index…</p></div>
<div class="card" data-tab="records"><h2>Records Pendings</h2><div id="records-pending" class="record-card record-pending">Records Pendings: —</div><p class="small">Use Record selector and filters → Detail status = Pending records for full selectors/open actions.</p></div>
<div class="card" data-tab="records"><h2>Records Completed</h2><div id="records-completed" class="record-card record-completed">Records Completed: —</div><p class="small">Use Record selector and filters → Detail status = Completed records for full selectors/open actions.</p></div>
<div class="card" data-tab="records"><h2>Database summary</h2><p class="small">Read-only archive database summary with counters, status breakdown, recent records and DB elements/columns.</p><pre id="records-db-summary">Database summary loading…</pre><p><button onclick="refreshDbReview('records-db-summary')">Refresh DB summary</button></p></div>
<div class="card" data-tab="records"><h2>Database review</h2><p class="small">Same database details in a collapsible review panel. Refresh after a run or a reset.</p><pre id="db-review">Loading database snapshot…</pre><p><button onclick="refreshDbReview()">Refresh DB snapshot</button></p></div>
<div class="card" data-tab="overview"><h2>System health <span class="kpi-live" id="overview-live-stamp">LIVE</span></h2><p class="small">Snapshot of the last completed run, current intake and service reachability. Full analysis lives in the KPIs tab; run controls in Operations.</p><div id="overview-kpis" class="kpi-grid">Loading overview…</div></div>
<div class="card" data-tab="overview"><h2>Last run stages</h2><p class="small" id="overview-last-run">No completed run recorded yet.</p><div id="overview-stages" class="chart"></div></div>
<div class="card" data-tab="overview"><h2>Services</h2><div id="overview-services" class="small">Loading services…</div><p class="small">Webhook access details and the changedetection script live in the Integrations tab.</p></div>
<div class="card" data-tab="operations"><h2>Queue process</h2><p id="queue-summary" class="small">Loading queue…</p><pre id="queue-log"></pre></div>
<div class="card" data-tab="operations"><h2>Monitor buttons</h2><div class="subsection"><h3>Run controls</h3><div class="control-row"><span class="control-label">Mode</span><span class="mode-group" id="run-mode"><label><input type="radio" name="run-mode" value="auto" disabled><span>automatic</span></label><label><input type="radio" name="run-mode" value="restart" checked><span>run pending only</span></label><label><input type="radio" name="run-mode" value="manual"><span>manual run</span></label><label><input type="radio" name="run-mode" value="test"><span>test run</span></label></span></div><div class="control-row"><span class="control-label">Run</span><label class="small">Index page cap <input id="index-limit" value="0" size="4"></label> <label class="small">Detail limit <input id="detail-limit" value="0" size="4"></label><span class="control-sep"></span><button id="run-button" class="primary" onclick="requestRun()">Request selected run</button><button class="danger" onclick="stopRun()">Stop active run</button></div><div class="control-row"><span class="control-label">System</span><button class="primary" onclick="startAll()" title="Brings Docker integrations and the webhook listener back up, and opens the monitor">▶ Start All</button><button class="danger" onclick="stopAll()" title="DANGER: stops collectors and infrastructure; monitors stay open">⛔ Stop All</button><span class="control-sep"></span><button onclick="devPause()" title="Stops any active run and pauses webhook/cron auto-triggers plus the updater's autostash, so editing this repo is safe">⏸ Dev Pause</button><button onclick="devResume()" title="Restores everything Dev Pause changed">▶ Dev Resume</button></div><p id="button-status" class="small" style="min-height:1.2em"></p><p class="small">Integrations: <a href="{CHANGEDETECTION_URL}" target="_blank">Open changedetection UI</a> · <a href="{WAHA_DASHBOARD_URL}" target="_blank">Open WAHA dashboard (pair by QR)</a> · container data lives in var/integrations; manage the stack from the Integrations buttons below. Container settings apply on the next stack restart.</p><p class="small" id="run-hint"><strong>Mode:</strong> automatic is shown for changedetection/webhook runs only; run pending only queues the normal collector; manual run starts the worker now; test run uses the isolated test zone. Index page cap is optional: 0 means crawl all pages until the portal has no Next page; detail limit controls detail/test records (0 = unlimited: download until no pending entries remain).</p></div><div class="subsection"><h3>Action buttons</h3><div id="action-zones"></div></div></div>
<div class="card" data-tab="operations"><h2>Diagnostics</h2><table id="diagnostics"></table></div>
<div class="card" data-tab="operations"><h2>Recent worker log</h2><pre id="worker-log" class="log-pane"></pre></div>
<div class="card" data-tab="operations"><h2>Current action log</h2><pre id="current-log" class="log-pane"></pre></div>
<div class="card" data-tab="decision"><h2>KPI Dashboard <span class="kpi-live" id="kpi-live-stamp">LIVE</span></h2><p class="small">All KPIs in one tab: index scan intake, detail download throughput, WAHA delivery, deadline repair, plus diagrams about the collected items, contracting entities and locations so the numbers point at a decision. Use the filters to slice every card and diagram to a time window, a group or an entity.</p><div class="kpi-filter-bar"><label class="small">Window <select id="kpi-days" onchange="refreshDecisionDashboard()"><option value="0" selected>All time</option><option value="7">Last 7 days</option><option value="30">Last 30 days</option><option value="90">Last 90 days</option><option value="365">Last year</option></select></label> <label class="small">Group <input id="kpi-grupo" list="kpi-grupo-list" size="14" placeholder="all groups"></label><datalist id="kpi-grupo-list"></datalist> <label class="small">Entity <input id="kpi-entidad" list="kpi-entidad-list" size="26" placeholder="all entities"></label><datalist id="kpi-entidad-list"></datalist> <button class="primary" onclick="refreshDecisionDashboard()">Apply filters</button> <button onclick="resetKpiFilters()">Reset</button> <button onclick="window.location = '/api/kpi-export?' + kpiFilterParams()">Export CSV</button> <span id="kpi-filter-state" class="small"></span></div><div id="decision-kpis" class="kpi-grid"></div><div class="diagram-grid"><div class="chart"><h3>Detail status mix</h3><div id="decision-status"></div></div><div class="chart"><h3>Index groups</h3><div id="decision-groups"></div></div><div class="chart"><h3>Daily intake (last 14 days)</h3><div id="decision-daily"></div></div><div class="chart"><h3>Monthly intake trend</h3><div id="decision-trend"></div></div><div class="chart"><h3>Top contracting entities</h3><div id="decision-entities"></div></div><div class="chart"><h3>Locations / buying units (from details)</h3><div id="decision-locations"></div></div><div class="chart"><h3>Most frequent items</h3><div id="decision-top-items"></div></div><div class="chart"><h3>Latest parsed items</h3><div id="decision-latest-items"></div></div><div class="chart"><h3>Detail queue pressure</h3><div id="decision-deadlines"></div></div><div class="chart"><h3>Items analysis</h3><div id="decision-items"></div></div><div class="chart"><h3>Item keywords</h3><div id="decision-item-keywords" class="keyword-cloud"></div></div></div><pre id="decision-recommendations">Loading decision signals…</pre><p><button onclick="refreshDecisionDashboard()">Refresh KPIs</button></p></div>
<div class="card" data-tab="calendar"><h2>Opportunity calendar</h2><p class="small">Collected opportunities by day, week, month or year. Click a month to open it, a day to zoom to its week, a week-day header to zoom to that day.</p><div class="cal-controls-row"><div class="cal-cluster"><label class="small">View <select id="cal-view" onchange="loadCalendar()"><option value="day">Day</option><option value="week">Week</option><option value="month" selected>Month</option><option value="year">Year</option></select></label> <label class="small">Date field <select id="cal-field" onchange="loadCalendar()"><option value="end" selected>Deadline (end)</option><option value="start">Start</option><option value="downloaded">Downloaded</option></select></label> <label class="small">Anchor <input id="cal-date" size="10" placeholder="YYYY-MM-DD"></label></div><div class="cal-nav"><button onclick="loadCalendar(-1)" title="Previous period">◀ Prev</button><button class="today" onclick="loadCalendar(0)" title="Jump to today">Today</button><button onclick="loadCalendar(1)" title="Next period">Next ▶</button></div><button class="primary" style="margin:0" onclick="loadCalendar()">Show</button></div><p class="cal-filter-row"><label class="small"><b>Keyword filter</b> <input id="cal-filter" size="48" placeholder="e.g. construccion, salud — partial match, accents ignored" onchange="loadCalendar()"></label> <button onclick="loadCalendar()">Apply</button> <button onclick="document.getElementById('cal-filter').value=''; loadCalendar()">Clear</button> <span class="xs">Filters numero, descripcion, entidad, dependencia, modalidad and grupo.</span></p><div class="cal-subtabs"><button type="button" class="active" data-calpane="visual" onclick="showCalPane('visual')">Visual calendar</button><button type="button" data-calpane="text" onclick="showCalPane('text')">Text summary</button><button type="button" data-calpane="list" onclick="showCalPane('list')">Opportunities list</button></div><div id="calpane-visual"><div id="calendar-visual" class="chart" style="min-height:120px;margin:8px 0">Calendar visual loading…</div></div><div id="calpane-text" hidden><div id="calendar-text" class="cal-summary-wrap" style="max-height: 520px; overflow: auto">Loading calendar…</div></div><div id="calpane-list" hidden><div id="calendar-list" class="cal-summary-wrap" style="max-height: 560px; overflow: auto">Loading…</div></div></div>
<div class="card" data-tab="scheduler"><h2>changedetection schedule <span class="small">(read-only)</span></h2><p class="small">What changedetection itself has active and scheduled right now — this panel only reads changedetection's API/datastore, it never changes anything there. Control which trigger actually starts a run below (webhook vs cron) and the "Automatic runs from changedetection" toggle in Settings.</p><div id="cd-schedule-banner" class="small"></div><div id="cd-schedule-summary" class="small">Loading changedetection schedule…</div><table id="cd-schedule-table" class="small" style="width:100%;border-collapse:collapse"></table><p><button onclick="refreshChangedetectionSchedule()">Refresh changedetection schedule</button></p></div>
<div class="card" data-tab="scheduler"><h2>Automatic scheduler (cron)</h2><p class="small">Runs the collector on a repeating schedule instead of the changedetection webhook trigger. Enabling this sets Auto-run source to cron and installs a crontab entry (via <code>src/50_tools/160-manage-cron-schedule.py</code>, no manual <code>crontab -e</code> needed); disabling it removes that entry and switches Auto-run source back to changedetection.</p><p><label class="small"><input type="checkbox" id="cron-enabled"> Enable scheduled automatic runs</label></p><p class="xs">Days <label><input type="radio" name="cron-days" value="daily" checked> Daily</label> <label><input type="radio" name="cron-days" value="weekdays"> Weekdays (Mon-Fri)</label> <label><input type="radio" name="cron-days" value="weekends"> Weekends (Sat-Sun)</label> <label><input type="radio" name="cron-days" value="custom"> Custom</label></p><p><label class="small">Custom days (0=Sun..6=Sat) <input id="cron-custom-days" size="20" placeholder="e.g. 1,3,5"></label></p><p><label class="small">Start time (HH:MM) <input id="cron-start" size="8" value="08:00"></label> <label class="small">End time (HH:MM) <input id="cron-end" size="8" value="18:00"></label> <label class="small">Repeat every (minutes) <input id="cron-interval" size="6" value="30"></label></p><p><button class="primary" onclick="applyCronSchedule()">Save &amp; Apply schedule</button> <button onclick="refreshCronScheduleStatus()">Refresh status</button></p><p class="small" id="cron-schedule-status"></p></div>
<div class="card" data-tab="integrations"><h2>changedetection Browser Steps JS</h2><p class="small">Paste this into <strong>ChangeDetection → Watch → Browser Steps → Execute JS</strong>. Keep CSS filter <code>#pc-monitor-output</code>, and leave Visual Filter, Remove elements and Triggers empty/disabled. It crawls all Programadas pages first, then all Abiertas pages.</p><p><button onclick="loadChangedetectionScript()">Load script</button> <button onclick="copyChangedetectionScript()">Copy script</button> <span id="cd-script-state" class="small"></span></p><textarea id="changedetection-script" rows="16" style="width:100%; box-sizing:border-box" placeholder="Press Load script"></textarea></div>
<div class="card" data-tab="whatsapp"><h2>WhatsApp settings</h2><p class="small">All WhatsApp options in one place: destinations, delivery settings, WAHA server connection, toggles and per-destination content filters.</p><div class="subsection"><h3>Destinations & toggles</h3><div class="destination-grid"><label>Default / one group</label><textarea id="waha-message-wa" rows="2" placeholder="12036...@g.us (used when a purpose-specific group is blank)"></textarea><label>Index alerts</label><input id="waha-index-wa" size="32" placeholder="blank = default group"><label>Item details</label><input id="waha-details-wa" size="32" placeholder="blank = default group"><label>Status changes</label><input id="waha-status-wa" size="32" placeholder="blank = default group"><label>Open Now Opportunities</label><input id="waha-open-now-wa" size="32" placeholder="blank = Index alerts / default group"><label>System health</label><input id="waha-system-wa" size="32" placeholder="blank = default group"><label>Final summary per round</label><input id="waha-summary-wa" size="32" placeholder="blank = default group"></div><p><label class="small"><input type="checkbox" id="notify-whatsapp-wa" onchange="syncWhatsappMirror('wa'); saveMonitorSetting('PC_NOTIFY_WHATSAPP', this.checked ? '1' : '0')"> Notify by WhatsApp (index alerts)</label><br><label class="small"><input type="checkbox" id="notify-details-wa" onchange="syncWhatsappMirror('wa'); saveMonitorSetting('PC_NOTIFY_DETAILS', this.checked ? '1' : '0')"> Detail follow-up WhatsApp</label></p><p><button onclick="saveWahaFrom('wa')">Save WhatsApp destinations</button> <button onclick="sendTestWhatsapp()">Send test WhatsApp</button></p></div><div class="subsection"><h3>Delivery & server settings</h3><div class="settings-grid"><label class="small">WhatsApp source <input id="set-PC_WAHA_SOURCE" size="16"></label> <label class="small">WhatsApp within N days <input id="set-PC_NOTIFY_WITHIN_DAYS" size="5" placeholder="all"></label> <label class="small">WAHA retries <input id="set-PC_WAHA_RETRIES" size="5"></label> <label class="small">Delay between sends (s) <input id="set-PC_WAHA_SEND_DELAY_SECONDS" size="5"></label> <label class="small">Digest above N new records <input id="set-PC_NOTIFY_INDEX_DIGEST_THRESHOLD" size="5"></label> <label class="small">Idle status every N hours <input id="set-PC_NOTIFY_IDLE_EVERY_HOURS" size="5"></label> <label class="small">WAHA base URL <input id="set-PC_WAHA_BASE_URL" size="24"></label> <label class="small">WAHA session <input id="set-PC_WAHA_SESSION" size="12"></label> <label class="small">WAHA events <input id="set-PC_WAHA_NOTIFY_EVENTS" size="40"></label> <label class="small">WAHA server port <input id="set-WAHA_PORT" size="6"></label> <label class="small">WAHA server API key <input id="set-WAHA_API_KEY" size="20"></label> <label class="small">WAHA dashboard user <input id="set-WAHA_DASHBOARD_USERNAME" size="12"></label> <label class="small">WAHA dashboard password (generated by setup) <input id="set-WAHA_DASHBOARD_PASSWORD" size="14"></label></div><p><button onclick="saveAdvancedSettings()">Save WhatsApp advanced settings</button></p><p class="xs">The WAHA dashboard login is user admin with a RANDOM password generated by setup — see data/config/integration-access.txt. Change it here whenever you like — it applies on the next docker stack restart.</p><p><label class="small"><input type="checkbox" id="set-PC_WAHA_ENABLED" onchange="saveMonitorSetting('PC_WAHA_ENABLED', this.checked ? '1' : '0')"> Enable WAHA WhatsApp sending</label> <label class="small"><input type="checkbox" id="set-PC_NOTIFY_SKIP_EXPIRED" onchange="saveMonitorSetting('PC_NOTIFY_SKIP_EXPIRED', this.checked ? '1' : '0')"> Skip already-expired opportunities</label> <label class="small"><input type="checkbox" id="set-PC_NOTIFY_DETAILS_INLINE" onchange="saveMonitorSetting('PC_NOTIFY_DETAILS_INLINE', this.checked ? '1' : '0')"> Send each detail message right after its download</label> <label class="small"><input type="checkbox" id="set-PC_INDEX_FROM_SNAPSHOT" onchange="saveMonitorSetting('PC_INDEX_FROM_SNAPSHOT', this.checked ? '1' : '0')"> AUTO runs import index from changedetection snapshot</label></p></div><div class="subsection"><h3>Content filters</h3><p><label class="small">Shared <input id="flt-global" size="30"></label> <label class="small">Index alerts <input id="flt-index" size="30"></label> <label class="small">Item details <input id="flt-details" size="30"></label> <label class="small">Status changes <input id="flt-status" size="30"></label> <label class="small">Open Now Opportunities <input id="flt-open-now" size="30"></label> <button onclick="saveWahaFilters()">Save filters</button></p></div></div>
<div class="card" data-tab="whatsapp"><h2>WhatsApp client profiles</h2><div class="subsection"><h3>Add / update a client</h3><p class="small">Pick any destination returned by WAHA or type a custom chat ID; the filter accepts custom expressions (OR with commas, AND with '+', NOT with '-').</p><p><label class="small">Client name <input id="client-name" size="18"></label> <label class="small">Destination <select id="client-group-select"><option value="">— search first —</option></select></label> <label class="small">or custom chat ID <input id="client-chat-custom" size="22" placeholder="12036...@g.us"></label></p><p><span class="small">Purposes</span> <label class="small"><input type="checkbox" id="client-purpose-index" checked> index</label> <label class="small"><input type="checkbox" id="client-purpose-details" checked> details</label> <label class="small"><input type="checkbox" id="client-purpose-status" checked> status</label> <label class="small">Filter expression <input id="client-filters" size="30" placeholder="salud + insumos, -construccion"></label> <button onclick="addClientProfile()">Add to profiles</button></p></div><div class="subsection"><h3>Clients</h3><p class="small">Every saved client at a glance. Edit loads the client into the form above (press "Add to profiles" to save the changes); Disable pauses deliveries and calendar access without deleting.</p><div id="client-profile-list" class="small">Loading client profiles…</div></div><div class="subsection"><h3>Profiles (JSON)</h3><p class="small">Full list, editable by hand. Purposes: index, details, status, or all.</p><textarea id="waha-clients" rows="10" placeholder='[{{"name":"Client A","chat_id":"12036...@g.us","purposes":["index","details"],"filters":"salud + insumos, -construccion","enabled":true}}]'></textarea><p><button onclick="saveWahaClients()">Save client profiles</button></p></div></div><div class="card" data-tab="whatsapp"><h2>WAHA Directory Search</h2><p class="small">Search the complete WAHA directory by one or more words from a name or chat ID. Results filter live from a short-lived local cache, so typing does not repeatedly download contacts, groups, communities and channels.</p><p><label class="small">Name or ID <input id="waha-search-q" size="40" placeholder="e.g. Chiriquí contratistas, 12036, @g.us" oninput="scheduleWahaSearch()" onkeydown="if (event.key === 'Enter') {{ event.preventDefault(); wahaSearch(); }}"></label> <button onclick="wahaSearch()">Search</button> <button onclick="wahaSearch(true)">Refresh directory</button> <span id="waha-search-state" class="small"></span></p><div id="waha-search-results" class="small"></div></div>
<div class="card" data-tab="whatsapp"><h2>WhatsApp message formats</h2><p class="small">Customize the text of each message family, including system health / worker messages with {{{{placeholder}}}} fields (unknown placeholders stay literal). <label class="small">Format <select id="fmt-kind" onchange="loadWahaFormat()"><option value="index" selected>Index alert</option><option value="details">Detail follow-up</option><option value="status">Status change</option><option value="system">System / health</option><option value="summary">Final summary</option></select></label> <button onclick="previewWahaFormat()">Preview</button> <button onclick="saveWahaFormat()">Save format</button> <button onclick="resetWahaFormat()">Reset to default</button> <span id="fmt-state" class="small"></span></p><textarea id="fmt-template" rows="8" style="width:100%; box-sizing:border-box"></textarea><p class="small" id="fmt-placeholders"></p><pre id="fmt-preview" style="max-height: 300px"></pre></div>
<div class="card" data-tab="settings"><h2>Settings</h2><details class="adv-settings" open><summary class="small">Collector, timer &amp; storage settings (apply on the next run/launch)</summary><h3>Storage paths</h3><div class="settings-grid"><label class="small">Records folder <input id="records-dir" size="42"></label> <label class="small">Calendar packages <input id="calendar-dir" size="42"></label> <label class="small">Test sandbox <input id="records-test-dir" size="42"></label> <button onclick="savePathSettings()">Save paths</button></div><h3>Run cadence</h3><div class="settings-grid"><label class="small">Auto-run source <select id="set-PC_AUTORUN_SOURCE"><option value="changedetection">changedetection webhook</option><option value="cron">manual cron</option></select></label><label class="small">Next-run interval (min) <input id="set-PC_NEXT_RUN_INTERVAL_MINUTES" size="5"></label> <label class="small">Cron index page cap <input id="set-PC_CRON_INDEX_LIMIT" size="5"></label> <label class="small">Cron detail limit (0 = all) <input id="set-PC_CRON_DETAIL_LIMIT" size="5"></label> <label class="small">Webhook index page cap <input id="set-PC_WEBHOOK_INDEX_LIMIT" size="5"></label> <label class="small">Webhook detail limit (0 = all) <input id="set-PC_WEBHOOK_DETAIL_LIMIT" size="5"></label> <label class="small">Test-zone records <input id="set-PC_TEST_ZONE_LIMIT" size="5"></label> <label class="small">Monitor stale sec <input id="set-PC_MONITOR_STALE_SECONDS" size="5"></label> <label class="small">Deadline 'soon' days <input id="set-PC_MONITOR_DEADLINE_SOON_DAYS" size="5"></label></div><p class="small"><label><input type="checkbox" id="source-changedetection-active" disabled> changedetection/webhook active</label> <label><input type="checkbox" id="source-cron-active" disabled> cron active</label> <span id="autorun-source-note"></span></p><h3>Timer window</h3><div class="settings-grid"><label class="small">Timer width <input id="set-PC_NEXT_RUN_TIMER_WIDTH" size="5"></label> <label class="small">Timer height <input id="set-PC_NEXT_RUN_TIMER_HEIGHT" size="5"></label> <label class="small">Timer top <input id="set-PC_NEXT_RUN_TIMER_TOP" size="5"></label> <label class="small">Timer latest records <input id="set-PC_NEXT_RUN_TIMER_RECORDS" size="5"></label> <label class="small">Timer data refresh sec <input id="set-PC_NEXT_RUN_TIMER_DATA_REFRESH_SECONDS" size="5"></label></div><h3>Integrations</h3><div class="settings-grid"><label class="small">Monitor bind host <input id="set-PC_MONITOR_HOST" size="16" placeholder="127.0.0.1 or 0.0.0.0"></label><label class="small">changedetection URL <input id="set-CHANGEDETECTION_BASE_URL" size="24"></label> <label class="small">Webhook listener port <input id="set-PC_WEBHOOK_PORT" size="6"></label> <label class="small">Webhook public host <input id="set-PC_WEBHOOK_PUBLIC_HOST" size="22"></label><button onclick="saveAdvancedSettings()">Save settings</button></div><h3>Access &amp; sign-in</h3><p class="xs">The front page (/) is a login for everyone except this PC itself (127.0.0.1 always gets straight in). <b>Manager</b>: local username/password below — grants this admin monitor. <b>Clients</b>: Firebase email/Google sign-in — they land on the calendar-only dashboard at <a href="/client-calendar" target="_blank" rel="noopener">/client-calendar</a>. An email listed in "Admin emails" also gets the admin monitor when signing in via Firebase. The API token lets the Android admin app call the protected APIs (X-PC-Admin-Token header or ?admin_token=).</p><div class="settings-grid"><label class="small">Manager username <input id="set-PC_ADMIN_USERNAME" size="18" autocomplete="off"></label> <label class="small">Manager password <input id="set-PC_ADMIN_PASSWORD" size="18" type="password" autocomplete="new-password"></label> <label class="small">Admin emails (comma separated) <input id="set-PC_ADMIN_EMAILS" size="34" placeholder="you@gmail.com, other@x.com"></label> <label class="small">Admin API token (Android admin app) <input id="set-PC_ADMIN_API_TOKEN" size="26" placeholder="blank = off"></label></div><h4 class="small" style="margin:10px 0 4px">Client sign-in (Firebase web app)</h4><p class="xs">Same Firebase project as the Android app: in Firebase console → Project settings → Your apps, add a <b>Web</b> app and copy its config here; also add this monitor's host to Authentication → Settings → Authorized domains for Google sign-in. Blank = client sign-in disabled (manager login and ?uid= links keep working).</p><div class="settings-grid"><label class="small">API key <input id="set-PC_FIREBASE_WEB_API_KEY" size="34"></label> <label class="small">Auth domain <input id="set-PC_FIREBASE_WEB_AUTH_DOMAIN" size="28" placeholder="your-project.firebaseapp.com"></label> <label class="small">Project ID <input id="set-PC_FIREBASE_WEB_PROJECT_ID" size="20"></label> <label class="small">App ID <input id="set-PC_FIREBASE_WEB_APP_ID" size="34" placeholder="1:1234:web:abcd"></label> <button onclick="saveAdvancedSettings()">Save settings</button></div><p class="xs">Auto-run source is exclusive: cron active disables webhook collection; changedetection active disables cron collection. Use <code>src/20_pipeline/115-cron-run.sh</code> from crontab.</p><p><label class="small"><input type="checkbox" id="set-PC_TEST_ZONE_AUTORUN" onchange="saveMonitorSetting('PC_TEST_ZONE_AUTORUN', this.checked ? '1' : '0')"> Auto-run test zone when no new records</label> <label class="small"><input type="checkbox" id="set-PC_RUN_UPDATE_BEFORE_RUN" onchange="saveMonitorSetting('PC_RUN_UPDATE_BEFORE_RUN', this.checked ? '1' : '0')"> Update local copy before each run</label> <label class="small" title="OFF = manual mode: the webhook listener keeps running but ignores incoming changedetection triggers instead of starting a run."><input type="checkbox" id="set-PC_WEBHOOK_AUTO_RUN" onchange="saveMonitorSetting('PC_WEBHOOK_AUTO_RUN', this.checked ? '1' : '0')"> Automatic runs from changedetection (webhook)</label></p></details></div>
<div class="card" data-tab="settings"><h2>Monitor users &amp; tab access</h2><p class="small">Users you add here sign in with the same front-page <b>Manager</b> form, but only see — and can only drive — the tabs you grant them. Full admin stays with the manager account and PC_ADMIN_EMAILS. Server-side, their sessions get read access plus the actions belonging to their tabs; secret settings values are never sent to them.</p><div class="subsection"><h3>Add / update a user</h3><p><label class="small">Username <input id="mu-username" size="14" autocomplete="off"></label> <label class="small">Password <input id="mu-password" size="14" type="password" autocomplete="new-password"></label></p><p><span class="small">Tabs:</span> <label class="small"><input type="checkbox" id="mu-tab-overview" checked> Overview</label> <label class="small"><input type="checkbox" id="mu-tab-calendar" checked> Calendar</label> <label class="small"><input type="checkbox" id="mu-tab-decision"> KPIs</label> <label class="small"><input type="checkbox" id="mu-tab-records"> Opportunities</label> <label class="small"><input type="checkbox" id="mu-tab-operations"> Operations</label> <label class="small"><input type="checkbox" id="mu-tab-whatsapp"> WhatsApp</label> <label class="small"><input type="checkbox" id="mu-tab-scheduler"> Scheduler</label> <label class="small"><input type="checkbox" id="mu-tab-integrations"> Integrations</label> <label class="small"><input type="checkbox" id="mu-tab-settings"> Settings</label> <button class="primary" onclick="addMonitorUser()">Add / update user</button></p></div><div class="subsection"><h3>Users (JSON)</h3><p class="small">Full list, editable by hand. Remove a line to delete the user; set "enabled": false to suspend without deleting.</p><textarea id="monitor-users" rows="6" placeholder='[{{"username":"maria","password":"secret","tabs":["overview","calendar"],"enabled":true}}]'></textarea><p><button onclick="saveMonitorUsers()">Save users</button> <button onclick="loadMonitorUsers()">Reload</button> <span id="mu-state" class="small"></span></p></div></div>
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
  if (clientsBox && document.activeElement !== clientsBox) {{ clientsBox.value = data.waha_clients || '[]'; renderClientProfileList(); }}
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
  ['PC_WAHA_SOURCE','PC_AUTORUN_SOURCE','PC_CRON_INDEX_LIMIT','PC_CRON_DETAIL_LIMIT','PC_MONITOR_HOST','PC_NEXT_RUN_INTERVAL_MINUTES','PC_MONITOR_DEADLINE_SOON_DAYS','PC_WEBHOOK_INDEX_LIMIT','PC_WEBHOOK_DETAIL_LIMIT','PC_NOTIFY_WITHIN_DAYS','PC_WAHA_RETRIES','PC_WAHA_SEND_DELAY_SECONDS','PC_NOTIFY_INDEX_DIGEST_THRESHOLD','PC_NOTIFY_IDLE_EVERY_HOURS','PC_WAHA_BASE_URL','PC_WAHA_SESSION','PC_WAHA_NOTIFY_EVENTS','PC_TEST_ZONE_LIMIT','PC_MONITOR_STALE_SECONDS','PC_NEXT_RUN_TIMER_WIDTH','PC_NEXT_RUN_TIMER_HEIGHT','PC_NEXT_RUN_TIMER_TOP','PC_NEXT_RUN_TIMER_RECORDS','PC_NEXT_RUN_TIMER_DATA_REFRESH_SECONDS','CHANGEDETECTION_BASE_URL','PC_WEBHOOK_PORT','PC_WEBHOOK_PUBLIC_HOST','WAHA_PORT','WAHA_API_KEY','WAHA_DASHBOARD_USERNAME','WAHA_DASHBOARD_PASSWORD','PC_FIREBASE_WEB_API_KEY','PC_FIREBASE_WEB_AUTH_DOMAIN','PC_FIREBASE_WEB_PROJECT_ID','PC_FIREBASE_WEB_APP_ID','PC_ADMIN_USERNAME','PC_ADMIN_PASSWORD','PC_ADMIN_EMAILS','PC_ADMIN_API_TOKEN','PC_TEMPLATES_SRC_DIR'].forEach(key => {{ const el = document.getElementById('set-' + key); if (el && document.activeElement !== el && settings[key] !== undefined) el.value = settings[key]; }});
  monitorSettingsLoaded = true;
  updateAutorunSourceIndicators(settings);
  moveWebhookAutoRunControlToScheduler();
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
  if (!window.confirm('This stops workers, test zone, calendar builder, webhook listener and updaters. Monitors stay open so you can resume from here. Continue?')) return;
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
    if (data.dev_mode_active) bits.push('<span style="color:#AE2D1C;font-weight:700">⏸ Dev mode is ON — all automatic triggers are paused.</span>');
    if (!data.webhook_auto_run) bits.push('<span style="color:#AE2D1C;font-weight:700">⚠ Automatic runs from changedetection (webhook) is OFF — new changedetection changes will NOT start a run or send WhatsApp messages.</span>');
    else bits.push('<span style="color:#247A47">✓ Automatic runs from changedetection (webhook) is ON.</span>');
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
  // Merge over the existing entry so self-service fields the form doesn't
  // carry (firebase_uid, email, phone, app_code…) survive an edit.
  if (existing >= 0) list[existing] = Object.assign({{}}, list[existing], profile); else list.push(profile);
  box.value = JSON.stringify(list, null, 2);
  saveWahaClients();
  renderClientProfileList();
  document.getElementById('waha-search-state').textContent = `Client "${{profile.name}}" saved (${{chatId}}).`;
}}
// ---- Client list with per-row actions (renders from the JSON textarea) ----
function clientProfiles() {{
  const box = document.getElementById('waha-clients');
  try {{
    const list = JSON.parse((box && box.value) || '[]');
    return Array.isArray(list) ? list : [];
  }} catch (err) {{ return []; }}
}}
function renderClientProfileList() {{
  const node = document.getElementById('client-profile-list');
  if (!node) return;
  const list = clientProfiles();
  if (!list.length) {{ node.textContent = 'No client profiles yet — add one above.'; return; }}
  const rows = list.map((c, i) => `<tr class="${{c.enabled === false ? 'client-off' : ''}}"><td>${{esc(c.name || '')}}</td><td class="mono-cell">${{esc(c.chat_id || '')}}</td><td>${{esc((c.purposes || []).join(', '))}}</td><td>${{esc(c.filters || '')}}</td><td class="mono-cell">${{esc(c.email || '')}}${{c.phone ? '<br>' + esc(c.phone) : ''}}</td><td>${{c.enabled === false ? 'disabled' : 'enabled'}}</td><td class="client-actions"><button onclick="editClientProfile(${{i}})">Edit</button><button onclick="toggleClientProfile(${{i}})">${{c.enabled === false ? 'Enable' : 'Disable'}}</button><button class="danger" onclick="deleteClientProfile(${{i}})">Delete</button></td></tr>`).join('');
  node.innerHTML = `<table class="cal-summary-table client-table"><thead><tr><th>Name</th><th>Destination</th><th>Purposes</th><th>Filters</th><th>Email / phone</th><th>State</th><th>Actions</th></tr></thead><tbody>${{rows}}</tbody></table>`;
}}
function writeClientProfiles(list, note) {{
  const box = document.getElementById('waha-clients');
  box.value = JSON.stringify(list, null, 2);
  saveWahaClients();
  renderClientProfileList();
  if (note) document.getElementById('waha-search-state').textContent = note;
}}
function editClientProfile(i) {{
  const c = clientProfiles()[i];
  if (!c) return;
  document.getElementById('client-name').value = c.name || '';
  document.getElementById('client-chat-custom').value = c.chat_id || '';
  ['index', 'details', 'status'].forEach(p => {{ document.getElementById('client-purpose-' + p).checked = (c.purposes || []).includes(p); }});
  document.getElementById('client-filters').value = c.filters || '';
  document.getElementById('waha-search-state').textContent = `Editing "${{c.name || c.chat_id}}" — adjust the fields above and press "Add to profiles" to save.`;
}}
function toggleClientProfile(i) {{
  const list = clientProfiles();
  if (!list[i]) return;
  list[i].enabled = list[i].enabled === false;
  writeClientProfiles(list, `Client "${{list[i].name || list[i].chat_id}}" ${{list[i].enabled ? 'enabled' : 'disabled'}}.`);
}}
function deleteClientProfile(i) {{
  const list = clientProfiles();
  if (!list[i]) return;
  if (!confirm(`Delete client "${{list[i].name || list[i].chat_id}}"?`)) return;
  const removed = list.splice(i, 1)[0];
  writeClientProfiles(list, `Client "${{removed.name || removed.chat_id}}" deleted.`);
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
// Keep event chips compact by default. The display checkboxes below let users
// reveal the source hour and opportunity number when those details are useful.
let calendarShowHours = false;
let calendarShowNumbers = false;
let calendarShowEarlyHours = false;
let calendarShowWeekend = false;
function initCalendarDisplayControls() {{
  const filter = document.getElementById('cal-filter');
  const filterRow = filter && filter.closest('.cal-filter-row');
  if (!filterRow || filterRow.dataset.calendarLayoutReady === '1') return;
  const controlsRow = filterRow.previousElementSibling;
  if (controlsRow) controlsRow.classList.add('cal-controls-row');
  filter.size = 28;
  filterRow.dataset.calendarLayoutReady = '1';
  const row = document.createElement('p');
  row.className = 'cal-display-row';
  row.innerHTML = '<b>Event display</b>'
    + ' <label class="small"><input type="checkbox" id="cal-show-hours"> Show hours</label>'
    + ' <label class="small"><input type="checkbox" id="cal-show-numbers"> Show numbers</label>'
    + ' <label class="small"><input type="checkbox" id="cal-show-early-hours"> Show 00–06 rows</label>'
    + ' <label class="small"><input type="checkbox" id="cal-show-weekend"> Show Sat/Sun</label>';
  filterRow.parentNode.insertBefore(row, filterRow.nextSibling);
  document.getElementById('cal-show-hours').addEventListener('change', event => {{
    calendarShowHours = event.target.checked;
    renderCalendarVisual();
  }});
  document.getElementById('cal-show-numbers').addEventListener('change', event => {{
    calendarShowNumbers = event.target.checked;
    renderCalendarVisual();
  }});
  document.getElementById('cal-show-early-hours').addEventListener('change', event => {{
    calendarShowEarlyHours = event.target.checked;
    renderCalendarVisual();
  }});
  document.getElementById('cal-show-weekend').addEventListener('change', event => {{
    calendarShowWeekend = event.target.checked;
    renderCalendarVisual();
  }});
}}
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
    // /api/calendar is still fetched for its anchor/shift mechanics, but both
    // text panes are now HTML tables built from the calendar-grid JSON by
    // renderCalendarTextTable() / renderCalendarListTable() below.
    renderCalendarVisual();
  }} catch (err) {{ document.getElementById('calendar-text').textContent = 'Calendar unavailable: ' + err; }}
}}
function saveTemplatesSelection() {{
  const files = Array.from(document.querySelectorAll('.tpl-file:checked')).map(el => 'file=' + el.value);
  postForm('/api/templates-select', files.join('&'));
  setTimeout(loadTemplates, 400);
}}
function moveWebhookAutoRunControlToScheduler() {{
  const toggle = document.getElementById('set-PC_WEBHOOK_AUTO_RUN');
  const banner = document.getElementById('cd-schedule-banner');
  if (!toggle || !banner || toggle.dataset.schedulerMoved === '1') return;
  const label = toggle.closest('label');
  if (!label) return;
  const row = document.createElement('p');
  row.className = 'small';
  row.style.cssText = 'margin:8px 0;padding:8px 10px;background:#F6F5F2;border:1px solid #DEDBD4;border-radius:8px';
  row.appendChild(document.createTextNode('Trigger control: '));
  row.appendChild(label);
  banner.parentNode.insertBefore(row, banner.nextSibling);
  toggle.dataset.schedulerMoved = '1';
  const intro = banner.parentElement.querySelector('p.small');
  if (intro) intro.textContent = intro.textContent.replace('toggle in Settings', 'toggle below');
}}
moveWebhookAutoRunControlToScheduler();
function saveMonitorSetting(key, value) {{ postForm('/api/monitor-setting', 'key=' + encodeURIComponent(key) + '&value=' + encodeURIComponent(value)); }}
// Sister ARL-89 sites on this same host: WordPress home (8095) and the
// Homepage tools dashboard (8080). Links are built from location.hostname so
// they work identically from localhost and from any LAN PC.
const ARL_HOME_PORT = 8095;
const ARL_TOOLS_PORT = 8080;
function siteUrl(port) {{ return 'http://' + (location.hostname || '127.0.0.1') + ':' + port + '/'; }}
function initHeaderLinks() {{
  const home = document.getElementById('link-home-site');
  const tools = document.getElementById('link-tools-site');
  if (home) home.href = siteUrl(ARL_HOME_PORT);
  if (tools) tools.href = siteUrl(ARL_TOOLS_PORT);
}}
function toggleProgressCard() {{
  const card = document.getElementById('progress-card');
  const btn = document.getElementById('progress-toggle');
  card.classList.toggle('collapsed');
  const hidden = card.classList.contains('collapsed');
  if (btn) btn.textContent = hidden ? 'Show progress' : 'Hide progress';
  try {{ localStorage.setItem('pc_progress_hidden', hidden ? '1' : '0'); }} catch (err) {{ /* private mode */ }}
}}
function initProgressCard() {{
  try {{ if (localStorage.getItem('pc_progress_hidden') === '1') toggleProgressCard(); }} catch (err) {{ /* private mode */ }}
}}
function togglePane(id, btn) {{
  const el = document.getElementById(id);
  if (!el) return;
  el.hidden = !el.hidden;
  if (btn) btn.textContent = el.hidden ? 'Show' : 'Hide';
}}
// Staff sessions (manager-created users) only see the tabs the manager
// granted; tab buttons for everything else are hidden and showTab refuses
// them. Cards stay in the DOM (display:none) so the refresh JS keeps working.
async function applyTabAccess() {{
  let info;
  try {{
    info = await (await fetch('/api/session-info', {{cache: 'no-store'}})).json();
  }} catch (err) {{ return; }}
  if (!info || info.role !== 'staff') return;
  const allowed = new Set(info.tabs || []);
  staffAllowedTabs = allowed;
  let first = null;
  document.querySelectorAll('[data-tab-button]').forEach(btn => {{
    const tab = btn.dataset.tabButton;
    if (!allowed.has(tab)) {{ btn.style.display = 'none'; }}
    else if (!first) {{ first = tab; }}
  }});
  if (first) showTab(first);
  const userBox = document.getElementById('progress-toggle');
  if (info.user && userBox) userBox.insertAdjacentHTML('beforebegin', `<span class="small" style="color:var(--concrete-300)">${{esc(info.user)}}</span>`);
}}
// Calendar sub-tabs: the visual calendar, the ASCII text summary and the
// day-by-day opportunities list are three views of the same range — shown
// one at a time instead of stacked.
function showCalPane(name) {{
  ['visual', 'text', 'list'].forEach(pane => {{
    const el = document.getElementById('calpane-' + pane);
    if (el) el.hidden = pane !== name;
  }});
  document.querySelectorAll('[data-calpane]').forEach(btn => btn.classList.toggle('active', btn.dataset.calpane === name));
}}
async function adminSignOut() {{
  try {{ await fetch('/api/session-logout', {{method: 'POST'}}); }} catch (err) {{ /* cookie clear is best-effort */ }}
  location.href = siteUrl(ARL_HOME_PORT);  // leave the monitor for the ARL-89 home site
}}
// ---- Monitor users & tab access (manager only) ----
const MONITOR_TAB_IDS = ['overview', 'calendar', 'decision', 'records', 'operations', 'whatsapp', 'scheduler', 'integrations', 'settings'];
async function loadMonitorUsers() {{
  const box = document.getElementById('monitor-users');
  if (!box) return;
  try {{
    const resp = await fetch('/api/monitor-users', {{cache: 'no-store'}});
    if (!resp.ok) {{ if (resp.status === 403) {{ box.value = '(manager only)'; box.disabled = true; }} return; }}
    if (document.activeElement !== box) box.value = JSON.stringify(await resp.json(), null, 2);
  }} catch (err) {{ /* leave as is */ }}
}}
async function saveMonitorUsers() {{
  const box = document.getElementById('monitor-users');
  const state = document.getElementById('mu-state');
  try {{
    const resp = await fetch('/api/monitor-users', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
      body: 'users=' + encodeURIComponent(box.value || '[]'),
    }});
    const text = await resp.text();
    if (!resp.ok) {{ state.textContent = text.trim(); return; }}
    box.value = JSON.stringify(JSON.parse(text), null, 2);
    state.textContent = 'Users saved.';
  }} catch (err) {{ state.textContent = 'Save failed: ' + err; }}
}}
function addMonitorUser() {{
  const state = document.getElementById('mu-state');
  const username = document.getElementById('mu-username').value.trim();
  const password = document.getElementById('mu-password').value;
  if (!username || !password) {{ state.textContent = 'Username and password are required.'; return; }}
  const tabs = MONITOR_TAB_IDS.filter(tab => (document.getElementById('mu-tab-' + tab) || {{}}).checked);
  const box = document.getElementById('monitor-users');
  let users = [];
  try {{ users = JSON.parse(box.value || '[]'); }} catch (err) {{ users = []; }}
  if (!Array.isArray(users)) users = [];
  const existing = users.findIndex(u => u && u.username === username);
  const entry = {{username, password, tabs, enabled: true}};
  if (existing >= 0) users[existing] = Object.assign({{}}, users[existing], entry);
  else users.push(entry);
  box.value = JSON.stringify(users, null, 2);
  saveMonitorUsers();
  document.getElementById('mu-password').value = '';
}}
function savePathSettings() {{
  [['PC_RECORDS_DIR', 'records-dir'], ['PC_CALENDAR_DIR', 'calendar-dir'], ['PC_RECORDS_TEST_DIR', 'records-test-dir']].forEach(([key, id]) => saveMonitorSetting(key, document.getElementById(id).value));
}}
let monitorSettingsLoaded = false;
function saveAdvancedSettings() {{
  // Guard against the blank-wipe foot-gun: saving before the first
  // /api/status refresh would write '' into every advanced setting
  // (this once cleared WAHA_API_KEY and broke the directory search).
  if (!monitorSettingsLoaded) {{ alert('Settings are still loading — try again in a moment.'); return; }}
  ['PC_WAHA_SOURCE','PC_AUTORUN_SOURCE','PC_CRON_INDEX_LIMIT','PC_CRON_DETAIL_LIMIT','PC_MONITOR_HOST','PC_NEXT_RUN_INTERVAL_MINUTES','PC_MONITOR_DEADLINE_SOON_DAYS','PC_WEBHOOK_INDEX_LIMIT','PC_WEBHOOK_DETAIL_LIMIT','PC_NOTIFY_WITHIN_DAYS','PC_WAHA_RETRIES','PC_WAHA_SEND_DELAY_SECONDS','PC_NOTIFY_INDEX_DIGEST_THRESHOLD','PC_NOTIFY_IDLE_EVERY_HOURS','PC_WAHA_BASE_URL','PC_WAHA_SESSION','PC_WAHA_NOTIFY_EVENTS','PC_TEST_ZONE_LIMIT','PC_MONITOR_STALE_SECONDS','PC_NEXT_RUN_TIMER_WIDTH','PC_NEXT_RUN_TIMER_HEIGHT','PC_NEXT_RUN_TIMER_TOP','PC_NEXT_RUN_TIMER_RECORDS','PC_NEXT_RUN_TIMER_DATA_REFRESH_SECONDS','CHANGEDETECTION_BASE_URL','PC_WEBHOOK_PORT','PC_WEBHOOK_PUBLIC_HOST','WAHA_PORT','WAHA_API_KEY','WAHA_DASHBOARD_USERNAME','WAHA_DASHBOARD_PASSWORD','PC_FIREBASE_WEB_API_KEY','PC_FIREBASE_WEB_AUTH_DOMAIN','PC_FIREBASE_WEB_PROJECT_ID','PC_FIREBASE_WEB_APP_ID','PC_ADMIN_USERNAME','PC_ADMIN_PASSWORD','PC_ADMIN_EMAILS','PC_ADMIN_API_TOKEN'].forEach(key => {{ const el = document.getElementById('set-' + key); if (el) saveMonitorSetting(key, el.value); }});
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
const STATUS_COLOR = {{expired: '#AE2D1C', soon: '#B06E00', upcoming: '#247A47', unknown: '#7D7872'}};
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
function calendarIsoWeekNumber(date) {{
  const d = new Date(Date.UTC(date.getUTCFullYear(), date.getUTCMonth(), date.getUTCDate()));
  const day = (d.getUTCDay() + 6) % 7;
  d.setUTCDate(d.getUTCDate() - day + 3);
  const firstThursday = new Date(Date.UTC(d.getUTCFullYear(), 0, 4));
  return 1 + Math.round((d - firstThursday) / 604800000);
}}
function calendarMonthWeekStart(grid, row) {{
  const parts = String(grid.start || '').slice(0, 10).split('-').map(Number);
  const first = new Date(Date.UTC(parts[0] || 1970, (parts[1] || 1) - 1, parts[2] || 1));
  first.setUTCDate(first.getUTCDate() - Number(grid.first_weekday || 0) + row * 7);
  return first;
}}
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
// Progressive zoom: year → month → week → day. Events are only interactive
// (location hover tooltip + link to the opportunity page) in the day view —
// in month/week views a click anywhere in a cell zooms in instead.
let calendarEventsInteractive = false;
function calendarZoomTo(iso, view) {{
  document.getElementById('cal-date').value = iso;
  document.getElementById('cal-view').value = view;
  loadCalendar();
}}
function renderCalendarEvent(ev) {{
  const number = calendarShowNumbers && ev.numero ? ev.numero + ' · ' : '';
  const clock = calendarShowHours && ev.clock && ev.clock !== '--:--' ? ev.clock + ' ' : '';
  const title = number + (ev.description || '(sin descripcion)');
  const cls = calendarEventClass(ev);
  const label = esc(clock + title);
  if (!calendarEventsInteractive) {{
    return `<span class="calevent ${{cls}}">${{label}}</span>`;
  }}
  if (ev.url) {{
    return `<a class="calevent ${{cls}}" data-numero="${{esc(ev.numero)}}" href="${{esc(ev.url)}}" target="_blank" rel="noopener">${{label}}</a>`;
  }}
  return `<span class="calevent ${{cls}}" data-numero="${{esc(ev.numero)}}">${{label}}</span>`;
}}
function renderCalendarDayCell(day, events, blank, maxShown) {{
  if (blank) return '<div class="calcell blank"></div>';
  const limit = maxShown || 4;
  const shown = (events || []).slice(0, limit).map(renderCalendarEvent).join('');
  const more = (events || []).length > limit ? `<span class="calevent more">+${{events.length - limit}} more</span>` : '';
  const classes = ['calcell'];
  if (day.iso === day.today) classes.push('today');
  return `<div class="${{classes.join(' ')}}" onclick="calendarZoomTo('${{day.iso}}', 'day')"><div class="num"><span>${{day.label}}</span><span class="count">${{events.length || ''}}</span></div>${{shown}}${{more}}</div>`;
}}
function calendarVisibilityWarning(earlyCount, weekendCount) {{
  const notices = [];
  if (earlyCount) notices.push(`${{earlyCount}} event(s) between 00:00 and 06:00 are hidden. Use “Show 00–06 rows”.`);
  if (weekendCount) notices.push(`${{weekendCount}} event(s) on Saturday/Sunday are hidden. Use “Show Sat/Sun”.`);
  return notices.length ? `<div class="calendar-warning" role="status">⚠ ${{notices.join(' ')}}</div>` : '';
}}
// "Text summary" pane: the same range as the visual calendar, rendered as a
// compact counts TABLE (per view) instead of the old ASCII <pre> — easier to
// read and every cell zooms like the visual calendar does.
function renderCalendarTextTable(g, view, title) {{
  const node = document.getElementById('calendar-text');
  if (!node) return;
  const grouped = g.events || {{}};
  const countOf = iso => (grouped[iso] || []).length;
  let html = `<p class="small" style="margin:2px 0 8px">${{esc(title)}}</p>`;
  if (view === 'year') {{
    const peak = Math.max(1, ...Object.values(g.month_counts || {{}}).map(Number));
    const rows = (g.months || []).map(m => {{
      const count = Number((g.month_counts || {{}})[m.value] || 0);
      const width = Math.round(100 * count / peak);
      return `<tr onclick="calendarZoomTo('${{m.value}}-01', 'month')"><td>${{esc(m.label)}}</td><td class="num-cell">${{count || ''}}</td><td class="trend-cell"><div class="bar-track"><div class="bar-fill" style="width:${{width}}%"></div></div></td></tr>`;
    }}).join('');
    html += `<table class="cal-summary-table"><thead><tr><th>Month</th><th>Opportunities</th><th>Trend</th></tr></thead><tbody>${{rows}}</tbody></table>`;
  }} else if (view === 'month') {{
    const days = g.days || [];
    const leading = Number(g.first_weekday || 0);
    const rowCount = Math.ceil((leading + days.length) / 7);
    let body = '';
    for (let row = 0; row < rowCount; row++) {{
      let tr = '';
      for (let col = 0; col < 7; col++) {{
        const index = row * 7 + col - leading;
        if (index < 0 || index >= days.length) {{ tr += '<td class="blank"></td>'; continue; }}
        const day = days[index];
        const count = countOf(day.iso);
        tr += `<td${{day.iso === g.today ? ' class="today-cell"' : ''}} onclick="calendarZoomTo('${{day.iso}}', 'week')" title="Open week of ${{day.iso}}"><b>${{day.label}}</b>${{count ? `<span class="cnt">${{count}}</span>` : '<span class="cnt zero">·</span>'}}</td>`;
      }}
      body += `<tr>${{tr}}</tr>`;
    }}
    html += `<table class="cal-summary-table cal-summary-month"><thead><tr>${{WEEKDAY_LABELS.map(d => `<th>${{d}}</th>`).join('')}}</tr></thead><tbody>${{body}}</tbody></table>`;
  }} else if (view === 'week') {{
    const rows = (g.days || []).map((day, i) => `<tr onclick="calendarZoomTo('${{day.iso}}', 'day')"><td>${{WEEKDAY_LABELS[i] || ''}} ${{esc(day.iso)}}${{day.iso === g.today ? ' <b>(today)</b>' : ''}}</td><td class="num-cell">${{countOf(day.iso) || ''}}</td></tr>`).join('');
    html += `<table class="cal-summary-table"><thead><tr><th>Day</th><th>Opportunities</th></tr></thead><tbody>${{rows}}</tbody></table>`;
  }} else {{
    const day = (g.days || [])[0] || {{}};
    const evs = grouped[day.iso] || [];
    const hasTime = ev => ev.clock && ev.clock !== '--:--';
    const byHour = {{}};
    let notime = 0;
    evs.forEach(ev => {{ if (hasTime(ev)) {{ const hour = ev.clock.slice(0, 2); byHour[hour] = (byHour[hour] || 0) + 1; }} else {{ notime += 1; }} }});
    let rows = notime ? `<tr><td>No time</td><td class="num-cell">${{notime}}</td></tr>` : '';
    rows += Object.keys(byHour).sort().map(hour => `<tr><td>${{hour}}:00</td><td class="num-cell">${{byHour[hour]}}</td></tr>`).join('');
    html += `<table class="cal-summary-table"><thead><tr><th>Hour</th><th>Opportunities</th></tr></thead><tbody>${{rows || '<tr><td colspan="2">No opportunities this day.</td></tr>'}}</tbody></table>`;
  }}
  node.innerHTML = html;
}}
// "Opportunities list" pane: every event in the current range as a tidy
// table (Date | Time | Numero | Description | Status) instead of the old
// ASCII day-by-day dump. Dates zoom to the day view; numeros open the
// opportunity page. Rendering is capped so a busy month can't freeze the tab.
const CALENDAR_LIST_MAX_ROWS = 800;
function renderCalendarListTable(g, title) {{
  const node = document.getElementById('calendar-list');
  if (!node) return;
  const grouped = g.events || {{}};
  const dates = Object.keys(grouped).sort();
  let rows = '';
  let shown = 0;
  let total = 0;
  for (const iso of dates) {{
    for (const ev of (grouped[iso] || [])) {{
      total += 1;
      if (shown >= CALENDAR_LIST_MAX_ROWS) continue;
      shown += 1;
      const cls = calendarEventClass(ev);
      const numero = ev.url
        ? `<a href="${{esc(ev.url)}}" target="_blank" rel="noopener">${{esc(ev.numero || '')}}</a>`
        : esc(ev.numero || '');
      rows += `<tr class="list-${{cls || 'ok'}}"><td class="date-cell" onclick="calendarZoomTo('${{iso}}', 'day')" title="Open the day view">${{iso}}</td><td class="num-cell">${{ev.clock && ev.clock !== '--:--' ? ev.clock : '—'}}</td><td class="mono-cell">${{numero}}</td><td class="desc-cell">${{esc(ev.description || '(sin descripcion)')}}</td><td>${{esc(ev.status || '')}}</td></tr>`;
    }}
  }}
  const capNote = total > shown ? ` — showing the first ${{shown}} of ${{total}}; zoom in or use the keyword filter for the rest` : '';
  node.innerHTML = `<p class="small" style="margin:2px 0 8px">${{esc(title)}}${{capNote}} · click a date to open its day, a numero to open the opportunity.</p>`
    + `<table class="cal-summary-table cal-list-table"><thead><tr><th>Date</th><th>Time</th><th>Numero</th><th>Description</th><th>Status</th></tr></thead><tbody>${{rows || '<tr><td colspan="5">No opportunities in this range.</td></tr>'}}</tbody></table>`;
}}
async function renderCalendarVisual() {{
  const node = document.getElementById('calendar-visual');
  if (!node) return;
  const field = (document.getElementById('cal-field') || {{}}).value || 'end';
  const view = (document.getElementById('cal-view') || {{}}).value || 'month';
  calendarEventsInteractive = view === 'day';
  const anchor = ((document.getElementById('cal-date') || {{}}).value || calendarAnchor || '').trim();
  const keyword = ((document.getElementById('cal-filter') || {{}}).value || '').trim();
  let params = 'field=' + encodeURIComponent(field) + '&view=' + encodeURIComponent(view);
  if (anchor) params += '&date=' + encodeURIComponent(anchor);
  if (keyword) params += '&filter=' + encodeURIComponent(keyword);
  try {{
    const g = await (await fetch('/api/calendar-grid?' + params, {{cache: 'no-store'}})).json();
    const grouped = g.events || {{}};
    const title = `${{g.label || view}} · ${{g.start}} to ${{g.end}} · ${{g.total || 0}} opportunities`;
    renderCalendarTextTable(g, view, title);
    renderCalendarListTable(g, title);
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
      const earlyEvents = byHour.slice(0, 7).flat();
      const collapsedEarlyRow = !calendarShowEarlyHours && earlyEvents.length
        ? `<div class="hour-label timeline-collapsed">00–06</div><div class="hour-lane timeline-collapsed">${{earlyEvents.map(renderCalendarEvent).join('')}}</div>` : '';
      const visibilityWarning = calendarVisibilityWarning(calendarShowEarlyHours ? 0 : earlyEvents.length, 0);
      const firstHour = calendarShowEarlyHours ? 0 : 7;
      const hourRows = byHour.slice(firstHour).map((evsAtHour, index) => {{
        const h = index + firstHour;
        return `<div class="hour-label">${{String(h).padStart(2, '0')}}:00</div><div class="hour-lane">${{evsAtHour.map(renderCalendarEvent).join('')}}</div>`;
      }}).join('');
      const note = evs.length ? 'Hover an event for location details; click it to open the opportunity.' : 'No opportunities on this day.';
      node.innerHTML = `<div class="calendar-board day-agenda"><div class="calendar-title"><h3>${{esc(title)}}</h3><span class="small">${{note}}</span></div>${{visibilityWarning}}<div class="timeline-scroll"><div class="timeline">${{notimeRow}}${{collapsedEarlyRow}}${{hourRows}}</div></div></div>`;
      return;
    }}
    if (view === 'week') {{
      // Hourly timeline with one column per visible day: a time-label column
      // plus weekday/weekend columns, each split into 24 hour lanes.
      const cols = days.map((day, i) => {{
        const evs = grouped[day.iso] || [];
        const byHour = Array.from({{length: 24}}, () => []);
        evs.forEach(ev => {{ if (hasTime(ev)) byHour[hourOf(ev)].push(ev); }});
        return {{ i, iso: day.iso, notime: evs.filter(ev => !hasTime(ev)), byHour, total: evs.length }};
      }});
      const visibleCols = calendarShowWeekend ? cols : cols.filter(c => c.i < 5);
      const anyNotime = visibleCols.some(c => c.notime.length);
      const hiddenWeekendEvents = calendarShowWeekend
        ? 0 : cols.filter(c => c.i >= 5).reduce((sum, c) => sum + c.total, 0);
      const hiddenEarlyEvents = calendarShowEarlyHours
        ? 0 : visibleCols.reduce((sum, c) => sum + c.byHour.slice(0, 7).flat().length, 0);
      const hasEarlyEvents = visibleCols.some(c => c.byHour.slice(0, 7).some(evsAtHour => evsAtHour.length));
      const collapsedEarlyRow = !calendarShowEarlyHours && hasEarlyEvents
        ? '<div class="hour-label wk-collapsed">00–06</div>' + visibleCols.map(c => `<div class="hour-lane wk-collapsed">${{c.byHour.slice(0, 7).flat().map(renderCalendarEvent).join('')}}</div>`).join('') : '';
      let hourRows = '';
      const firstHour = calendarShowEarlyHours ? 0 : 7;
      for (let h = firstHour; h < 24; h++) {{
        hourRows += `<div class="hour-label">${{String(h).padStart(2, '0')}}:00</div>`;
        hourRows += visibleCols.map(c => `<div class="hour-lane">${{c.byHour[h].map(renderCalendarEvent).join('')}}</div>`).join('');
      }}
      const visibleHead = '<div class="wk-corner"></div>' + visibleCols.map(c => `<div class="wk-head zoomable" onclick="calendarZoomTo('${{c.iso}}', 'day')">${{WEEKDAY_LABELS[c.i] || ''}} ${{esc((c.iso || '').slice(5))}}</div>`).join('');
      const visibleNotimeRow = anyNotime
        ? '<div class="hour-label wk-notime">No time</div>' + visibleCols.map(c => `<div class="hour-lane wk-notime">${{c.notime.map(renderCalendarEvent).join('')}}</div>`).join('') : '';
      const visibilityWarning = calendarVisibilityWarning(hiddenEarlyEvents, hiddenWeekendEvents);
      node.innerHTML = `<div class="calendar-board week-agenda"><div class="calendar-title"><h3>${{esc(title)}}</h3><span class="small">Click a day header to zoom to that day.</span></div>${{visibilityWarning}}<div class="timeline-scroll"><div class="week-timeline" style="grid-template-columns: 64px repeat(${{visibleCols.length}}, minmax(0, 1fr));">${{visibleHead}}${{visibleNotimeRow}}${{collapsedEarlyRow}}${{hourRows}}</div></div></div>`;
      return;
    }}
    const leading = Number(g.first_weekday || 0);
    const rowCount = Math.ceil((leading + days.length) / 7);
    const weeks = g.weeks || [];
    const visibleDayColumns = calendarShowWeekend ? [0, 1, 2, 3, 4, 5, 6] : [0, 1, 2, 3, 4];
    const hiddenWeekendEvents = calendarShowWeekend ? 0 : days.reduce((sum, day) => {{
      const weekday = new Date(day.iso + 'T12:00:00Z').getUTCDay();
      return sum + ((weekday === 0 || weekday === 6) ? (grouped[day.iso] || []).length : 0);
    }}, 0);
    const visibilityWarning = calendarVisibilityWarning(0, hiddenWeekendEvents);
    let cells = '<div class="week-number-header">Wk</div>' + visibleDayColumns.map(i => `<div class="dow">${{WEEKDAY_LABELS[i]}}</div>`).join('');
    for (let row = 0; row < rowCount; row++) {{
      const weekInfo = weeks[row] || {{}};
      const weekStart = weekInfo.start ? new Date(weekInfo.start + 'T12:00:00Z') : calendarMonthWeekStart(g, row);
      const weekIso = weekInfo.start || weekStart.toISOString().slice(0, 10);
      const weekNumber = weekInfo.number || calendarIsoWeekNumber(weekStart);
      cells += `<button class="week-number" type="button" title="Open week ${{weekNumber}}" onclick="calendarZoomTo('${{weekIso}}', 'week')">${{weekNumber}}</button>`;
      for (const col of visibleDayColumns) {{
        const index = row * 7 + col - leading;
        cells += index < 0 || index >= days.length
          ? renderCalendarDayCell(null, [], true)
          : renderCalendarDayCell(days[index], grouped[days[index].iso] || [], false);
      }}
    }}
    node.innerHTML = `<div class="calendar-board"><div class="calendar-title"><h3>${{esc(title)}}</h3><span class="small">Click a day to open its day view; click Wk to open the week.</span></div>${{visibilityWarning}}<div class="calgrid" style="grid-template-columns: 48px repeat(${{visibleDayColumns.length}}, minmax(0, 1fr));">${{cells}}</div></div>`;
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

let staffAllowedTabs = null;  // null = full admin (all tabs); Set for staff sessions
function showTab(tab) {{
  if (staffAllowedTabs && !staffAllowedTabs.has(tab)) return;
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
    const chip = (ok, onText, offText) => `<span style="color:${{ok ? '#247A47' : '#AE2D1C'}};font-weight:700">${{ok ? onText : offText}}</span>`;
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
initHeaderLinks();
initProgressCard();
applyTabAccess();
loadMonitorUsers();
initCalendarDisplayControls();
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
{_LOC_TOOLTIP_JS}
</script>
</body>
</html>"""


class MonitorHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: object) -> None:
        return

    def send_text(self, status: int, body: str, content_type: str, extra_headers: dict[str, str] | None = None) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(encoded)

    # ---- Admin access checks ----
    def _is_local_request(self) -> bool:
        """True when the request comes from this PC itself (never locked out)."""
        return self.client_address[0] in ("127.0.0.1", "::1", "::ffff:127.0.0.1")

    def _session(self) -> dict | None:
        cookies = self.headers.get("Cookie", "")
        for part in cookies.split(";"):
            name, _, value = part.strip().partition("=")
            if name == SESSION_COOKIE_NAME and value:
                return parse_session_token(value)
        return None

    def _has_admin_access(self) -> bool:
        return self._access_level()[0] == "admin"

    def _access_level(self) -> tuple[str | None, list[str]]:
        """("admin", all tabs) for full access, ("staff", granted tabs) for a
        manager-created user, (None, []) for no valid session."""
        if self._is_local_request():
            return "admin", list(VALID_MONITOR_TABS)
        session = self._session()
        if session and session.get("role") == "admin":
            return "admin", list(VALID_MONITOR_TABS)
        if session and session.get("role") == "staff":
            tabs = [t for t in (session.get("tabs") or []) if t in VALID_MONITOR_TABS]
            return "staff", tabs
        expected = load_monitor_settings().get("PC_ADMIN_API_TOKEN", "").strip()
        if expected:
            supplied = (self.headers.get("X-PC-Admin-Token", "")
                        or parse_qs(urlparse(self.path).query).get("admin_token", [""])[0]).strip()
            if supplied and hmac.compare_digest(supplied, expected):
                return "admin", list(VALID_MONITOR_TABS)
        return None, []

    def _session_cookie_header(self, token: str, max_age: int) -> dict[str, str]:
        return {"Set-Cookie": f"{SESSION_COOKIE_NAME}={token}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Lax"}

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", "0") or "0")
        form = parse_qs(self.rfile.read(length).decode("utf-8", errors="replace"))
        if path == "/api/session-login":
            # Manager path: local username/password from monitor settings.
            username = form.get("user", [""])[0].strip()
            password = form.get("password", [""])[0]
            if username or password:
                settings = load_monitor_settings()
                expected_user = settings.get("PC_ADMIN_USERNAME", "").strip()
                expected_password = settings.get("PC_ADMIN_PASSWORD", "")
                if expected_user and expected_password \
                        and hmac.compare_digest(username, expected_user) \
                        and hmac.compare_digest(password, expected_password):
                    token = make_session_token("local-admin", username, "admin")
                    self.send_text(200, json.dumps({"role": "admin", "user": username}),
                                   "application/json; charset=utf-8",
                                   self._session_cookie_header(token, SESSION_TTL_SECONDS))
                    return
                # Manager-created users (monitor_users.json): same form, but
                # the session only carries the tabs the manager granted.
                staff = find_monitor_user(username, password)
                if staff is not None:
                    token = make_session_token("staff:" + staff["username"], staff["username"], "staff", tabs=staff["tabs"])
                    self.send_text(200, json.dumps({"role": "staff", "user": staff["username"], "tabs": staff["tabs"]}),
                                   "application/json; charset=utf-8",
                                   self._session_cookie_header(token, SESSION_TTL_SECONDS))
                    return
                if not expected_user or not expected_password:
                    self.send_text(503, "Manager login is not configured (PC_ADMIN_USERNAME / PC_ADMIN_PASSWORD).\n", "text/plain; charset=utf-8")
                    return
                time.sleep(0.8)  # slow down brute force
                self.send_text(403, "Invalid username or password.\n", "text/plain; charset=utf-8")
                return
            # Firebase path: clients (and any PC_ADMIN_EMAILS admin) send the
            # signed-in user's ID token, verified server-side with Google.
            id_token = form.get("idToken", [""])[0].strip()
            if not id_token:
                self.send_text(400, "missing credentials\n", "text/plain; charset=utf-8")
                return
            verified = verify_firebase_id_token(id_token)
            if verified is None:
                self.send_text(403, "Sign-in could not be verified. Try again.\n", "text/plain; charset=utf-8")
                return
            role = "admin" if verified["email"] and verified["email"] in admin_emails() else "client"
            token = make_session_token(verified["uid"], verified["email"], role)
            self.send_text(200, json.dumps({"role": role, "email": verified["email"]}),
                           "application/json; charset=utf-8",
                           self._session_cookie_header(token, SESSION_TTL_SECONDS))
            return
        if path == "/api/session-logout":
            self.send_text(200, json.dumps({"ok": True}), "application/json; charset=utf-8",
                           self._session_cookie_header("", 0))
            return
        if path == "/api/monitor-users":
            # Full admin only: create/replace the manager-defined users list.
            if self._access_level()[0] != "admin":
                self.send_text(403, "manager access required\n", "text/plain; charset=utf-8")
                return
            try:
                saved = save_monitor_users_text(form.get("users", ["[]"])[0])
            except ValueError as exc:
                self.send_text(400, f"{exc}\n", "text/plain; charset=utf-8")
                return
            self.send_text(200, json.dumps(saved, ensure_ascii=False), "application/json; charset=utf-8")
            return
        if path not in OPEN_POST_PATHS:
            access, tabs = self._access_level()
            if access is None:
                self.send_text(401, "admin sign-in required\n", "text/plain; charset=utf-8")
                return
            if access == "staff":
                required_tab = STAFF_POST_TAB_MAP.get(path)
                if required_tab is None or required_tab not in tabs:
                    self.send_text(403, "your account does not have access to this action\n", "text/plain; charset=utf-8")
                    return
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
        if path in ("/", "/index.html"):
            # Front page: dashboard for admin AND staff sessions (staff see
            # only their granted tabs, applied by /api/session-info), login
            # page otherwise.
            if self._access_level()[0] is not None:
                self.send_text(200, HTML, "text/html; charset=utf-8")
            else:
                self.send_text(200, LOGIN_HTML, "text/html; charset=utf-8")
            return
        access, access_tabs = (self._access_level() if path not in OPEN_GET_PATHS else ("open", []))
        if access is None:
            self.send_text(401, "admin sign-in required\n", "text/plain; charset=utf-8")
            return
        if path == "/api/session-info":
            session = self._session() or {}
            payload = {
                "role": access if access != "open" else None,
                "user": session.get("email", "local" if self._is_local_request() else ""),
                "tabs": access_tabs if access == "staff" else list(VALID_MONITOR_TABS),
            }
            self.send_text(200, json.dumps(payload, ensure_ascii=False), "application/json; charset=utf-8")
            return
        if path == "/api/monitor-users":
            if access != "admin":
                self.send_text(403, "manager access required\n", "text/plain; charset=utf-8")
                return
            self.send_text(200, json.dumps(read_monitor_users(), ensure_ascii=False, indent=2), "application/json; charset=utf-8")
            return
        if path == "/api/status":
            payload = status_payload()
            if access == "staff":
                # Staff sessions never receive secret settings values.
                settings_map = payload.get("settings")
                if isinstance(settings_map, dict):
                    payload["settings"] = {k: v for k, v in settings_map.items() if k not in SENSITIVE_SETTING_KEYS}
            self.send_text(200, json.dumps(payload, ensure_ascii=False, indent=2), "application/json; charset=utf-8")
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
                base_url = (os.environ.get("PC_WAHA_BASE_URL") or setting("PC_WAHA_BASE_URL", "http://127.0.0.1:3000")).rstrip("/")
                api_key = waha_api_key()
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
        if path == "/api/opportunity-location":
            # All delivery/location data for one opportunity — used by the
            # calendar hover tooltip. Read on demand so the grid stays light.
            params = parse_qs(urlparse(self.path).query)
            numero = (params.get("numero", [""])[0] or "").strip()
            if not numero:
                self.send_text(400, "missing ?numero=\n", "text/plain; charset=utf-8")
                return
            self.send_text(200, json.dumps(opportunity_location(numero), ensure_ascii=False), "application/json; charset=utf-8")
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
        if path in ("/client-calendar", "/client"):
            self.send_text(200, CLIENT_CALENDAR_HTML, "text/html; charset=utf-8")
            return
        if path == "/api/client-auth-config":
            # Firebase web-app config for the browser client dashboard's
            # sign-in screen. Public identifiers only (Firebase web configs
            # are not secrets); "configured" tells the page whether the
            # operator has filled them in yet.
            settings = load_monitor_settings()
            config = {
                "apiKey": settings.get("PC_FIREBASE_WEB_API_KEY", ""),
                "authDomain": settings.get("PC_FIREBASE_WEB_AUTH_DOMAIN", ""),
                "projectId": settings.get("PC_FIREBASE_WEB_PROJECT_ID", ""),
                "appId": settings.get("PC_FIREBASE_WEB_APP_ID", ""),
            }
            config["configured"] = bool(config["apiKey"] and config["authDomain"] and config["projectId"] and config["appId"])
            self.send_text(200, json.dumps(config, ensure_ascii=False), "application/json; charset=utf-8")
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
