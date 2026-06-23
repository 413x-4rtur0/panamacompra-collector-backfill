#!/usr/bin/env python3
"""Send rich PanamaCompra "what is new" notifications to WhatsApp via WAHA.

The run-all worker calls this script after a successful index+detail iteration.
It looks at the archive database for records that were saved but never announced
and sends one WhatsApp message per new opportunity using the
"🟢 NUEVA OPORTUNIDAD DETECTADA" template. When nothing new is found it sends a
single "⚪ Sin nuevas entradas" status message instead.

Design notes
------------
* Dependency-free: it reuses pc_common (stdlib only) for the archive DB and
  pc_waha_notify for the WAHA HTTP send + enable/skip logic.
* It NEVER blocks a collector run: any failure is caught and logged, and the
  worker invokes it with `|| true`.
* First-run baseline: when the `notified_at` column is brand new, every existing
  saved record would otherwise look "new" and flood the group. The first run
  records a baseline (marks current saved records as already-notified) and sends
  no opportunity messages, so only genuinely new records are announced later.
* Optional keyword filter: put one keyword per line in
  data/config/waha_keywords.txt. When present, only records whose title /
  description / entity match a keyword are announced (and the matched keywords
  are listed in the message). When the file is missing/empty, every new record
  is announced and the match line reads "Sin filtro (todas las entradas)".
"""
from __future__ import annotations

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


def establish_baseline(conn) -> None:
    """First run: announce nothing, just remember the records that already exist."""
    conn.execute(
        "UPDATE opportunities SET notified_at = ? WHERE detail_status = 'saved' AND notified_at IS NULL",
        (now_str(),),
    )
    conn.commit()
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    BASELINE_MARKER.write_text(now_str() + "\n", encoding="utf-8")
    print("WAHA new-record baseline established; existing saved records will not be re-announced.")


def main() -> int:
    if not waha.env_bool("PC_WAHA_ENABLED", False):
        print("WAHA new-record notification skipped: set PC_WAHA_ENABLED=1 to enable.")
        return 0

    chat_id = os.environ.get("PC_WAHA_CHAT_ID", "").strip() or waha.saved_chat_id()
    if not chat_id:
        # Without a destination nothing can be sent. Do NOT touch the database so
        # records remain "new" and get announced once a chat id is configured.
        print("WAHA new-record notification skipped: PC_WAHA_CHAT_ID is not set.")
        return 0

    conn = pc_common.init_db()

    # Establish the baseline on the very first run so an existing archive does not
    # produce a burst of "new opportunity" messages.
    if not BASELINE_MARKER.exists():
        establish_baseline(conn)
        return 0

    total_records = conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]

    new_rows = conn.execute(
        """
        SELECT * FROM opportunities
        WHERE detail_status = 'saved' AND notified_at IS NULL
        ORDER BY detail_saved_at, first_seen
        """
    ).fetchall()

    keywords = load_keywords()
    try:
        max_messages = max(1, int(cfg("PC_WAHA_MAX_NEW_MESSAGES", "12")))
    except ValueError:
        max_messages = 12

    sent = 0
    truncated_extra = 0
    for row in new_rows:
        summary = load_detail_summary(row["detail_json_path"])
        if keywords:
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
            if not matches:
                # No keyword matched: mark as seen so it is not rechecked, but do
                # not announce it.
                mark_notified(conn, row["numero"])
                continue
            match_line = ", ".join(matches)
        else:
            match_line = "Sin filtro (todas las entradas)"

        if sent >= max_messages:
            truncated_extra += 1
            mark_notified(conn, row["numero"])
            continue

        message = build_opportunity_message(row, summary, match_line)
        if not enabled_and_send("new", message):
            # Leave notified_at unset so the record is retried on the next run
            # rather than silently lost when WAHA is unreachable.
            continue
        mark_notified(conn, row["numero"])
        sent += 1

    if truncated_extra:
        enabled_and_send(
            "new",
            (
                "🟢 PanamaCompra: además de las anteriores se detectaron "
                f"{truncated_extra} oportunidad(es) nueva(s) adicionales.\n"
                f"🕒 {now_str()}"
            ),
        )

    if sent == 0 and truncated_extra == 0:
        enabled_and_send("none", build_empty_message(total_records))

    return 0


def enabled_and_send(event: str, text: str) -> bool:
    """Send through WAHA respecting the per-event enable list. Returns True on a
    successful send (or a configured skip that should still be treated as done)."""
    if not waha.enabled_for_event(event):
        print(f"WAHA notification skipped: event {event!r} is not enabled.")
        return False
    try:
        waha.send_text(text)
        return True
    except Exception as exc:  # noqa: BLE001 - never let a notify failure stop a run
        print(f"WAHA notification failed: {exc}", file=sys.stderr)
        return False


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - defensive: never break the worker
        print(f"pc_notify_new_records fatal error: {exc}", file=sys.stderr)
        raise SystemExit(0)
