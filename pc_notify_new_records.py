#!/usr/bin/env python3
"""Send rich PanamaCompra "what is new" notifications to WhatsApp via WAHA.

The run-all worker calls this script after detail and calendar processing.
``--announce`` sends one rich "🟢 NUEVA OPORTUNIDAD DETECTADA" message per saved
record with monitor-visible progress; ``--idle`` sends one "⚪ Sin nuevas
entradas" status; ``--flush`` retries saved records that still have no
``notified_at`` timestamp.

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
    return os.environ.get("PC_WAHA_CHAT_ID", "").strip()


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


# Human-readable label for a pending_status_change code stored by the index step.
STATUS_CHANGE_LABELS = {
    "abierta": "Programada → Abierta",
    "cancelada": "Programada → Cancelada",  # planned future transition
}


def build_status_change_message(row, summary: dict, change_code: str) -> str:
    title = clean_field(row["descripcion"] or row["short_description"] or summary.get("descripcion"))
    entity = clean_field(row["entidad"] or summary.get("entidad"))
    closing_date = clean_field(row["finish_date_guess"])
    url = clean_field(row["link"] or summary.get("enlace_publico") or summary.get("enlace_interno"))
    change_label = STATUS_CHANGE_LABELS.get(change_code, change_code or DASH)
    return (
        "🔄 OPORTUNIDAD ACTUALIZADA\n"
        "\n"
        f"📌 Fuente: {SOURCE_NAME}\n"
        f"🏷️ Título: {title}\n"
        f"🏢 Entidad: {entity}\n"
        f"🔁 Estado: {change_label}\n"
        f"⏰ Cierre: {closing_date}\n"
        "\n"
        "🔗 Ver oportunidad:\n"
        f"{url}\n"
        "\n"
        f"🕒 Actualizado: {now_str()}\n"
        f"🆔 ID: {clean_field(row['numero'])}"
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
    """Announce a single saved record. Idempotent: a record is sent at most
    once (guarded by notified_at). Returns True if a message was sent. Never
    raises, so messaging failures do not break collection runs."""
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
            clear_status_change(conn, numero)
        return sent
    except Exception as exc:  # noqa: BLE001 - never break a run
        print(f"WAHA status-change notify error for {numero}: {exc}", file=sys.stderr)
        return False


def flush_unannounced(conn) -> int:
    """Announce any saved records that were not yet sent (for example because
    WAHA was briefly unreachable). Returns the number sent."""
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
    """Visible MESSAGING step. After the detail download, send — one by one with
    per-message monitor progress — a message for every:

      * new entry (🟢 NUEVA OPORTUNIDAD DETECTADA), and
      * status change (🔄 OPORTUNIDAD ACTUALIZADA, e.g. Programada → Abierta).

    When there is nothing to send it publishes the "Sin nuevas entradas" status.
    Returns the number of messages actually sent. Never raises."""
    step_current = os.environ.get("PC_MSG_STEP_CURRENT", "4")
    step_total = os.environ.get("PC_MSG_STEP_TOTAL", "5")

    # ("new", numero) then ("update", numero), oldest first within each group.
    new_rows = conn.execute(
        "SELECT numero FROM opportunities WHERE detail_status = 'saved' AND notified_at IS NULL "
        "ORDER BY detail_saved_at, first_seen"
    ).fetchall()
    update_rows = conn.execute(
        "SELECT numero FROM opportunities WHERE pending_status_change IS NOT NULL "
        "ORDER BY last_seen, first_seen"
    ).fetchall()
    queue = [("new", r["numero"]) for r in new_rows] + [("update", r["numero"]) for r in update_rows]
    total = len(queue)

    if total == 0:
        total_records = conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]
        send_text("none", build_empty_message(total_records))
        pc_common.write_run_progress(
            "MESSAGING", "RUNNING", 98,
            "Step 4/5: no new opportunities or status changes to send.",
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
        else:
            match_line = match_line_for(full_row, summary, load_keywords()) if full_row is not None else None
            preview = (
                _one_line_preview(build_opportunity_message(full_row, summary, match_line))
                if (full_row is not None and match_line is not None)
                else f"{label} (sin coincidencia de palabra clave)"
            )
        pc_common.write_run_progress(
            "MESSAGING", "RUNNING",
            min(99, 96 + int(3 * index / total)),
            f"Step 4/5: sending WhatsApp {index}/{total} ({kind}): {label}",
            step_current=step_current, step_total=step_total,
            item_current=index, item_total=total,
            records_new=total, records_saved=sent,
            extra=preview,
        )
        ok = notify_status_change(conn, numero) if kind == "update" else notify_saved_record(conn, numero)
        if ok:
            sent += 1
        else:
            skipped += 1

    pc_common.write_run_progress(
        "MESSAGING", "RUNNING", 99,
        f"Step 4/5: WhatsApp done — {sent} sent, {skipped} skipped of {total} ({len(new_rows)} new, {len(update_rows)} updates).",
        step_current=step_current, step_total=step_total,
        item_current=total, item_total=total,
        records_new=len(new_rows), records_saved=sent,
    )
    print(f"WAHA announce complete: {sent} sent, {skipped} skipped of {total}.")
    return sent


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="PanamaCompra WAHA new-record notifier")
    parser.add_argument("--idle", action="store_true", help="send the 'Sin nuevas entradas' status (run found no new records)")
    parser.add_argument("--flush", action="store_true", help="announce any saved records not yet sent (safety net)")
    parser.add_argument("--announce", action="store_true", help="announce every new record one by one, publishing per-message monitor progress (the visible MESSAGING step)")
    args = parser.parse_args(argv)

    if not waha_enabled():
        print("WAHA notification skipped: set PC_WAHA_ENABLED=1 to enable.")
        return 0
    if not waha_destination():
        print("WAHA notification skipped: PC_WAHA_CHAT_ID is not set.")
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
        print(f"pc_notify_new_records fatal error: {exc}", file=sys.stderr)
        raise SystemExit(0)
