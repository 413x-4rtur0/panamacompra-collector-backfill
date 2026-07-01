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


def configured_chat_id() -> str:
    """Destination chat id: env first, then the monitor-saved file."""
    chat_id = os.environ.get("PC_WAHA_CHAT_ID", "").strip()
    if chat_id:
        return chat_id
    if CHAT_ID_PATH.exists():
        return CHAT_ID_PATH.read_text(encoding="utf-8", errors="replace").strip()
    return ""


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


def build_message(event: str, status: str, message: str) -> str:
    prefix = os.environ.get("PC_WAHA_PREFIX", "PanamaCompra")
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    parts = [f"{prefix} [{event.upper()}]", f"Status: {status}"]
    mode_label = run_mode_label()
    if mode_label:
        parts.append(f"Run: {mode_label}")
    parts.append(f"Time: {timestamp}")
    body = message.strip() or saved_message()
    if body:
        parts.append(body)
    return "\n".join(parts)


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


def send_text(text: str) -> None:
    global _send_failures
    base_url = os.environ.get("PC_WAHA_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    session = os.environ.get("PC_WAHA_SESSION", DEFAULT_SESSION)
    chat_id = configured_chat_id()
    api_key = os.environ.get("PC_WAHA_API_KEY", "").strip()
    timeout = float(os.environ.get("PC_WAHA_TIMEOUT_SECONDS", "30"))

    if not chat_id:
        print("WAHA notification skipped: PC_WAHA_CHAT_ID is not set.")
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
    args = parser.parse_args()

    if args.save_message:
        save_message(args.message)
        print(f"Saved reusable WAHA message to {SAVED_MESSAGE_PATH}.")

    if not env_bool("PC_WAHA_ENABLED", False):
        print("WAHA notification skipped: set PC_WAHA_ENABLED=1 and PC_WAHA_CHAT_ID to enable.")
        return 0

    if not enabled_for_event(args.event):
        print(f"WAHA notification skipped: event {args.event!r} is not enabled.")
        return 0

    try:
        send_text(build_message(args.event, args.status, args.message))
    except (OSError, urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
        print(f"WAHA notification failed: {exc}", file=sys.stderr)
        return 1 if env_bool("PC_WAHA_STRICT", False) else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
