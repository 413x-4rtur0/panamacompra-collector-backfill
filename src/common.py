#!/usr/bin/env python3
import csv
import json
import os
import re
import sqlite3
import unicodedata
from pathlib import Path
from datetime import datetime

# This module lives at <repo_root>/src/common.py, so the repo root is one
# directory above it.
BASE_DIR = Path(__file__).resolve().parent.parent
APP_ROOT = Path(os.environ.get("APP_ROOT", BASE_DIR)).expanduser().resolve()
APP_MODE = os.environ.get("APP_MODE", "development" if (APP_ROOT / ".git").exists() else "portable")


def env_path(name: str, default: Path, *, base: Path | None = None) -> Path:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    path = Path(raw).expanduser()
    return path if path.is_absolute() else (base or APP_ROOT) / path


def default_state_dir() -> Path:
    if APP_MODE == "installed":
        xdg_data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")).expanduser()
        return xdg_data_home / "panamacompra"
    return APP_ROOT / "var"


def default_config_dir() -> Path:
    if APP_MODE == "installed":
        xdg_config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")).expanduser()
        return xdg_config_home / "panamacompra"
    return APP_ROOT / "config"


STATE_DIR = env_path("PC_STATE_DIR", default_state_dir())
CONFIG_DIR = env_path("PC_CONFIG_DIR", default_config_dir())
DATA_DIR = env_path("PC_DATA_DIR", STATE_DIR / "data")
RECORDS_DIR = env_path("PC_RECORDS_DIR", STATE_DIR / "records")
LOG_DIR = env_path("PC_LOG_DIR", DATA_DIR / "logs")
RUN_DIR = env_path("PC_RUN_DIR", STATE_DIR / "run")
QUEUE_DIR = env_path("PC_QUEUE_DIR", DATA_DIR / "queue")
DB_PATH = env_path("PC_ARCHIVE_DB_PATH", DATA_DIR / "panamacompra_archive.db")
CSV_PATH = env_path("PC_INDEX_CSV_PATH", DATA_DIR / "panamacompra_index.csv")
# Runtime settings written by the monitors (monitor_settings.env, waha_chat_id.txt,
# waha_keywords.txt, ...). Distinct from CONFIG_DIR above, which holds the
# bootstrap/override config (defaults.env, .env) resolved before DATA_DIR exists.
DATA_CONFIG_DIR = DATA_DIR / "config"
# Combined ICS calendar (every event) for a single Thunderbird subscription.
CALENDAR_DIR = env_path("PC_CALENDAR_DIR", DATA_DIR / "calendar")
COMBINED_CALENDAR_PATH = env_path("PC_COMBINED_CALENDAR_PATH", CALENDAR_DIR / "panamacompra.ics")
# Per-record .ics exports written after a successful WhatsApp notification.
CALENDAR_EXPORT_DIR = env_path("PC_CALENDAR_EXPORT_DIR", DATA_DIR / "calendar_exports")
# Testing zone: an isolated sandbox so the last N records can be re-run with the
# current code without touching the real archive (records/) or DB.
RECORDS_TEST_DIR = env_path("PC_RECORDS_TEST_DIR", STATE_DIR / "records_test")

BASE_URL = "https://www.panamacompra.gob.pa/Inicio/#/cotizaciones-en-linea/cotizaciones-en-linea"


PROGRESS_PATH = LOG_DIR / "run_all_progress.env"

def shell_quote(value):
    """Return a single-quoted shell value for monitor progress env files."""
    return "'" + str(value).replace("'", "'\\''") + "'"

def write_run_progress(
    phase,
    status,
    percent,
    message,
    *,
    started_at=None,
    detail_limit=None,
    index_limit=None,
    eta=None,
    step_current=None,
    step_total=None,
    item_current=None,
    item_total=None,
    records_found=None,
    records_new=None,
    records_existing=None,
    records_saved=None,
    records_failed=None,
    records_pending=None,
    records_test=None,
    mode=None,
    extra=None,
):
    """Atomically publish run-all progress for the terminal monitor.

    The shell monitor reads this simple env file. Collectors call this while
    they run so the monitor can show measured progress, current step, counters,
    and diagnostics instead of only a coarse phase estimate.
    """
    ensure_dirs()
    started_at = started_at or os.environ.get("PC_RUN_STARTED_AT", "")
    detail_limit = detail_limit if detail_limit is not None else os.environ.get("PC_DETAIL_LIMIT", "-")
    if index_limit is None:
        index_limit = os.environ.get("PC_INDEX_LIMIT", os.environ.get("PC_MAX_PAGES_PER_GROUP", "-"))

    fields = {
        "PHASE": phase,
        "STATUS": status,
        "PERCENT": max(0, min(100, int(percent))),
        "MESSAGE": message,
        "INDEX_LIMIT": index_limit,
        "ETA": eta if eta is not None else "-",
        "DETAIL_LIMIT": detail_limit,
        "STARTED_AT": started_at,
        "UPDATED_AT": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "WORKER_PID": os.environ.get("PC_WORKER_PID", "-"),
        "MODE": mode or os.environ.get("PC_RUN_MODE", "IDLE"),
        "STEP_CURRENT": step_current if step_current is not None else "-",
        "STEP_TOTAL": step_total if step_total is not None else "-",
        "ITEM_CURRENT": item_current if item_current is not None else "-",
        "ITEM_TOTAL": item_total if item_total is not None else "-",
        "RECORDS_FOUND": records_found if records_found is not None else "-",
        "RECORDS_NEW": records_new if records_new is not None else "-",
        "RECORDS_EXISTING": records_existing if records_existing is not None else "-",
        "RECORDS_SAVED": records_saved if records_saved is not None else "-",
        "RECORDS_FAILED": records_failed if records_failed is not None else "-",
        "RECORDS_PENDING": records_pending if records_pending is not None else "-",
        "RECORDS_TEST": records_test if records_test is not None else "-",
        "EXTRA": extra if extra is not None else "-",
    }

    tmp = PROGRESS_PATH.with_suffix(PROGRESS_PATH.suffix + ".tmp")
    tmp.write_text("".join(f"{key}={shell_quote(value)}\n" for key, value in fields.items()), encoding="utf-8")
    tmp.replace(PROGRESS_PATH)


# --- Run timing history + count-based ETA ---------------------------------
# Collectors persist how long their last run took per unit of work (one detail
# page, one index page) so the *next* run can show an ETA from its very first
# item, before it has measured its own pace. Each run also refines the estimate
# live from its own elapsed time. The ETA the monitor shows is therefore the
# remaining index/detail items still to handle times the seconds each takes.
DETAIL_TIMING_PATH = LOG_DIR / "detail_timing.env"
INDEX_TIMING_PATH = LOG_DIR / "index_timing.env"


def _read_env_file(path):
    data = {}
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return data
    for line in text.splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip().strip("'\"")
    return data


def _write_env_file(path, values):
    ensure_dirs()
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("".join(f"{key}={shell_quote(value)}\n" for key, value in values.items()), encoding="utf-8")
    tmp.replace(path)


def format_duration(seconds):
    """Human 'Xh YYm' / 'Xm YYs' for ETA/elapsed display; '-' when unknown."""
    try:
        seconds = int(round(float(seconds)))
    except (TypeError, ValueError):
        return "-"
    if seconds < 0:
        return "-"
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours > 0:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m {secs:02d}s"


def record_phase_timing(path, *, seconds, count):
    """Persist the average seconds-per-item of a finished phase for next time."""
    try:
        count = int(count)
        seconds = float(seconds)
    except (TypeError, ValueError):
        return
    if count <= 0 or seconds < 0:
        return
    _write_env_file(path, {
        "LAST_SECONDS": int(round(seconds)),
        "LAST_COUNT": count,
        "LAST_AVG_SECONDS": round(seconds / count, 3),
        "UPDATED_AT": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })


def previous_phase_avg_seconds(path):
    """Average seconds-per-item from the last recorded run, or None."""
    try:
        avg = float(_read_env_file(path).get("LAST_AVG_SECONDS", ""))
    except (TypeError, ValueError):
        return None
    return avg if avg > 0 else None


def eta_seconds_from_counts(done, total, elapsed_seconds, prev_avg_seconds=None):
    """Estimate seconds remaining from item counts.

    Uses the live average (elapsed / done) once at least one item is finished,
    otherwise the previous run's per-item average. Returns None when neither is
    available so the caller can fall back to a coarser estimate."""
    remaining = max(0, int(total) - int(done))
    if remaining <= 0:
        return 0
    avg = None
    if done > 0 and elapsed_seconds and elapsed_seconds > 0:
        avg = elapsed_seconds / done
    elif prev_avg_seconds and prev_avg_seconds > 0:
        avg = prev_avg_seconds
    if avg is None:
        return None
    return avg * remaining


def env_int(name, default, minimum=None):
    """Read an integer environment variable with a safe fallback.

    Long-running shell workflows should not crash on a mistyped optional
    setting. Invalid values fall back to the documented default; values below
    ``minimum`` are clamped when a minimum is supplied.
    """
    raw = os.environ.get(name)
    try:
        value = int(raw) if raw not in (None, "") else int(default)
    except (TypeError, ValueError):
        return int(default)

    if minimum is not None and value < minimum:
        return minimum
    return value

INDEX_HEADER = [
    "numero", "grupo", "tipo_url", "estado", "descripcion", "short_description",
    "entidad", "dependencia", "fecha", "modalidad", "link", "first_seen",
    "last_seen", "date_folder", "record_folder", "index_json_path",
    "detail_status", "finish_date_guess"
]

def now_iso():
    return datetime.now().isoformat(timespec="seconds")

def date_folder_name():
    return datetime.now().strftime("%y-%m-%d")

def clean(text):
    return re.sub(r"\s+", " ", text or "").strip()

def safe_name(text):
    text = clean(text)
    text = re.sub(r"[^A-Za-z0-9._() -]+", "_", text)
    text = re.sub(r"\s+", "_", text).strip("._- ")
    return text[:160] or "unknown"

def short_description(text, max_len=80):
    text = clean(text)
    text = re.sub(r"[^A-Za-z0-9ÁÉÍÓÚÜÑáéíóúüñ .,;:_()/-]+", "", text)
    return text[:max_len].strip()

# --------------------------------------------------------------------------
# Folder-naming helpers: (finish_stamp)-(numero)-(desc_slug)
#
# Example leaf:
#   (2022-10-11_12_00)-(2022-0-12-214-12-CL-008498)-(FRS-126-CMPRS-D-CJ-PLSTC)
# --------------------------------------------------------------------------

DESC_SLUG_MAX = env_int("PC_DESC_SLUG_MAX", "24", minimum=1)
_VOWELS = set("AEIOU")

def strip_accents(text):
    """Map accented characters to ASCII, e.g. plásticas -> plasticas, Ñ -> N."""
    nfkd = unicodedata.normalize("NFKD", str(text or ""))
    return "".join(ch for ch in nfkd if not unicodedata.combining(ch))

def desc_slug(text, max_len=DESC_SLUG_MAX):
    """Build the description token for a folder name.

    Uppercase, strip accents, drop vowels (A E I O U), collapse every run of
    non ``[A-Z0-9]`` characters into one ``-``, then truncate to ``max_len``.
    This keeps network-share folder names shorter and avoids ``--`` / ``---``.

    'FORIS 126: Compras de Caja plásticas' -> 'FRS-126-CMPRS-D-CJ-PLSTC'
    """
    s = strip_accents(text).upper()
    s = "".join(ch for ch in s if ch not in _VOWELS)
    s = "".join(ch if ("A" <= ch <= "Z" or "0" <= ch <= "9") else "-" for ch in s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s[:max_len].strip("-")

def _parse_ddmmyyyy(text):
    """Return the first DD-MM-YYYY / DD/MM/YYYY date as ISO YYYY-MM-DD, else ''."""
    m = re.search(r"(\d{1,2})[-/](\d{1,2})[-/](\d{4})", str(text or ""))
    if not m:
        return ""
    day, month, year = m.group(1), m.group(2), m.group(3)
    return f"{year}-{int(month):02d}-{int(day):02d}"

def _parse_ddmmyyyy_matches(text):
    """All DD-MM-YYYY / DD/MM/YYYY dates in text as (match, ISO date)."""
    out = []
    for match in re.finditer(r"(\d{1,2})[-/](\d{1,2})[-/](\d{4})", str(text or "")):
        day, month, year = match.group(1), match.group(2), match.group(3)
        out.append((match, f"{year}-{int(month):02d}-{int(day):02d}"))
    return out


def _window_datetimes_from_text(window):
    """Return (dtstart, dtend) for one close-window value.

    Handles the PanamaCompra range form seen in details, for example
    ``23-06-2026 a 26-06-2026``. A date-only range means the full first day
    through the full last day: start at 00:00 and end at 23:59. If each date has
    an explicit time, those times are respected. Single-date windows keep the
    legacy behavior: earliest time -> latest time, or noon when no time exists.
    """
    text = str(window or "")
    dates = _parse_ddmmyyyy_matches(text)
    if not dates:
        return "", ""
    if len(dates) >= 2:
        first_match, start_date = dates[0]
        last_match, end_date = dates[-1]
        start_segment = text[first_match.end():last_match.start()]
        end_segment = text[last_match.end():]
        start_times = _parse_times_24h(start_segment)
        end_times = _parse_times_24h(end_segment)
        start_time = start_times[0] if start_times else "00:00"
        end_time = end_times[-1] if end_times else "23:59"
        return f"{start_date}T{start_time}:00", f"{end_date}T{end_time}:00"

    date = dates[0][1]
    times = sorted(_parse_times_24h(text)) or ["12:00"]
    return f"{date}T{times[0]}:00", f"{date}T{times[-1]}:00"


def _parse_times_24h(text):
    """Return every clock time in ``text`` as 24h 'HH:MM', in order.

    Handles 12h AM/PM ('12:00 PM' -> '12:00', '03:00 PM' -> '15:00',
    '12:00 AM' -> '00:00') and leaves bare 24h values as-is.
    """
    out = []
    for hh, mm, ap in re.findall(r"(\d{1,2}):(\d{2})\s*([AaPp]\.?\s*[Mm]\.?)?", str(text or "")):
        hh, mm = int(hh), int(mm)
        ap = ap.lower().replace(".", "").replace(" ", "")
        if ap == "pm" and hh != 12:
            hh += 12
        elif ap == "am" and hh == 12:
            hh = 0
        out.append(f"{hh % 24:02d}:{mm % 60:02d}")
    return out

def find_kv(key_values, *needles):
    """First value whose accent-insensitive lowercased key contains all needles."""
    for key, value in (key_values or {}).items():
        kl = strip_accents(str(key)).lower()
        if value and all(n in kl for n in needles):
            return str(value)
    return ""

def _resolve_close_datetimes(key_values, text):
    """Single source of truth for a record's close window.

    Returns ``(window_text, dtstart, dtend)`` where ``dtstart`` / ``dtend`` are
    ``'YYYY-MM-DDTHH:MM:SS'`` (or ``''`` when no usable date exists).

    This is shared by BOTH the folder-name finish stamp (``compute_finish_stamp``)
    and the calendar DTSTART/DTEND (``_calendar_window_datetimes``) so the date
    encoded in ``(finish)-(numero)-(desc)`` can never diverge from the ``.ics``
    DTEND for the same record.

    Priority (close-before-delivery, structured-before-text):
      1) 'presentación de cotizaciones' / 'cierre' / 'límite' field in key_values
      2) delivery ('entrega') field in key_values
      3) the same close field parsed from the saved detail text
      4) the delivery ('día y hora de entrega') field from the text
      5) an 'entrega ... DD-MM-YYYY' phrase in the text   (date only, noon)
      6) any DD-MM-YYYY date anywhere in the text          (date only, noon)
    For 1-4 the window's own clock times are used (earliest -> DTSTART, latest ->
    DTEND). Date-only ranges such as '23-06-2026 a 26-06-2026' mean the full
    first day through the full last day (00:00 -> 23:59). Single date-only
    windows and broad text fallbacks still use noon.
    """
    key_values = key_values or {}
    text = str(text or "")
    text_fields = parse_detail_fields(text)

    def from_window(window):
        dtstart, dtend = _window_datetimes_from_text(window)
        if not dtend:
            return None
        return window, dtstart, dtend

    for window in (
        _close_window_value(key_values),
        find_kv(key_values, "entrega"),
        _close_window_value(text_fields),
        find_kv(text_fields, "dia", "hora", "entrega"),
    ):
        if window:
            resolved = from_window(window)
            if resolved:
                return resolved

    m = re.search(
        r"entrega[^0-9]{0,40}(\d{1,2}[-/]\d{1,2}[-/]\d{4})",
        strip_accents(text),
        flags=re.IGNORECASE,
    )
    if m:
        date = _parse_ddmmyyyy(m.group(1))
        if date:
            return m.group(1), f"{date}T12:00:00", f"{date}T12:00:00"

    # Last-resort broad text parsing should still preserve a visible date range
    # instead of collapsing it to noon on the first date. This catches details
    # whose labels were not parsed into key/value fields but whose body contains
    # forms like "23-06-2026 a 26-06-2026".
    dtstart, dtend = _window_datetimes_from_text(text)
    if dtend:
        return text, dtstart, dtend
    return "", "", ""

def compute_finish_stamp(key_values, text):
    """Return 'YYYY-MM-DD_HH:MM' for when proposals stop being accepted, or ''.

    Derived from the shared ``_resolve_close_datetimes`` close window (DTEND), so
    the folder-name finish stamp is always the same instant as the calendar's
    DTEND for that record.
    """
    _, _, dtend = _resolve_close_datetimes(key_values, text)
    return f"{dtend[:10]}_{dtend[11:16]}" if dtend else ""

def build_record_folder_leaf(finish_stamp, numero, desc):
    """Compose a readable, network-friendly record folder leaf.

    Use parenthesized tokens instead of square brackets: parentheses remain
    readable on network shares without colliding with shell/glob bracket syntax.
    The three human-scannable parts stay explicit: close date, NUMERO and short
    description.
    """
    stamp = safe_name(str(finish_stamp or "NO-DATE").replace(":", "_"))
    number = safe_name(numero or "NO-NUMERO")
    label = safe_name(desc or "NO-DESC")
    return f"({stamp})-({number})-({label})"

# Words dropped when turning a section heading into a short file identifier.
_SECTION_STOPWORDS = {"de", "la", "del", "el", "los", "las", "y", "en", "a", "para"}

def section_identifier(section, index=0):
    """Short, filename-safe identifier for a table's section/category heading.

    Derived dynamically from the section title scraped from the page (accents
    stripped, stopwords dropped, joined with '-', capped). Falls back to
    'tabla-NNN' when no section heading was detected.
    """
    words = [
        w for w in re.split(r"[^a-z0-9]+", strip_accents(str(section or "")).lower())
        if w and w not in _SECTION_STOPWORDS
    ]
    ident = "-".join(words)[:28].strip("-")
    return ident or f"tabla-{int(index or 0):03d}"



def split_file_label(value):
    """Uppercase filename token used by split table/detail section files."""
    return safe_name(strip_accents(str(value or "SECTION")).replace("_", "-")).upper()


def save_detail_section_jsons(record_folder, numero, sections, overwrite=False):
    """Write major detail views as network-friendly split JSON files.

    Files follow the requested pattern::

      <NUMERO>-DETAIL-000-ALL.json
      <NUMERO>-DETAIL-###-<SECTION>.json

    The ``000-ALL`` file carries every logical section together, while numbered
    section files make summary/items/calendar/fields/links easy to inspect.
    """
    sections_dir = Path(record_folder) / "detail_sections"
    sections_dir.mkdir(parents=True, exist_ok=True)
    n = safe_name(numero)
    if overwrite:
        for old_path in list(sections_dir.glob(f"{n}.*.json")) + list(sections_dir.glob(f"{n}-DETAIL-*.json")):
            old_path.unlink()

    written = 0
    descriptors = {}
    all_path = sections_dir / f"{n}-DETAIL-000-ALL.json"
    all_doc = {"numero": str(numero or ""), "kind": "DETAIL", "sections": sections}
    if overwrite or not all_path.exists():
        all_path.write_text(json.dumps(all_doc, ensure_ascii=False, indent=2), encoding="utf-8")
        written += 1
    descriptors["_all"] = {"file": all_path.name, "index": 0, "label": "ALL", "count": len(sections)}

    for idx, (name, payload) in enumerate(sections.items(), start=1):
        label = split_file_label(name)
        path = sections_dir / f"{n}-DETAIL-{idx:03d}-{label}.json"
        doc = {"numero": str(numero or ""), "kind": "DETAIL", "section": name, "section_index": idx, "data": payload}
        if overwrite or not path.exists():
            path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
            written += 1
        if isinstance(payload, list):
            count = len(payload)
        elif isinstance(payload, dict):
            count = len(payload)
        else:
            count = 1 if payload not in (None, "") else 0
        descriptors[name] = {"file": path.name, "index": idx, "label": label, "count": count}
    return written, descriptors

def save_table_jsons(record_folder, numero, tables, overwrite=False):
    """Write table JSON files using the requested readable split layout.

    Files follow::

      <NUMERO>-TABLE-000-ALL.json
      <NUMERO>-TABLE-###-<SECTION>.json

    The all file stores the complete list. Each numbered section file stores one
    full table (clean rows, raw rows and rows-with-links together).
    """
    tables_dir = Path(record_folder) / "tables"
    tables_dir.mkdir(parents=True, exist_ok=True)
    n = safe_name(numero)
    if overwrite:
        for old_path in list(tables_dir.glob(f"{n}.table*.json")) + list(tables_dir.glob(f"{n}-TABLE-*.json")):
            old_path.unlink()

    normalized_tables = []
    descriptors = []
    for position, table in enumerate(tables or [], start=1):
        idx = int(table.get("table_index") or position)
        section = table.get("section") or ""
        ident = section_identifier(section, idx)
        label = split_file_label(ident)
        doc = {
            "numero": str(numero or ""),
            "kind": "TABLE",
            "table_index": idx,
            "section": section,
            "identifier": ident,
            "headers": table.get("headers", []),
            "rows": table.get("rows", []),
            "key_values": table.get("key_values", {}),
            "links_count": table.get("links_count", 0),
            "links": table.get("links", []),
            "raw_rows": table.get("raw_rows", []),
            "rows_with_links": table.get("rows_with_links", []),
            "raw_rows_with_links": table.get("raw_rows_with_links", []),
        }
        normalized_tables.append(doc)

    written = 0
    all_path = tables_dir / f"{n}-TABLE-000-ALL.json"
    all_doc = {"numero": str(numero or ""), "kind": "TABLE", "tables_count": len(normalized_tables), "tables": normalized_tables}
    if overwrite or not all_path.exists():
        all_path.write_text(json.dumps(all_doc, ensure_ascii=False, indent=2), encoding="utf-8")
        written += 1

    for doc in normalized_tables:
        idx = int(doc["table_index"])
        label = split_file_label(doc["identifier"])
        path = tables_dir / f"{n}-TABLE-{idx:03d}-{label}.json"
        if overwrite or not path.exists():
            path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
            written += 1
        descriptors.append({
            "table_index": idx,
            "section": doc["section"],
            "identifier": doc["identifier"],
            "files": {"all": all_path.name, "section": path.name},
        })
    return written, descriptors

def _ics_escape(value):
    """Escape a value for an iCalendar text property."""
    return (
        str(value or "")
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\n", "\\n")
    )

def _ics_datetime(value):
    """Convert YYYY-MM-DDTHH:MM:SS-ish values to ICS DATE-TIME form."""
    if not value:
        return ""
    digits = re.sub(r"[^0-9]", "", str(value))[:14]
    if len(digits) >= 8:
        return f"{digits[:8]}T{digits[8:14]}" if len(digits) > 8 else digits
    return digits


def _ics_utc_datetime(value):
    stamp = _ics_datetime(value)
    return f"{stamp}Z" if stamp else ""


def _ics_param(value):
    """Escape and quote an iCalendar parameter value."""
    escaped = str(value or "").replace('"', r'\"')
    return f'"{escaped}"'


def _ics_fold_line(line, limit=75):
    """Fold one iCalendar content line at a conservative character limit."""
    line = str(line)
    if len(line) <= limit:
        return [line]
    out = []
    first = True
    while line:
        width = limit if first else limit - 1
        out.append(("" if first else " ") + line[:width])
        line = line[width:]
        first = False
    return out


_ICS_HEADER = [
    "BEGIN:VCALENDAR",
    "VERSION:2.0",
    "PRODID:-//panamacompra-collector//EN",
    "CALSCALE:GREGORIAN",
    "METHOD:PUBLISH",
]

def _vevent_lines(calendar):
    """Folded VEVENT lines (BEGIN/END included) for one calendar object.

    URL carries the record link so calendar apps (Thunderbird) show it as a
    clickable link; the link is also repeated in DESCRIPTION. ATTENDEE lines live
    inside the VEVENT so a multi-event calendar keeps each event's attendees.
    """
    calendar = calendar or {}
    tz = calendar.get("timezone") or CALENDAR_TZ
    fields = [
        ("UID", calendar.get("uid")),
        ("SUMMARY", _ics_escape(calendar.get("summary"))),
        (f"DTSTART;TZID={tz};VALUE=DATE-TIME", _ics_datetime(calendar.get("dtstart"))),
        (f"DTEND;TZID={tz};VALUE=DATE-TIME", _ics_datetime(calendar.get("dtend"))),
        ("DTSTAMP;VALUE=DATE-TIME", _ics_utc_datetime(calendar.get("dtstamp"))),
        ("DESCRIPTION", _ics_escape(calendar.get("description"))),
        ("LOCATION", _ics_escape(calendar.get("location"))),
        ("URL", calendar.get("url_publico") or calendar.get("url_interno")),
    ]
    organizer = calendar.get("organizer") or {}
    if organizer.get("email"):
        params = []
        if organizer.get("name"):
            params.append(f"CN={_ics_param(organizer.get('name'))}")
        if organizer.get("role"):
            params.append(f"ROLE={_ics_param(organizer.get('role'))}")
        param_text = ";" + ";".join(params) if params else ""
        fields.append((f"ORGANIZER{param_text}", f"MAILTO:{organizer.get('email')}"))
    for attendee in calendar.get("attendees") or []:
        fields.append(("ATTENDEE", f"MAILTO:{attendee}"))
    out = ["BEGIN:VEVENT"]
    for key, value in fields:
        if value:
            out.extend(_ics_fold_line(f"{key}:{value}"))
    out.append("END:VEVENT")
    return out

def calendar_to_ics(calendar):
    """Return a VCALENDAR string wrapping one record's VEVENT."""
    lines = list(_ICS_HEADER) + _vevent_lines(calendar) + ["END:VCALENDAR", ""]
    return "\r\n".join(lines)

def calendars_to_ics(calendars):
    """Return one VCALENDAR string containing a VEVENT for every calendar given."""
    lines = list(_ICS_HEADER)
    for calendar in calendars:
        if calendar:
            lines += _vevent_lines(calendar)
    lines += ["END:VCALENDAR", ""]
    return "\r\n".join(lines)

def write_calendar_ics(path, calendar):
    """Write a calendar .ics file for later import/review."""
    path = Path(path)
    path.write_bytes(calendar_to_ics(calendar).encode("utf-8"))
    return path

def key_values_from_rows(rows):
    """For a strictly 2-column table, return {col0: col1}; otherwise {}.

    Every row is used (including the first), so a header-like first row such as
    'Fecha de Publicación' -> '18-06-2026 ...' is captured as a pair too.
    """
    rows = rows or []
    if not rows or any(len(r) != 2 for r in rows):
        return {}
    out = {}
    for key, value in rows:
        key = clean(key)
        if key:
            out[key] = clean(value)
    return out

# --------------------------------------------------------------------------
# Detail views: parse the saved detail text into a structured summary, a
# numbered item list, and a calendar event (an ICS VEVENT expressed as JSON),
# so each record's detail.json carries the same facts the old SUMMARY.csv /
# items.csv / .ics artifacts did, but in one JSON document.
# --------------------------------------------------------------------------

VIEWS_SCHEMA_VERSION = 1
CALENDAR_TZ = os.environ.get("PC_CALENDAR_TZ", "America/Panama")
# Default attendees for the calendar event (overridable, comma-separated).
_DEFAULT_ATTENDEES = "a2gutierrezmora@gmail.com,razelgutierrez@gmail.com"

# Header that introduces the items block in older detail text.
_ITEMS_HEADER_RE = re.compile(r"cantidad.*unidad\s*de\s*medida.*descripcion", re.IGNORECASE)
# Section title that precedes the items grid on the V3 portal ("Ítems de la cotización:").
_ITEMS_SECTION_RE = re.compile(r"item\w*\s+de\s+la\s+cotiz", re.IGNORECASE)
# An item text line: "<qty> <unit> <description>", e.g. "2 Unidad BATERIA 200 Ah".
_ITEM_LINE_RE = re.compile(r"^(\d+(?:[.,]\d+)?)\s+(\S+)\s+(.+)$")

def _norm_label(text):
    """Accent-stripped, lowercased, ':'-trimmed label used to match fields."""
    return clean(strip_accents(str(text or ""))).lower().rstrip(":").strip()

# Known detail labels (normalized via _norm_label), across the older and the V3
# portal layouts. Used to gate the label-on-its-own-line fallback in
# parse_detail_fields; colon/tab pages never reach that path.
_KNOWN_LABELS = {
    # Older portal labels
    "numero", "estado", "modalidad", "lugar",
    "descripcion", "descripcion de la solicitud", "objeto de la contratacion",
    "entidad", "dependencia", "unidad de compra", "direccion",
    "provincia de entrega",
    "nombre", "cargo", "telefono", "correo_electronico", "correo electronico",
    "forma de entrega", "dias de entrega", "forma de pago",
    "dia y hora de entrega", "precio estimado",
    "enlace publico", "enlace interno",
    "fecha y hora presentacion de cotizaciones", "fecha de presentacion",
    "fecha y hora de cierre", "fecha limite", "presentacion de propuestas",
    # V3 portal labels
    "numero de proceso", "tipo de proceso", "titulo",
    "modalidad de adjudicacion", "objeto de contratacion", "proceso posterior",
    "provincia", "direccion de la unidad de compra", "fecha de publicacion",
    "termino de entrega",
}

def _field_pairs_on_line(raw):
    """(label, value) pairs from one raw line.

    Tab-separated 'Label<TAB>Value' (the V3 portal) is tried first — clean()
    collapses tabs, so this must run on the raw line — otherwise a single
    'Label: value' colon split (older portal; values may contain ':').
    """
    if "\t" in raw:
        parts = [clean(p) for p in raw.split("\t") if clean(p)]
        if len(parts) == 2:
            return [(parts[0], parts[1])]
        return []
    line = clean(raw)
    if ":" in line:
        key, value = line.split(":", 1)
        key, value = clean(key), clean(value)
        # Do not mistake the colon inside a clock time for a label separator,
        # e.g. "Fecha y hora presentación ... 23-06-2026 08:00 a ...".
        # Real labels should not already contain a DD-MM-YYYY date.
        if key and value and not re.search(r"\d{1,2}[-/]\d{1,2}[-/]\d{4}", key):
            return [(key, value)]
    return []

def parse_detail_fields(text):
    """Parse the saved detail text into an ordered {label: value} dict.

    Handles the portal renderings seen in the archive:
      * V3: tab-separated 'Label<TAB>Value' on one line.
      * older: 'Label: value' on one line (split once; values may contain ':').
      * older: a label on its own line with the value on the next line.
    Parsing stops at the items section so the items grid is not mined for fields.
    """
    raw_lines = str(text or "").splitlines()
    cut = len(raw_lines)
    for i, raw in enumerate(raw_lines):
        norm = strip_accents(clean(raw))
        if norm and (_ITEMS_SECTION_RE.search(norm) or _ITEMS_HEADER_RE.search(norm)):
            cut = i
            break
    raw_lines = raw_lines[:cut]

    fields = {}
    for raw in raw_lines:
        for key, value in _field_pairs_on_line(raw):
            if key and value and key not in fields:
                fields[key] = value

    # Fallback: a label on its own line with the value on the next (no tab/':').
    # Only used when same-line parsing found no recognizable label, so the tab and
    # colon paths are never disturbed (value lines may contain ':' from clock
    # times, so "no pairs found" is not a reliable trigger).
    if not any(_norm_label(key) in _KNOWN_LABELS for key in fields):
        nextline = [clean(x) for x in raw_lines if clean(x)]
        paired = {}
        for i, line in enumerate(nextline[:-1]):
            if _norm_label(line) in _KNOWN_LABELS and line not in paired:
                value = nextline[i + 1]
                if _norm_label(value) not in _KNOWN_LABELS:
                    paired[line] = value
        if paired:
            fields = paired
    return fields

def _items_header_index(header):
    """Map an items-grid header row to {field: column_index}."""
    idx = {}
    for i, raw in enumerate(header):
        h = _norm_label(raw)
        if h == "r" and "r" not in idx:
            idx["r"] = i
        elif "codigo" in h:
            idx["codigo"] = i
        elif "clasificac" in h:
            idx["clasificacion"] = i
        elif "cantidad" in h:
            idx["cantidad"] = i
        elif "unidad" in h:
            idx["unidad"] = i
        elif "descripcion" in h:
            idx["descripcion"] = i
        elif "ses" in h:
            idx["ses"] = i
    return idx

def _items_from_tables(tables):
    """Numbered items from an items grid among ``tables`` (carrying código /
    clasificación / SES columns), or [] if no such grid is present."""
    for table in tables or []:
        rows = table.get("raw_rows") or []
        if len(rows) < 2:
            continue
        idx = _items_header_index(rows[0])
        if "descripcion" not in idx or "cantidad" not in idx:
            continue
        width = len(rows[0])
        items = []
        for row in rows[1:]:
            if len(row) != width:
                continue
            def cell(name):
                j = idx.get(name)
                return clean(row[j]) if j is not None and j < len(row) else ""
            descripcion = cell("descripcion")
            if not descripcion:
                continue
            items.append({
                "r": cell("r") or str(len(items) + 1),
                "codigo": cell("codigo"),
                "clasificacion": cell("clasificacion"),
                "cantidad": cell("cantidad"),
                "unidad_de_medida": cell("unidad"),
                "descripcion": descripcion,
                "ses": cell("ses"),
            })
        if items:
            return items
    return []

def _items_from_text(text):
    """Numbered items from the 'Cantidad / Unidad de Medida / Descripción' text
    block (quantity, unit and description only; no códigos)."""
    lines = str(text or "").splitlines()
    start = None
    for i, raw in enumerate(lines):
        if _ITEMS_HEADER_RE.search(strip_accents(clean(raw))):
            start = i + 1
            break
    if start is None:
        return []
    items = []
    for raw in lines[start:]:
        line = clean(raw)
        if not line:
            continue
        m = _ITEM_LINE_RE.match(line)
        if not m:
            break
        items.append({
            "r": str(len(items) + 1),
            "codigo": "",
            "clasificacion": "",
            "cantidad": m.group(1),
            "unidad_de_medida": m.group(2),
            "descripcion": clean(m.group(3)),
            "ses": "",
        })
    return items

def parse_detail_items(text, tables=None):
    """Numbered items for a record: prefer the items grid (códigos / clasificación
    from ``tables``), else fall back to the detail-text block."""
    return _items_from_tables(tables) or _items_from_text(text)

def build_summary(fields, numero=""):
    """Curated key facts for detail.json (mirrors the old SUMMARY.csv)."""
    return {
        "numero": find_kv(fields, "numero") or numero,
        "descripcion": find_kv(fields, "descripcion"),
        "objeto_de_la_contratacion": find_kv(fields, "objeto"),
        "entidad": find_kv(fields, "entidad"),
        "dependencia": find_kv(fields, "dependencia"),
        "unidad_de_compra": find_kv(fields, "unidad", "compra"),
        "direccion": find_kv(fields, "direccion"),
        "provincia_de_entrega": find_kv(fields, "provincia", "entrega"),
        "contacto": {
            "nombre": find_kv(fields, "nombre"),
            "cargo": find_kv(fields, "cargo"),
            "telefono": find_kv(fields, "telefono"),
            "correo_electronico": find_kv(fields, "correo"),
        },
        "forma_de_entrega": find_kv(fields, "forma", "entrega"),
        "dias_de_entrega": find_kv(fields, "dias", "entrega") or find_kv(fields, "termino", "entrega"),
        "forma_de_pago": find_kv(fields, "forma", "pago"),
        "dia_y_hora_de_entrega": find_kv(fields, "dia", "hora", "entrega"),
        "precio_estimado": find_kv(fields, "precio"),
        "enlace_publico": find_kv(fields, "enlace", "publico"),
        "enlace_interno": find_kv(fields, "enlace", "interno"),
    }

def _calendar_attendees():
    raw = os.environ.get("PC_CALENDAR_ATTENDEES", _DEFAULT_ATTENDEES)
    return [a.strip() for a in raw.split(",") if a.strip()]

def _close_window_value(fields):
    """Value of the 'presentación de cotizaciones' / cierre / límite field, or ''."""
    for key, value in (fields or {}).items():
        kl = strip_accents(str(key)).lower()
        if value and (
            ("presentaci" in kl and ("cotiza" in kl or "propuesta" in kl))
            or "cierre" in kl
            or "limite" in kl
        ):
            return str(value)
    return ""

def _location(fields):
    """LOCATION as '(Provincia) - (Dirección de la unidad de compra)'."""
    provincia = ""
    for key, value in (fields or {}).items():
        if value and _norm_label(key) == "provincia":
            provincia = str(value)
            break
    provincia = provincia or find_kv(fields, "provincia")
    direccion = find_kv(fields, "direccion")
    parts = [p for p in (provincia, direccion) if p]
    return " - ".join(f"({p})" for p in parts) or "Panamá, PA"

def _calendar_description(fields, items, link=""):
    """Review-friendly ICS description: the link, the description, and the item
    list (without the items-table header)."""
    lines = []
    url = link or find_kv(fields, "enlace", "publico") or find_kv(fields, "enlace", "interno")
    if url:
        lines.append(f"LINK : {url}")
    descripcion = find_kv(fields, "descripcion")
    if descripcion:
        if lines:
            lines.append("")
        lines.append(f"DESCR: {descripcion}")
    if items:
        if lines:
            lines.append("")
        lines.append("ITEMS:")
        for it in items:
            item_line = " ".join(p for p in [
                str(it.get("cantidad") or "").strip(),
                str(it.get("unidad_de_medida") or "").strip(),
                str(it.get("descripcion") or "").strip(),
            ] if p)
            if item_line:
                lines.append(item_line)
    return "\n".join(lines)

def _calendar_window_datetimes(key_values, text=""):
    """Return (source text, dtstart, dtend) for calendar import.

    Thin wrapper over the shared ``_resolve_close_datetimes`` so the calendar's
    DTSTART/DTEND are computed from exactly the same close window (and the same
    inputs) as the folder-name finish stamp.
    """
    return _resolve_close_datetimes(key_values, text)


def build_calendar(fields, items, numero="", dtstamp=None, link="",
                   window_key_values=None, window_text=""):
    """An ICS VEVENT for the record, expressed as JSON.

    DTSTART/DTEND come from the 'Fecha y hora presentación de cotizaciones' /
    cierre / límite window (first/last clock time on its date), falling back to
    the 'Día y Hora de Entrega' window for older records. If a source has a
    single date but no explicit clock time, the event is kept importable at 12:00;
    date-only ranges span 00:00 on the first date through 23:59 on the last.
    """
    numero = find_kv(fields, "numero") or numero
    descripcion = find_kv(fields, "descripcion")
    # Resolve the close window from the table key_values + detail text (the same
    # inputs compute_finish_stamp uses for the folder name) when supplied, so the
    # DTEND here matches the folder's finish stamp. Fall back to the text fields
    # alone for callers that do not pass table key_values.
    kv_for_window = window_key_values if window_key_values is not None else fields
    window, dtstart, dtend = _calendar_window_datetimes(kv_for_window, window_text)
    summary = " / ".join(p for p in [descripcion, f"({numero})" if numero else ""] if p)
    return {
        "uid": f"{numero}@panamacompra" if numero else "",
        "summary": summary,
        "timezone": CALENDAR_TZ,
        "date": dtstart[:10] if dtstart else "",
        "start": dtstart[11:16] if dtstart else "",
        "end": dtend[11:16] if dtend else "",
        "dtstart": dtstart,
        "dtend": dtend,
        "dtstamp": dtstamp or now_iso(),
        "source_window": window,
        "location": _location(fields),
        "organizer": {
            "name": find_kv(fields, "nombre"),
            "role": find_kv(fields, "cargo"),
            "email": find_kv(fields, "correo"),
        },
        "attendees": _calendar_attendees(),
        "url_publico": link or find_kv(fields, "enlace", "publico"),
        "url_interno": find_kv(fields, "enlace", "interno"),
        "precio_estimado": find_kv(fields, "precio"),
        "description": _calendar_description(fields, items, link=link),
    }

def build_detail_views(text, tables=None, numero="", dtstamp=None, link=""):
    """Return (summary, items, calendar, fields) parsed from a record's detail
    text (and optional saved tables for item códigos)."""
    fields = parse_detail_fields(text)
    items = parse_detail_items(text, tables)
    summary = build_summary(fields, numero)
    # Aggregate the per-table key/values the same way the folder-naming path does
    # (naming_fields / rename-record-folders.py), so the calendar DTEND is derived
    # from the same close window as the folder's finish stamp.
    agg_kv = {}
    for table in tables or []:
        agg_kv.update(table.get("key_values", {}))
    calendar = build_calendar(
        fields, items, numero, dtstamp=dtstamp, link=link,
        window_key_values=agg_kv, window_text=text,
    )
    return summary, items, calendar, fields

def rename_record_folder(conn, numero, current_folder, new_leaf):
    """Rename a record folder's leaf and update the DB path columns.

    Returns one of: 'already', 'missing', 'conflict', 'renamed'.
    Does not merge into an existing target and never overwrites files.
    """
    current = Path(current_folder)
    if not current.exists():
        return "missing", current
    if current.name == new_leaf:
        return "already", current

    target = current.parent / new_leaf
    if target.exists():
        return "conflict", target

    current.rename(target)
    n = safe_name(numero)
    index_json = target / f"{n}.json"
    detail_json = target / f"{n}.detail.json"
    conn.execute(
        "UPDATE opportunities SET record_folder = ?, index_json_path = ?, "
        "detail_json_path = ?, record_folder_leaf = ?, db_reviewed_at = ? WHERE numero = ?",
        (str(target), str(index_json), str(detail_json), target.name, now_iso(), numero),
    )
    conn.commit()

    # Repoint the moved detail JSON's file paths so they match the new folder.
    try:
        data = json.loads(detail_json.read_text(encoding="utf-8"))
        data["files"] = {
            "index_json": str(index_json),
            "detail_json": str(detail_json),
            "detail_html": str(target / f"{n}.detail.html"),
            "detail_txt": str(target / f"{n}.detail.txt"),
            "calendar_ics": str(target / f"{n}.calendar.ics"),
            "tables_folder": str(target / "tables"),
        }
        data["folder_renamed_from"] = str(current)
        data["folder_renamed_at"] = now_iso()
        detail_json.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass

    return "renamed", target

def detect_url_type(link):
    link = link or ""
    low = link.lower()
    if "/solicitud-de-cotizacion/" in link:
        return "solicitud-de-cotizacion"
    if "/pliego-de-cargos/" in link:
        return "pliego-de-cargos"
    # Previous-version (v2) public preview URL, e.g. .../v2/#!/vistaPreviaCP?NumLc=...
    if "vistapreviacp" in low or "numlc=" in low:
        return "vista-previa"
    return "unknown"

def browser_executable():
    """Return the path to Firefox executable for Playwright."""
    # Playwright's bundled Firefox is used by default when launching firefox
    # No need to specify executable_path for Playwright-managed browsers
    return None

def ensure_dirs():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    RECORDS_DIR.mkdir(parents=True, exist_ok=True)
    CALENDAR_DIR.mkdir(parents=True, exist_ok=True)
    RECORDS_TEST_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)


def ensure_db_schema(conn):
    """Apply lightweight migrations for databases created by older versions."""
    existing_columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(opportunities)").fetchall()
    }

    migrations = {
        "detail_attempts": "ALTER TABLE opportunities ADD COLUMN detail_attempts INTEGER DEFAULT 0",
        "detail_saved_at": "ALTER TABLE opportunities ADD COLUMN detail_saved_at TEXT",
        "detail_json_path": "ALTER TABLE opportunities ADD COLUMN detail_json_path TEXT",
        "finish_date_guess": "ALTER TABLE opportunities ADD COLUMN finish_date_guess TEXT",
        "start_date_guess": "ALTER TABLE opportunities ADD COLUMN start_date_guess TEXT",
        # Timestamp of the WAHA "new opportunity" WhatsApp notification, used by
        # notify_new_records.py so each record is announced at most once.
        "notified_at": "ALTER TABLE opportunities ADD COLUMN notified_at TEXT",
        # Pending status-change announcement (e.g. "abierta" when a record already
        # announced as Programada moves to the Abiertas list). Cleared once the
        # MESSAGING step sends the update message.
        "pending_status_change": "ALTER TABLE opportunities ADD COLUMN pending_status_change TEXT",
        "last_notified_status": "ALTER TABLE opportunities ADD COLUMN last_notified_status TEXT",
        "last_notified_items_hash": "ALTER TABLE opportunities ADD COLUMN last_notified_items_hash TEXT",
        "last_notified_signature": "ALTER TABLE opportunities ADD COLUMN last_notified_signature TEXT",
        "last_calendar_export_path": "ALTER TABLE opportunities ADD COLUMN last_calendar_export_path TEXT",
        # Maintained by maintain-database.py / detail saves. These make monitor
        # summaries and update checks independent from repeatedly opening every
        # detail JSON file.
        "record_folder_leaf": "ALTER TABLE opportunities ADD COLUMN record_folder_leaf TEXT",
        "files_layout_version": "ALTER TABLE opportunities ADD COLUMN files_layout_version INTEGER DEFAULT 1",
        "detail_sections_count": "ALTER TABLE opportunities ADD COLUMN detail_sections_count INTEGER DEFAULT 0",
        "tables_count": "ALTER TABLE opportunities ADD COLUMN tables_count INTEGER DEFAULT 0",
        "db_reviewed_at": "ALTER TABLE opportunities ADD COLUMN db_reviewed_at TEXT",
    }

    for column, statement in migrations.items():
        if column not in existing_columns:
            conn.execute(statement)

    conn.execute("""
    CREATE INDEX IF NOT EXISTS idx_opportunities_detail_queue
    ON opportunities(detail_status, detail_attempts, first_seen)
    """)
    conn.execute("""
    CREATE INDEX IF NOT EXISTS idx_opportunities_last_seen
    ON opportunities(last_seen)
    """)
    conn.execute("""
    CREATE INDEX IF NOT EXISTS idx_opportunities_finish_date
    ON opportunities(finish_date_guess)
    """)
    conn.execute("""
    CREATE INDEX IF NOT EXISTS idx_opportunities_start_date
    ON opportunities(start_date_guess)
    """)
    conn.execute("""
    CREATE INDEX IF NOT EXISTS idx_opportunities_layout_version
    ON opportunities(files_layout_version)
    """)

def init_db(db_path=None):
    ensure_dirs()

    conn = sqlite3.connect(db_path or DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=30000;")

    conn.execute("""
    CREATE TABLE IF NOT EXISTS opportunities (
        numero TEXT PRIMARY KEY,
        grupo TEXT,
        tipo_url TEXT,
        estado TEXT,
        descripcion TEXT,
        short_description TEXT,
        entidad TEXT,
        dependencia TEXT,
        fecha TEXT,
        modalidad TEXT,
        link TEXT,
        first_seen TEXT,
        last_seen TEXT,
        date_folder TEXT,
        record_folder TEXT,
        index_json_path TEXT,
        detail_status TEXT DEFAULT 'pending',
        detail_attempts INTEGER DEFAULT 0,
        detail_saved_at TEXT,
        detail_json_path TEXT,
        finish_date_guess TEXT,
        start_date_guess TEXT
    )
    """)

    ensure_db_schema(conn)

    conn.commit()

    # Only the main archive DB maintains the first-seen CSV; sandbox/in-memory
    # DBs (e.g. the testing zone) must not touch it.
    if (db_path or DB_PATH) == DB_PATH and not CSV_PATH.exists():
        with CSV_PATH.open("w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(INDEX_HEADER)

    return conn

def get_record_folder(date_folder, numero):
    return RECORDS_DIR / date_folder / safe_name(numero)

def archive_index_json_path(record_folder, numero):
    return record_folder / f"{safe_name(numero)}.json"

def archive_detail_json_path(record_folder, numero):
    return record_folder / f"{safe_name(numero)}.detail.json"

def find_existing_record_archive(numero, records_dir=RECORDS_DIR, date_folder=None):
    """Find an on-disk archive folder for ``numero`` even after folder renames.

    The immutable index JSON keeps the stable ``<NUMERO>.json`` filename inside
    both old ``records/YY-MM-DD/NUMERO/`` leaves and renamed
    ``records/YY-MM-DD/(finish)-(numero)-(desc)/`` leaves.  Use that file as
    the source of truth so a rebuilt/empty DB does not create a duplicate
    ``NUMERO`` folder just because the original leaf was renamed.
    """
    n = safe_name(numero)
    root = Path(records_dir)
    day_dirs = [root / date_folder] if date_folder else sorted(root.glob("*"))
    for day_dir in day_dirs:
        if not day_dir.is_dir():
            continue
        direct = day_dir / n / f"{n}.json"
        if direct.exists():
            return direct.parent, direct
        for index_json in sorted(day_dir.glob(f"*/{n}.json")):
            if index_json.is_file():
                return index_json.parent, index_json
    return None, None

def archive_complete(record_folder, numero):
    """Return True only when the on-disk archive has the current file set.

    Older migrated records may have just the index JSON, or detail HTML/text
    without the newer summary/calendar views and ``.ics`` companion file. Treat
    those as incomplete so update/backfill tools re-download or rebuild them
    instead of leaving old/incomplete folders stuck forever.
    """
    n = safe_name(numero)
    detail_json = record_folder / f"{n}.detail.json"
    required = [
        record_folder / f"{n}.json",
        detail_json,
        record_folder / f"{n}.detail.html",
        record_folder / f"{n}.detail.txt",
        record_folder / f"{n}.calendar.ics",
    ]
    if not record_folder.exists() or not all(path.exists() for path in required):
        return False
    try:
        data = json.loads(detail_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return all(key in data for key in ("summary", "items", "calendar", "views_schema_version"))

def write_json_once(path, data):
    if path.exists():
        return False
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return True

def write_text_once(path, text):
    if path.exists():
        return False
    path.write_text(text, encoding="utf-8", errors="ignore")
    return True

def find_existing_opportunity(conn, numero):
    return conn.execute("SELECT * FROM opportunities WHERE numero = ?", (numero,)).fetchone()

def append_index_csv(row):
    # Append-only "first seen" log: written once when a NUMERO is first inserted
    # and never updated afterwards. It does NOT reflect later changes to
    # last_seen or detail_status. Use the SQLite DB for current state.
    with CSV_PATH.open("a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow([
            row.get("numero"),
            row.get("grupo"),
            row.get("tipo_url"),
            row.get("estado"),
            row.get("descripcion"),
            row.get("short_description"),
            row.get("entidad"),
            row.get("dependencia"),
            row.get("fecha"),
            row.get("modalidad"),
            row.get("link"),
            row.get("first_seen"),
            row.get("last_seen"),
            row.get("date_folder"),
            row.get("record_folder"),
            row.get("index_json_path"),
            row.get("detail_status"),
            row.get("finish_date_guess"),
        ])

def insert_or_update_index(conn, row):
    existing = find_existing_opportunity(conn, row["numero"])

    if existing:
        # Detect a status transition worth announcing: a record that was already
        # announced (notified_at set) and moves from the Programadas list to the
        # Abiertas list. (Cancelada is a planned future transition.) The flag is
        # consumed by the MESSAGING step. COALESCE keeps any flag still pending.
        old_grupo = (existing["grupo"] or "").strip().lower()
        new_grupo = (row["grupo"] or "").strip().lower()
        already_announced = bool(existing["notified_at"]) if "notified_at" in existing.keys() else False
        status_change = None
        if already_announced and old_grupo.startswith("programad") and new_grupo.startswith("abiert"):
            status_change = "abierta"

        # Do not rewrite files. Only update lightweight DB tracking.
        conn.execute("""
        UPDATE opportunities
        SET grupo = ?,
            estado = ?,
            descripcion = ?,
            short_description = ?,
            entidad = ?,
            dependencia = ?,
            fecha = ?,
            modalidad = ?,
            link = ?,
            last_seen = ?,
            tipo_url = ?,
            pending_status_change = COALESCE(?, pending_status_change)
        WHERE numero = ?
        """, (
            row["grupo"],
            row["estado"],
            row["descripcion"],
            row["short_description"],
            row["entidad"],
            row["dependencia"],
            row["fecha"],
            row["modalidad"],
            row["link"],
            row["last_seen"],
            row["tipo_url"],
            status_change,
            row["numero"],
        ))
        conn.commit()
        return "existing"

    conn.execute("""
    INSERT INTO opportunities (
        numero, grupo, tipo_url, estado, descripcion, short_description,
        entidad, dependencia, fecha, modalidad, link, first_seen, last_seen,
        date_folder, record_folder, index_json_path, detail_status,
        finish_date_guess
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        row["numero"],
        row["grupo"],
        row["tipo_url"],
        row["estado"],
        row["descripcion"],
        row["short_description"],
        row["entidad"],
        row["dependencia"],
        row["fecha"],
        row["modalidad"],
        row["link"],
        row["first_seen"],
        row["last_seen"],
        row["date_folder"],
        row["record_folder"],
        row["index_json_path"],
        row["detail_status"],
        row["finish_date_guess"],
    ))

    conn.commit()
    append_index_csv(row)
    return "new"

def guess_finish_date_from_text(text):
    text = text or ""
    patterns = [
        r"Fecha\s+de\s+presentaci[oó]n\s*:?\s*([0-9]{1,2}[-/][0-9]{1,2}[-/][0-9]{4}(?:\s+[0-9]{1,2}:[0-9]{2}\s*(?:AM|PM|a\.?\s*m\.?|p\.?\s*m\.?)?)?)",
        r"Fecha\s+y\s+hora\s+de\s+cierre\s*:?\s*([0-9]{1,2}[-/][0-9]{1,2}[-/][0-9]{4}(?:\s+[0-9]{1,2}:[0-9]{2}\s*(?:AM|PM|a\.?\s*m\.?|p\.?\s*m\.?)?)?)",
        r"Fecha\s+l[ií]mite\s*:?\s*([0-9]{1,2}[-/][0-9]{1,2}[-/][0-9]{4}(?:\s+[0-9]{1,2}:[0-9]{2}\s*(?:AM|PM|a\.?\s*m\.?|p\.?\s*m\.?)?)?)",
        r"Presentaci[oó]n\s+de\s+propuestas\s*:?\s*([0-9]{1,2}[-/][0-9]{1,2}[-/][0-9]{4}(?:\s+[0-9]{1,2}:[0-9]{2}\s*(?:AM|PM|a\.?\s*m\.?|p\.?\s*m\.?)?)?)",
    ]

    for pat in patterns:
        m = re.search(pat, text, flags=re.IGNORECASE)
        if m:
            return clean(m.group(1))

    return ""

def update_detail_status(conn, numero, status, detail_json_path=None,
                         finish_date_guess=None, start_date_guess=None,
                         increment_attempts=True):
    """Update a record's detail status.

    ``detail_attempts`` counts genuine download attempts so a permanently broken
    URL is eventually abandoned (``MAX_DETAIL_ATTEMPTS``). Pass
    ``increment_attempts=False`` for no-op transitions (re-completing an
    already-saved archive, or refreshing views from saved HTML) so those do not
    burn the retry budget — that was previously starving records that only needed
    a missing ``.ics`` / view regenerated and left them stuck incomplete.
    """
    conn.execute("""
    UPDATE opportunities
    SET detail_status = ?,
        detail_attempts = detail_attempts + ?,
        detail_saved_at = ?,
        detail_json_path = COALESCE(?, detail_json_path),
        finish_date_guess = COALESCE(NULLIF(?, ''), finish_date_guess),
        start_date_guess = COALESCE(NULLIF(?, ''), start_date_guess)
    WHERE numero = ?
    """, (
        status,
        1 if increment_attempts else 0,
        now_iso() if status == "saved" else None,
        str(detail_json_path) if detail_json_path else None,
        finish_date_guess or "",
        start_date_guess or "",
        numero,
    ))
    conn.commit()
