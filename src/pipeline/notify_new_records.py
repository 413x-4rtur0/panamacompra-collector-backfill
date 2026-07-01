#!/usr/bin/env python3
"""Send rich PanamaCompra "what is new" notifications to WhatsApp via WAHA.

The run-all worker calls this script right after the index step, BEFORE detail
downloads, so subscribers hear about new opportunities immediately instead of
after the (potentially hours-long) download phase. ``--announce`` sends one rich
"🔔 Nueva Oportunidad" message per new record with monitor-visible progress
(item details show as pending when the detail page is not downloaded yet);
``--idle`` sends one "⚪ Sin nuevas entradas" status; ``--flush`` retries
records that still have no ``notified_at`` timestamp; ``--sync-snapshots
--since TS`` is called by the worker after the downloads finish so the freshly
downloaded items do not fire a duplicate "items updated" message on the next
run.

Design notes
------------
* Dependency-free: reuses pc_common (stdlib only) for the archive DB and
  waha_client for the WAHA HTTP send + enable/skip logic.
* It NEVER blocks a collector run: every failure is caught and logged.
* First-use baseline: ``ensure_baseline`` marks the records that already existed
  when WAHA was first enabled as already-announced, so an existing archive does
  not produce a burst of messages. Only records saved AFTER that are announced.
* Optional keyword filter: one keyword per line in data/config/waha_keywords.txt.
  When present, only records whose title/description/entity match a keyword are
  announced (matched keywords are listed). When empty, every new record is
  announced and the match line reads "Sin filtro (todas las entradas)".
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SRC_DIR))
sys.path.insert(0, str(_SRC_DIR / "notify"))
import common as pc_common
import waha_client as waha

CONFIG_DIR = pc_common.DATA_CONFIG_DIR
CALENDAR_EXPORT_DIR = pc_common.CALENDAR_EXPORT_DIR
KEYWORDS_PATH = CONFIG_DIR / "waha_keywords.txt"
BASELINE_MARKER = CONFIG_DIR / "waha_notify_initialized"
SETTINGS_PATH = CONFIG_DIR / "monitor_settings.env"

DASH = "—"


def _load_settings_file() -> dict[str, str]:
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


_SETTINGS_FILE = _load_settings_file()


def cfg(name: str, default: str) -> str:
    """Resolve a value: environment variable first, then the monitor settings
    file (data/config/monitor_settings.env), then the default. This lets the
    monitor's Settings panel control the notifier without env changes."""
    if name in os.environ:
        return os.environ[name]
    return _SETTINGS_FILE.get(name, default)


def cfg_bool(name: str, default: bool = False) -> bool:
    return cfg(name, "1" if default else "0").strip().lower() in {"1", "true", "yes", "on"}


def cfg_int(name: str):
    raw = cfg(name, "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


SOURCE_NAME = cfg("PC_WAHA_SOURCE", "Panamá Compra")


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def fmt_dt(value, *, with_time: bool = True) -> str:
    """Normalize a stored date/datetime string to 'YYYY-MM-DD HH:MM' (or a bare
    date when no time is present), so WhatsApp messages show consistent dates and
    times. Falls back to the cleaned original when it cannot be parsed."""
    raw = clean_field(value)
    if raw == DASH:
        return DASH
    text = raw.replace("T", " ").replace("_", " ").strip()
    formats = (
        ("%Y-%m-%d %H:%M:%S", True),
        ("%Y-%m-%d %H:%M", True),
        ("%Y-%m-%d", False),
        ("%d/%m/%Y %H:%M", True),
        ("%d/%m/%Y", False),
    )
    for candidate in (text, text[:19], text[:16], text[:10]):
        for fmt, has_time in formats:
            try:
                parsed = datetime.strptime(candidate, fmt)
            except ValueError:
                continue
            return parsed.strftime("%Y-%m-%d %H:%M" if (with_time and has_time) else "%Y-%m-%d")
    return raw


def parse_finish_date(row):
    """The record deadline (finish_date_guess) as a datetime, or None."""
    raw = (row["finish_date_guess"] or "").strip().replace("_", " ").replace("T", " ")
    if not raw:
        return None
    for candidate in (raw, raw[:19], raw[:16], raw[:10]):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(candidate, fmt)
            except ValueError:
                continue
    return None


def deadline_decision(row) -> str:
    """Whether a record's deadline lets it be announced now.

    Returns 'ok', 'expired' (deadline already passed → skip and never recheck) or
    'too_far' (deadline beyond the configured window → skip now, recheck later as
    time moves it into range). Controlled by PC_NOTIFY_SKIP_EXPIRED (0/1) and
    PC_NOTIFY_WITHIN_DAYS (int); both default off so every record is announced
    unless the operator opts in (env or monitor_settings.env)."""
    skip_expired = cfg_bool("PC_NOTIFY_SKIP_EXPIRED", False)
    within_days = cfg_int("PC_NOTIFY_WITHIN_DAYS")
    if not skip_expired and within_days is None:
        return "ok"
    deadline = parse_finish_date(row)
    if deadline is None:
        return "ok"  # no detectable deadline → never suppress on date grounds
    now = datetime.now()
    if skip_expired and deadline < now:
        return "expired"
    if within_days is not None and deadline > now + timedelta(days=within_days):
        return "too_far"
    return "ok"


def waha_enabled() -> bool:
    return waha.env_bool("PC_WAHA_ENABLED", False)


def waha_destination() -> str:
    chat_id = os.environ.get("PC_WAHA_CHAT_ID", "").strip()
    if chat_id:
        return chat_id
    path = CONFIG_DIR / "waha_chat_id.txt"
    if path.exists():
        return path.read_text(encoding="utf-8", errors="replace").strip()
    return ""


def load_keywords() -> list[str]:
    if not KEYWORDS_PATH.exists():
        return []
    keywords = []
    for line in KEYWORDS_PATH.read_text(encoding="utf-8", errors="replace").splitlines():
        token = line.strip()
        if token and not token.startswith("#"):
            keywords.append(token)
    return keywords


def matched_keywords(haystack: str, keywords: list[str]) -> list[str]:
    normalized = pc_common.strip_accents(haystack).lower()
    matches = []
    for keyword in keywords:
        if pc_common.strip_accents(keyword).lower() in normalized:
            matches.append(keyword)
    return matches


def load_detail_data(detail_json_path: str | None) -> dict:
    if not detail_json_path:
        return {}
    path = Path(detail_json_path)
    if not path.is_absolute():
        path = pc_common.APP_ROOT / path
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def load_split_detail_section(detail_data: dict, detail_json_path: str | None, section: str):
    descriptor = (detail_data.get("detail_sections") or {}).get(section)
    if not isinstance(descriptor, dict) or not descriptor.get("file") or not detail_json_path:
        return None
    base = Path(detail_json_path)
    if not base.is_absolute():
        base = pc_common.APP_ROOT / base
    path = base.parent / "detail_sections" / str(descriptor["file"])
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(doc, dict) and "data" in doc:
        return doc.get("data")
    return doc


def load_detail_summary(detail_json_path: str | None) -> dict:
    data = load_detail_data(detail_json_path)
    summary = data.get("summary")
    if not isinstance(summary, dict):
        summary = load_split_detail_section(data, detail_json_path, "summary")
    return summary if isinstance(summary, dict) else {}


def load_detail_items(detail_json_path: str | None) -> list[dict]:
    data = load_detail_data(detail_json_path)
    items = data.get("items")
    if not isinstance(items, list):
        items = load_split_detail_section(data, detail_json_path, "items")
    return items if isinstance(items, list) else []


def load_detail_calendar(detail_json_path: str | None) -> dict:
    data = load_detail_data(detail_json_path)
    calendar = data.get("calendar")
    if not isinstance(calendar, dict):
        calendar = load_split_detail_section(data, detail_json_path, "calendar")
    return calendar if isinstance(calendar, dict) else {}

def stable_hash(value) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def status_value(row) -> str:
    return clean_field(row["estado"] or row["grupo"])


def items_hash_for(row) -> str:
    return stable_hash(load_detail_items(row["detail_json_path"]))


def signature_for(row) -> tuple[str, str, str]:
    status = status_value(row)
    items_hash = items_hash_for(row)
    signature = stable_hash({"status": status, "items_hash": items_hash})
    return status, items_hash, signature


def is_cancelled_status(value: str) -> bool:
    normalized = pc_common.strip_accents(value or "").lower()
    return any(token in normalized for token in ("cancel", "anulad", "desiert"))


def clean_field(value) -> str:
    text = "" if value is None else str(value).strip()
    return text or DASH


def format_items(items: list[dict], *, limit: int = 10) -> str:
    if not items:
        return f"📦 *Items (0):*\n• {DASH}"
    lines = [f"📦 *Items ({len(items)}):*"]
    for index, item in enumerate(items[:limit], start=1):
        if not isinstance(item, dict):
            item = {"descripcion": item}
        desc = clean_field(item.get("descripcion") or item.get("description") or item.get("detalle") or item.get("clasificacion"))
        qty = clean_field(item.get("cantidad") or item.get("qty"))
        unit = clean_field(item.get("unidad") or item.get("unidad_medida") or item.get("unit"))
        bits = [f"• Item {index}: {desc}"]
        if qty != DASH:
            bits.append(f"Qty: {qty}")
        if unit != DASH:
            bits.append(f"Unit: {unit}")
        lines.append(" - ".join(bits))
    if len(items) > limit:
        lines.append(f"... [+ {len(items) - limit} más]")
    return "\n".join(lines)


def record_location(summary: dict) -> str:
    parts = [
        clean_field(summary.get("provincia_de_entrega")),
        clean_field(summary.get("lugar_de_entrega")),
    ]
    parts = [part for part in parts if part != DASH]
    return ", ".join(parts) if parts else DASH


def date_range(row, summary: dict) -> str:
    start = fmt_dt(row["fecha"] or summary.get("fecha_de_publicacion"))
    end = fmt_dt(row["finish_date_guess"] or summary.get("fecha_y_hora_limite_de_recepcion"))
    return f"{start} al {end}" if start != DASH or end != DASH else DASH


def build_record_message(row, summary: dict, *, variant: str, previous_status: str | None = None, match_line: str | None = None) -> str:
    detail_pending = row["detail_status"] != "saved"
    items = load_detail_items(row["detail_json_path"])
    status = status_value(row)
    title = clean_field(row["descripcion"] or row["short_description"] or summary.get("descripcion"))
    location = record_location(summary)
    url = clean_field(row["link"] or summary.get("enlace_publico") or summary.get("enlace_interno"))
    created = fmt_dt(row["first_seen"] or row["fecha"])
    downloaded = fmt_dt(row["detail_saved_at"]) if row["detail_saved_at"] else ("⏳ En descarga" if detail_pending else fmt_dt(now_str()))
    numero = clean_field(row["numero"])

    if variant == "new":
        heading = f"🔔 *Nueva Oportunidad - {SOURCE_NAME}*"
        status_line = f"📊 *Estado:* {status}"
    elif variant == "cancelled":
        heading = f"❌ *Oportunidad Cancelada - {SOURCE_NAME}*"
        status_line = f"📊 *Estado:* {status.upper()}"
    elif variant == "status":
        heading = f"⚠️ *Cambio de Estado - {SOURCE_NAME}*"
        status_line = f"📊 *Estado:* [ANTERIOR: {clean_field(previous_status)}] ➡️ [ACTUAL: {status}]"
    elif variant == "items":
        heading = f"🔄 *Actualización de Items - {SOURCE_NAME}*"
        status_line = f"📊 *Estado:* {status} (Sin cambios)"
    else:
        heading = f"🔔 *Oportunidad - {SOURCE_NAME}*"
        status_line = f"📊 *Estado:* {status}"

    parts = [
        heading,
        "",
        status_line,
        f"🔢 *Número:* {numero}",
        f"📝 *Descripción:* {title}",
        f"📍 *Ubicación:* {location}",
        f"📅 *Rango Fechas:* {date_range(row, summary)}",
        "",
        "📦 *Items:* ⏳ pendiente — los detalles se descargan después de este aviso" if detail_pending else format_items(items),
    ]
    if match_line:
        parts.extend(["", f"🔎 *Coincidencia:* {match_line}"])
    parts.extend([
        "",
        f"🔗 *Enlace:* {url}",
        f"🕒 *Creado:* {created}",
        f"⬇️ *Descargado:* {downloaded}",
    ])
    return "\n".join(parts)


def build_opportunity_message(row, summary: dict, match_line: str) -> str:
    return build_record_message(row, summary, variant="new", match_line=match_line)


def build_items_changed_message(row, summary: dict) -> str:
    return build_record_message(row, summary, variant="items")

def build_empty_message(records_checked: int) -> str:
    return (
        "⚪ Sin nuevas entradas\n"
        "\n"
        f"📌 Fuente: {SOURCE_NAME}\n"
        f"🕒 Revisión: {now_str()}\n"
        f"📊 Registros revisados: {records_checked}\n"
        "✅ Monitor activo"
    )


# Human-readable label for a pending_status_change code stored by the index step.
STATUS_CHANGE_LABELS = {
    "abierta": "Programada → Abierta",
    "cancelada": "Programada → Cancelada",  # planned future transition
}


def build_status_change_message(row, summary: dict, change_code: str) -> str:
    previous_status = row["last_notified_status"] or STATUS_CHANGE_LABELS.get(change_code, change_code or DASH)
    variant = "cancelled" if is_cancelled_status(status_value(row)) else "status"
    return build_record_message(row, summary, variant=variant, previous_status=previous_status)

def export_record_calendar(conn, row) -> str:
    calendar = load_detail_calendar(row["detail_json_path"])
    if not calendar:
        return ""
    stamp = (row["detail_saved_at"] or now_str())[:7].replace("-", "/")
    out_dir = CALENDAR_EXPORT_DIR / stamp
    out_path = out_dir / f"{pc_common.safe_name(row['numero'])}.ics"
    try:
        pc_common.write_calendar_ics(out_path, calendar)
        conn.execute("UPDATE opportunities SET last_calendar_export_path = ? WHERE numero = ?", (str(out_path), row["numero"]))
        conn.commit()
        return str(out_path)
    except Exception as exc:  # noqa: BLE001 - do not duplicate WhatsApp sends for ICS failures
        print(f"WAHA calendar export failed for {row['numero']}: {exc}", file=sys.stderr)
        return ""


def mark_notified(conn, numero: str) -> None:
    row = fetch_row(conn, numero)
    status, items_hash, signature = signature_for(row) if row is not None else ("", "", "")
    conn.execute(
        "UPDATE opportunities SET notified_at = ?, last_notified_status = ?, "
        "last_notified_items_hash = ?, last_notified_signature = ? WHERE numero = ?",
        (now_str(), status, items_hash, signature, numero),
    )
    conn.commit()


def mark_snapshot(conn, numero: str) -> None:
    row = fetch_row(conn, numero)
    if row is None:
        return
    status, items_hash, signature = signature_for(row)
    conn.execute(
        "UPDATE opportunities SET last_notified_status = ?, last_notified_items_hash = ?, "
        "last_notified_signature = ? WHERE numero = ?",
        (status, items_hash, signature, numero),
    )
    conn.commit()


def fetch_row(conn, numero: str):
    return conn.execute("SELECT * FROM opportunities WHERE numero = ?", (numero,)).fetchone()


def match_line_for(row, summary: dict, keywords: list[str]) -> str | None:
    """Return the '🔎 Coincidencia' line, or None when a keyword filter is active
    and this record matched nothing (so it should not be announced)."""
    if not keywords:
        return "Sin filtro (todas las entradas)"
    haystack = " ".join(
        str(value)
        for value in (
            row["descripcion"],
            row["short_description"],
            row["entidad"],
            summary.get("descripcion"),
        )
        if value
    )
    matches = matched_keywords(haystack, keywords)
    return ", ".join(matches) if matches else None


def send_text(event: str, text: str) -> bool:
    """Send through WAHA respecting the per-event enable list. Returns True only
    when the message was actually sent."""
    if not waha.enabled_for_event(event):
        print(f"WAHA notification skipped: event {event!r} is not enabled.")
        return False
    try:
        waha.send_text(text)
        return True
    except Exception as exc:  # noqa: BLE001 - never let a notify failure stop a run
        print(f"WAHA notification failed: {exc}", file=sys.stderr)
        return False


def ensure_baseline(conn) -> bool:
    """Mark the records that already existed when WAHA was first enabled as
    already-announced. Returns True if the baseline was established on this call.

    Called once at the start of a run (right after the index step), so only
    records the index finds afterwards are announced. Covers every existing
    record regardless of detail status: the announce step now runs before the
    detail downloads, so old pending records must not look "new" either."""
    if BASELINE_MARKER.exists():
        return False
    if not (waha_enabled() and waha_destination()):
        # Wait until WAHA is usable so the baseline reflects the real "before"
        # state the first time messages can actually be sent.
        return False
    existing_rows = conn.execute(
        "SELECT numero FROM opportunities WHERE notified_at IS NULL"
    ).fetchall()
    conn.execute(
        "UPDATE opportunities SET notified_at = ? WHERE notified_at IS NULL",
        (now_str(),),
    )
    conn.commit()
    for row in existing_rows:
        mark_snapshot(conn, row["numero"])
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    BASELINE_MARKER.write_text(now_str() + "\n", encoding="utf-8")
    print("WAHA baseline established; existing records will not be announced.")
    return True


def notify_manual_record(conn, numero: str, *, force: bool = False) -> bool:
    """Send a user-requested WhatsApp notification for one record."""
    try:
        if not (waha_enabled() and waha_destination()):
            return False
        row = fetch_row(conn, numero)
        if row is None or row["detail_status"] != "saved":
            print(f"WAHA manual notify skipped for {numero}: record missing or detail not saved.")
            return False
        if row["notified_at"] and not force:
            print(f"WAHA manual notify skipped for {numero}: already notified (use --force).")
            return False
        summary = load_detail_summary(row["detail_json_path"])
        text = build_record_message(row, summary, variant="manual", match_line="Enviado manualmente desde el monitor")
        if not send_text("new", text):
            return False
        export_record_calendar(conn, row)
        mark_notified(conn, numero)
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"WAHA manual notify error for {numero}: {exc}", file=sys.stderr)
        return False

def notify_saved_record(conn, numero: str) -> bool:
    """Announce a single new record. Runs right after the index step, so the
    record's detail page may not be downloaded yet (the message then shows the
    items as pending). Idempotent: a record is sent at most once (guarded by
    notified_at). Returns True if a message was sent. Never raises, so
    messaging failures do not break collection runs."""
    try:
        if not (waha_enabled() and waha_destination()):
            return False
        if not BASELINE_MARKER.exists():
            # Baseline not set up yet (ensure_baseline should run first). Skip to
            # avoid mistaking a pre-existing record for a new one.
            return False
        row = fetch_row(conn, numero)
        if row is None or row["notified_at"]:
            return False
        decision = deadline_decision(row)
        if decision == "expired":
            # Past its deadline: never actionable, so mark done and stay quiet.
            mark_notified(conn, numero)
            return False
        if decision == "too_far":
            # Beyond the announce window for now; leave it unmarked so a later
            # run re-checks it once the deadline moves into range.
            return False
        summary = load_detail_summary(row["detail_json_path"])
        match_line = match_line_for(row, summary, load_keywords())
        if match_line is None:
            # Filtered out by keywords: remember it so it is not rechecked.
            mark_notified(conn, numero)
            return False
        if not send_text("new", build_opportunity_message(row, summary, match_line)):
            # Leave notified_at unset so a later --flush retries it.
            return False
        export_record_calendar(conn, row)
        mark_notified(conn, numero)
        return True
    except Exception as exc:  # noqa: BLE001 - defensive: never break a download
        print(f"WAHA record notify error for {numero}: {exc}", file=sys.stderr)
        return False


def clear_status_change(conn, numero: str) -> None:
    conn.execute("UPDATE opportunities SET pending_status_change = NULL WHERE numero = ?", (numero,))
    conn.commit()


def notify_status_change(conn, numero: str) -> bool:
    """Announce a single record's status transition (e.g. Programada → Abierta).
    Idempotent: clears the pending flag whether or not a message is sent. Returns
    True only when a message was actually sent. Never raises."""
    try:
        if not (waha_enabled() and waha_destination()):
            return False
        row = fetch_row(conn, numero)
        if row is None or not row["pending_status_change"]:
            return False
        summary = load_detail_summary(row["detail_json_path"])
        # Respect the same keyword filter as new records.
        if match_line_for(row, summary, load_keywords()) is None:
            clear_status_change(conn, numero)
            return False
        sent = send_text("update", build_status_change_message(row, summary, row["pending_status_change"]))
        if sent:
            export_record_calendar(conn, row)
            mark_snapshot(conn, numero)
            clear_status_change(conn, numero)
        return sent
    except Exception as exc:  # noqa: BLE001 - never break a run
        print(f"WAHA status-change notify error for {numero}: {exc}", file=sys.stderr)
        return False


def notify_detected_status_change(conn, numero: str) -> bool:
    """Announce a status/cancellation change detected from the saved snapshot.

    This catches transitions that were not explicitly flagged by the index step.
    """
    try:
        if not (waha_enabled() and waha_destination()):
            return False
        row = fetch_row(conn, numero)
        if row is None or row["detail_status"] != "saved" or not row["notified_at"]:
            return False
        current_status, _items_hash, current_signature = signature_for(row)
        if row["last_notified_signature"] == current_signature or row["last_notified_status"] == current_status:
            return False
        summary = load_detail_summary(row["detail_json_path"])
        if match_line_for(row, summary, load_keywords()) is None:
            mark_snapshot(conn, numero)
            return False
        sent = send_text("update", build_record_message(
            row,
            summary,
            variant="cancelled" if is_cancelled_status(current_status) else "status",
            previous_status=row["last_notified_status"],
        ))
        if sent:
            export_record_calendar(conn, row)
            mark_snapshot(conn, numero)
        return sent
    except Exception as exc:  # noqa: BLE001 - never break a run
        print(f"WAHA detected-status notify error for {numero}: {exc}", file=sys.stderr)
        return False

def notify_items_change(conn, numero: str) -> bool:
    """Announce item-list/content changes after a record was previously sent."""
    try:
        if not (waha_enabled() and waha_destination()):
            return False
        row = fetch_row(conn, numero)
        if row is None or row["detail_status"] != "saved" or not row["notified_at"]:
            return False
        current_status, current_items_hash, current_signature = signature_for(row)
        if row["last_notified_signature"] == current_signature:
            return False
        if row["last_notified_status"] != current_status:
            # Let the status-change path own status/cancellation announcements.
            return False
        if row["last_notified_items_hash"] == current_items_hash:
            mark_snapshot(conn, numero)
            return False
        summary = load_detail_summary(row["detail_json_path"])
        if match_line_for(row, summary, load_keywords()) is None:
            mark_snapshot(conn, numero)
            return False
        sent = send_text("update", build_items_changed_message(row, summary))
        if sent:
            export_record_calendar(conn, row)
            mark_snapshot(conn, numero)
        return sent
    except Exception as exc:  # noqa: BLE001 - never break a run
        print(f"WAHA items-change notify error for {numero}: {exc}", file=sys.stderr)
        return False

def sync_snapshots(conn, since: str) -> int:
    """After the detail downloads, refresh the stored status/items snapshot for
    records announced since ``since`` (this run's early MESSAGING step), and
    export their per-record calendars. Without this, the items downloaded right
    after the announcement would fire a spurious "items updated" message on the
    next run. Sends nothing. Returns the number of snapshots refreshed."""
    rows = conn.execute(
        "SELECT numero FROM opportunities WHERE detail_status = 'saved' "
        "AND notified_at IS NOT NULL AND notified_at >= ?",
        (since,),
    ).fetchall()
    updated = 0
    for row in rows:
        try:
            full = fetch_row(conn, row["numero"])
            if full is None:
                continue
            _status, _items_hash, signature = signature_for(full)
            if full["last_notified_signature"] == signature:
                continue
            export_record_calendar(conn, full)
            mark_snapshot(conn, full["numero"])
            updated += 1
        except Exception as exc:  # noqa: BLE001 - never break a run
            print(f"WAHA snapshot sync error for {row['numero']}: {exc}", file=sys.stderr)
    return updated


def flush_unannounced(conn) -> int:
    """Announce any records that were not yet sent (for example because WAHA
    was briefly unreachable). Returns the number sent."""
    rows = conn.execute(
        "SELECT numero FROM opportunities WHERE notified_at IS NULL "
        "ORDER BY first_seen, last_seen"
    ).fetchall()
    sent = 0
    for row in rows:
        if notify_saved_record(conn, row["numero"]):
            sent += 1
    return sent


def _short_label(row) -> str:
    """One-line 'NUMERO — description' label for the monitor message line."""
    numero = clean_field(row["numero"])
    desc = clean_field(row["descripcion"] or row["short_description"])
    return f"{numero} {DASH} {desc}"


def _one_line_preview(text: str, limit: int = 160) -> str:
    """Collapse the multi-line WhatsApp body to a single readable line for the
    monitor's progress file (which is parsed line by line)."""
    flat = " · ".join(part.strip() for part in text.splitlines() if part.strip())
    return (flat[: limit - 1] + "…") if len(flat) > limit else flat


def _percent_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


def announce_with_progress(conn) -> int:
    """Visible MESSAGING step. Right after the index step (before detail
    downloads), send — one by one with per-message monitor progress — a message
    for every:

      * new entry (🔔 Nueva Oportunidad, items shown as pending download), and
      * status change (e.g. Programada → Abierta) or item change detected on
        records downloaded in previous runs.

    When there is nothing to send it publishes the "Sin nuevas entradas" status.
    Returns the number of messages actually sent. Never raises."""
    step_current = os.environ.get("PC_MSG_STEP_CURRENT", "2")
    step_total = os.environ.get("PC_MSG_STEP_TOTAL", "6")
    # Progress percent window for this step, set by the worker to match its
    # position in the run (defaults keep the old end-of-run scale).
    percent_base = _percent_int("PC_MSG_PERCENT_BASE", 96)
    percent_done = max(percent_base + 1, _percent_int("PC_MSG_PERCENT_DONE", 99))

    # ("new", numero), then status updates, then item-only changes, oldest first.
    new_rows = conn.execute(
        "SELECT numero FROM opportunities WHERE notified_at IS NULL "
        "ORDER BY first_seen, last_seen"
    ).fetchall()
    update_rows = conn.execute(
        "SELECT numero FROM opportunities WHERE pending_status_change IS NOT NULL "
        "ORDER BY last_seen, first_seen"
    ).fetchall()
    update_numbers = {r["numero"] for r in update_rows}
    detected_status_rows = []
    changed_rows = []
    for row in conn.execute(
        "SELECT numero FROM opportunities WHERE detail_status = 'saved' AND notified_at IS NOT NULL "
        "AND last_notified_signature IS NOT NULL ORDER BY detail_saved_at, first_seen"
    ).fetchall():
        if row["numero"] in update_numbers:
            continue
        full = fetch_row(conn, row["numero"])
        if full is None:
            continue
        status, items_hash, signature = signature_for(full)
        if full["last_notified_signature"] == signature:
            continue
        if full["last_notified_status"] != status:
            detected_status_rows.append(row)
        elif full["last_notified_items_hash"] != items_hash:
            changed_rows.append(row)
    queue = (
        [("new", r["numero"]) for r in new_rows]
        + [("update", r["numero"]) for r in update_rows]
        + [("status", r["numero"]) for r in detected_status_rows]
        + [("items", r["numero"]) for r in changed_rows]
    )
    total = len(queue)

    if total == 0:
        total_records = conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]
        send_text("none", build_empty_message(total_records))
        pc_common.write_run_progress(
            "MESSAGING", "RUNNING", percent_done - 1,
            f"Step {step_current}/{step_total}: no new opportunities or status changes to send.",
            step_current=step_current, step_total=step_total,
            item_current=0, item_total=0, records_new=0,
        )
        return 0

    sent = 0
    skipped = 0
    for index, (kind, numero) in enumerate(queue, start=1):
        full_row = fetch_row(conn, numero)
        label = _short_label(full_row) if full_row is not None else numero
        summary = load_detail_summary(full_row["detail_json_path"]) if full_row is not None else {}
        if kind == "update":
            change = full_row["pending_status_change"] if full_row is not None else ""
            verb = STATUS_CHANGE_LABELS.get(change, change or "actualización")
            preview = f"🔄 {label} ({verb})"
        elif kind == "status":
            preview = f"🟡 {label} (estado cambiado)"
        elif kind == "items":
            preview = f"🔵 {label} (items modificados)"
        else:
            match_line = match_line_for(full_row, summary, load_keywords()) if full_row is not None else None
            preview = (
                _one_line_preview(build_opportunity_message(full_row, summary, match_line))
                if (full_row is not None and match_line is not None)
                else f"{label} (sin coincidencia de palabra clave)"
            )
        pc_common.write_run_progress(
            "MESSAGING", "RUNNING",
            min(percent_done, percent_base + int((percent_done - percent_base) * index / total)),
            f"Step {step_current}/{step_total}: sending WhatsApp {index}/{total} ({kind}): {label}",
            step_current=step_current, step_total=step_total,
            item_current=index, item_total=total,
            records_new=total, records_saved=sent,
            extra=preview,
        )
        if kind == "update":
            ok = notify_status_change(conn, numero)
        elif kind == "status":
            ok = notify_detected_status_change(conn, numero)
        elif kind == "items":
            ok = notify_items_change(conn, numero)
        else:
            ok = notify_saved_record(conn, numero)
        if ok:
            sent += 1
        else:
            skipped += 1

    pc_common.write_run_progress(
        "MESSAGING", "RUNNING", percent_done,
        f"Step {step_current}/{step_total}: WhatsApp done — {sent} sent, {skipped} skipped of {total} ({len(new_rows)} new, {len(update_rows) + len(detected_status_rows)} status updates, {len(changed_rows)} item changes).",
        step_current=step_current, step_total=step_total,
        item_current=total, item_total=total,
        records_new=len(new_rows), records_saved=sent,
    )
    print(f"WAHA announce complete: {sent} sent, {skipped} skipped of {total}.")
    return sent


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="PanamaCompra WAHA new-record notifier")
    parser.add_argument("--idle", action="store_true", help="send the 'Sin nuevas entradas' status (run found no new records)")
    parser.add_argument("--flush", action="store_true", help="announce any records not yet sent (safety net)")
    parser.add_argument("--announce", action="store_true", help="announce every new record one by one, publishing per-message monitor progress (the visible MESSAGING step, right after the index)")
    parser.add_argument("--sync-snapshots", action="store_true", help="silently refresh notified snapshots (and export calendars) for records announced since --since; called after the detail downloads")
    parser.add_argument("--since", default="", help="timestamp (YYYY-MM-DD HH:MM:SS) limiting --sync-snapshots to records notified at/after it")
    parser.add_argument("--record", action="append", default=[], help="manually send notification for a specific record NUMERO; repeat for several records")
    parser.add_argument("--force", action="store_true", help="with --record, send even if the record was already notified")
    args = parser.parse_args(argv)

    if not waha_enabled():
        print("WAHA notification skipped: set PC_WAHA_ENABLED=1 to enable.")
        return 0
    if not waha_destination():
        print("WAHA notification skipped: PC_WAHA_CHAT_ID is not set.")
        return 0

    conn = pc_common.init_db()
    just_baselined = ensure_baseline(conn)

    if args.record:
        sent = 0
        for numero in args.record:
            if notify_manual_record(conn, numero, force=args.force):
                sent += 1
        print(f"WAHA manual record notification complete: {sent} record(s) sent.")
        return 0

    if args.sync_snapshots:
        updated = sync_snapshots(conn, args.since or "1970-01-01 00:00:00")
        print(f"WAHA snapshot sync complete: {updated} record(s) refreshed.")
        return 0

    if args.idle:
        if just_baselined:
            # Right after establishing the baseline, do not claim "no new entries".
            return 0
        total_records = conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]
        send_text("none", build_empty_message(total_records))
        return 0

    if just_baselined:
        # Nothing to announce on the very first run that established the baseline;
        # also drop any status-change flags so the baseline run stays silent.
        conn.execute("UPDATE opportunities SET pending_status_change = NULL")
        conn.commit()
        return 0

    if args.announce:
        # Visible MESSAGING step: send every new record and status change one by
        # one with per-message progress.
        announce_with_progress(conn)
        return 0

    # Default / --flush: announce stragglers (for example after WAHA failed).
    sent = flush_unannounced(conn)
    print(f"WAHA flush complete: {sent} record(s) announced.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - defensive: never break the worker
        print(f"notify_new_records fatal error: {exc}", file=sys.stderr)
        raise SystemExit(0)
