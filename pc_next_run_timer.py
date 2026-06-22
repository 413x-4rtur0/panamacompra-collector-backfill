#!/usr/bin/env python3
"""Tiny Tk timer showing when the next 30-minute live run is scheduled.

This monitor automatically hides when a live run is active and reappears
after the run finishes to show the countdown until the next scheduled run.
"""
from __future__ import annotations

import os
import subprocess
import tkinter as tk
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
PROGRESS_FILE = BASE_DIR / "data" / "logs" / "run_all_progress.env"


def is_live_run_active() -> bool:
    """Check if a live run is currently in progress."""
    # Check if worker process is running
    result = subprocess.run(
        ["pgrep", "-f", "[p]c_run_all_worker.sh"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )
    if result.returncode == 0:
        return True
    
    # Check progress file for active phase
    if PROGRESS_FILE.exists():
        content = PROGRESS_FILE.read_text(encoding="utf-8", errors="replace")
        for line in content.splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip() == "PHASE":
                phase = value.strip().strip("'\"")
                if phase in ("INDEX", "DETAIL", "CALENDAR", "TEST"):
                    return True
    return False


def next_run_time() -> datetime:
    """Calculate the next 30-minute interval (00, 30 past the hour)."""
    now = datetime.now()
    # Round up to next 30-minute mark
    if now.minute < 30:
        next_run = now.replace(minute=30, second=0, microsecond=0)
    else:
        next_run = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    return next_run


def countdown_string(target: datetime) -> str:
    """Return a human-readable countdown string."""
    delta = target - datetime.now()
    total_seconds = int(delta.total_seconds())
    if total_seconds <= 0:
        return "Starting now!"
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    if hours > 0:
        return f"{hours}h {minutes}m {seconds}s"
    else:
        return f"{minutes}m {seconds}s"


def main() -> int:
    root = tk.Tk()
    root.title("Next Live Run")
    root.configure(bg="#1e293b")
    
    # Small fixed size
    root.geometry("280x140")
    root.resizable(False, False)
    
    # Position: 30px from top, centered horizontally
    root.update_idletasks()
    x = (root.winfo_screenwidth() - 280) // 2
    y = 30
    root.geometry(f"280x140+{x}+{y}")
    
    # Keep window on top
    root.attributes("-topmost", True)
    
    # Title label
    title_label = tk.Label(
        root,
        text="Next Live Run",
        font=("Sans", 14, "bold"),
        bg="#1e293b",
        fg="#fbbf24"
    )
    title_label.pack(pady=(20, 5))
    
    # Next time label
    next_var = tk.StringVar(value="Loading...")
    next_label = tk.Label(
        root,
        textvariable=next_var,
        font=("Sans", 12),
        bg="#1e293b",
        fg="#e5e7eb"
    )
    next_label.pack(pady=5)
    
    # Countdown label
    count_var = tk.StringVar(value="")
    count_label = tk.Label(
        root,
        textvariable=count_var,
        font=("Mono", 18, "bold"),
        bg="#1e293b",
        fg="#22c55e"
    )
    count_label.pack(pady=10)
    
    # Status indicator
    status_var = tk.StringVar(value="")
    status_label = tk.Label(
        root,
        textvariable=status_var,
        font=("Sans", 9),
        bg="#1e293b",
        fg="#94a3b8"
    )
    status_label.pack(pady=(5, 10))
    
    def refresh() -> None:
        if is_live_run_active():
            # Hide the countdown when a run is active
            next_var.set("--:--:--")
            count_var.set("RUNNING")
            status_var.set("Live run in progress...")
            count_label.configure(fg="#f59e0b")  # Orange when running
        else:
            # Show countdown to next scheduled run
            next_dt = next_run_time()
            next_var.set(next_dt.strftime("%H:%M:%S"))
            count_var.set(countdown_string(next_dt))
            status_var.set("Waiting for next run")
            count_label.configure(fg="#22c55e")  # Green when waiting
        
        # Refresh every second
        root.after(1000, refresh)
    
    refresh()
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
