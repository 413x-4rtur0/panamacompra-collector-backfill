#!/usr/bin/env python3
"""Send optional PanamaCompra notifications to a private WhatsApp group via WAHA.

The script is intentionally dependency-free. It uses WAHA's HTTP API when
PC_WAHA_CHAT_ID is configured; otherwise it exits successfully so collector runs
are never blocked by missing notification configuration.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

# Resolve the runtime config directory through common.py so the chat id and
# saved message live in the same data/config folder the monitors write to
# (var/data/config in development/portable mode, XDG state dir when installed).
_SRC_DIR = Path(__file__).resolve().parent.parent
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))
import common as pc_common

DEFAULT_BASE_URL = "http://127.0.0.1:3000"
DEFAULT_SESSION = "default"
CONFIG_DIR = pc_common.DATA_CONFIG_DIR
SAVED_MESSAGE_PATH = CONFIG_DIR / "waha_message.txt"
CHAT_ID_PATH = CONFIG_DIR / "waha_chat_id.txt"
SYSTEM_FORMAT_PATH = CONFIG_DIR / "waha_format_system.txt"
SUMMARY_FORMAT_PATH = CONFIG_DIR / "waha_format_summary.txt"

# Per-purpose destinations, so the index alerts, the item-detail follow-ups and
# the status-change, system-health and final-summary messages can each go to a
# different group/channel. Every purpose falls back to the default destination
# when not configured.
CHAT_PURPOSES = ("index", "details", "status", "system", "summary")


def chat_id_path(purpose: str = "") -> Path:
    """Monitor-saved chat id file for a purpose ('' = default destination)."""
    if purpose in CHAT_PURPOSES:
        return CONFIG_DIR / f"waha_chat_id_{purpose}.txt"
    return CHAT_ID_PATH


def _read_chat_file(path: Path) -> str:
    if path.exists():
        return path.read_text(encoding="utf-8", errors="replace").strip()
    return ""


def _purpose_chat_id_without_fallback(purpose: str) -> str:
    """Purpose-specific chat id from env/file only, without default fallback."""
    if purpose not in CHAT_PURPOSES:
        return ""
    env_value = os.environ.get(f"PC_WAHA_CHAT_ID_{purpose.upper()}", "").strip()
    if env_value:
        return env_value
    return _read_chat_file(chat_id_path(purpose))


def _default_chat_id() -> str:
    chat_id = os.environ.get("PC_WAHA_CHAT_ID", "").strip()
    if chat_id:
        return chat_id
    return _read_chat_file(CHAT_ID_PATH)


def _single_purpose_fallback_chat_id() -> str:
    """If the operator filled exactly one purpose field, treat it as one group.

    This prevents the common misconfiguration where only Summary/System is filled:
    the final summary sends, but index/detail messages silently have no default.
    When multiple purpose-specific fields exist and the default is blank, missing
    purposes still skip so we do not guess between different groups.
    """
    values = []
    for purpose in CHAT_PURPOSES:
        value = _purpose_chat_id_without_fallback(purpose)
        if value and value not in values:
            values.append(value)
    return values[0] if len(values) == 1 else ""


def configured_chat_id(purpose: str = "", *, _debug_info: list[str] | None = None) -> str:
    """Destination chat id for a purpose.

    Order: purpose-specific env/file, default env/file, then an exactly-one
    purpose-specific fallback. The last case makes "one group" work even if the
    chat id was accidentally placed in Summary/System/Index instead of Default.

    When ``_debug_info`` is passed (a list), diagnostic messages about the
    resolution chain are appended to it — useful for callers that want to log
    why sending was skipped.
    """
    def _dbg(msg: str) -> None:
        if _debug_info is not None:
            _debug_info.append(msg)

    purpose_value = _purpose_chat_id_without_fallback(purpose)
    if purpose_value:
        return purpose_value
    _dbg(f"  no purpose-specific for {purpose!r}")

    default_value = _default_chat_id()
    if default_value:
        return default_value
    _dbg(f"  no default chat id (env PC_WAHA_CHAT_ID or file {CHAT_ID_PATH})")

    single = _single_purpose_fallback_chat_id()
    if single:
        return single
    _dbg("  no single-purpose fallback (0 or 2+ purpose-specific files have values)")
    return ""


def any_destination_configured() -> bool:
    """True when the default destination or any per-purpose destination is set."""
    if _default_chat_id():
        return True
    return any(_purpose_chat_id_without_fallback(purpose) for purpose in CHAT_PURPOSES)


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def enabled_for_event(event: str) -> bool:
    configured_events = os.environ.get("PC_WAHA_NOTIFY_EVENTS", "info,start,done,failed,timeout,resume,update,new,none")
    wanted = {part.strip().lower() for part in configured_events.split(",") if part.strip()}
    return "all" in wanted or event.lower() in wanted


def read_saved_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace").strip()


def saved_message() -> str:
    return read_saved_text(SAVED_MESSAGE_PATH)



def save_message(message: str) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    SAVED_MESSAGE_PATH.write_text(message.strip() + "\n", encoding="utf-8")


# Friendly labels for the run mode that produced a run-outcome message, so a
# WhatsApp alert says whether it came from the automatic (changedetection) flow
# or an operator-initiated restart/manual/test run.
RUN_MODE_LABELS = {
    "AUTO": "Automático (changedetection)",
    "RESTART": "Reinicio de pendientes",
    "MANUAL": "Manual",
    "TEST": "Prueba (sandbox)",
}


def run_mode_label() -> str:
    """Friendly label for the current run mode, or '' when not inside a run."""
    mode = os.environ.get("PC_RUN_MODE", "").strip().upper()
    if not mode or mode == "IDLE":
        return ""
    return RUN_MODE_LABELS.get(mode, mode.title())


class _SafeDict(dict):
    def __missing__(self, key):  # noqa: D105
        return "{" + key + "}"


def load_operational_format(kind: str) -> str:
    path = SUMMARY_FORMAT_PATH if kind == "summary" else SYSTEM_FORMAT_PATH
    if path.exists():
        text = path.read_text(encoding="utf-8", errors="replace").strip("\n")
        if text.strip():
            return text
    return ""


def build_message(event: str, status: str, message: str, purpose: str = "") -> str:
    prefix = os.environ.get(
        "PC_WAHA_PREFIX",
        os.environ.get("PC_WAHA_SOURCE", "Panamá Compra"),
    )
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M")
    mode_label = run_mode_label()
    body = message.strip() or saved_message()
    kind = "summary" if purpose == "summary" else "system"
    heading = (
        f"📊 *Resumen final de ronda - {prefix}*"
        if kind == "summary"
        else f"🛠️ *Sistema - {prefix}*"
    )
    custom = load_operational_format(kind)
    if custom:
        return custom.format_map(_SafeDict({
            "heading": heading,
            "event": event,
            "status": status,
            "message": body,
            "time": timestamp,
            "run": mode_label,
            "fuente": prefix,
        }))

    # Keep the actual built-in message identical to `pcc format show/preview`
    # (DEFAULT_FORMATS in 020-notify-whatsapp.py).
    return (
        f"{heading}\n\n"
        f"Status: {status}\n"
        f"Run: {mode_label or '—'}\n"
        f"Time: {timestamp}\n\n"
        f"{body}"
    )


# Simple in-process circuit breaker: after several outright failures (e.g. WAHA
# is down) we stop retrying so a batch of messages does not pile up many seconds
# of backoff per message. A single success clears it.
_send_failures = 0


def _max_send_attempts() -> int:
    """1 + PC_WAHA_RETRIES (default 2 retries → 3 attempts), minimum 1."""
    try:
        retries = int(os.environ.get("PC_WAHA_RETRIES", "2"))
    except (TypeError, ValueError):
        retries = 2
    return max(1, retries + 1)


def _breaker_threshold() -> int:
    """Consecutive outright failures before retries are suppressed (still one
    attempt per message). Tunable via PC_WAHA_BREAKER_THRESHOLD, minimum 1."""
    try:
        return max(1, int(os.environ.get("PC_WAHA_BREAKER_THRESHOLD", "3")))
    except (TypeError, ValueError):
        return 3


def send_text(text: str, purpose: str = "", chat_id_override: str = "") -> None:
    global _send_failures
    base_url = os.environ.get("PC_WAHA_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    session = os.environ.get("PC_WAHA_SESSION", DEFAULT_SESSION)
    _debug: list[str] = []
    chat_id = chat_id_override.strip() or configured_chat_id(purpose, _debug_info=_debug)
    api_key = (os.environ.get("PC_WAHA_API_KEY") or os.environ.get("WAHA_API_KEY", "")).strip()
    timeout = float(os.environ.get("PC_WAHA_TIMEOUT_SECONDS", "30"))

    if not chat_id:
        _debug.insert(0, f"WAHA notification skipped: no destination for {purpose!r}.")
        _debug.insert(1, f"  Files scanned in {CONFIG_DIR}/waha_chat_id*.txt")
        _debug.insert(2, f"  Env PC_WAHA_CHAT_ID={os.environ.get('PC_WAHA_CHAT_ID', '')!r}")
        _debug.insert(3, f"  Env PC_WAHA_CHAT_ID_{purpose.upper() if purpose else ''}={os.environ.get(f'PC_WAHA_CHAT_ID_{purpose.upper()}' if purpose else 'UNSET', '')!r}")
        print("\n".join(_debug), file=sys.stderr)
        return

    payload = json.dumps({"session": session, "chatId": chat_id, "text": text}).encode("utf-8")
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if api_key:
        headers["X-Api-Key"] = api_key

    # Stop retrying once the endpoint looks persistently down, to bound latency.
    attempts = 1 if _send_failures >= _breaker_threshold() else _max_send_attempts()
    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(f"{base_url}/api/sendText", data=payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - local/private WAHA endpoint by configuration
                response.read()
            _send_failures = 0
            print(f"WAHA notification sent to {chat_id} via session {session}.")
            pc_common.log_app_notification(purpose, text, chat_id)
            return
        except (OSError, urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
            if attempt >= attempts:
                _send_failures += 1
                raise
            backoff = min(8.0, 2.0 ** (attempt - 1))  # 1s, 2s, 4s, …
            print(f"WAHA send attempt {attempt}/{attempts} failed: {exc}; retrying in {backoff:.0f}s.", file=sys.stderr)
            time.sleep(backoff)


def main() -> int:
    parser = argparse.ArgumentParser(description="Send a PanamaCompra text notification through WAHA.")
    parser.add_argument("--event", default="info", help="Event name, for example start/done/failed/resume/update.")
    parser.add_argument("--status", default="INFO", help="Short status label for the message body.")
    parser.add_argument("--message", default="", help="Additional notification message text. If omitted, the saved reusable message is used.")
    parser.add_argument("--save-message", action="store_true", help="Save --message as the reusable group notification message for this and future runs.")
    parser.add_argument("--force-send", action="store_true", help="Send even when PC_WAHA_ENABLED=0 or the event is not listed in PC_WAHA_NOTIFY_EVENTS (used for explicit tests).")
    parser.add_argument("--purpose", default="", choices=["", *CHAT_PURPOSES], help="Use a purpose-specific destination, falling back to the default chat id.")
    args = parser.parse_args()

    if args.save_message:
        save_message(args.message)
        print(f"Saved reusable WAHA message to {SAVED_MESSAGE_PATH}.")

    if not args.force_send and not env_bool("PC_WAHA_ENABLED", False):
        print("WAHA notification skipped: set PC_WAHA_ENABLED=1 and PC_WAHA_CHAT_ID to enable.")
        return 0

    if not args.force_send and not enabled_for_event(args.event):
        print(f"WAHA notification skipped: event {args.event!r} is not enabled.")
        return 0

    try:
        send_text(build_message(args.event, args.status, args.message, purpose=args.purpose), purpose=args.purpose)
    except (OSError, urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
        print(f"WAHA notification failed: {exc}", file=sys.stderr)
        return 1 if env_bool("PC_WAHA_STRICT", False) else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
