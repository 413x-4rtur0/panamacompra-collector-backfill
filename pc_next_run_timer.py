#!/usr/bin/env python3
"""Tiny fixed-size Tk dashboard for the next scheduled live run.

It stays near the top-center of the desktop while the collector is idle and
withdraws while a live run is active (reappearing when the run ends). Besides the
countdown to the next run it shows context the operator usually wants at a
glance: the current git branch, the latest collected records, and a summary of
the last run (new/saved counts and total archive size).
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import tkinter as tk
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
PROGRESS_FILE = BASE_DIR / "data" / "logs" / "run_all_progress.env"
ARCHIVE_DB = BASE_DIR / "data" / "panamacompra_archive.db"
INTERVAL_MINUTES = max(1, int(os.environ.get("PC_NEXT_RUN_INTERVAL_MINUTES", "30")))
# Fixed window size. Bigger by default than the old timer because it now carries
# the branch, latest records and last-run summary; still pinned (resizable off).
WINDOW_WIDTH = int(os.environ.get("PC_NEXT_RUN_TIMER_WIDTH", "340"))
WINDOW_HEIGHT = int(os.environ.get("PC_NEXT_RUN_TIMER_HEIGHT", "258"))
WINDOW_TOP = int(os.environ.get("PC_NEXT_RUN_TIMER_TOP", "30"))
# How many of the most recent records to list.
RECORDS_SHOWN = max(1, int(os.environ.get("PC_NEXT_RUN_TIMER_RECORDS", "3")))
# Refresh the cheap countdown every second; the heavier git/DB reads less often.
DATA_REFRESH_TICKS = max(1, int(os.environ.get("PC_NEXT_RUN_TIMER_DATA_REFRESH_SECONDS", "10")))

ACTIVE_PHASES = {"STARTING", "UPDATE", "INDEX", "DETAIL", "CALENDAR", "MESSAGING", "TEST"}
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

    The countdown is anchored to the real previous run, so it reflects when the
    next run is actually due (last start + interval) rather than an arbitrary
    wall-clock boundary.
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


def countdown_string(target: datetime) -> str:
    total_seconds = max(0, int((target - datetime.now()).total_seconds()))
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    if hours:
        return f"{hours}h {minutes:02d}m {seconds:02d}s"
    return f"{minutes:02d}m {seconds:02d}s"


def git_branch() -> str:
    """Current git branch of the checkout (or '-' if it cannot be read)."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=BASE_DIR, capture_output=True, text=True, timeout=3,
        )
        return result.stdout.strip() or "-"
    except (OSError, subprocess.SubprocessError):
        return "-"


def archive_snapshot(limit: int = RECORDS_SHOWN) -> tuple[int, list[tuple[str, str]]]:
    """Return (total_records, [(numero, short_description), ...]) newest first.

    Read-only and defensive: a missing/locked/empty DB yields (0, [])."""
    if not ARCHIVE_DB.exists():
        return 0, []
    try:
        conn = sqlite3.connect(f"file:{ARCHIVE_DB}?mode=ro", uri=True, timeout=2)
    except sqlite3.Error:
        return 0, []
    try:
        conn.row_factory = sqlite3.Row
        total = conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]
        rows = conn.execute(
            "SELECT numero, COALESCE(NULLIF(short_description, ''), descripcion, '') AS d "
            "FROM opportunities ORDER BY first_seen DESC, numero DESC LIMIT ?",
            (limit,),
        ).fetchall()
    except sqlite3.Error:
        return 0, []
    finally:
        conn.close()
    return total, [(str(r["numero"] or ""), str(r["d"] or "")) for r in rows]


def _truncate(text: str, width: int) -> str:
    text = text or ""
    return (text[: width - 1] + "…") if len(text) > width else text


def main() -> int:
    root = tk.Tk()
    root.title("Next Live Run")
    root.configure(bg="#1e293b")
    root.resizable(False, False)
    root.attributes("-topmost", True)

    root.update_idletasks()
    x = max(0, (root.winfo_screenwidth() - WINDOW_WIDTH) // 2)
    # Pin the window to a fixed size and position so it never grows with content.
    root.geometry(f"{WINDOW_WIDTH}x{WINDOW_HEIGHT}+{x}+{WINDOW_TOP}")

    tk.Label(root, text="Next live run", font=("Sans", 12, "bold"), bg="#1e293b", fg="#fbbf24").pack(pady=(10, 1))
    next_var = tk.StringVar(value="Loading...")
    tk.Label(root, textvariable=next_var, font=("Sans", 11), bg="#1e293b", fg="#e5e7eb").pack(pady=1)
    count_var = tk.StringVar(value="")
    count_label = tk.Label(root, textvariable=count_var, font=("Mono", 18, "bold"), bg="#1e293b", fg="#22c55e")
    count_label.pack(pady=2)

    tk.Frame(root, bg="#334155", height=1).pack(fill="x", padx=14, pady=(4, 4))

    branch_var = tk.StringVar(value="Branch: …")
    tk.Label(root, textvariable=branch_var, font=("Sans", 9, "bold"), bg="#1e293b", fg="#93c5fd").pack(pady=0)
    summary_var = tk.StringVar(value="")
    tk.Label(root, textvariable=summary_var, font=("Sans", 9), bg="#1e293b", fg="#e5e7eb").pack(pady=0)
    lastrun_var = tk.StringVar(value="")
    tk.Label(root, textvariable=lastrun_var, font=("Sans", 8), bg="#1e293b", fg="#94a3b8").pack(pady=0)

    tk.Label(root, text="Latest records", font=("Sans", 8, "bold"), bg="#1e293b", fg="#fbbf24").pack(pady=(4, 0))
    latest_var = tk.StringVar(value="…")
    tk.Label(
        root, textvariable=latest_var, font=("Mono", 8), bg="#1e293b", fg="#bbf7d0",
        justify="left", anchor="w",
    ).pack(fill="x", padx=12)

    status_var = tk.StringVar(value="")
    tk.Label(root, textvariable=status_var, font=("Sans", 8), bg="#1e293b", fg="#94a3b8").pack(side="bottom", pady=(0, 6))

    state = {"was_active": False, "tick": 0, "branch": "-"}

    def refresh_data() -> None:
        state["branch"] = git_branch()
        branch_var.set(f"Branch: {_truncate(state['branch'], 38)}")
        values = progress_values()
        total, latest = archive_snapshot()
        new = values.get("RECORDS_NEW", "-")
        saved = values.get("RECORDS_SAVED", "-")
        summary_var.set(f"New: {new}   ·   Saved: {saved}   ·   Archive: {total}")
        last_start = values.get("STARTED_AT", "") or "—"
        last_status = values.get("STATUS", "") or "—"
        lastrun_var.set(f"Last run: {last_start}  ·  {last_status}")
        if latest:
            latest_var.set("\n".join(f"{num}\n  {_truncate(desc, 40) or '(sin descripción)'}" for num, desc in latest))
        else:
            latest_var.set("(sin registros todavía)")

    def refresh() -> None:
        active = is_live_run_active()
        if active:
            state["was_active"] = True
            if root.state() != "withdrawn":
                root.withdraw()
        else:
            if root.state() == "withdrawn":
                root.deiconify()
                root.lift()
            next_dt = next_run_time()
            remaining = int((next_dt - datetime.now()).total_seconds())
            next_var.set(next_dt.strftime("%H:%M:%S"))
            count_var.set(countdown_string(next_dt))
            # Turn the countdown amber when the next run is imminent (< 60s).
            count_label.configure(fg="#f59e0b" if remaining <= 60 else "#22c55e")
            basis = "after last run" if last_live_run_start() is not None else "on the clock"
            status_var.set(f"Every {INTERVAL_MINUTES} min ({basis})" + (" · run finished" if state["was_active"] else ""))
            # Refresh the heavier branch/DB data periodically (and right after a run).
            if state["tick"] % DATA_REFRESH_TICKS == 0 or state["was_active"]:
                refresh_data()
            state["was_active"] = False
        state["tick"] += 1
        root.after(1000, refresh)

    refresh_data()
    refresh()
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
