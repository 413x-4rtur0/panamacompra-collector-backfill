#!/usr/bin/env python3
"""Shared KPI/data engine for the PanamaCompra monitors (audit Phase 4).

Single source of truth for the archive-database aggregates and helpers that
were previously duplicated between the Tk monitor (001a), the web monitor
(001b) and the ``pcc kpi`` heredoc. Both monitors import from here, and
``bin/pcc kpi`` executes this file directly, so the three surfaces always
report the same numbers for the same filters.

CLI usage (what ``pcc kpi`` runs):

    monitor_common.py [--days N] [--grupo X] [--entidad Y] [--json]

Text mode prints the KPI summary plus terminal bar charts; ``--json`` emits
the full payload for scripts.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parent.parent
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))
import common as pc_common

# ── WAHA helpers (shared between both monitors) ──────────────────────────────

WAHA_DEFAULT_BASE_URL = "http://127.0.0.1:3000"


def _waha_api_get(path: str, timeout: float = 8.0):
    """GET a WAHA REST endpoint. Returns parsed JSON or raises."""
    import urllib.request
    base_url = os.environ.get("PC_WAHA_BASE_URL", WAHA_DEFAULT_BASE_URL).rstrip("/")
    api_key = (os.environ.get("PC_WAHA_API_KEY") or os.environ.get("WAHA_API_KEY", "")).strip()
    headers = {"Accept": "application/json"}
    if api_key:
        headers["X-Api-Key"] = api_key
    request = urllib.request.Request(f"{base_url}{path}", headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - local WAHA
        return json.loads(response.read().decode("utf-8", "replace"))


def _waha_entry_id(item: dict) -> str:
    """Chat id from a WAHA group/contact payload, tolerating format variants."""
    raw = item.get("id")
    if isinstance(raw, dict):
        return str(raw.get("_serialized") or raw.get("id") or "")
    return str(raw or "")


_WAHA_KINDS = {"@g.us": "group", "@c.us": "contact", "@s.whatsapp.net": "channel",
               "@lid": "community", "@newsletter": "broadcast"}


def _waha_kind_for_chat_id(chat_id: str) -> str:
    """Classify a chat ID by its suffix."""
    for suffix, kind in _WAHA_KINDS.items():
        if suffix in chat_id:
            return kind
    return "chat"


def waha_fetch_all(query: str = "", *,
                   base_url: str = "", api_key: str = "",
                   include_chats: bool = True) -> list[dict]:
    """Fetch all groups/contacts/chats from every WAHA session and return
    structured matches.

    Each match dict: ``{"session", "id", "name", "kind"}``.

    When ``query`` is non-empty, results are filtered by accent-insensitive
    substring match on name. Empty query returns everything (groups first).
    Never raises: returns [] on error.
    """
    import urllib.request  # noqa: PLC0415
    import urllib.error  # noqa: PLC0415

    if not base_url:
        base_url = os.environ.get("PC_WAHA_BASE_URL", WAHA_DEFAULT_BASE_URL)
    base_url = base_url.rstrip("/")
    if not api_key:
        api_key = (os.environ.get("PC_WAHA_API_KEY") or os.environ.get("WAHA_API_KEY", "")).strip()

    headers = {"Accept": "application/json"}
    if api_key:
        headers["X-Api-Key"] = api_key

    def _get(path: str, timeout: float = 8.0):
        req = urllib.request.Request(f"{base_url}{path}", headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8", "replace"))

    wanted = pc_common.strip_accents(query or "").lower().strip()

    try:
        sessions = _get("/api/sessions?all=true")
    except Exception:
        return []
    if not isinstance(sessions, list):
        return []

    matches: list[dict] = []
    seen_ids: set[str] = set()

    for s in sessions:
        if not isinstance(s, dict):
            continue
        sname = str(s.get("name") or "default")
        status = str(s.get("status") or "").upper()
        if status and status not in {"WORKING", "RUNNING", "STARTING"}:
            continue

        for kind, path in (("group", f"/api/{sname}/groups"),
                           ("contact", f"/api/contacts/all?session={sname}")):
            try:
                entries = _get(path)
            except Exception:
                continue
            if not isinstance(entries, list):
                continue
            for item in entries:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or item.get("subject") or item.get("pushname") or "").strip()
                chat_id = _waha_entry_id(item)
                if not chat_id or chat_id in seen_ids:
                    continue
                seen_ids.add(chat_id)
                if kind == "contact" and not name:
                    continue
                if wanted and wanted not in pc_common.strip_accents(name).lower():
                    continue
                matches.append({"session": sname, "id": chat_id, "name": name or chat_id, "kind": kind})

        if not include_chats:
            continue

        try:
            for item in _get(f"/api/{sname}/chats"):
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "").strip()
                chat_id = _waha_entry_id(item)
                if not chat_id or chat_id in seen_ids:
                    continue
                seen_ids.add(chat_id)
                if wanted and wanted not in pc_common.strip_accents(name).lower():
                    continue
                matches.append({"session": sname, "id": chat_id, "name": name or chat_id,
                                "kind": _waha_kind_for_chat_id(chat_id)})
        except Exception:
            pass

    matches.sort(key=lambda m: (m["kind"] != "group", pc_common.strip_accents(m["name"]).lower()))
    return matches[:250]


def waha_session_status() -> dict:
    """Health of the configured WAHA session (PC_WAHA_SESSION, default
    'default'), for the monitor's session-needs-attention banner. Never
    raises: WAHA being down or unreachable is a normal, reportable state."""
    session_name = setting("PC_WAHA_SESSION", "default")
    try:
        sessions = _waha_api_get("/api/sessions?all=true")
    except Exception as exc:  # noqa: BLE001 - WAHA down is a normal state
        return {"session": session_name, "status": "UNREACHABLE", "connected": False,
                "needs_qr": False, "message": f"WAHA unreachable: {exc}"}
    match = next((s for s in sessions if isinstance(s, dict) and s.get("name") == session_name), None)
    if match is None:
        return {"session": session_name, "status": "NOT_FOUND", "connected": False,
                "needs_qr": False, "message": f"No WAHA session named '{session_name}'."}
    status = str(match.get("status") or "UNKNOWN").upper()
    connected = status in {"WORKING", "RUNNING"}
    return {
        "session": session_name,
        "status": status,
        "connected": connected,
        "needs_qr": status in {"SCAN_QR_CODE", "STARTING"},
        "message": "" if connected else f"WAHA session '{session_name}' is {status}.",
    }


ARCHIVE_DB = pc_common.DB_PATH
MONITOR_SETTINGS_FILE = pc_common.DATA_CONFIG_DIR / "monitor_settings.env"
# Written by 100-run-worker.sh on every clean completion: started/finished
# stamps, per-stage seconds and the index source.
LAST_SUMMARY_FILE = pc_common.LOG_DIR / "run_all_last_summary.env"

# Detail-summary labels that describe WHERE the opportunity is bought/delivered.
# Used to build the KPI location diagram from the saved detail pages.
LOCATION_SUMMARY_KEYS = ("Lugar", "lugar", "Provincia", "provincia", "Unidad de compra", "Dependencia")


def _read_env_style_file(path: Path) -> dict[str, str]:
    """Parse a KEY='value' env file (settings / last-summary). Never raises."""
    data: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return data
    for line in text.splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        try:
            parsed = shlex.split(raw_value, posix=True)
            data[key.strip()] = parsed[0] if parsed else ""
        except ValueError:
            data[key.strip()] = raw_value.strip().strip("'").strip('"')
    return data


def setting(key: str, default: str = "") -> str:
    """Monitor setting with the project-wide precedence: environment variable
    first, then the monitor settings file, then the default."""
    env_value = os.environ.get(key)
    if env_value is not None and str(env_value).strip():
        return str(env_value).strip()
    return _read_env_style_file(MONITOR_SETTINGS_FILE).get(key, default)


def soon_days_setting() -> int:
    """Operator's "deadline soon" window (PC_MONITOR_DEADLINE_SOON_DAYS)."""
    try:
        return max(1, int(setting("PC_MONITOR_DEADLINE_SOON_DAYS", "7")))
    except (TypeError, ValueError):
        return 7


def read_last_summary() -> dict[str, str]:
    """Parse run_all_last_summary.env. Missing file yields an empty dict."""
    if not LAST_SUMMARY_FILE.exists():
        return {}
    return _read_env_style_file(LAST_SUMMARY_FILE)


def _to_24h(text: str) -> str:
    """Rewrite any 12h 'HH:MM AM/PM' (including 'a.m.'/'p. m.' variants) inside
    ``text`` as 24h 'HH:MM' so strptime's 24h formats can parse it."""
    def repl(m):
        hh, mm = int(m.group(1)), m.group(2)
        ap = m.group(3).lower().replace(".", "").replace(" ", "")
        if ap == "pm" and hh != 12:
            hh += 12
        elif ap == "am" and hh == 12:
            hh = 0
        return f"{hh % 24:02d}:{mm}"
    return re.sub(r"(\d{1,2}):(\d{2})(?::\d{2})?\s*([AaPp]\.?\s*[Mm]\.?)", repl, text)


def compact_dt(raw: str) -> str:
    """Reformat a stored date/datetime string to 'YYYY-MM-DD_HH-MM' (or a bare
    'YYYY-MM-DD'), always 24h, for display in the monitor UIs. Falls back to
    the raw text when it cannot be parsed; blank input returns ''."""
    text = _to_24h((raw or "").strip().replace("T", " ").replace("_", " "))
    if not text:
        return ""
    for candidate in (text, text[:19], text[:16], text[:10]):
        for fmt, has_time in (("%Y-%m-%d %H:%M:%S", True), ("%Y-%m-%d %H:%M", True), ("%Y-%m-%d", False),
                              ("%d/%m/%Y %H:%M", True), ("%d/%m/%Y", False)):
            try:
                parsed = datetime.strptime(candidate, fmt)
            except ValueError:
                continue
            return parsed.strftime("%Y-%m-%d_%H-%M" if has_time else "%Y-%m-%d")
    return text


def finish_stamp_from_folder(record_folder: str) -> str:
    """Fallback DTEND for records whose finish_date_guess column is empty: read the
    close stamp from the folder leaf '(YYYY-MM-DD_HH-MM)-(numero)-(desc)'. Returns
    'YYYY-MM-DD HH:MM' (or 'YYYY-MM-DD'), or '' when the folder carries no stamp."""
    name = os.path.basename((record_folder or "").rstrip("/"))
    # Only inspect the first parenthesized token so the NUMERO (which also holds
    # digits and dashes) cannot be mistaken for the close date.
    if name.startswith("(") and ")" in name:
        name = name[1:name.index(")")]
    # Accepts both '_' and '-'/':'  between HH and MM: folders written before
    # build_record_folder_leaf() switched to '-' still use 'HH_MM'.
    match = re.search(r"(\d{4}-\d{2}-\d{2})(?:[ _T]?(\d{2})[_:-](\d{2}))?", name)
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
    archive DB for the monitors' record-index selectors. Newest first.

    Never raises: a missing, empty or locked database simply yields an empty
    list so the monitors keep working before the collector has ever run."""
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


def summarize_items_for_kpi(conn: sqlite3.Connection, limit: int = 300,
                            flt: str = "", flt_params: tuple = ()) -> dict[str, object]:
    """Summarize detail items for decision KPIs without scanning unbounded data.

    ``flt``/``flt_params`` is an optional extra WHERE fragment (same filters the
    KPI dashboard applies to the archive queries)."""
    rows = conn.execute(
        "SELECT numero, descripcion, detail_json_path, "
        "COALESCE(detail_saved_at, first_seen, '') AS saved_at FROM opportunities "
        "WHERE COALESCE(detail_json_path, '') <> '' "
        + (f"AND {flt} " if flt else "")
        + "ORDER BY COALESCE(detail_saved_at, first_seen, '') DESC, numero DESC LIMIT ?",
        (*flt_params, limit),
    ).fetchall()
    total_items = 0
    with_items = 0
    max_items = {"numero": "", "descripcion": "", "count": 0}
    keyword_counts: Counter[str] = Counter()
    location_counts: Counter[str] = Counter()
    item_name_counts: Counter[str] = Counter()
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
            name = str(item.get("descripcion") or item.get("description") or item.get("nombre") or item.get("name") or "").strip()
            if name:
                # Normalized item name so the SAME product bought repeatedly
                # surfaces as a "most frequent item" bar.
                item_name_counts[name.lower()[:48]] += 1
            text = " ".join(str(item.get(k, "")) for k in ("descripcion", "description", "nombre", "name", "codigo", "code"))
            for word in re.findall(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ0-9]{4,}", text.lower()):
                if not word.isdigit():
                    keyword_counts[word] += 1
            if len(sample_items) < 10:
                # Rows come newest-first, so these are the LATEST parsed items.
                qty = str(item.get("cantidad") or item.get("qty") or item.get("quantity") or "")
                sample_items.append({
                    "numero": str(row["numero"] or ""),
                    "descripcion": name[:90],
                    "cantidad": qty,
                    "saved_at": str(row["saved_at"] or "")[:16],
                })
    avg_items = round(total_items / with_items, 1) if with_items else 0
    return {
        "sampled_records": len(rows),
        "records_with_items": with_items,
        "total_items": total_items,
        "avg_items_per_record": avg_items,
        "max_items_record": max_items,
        "top_item_keywords": [{"label": k, "count": v} for k, v in keyword_counts.most_common(12)],
        "top_items": [{"label": k, "count": v} for k, v in item_name_counts.most_common(10)],
        "top_locations": [{"label": k, "count": v} for k, v in location_counts.most_common(10)],
        "sample_items": sample_items,
    }


def db_review_stats(days: int = 0, grupo: str = "", entidad: str = "") -> dict[str, object]:
    """Aggregate counts for the monitors' KPI dashboards and DB-review panels.

    Superset payload consumed by the Tk monitor, the web monitor's
    ``/api/db-stats`` endpoint and the ``pcc kpi`` CLI. Never raises; a
    missing/locked DB yields zeros so panels render before the first run.

    ``days``/``grupo``/``entidad`` are the KPI dashboard filters: 0/blank means
    no filter; otherwise every aggregate is restricted to records first seen in
    the window and/or matching the group/entity."""
    soon_days = soon_days_setting()
    empty = {
        "db_exists": ARCHIVE_DB.exists(), "total": 0, "saved": 0, "pending": 0,
        "failed": 0, "notified": 0, "notify_backlog": 0, "detail_notify_backlog": 0, "needs_deadline": 0,
        "new_today": 0, "closing_soon": 0, "soon_days": soon_days, "abiertas": 0, "programadas": 0,
        "alerts_failed": 0, "new_records": 0, "existing_records": 0,
        "with_detail_json": 0, "item_analysis": {}, "groups": [], "entities": [], "dependencias": [],
        "recent": [], "completed_recent": [], "columns": [], "status_breakdown": [], "monthly_trend": [],
        "daily_intake": [], "filters": {"days": days, "grupo": grupo, "entidad": entidad},
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

        # Optional dashboard filters applied to EVERY aggregate below, so the
        # cards, diagrams and item analysis all answer for the same slice.
        flt_conditions: list[str] = []
        flt_params: list[str] = []
        if days and int(days) > 0:
            flt_conditions.append(
                "REPLACE(REPLACE(substr(COALESCE(NULLIF(first_seen, ''), detail_saved_at, ''), 1, 10), '_', '-'), 'T', '') >= date('now', ?)")
            flt_params.append(f"-{int(days)} day")
        if grupo:
            flt_conditions.append("COALESCE(grupo, '') = ?")
            flt_params.append(grupo)
        if entidad and "entidad" in columns:
            flt_conditions.append("COALESCE(entidad, '') = ?")
            flt_params.append(entidad)
        flt = " AND ".join(flt_conditions)
        flt_where = f" WHERE {flt}" if flt else ""
        flt_and = f" AND {flt}" if flt else ""

        def count(where: str = "") -> int:
            clauses = [c for c in (where, flt) if c]
            sql = "SELECT COUNT(*) FROM opportunities" + ((" WHERE " + " AND ".join(clauses)) if clauses else "")
            return int(conn.execute(sql, flt_params if flt else []).fetchone()[0])

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
                "SELECT detail_status, COUNT(*) AS c FROM opportunities" + flt_where
                + " GROUP BY detail_status ORDER BY c DESC",
                flt_params,
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

        # "Closing soon" window follows the operator's deadline-soon setting so
        # the KPI card, the record-list legend and the repair tools agree.
        first_seen_day = "REPLACE(REPLACE(substr(COALESCE(NULLIF(first_seen, ''), detail_saved_at, ''), 1, 10), '_', '-'), 'T', '')"

        return {
            "db_exists": True,
            "total": count(),
            "saved": count("detail_status = 'saved'"),
            "pending": count("detail_status = 'pending'"),
            "failed": count("detail_status = 'failed'"),
            "new_today": count(f"{first_seen_day} = date('now')"),
            "closing_soon": count(
                "substr(COALESCE(finish_date_guess, ''), 1, 10) >= date('now') "
                f"AND substr(COALESCE(finish_date_guess, ''), 1, 10) <= date('now', '+{soon_days} day')"),
            "soon_days": soon_days,
            "abiertas": count("COALESCE(grupo, '') = 'Abiertas'"),
            "programadas": count("COALESCE(grupo, '') = 'Programadas'"),
            # WhatsApp delivery tracking (audit Phase 3): records whose LAST
            # send attempt failed. 0 until 020-notify-whatsapp.py has run with
            # the notify_error column present.
            "alerts_failed": count("COALESCE(notify_error, '') <> ''") if "notify_error" in columns else 0,
            "new_records": count("detail_status = 'pending'"),
            "existing_records": count("detail_status = 'saved'"),
            "notified": count("notified_at IS NOT NULL") if has_notified else 0,
            "notify_backlog": count("notified_at IS NULL") if has_notified else 0,
            "detail_notify_backlog": count(
                "detail_status = 'saved' AND notified_at IS NOT NULL AND detail_notified_at IS NULL"
            ) if has_notified and "detail_notified_at" in columns else 0,
            "needs_deadline": count(needs_deadline_where),
            "with_detail_json": count("COALESCE(detail_json_path, '') <> ''"),
            "item_analysis": summarize_items_for_kpi(conn, flt=flt, flt_params=tuple(flt_params)),
            "recent": [
                {"numero": str(r["numero"] or ""), "descripcion": str(r["descripcion"] or r["short_description"] or ""), "detail_status": str(r["detail_status"] or "")}
                for r in conn.execute(
                    "SELECT numero, descripcion, short_description, detail_status FROM opportunities"
                    + flt_where +
                    " ORDER BY COALESCE(detail_saved_at, first_seen, '') DESC, numero DESC LIMIT 12",
                    flt_params,
                ).fetchall()
            ],
            "completed_recent": [
                {"numero": str(r["numero"] or ""), "finish_date_guess": str(r["finish_date_guess"] or "") or finish_stamp_from_folder(str(r["record_folder"] or "")) or finish_stamp_from_detail_json(str(r["detail_json_path"] or "")), "descripcion": str(r["descripcion"] or r["short_description"] or "")}
                for r in conn.execute(
                    "SELECT numero, finish_date_guess, record_folder, detail_json_path, descripcion, short_description FROM opportunities "
                    "WHERE detail_status = 'saved'" + flt_and +
                    " ORDER BY COALESCE(detail_saved_at, first_seen, '') DESC, numero DESC LIMIT 5",
                    flt_params,
                ).fetchall()
            ],
            "columns": column_details,
            "status_breakdown": status_rows,
            "monthly_trend": [
                {"label": str(r["period"] or "unknown"), "count": int(r["c"])}
                for r in conn.execute(
                    "SELECT substr(COALESCE(NULLIF(first_seen, ''), detail_saved_at, finish_date_guess, 'unknown'), 1, 7) AS period, COUNT(*) AS c "
                    "FROM opportunities" + flt_where + " GROUP BY period ORDER BY period DESC LIMIT 12",
                    flt_params,
                ).fetchall()
            ],
            # Records first seen per day, newest first — the "how alive is the
            # intake" diagram for the KPI dashboard.
            "daily_intake": [
                {"label": str(r["day"]), "count": int(r["c"])}
                for r in conn.execute(
                    "SELECT REPLACE(REPLACE(substr(COALESCE(NULLIF(first_seen, ''), detail_saved_at, ''), 1, 10), '_', '-'), 'T', '') AS day, COUNT(*) AS c "
                    "FROM opportunities" + flt_where + " GROUP BY day HAVING day <> '' ORDER BY day DESC LIMIT 14",
                    flt_params,
                ).fetchall()
            ],
            "groups": [
                {"grupo": str(r["grupo"] or "(sin grupo)"), "count": int(r["c"])}
                for r in conn.execute(
                    "SELECT grupo, COUNT(*) AS c FROM opportunities"
                    + flt_where + " GROUP BY grupo ORDER BY c DESC",
                    flt_params,
                ).fetchall()
            ],
            # WHO buys and WHERE: contracting entities and their dependencies,
            # for the KPI location/buyer diagrams.
            "entities": [
                {"label": str(r["entidad"] or "(sin entidad)"), "count": int(r["c"])}
                for r in conn.execute(
                    "SELECT entidad, COUNT(*) AS c FROM opportunities"
                    + flt_where + " GROUP BY entidad ORDER BY c DESC LIMIT 12",
                    flt_params,
                ).fetchall()
            ] if "entidad" in columns else [],
            "dependencias": [
                {"label": str(r["dependencia"] or "(sin dependencia)"), "count": int(r["c"])}
                for r in conn.execute(
                    "SELECT dependencia, COUNT(*) AS c FROM opportunities"
                    + flt_where + " GROUP BY dependencia ORDER BY c DESC LIMIT 12",
                    flt_params,
                ).fetchall()
            ] if "dependencia" in columns else [],
            "filters": {"days": int(days or 0), "grupo": grupo, "entidad": entidad},
        }
    except sqlite3.Error:
        return empty
    finally:
        conn.close()


def stats_to_csv(stats: dict[str, object]) -> str:
    """Flatten the KPI payload into a small CSV for the web export button:
    one metric per row, then the group/entity/status breakdowns."""
    import csv
    import io
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["section", "label", "value"])
    filters = stats.get("filters") or {}
    writer.writerow(["filters", "days", filters.get("days", 0)])
    writer.writerow(["filters", "grupo", filters.get("grupo", "")])
    writer.writerow(["filters", "entidad", filters.get("entidad", "")])
    for key in ("total", "new_today", "closing_soon", "abiertas", "programadas", "saved",
                "pending", "failed", "notified", "alerts_failed", "notify_backlog",
                "detail_notify_backlog", "needs_deadline", "with_detail_json"):
        writer.writerow(["kpi", key, stats.get(key, 0)])
    for row in stats.get("status_breakdown") or []:
        writer.writerow(["detail_status", row.get("status", ""), row.get("count", 0)])
    for row in stats.get("groups") or []:
        writer.writerow(["group", row.get("grupo", ""), row.get("count", 0)])
    for row in stats.get("entities") or []:
        writer.writerow(["entity", row.get("label", ""), row.get("count", 0)])
    for row in stats.get("daily_intake") or []:
        writer.writerow(["daily_intake", row.get("label", ""), row.get("count", 0)])
    for row in stats.get("monthly_trend") or []:
        writer.writerow(["monthly_trend", row.get("label", ""), row.get("count", 0)])
    items = stats.get("item_analysis") or {}
    for row in items.get("top_items") or []:
        writer.writerow(["top_item", row.get("label", ""), row.get("count", 0)])
    for row in items.get("top_locations") or []:
        writer.writerow(["location", row.get("label", ""), row.get("count", 0)])
    return out.getvalue()


def _bar_rows(title: str, rows: list[tuple[str, int]], width: int = 28) -> None:
    rows = [(str(label), int(count)) for label, count in rows if str(label).strip()][:8]
    if not rows:
        return
    print()
    print(title)
    top = max(count for _, count in rows) or 1
    for label, count in rows:
        bar = "█" * max(1, round(width * count / top))
        print(f"  {label[:36]:<36} {bar} {count}")


def _print_text_report(stats: dict[str, object]) -> None:
    """Terminal KPI report — same numbers as the monitors' KPIs tab."""
    filters = stats.get("filters") or {}
    items = stats.get("item_analysis") or {}
    total = int(stats.get("total") or 0)
    saved = int(stats.get("saved") or 0)
    closure = round((saved / total) * 100) if total else 0
    print("Index + Detail KPI summary")
    active = []
    if int(filters.get("days") or 0) > 0:
        active.append(f"last {filters['days']} days")
    if filters.get("grupo"):
        active.append(f"group {filters['grupo']}")
    if filters.get("entidad"):
        active.append(f"entity {filters['entidad']}")
    if active:
        print("Filters:                           " + " · ".join(active))
    print(f"Index archive total:               {total}")
    print(f"New today:                         {stats.get('new_today', 0)}")
    print(f"Closing within {stats.get('soon_days', 7)} days:             {stats.get('closing_soon', 0)}")
    print(f"Abiertas / Programadas:            {stats.get('abiertas', 0)} / {stats.get('programadas', 0)}")
    print(f"Index records awaiting alert:      {stats.get('notify_backlog', 0)}")
    print(f"Details saved:                     {saved}")
    print(f"Details pending:                   {stats.get('pending', 0)}")
    print(f"Details failed:                    {stats.get('failed', 0)}")
    print(f"Detail JSON coverage:              {stats.get('with_detail_json', 0)}/{total} ({closure}% saved)")
    print(f"WAHA index alerts sent:            {stats.get('notified', 0)}")
    print(f"WAHA failed alerts (last attempt): {stats.get('alerts_failed', 0)}")
    print(f"WAHA detail follow-up backlog:     {stats.get('detail_notify_backlog', 0)}")
    if items:
        print(f"Item lines parsed (latest {items.get('sampled_records', 0)}):     "
              f"{items.get('total_items', 0)} across {items.get('records_with_items', 0)} records; "
              f"avg={items.get('avg_items_per_record', 0)}")
        biggest = items.get("max_items_record") or {}
        print(f"Largest item record:               {biggest.get('numero') or '-'} ({biggest.get('count', 0)} items)")
        keywords = " · ".join(f"{r['label']}={r['count']}" for r in (items.get("top_item_keywords") or [])[:8])
        if keywords:
            print("Top item keywords:                 " + keywords)
    statuses = " · ".join(f"{r['status']}={r['count']}" for r in stats.get("status_breakdown") or [])
    if statuses:
        print("Detail status mix:                 " + statuses)
    trend = " · ".join(f"{r['label']}={r['count']}" for r in (stats.get("monthly_trend") or [])[:6])
    if trend:
        print("Recent index/detail trend:         " + trend)
    sample = (items.get("sample_items") or [])[:8]
    if sample:
        print("Latest parsed items:")
        for it in sample:
            qty = f" · qty {it['cantidad']}" if it.get("cantidad") else ""
            print(f"  - {it.get('numero')}: {it.get('descripcion')}{qty}")
    last = read_last_summary()
    if last.get("FINISHED_AT"):
        print()
        print(f"Last run: finished {compact_dt(last['FINISHED_AT'])} · duration {last.get('TOTAL_TEXT') or last.get('TOTAL_SECONDS', '?')}"
              f" · index source: {last.get('INDEX_SOURCE') or 'crawler'}")
    _bar_rows("Index groups:", [(r["grupo"], r["count"]) for r in stats.get("groups") or []])
    _bar_rows("Top contracting entities:", [(r["label"], r["count"]) for r in stats.get("entities") or []])
    _bar_rows("Locations / buying units (from details):",
              [(r["label"], r["count"]) for r in items.get("top_locations") or []])
    _bar_rows("Most frequent items:", [(r["label"], r["count"]) for r in items.get("top_items") or []])
    _bar_rows("Daily intake (last 14 days):", [(r["label"], r["count"]) for r in stats.get("daily_intake") or []])


def main() -> int:
    parser = argparse.ArgumentParser(description="PanamaCompra KPI summary (shared engine used by the monitors and pcc kpi).")
    parser.add_argument("--days", type=int, default=0, help="Restrict to records first seen in the last N days (0 = all time).")
    parser.add_argument("--grupo", "--group", default="", help="Restrict to one index group (e.g. Abiertas).")
    parser.add_argument("--entidad", "--entity", default="", help="Restrict to one contracting entity.")
    parser.add_argument("--json", action="store_true", help="Emit the full payload as JSON instead of the text report.")
    parser.add_argument("--csv", action="store_true", help="Emit the flat CSV export used by the web monitor's export button.")
    args = parser.parse_args()
    stats = db_review_stats(days=max(0, args.days), grupo=args.grupo.strip(), entidad=args.entidad.strip())
    if args.json:
        print(json.dumps(stats, ensure_ascii=False, indent=2))
    elif args.csv:
        sys.stdout.write(stats_to_csv(stats))
    else:
        _print_text_report(stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
