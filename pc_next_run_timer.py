#!/usr/bin/env python3
"""Tiny Tk countdown for the next scheduled live run.

The widget stays near the top-center of the desktop while the collector is idle,
withdraws completely when a live run starts, and reappears when that run ends.
"""
from __future__ import annotations

import os
import subprocess
import tkinter as tk
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
PROGRESS_FILE = BASE_DIR / "data" / "logs" / "run_all_progress.env"
INTERVAL_MINUTES = max(1, int(os.environ.get("PC_NEXT_RUN_INTERVAL_MINUTES", "30")))
WINDOW_WIDTH = int(os.environ.get("PC_NEXT_RUN_TIMER_WIDTH", "420"))
WINDOW_HEIGHT = int(os.environ.get("PC_NEXT_RUN_TIMER_HEIGHT", "232"))
WINDOW_TOP = int(os.environ.get("PC_NEXT_RUN_TIMER_TOP", "30"))

ACTIVE_PHASES = {"STARTING", "UPDATE", "INDEX", "DETAIL", "CALENDAR", "TEST"}
ACTIVE_STATUSES = {"RUNNING"}


def progress_values() -> dict[str, str]:
    values: dict[str, str] = {}
    if not PROGRESS_FILE.exists():
        return values
    for line in PROGRESS_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


def is_live_run_active() -> bool:
    if subprocess.run(["pgrep", "-f", "[p]c_run_all_worker.sh"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
        return True
    values = progress_values()
    return values.get("MODE") == "LIVE" and (values.get("PHASE") in ACTIVE_PHASES or values.get("STATUS") in ACTIVE_STATUSES)


def _parse_progress_timestamp(text: str) -> datetime | None:
    text = (text or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def last_live_run_start() -> datetime | None:
    """When the most recent live run started, taken from the progress file.

    The timer counts down from the real previous run, so the countdown reflects
    when the next run is actually due (last start + interval) instead of an
    arbitrary wall-clock boundary.
    """
    values = progress_values()
    if values.get("MODE") and values.get("MODE") != "LIVE":
        return None
    return _parse_progress_timestamp(values.get("STARTED_AT", "")) or _parse_progress_timestamp(values.get("UPDATED_AT", ""))


def clock_bucket_next_run(now: datetime) -> datetime:
    minute_bucket = (now.minute // INTERVAL_MINUTES + 1) * INTERVAL_MINUTES
    if minute_bucket >= 60:
        return (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    return now.replace(minute=minute_bucket, second=0, microsecond=0)


def next_run_time() -> datetime:
    now = datetime.now()
    started = last_live_run_start()
    if started is not None:
        target = started + timedelta(minutes=INTERVAL_MINUTES)
        # If the expected run is overdue, roll forward in whole intervals so the
        # countdown always points at the next upcoming slot rather than the past.
        while target <= now:
            target += timedelta(minutes=INTERVAL_MINUTES)
        return target
    return clock_bucket_next_run(now)


def short_value(values: dict[str, str], key: str, default: str = "-") -> str:
    value = (values.get(key) or "").strip()
    return value if value else default


def previous_run_summary(values: dict[str, str]) -> str:
    """Compact previous-run stats for the timer window.

    These fields come from data/logs/run_all_progress.env and are the useful
    "what happened last time?" counters the operator asked to see while
    waiting for the next live run.
    """
    found = short_value(values, "RECORDS_FOUND")
    new = short_value(values, "RECORDS_NEW")
    existing = short_value(values, "RECORDS_EXISTING")
    saved = short_value(values, "RECORDS_SAVED")
    failed = short_value(values, "RECORDS_FAILED")
    pending = short_value(values, "RECORDS_PENDING")
    return f"Last index: found {found} · new {new} · existing {existing}\nDetails: saved/skipped {saved} · failed {failed} · pending {pending}"


def previous_run_status(values: dict[str, str]) -> str:
    phase = short_value(values, "PHASE", "IDLE")
    status = short_value(values, "STATUS", "DONE")
    updated = short_value(values, "UPDATED_AT")
    started = short_value(values, "STARTED_AT")
    return f"Previous run: {phase}/{status} · started {started} · updated {updated}"


def compact_message(values: dict[str, str], limit: int = 130) -> str:
    message = short_value(values, "MESSAGE", "No active process.")
    if len(message) <= limit:
        return message
    return message[: limit - 1].rstrip() + "…"


def countdown_string(target: datetime) -> str:
    total_seconds = max(0, int((target - datetime.now()).total_seconds()))
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    if hours:
        return f"{hours}h {minutes:02d}m {seconds:02d}s"
    return f"{minutes:02d}m {seconds:02d}s"


def main() -> int:
    root = tk.Tk()
    root.title("Next Live Run")
    root.configure(bg="#0f172a")
    root.resizable(False, False)
    root.attributes("-topmost", True)

    root.update_idletasks()
    x = max(0, (root.winfo_screenwidth() - WINDOW_WIDTH) // 2)
    root.geometry(f"{WINDOW_WIDTH}x{WINDOW_HEIGHT}+{x}+{WINDOW_TOP}")

    card = tk.Frame(root, bg="#111827", bd=1, relief="solid", highlightbackground="#334155", highlightthickness=1)
    card.pack(fill="both", expand=True, padx=8, pady=8)
    card.columnconfigure(0, weight=1)

    tk.Label(card, text="Next live run", font=("Sans", 14, "bold"), bg="#111827", fg="#fbbf24").grid(row=0, column=0, sticky="ew", pady=(10, 0), padx=12)
    next_var = tk.StringVar(value="Loading...")
    tk.Label(card, textvariable=next_var, font=("Sans", 11), bg="#111827", fg="#e5e7eb").grid(row=1, column=0, sticky="ew", pady=(2, 0), padx=12)
    count_var = tk.StringVar(value="")
    tk.Label(card, textvariable=count_var, font=("Mono", 24, "bold"), bg="#111827", fg="#22c55e").grid(row=2, column=0, sticky="ew", pady=(2, 4), padx=12)
    status_var = tk.StringVar(value="")
    tk.Label(card, textvariable=status_var, font=("Sans", 9), bg="#111827", fg="#93c5fd", wraplength=WINDOW_WIDTH - 38, justify="center").grid(row=3, column=0, sticky="ew", pady=(0, 4), padx=12)
    stats_var = tk.StringVar(value="")
    tk.Label(card, textvariable=stats_var, font=("Sans", 9, "bold"), bg="#0b1220", fg="#e5e7eb", wraplength=WINDOW_WIDTH - 42, justify="center", padx=8, pady=6).grid(row=4, column=0, sticky="ew", pady=(2, 4), padx=12)
    message_var = tk.StringVar(value="")
    tk.Label(card, textvariable=message_var, font=("Sans", 8), bg="#111827", fg="#94a3b8", wraplength=WINDOW_WIDTH - 38, justify="center").grid(row=5, column=0, sticky="ew", pady=(0, 8), padx=12)

    was_active = False

    def refresh() -> None:
        nonlocal was_active
        active = is_live_run_active()
        if active:
            was_active = True
            if root.state() != "withdrawn":
                root.withdraw()
        else:
            if root.state() == "withdrawn":
                root.deiconify()
                root.lift()
            values = progress_values()
            next_dt = next_run_time()
            next_var.set("Next due at " + next_dt.strftime("%H:%M:%S"))
            count_var.set(countdown_string(next_dt))
            basis = "after last live run" if last_live_run_start() is not None else "on the clock"
            status_var.set(f"Every {INTERVAL_MINUTES} min · {basis}" + (" · run finished" if was_active else ""))
            stats_var.set(previous_run_summary(values))
            message_var.set(previous_run_status(values) + "\n" + compact_message(values))
            was_active = False
        root.after(1000, refresh)

    refresh()
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
