#!/usr/bin/env python3
"""Send rich PanamaCompra "what is new" notifications to WhatsApp via WAHA.

Two paths use the helpers here:

* Real time (preferred): ``pc_detail_downloader.py`` calls ``notify_saved_record``
  immediately after each individual detail page is downloaded and saved, so one
  "🟢 NUEVA OPORTUNIDAD DETECTADA" message goes out per new record as soon as its
  detail (and therefore all of its fields) is available — then it moves on to the
  next new entry and repeats.
* End of run: the worker calls this script with ``--idle`` when a run found no new
  records (sends one "⚪ Sin nuevas entradas" status) or ``--flush`` to announce
  any saved record whose real-time send failed (a safety net).

Design notes
------------
* Dependency-free: reuses pc_common (stdlib only) for the archive DB and
  pc_waha_notify for the WAHA HTTP send + enable/skip logic.
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
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import pc_common
import pc_waha_notify as waha

BASE_DIR = Path(__file__).resolve().parent
CONFIG_DIR = BASE_DIR / "data" / "config"
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


SOURCE_NAME = cfg("PC_WAHA_SOURCE", "Panamá Compra")


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def waha_enabled() -> bool:
    return waha.env_bool("PC_WAHA_ENABLED", False)


def waha_destination() -> str:
    return os.environ.get("PC_WAHA_CHAT_ID", "").strip() or waha.saved_chat_id()


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


def load_detail_summary(detail_json_path: str | None) -> dict:
    if not detail_json_path:
        return {}
    path = Path(detail_json_path)
    if not path.is_absolute():
        path = BASE_DIR / path
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    summary = data.get("summary")
    return summary if isinstance(summary, dict) else {}


def clean_field(value) -> str:
    text = "" if value is None else str(value).strip()
    return text or DASH


def build_opportunity_message(row, summary: dict, match_line: str) -> str:
    title = clean_field(row["descripcion"] or row["short_description"] or summary.get("descripcion"))
    entity = clean_field(row["entidad"] or summary.get("entidad"))
    province = clean_field(summary.get("provincia_de_entrega"))
    published_date = clean_field(row["fecha"])
    closing_date = clean_field(row["finish_date_guess"])
    amount = clean_field(summary.get("precio_estimado"))
    url = clean_field(row["link"] or summary.get("enlace_publico") or summary.get("enlace_interno"))
    detected_at = clean_field(row["detail_saved_at"] or row["first_seen"] or now_str())
    entry_id = clean_field(row["numero"])

    return (
        "🟢 NUEVA OPORTUNIDAD DETECTADA\n"
        "\n"
        f"📌 Fuente: {SOURCE_NAME}\n"
        f"🏷️ Título: {title}\n"
        f"🏢 Entidad: {entity}\n"
        f"📍 Provincia: {province}\n"
        f"📅 Publicado: {published_date}\n"
        f"⏰ Cierre: {closing_date}\n"
        f"💰 Monto estimado: {amount}\n"
        "\n"
        f"🔎 Coincidencia: {match_line}\n"
        "\n"
        "🔗 Ver oportunidad:\n"
        f"{url}\n"
        "\n"
        f"🕒 Detectado: {detected_at}\n"
        f"🆔 ID: {entry_id}"
    )


def build_empty_message(records_checked: int) -> str:
    return (
        "⚪ Sin nuevas entradas\n"
        "\n"
        f"📌 Fuente: {SOURCE_NAME}\n"
        f"🕒 Revisión: {now_str()}\n"
        f"📊 Registros revisados: {records_checked}\n"
        "✅ Monitor activo"
    )


def mark_notified(conn, numero: str) -> None:
    conn.execute("UPDATE opportunities SET notified_at = ? WHERE numero = ?", (now_str(), numero))
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

    Called once at the start of a detail-download run (before new records are
    saved), so only records saved afterwards are announced."""
    if BASELINE_MARKER.exists():
        return False
    if not (waha_enabled() and waha_destination()):
        # Wait until WAHA is usable so the baseline reflects the real "before"
        # state the first time messages can actually be sent.
        return False
    conn.execute(
        "UPDATE opportunities SET notified_at = ? WHERE detail_status = 'saved' AND notified_at IS NULL",
        (now_str(),),
    )
    conn.commit()
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    BASELINE_MARKER.write_text(now_str() + "\n", encoding="utf-8")
    print("WAHA baseline established; existing saved records will not be announced.")
    return True


def notify_saved_record(conn, numero: str) -> bool:
    """Announce a single just-saved record in real time. Idempotent: a record is
    sent at most once (guarded by notified_at). Returns True if a message was
    sent. Designed to be called right after a detail is saved; never raises."""
    try:
        if not (waha_enabled() and waha_destination()):
            return False
        if not BASELINE_MARKER.exists():
            # Baseline not set up yet (ensure_baseline should run first). Skip to
            # avoid mistaking a pre-existing record for a new one.
            return False
        row = fetch_row(conn, numero)
        if row is None or row["detail_status"] != "saved" or row["notified_at"]:
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
        mark_notified(conn, numero)
        return True
    except Exception as exc:  # noqa: BLE001 - defensive: never break a download
        print(f"WAHA per-detail notify error for {numero}: {exc}", file=sys.stderr)
        return False


def flush_unannounced(conn) -> int:
    """Announce any saved records that were not yet sent (e.g. a real-time send
    failed because WAHA was briefly unreachable). Returns the number sent."""
    rows = conn.execute(
        "SELECT numero FROM opportunities WHERE detail_status = 'saved' AND notified_at IS NULL "
        "ORDER BY detail_saved_at, first_seen"
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


def announce_with_progress(conn) -> int:
    """Announce every not-yet-sent new record one by one, publishing run-all
    progress before each send so the monitor shows a visible 'sending messages'
    step (current item, total, and a preview of the message format).

    Returns the number of messages actually sent. Never raises."""
    step_current = os.environ.get("PC_MSG_STEP_CURRENT", "4")
    step_total = os.environ.get("PC_MSG_STEP_TOTAL", "5")
    rows = conn.execute(
        "SELECT numero FROM opportunities WHERE detail_status = 'saved' AND notified_at IS NULL "
        "ORDER BY detail_saved_at, first_seen"
    ).fetchall()
    total = len(rows)

    if total == 0:
        pc_common.write_run_progress(
            "MESSAGING", "RUNNING", 98,
            "Step 4/5: no new opportunities to send.",
            step_current=step_current, step_total=step_total,
            item_current=0, item_total=0, records_new=0,
        )
        return 0

    sent = 0
    skipped = 0
    for index, row in enumerate(rows, start=1):
        numero = row["numero"]
        full_row = fetch_row(conn, numero)
        label = _short_label(full_row) if full_row is not None else numero
        summary = load_detail_summary(full_row["detail_json_path"]) if full_row is not None else {}
        match_line = match_line_for(full_row, summary, load_keywords()) if full_row is not None else None
        preview = (
            _one_line_preview(build_opportunity_message(full_row, summary, match_line))
            if (full_row is not None and match_line is not None)
            else f"{label} (sin coincidencia de palabra clave)"
        )
        pc_common.write_run_progress(
            "MESSAGING", "RUNNING",
            min(99, 96 + int(3 * index / total)),
            f"Step 4/5: sending WhatsApp {index}/{total}: {label}",
            step_current=step_current, step_total=step_total,
            item_current=index, item_total=total,
            records_new=total, records_saved=sent,
            extra=preview,
        )
        if notify_saved_record(conn, numero):
            sent += 1
        else:
            skipped += 1

    pc_common.write_run_progress(
        "MESSAGING", "RUNNING", 99,
        f"Step 4/5: WhatsApp done — {sent} sent, {skipped} skipped of {total} new.",
        step_current=step_current, step_total=step_total,
        item_current=total, item_total=total,
        records_new=total, records_saved=sent,
    )
    print(f"WAHA announce complete: {sent} sent, {skipped} skipped of {total}.")
    return sent


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="PanamaCompra WAHA new-record notifier")
    parser.add_argument("--idle", action="store_true", help="send the 'Sin nuevas entradas' status (run found no new records)")
    parser.add_argument("--flush", action="store_true", help="announce any saved records not yet sent in real time (safety net)")
    parser.add_argument("--announce", action="store_true", help="announce every new record one by one, publishing per-message monitor progress (the visible MESSAGING step)")
    args = parser.parse_args(argv)

    if not waha_enabled():
        print("WAHA notification skipped: set PC_WAHA_ENABLED=1 to enable.")
        return 0
    if not waha_destination():
        print("WAHA notification skipped: no chat id (PC_WAHA_CHAT_ID or data/config/waha_chat_id.txt).")
        return 0

    conn = pc_common.init_db()
    just_baselined = ensure_baseline(conn)

    if args.idle:
        if just_baselined:
            # Right after establishing the baseline, do not claim "no new entries".
            return 0
        total_records = conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]
        send_text("none", build_empty_message(total_records))
        return 0

    if just_baselined:
        # Nothing to announce on the very first run that established the baseline.
        return 0

    if args.announce:
        # Visible MESSAGING step: send every new record one by one with progress.
        announce_with_progress(conn)
        return 0

    # Default / --flush: announce stragglers (e.g. a real-time send failed).
    sent = flush_unannounced(conn)
    print(f"WAHA flush complete: {sent} record(s) announced.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - defensive: never break the worker
        print(f"pc_notify_new_records fatal error: {exc}", file=sys.stderr)
        raise SystemExit(0)
