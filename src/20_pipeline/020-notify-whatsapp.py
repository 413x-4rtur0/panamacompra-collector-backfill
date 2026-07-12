#!/usr/bin/env python3
"""Send rich PanamaCompra "what is new" notifications to WhatsApp via WAHA.

Two notifier phases, called by the run-all worker:

* ``--announce`` (index phase) runs right after the index step, BEFORE detail
  downloads, so subscribers hear about new opportunities immediately instead of
  after the (potentially hours-long) download phase. It sends one rich
  "🔔 Nueva Oportunidad" message per new record with monitor-visible progress;
  item details show as pending because the detail page is not downloaded yet.
* ``--announce-details`` (detail phase) runs after the downloads and detail
  views finish. For every record announced from the index it sends the
  follow-up "📥 Detalles Completos" message in the full rich format — real
  items, location, complete date range — and exports the record's calendar.
  Guarded by ``detail_notified_at`` so each record gets exactly one follow-up.

``--idle`` sends one "⚪ Sin nuevas entradas" status; ``--flush`` retries
records that still have no ``notified_at`` timestamp; ``--sync-snapshots
--since TS`` is the silent fallback used when the detail phase is disabled
(PC_NOTIFY_DETAILS=0) so the downloaded items do not fire a duplicate "items
updated" message on the next run.

Readability controls (all also readable from monitor_settings.env):

* ``PC_WAHA_SEND_DELAY_SECONDS`` (default 3) paces consecutive sends so a batch
  arrives as separate readable messages instead of one burst; 0 disables.
* ``PC_NOTIFY_INDEX_DIGEST_THRESHOLD`` (default 10) collapses the index alerts
  into compact digest message(s) when a run finds more new records than the
  threshold; each record still gets its own detail follow-up. 0 disables.
* ``PC_NOTIFY_IDLE_EVERY_HOURS`` (default 6) throttles the idle status message
  so frequent webhook runs do not repeat "Sin nuevas entradas"; 0 = every run.
* ``PC_NOTIFY_DETAILS_INLINE`` (default 1, read by 030-collect-details.py)
  sends each record's detail follow-up right after ITS download finishes, so
  those messages arrive naturally spaced across the download phase. The
  ``--announce-details`` step stays as the idempotent catch-up.

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
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SRC_DIR))
import common as pc_common

waha = pc_common.load_script("src/30_notify/010-waha-client.py", "waha_client")

CONFIG_DIR = pc_common.DATA_CONFIG_DIR
CALENDAR_EXPORT_DIR = pc_common.CALENDAR_EXPORT_DIR
KEYWORDS_PATH = CONFIG_DIR / "waha_keywords.txt"
CLIENTS_PATH = CONFIG_DIR / "waha_clients.json"
BASELINE_MARKER = CONFIG_DIR / "waha_notify_initialized"
DETAIL_BASELINE_MARKER = CONFIG_DIR / "waha_detail_notify_initialized"
SETTINGS_PATH = CONFIG_DIR / "monitor_settings.env"
# Timestamp of the last "⚪ Sin nuevas entradas" message, so idle status is
# throttled (PC_NOTIFY_IDLE_EVERY_HOURS) instead of repeating on every run.
IDLE_MARKER = CONFIG_DIR / "waha_idle_last_sent.txt"

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


def _to_24h(text: str) -> str:
    """Rewrite any 12h 'HH:MM AM/PM' (including 'a.m.'/'p. m.' variants) inside
    ``text`` as 24h 'HH:MM' so strptime's 24h formats can parse it."""
    def repl(m):
        hh, mm = int(m.group(1)), int(m.group(2))
        ap = m.group(3).lower().replace(".", "").replace(" ", "")
        if ap == "pm" and hh != 12:
            hh += 12
        elif ap == "am" and hh == 12:
            hh = 0
        return f"{hh % 24:02d}:{mm}"
    return re.sub(r"(\d{1,2}):(\d{2})(?::\d{2})?\s*([AaPp]\.?\s*[Mm]\.?)", repl, text)


def fmt_dt(value, *, with_time: bool = True) -> str:
    """Normalize a stored date/datetime string to 'YYYY-MM-DD_HH-MM' (or a
    bare 'YYYY-MM-DD' when no time is present), always 24h, so WhatsApp
    messages show consistent dates and times. Falls back to the cleaned
    original when it cannot be parsed."""
    raw = clean_field(value)
    if raw == DASH:
        return DASH
    text = _to_24h(raw.replace("T", " ").replace("_", " ").strip())
    formats = (
        ("%Y-%m-%d %H:%M:%S", True),
        ("%Y-%m-%d %H:%M", True),
        ("%Y-%m-%d", False),
        ("%d/%m/%Y %H:%M", True),
        ("%d/%m/%Y", False),
        ("%d-%m-%Y %H:%M", True),
        ("%d-%m-%Y", False),
    )
    for candidate in (text, text[:19], text[:16], text[:10]):
        for fmt, has_time in formats:
            try:
                parsed = datetime.strptime(candidate, fmt)
            except ValueError:
                continue
            return parsed.strftime("%Y-%m-%d_%H-%M" if (with_time and has_time) else "%Y-%m-%d")
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


def waha_destination() -> bool:
    """True when any WhatsApp destination is configured (the default chat id or
    any of the per-purpose index/details/status/system/summary destinations, or
    at least one enabled client profile with its own chat/group id)."""
    return waha.any_destination_configured() or bool(load_client_profiles())


FILTER_PURPOSES = ("index", "details", "status")


def keywords_path(purpose: str = "") -> Path:
    """Filter file for a WhatsApp destination ('' = the shared/global filter)."""
    if purpose in FILTER_PURPOSES:
        return CONFIG_DIR / f"waha_keywords_{purpose}.txt"
    return KEYWORDS_PATH


def parse_filter_rules(text: str) -> tuple[list[list[str]], list[list[str]]]:
    """Parse filter rules from a file/entry.

    One rule per line (commas also separate rules). Operators:
      * OR  between rules: a record is announced when ANY rule matches.
      * AND inside a rule with '+': ``salud + panama`` requires both words.
      * NOT with a leading '-': ``-construccion`` excludes matching records
        even when an include rule matched. ``-obra + calle`` excludes only
        records containing both words.
    Matching is accent- and case-insensitive substring search over the record's
    description, entity, dependency, modality and detail summary. Returns
    (include_rules, exclude_rules) as lists of term lists."""
    import re as _re
    includes: list[list[str]] = []
    excludes: list[list[str]] = []
    for raw in _re.split(r"[,\n]", text or ""):
        token = raw.strip()
        if not token or token.startswith("#"):
            continue
        negate = token.startswith("-")
        if negate:
            token = token[1:].strip()
        terms = [term.strip() for term in token.split("+") if term.strip()]
        if terms:
            (excludes if negate else includes).append(terms)
    return includes, excludes


def load_filter_rules(purpose: str = "") -> tuple[list[list[str]], list[list[str]]]:
    """Rules for a destination: its own file when it has rules, else the
    shared filter file, else no filter (everything announced)."""
    candidates = [keywords_path(purpose)] if purpose in FILTER_PURPOSES else []
    candidates.append(KEYWORDS_PATH)
    for path in candidates:
        if path.exists():
            includes, excludes = parse_filter_rules(path.read_text(encoding="utf-8", errors="replace"))
            if includes or excludes:
                return includes, excludes
    return [], []


def load_client_profiles() -> list[dict]:
    """Optional per-client WhatsApp fan-out profiles.

    Saved as data/config/waha_clients.json:
      [{"name":"Client A","chat_id":"120...@g.us","purposes":["index","details"],"filters":"salud + insumos, -construccion"}]
    """
    try:
        raw = json.loads(CLIENTS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(raw, list):
        return []
    profiles = []
    for item in raw:
        if not isinstance(item, dict) or item.get("enabled", True) is False:
            continue
        chat_id = str(item.get("chat_id") or "").strip()
        if not chat_id:
            continue
        purposes = item.get("purposes") or ["index", "details", "status"]
        if isinstance(purposes, str):
            purposes = [p.strip() for p in purposes.split(",")]
        purposes = [str(p).strip().lower() for p in purposes if str(p).strip()]
        profiles.append({
            "name": str(item.get("name") or chat_id).strip(),
            "chat_id": chat_id,
            "purposes": purposes or ["index", "details", "status"],
            "filters": str(item.get("filters") or "").strip(),
        })
    return profiles


def evaluate_filter(haystack: str, includes: list[list[str]], excludes: list[list[str]]) -> str | None:
    """The '🔎 Coincidencia' line, or None when the record must not be sent."""
    normalized = pc_common.strip_accents(haystack).lower()

    def rule_matches(terms: list[str]) -> bool:
        return all(pc_common.strip_accents(term).lower() in normalized for term in terms)

    if any(rule_matches(rule) for rule in excludes):
        return None
    if not includes:
        return "Sin filtro (todas las entradas)" if not excludes else "Pasa las exclusiones"
    matched = [" + ".join(rule) for rule in includes if rule_matches(rule)]
    return ", ".join(matched) if matched else None


def row_filter_haystack(row, summary: dict) -> str:
    return " ".join(
        str(value)
        for value in (
            row["descripcion"],
            row["short_description"],
            row["entidad"],
            row["dependencia"],
            row["modalidad"],
            row["grupo"],
            summary.get("descripcion"),
            " ".join(str(item.get("descripcion") or item.get("description") or item) for item in load_detail_items(row["detail_json_path"])[:20]) if row["detail_json_path"] else "",
        )
        if value
    )


def matching_client_profiles(purpose: str, row, summary: dict) -> list[dict]:
    profiles = []
    haystack = row_filter_haystack(row, summary)
    for profile in load_client_profiles():
        purposes = profile["purposes"]
        if "all" not in purposes and purpose not in purposes:
            continue
        includes, excludes = parse_filter_rules(profile["filters"])
        if evaluate_filter(haystack, includes, excludes) is None:
            continue
        profiles.append(profile)
    return profiles


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


def record_location(summary: dict, row=None) -> str:
    """Every location fact available for the record, most specific first.

    Location is a priority field: collect ALL provincia/lugar/dirección values
    present in the detail summary (not just the two fixed keys), and when the
    summary has none fall back to unidad de compra → dependencia → entidad so
    the line is never empty."""
    parts: list[str] = []

    def add(value) -> None:
        value = clean_field(value)
        if value != DASH and value not in parts:
            parts.append(value)

    for needle in ("provincia", "lugar", "direccion"):
        for key, value in (summary or {}).items():
            if isinstance(value, str) and needle in pc_common.strip_accents(str(key)).lower():
                add(value)
    if not parts:
        add(summary.get("unidad_de_compra"))
    if not parts and row is not None:
        add(row["dependencia"])
        if not parts:
            add(row["entidad"])
    return ", ".join(parts) if parts else "Panamá (sin dirección específica)"


def date_range(row, summary: dict) -> str:
    start = fmt_dt(row["fecha"] or summary.get("fecha_de_publicacion"))
    end = fmt_dt(row["finish_date_guess"] or summary.get("fecha_y_hora_limite_de_recepcion"))
    return f"{start}---{end}" if start != DASH or end != DASH else DASH


def contact_values(summary: dict) -> dict[str, str]:
    contact = summary.get("contacto") if isinstance(summary.get("contacto"), dict) else {}
    return {
        "nombre": clean_field(contact.get("nombre") or summary.get("contacto_nombre")),
        "cargo": clean_field(contact.get("cargo") or summary.get("contacto_cargo")),
        "telefono": clean_field(contact.get("telefono") or summary.get("telefono")),
        "correo": clean_field(contact.get("correo_electronico") or summary.get("correo_electronico") or summary.get("correo")),
    }


def format_contact_block(contact: dict[str, str]) -> str:
    lines = []
    if contact["nombre"] != DASH:
        name = contact["nombre"]
        if contact["cargo"] != DASH:
            name = f"{name} ({contact['cargo']})"
        lines.append(f"👤 *Contacto:* {name}")
    if contact["telefono"] != DASH:
        lines.append(f"☎️ *Teléfono:* {contact['telefono']}")
    if contact["correo"] != DASH:
        lines.append(f"✉️ *Correo:* {contact['correo']}")
    return "\n".join(lines) if lines else DASH


def build_record_message(row, summary: dict, *, variant: str, previous_status: str | None = None, match_line: str | None = None) -> str:
    detail_pending = row["detail_status"] != "saved"
    items = load_detail_items(row["detail_json_path"])
    status = status_value(row)
    title = clean_field(row["descripcion"] or row["short_description"] or summary.get("descripcion"))
    location = record_location(summary, row)
    url = clean_field(row["link"] or summary.get("enlace_publico") or summary.get("enlace_interno"))
    created = fmt_dt(row["first_seen"] or row["fecha"])
    downloaded = fmt_dt(row["detail_saved_at"]) if row["detail_saved_at"] else ("⏳ En descarga" if detail_pending else fmt_dt(now_str()))
    numero = clean_field(row["numero"])
    contact = contact_values(summary)
    contact_block = DASH if detail_pending else format_contact_block(contact)
    precio_referencia = clean_field(summary.get("precio_estimado"))
    unidad_compra = clean_field(summary.get("unidad_de_compra"))

    if variant == "new":
        heading = f"🔔 *Nueva Oportunidad - {SOURCE_NAME}*"
        status_line = f"📊 *Estado:* {status}"
    elif variant == "abierta":
        heading = f"🟢 *Oportunidad Ahora Abierta - {SOURCE_NAME}*"
        status_line = f"📊 *Estado:* [ANTERIOR: {clean_field(previous_status)}] ➡️ [ACTUAL: {status}]"
    elif variant == "cancelled":
        heading = f"❌ *Oportunidad Cancelada - {SOURCE_NAME}*"
        status_line = f"📊 *Estado:* {status.upper()}"
    elif variant == "status":
        heading = f"⚠️ *Cambio de Estado - {SOURCE_NAME}*"
        status_line = f"📊 *Estado:* [ANTERIOR: {clean_field(previous_status)}] ➡️ [ACTUAL: {status}]"
    elif variant == "items":
        heading = f"🔄 *Actualización de Items - {SOURCE_NAME}*"
        status_line = f"📊 *Estado:* {status} (Sin cambios)"
    elif variant == "details":
        heading = f"📥 *Detalles Completos - {SOURCE_NAME}*"
        status_line = f"📊 *Estado:* {status}"
    else:
        heading = f"🔔 *Oportunidad - {SOURCE_NAME}*"
        status_line = f"📊 *Estado:* {status}"

    items_block = (
        "📦 *Items:* ⏳ pendiente — los detalles se descargan después de este aviso"
        if detail_pending else format_items(items)
    )
    fechas = date_range(row, summary)

    deadline_dt = parse_finish_date(row)
    dias_restantes = str((deadline_dt.date() - datetime.now().date()).days) if deadline_dt else DASH

    # Operator-customized layout for this message family, when saved.
    custom = load_custom_format(kind_for_variant(variant))
    if custom:
        return custom.format_map(_SafeDict({
            "heading": heading,
            "fuente": SOURCE_NAME,
            "estado": status,
            "estado_linea": status_line,
            "estado_anterior": clean_field(previous_status),
            "numero": numero,
            "descripcion": title,
            "entidad": clean_field(row["entidad"]),
            "ubicacion": location,
            "rango_fechas": fechas,
            "items": items_block,
            "coincidencia": match_line or DASH,
            "enlace": url,
            "creado": created,
            "descargado": downloaded,
            "modalidad": clean_field(row["modalidad"]),
            "dependencia": clean_field(row["dependencia"]),
            "unidad_compra": unidad_compra,
            "precio_referencia": precio_referencia,
            "grupo": clean_field(row["grupo"]),
            "contacto": contact_block,
            "contacto_nombre": contact["nombre"],
            "contacto_cargo": contact["cargo"],
            "telefono": contact["telefono"],
            "correo": contact["correo"],
            "fecha_inicio": fmt_dt(row["fecha"] or summary.get("fecha_de_publicacion")),
            "fecha_limite": fmt_dt(row["finish_date_guess"] or summary.get("fecha_y_hora_limite_de_recepcion")),
            "items_total": "⏳" if detail_pending else str(len(items)),
            "dias_restantes": dias_restantes,
        }))

    parts = [
        heading,
        "",
        status_line,
        f"🔢 *Número:* {numero}",
        f"📝 *Descripción:* {title}",
        f"📍 *Ubicación:* {location}",
        f"📅 *Rango Fechas:* {fechas}",
    ]
    if variant == "details":
        parts.append(f"🏢 *Unidad de Compra:* {unidad_compra}")
        parts.append(f"💰 *Precio de Referencia:* {precio_referencia}")
    if contact_block != DASH:
        parts.extend(["", contact_block])
    parts.extend(["", items_block])
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
        f"🕒 Revisión: {fmt_dt(now_str())}\n"
        f"📊 Registros revisados: {records_checked}\n"
        "✅ Monitor activo"
    )


# ---------------------------------------------------------------------------
# Customizable message formats. Each message family (index alert / detail
# follow-up / status change / operational system message) can be reformatted by the operator: a template
# saved to data/config/waha_format_<kind>.txt (via `pcc format` or either
# monitor) replaces the built-in layout. Templates use {placeholder} fields;
# unknown placeholders are left literally so a typo never breaks a send.
FORMAT_KINDS = ("index", "details", "status", "system", "summary")

PLACEHOLDERS = {
    "heading": "message heading with emoji (varies per message type)",
    "fuente": "source label (PC_WAHA_SOURCE, default 'Panamá Compra')",
    "estado": "current record status (e.g. Abierta)",
    "estado_linea": "the built-in '📊 Estado:' line (includes previous→current on status changes)",
    "estado_anterior": "previous status on status-change messages",
    "numero": "record number (NUMERO)",
    "descripcion": "record title/description",
    "entidad": "contracting entity",
    "ubicacion": "delivery province/place",
    "rango_fechas": "start–deadline range",
    "items": "formatted items block (or the 'pendiente' note before download)",
    "coincidencia": "matched keywords line",
    "enlace": "portal link",
    "creado": "first-seen/publication timestamp",
    "descargado": "local download timestamp",
    "modalidad": "procurement modality",
    "dependencia": "entity dependency/office",
    "unidad_compra": "purchasing/buying unit (unidad de compra)",
    "precio_referencia": "reference/estimated price (precio estimado)",
    "grupo": "portal list the record came from (Programadas/Abiertas)",
    "contacto": "formatted detail contact block (name/role/phone/email), or — before download",
    "contacto_nombre": "detail contact name",
    "contacto_cargo": "detail contact role/title",
    "telefono": "detail contact phone number",
    "correo": "detail contact email address",
    "fecha_inicio": "start/publication date on its own",
    "fecha_limite": "deadline date on its own",
    "items_total": "number of items (⏳ before the detail download)",
    "dias_restantes": "whole days until the deadline (negative = expired)",
    "event": "system event name for operational messages (start/done/failed/info)",
    "status": "short system status label (for example SYSTEM HEALTH OK)",
    "message": "operator/system message body",
    "time": "send timestamp for operational messages",
    "run": "friendly run mode for operational messages, or blank",
}

DEFAULT_FORMATS = {
    "index": (
        "{heading}\n\n{estado_linea}\n🔢 *Número:* {numero}\n📝 *Descripción:* {descripcion}\n"
        "📍 *Ubicación:* {ubicacion}\n📅 *Rango Fechas:* {rango_fechas}\n\n{items}\n\n"
        "🔎 *Coincidencia:* {coincidencia}\n\n🔗 *Enlace:* {enlace}\n🕒 *Creado:* {creado}\n⬇️ *Descargado:* {descargado}"
    ),
    "details": (
        "{heading}\n\n{estado_linea}\n🔢 *Número:* {numero}\n📝 *Descripción:* {descripcion}\n"
        "📍 *Ubicación:* {ubicacion}\n📅 *Rango Fechas:* {rango_fechas}\n"
        "🏢 *Unidad de Compra:* {unidad_compra}\n💰 *Precio de Referencia:* {precio_referencia}\n\n{contacto}\n\n{items}\n\n"
        "🔎 *Coincidencia:* {coincidencia}\n\n🔗 *Enlace:* {enlace}\n🕒 *Creado:* {creado}\n⬇️ *Descargado:* {descargado}"
    ),
    "status": (
        "{heading}\n\n{estado_linea}\n🔢 *Número:* {numero}\n📝 *Descripción:* {descripcion}\n"
        "📍 *Ubicación:* {ubicacion}\n📅 *Rango Fechas:* {rango_fechas}\n\n{items}\n\n"
        "🔗 *Enlace:* {enlace}\n🕒 *Creado:* {creado}\n⬇️ *Descargado:* {descargado}"
    ),
    "system": (
        "{heading}\n\nStatus: {status}\nRun: {run}\nTime: {time}\n\n{message}"
    ),
    "summary": (
        "{heading}\n\nStatus: {status}\nRun: {run}\nTime: {time}\n\n{message}"
    ),
}


class _SafeDict(dict):
    """format_map helper: unknown {placeholders} stay literal instead of raising."""

    def __missing__(self, key):  # noqa: D105
        return "{" + key + "}"


def format_path(kind: str) -> Path:
    return CONFIG_DIR / f"waha_format_{kind}.txt"


def load_custom_format(kind: str) -> str:
    """Operator-saved template for a message kind, or '' for the built-in layout."""
    path = format_path(kind)
    if path.exists():
        text = path.read_text(encoding="utf-8", errors="replace").strip("\n")
        if text.strip():
            return text
    return ""


def kind_for_variant(variant: str) -> str:
    if variant in ("new", "abierta"):
        return "index"
    if variant in ("details", "manual"):
        return "details"
    return "status"  # status / cancelled / items


def sample_context(kind: str) -> dict[str, str]:
    """Fabricated values so templates can be previewed without a real record.

    The "index" kind covers both 🔔 Nueva Oportunidad and 🟢 Oportunidad Ahora
    Abierta messages (both use the same format template). The preview uses the
    new-opportunity heading; the Abiertas variant differs only in {heading} and
    {estado_linea}, which users can observe via `pcc format preview status`."""
    headings = {
        "index": f"🔔 *Nueva Oportunidad - {SOURCE_NAME}*",
        "details": f"📥 *Detalles Completos - {SOURCE_NAME}*",
        "status": f"⚠️ *Cambio de Estado - {SOURCE_NAME}*",
        "system": f"🛠️ *Sistema - {SOURCE_NAME}*",
        "summary": f"📊 *Resumen final de ronda - {SOURCE_NAME}*",
    }
    items = (
        "📦 *Items:* ⏳ pendiente — los detalles se descargan después de este aviso"
        if kind == "index"
        else "📦 *Items (2):*\n• Item 1: Guantes de nitrilo - Qty: 100 - Unit: caja\n• Item 2: Mascarillas N95 - Qty: 50 - Unit: caja"
    )
    return {
        "heading": headings[kind],
        "fuente": SOURCE_NAME,
        "estado": "Abierta",
        "estado_linea": "📊 *Estado:* [ANTERIOR: Programada] ➡️ [ACTUAL: Abierta]" if kind == "status" else "📊 *Estado:* Abierta",
        "estado_anterior": "Programada",
        "numero": "OC-2026-000123",
        "descripcion": "Adquisición de insumos médicos para el centro de salud",
        "entidad": "Ministerio de Salud",
        "ubicacion": "Panamá, Ciudad de Panamá",
        "rango_fechas": "26-07-01_09-00 al 26-07-15_16-00",
        "items": items,
        "coincidencia": "salud",
        "enlace": "https://www.panamacompra.gob.pa/…/OC-2026-000123",
        "creado": "26-07-01_09-12",
        "descargado": "26-07-01_09-45",
        "modalidad": "Cotización en línea",
        "dependencia": "Dirección de Compras",
        "grupo": "Abiertas",
        "contacto": "👤 *Contacto:* Ana Pérez (Oficial de compras)\n☎️ *Teléfono:* 507-555-0101\n✉️ *Correo:* compras@example.pa",
        "contacto_nombre": "Ana Pérez",
        "contacto_cargo": "Oficial de compras",
        "telefono": "507-555-0101",
        "correo": "compras@example.pa",
        "fecha_inicio": "26-07-01_09-00",
        "fecha_limite": "26-07-15_16-00",
        "items_total": "⏳" if kind == "index" else "2",
        "dias_restantes": "13",
        "event": "done",
        "status": "DONE" if kind == "summary" else "SYSTEM HEALTH OK",
        "message": "Inicio: 26-07-07_08-00\nFin: 26-07-07_08-18\nDuración total: 18m 42s\nNuevos: 4\nDetalles guardados: 4" if kind == "summary" else "System health review finished with status: OK. Check data/logs/manual_actions.log or the terminal output for details.",
        "time": "26-07-07_10-30",
        "run": "Manual",
    }


def render_format(kind: str, template: str | None = None) -> str:
    """Render a template (given, custom, or default) with the sample context."""
    text = template if template is not None else (load_custom_format(kind) or DEFAULT_FORMATS[kind])
    return text.format_map(_SafeDict(sample_context(kind)))


# Human-readable label for a pending_status_change code (used in monitor progress lines).
STATUS_CHANGE_LABELS = {
    "abierta": "Programada → Abierta",
    "cancelada": "Programada → Cancelada",
}

# Previous-status fallback for the WhatsApp message when last_notified_status is not set.
# Separate from STATUS_CHANGE_LABELS (which are full transition descriptions used only in
# monitor progress previews, not in message bodies).
_PREV_STATUS_FOR_CHANGE = {
    "abierta": "Programada",
    "cancelada": "Programada",
}


def build_status_change_message(row, summary: dict, change_code: str, match_line: str | None = None) -> str:
    # _PREV_STATUS_FOR_CHANGE supplies just the previous status name ("Programada"),
    # not the full transition label from STATUS_CHANGE_LABELS ("Programada → Abierta").
    previous_status = row["last_notified_status"] or _PREV_STATUS_FOR_CHANGE.get(change_code, change_code or DASH)
    if change_code == "abierta":
        variant = "abierta"
    elif is_cancelled_status(status_value(row)):
        variant = "cancelled"
    else:
        variant = "status"
    return build_record_message(row, summary, variant=variant, previous_status=previous_status, match_line=match_line)

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


def mark_detail_notified(conn, numero: str) -> None:
    conn.execute(
        "UPDATE opportunities SET detail_notified_at = ? WHERE numero = ?",
        (now_str(), numero),
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


def match_line_for(row, summary: dict, purpose: str = "") -> str | None:
    """Return the '🔎 Coincidencia' line for a destination's filter, or None
    when the record must not be announced (no include rule matched, or an
    exclude rule matched). Each WhatsApp destination (index/details/status) can
    have its own rules, falling back to the shared filter file."""
    includes, excludes = load_filter_rules(purpose)
    if not includes and not excludes:
        return "Sin filtro (todas las entradas)"
    haystack = " ".join(
        str(value)
        for value in (
            row["descripcion"],
            row["short_description"],
            row["entidad"],
            row["dependencia"],
            row["modalidad"],
            summary.get("descripcion"),
        )
        if value
    )
    return evaluate_filter(haystack, includes, excludes)


def allowed_match_line_for(row, summary: dict, purpose: str = "") -> str | None:
    """Global/per-purpose filter result, with client profiles as an alternate
    route. A record can still be sent when it does not match the shared
    destination filter but does match at least one client profile."""
    match_line = match_line_for(row, summary, purpose)
    if match_line is not None:
        return match_line
    if matching_client_profiles(purpose, row, summary):
        return "Coincidencia por perfil de cliente"
    return None


def record_events_respect_filter() -> bool:
    """Whether rich opportunity messages should obey PC_WAHA_NOTIFY_EVENTS.

    Default false: PC_NOTIFY_WHATSAPP is the record-notification switch, while
    PC_WAHA_NOTIFY_EVENTS remains useful for short operational messages. This
    avoids the confusing setup where only `done` is enabled, so the final summary
    sends but changedetection/new-opportunity messages are silently filtered out.
    Set PC_WAHA_RECORD_EVENTS_RESPECT_FILTER=1 to restore strict event filtering.
    """
    return cfg_bool("PC_WAHA_RECORD_EVENTS_RESPECT_FILTER", False)


def record_send_outcome(conn, numero: str, ok: bool, error: str = "") -> None:
    """Persist the WhatsApp delivery outcome on the record row.

    ``notify_attempts`` counts every real send try; ``notify_error`` keeps the
    last failure reason and clears on success, so the monitors can show a
    "Failed alerts" KPI (attempted-and-failed vs never-attempted). Skipped
    sends (no destination / filtered out) are NOT counted as attempts. Never
    raises: outcome tracking must not break a notification run."""
    if conn is None or not numero:
        return
    try:
        conn.execute(
            "UPDATE opportunities SET notify_attempts = COALESCE(notify_attempts, 0) + 1, "
            "notify_error = ? WHERE numero = ?",
            ((error or "")[:300] or None, numero),
        )
        conn.commit()
    except Exception:  # noqa: BLE001 - delivery tracking is best-effort
        pass


def send_text(event: str, text: str, purpose: str = "", conn=None, numero: str = "") -> bool:
    """Send through WAHA, routed to the per-purpose destination.

    Record-level sends are controlled by PC_NOTIFY_WHATSAPP plus destination and
    keyword/date filters. They intentionally bypass PC_WAHA_NOTIFY_EVENTS unless
    PC_WAHA_RECORD_EVENTS_RESPECT_FILTER=1, so changedetection page updates are
    not hidden while only the final `done` summary continues to send.

    When ``conn``/``numero`` are given, the delivery outcome is persisted on the
    record row (notify_attempts / notify_error) for the Failed-alerts KPI.
    """
    if record_events_respect_filter() and not waha.enabled_for_event(event):
        print(f"WAHA notification skipped: event {event!r} is not enabled.")
        return False
    client_profiles = []
    default_matches = True
    if conn is not None and numero:
        row = fetch_row(conn, numero)
        if row is not None:
            summary = load_detail_summary(row["detail_json_path"])
            # The shared destination and each client profile have independent
            # filters. A profile match must never make a non-matching record
            # leak into the shared/default group.
            default_matches = match_line_for(row, summary, purpose) is not None
            client_profiles = matching_client_profiles(purpose, row, summary)
    default_chat_id = waha.configured_chat_id(purpose).strip()
    has_default_destination = bool(default_chat_id and default_matches)
    if not has_default_destination and not client_profiles:
        # No destination for this purpose and no matching client profile: leave
        # the record unmarked so it sends once a destination is configured.
        print(f"WAHA notification skipped: no destination configured{f' for {purpose!r}' if purpose else ''}.")
        return False
    try:
        sent_any = False
        sent_chat_ids: set[str] = set()
        if has_default_destination:
            waha.send_text(text, purpose=purpose)
            sent_any = True
            sent_chat_ids.add(default_chat_id)
        for profile in client_profiles:
            profile_chat_id = profile["chat_id"].strip()
            # A group may be configured both as the shared destination and as
            # one or more client profiles. Deliver one copy per unique chat.
            if profile_chat_id in sent_chat_ids:
                print(f"WAHA profile notification skipped for {profile['name']}: destination already notified.")
                continue
            waha.send_text(text, purpose=purpose, chat_id_override=profile_chat_id)
            sent_any = True
            sent_chat_ids.add(profile_chat_id)
            print(f"WAHA profile notification sent for {profile['name']}.")
        if not sent_any:
            return False
        record_send_outcome(conn, numero, True)
        return True
    except Exception as exc:  # noqa: BLE001 - never let a notify failure stop a run
        print(f"WAHA notification failed: {exc}", file=sys.stderr)
        record_send_outcome(conn, numero, False, str(exc))
        return False


def send_delay_seconds() -> float:
    """Pause between consecutive WhatsApp sends so a batch stays readable on the
    phone instead of arriving as one burst. PC_WAHA_SEND_DELAY_SECONDS (env or
    monitor settings), default 3 seconds; 0 disables pacing."""
    raw = cfg("PC_WAHA_SEND_DELAY_SECONDS", "3").strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 3.0


def pace_after_send(index: int, total: int) -> None:
    """Sleep the configured send delay, except after the batch's last message."""
    delay = send_delay_seconds()
    if delay > 0 and index < total:
        time.sleep(delay)


def index_digest_threshold() -> int:
    """New-record count above which the index alerts collapse into digest
    messages. PC_NOTIFY_INDEX_DIGEST_THRESHOLD, default 10; 0 disables the
    digest so every new record keeps its own message."""
    if load_client_profiles():
        return 0
    value = cfg_int("PC_NOTIFY_INDEX_DIGEST_THRESHOLD")
    return 10 if value is None else max(0, value)


# Records listed per digest message; more new records roll into "parte 2/2"
# messages so no single WhatsApp message becomes unreadably long.
DIGEST_RECORDS_PER_MESSAGE = 20


def idle_every_hours() -> float:
    """Minimum hours between "Sin nuevas entradas" idle messages.
    PC_NOTIFY_IDLE_EVERY_HOURS, default 6; 0 restores one message per run."""
    raw = cfg("PC_NOTIFY_IDLE_EVERY_HOURS", "6").strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 6.0


def idle_message_due() -> bool:
    hours = idle_every_hours()
    if hours <= 0:
        return True
    stamp = waha.read_saved_text(IDLE_MARKER)
    if not stamp:
        return True
    try:
        last = datetime.strptime(stamp[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return True
    return datetime.now() - last >= timedelta(hours=hours)


def mark_idle_sent() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    IDLE_MARKER.write_text(now_str() + "\n", encoding="utf-8")


def send_idle_message(conn) -> bool:
    """Send the idle status, throttled by PC_NOTIFY_IDLE_EVERY_HOURS."""
    if not idle_message_due():
        print("WAHA idle message suppressed: sent recently "
              f"(every {idle_every_hours():g}h; see {IDLE_MARKER.name}).")
        return False
    total_records = conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]
    if send_text("none", build_empty_message(total_records), purpose="index"):
        mark_idle_sent()
        return True
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
        if not send_text("new", text, conn=conn, numero=numero):
            return False
        export_record_calendar(conn, row)
        mark_notified(conn, numero)
        # Manual messages already carry the downloaded items, so no follow-up
        # detail message is owed.
        mark_detail_notified(conn, numero)
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
            mark_detail_notified(conn, numero)
            return False
        if decision == "too_far":
            # Beyond the announce window for now; leave it unmarked so a later
            # run re-checks it once the deadline moves into range.
            return False
        summary = load_detail_summary(row["detail_json_path"])
        match_line = allowed_match_line_for(row, summary, "index")
        if match_line is None:
            # Filtered out by keywords: remember it so it is not rechecked.
            mark_notified(conn, numero)
            mark_detail_notified(conn, numero)
            return False
        if not send_text("new", build_opportunity_message(row, summary, match_line), purpose="index", conn=conn, numero=numero):
            # Leave notified_at unset so a later --flush retries it.
            return False
        export_record_calendar(conn, row)
        mark_notified(conn, numero)
        if row["detail_status"] == "saved":
            # The message already carried the real items (detail downloaded in a
            # previous run), so no follow-up detail message is owed.
            mark_detail_notified(conn, numero)
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
    True only when a message was actually sent. Never raises.

    Programadas→Abiertas transitions (change_code="abierta") are routed to the
    "index" destination so they appear in the same feed as new-opportunity alerts.
    All other status changes continue going to the "status" destination."""
    try:
        if not (waha_enabled() and waha_destination()):
            return False
        row = fetch_row(conn, numero)
        if row is None or not row["pending_status_change"]:
            return False
        change_code = row["pending_status_change"]
        # Abiertas transitions go to the index feed (new-opportunity channel).
        purpose = "index" if change_code == "abierta" else "status"
        summary = load_detail_summary(row["detail_json_path"])
        match_line = allowed_match_line_for(row, summary, purpose)
        if match_line is None:
            clear_status_change(conn, numero)
            return False
        sent = send_text("update", build_status_change_message(row, summary, change_code, match_line=match_line), purpose=purpose, conn=conn, numero=numero)
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
        if allowed_match_line_for(row, summary, "status") is None:
            mark_snapshot(conn, numero)
            return False
        sent = send_text("update", build_record_message(
            row,
            summary,
            variant="cancelled" if is_cancelled_status(current_status) else "status",
            previous_status=row["last_notified_status"],
        ), purpose="status")
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
        if allowed_match_line_for(row, summary, "status") is None:
            mark_snapshot(conn, numero)
            return False
        sent = send_text("update", build_items_changed_message(row, summary), purpose="status", conn=conn, numero=numero)
        if sent:
            export_record_calendar(conn, row)
            mark_snapshot(conn, numero)
        return sent
    except Exception as exc:  # noqa: BLE001 - never break a run
        print(f"WAHA items-change notify error for {numero}: {exc}", file=sys.stderr)
        return False

def ensure_detail_baseline(conn, since: str) -> bool:
    """First-use baseline for the detail (second) notifier phase: mark records
    announced before this feature existed as already detail-notified, so an
    upgraded install does not burst follow-up messages for the whole archive.

    ``since`` is the current run's start time: records announced during THIS run
    stay eligible for their detail follow-up. Exception: when the main baseline
    itself was established during this run, nothing was actually messaged, so
    everything is marked."""
    if DETAIL_BASELINE_MARKER.exists():
        return False
    if not (waha_enabled() and waha_destination()):
        return False
    main_baseline_time = waha.read_saved_text(BASELINE_MARKER)
    cutoff = since
    if not since or (main_baseline_time and main_baseline_time >= since):
        cutoff = ""
    if cutoff:
        conn.execute(
            "UPDATE opportunities SET detail_notified_at = notified_at "
            "WHERE notified_at IS NOT NULL AND detail_notified_at IS NULL AND notified_at < ?",
            (cutoff,),
        )
    else:
        conn.execute(
            "UPDATE opportunities SET detail_notified_at = notified_at "
            "WHERE notified_at IS NOT NULL AND detail_notified_at IS NULL"
        )
    conn.commit()
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    DETAIL_BASELINE_MARKER.write_text(now_str() + "\n", encoding="utf-8")
    print("WAHA detail-notify baseline established; previously announced records will not get follow-up detail messages.")
    return True


def notify_detail_ready(conn, numero: str) -> bool:
    """Second notifier phase: send the follow-up '📥 Detalles Completos' message
    (real items, location, full date range) for a record that was announced from
    the index and whose detail page has now been downloaded. Idempotent via
    detail_notified_at. Returns True only when a message was sent. Never
    raises."""
    try:
        if not (waha_enabled() and waha_destination()):
            return False
        row = fetch_row(conn, numero)
        if row is None or row["detail_status"] != "saved" or not row["notified_at"] or row["detail_notified_at"]:
            return False
        summary = load_detail_summary(row["detail_json_path"])
        match_line = allowed_match_line_for(row, summary, "details")
        if match_line is None:
            # Filtered out for the details destination: settle it silently.
            mark_detail_notified(conn, numero)
            mark_snapshot(conn, numero)
            return False
        if not send_text("new", build_record_message(row, summary, variant="details", match_line=match_line), purpose="details", conn=conn, numero=numero):
            # Leave detail_notified_at unset so the next run retries.
            return False
        export_record_calendar(conn, row)
        mark_detail_notified(conn, numero)
        # Absorb the downloaded items into the notified snapshot so the next
        # run's change detection does not re-announce them as an item change.
        mark_snapshot(conn, numero)
        return True
    except Exception as exc:  # noqa: BLE001 - never break a run
        print(f"WAHA detail notify error for {numero}: {exc}", file=sys.stderr)
        return False


def announce_details_with_progress(conn) -> int:
    """Visible second MESSAGING step. After the detail downloads and view
    rebuilds, send — one by one with per-message monitor progress — the
    follow-up detail message for every record announced from the index whose
    detail page is now saved. Returns the number sent. Never raises."""
    step_current = os.environ.get("PC_MSG_STEP_CURRENT", "5")
    step_total = os.environ.get("PC_MSG_STEP_TOTAL", "7")
    percent_base = _percent_int("PC_MSG_PERCENT_BASE", 80)
    percent_done = max(percent_base + 1, _percent_int("PC_MSG_PERCENT_DONE", 83))

    rows = conn.execute(
        "SELECT numero FROM opportunities WHERE detail_status = 'saved' "
        "AND notified_at IS NOT NULL AND detail_notified_at IS NULL "
        "ORDER BY detail_saved_at, first_seen"
    ).fetchall()
    total = len(rows)

    if total == 0:
        pc_common.write_run_progress(
            "MESSAGING", "RUNNING", percent_done - 1,
            f"Step {step_current}/{step_total}: no detail follow-up messages to send.",
            step_current=step_current, step_total=step_total,
            item_current=0, item_total=0, records_new=0,
        )
        print("WAHA detail announce: nothing to send.")
        return 0

    sent = 0
    skipped = 0
    for index, row in enumerate(rows, start=1):
        full_row = fetch_row(conn, row["numero"])
        label = _short_label(full_row) if full_row is not None else row["numero"]
        summary = load_detail_summary(full_row["detail_json_path"]) if full_row is not None else {}
        match_line = allowed_match_line_for(full_row, summary, "details") if full_row is not None else None
        preview = (
            _one_line_preview(build_record_message(full_row, summary, variant="details", match_line=match_line))
            if (full_row is not None and match_line is not None)
            else f"{label} (sin coincidencia de palabra clave)"
        )
        pc_common.write_run_progress(
            "MESSAGING", "RUNNING",
            min(percent_done, percent_base + int((percent_done - percent_base) * index / total)),
            f"Step {step_current}/{step_total}: sending WhatsApp details {index}/{total}: {label}",
            step_current=step_current, step_total=step_total,
            item_current=index, item_total=total,
            records_new=total, records_saved=sent,
            extra=preview,
        )
        if notify_detail_ready(conn, row["numero"]):
            sent += 1
            pace_after_send(index, total)
        else:
            skipped += 1

    pc_common.write_run_progress(
        "MESSAGING", "RUNNING", percent_done,
        f"Step {step_current}/{step_total}: WhatsApp details done — {sent} sent, {skipped} skipped of {total}.",
        step_current=step_current, step_total=step_total,
        item_current=total, item_total=total,
        records_new=total, records_saved=sent,
    )
    print(f"WAHA detail announce complete: {sent} sent, {skipped} skipped of {total}.")
    return sent


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
    for index, row in enumerate(rows, start=1):
        if notify_saved_record(conn, row["numero"]):
            sent += 1
            pace_after_send(index, len(rows))
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


def _digest_trim(value, limit: int) -> str:
    text = clean_field(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def build_index_digest_messages(rows) -> list[tuple[list, str]]:
    """Compose the digest message(s) for many new records at once.

    Returns [(chunk_rows, text), ...] so the caller can mark exactly the records
    whose message was accepted. Each record takes two compact lines; chunks of
    DIGEST_RECORDS_PER_MESSAGE keep any single WhatsApp message readable."""
    chunks = [
        rows[i:i + DIGEST_RECORDS_PER_MESSAGE]
        for i in range(0, len(rows), DIGEST_RECORDS_PER_MESSAGE)
    ]
    out = []
    for part, chunk in enumerate(chunks, start=1):
        heading = f"🔔 *Nuevas Oportunidades ({len(rows)}) - {SOURCE_NAME}*"
        if len(chunks) > 1:
            heading += f" — parte {part}/{len(chunks)}"
        lines = [heading, ""]
        for offset, row in enumerate(chunk, start=1):
            if offset > 1:
                lines.append("")  # blank line so every opportunity starts on its own block
            position = (part - 1) * DIGEST_RECORDS_PER_MESSAGE + offset
            fecha = fmt_dt(row["finish_date_guess"] or row["fecha"])
            lines.append(f"{position}. *{clean_field(row['numero'])}* — "
                         f"{_digest_trim(row['descripcion'] or row['short_description'], 90)}")
            lines.append(f"    🏛 {_digest_trim(row['entidad'], 60)} · 📅 {fecha}")
            link = clean_field(row["link"])
            if link != DASH:
                lines.append(f"    🔗 {link}")
        lines += ["", f"🕒 {fmt_dt(now_str())} · 📥 Cada registro envía sus detalles completos al terminar su descarga"]
        out.append((chunk, "\n".join(lines)))
    return out


def collect_digest_rows(conn, numeros) -> list:
    """Apply the same guards as notify_saved_record (baseline, deadline window,
    keyword filter) to not-yet-announced records. Records that must never send
    are settled silently (marked notified), 'too_far' ones are left for a later
    run, and the rows eligible for announcing are returned."""
    eligible = []
    if not BASELINE_MARKER.exists():
        return eligible
    for numero in numeros:
        try:
            row = fetch_row(conn, numero)
            if row is None or row["notified_at"]:
                continue
            decision = deadline_decision(row)
            if decision == "expired":
                mark_notified(conn, numero)
                mark_detail_notified(conn, numero)
                continue
            if decision == "too_far":
                continue
            summary = load_detail_summary(row["detail_json_path"])
            if allowed_match_line_for(row, summary, "index") is None:
                mark_notified(conn, numero)
                mark_detail_notified(conn, numero)
                continue
            eligible.append(row)
        except Exception as exc:  # noqa: BLE001 - never break a run
            print(f"WAHA digest eligibility error for {numero}: {exc}", file=sys.stderr)
    return eligible


def announce_index_digest(conn, rows) -> tuple[int, int]:
    """Send the eligible new records as digest message(s). Returns
    (messages_sent, records_announced). A chunk's records are marked notified
    only after its message is accepted, so a failed send retries next run.
    Digested records keep detail_notified_at unset: each still gets its full
    'Detalles Completos' message once its detail page is downloaded."""
    sent_messages = 0
    announced = 0
    chunks = build_index_digest_messages(rows)
    for index, (chunk_rows, text) in enumerate(chunks, start=1):
        if not send_text("new", text, purpose="index"):
            # Track the failed attempt on every record of this chunk so the
            # Failed-alerts KPI counts them, then let the next run retry.
            for row in chunk_rows:
                record_send_outcome(conn, row["numero"], False, "index digest send failed")
            break  # leave the remaining chunks unmarked; the next run retries
        sent_messages += 1
        for row in chunk_rows:
            try:
                record_send_outcome(conn, row["numero"], True)
                export_record_calendar(conn, row)
                mark_notified(conn, row["numero"])
                announced += 1
            except Exception as exc:  # noqa: BLE001 - keep marking the rest
                print(f"WAHA digest mark error for {row['numero']}: {exc}", file=sys.stderr)
        pace_after_send(index, len(chunks))
    return sent_messages, announced


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
    step_total = os.environ.get("PC_MSG_STEP_TOTAL", "7")
    # Progress percent window for this step, set by the worker to match its
    # position in the run (defaults keep the old end-of-run scale).
    percent_base = _percent_int("PC_MSG_PERCENT_BASE", 96)
    percent_done = max(percent_base + 1, _percent_int("PC_MSG_PERCENT_DONE", 99))

    # ("new", numero), then status updates, then item-only changes, oldest first.
    new_rows = conn.execute(
        "SELECT numero FROM opportunities WHERE notified_at IS NULL "
        "ORDER BY first_seen, last_seen"
    ).fetchall()
    new_numbers = [r["numero"] for r in new_rows]

    # Digest mode: when a run finds many new records, collapse them into one
    # (or a few) compact digest message(s) instead of a long per-record burst.
    digest_rows: list = []
    threshold = index_digest_threshold()
    if threshold and len(new_rows) > threshold:
        eligible = collect_digest_rows(conn, new_numbers)
        if len(eligible) > threshold:
            digest_rows = eligible
            new_numbers = []
        else:
            # After deadline/keyword settling only a few remain: keep the
            # richer per-record messages for them.
            new_numbers = [row["numero"] for row in eligible]
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
        if full["detail_notified_at"] is None:
            # The detail (second) notifier phase still owes this record its
            # follow-up message with the downloaded items; do not report the
            # empty→downloaded items transition as an "items updated" change.
            continue
        status, items_hash, signature = signature_for(full)
        if full["last_notified_signature"] == signature:
            continue
        if full["last_notified_status"] != status:
            detected_status_rows.append(row)
        elif full["last_notified_items_hash"] != items_hash:
            changed_rows.append(row)
    queue = (
        [("new", numero) for numero in new_numbers]
        + [("update", r["numero"]) for r in update_rows]
        + [("status", r["numero"]) for r in detected_status_rows]
        + [("items", r["numero"]) for r in changed_rows]
    )
    total = len(queue)

    if total == 0 and not digest_rows:
        send_idle_message(conn)
        pc_common.write_run_progress(
            "MESSAGING", "RUNNING", percent_done - 1,
            f"Step {step_current}/{step_total}: no new opportunities or status changes to send.",
            step_current=step_current, step_total=step_total,
            item_current=0, item_total=0, records_new=0,
        )
        return 0

    sent = 0
    skipped = 0

    if digest_rows:
        pc_common.write_run_progress(
            "MESSAGING", "RUNNING", percent_base,
            f"Step {step_current}/{step_total}: sending WhatsApp digest for {len(digest_rows)} new records...",
            step_current=step_current, step_total=step_total,
            item_current=0, item_total=total or 1,
            records_new=len(digest_rows),
        )
        digest_messages, digest_announced = announce_index_digest(conn, digest_rows)
        sent += digest_messages
        print(f"WAHA digest: {digest_announced} new record(s) announced in {digest_messages} message(s).")
        if digest_messages and total:
            pace_after_send(0, 1)  # pause before the per-record messages below
    for index, (kind, numero) in enumerate(queue, start=1):
        full_row = fetch_row(conn, numero)
        label = _short_label(full_row) if full_row is not None else numero
        summary = load_detail_summary(full_row["detail_json_path"]) if full_row is not None else {}
        if kind == "update":
            change = full_row["pending_status_change"] if full_row is not None else ""
            if change == "abierta":
                preview = f"🟢 {label} (Ahora Abierta)"
            else:
                verb = STATUS_CHANGE_LABELS.get(change, change or "actualización")
                preview = f"🔄 {label} ({verb})"
        elif kind == "status":
            preview = f"🟡 {label} (estado cambiado)"
        elif kind == "items":
            preview = f"🔵 {label} (items modificados)"
        else:
            match_line = allowed_match_line_for(full_row, summary, "index") if full_row is not None else None
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
            pace_after_send(index, total)
        else:
            skipped += 1

    digest_note = f", {len(digest_rows)} new in digest" if digest_rows else ""
    pc_common.write_run_progress(
        "MESSAGING", "RUNNING", percent_done,
        f"Step {step_current}/{step_total}: WhatsApp done — {sent} sent, {skipped} skipped of {total}{digest_note} ({len(new_numbers)} new, {len(update_rows) + len(detected_status_rows)} status updates, {len(changed_rows)} item changes).",
        step_current=step_current, step_total=step_total,
        item_current=total, item_total=total,
        records_new=len(digest_rows) or len(new_numbers), records_saved=sent,
    )
    print(f"WAHA announce complete: {sent} sent, {skipped} skipped of {total}{digest_note}.")
    return sent


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="PanamaCompra WAHA new-record notifier")
    parser.add_argument("--idle", action="store_true", help="send the 'Sin nuevas entradas' status (run found no new records)")
    parser.add_argument("--flush", action="store_true", help="announce any records not yet sent (safety net)")
    parser.add_argument("--announce", action="store_true", help="announce every new record one by one, publishing per-message monitor progress (the visible MESSAGING step, right after the index)")
    parser.add_argument("--announce-details", action="store_true", help="send the follow-up detail message (real items) for records announced from the index whose detail page is now downloaded (second MESSAGING step)")
    parser.add_argument("--sync-snapshots", action="store_true", help="silently refresh notified snapshots (and export calendars) for records announced since --since; fallback when the detail phase is disabled")
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

    if args.announce_details:
        ensure_detail_baseline(conn, args.since)
        if not just_baselined:
            announce_details_with_progress(conn)
        return 0

    if args.idle:
        if just_baselined:
            # Right after establishing the baseline, do not claim "no new entries".
            return 0
        send_idle_message(conn)
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
