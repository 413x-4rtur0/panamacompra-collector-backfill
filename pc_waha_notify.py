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
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

DEFAULT_BASE_URL = "http://127.0.0.1:3000"
DEFAULT_SESSION = "default"
CONFIG_DIR = Path(__file__).resolve().parent / "data" / "config"
SAVED_MESSAGE_PATH = CONFIG_DIR / "waha_message.txt"


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def enabled_for_event(event: str) -> bool:
    configured_events = os.environ.get("PC_WAHA_NOTIFY_EVENTS", "info,start,done,failed,timeout,resume,update")
    wanted = {part.strip().lower() for part in configured_events.split(",") if part.strip()}
    return "all" in wanted or event.lower() in wanted


def saved_message() -> str:
    if not SAVED_MESSAGE_PATH.exists():
        return ""
    return SAVED_MESSAGE_PATH.read_text(encoding="utf-8", errors="replace").strip()


def save_message(message: str) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    SAVED_MESSAGE_PATH.write_text(message.strip() + "\n", encoding="utf-8")


def build_message(event: str, status: str, message: str) -> str:
    prefix = os.environ.get("PC_WAHA_PREFIX", "PanamaCompra")
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    parts = [f"{prefix} [{event.upper()}]", f"Status: {status}", f"Time: {timestamp}"]
    body = message.strip() or saved_message()
    if body:
        parts.append(body)
    return "\n".join(parts)


def send_text(text: str) -> None:
    base_url = os.environ.get("PC_WAHA_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    session = os.environ.get("PC_WAHA_SESSION", DEFAULT_SESSION)
    chat_id = os.environ.get("PC_WAHA_CHAT_ID", "").strip()
    api_key = os.environ.get("PC_WAHA_API_KEY", "").strip()
    timeout = float(os.environ.get("PC_WAHA_TIMEOUT_SECONDS", "10"))

    if not chat_id:
        print("WAHA notification skipped: PC_WAHA_CHAT_ID is not set.")
        return

    payload = json.dumps({"session": session, "chatId": chat_id, "text": text}).encode("utf-8")
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if api_key:
        headers["X-Api-Key"] = api_key

    request = urllib.request.Request(f"{base_url}/api/sendText", data=payload, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - local/private WAHA endpoint by configuration
        response.read()
        print(f"WAHA notification sent to {chat_id} via session {session}.")


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
