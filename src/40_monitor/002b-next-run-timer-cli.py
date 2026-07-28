#!/usr/bin/env python3
"""Low-resource terminal countdown using the Tk timer's shared scheduling logic."""
from __future__ import annotations

import argparse
import fcntl
import importlib.util
import json
import os
import select
import shutil
import sys
import termios
import time
import tty
from datetime import datetime
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().with_name("002-next-run-timer.py")
SPEC = importlib.util.spec_from_file_location("panamacompra_next_run_timer_core", MODULE_PATH)
assert SPEC and SPEC.loader
CORE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = CORE
SPEC.loader.exec_module(CORE)

REFRESH_SECONDS = max(1, CORE.setting_int("PC_NEXT_RUN_CLI_REFRESH_SECONDS", "2", 1))
# Git/SQLite/API data changes much less often than the visible countdown. Keep
# that heavier refresh separate and slower; the one-second loop only formats a
# timestamp and writes the two terminal lines whose text changed.
DATA_REFRESH_SECONDS = max(10, CORE.setting_int("PC_NEXT_RUN_CLI_DATA_REFRESH_SECONDS", "60", 5))
LATEST_RECORDS = max(1, CORE.setting_int("PC_NEXT_RUN_CLI_RECORDS", "4", 1))


def lock_path(instance: str = "terminal") -> Path:
    safe_instance = "".join(ch for ch in str(instance or "terminal").lower() if ch.isalnum() or ch in "_-")
    return Path(f"/tmp/panamacompra_cli_timer_{safe_instance or 'terminal'}.lock")


def acquire_lock(instance: str = "terminal"):
    handle = lock_path(instance).open("w", encoding="utf-8")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


def terminal_width() -> int:
    return max(72, min(140, shutil.get_terminal_size((100, 30)).columns))


def fit(value: object, width: int) -> str:
    text = " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split())
    return text if len(text) <= width else text[:max(1, width - 1)] + "…"


def line(text: object, width: int) -> str:
    return f"│ {fit(text, width - 3)}"


def section(title: str, width: int) -> str:
    label = f" {title} "
    return "├" + label + "─" * max(0, width - len(label) - 2) + "┤"


def timer_snapshot() -> dict[str, object]:
    return CORE.timer_snapshot(LATEST_RECORDS)


def refresh_countdown(snapshot: dict[str, object]) -> None:
    target = snapshot.get("target")
    if isinstance(target, datetime):
        snapshot["now"] = datetime.now()
        snapshot["countdown"] = CORE.countdown_string(target)


def render(snapshot: dict[str, object]) -> str:
    width = terminal_width()
    target = snapshot["target"]
    progress = snapshot["progress"] if isinstance(snapshot.get("progress"), dict) else {}
    summary = snapshot["summary"] if isinstance(snapshot.get("summary"), dict) else {}
    counts = snapshot["archive_counts"] if isinstance(snapshot.get("archive_counts"), dict) else {}
    latest = snapshot["latest"] if isinstance(snapshot.get("latest"), list) else []
    mode = "LIVE RUN ACTIVE" if snapshot.get("active") else "WAITING"
    title = "NEXT CHANGEDETECTION CHECK" if CORE.AUTORUN_SOURCE == "changedetection" else "NEXT LIVE RUN"
    rows = [
        "╭" + "─" * (width - 2) + "╮",
        line(f"PANAMACOMPRA CLI TIMER · {mode} · {datetime.now():%Y-%m-%d %H:%M:%S}", width),
        section(title, width),
        line(f"{snapshot['countdown']}  →  {target:%Y-%m-%d %H:%M:%S}", width),
        line(snapshot.get("source") or "Local interval fallback", width),
        line(f"Schedule source: {'changedetection API' if snapshot.get('authoritative') else 'fallback'}", width),
        section("SYSTEM", width),
        line(f"Branch {snapshot.get('branch')} · {snapshot.get('queue')}", width),
        line(
            f"Archive {snapshot.get('archive_total', 0)} · saved {counts.get('saved', 0)} · "
            f"pending {counts.get('pending', 0)} · failed {counts.get('failed', 0)}",
            width,
        ),
        line(
            f"Last run {summary.get('FINISHED_AT', '—')} · new {progress.get('RECORDS_NEW', '—')} · "
            f"saved {progress.get('RECORDS_SAVED', '—')}",
            width,
        ),
        line(snapshot.get("duration"), width),
        section("LATEST RECORDS", width),
    ]
    if latest:
        for downloaded, numero, end, status, description in latest:
            rows.append(line(f"{downloaded} · {numero} · end {end} · {status} · {description}", width))
        displayed_latest = len(latest)
    else:
        rows.append(line("No collected records yet.", width))
        displayed_latest = 1
    for _unused in range(max(0, LATEST_RECORDS - displayed_latest)):
        rows.append(line("—", width))
    rows.append(section("CURRENT RUN", width))
    if snapshot.get("active"):
        rows.append(line(
            f"{progress.get('PHASE', 'RUNNING')} · {progress.get('PERCENT', '—')}% · "
            f"{progress.get('MESSAGE', 'Collector is active')}",
            width,
        ))
    else:
        rows.append(line("Collector idle · waiting for the next automatic or manual run.", width))
    rows.extend([
        section("CONTROLS", width),
        line("q quit · r refresh schedule/data · h help", width),
        line("Updates in place; Tk timer remains available separately.", width),
        "╰" + "─" * (width - 2) + "╯",
    ])
    return "\n".join(rows)


def changed_line_updates(frame: str, previous_lines: list[str]) -> tuple[str, list[str]]:
    """Render only terminal rows whose content changed.

    Clearing the whole alternate screen every second caused visible flashing.
    Absolute cursor moves plus per-line clearing keep static rows untouched and
    normally update only the clock and countdown lines.
    """
    current_lines = frame.splitlines()
    updates = []
    for index in range(max(len(current_lines), len(previous_lines))):
        current = current_lines[index] if index < len(current_lines) else ""
        previous = previous_lines[index] if index < len(previous_lines) else None
        if current != previous:
            updates.append(f"\033[{index + 1};1H\033[2K{current}")
    return "".join(updates), current_lines


def json_payload(snapshot: dict[str, object]) -> dict[str, object]:
    return CORE.timer_json_payload(snapshot)


def show_help() -> None:
    print("PanamaCompra CLI Timer")
    print("  q  Close only this timer")
    print("  r  Refresh changedetection schedule and archive data now")
    print("  h  Show this help")
    print("\nPress any key to return...", end="", flush=True)
    sys.stdin.read(1)


def run_interactive(snapshot: dict[str, object]) -> int:
    input_fd = sys.stdin.fileno()
    previous_terminal = termios.tcgetattr(input_fd)
    tty.setcbreak(input_fd)
    print("\033[?1049h\033[?25l\033[H\033[2J", end="", flush=True)
    last_data_refresh = time.monotonic()
    previous_lines: list[str] = []
    try:
        while True:
            refresh_countdown(snapshot)
            updates, previous_lines = changed_line_updates(render(snapshot), previous_lines)
            if updates:
                print(updates, end="", flush=True)
            readable, _writable, _errors = select.select([sys.stdin], [], [], REFRESH_SECONDS)
            if readable:
                key = sys.stdin.read(1).lower()
                if key == "q":
                    return 0
                if key == "h":
                    print("\033[?25h\033[H\033[2J", end="", flush=True)
                    show_help()
                    print("\033[?25l\033[H\033[2J", end="", flush=True)
                    previous_lines = []
                    last_data_refresh = 0
                if key == "r":
                    last_data_refresh = 0
            if time.monotonic() - last_data_refresh >= DATA_REFRESH_SECONDS:
                snapshot = timer_snapshot()
                last_data_refresh = time.monotonic()
    finally:
        termios.tcsetattr(input_fd, termios.TCSADRAIN, previous_terminal)
        print("\033[?25h\033[?1049l", end="", flush=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Low-resource PanamaCompra next-run terminal timer")
    parser.add_argument("--once", action="store_true", help="print one timer snapshot and exit")
    parser.add_argument("--json", action="store_true", help="print one JSON snapshot and exit")
    parser.add_argument(
        "--instance",
        default="terminal",
        help="singleton scope (desktop launchers use 'desktop'; direct terminals use 'terminal')",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    snapshot = timer_snapshot()
    if args.json:
        print(json.dumps(json_payload(snapshot), ensure_ascii=False, indent=2))
        return 0
    if args.once or not (sys.stdin.isatty() and sys.stdout.isatty()):
        print(render(snapshot))
        return 0
    lock_handle = acquire_lock(args.instance)
    if lock_handle is None:
        print("Another PanamaCompra CLI timer is already open.", file=sys.stderr)
        return 0
    try:
        return run_interactive(snapshot)
    finally:
        lock_handle.close()


if __name__ == "__main__":
    raise SystemExit(main())
