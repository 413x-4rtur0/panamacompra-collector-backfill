#!/usr/bin/env python3
import csv
import json
import os
import re
import signal
import sqlite3
import unicodedata
from pathlib import Path
from datetime import datetime

# This module lives at <repo_root>/src/common.py, so the repo root is one
# directory above it.
BASE_DIR = Path(__file__).resolve().parent.parent
APP_ROOT = Path(os.environ.get("APP_ROOT", BASE_DIR)).expanduser().resolve()
APP_MODE = os.environ.get("APP_MODE", "development" if (APP_ROOT / ".git").exists() else "portable")


def load_script(relative_path: str, module_name: str | None = None):
    """Import a numbered script (###-feature.py, dashes in the file name) as a
    Python module. Used everywhere a script is reused as a library, since
    numbered names cannot be imported with a plain `import` statement."""
    import importlib.util

    path = APP_ROOT / relative_path
    name = module_name or Path(relative_path).stem.replace("-", "_")
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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

    effective_mode = str(mode or os.environ.get("PC_RUN_MODE", "IDLE"))
    run_type = os.environ.get("PC_RUN_TYPE") or ("TEST" if effective_mode.upper() == "TEST" else "COLLECTOR")
    run_source = os.environ.get("PC_RUN_SOURCE") or os.environ.get("PC_PRIORITY_SOURCE") or ("manual-test" if run_type == "TEST" else "unknown")
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
        "MODE": effective_mode,
        "RUN_TYPE": run_type,
        "RUN_SOURCE": run_source,
        "RUN_TRIGGER": os.environ.get("PC_RUN_TRIGGER", "unknown"),
        "TEST_AUTORUN": os.environ.get("PC_TEST_AUTORUN", "0"),
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


class WatchdogTimeout(Exception):
    """Raised by run_with_watchdog() when the wrapped call overran its
    budget."""


def run_with_watchdog(seconds, fn, *args, **kwargs):
    """Run fn(*args, **kwargs) under a hard wall-clock deadline (SIGALRM),
    raising WatchdogTimeout instead of letting it run forever.

    Exists because a Playwright/Firefox call can wedge at the browser-
    connection level, not just the page level -- when that happens, the
    per-operation timeouts already inside process_detail()/cuadro fetching
    (page.goto, inner_text, ...) never fire, because they assume the browser
    process itself is still responsive enough to honor a cancellation. A
    background collector that hits this hangs forever, holding its lock and
    silently freezing that whole priority lane until someone notices and
    kills it by hand -- confirmed happening in production (037b, 2026-07-30).
    Callers should close and relaunch their browser after a WatchdogTimeout,
    since the browser itself is presumed wedged, not just the one page.

    Unix only (SIGALRM) and main-thread only -- true for every caller here,
    each a single-threaded per-record collector loop."""
    def _on_alarm(signum, frame):
        raise WatchdogTimeout(f"exceeded {seconds}s")

    previous_handler = signal.signal(signal.SIGALRM, _on_alarm)
    signal.alarm(seconds)
    try:
        return fn(*args, **kwargs)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous_handler)


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
#   (2022-10-11_12-00)-(2022-0-12-214-12-CL-008498)-(FRS-126-CMPRS-D-CJ-PLSTC)
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

def date_folder_from_fecha(fecha_text):
    """Derive the YY-MM-DD day folder from the portal's own FECHA field.

    Backfilled/snapshot-imported records are inserted long after their real
    publish date, so date_folder_name() (today) would file them under the
    processing day instead of the day the record actually appeared on the
    portal. Returns '' when fecha doesn't parse, so callers can fall back to
    date_folder_name() for the rare row missing a usable date."""
    iso = _parse_ddmmyyyy(fecha_text)
    return iso[2:] if iso else ""

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

def date_folder_from_finish_stamp(finish_stamp):
    """Derive the existing YY-MM-DD parent folder from a finish stamp."""
    match = re.search(r"(\d{4})-(\d{2})-(\d{2})", str(finish_stamp or ""))
    if not match:
        return ""
    return f"{match.group(1)[2:]}-{match.group(2)}-{match.group(3)}"

def build_record_folder_leaf(finish_stamp, numero, desc):
    """Compose a readable, network-friendly record folder leaf.

    Use parenthesized tokens instead of square brackets: parentheses remain
    readable on network shares without colliding with shell/glob bracket syntax.
    The three human-scannable parts stay explicit: close date, NUMERO and short
    description.
    """
    stamp = safe_name(str(finish_stamp or "NO-DATE").replace(":", "-"))
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
    path.parent.mkdir(parents=True, exist_ok=True)
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
    # (naming_fields / 070-rename-record-folders.py), so the calendar DTEND is derived
    # from the same close window as the folder's finish stamp.
    agg_kv = {}
    for table in tables or []:
        agg_kv.update(table.get("key_values", {}))
    calendar = build_calendar(
        fields, items, numero, dtstamp=dtstamp, link=link,
        window_key_values=agg_kv, window_text=text,
    )
    return summary, items, calendar, fields

def rename_record_folder(conn, numero, current_folder, new_leaf, new_date_folder=None):
    """Rename a record folder's leaf and update the DB path columns.

    Returns one of: 'already', 'missing', 'conflict', 'renamed'.
    Does not merge into an existing target and never overwrites files.
    """
    current = Path(current_folder)
    if not current.exists():
        return "missing", current
    target_parent = current.parent
    if new_date_folder:
        target_parent = current.parent.parent / safe_name(new_date_folder)
    if current.parent == target_parent and current.name == new_leaf:
        return "already", current

    target_parent.mkdir(parents=True, exist_ok=True)
    target = target_parent / new_leaf
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
        # 020-notify-whatsapp.py so each record is announced at most once.
        "notified_at": "ALTER TABLE opportunities ADD COLUMN notified_at TEXT",
        # Timestamp of the follow-up WhatsApp message with the downloaded item
        # details (second notifier phase). NULL = full-detail message still owed
        # for a record already announced from the index.
        "detail_notified_at": "ALTER TABLE opportunities ADD COLUMN detail_notified_at TEXT",
        # Pending status-change announcement (e.g. "abierta" when a record already
        # announced as Programada moves to the Abiertas list). Cleared once the
        # MESSAGING step sends the update message.
        "pending_status_change": "ALTER TABLE opportunities ADD COLUMN pending_status_change TEXT",
        # WhatsApp delivery tracking (audit Phase 3): attempts increment on
        # every record-level send try; notify_error keeps the LAST failure
        # reason and is cleared by the next successful send. Together they let
        # the monitors distinguish "never attempted" from "attempted and failed".
        "notify_attempts": "ALTER TABLE opportunities ADD COLUMN notify_attempts INTEGER DEFAULT 0",
        "notify_error": "ALTER TABLE opportunities ADD COLUMN notify_error TEXT",
        "last_notified_status": "ALTER TABLE opportunities ADD COLUMN last_notified_status TEXT",
        "last_notified_items_hash": "ALTER TABLE opportunities ADD COLUMN last_notified_items_hash TEXT",
        "last_notified_signature": "ALTER TABLE opportunities ADD COLUMN last_notified_signature TEXT",
        "last_calendar_export_path": "ALTER TABLE opportunities ADD COLUMN last_calendar_export_path TEXT",
        # Maintained by 050-maintain-database.py / detail saves. These make monitor
        # summaries and update checks independent from repeatedly opening every
        # detail JSON file.
        "record_folder_leaf": "ALTER TABLE opportunities ADD COLUMN record_folder_leaf TEXT",
        "files_layout_version": "ALTER TABLE opportunities ADD COLUMN files_layout_version INTEGER DEFAULT 1",
        "detail_sections_count": "ALTER TABLE opportunities ADD COLUMN detail_sections_count INTEGER DEFAULT 0",
        "tables_count": "ALTER TABLE opportunities ADD COLUMN tables_count INTEGER DEFAULT 0",
        "db_reviewed_at": "ALTER TABLE opportunities ADD COLUMN db_reviewed_at TEXT",
        # Closed + cuadro de cotizacion background crawl (037/038-collect-*):
        # mirrors detail_status/detail_attempts/detail_saved_at/detail_json_path
        # exactly, but for the separate low-frequency background pipeline that
        # fetches the price-comparison table once a record closes. NULL means
        # "not a Cerrada (or not processed by that pipeline yet)"; 'no_bids'
        # covers a closed opportunity whose cuadro shows zero proponentes
        # (nothing to store, but never worth re-attempting).
        "cotizacion_status": "ALTER TABLE opportunities ADD COLUMN cotizacion_status TEXT",
        "cotizacion_attempts": "ALTER TABLE opportunities ADD COLUMN cotizacion_attempts INTEGER DEFAULT 0",
        "cotizacion_saved_at": "ALTER TABLE opportunities ADD COLUMN cotizacion_saved_at TEXT",
        "cotizacion_json_path": "ALTER TABLE opportunities ADD COLUMN cotizacion_json_path TEXT",
        # The "Ver documento" -> cuadro-de-cotizaciones URL discovered on the
        # solicitud-de-cotizacion detail page, cached so a retry never needs to
        # re-fetch that page just to rediscover the same link.
        "cuadro_link": "ALTER TABLE opportunities ADD COLUMN cuadro_link TEXT",
        # Short lowercase code for the record's last grupo/estado transition
        # (see status_change_code) plus when it was set and, for a "cerrada"
        # code, when the closure was detected. Kept in sync by
        # insert_or_update_index/reconcile_legacy_status_history so grupo
        # never drifts from what the platform actually reports.
        "status_flag": "ALTER TABLE opportunities ADD COLUMN status_flag TEXT",
        "status_updated_at": "ALTER TABLE opportunities ADD COLUMN status_updated_at TEXT",
        "closed_at": "ALTER TABLE opportunities ADD COLUMN closed_at TEXT",
    }

    for column, statement in migrations.items():
        if column not in existing_columns:
            conn.execute(statement)

    # Append-only audit trail of every grupo/estado transition observed for a
    # record, written by insert_or_update_index (source = whichever pipeline
    # saw it) and by reconcile_legacy_status_history (source =
    # 'legacy-reconcile') for transitions that happened out of band. Powers
    # both the WhatsApp status digest and status_flag/closed_at bookkeeping.
    conn.execute("""
    CREATE TABLE IF NOT EXISTS opportunity_status_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        numero TEXT NOT NULL,
        observed_at TEXT NOT NULL,
        source TEXT NOT NULL DEFAULT 'index',
        previous_grupo TEXT,
        previous_estado TEXT,
        new_grupo TEXT,
        new_estado TEXT,
        change_code TEXT NOT NULL,
        notification_state TEXT NOT NULL DEFAULT 'not_required',
        notified_at TEXT,
        closure_at TEXT
    )
    """)
    conn.execute("""
    CREATE INDEX IF NOT EXISTS idx_status_history_numero
    ON opportunity_status_history(numero, observed_at)
    """)
    conn.execute("""
    CREATE INDEX IF NOT EXISTS idx_status_history_pending
    ON opportunity_status_history(notification_state, observed_at)
    """)
    conn.execute("""
    CREATE INDEX IF NOT EXISTS idx_status_history_numero_closure
    ON opportunity_status_history(numero, closure_at, observed_at)
    """)

    # One row per (numero, item_index, proponente): a single provider's bid on
    # a single line item within one closed opportunity's cuadro de
    # cotizaciones. Deliberately denormalized (item_descripcion repeated per
    # bidder row) rather than split into a separate items table — every query
    # this feeds (per-item min/avg/max price across bidders) groups by
    # numero+item_index anyway, so a join back to a separate items table would
    # only add cost, not save any.
    conn.execute("""
    CREATE TABLE IF NOT EXISTS cotizacion_bids (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        numero TEXT NOT NULL,
        item_index INTEGER NOT NULL,
        item_descripcion TEXT,
        especificaciones_comprador TEXT,
        cantidad_solicitada TEXT,
        unidad_medida TEXT,
        proponente TEXT NOT NULL,
        especificaciones_proponente TEXT,
        cantidad_cotizada TEXT,
        precio_unitario REAL,
        monto_neto REAL,
        impuestos TEXT,
        collected_at TEXT NOT NULL,
        UNIQUE(numero, item_index, proponente)
    )
    """)
    conn.execute("""
    CREATE INDEX IF NOT EXISTS idx_cotizacion_bids_numero
    ON cotizacion_bids(numero)
    """)
    conn.execute("""
    CREATE INDEX IF NOT EXISTS idx_cotizacion_bids_item
    ON cotizacion_bids(item_descripcion)
    """)

    # Single-row resumable cursor for the historical Closed-opportunities
    # backfill (037-collect-closed-index.py --mode backfill): which index
    # page it last finished, so each low-resource background run can pick up
    # a few more pages deeper into the archive instead of re-scanning from
    # page 1 or needing to hold state anywhere but the one archive DB every
    # other part of the pipeline already reads/writes.
    conn.execute("""
    CREATE TABLE IF NOT EXISTS closed_crawl_state (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        grupo TEXT NOT NULL DEFAULT 'Closed',
        backfill_page INTEGER NOT NULL DEFAULT 1,
        backfill_complete INTEGER NOT NULL DEFAULT 0,
        last_forward_run_at TEXT,
        last_backfill_run_at TEXT
    )
    """)
    # Older DBs still have the original single-row schema, including a
    # CHECK (id = 1) constraint that ALTER TABLE cannot drop -- inserting a
    # second row (grupo='Cancelled') for the Cancelled backfill would violate
    # it forever otherwise. Detect that constraint from sqlite_master's own
    # SQL text and rebuild the table without it (standard SQLite pattern:
    # rename, recreate, copy, drop); the id=1 row survives with the same id,
    # so the monitor's own direct "WHERE id = 1" SQL keeps working unchanged.
    old_schema_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='closed_crawl_state'"
    ).fetchone()
    if old_schema_sql and "CHECK" in (old_schema_sql["sql"] or "").upper():
        conn.execute("ALTER TABLE closed_crawl_state RENAME TO closed_crawl_state_old")
        conn.execute("""
        CREATE TABLE closed_crawl_state (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            grupo TEXT NOT NULL DEFAULT 'Closed',
            backfill_page INTEGER NOT NULL DEFAULT 1,
            backfill_complete INTEGER NOT NULL DEFAULT 0,
            last_forward_run_at TEXT,
            last_backfill_run_at TEXT
        )
        """)
        conn.execute("""
        INSERT INTO closed_crawl_state (id, grupo, backfill_page, backfill_complete,
                                         last_forward_run_at, last_backfill_run_at)
        SELECT id, 'Closed', backfill_page, backfill_complete,
               last_forward_run_at, last_backfill_run_at
        FROM closed_crawl_state_old
        """)
        conn.execute("DROP TABLE closed_crawl_state_old")

    crawl_state_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(closed_crawl_state)").fetchall()
    }
    if "grupo" not in crawl_state_columns:
        conn.execute("ALTER TABLE closed_crawl_state ADD COLUMN grupo TEXT NOT NULL DEFAULT 'Closed'")
    conn.execute("""
    INSERT OR IGNORE INTO closed_crawl_state (id, grupo, backfill_page, backfill_complete)
    VALUES (1, 'Closed', 1, 0)
    """)

    # Local mirror of every outbound WAHA/WhatsApp send (see
    # log_app_notification below), so a client with no phone number — e.g.
    # the Android monitor app — can poll GET /api/notifications for the same
    # events WhatsApp would have received.
    conn.execute("""
    CREATE TABLE IF NOT EXISTS app_notifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL,
        purpose TEXT,
        chat_id TEXT,
        text TEXT NOT NULL
    )
    """)

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


def get_closed_crawl_state(conn, grupo: str = "Closed") -> dict:
    """The resumable backfill cursor for one group (see ensure_db_schema).
    Always returns a row — the 'Closed' row (id=1) is seeded on every DB
    open; any other group's row is created here on first access, since only
    Closed has ever needed a backfill cursor until Cancelled backfill was
    added."""
    row = conn.execute("SELECT * FROM closed_crawl_state WHERE grupo = ?", (grupo,)).fetchone()
    if row is None:
        conn.execute("INSERT OR IGNORE INTO closed_crawl_state (grupo) VALUES (?)", (grupo,))
        conn.commit()
        row = conn.execute("SELECT * FROM closed_crawl_state WHERE grupo = ?", (grupo,)).fetchone()
    return dict(row)


def update_closed_crawl_state(conn, grupo: str = "Closed", **fields) -> None:
    """Partial update of one group's backfill cursor row. Keys must be real
    columns (backfill_page, backfill_complete, last_forward_run_at,
    last_backfill_run_at) — this is an internal helper, not user input."""
    if not fields:
        return
    set_clause = ", ".join(f"{key} = ?" for key in fields)
    conn.execute(f"UPDATE closed_crawl_state SET {set_clause} WHERE grupo = ?",
                 list(fields.values()) + [grupo])
    conn.commit()


def parse_money(value) -> float | None:
    """'B/. 1,021.25' / '---' / '' -> 1021.25 / None / None."""
    if value is None:
        return None
    text = str(value).replace("B/.", "").replace(",", "").strip()
    if not text or text == "---":
        return None
    try:
        return float(text)
    except ValueError:
        return None


def save_cotizacion_bids(conn, numero: str, bids: list[dict]) -> int:
    """Replace every stored bid for ``numero`` with ``bids`` (list of dicts
    with the cotizacion_bids columns minus id/collected_at) — a closed
    opportunity's cuadro never legitimately shrinks, but re-running the
    collector on it (retry, manual refresh) should reflect the page's
    current content exactly rather than accumulate stale rows alongside
    fresh ones. Returns the number of rows written."""
    now = now_iso()
    conn.execute("DELETE FROM cotizacion_bids WHERE numero = ?", (numero,))
    for bid in bids:
        conn.execute(
            "INSERT INTO cotizacion_bids "
            "(numero, item_index, item_descripcion, especificaciones_comprador, "
            "cantidad_solicitada, unidad_medida, proponente, especificaciones_proponente, "
            "cantidad_cotizada, precio_unitario, monto_neto, impuestos, collected_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                numero, bid.get("item_index"), bid.get("item_descripcion"),
                bid.get("especificaciones_comprador"), bid.get("cantidad_solicitada"),
                bid.get("unidad_medida"), bid.get("proponente"), bid.get("especificaciones_proponente"),
                bid.get("cantidad_cotizada"), bid.get("precio_unitario"), bid.get("monto_neto"),
                bid.get("impuestos"), now,
            ),
        )
    conn.commit()
    return len(bids)


def cotizacion_price_stats(conn, *, numero: str = "", item_query: str = "", limit: int = 200) -> list[dict]:
    """Per-item (numero, item_index) price stats across every bidder: min/avg/
    max precio_unitario, bidder count, and the winning (lowest-price)
    proponente. Powers the monitor's cotizaciones tab — filter by exact
    numero and/or a partial, case/accent-insensitive item_descripcion match."""
    where = ["precio_unitario IS NOT NULL"]
    params: list = []
    if numero:
        where.append("numero = ?")
        params.append(numero)
    if item_query:
        where.append("LOWER(item_descripcion) LIKE ? ESCAPE '\\'")
        escaped = item_query.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params.append(f"%{escaped}%")
    sql = f"""
        SELECT numero, item_index, item_descripcion,
               COUNT(*) AS bidder_count,
               MIN(precio_unitario) AS min_price,
               AVG(precio_unitario) AS avg_price,
               MAX(precio_unitario) AS max_price
        FROM cotizacion_bids
        WHERE {" AND ".join(where)}
        GROUP BY numero, item_index
        ORDER BY numero DESC, item_index ASC
        LIMIT ?
    """
    params.append(max(1, min(5000, limit)))
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    for row in rows:
        winner = conn.execute(
            "SELECT proponente FROM cotizacion_bids WHERE numero = ? AND item_index = ? "
            "AND precio_unitario = ? ORDER BY proponente LIMIT 1",
            (row["numero"], row["item_index"], row["min_price"]),
        ).fetchone()
        row["best_proponente"] = winner["proponente"] if winner else None
    return rows


def cotizacion_kpis(conn, limit: int = 10, *, days: int = 0, grupo: str = "",
                     entidad: str = "", location: str = "") -> dict:
    """Aggregate price/provider KPIs across every collected cuadro de
    cotizaciones -- powers the monitor KPI dashboard's 'Cotizaciones
    pricing' card. Never raises; an empty table yields zeros so the card
    renders fine before the first cuadro has ever been collected.

    days/grupo/entidad/location are the SAME dashboard filters
    db_review_stats() applies to every other KPI card, joined here through
    opportunities.numero since cotizacion_bids itself carries no date/
    grupo/entidad columns -- so this card answers for the same filtered
    slice as the rest of the dashboard instead of always showing the
    whole archive's totals.

    total_best_value/total_avg_value sum, per item, the cheapest bid and
    the average bid respectively -- their difference (potential_savings)
    is what always picking the lowest bidder saves versus an average
    choice, a genuine procurement signal rather than a vanity total."""
    empty = {
        "total_bids": 0, "total_items": 0, "opportunities_with_prices": 0,
        "total_best_value": 0.0, "total_avg_value": 0.0, "total_max_value": 0.0, "potential_savings": 0.0,
        "avg_price_spread_pct": 0.0, "top_items_by_value": [], "top_providers": [],
    }

    flt_conditions: list[str] = []
    flt_params: list = []
    if days and int(days) > 0:
        flt_conditions.append(
            "REPLACE(REPLACE(substr(COALESCE(NULLIF(o.first_seen, ''), o.detail_saved_at, ''), 1, 10), '_', '-'), 'T', '') >= date('now', ?)")
        flt_params.append(f"-{int(days)} day")
    if grupo:
        flt_conditions.append("COALESCE(o.grupo, '') = ?")
        flt_params.append(grupo)
    if entidad:
        flt_conditions.append("COALESCE(o.entidad, '') = ?")
        flt_params.append(entidad)
    if location:
        escaped = location.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        location_like = f"%{escaped}%"
        flt_conditions.append("(COALESCE(o.dependencia, '') LIKE ? ESCAPE '\\' OR COALESCE(o.entidad, '') LIKE ? ESCAPE '\\')")
        flt_params.extend([location_like, location_like])
    flt_join = "JOIN opportunities o ON o.numero = cb.numero"
    flt_where = (" WHERE " + " AND ".join(flt_conditions)) if flt_conditions else ""

    total_bids = conn.execute(
        f"SELECT COUNT(*) AS c FROM cotizacion_bids cb {flt_join}{flt_where}", flt_params
    ).fetchone()["c"]
    if not total_bids:
        return empty

    opportunities_with_prices = conn.execute(
        f"SELECT COUNT(DISTINCT cb.numero) AS c FROM cotizacion_bids cb {flt_join}{flt_where}", flt_params
    ).fetchone()["c"]

    per_item = conn.execute(f"""
        SELECT cb.numero, cb.item_index,
               MIN(cb.precio_unitario) AS min_price, AVG(cb.precio_unitario) AS avg_price,
               MIN(cb.monto_neto) AS min_value, AVG(cb.monto_neto) AS avg_value, MAX(cb.monto_neto) AS max_value
        FROM cotizacion_bids cb {flt_join}
        {flt_where}{" AND " if flt_where else " WHERE "}cb.precio_unitario IS NOT NULL AND cb.precio_unitario > 0
        GROUP BY cb.numero, cb.item_index
    """, flt_params).fetchall()
    spreads = [(r["avg_price"] - r["min_price"]) / r["min_price"] * 100 for r in per_item if r["min_price"]]
    avg_price_spread_pct = sum(spreads) / len(spreads) if spreads else 0.0
    total_best_value = sum((r["min_value"] or 0) for r in per_item)
    total_avg_value = sum((r["avg_value"] or 0) for r in per_item)
    total_max_value = sum((r["max_value"] or 0) for r in per_item)

    top_items = conn.execute(f"""
        SELECT cb.numero, cb.item_index, cb.item_descripcion,
               MIN(cb.precio_unitario) AS min_price, AVG(cb.precio_unitario) AS avg_price, MAX(cb.precio_unitario) AS max_price,
               MAX(cb.monto_neto) AS max_value, COUNT(*) AS bidder_count
        FROM cotizacion_bids cb {flt_join}
        {flt_where}{" AND " if flt_where else " WHERE "}cb.precio_unitario IS NOT NULL
        GROUP BY cb.numero, cb.item_index
        ORDER BY max_value DESC
        LIMIT ?
    """, (*flt_params, limit)).fetchall()

    top_providers = conn.execute(f"""
        SELECT cb.proponente, COUNT(*) AS bid_count, COALESCE(SUM(cb.monto_neto), 0) AS total_value
        FROM cotizacion_bids cb {flt_join}{flt_where}
        GROUP BY cb.proponente
        ORDER BY bid_count DESC
        LIMIT ?
    """, (*flt_params, limit)).fetchall()

    return {
        "total_bids": total_bids,
        "total_items": len(per_item),
        "opportunities_with_prices": opportunities_with_prices,
        "total_best_value": round(total_best_value, 2),
        "total_avg_value": round(total_avg_value, 2),
        "total_max_value": round(total_max_value, 2),
        "potential_savings": round(total_avg_value - total_best_value, 2),
        "avg_price_spread_pct": round(avg_price_spread_pct, 1),
        "top_items_by_value": [dict(r) for r in top_items],
        "top_providers": [dict(r) for r in top_providers],
    }


def cotizacion_bids_for_numero(conn, numero: str) -> list[dict]:
    """Every stored bid row for one closed opportunity, flat and ordered by
    item then price (cheapest first) — the full 'cuadro de cotizaciones'
    behind one row of cotizacion_price_stats(), for a monitor drill-down
    into exactly who quoted what on a specific item."""
    rows = conn.execute("""
        SELECT item_index, item_descripcion, especificaciones_comprador,
               cantidad_solicitada, unidad_medida, proponente,
               especificaciones_proponente, cantidad_cotizada,
               precio_unitario, monto_neto, impuestos
        FROM cotizacion_bids
        WHERE numero = ?
        ORDER BY item_index ASC, precio_unitario ASC
    """, (numero,)).fetchall()
    return [dict(r) for r in rows]


def log_app_notification(purpose: str, text: str, chat_id: str = "") -> None:
    """Best-effort local record of an outbound WAHA message.

    Called from waha.send_text() right after a successful send, which is the
    one choke point every WhatsApp message passes through regardless of
    caller (the per-record notifier import or the CLI invocation from
    100-run-worker.sh for system/summary messages) — so this table mirrors
    WhatsApp delivery 1:1 for other clients (e.g. the Android monitor app's
    GET /api/notifications) without duplicating each call site. Never raises:
    a logging failure must not break the actual WhatsApp send.
    """
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        conn.execute(
            "INSERT INTO app_notifications (created_at, purpose, chat_id, text) VALUES (?, ?, ?, ?)",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), purpose, chat_id, text),
        )
        conn.commit()
        conn.close()
    except Exception:  # noqa: BLE001 - logging must never break a notify send
        pass

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
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return True

def write_text_once(path, text):
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
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

_STATUS_CODE_ALIASES = {
    "abierta": "abierta",
    "cerrada": "cerrada",
    "cancelada": "cancelada",
    "programada": "programada",
    "desierta": "desierta",
    "adjudicada": "adjudicada",
}

def status_change_code(old_estado, new_estado, old_grupo, new_grupo):
    """Short lowercase, accent-stripped code for a grupo/estado transition
    (e.g. "Cerrada" -> "cerrada"). Falls back to the new grupo (singular)
    when estado is missing/unrecognized, so a code can still be derived."""
    def normalize(text):
        text = unicodedata.normalize("NFKD", (text or "").strip())
        return "".join(ch for ch in text if not unicodedata.combining(ch)).lower()

    code = normalize(new_estado)
    if code:
        return _STATUS_CODE_ALIASES.get(code, code)

    fallback = normalize(new_grupo)
    return fallback[:-1] if fallback.endswith("s") else fallback

def insert_or_update_index(conn, row, source="index"):
    existing = find_existing_opportunity(conn, row["numero"])

    if existing:
        # Detect a status transition worth announcing: a record that was already
        # announced (notified_at set) and moves from the Programadas list to the
        # Abiertas list. (Cancelada is a planned future transition.) The flag is
        # consumed by the MESSAGING step. COALESCE keeps any flag still pending.
        old_grupo_raw = existing["grupo"] or ""
        old_estado_raw = existing["estado"] or ""
        new_grupo_raw = row["grupo"] or ""
        new_estado_raw = row["estado"] or ""
        already_announced = bool(existing["notified_at"]) if "notified_at" in existing.keys() else False
        transitioned = (old_grupo_raw, old_estado_raw) != (new_grupo_raw, new_estado_raw)

        status_change = None
        change_code = None
        status_updated_at_value = None
        if transitioned:
            change_code = status_change_code(old_estado_raw, new_estado_raw, old_grupo_raw, new_grupo_raw)
            status_updated_at_value = row["last_seen"]
            if already_announced:
                status_change = change_code
            # A real transition always resolves closed_at explicitly: set on a
            # fresh closure, cleared on any other transition (e.g. a record
            # reopening after being marked cerrada) so it never goes stale.
            closed_at_value = row["last_seen"] if change_code == "cerrada" else None
        else:
            # No transition: leave closed_at exactly as it was.
            closed_at_value = existing["closed_at"]

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
            pending_status_change = COALESCE(?, pending_status_change),
            status_flag = COALESCE(?, status_flag),
            status_updated_at = COALESCE(?, status_updated_at),
            closed_at = ?
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
            change_code,
            status_updated_at_value,
            closed_at_value,
            row["numero"],
        ))

        if transitioned:
            conn.execute("""
            INSERT INTO opportunity_status_history (
                numero, observed_at, source, previous_grupo, previous_estado,
                new_grupo, new_estado, change_code, notification_state, closure_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            """, (
                row["numero"],
                row["last_seen"],
                source,
                old_grupo_raw,
                old_estado_raw,
                new_grupo_raw,
                new_estado_raw,
                change_code,
                row.get("finish_date_guess"),
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

def reconcile_legacy_status_history(conn):
    """Back-fill status_flag/closed_at/history for records whose grupo/estado
    already moved on (e.g. a backfill import wrote the new state directly)
    without ever going through insert_or_update_index, so status_flag was
    never synced. Only considers records the live index flow previously
    tracked (notified_at set) — untouched backfill-only rows have no prior
    known estado to diff against and are left alone. Idempotent: a record
    stops matching once its status_flag reflects the current estado."""
    candidates = conn.execute("""
        SELECT numero, grupo, estado, last_seen, last_notified_status, status_flag
        FROM opportunities
        WHERE notified_at IS NOT NULL
          AND last_notified_status IS NOT NULL
          AND last_notified_status != estado
    """).fetchall()

    reconciled = 0
    for candidate in candidates:
        expected_code = status_change_code(
            candidate["last_notified_status"], candidate["estado"],
            candidate["grupo"], candidate["grupo"],
        )
        if candidate["status_flag"] == expected_code:
            continue

        # Resolve closed_at explicitly (not COALESCE): set on cerrada, cleared
        # otherwise, so a record that reopened after a stale 'cerrada' flag
        # doesn't keep an equally stale closed_at timestamp.
        closed_at_value = candidate["last_seen"] if expected_code == "cerrada" else None
        conn.execute("""
            UPDATE opportunities
            SET status_flag = ?,
                status_updated_at = ?,
                closed_at = ?
            WHERE numero = ?
        """, (expected_code, candidate["last_seen"], closed_at_value, candidate["numero"]))

        conn.execute("""
            INSERT INTO opportunity_status_history (
                numero, observed_at, source, previous_grupo, previous_estado,
                new_grupo, new_estado, change_code, notification_state, closure_at
            ) VALUES (?, ?, 'legacy-reconcile', NULL, ?, ?, ?, ?, 'reconciled', NULL)
        """, (
            candidate["numero"],
            candidate["last_seen"],
            candidate["last_notified_status"],
            candidate["grupo"],
            candidate["estado"],
            expected_code,
        ))
        reconciled += 1

    conn.commit()
    return reconciled

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
