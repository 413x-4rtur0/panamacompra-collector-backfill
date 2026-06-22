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
WINDOW_WIDTH = int(os.environ.get("PC_NEXT_RUN_TIMER_WIDTH", "280"))
WINDOW_HEIGHT = int(os.environ.get("PC_NEXT_RUN_TIMER_HEIGHT", "118"))
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


def next_run_time() -> datetime:
    now = datetime.now()
    minute_bucket = (now.minute // INTERVAL_MINUTES + 1) * INTERVAL_MINUTES
    if minute_bucket >= 60:
        return (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    return now.replace(minute=minute_bucket, second=0, microsecond=0)


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
    root.configure(bg="#1e293b")
    root.resizable(False, False)
    root.attributes("-topmost", True)

    root.update_idletasks()
    x = max(0, (root.winfo_screenwidth() - WINDOW_WIDTH) // 2)
    root.geometry(f"{WINDOW_WIDTH}x{WINDOW_HEIGHT}+{x}+{WINDOW_TOP}")

    tk.Label(root, text="Next live run", font=("Sans", 12, "bold"), bg="#1e293b", fg="#fbbf24").pack(pady=(12, 2))
    next_var = tk.StringVar(value="Loading...")
    tk.Label(root, textvariable=next_var, font=("Sans", 11), bg="#1e293b", fg="#e5e7eb").pack(pady=1)
    count_var = tk.StringVar(value="")
    tk.Label(root, textvariable=count_var, font=("Mono", 18, "bold"), bg="#1e293b", fg="#22c55e").pack(pady=3)
    status_var = tk.StringVar(value="")
    tk.Label(root, textvariable=status_var, font=("Sans", 8), bg="#1e293b", fg="#94a3b8").pack(pady=(0, 8))

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
            next_dt = next_run_time()
            next_var.set(next_dt.strftime("%H:%M:%S"))
            count_var.set(countdown_string(next_dt))
            status_var.set(f"Every {INTERVAL_MINUTES} minutes" + (" · live run finished" if was_active else ""))
            was_active = False
        root.after(1000, refresh)

    refresh()
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
