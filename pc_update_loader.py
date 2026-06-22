#!/usr/bin/env python3
"""Tk loader window that runs update_local_copy.sh first, then opens the monitor.

The window appears immediately with a real (step-based) progress bar, streams the
update output, and only opens the monitor AFTER the local copy finished updating —
so the user sees the update progress first and then a monitor reflecting the
already-updated code.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk

BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "data" / "logs"
UPDATE_SCRIPT = BASE_DIR / "update_local_copy.sh"
MONITOR_SCRIPT = BASE_DIR / "pc_open_monitor.sh"

# update_local_copy.sh prints numbered steps "1) ..." through "10) ...".
TOTAL_STEPS = 10
STEP_RE = re.compile(r"^\s*(\d{1,2})\)\s")


def open_monitor() -> None:
    subprocess.Popen([str(MONITOR_SCRIPT)], cwd=BASE_DIR, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run update_local_copy.sh in a separate loader window")
    parser.add_argument("--open-monitor-after", action="store_true", help="open the normal monitor after a successful update")
    args = parser.parse_args()

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f"update_loader_{time.strftime('%Y%m%d_%H%M%S')}.log"

    if not os.environ.get("DISPLAY"):
        with log_file.open("w", encoding="utf-8") as out:
            result = subprocess.run([str(UPDATE_SCRIPT)], cwd=BASE_DIR, stdout=out, stderr=subprocess.STDOUT)
        if result.returncode == 0 and args.open_monitor_after:
            open_monitor()
        return result.returncode

    root = tk.Tk()
    root.title("PanamaCompra Local Update")
    root.geometry("760x440")
    root.configure(bg="#0f172a")
    root.columnconfigure(0, weight=1)
    root.rowconfigure(3, weight=1)

    # Make sure the updater window is shown first and grabs attention before the
    # monitor opens. Topmost is dropped again once the window is on screen.
    root.attributes("-topmost", True)
    root.after(1200, lambda: root.attributes("-topmost", False))

    status_var = tk.StringVar(value="Updating local copy before opening the monitor...")
    step_var = tk.StringVar(value=f"Step 0 / {TOTAL_STEPS}")
    ttk.Label(root, text="PanamaCompra updater", font=("Sans", 16, "bold")).grid(row=0, column=0, sticky="w", padx=14, pady=(14, 4))
    ttk.Label(root, textvariable=status_var).grid(row=1, column=0, sticky="w", padx=14, pady=(0, 2))
    ttk.Label(root, textvariable=step_var).grid(row=2, column=0, sticky="w", padx=14, pady=(0, 8))
    text = tk.Text(root, bg="#020617", fg="#e5e7eb", insertbackground="#e5e7eb", wrap="word")
    scroll = ttk.Scrollbar(root, orient="vertical", command=text.yview)
    text.configure(yscrollcommand=scroll.set)
    text.grid(row=3, column=0, sticky="nsew", padx=(14, 0), pady=(0, 14))
    scroll.grid(row=3, column=1, sticky="ns", padx=(0, 14), pady=(0, 14))
    bar = ttk.Progressbar(root, mode="determinate", maximum=TOTAL_STEPS)
    bar.grid(row=4, column=0, columnspan=2, sticky="ew", padx=14, pady=(0, 14))

    proc_holder: dict[str, subprocess.Popen[str] | None] = {"proc": None}

    def append(value: str) -> None:
        text.configure(state="normal")
        text.insert("end", value)
        text.see("end")
        text.configure(state="disabled")

    def mark_step(value: str) -> None:
        match = STEP_RE.match(value)
        if not match:
            return
        step = min(TOTAL_STEPS, int(match.group(1)))
        bar["value"] = step
        step_var.set(f"Step {step} / {TOTAL_STEPS}: {value.strip()[:60]}")

    def worker() -> None:
        with log_file.open("w", encoding="utf-8") as out:
            proc = subprocess.Popen([str(UPDATE_SCRIPT)], cwd=BASE_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
            proc_holder["proc"] = proc
            assert proc.stdout is not None
            for line in proc.stdout:
                out.write(line)
                out.flush()
                root.after(0, append, line)
                root.after(0, mark_step, line)
            code = proc.wait()

        def finish() -> None:
            if code == 0:
                bar["value"] = TOTAL_STEPS
                step_var.set(f"Step {TOTAL_STEPS} / {TOTAL_STEPS}: done")
                status_var.set("Update completed. Opening the monitor with the updated code...")
                # Open the monitor only AFTER the update finished, then close the
                # loader so the sequence is clearly "update first, monitor after".
                if args.open_monitor_after:
                    root.after(900, open_monitor)
                    root.after(2600, root.destroy)
                else:
                    root.after(1500, root.destroy)
            else:
                status_var.set(f"Update failed with exit {code}. Review the log: {log_file.relative_to(BASE_DIR)}")

        root.after(0, finish)

    def on_close() -> None:
        proc = proc_holder.get("proc")
        if proc and proc.poll() is None:
            proc.terminate()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    threading.Thread(target=worker, daemon=True).start()
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
