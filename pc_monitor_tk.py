#!/usr/bin/env python3
"""Lightweight native Tk monitor for PanamaCompra run-all progress.

This gives a real desktop window without starting Firefox or a local web server.
It uses only Python's standard library and refreshes on a timer.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import NamedTuple

BASE_DIR = Path(__file__).resolve().parent
PROGRESS_FILE = BASE_DIR / "data" / "logs" / "run_all_progress.env"
WORKER_LOG = BASE_DIR / "data" / "logs" / "run_all_worker.log"
CURRENT_LOG = BASE_DIR / "data" / "logs" / "run_all_current.log"
REQUEST_FLAG = BASE_DIR / "data" / "queue" / "run_all_requested.flag"
WAHA_CHAT_ID_PATH = BASE_DIR / "data" / "config" / "waha_chat_id.txt"
MANUAL_ACTION_LOG = BASE_DIR / "data" / "logs" / "manual_actions.log"
REFRESH_SECONDS = max(2, int(os.environ.get("PC_MONITOR_TK_REFRESH_SECONDS", "3")))
IDLE_REFRESH_SECONDS = max(REFRESH_SECONDS, int(os.environ.get("PC_MONITOR_TK_IDLE_REFRESH_SECONDS", "15")))
# Default 0 = never auto-close. The monitor is opened manually, so it stays open
# for manual work until the user closes it. Set PC_MONITOR_TK_AUTO_CLOSE_SECONDS
# to a positive number for unattended/automated contexts that should self-close.
AUTO_CLOSE_SECONDS = max(0, int(os.environ.get("PC_MONITOR_TK_AUTO_CLOSE_SECONDS", "0")))

class ManualAction(NamedTuple):
    zone: str
    label: str
    command: tuple[str, ...]
    comment: str
    open_after: Path | None = None


RECORDS_TEST_PARENT = BASE_DIR / "records_test"


MANUAL_ACTIONS = [
    # ========================================================================
    # COLLECTOR RUNNERS - Start, stop, and monitor data collection
    # ========================================================================
    ManualAction("Collector Runners", "Request full collection", ("./pc_request_run_all.sh", "99"), "Queues a normal live run (up to 99 details) and opens the monitor."),
    ManualAction("Collector Runners", "Run collection now", ("./pc_run_all_now.sh", "99"), "Starts the run-all worker immediately in this terminal for up to 99 detail pages."),
    ManualAction("Collector Runners", "STOP all runners", ("./pc_stop_run_all.sh",), "Stops ALL running processes: workers, test zone, calendar builder, monitors, webhook listener, and updaters."),
    ManualAction("Collector Runners", "Show run status", ("./pc_run_all_status.sh",), "Writes a process/log status snapshot to the manual action log."),
    
    # ========================================================================
    # TESTING & VALIDATION - Test zone and system health checks
    # ========================================================================
    ManualAction("Testing & Validation", "Run test zone", ("./pc_test_zone.py", "--limit", "5", "--apply"), "Re-runs the latest 5 records in isolated sandbox (records_test/), leaving real archive untouched.", RECORDS_TEST_PARENT),
    ManualAction("Testing & Validation", "Review system health", ("./review_panamacompra_system.sh",), "Runs repository health checks and troubleshooting summary."),
    
    # ========================================================================
    # UPDATER & MIGRATION - Keep code fresh and migrate data
    # ========================================================================
    ManualAction("Updater & Migration", "Update local copy", ("./pc_update_loader.py", "--open-monitor-after"), "Opens centered updater window, refreshes checkout/dependencies, then reopens monitor."),
    ManualAction("Updater & Migration", "Pre-run update only", ("./pc_update_before_run.sh",), "Runs lightweight git/dependency refresh used before worker iterations."),
    ManualAction("Updater & Migration", "Normalize folder names", ("./pc_rename_record_folders.py", "--apply"), "Normalizes existing record folder names using current naming rules."),
    ManualAction("Updater & Migration", "Migrate old records", ("./migrate_previous_records.sh",), "Imports/migrates previous record archives into the current layout."),
    
    # ========================================================================
    # DATA TOOLS - Rebuild views, calendars, and integrations
    # ========================================================================
    ManualAction("Data Tools", "Rebuild detail views", ("./pc_build_detail_views.py", "--apply"), "Rebuilds saved record views, ICS files, and split tables without browser."),
    ManualAction("Data Tools", "Rebuild calendar packages", ("./pc_build_calendar.py", "--all"), "Rebuilds calendar import packages (.ics) for all dated record folders."),
    ManualAction("Data Tools", "Import calendars to app", ("bash", "-lc", "PC_CALENDAR_AUTO_IMPORT=1 ./pc_build_calendar.py --all"), "Rebuilds all packages and opens each .ics with desktop calendar app."),
    ManualAction("Data Tools", "Start webhook listener", ("./webhook_listener.py",), "Starts local webhook listener in background; use STOP all runners to halt."),
    ManualAction("Data Tools", "Open web monitor", ("bash", "-lc", "PC_MONITOR_MODE=web ./pc_open_monitor.sh"), "Starts/opens optional browser-based monitor at configured local URL."),
]


DEFAULT_PROGRESS = {
    "PHASE": "IDLE",
    "STATUS": "DONE",
    "PERCENT": "100",
    "MESSAGE": "No active process.",
    "DETAIL_LIMIT": "-",
    "STARTED_AT": "",
    "UPDATED_AT": "-",
    "WORKER_PID": "-",
    "MODE": "LIVE",
    "STEP_CURRENT": "-",
    "STEP_TOTAL": "-",
    "ITEM_CURRENT": "-",
    "ITEM_TOTAL": "-",
    "RECORDS_FOUND": "-",
    "RECORDS_NEW": "-",
    "RECORDS_EXISTING": "-",
    "RECORDS_SAVED": "-",
    "RECORDS_FAILED": "-",
    "RECORDS_PENDING": "-",
    "RECORDS_TEST": "-",
    "EXTRA": "-",
}


def parse_progress_file() -> dict[str, str]:
    data = DEFAULT_PROGRESS.copy()
    if not PROGRESS_FILE.exists():
        return data

    for line in PROGRESS_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if key not in data:
            continue
        try:
            parsed = shlex.split(raw_value, posix=True)
            data[key] = parsed[0] if parsed else ""
        except ValueError:
            data[key] = raw_value.strip().strip("'").strip('"')
    return data


def tail(path: Path, lines: int) -> str:
    if not path.exists():
        return f"No {path.name} yet."
    content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(content[-lines:])


def running(pattern: str) -> bool:
    return subprocess.run(["pgrep", "-f", pattern], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def process_snapshot() -> dict[str, bool]:
    """Detect all running PanamaCompra processes for the monitor display."""
    worker = running("[p]c_run_all_worker.sh")
    test = running("[p]ython(3)? -u ./pc_test_zone.py")
    updater = running("[u]pdate_local_copy.sh") or running("[p]c_update_loader.py")
    webhook = running("[w]ebhook_listener.py") or running("[p]ython3? -u ./webhook_listener.py")
    monitor_tk = running("[p]c_monitor_tk.py") or running("[p]ython3? -u ./pc_monitor_tk.py")
    monitor_server = running("[p]c_monitor_server.py") or running("[p]ython3? -u ./pc_monitor_server.py")
    timer = running("[p]c_next_run_timer.py")
    
    return {
        "normal_run": worker and not test,
        "test_run": test,
        "worker": worker,
        "index": running("[p]ython(3)? -u ./pc_index_collector.py"),
        "detail": running("[p]ython(3)? -u ./pc_detail_downloader.py"),
        "calendar": running("[p]ython(3)? -u ./pc_build_calendar.py"),
        "request": REQUEST_FLAG.exists(),
        # Additional runners that should be stopped by pc_stop_run_all.sh
        "updater": updater,
        "webhook": webhook,
        "monitor_tk": monitor_tk,
        "monitor_server": monitor_server,
        "timer": timer,
    }


def percent_value(progress: dict[str, str]) -> int:
    try:
        return max(0, min(100, int(progress.get("PERCENT", "0"))))
    except ValueError:
        return 0


def is_done(processes: dict[str, bool], progress: dict[str, str]) -> bool:
    if any(processes.values()):
        return False
    return progress.get("STATUS") in {"DONE", "FAILED", "TIMEOUT"} or progress.get("PHASE") in {"DONE", "IDLE"}


def status_snapshot() -> dict[str, object]:
    progress = parse_progress_file()
    processes = process_snapshot()
    done = is_done(processes, progress)
    return {
        "progress": progress,
        "percent": percent_value(progress),
        "processes": processes,
        "done": done,
        "auto_close_enabled": done and progress.get("MODE", "LIVE").upper() == "LIVE" and not processes.get("test_run", False),
        "refresh_seconds": IDLE_REFRESH_SECONDS if done else REFRESH_SECONDS,
        "auto_close_seconds": AUTO_CLOSE_SECONDS,
        "worker_log": tail(WORKER_LOG, 18),
        "current_log": tail(CURRENT_LOG, 28),
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "waha_chat_id": WAHA_CHAT_ID_PATH.read_text(encoding="utf-8", errors="replace").strip() if WAHA_CHAT_ID_PATH.exists() else "",
    }


def run_tk() -> int:
    try:
        import tkinter as tk
        from tkinter import ttk
    except Exception as exc:  # pragma: no cover - depends on host packages
        print(f"ERROR: Tkinter is not available: {exc}", file=sys.stderr)
        return 2

    root = tk.Tk()
    root.title("PanamaCompra Progress")
    geometry = os.environ.get("PC_MONITOR_TK_GEOMETRY", "980x760")
    root.geometry(geometry)
    root.configure(bg="#0f172a")
    try:
        root.attributes("-alpha", float(os.environ.get("PC_MONITOR_TK_ALPHA", "0.60")))
    except tk.TclError:
        pass

    def center_window() -> None:
        root.update_idletasks()
        width = root.winfo_width()
        height = root.winfo_height()
        if width <= 1 or height <= 1:
            width, height = [int(part) for part in geometry.split("x", 1)]
        x = max(0, (root.winfo_screenwidth() - width) // 2)
        y = max(0, (root.winfo_screenheight() - height) // 2)
        root.geometry(f"{width}x{height}+{x}+{y}")

    center_window()

    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    style.configure("TFrame", background="#0f172a")
    style.configure("Card.TFrame", background="#111827", relief="solid", borderwidth=1)
    style.configure("TLabel", background="#0f172a", foreground="#e5e7eb")
    style.configure("Card.TLabel", background="#111827", foreground="#e5e7eb")
    style.configure("Title.TLabel", background="#111827", foreground="#e5e7eb", font=("Sans", 16, "bold"))
    style.configure("Message.TLabel", background="#111827", foreground="#fef3c7", font=("Sans", 11, "bold"))
    style.configure("Done.TLabel", background="#111827", foreground="#bbf7d0", font=("Sans", 10, "bold"))
    style.configure("Horizontal.TProgressbar", thickness=26)

    root.columnconfigure(0, weight=1)
    root.rowconfigure(0, weight=1)

    canvas = tk.Canvas(root, bg="#0f172a", highlightthickness=0)
    scrollbar = ttk.Scrollbar(root, orient="vertical", command=canvas.yview)
    canvas.configure(yscrollcommand=scrollbar.set)
    canvas.grid(row=0, column=0, sticky="nsew")
    scrollbar.grid(row=0, column=1, sticky="ns")

    content = ttk.Frame(canvas, style="TFrame")
    content_window = canvas.create_window((0, 0), window=content, anchor="nw")
    content.columnconfigure(0, weight=1)
    content.rowconfigure(5, weight=1)

    def update_scroll_region(_event: tk.Event | None = None) -> None:
        canvas.configure(scrollregion=canvas.bbox("all"))

    def resize_content(event: tk.Event) -> None:
        canvas.itemconfigure(content_window, width=event.width)

    def on_mousewheel(event: tk.Event) -> None:
        # X11 (Linux) delivers wheel events as Button-4 (up) / Button-5 (down)
        # with no usable event.delta, so those events never scrolled the window.
        # Windows/macOS deliver <MouseWheel> with a signed event.delta instead.
        num = getattr(event, "num", 0)
        if num == 4:
            canvas.yview_scroll(-3, "units")
        elif num == 5:
            canvas.yview_scroll(3, "units")
        elif event.delta:
            canvas.yview_scroll(int(-1 * (event.delta / 120)) * 3, "units")

    content.bind("<Configure>", update_scroll_region)
    canvas.bind("<Configure>", resize_content)
    # Bind on all widgets so the wheel scrolls the page no matter where the
    # pointer is. <MouseWheel> covers Windows/macOS; Button-4/5 cover X11/Linux.
    canvas.bind_all("<MouseWheel>", on_mousewheel)
    canvas.bind_all("<Button-4>", on_mousewheel)
    canvas.bind_all("<Button-5>", on_mousewheel)

    header = ttk.Frame(content, style="Card.TFrame", padding=14)
    header.grid(row=0, column=0, sticky="ew", padx=14, pady=(14, 8))
    header.columnconfigure(0, weight=1)

    title = ttk.Label(header, text="PanamaCompra Progress Monitor", style="Title.TLabel")
    title.grid(row=0, column=0, sticky="w")
    meta_var = tk.StringVar(value="Loading...")
    ttk.Label(header, textvariable=meta_var, style="Card.TLabel").grid(row=1, column=0, sticky="w", pady=(4, 8))

    progress_var = tk.IntVar(value=0)
    bar = ttk.Progressbar(header, maximum=100, variable=progress_var, style="Horizontal.TProgressbar")
    bar.grid(row=2, column=0, sticky="ew")
    message_var = tk.StringVar(value="Loading...")
    ttk.Label(header, textvariable=message_var, style="Message.TLabel", wraplength=900).grid(row=3, column=0, sticky="w", pady=(8, 4))
    done_var = tk.StringVar(value="")
    ttk.Label(header, textvariable=done_var, style="Done.TLabel").grid(row=4, column=0, sticky="w")
    processes_var = tk.StringVar(value="")
    ttk.Label(header, textvariable=processes_var, style="Card.TLabel").grid(row=5, column=0, sticky="w", pady=(8, 0))

    controls = ttk.Frame(content, style="Card.TFrame", padding=14)
    controls.grid(row=1, column=0, sticky="ew", padx=14, pady=8)
    controls.columnconfigure(1, weight=1)
    button_status_var = tk.StringVar(value="")
    run_mode_var = tk.StringVar(value="live")
    run_limit_var = tk.StringVar(value="99")

    def selected_limit(default: str = "99") -> str:
        value = run_limit_var.get().strip() or default
        return value if value.isdigit() and int(value) > 0 else default

    def request_run_now() -> None:
        limit = selected_limit()
        if run_mode_var.get() == "test":
            subprocess.Popen([str(BASE_DIR / "pc_test_zone.py"), "--limit", limit, "--apply"], cwd=BASE_DIR, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            button_status_var.set(f"Test-zone run requested with limit {limit}.")
            return
        subprocess.Popen([str(BASE_DIR / "pc_request_run_all.sh"), limit], cwd=BASE_DIR, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        button_status_var.set(f"Live run requested with detail limit {limit}.")

    def save_waha_destination() -> None:
        WAHA_CHAT_ID_PATH.parent.mkdir(parents=True, exist_ok=True)
        WAHA_CHAT_ID_PATH.write_text(waha_var.get().strip() + "\n", encoding="utf-8")
        button_status_var.set("WhatsApp destination saved.")

    ttk.Label(controls, text="Run selector:", style="Card.TLabel").grid(row=0, column=0, sticky="w")
    ttk.Combobox(controls, textvariable=run_mode_var, values=("live", "test"), width=8, state="readonly").grid(row=0, column=1, sticky="w", padx=(0, 8))
    ttk.Label(controls, text="Limit:", style="Card.TLabel").grid(row=0, column=2, sticky="e")
    ttk.Entry(controls, textvariable=run_limit_var, width=8).grid(row=0, column=3, sticky="w", padx=(6, 8))
    ttk.Button(controls, text="Request selected run", command=request_run_now).grid(row=0, column=4, sticky="w")
    ttk.Label(controls, text="Choose live for the normal collector or test for the isolated test-zone script; limit controls detail/test records.", style="Card.TLabel", wraplength=520).grid(row=1, column=0, columnspan=5, sticky="w", pady=(6, 0))
    ttk.Button(controls, text="Save WhatsApp destination", command=save_waha_destination).grid(row=2, column=0, sticky="w", pady=(8, 0))
    ttk.Label(controls, textvariable=button_status_var, style="Card.TLabel").grid(row=2, column=1, columnspan=4, sticky="w", padx=(8, 0), pady=(8, 0))
    ttk.Label(controls, text="WhatsApp group/channel ID for automated 'what is new' messages:", style="Card.TLabel").grid(row=3, column=0, sticky="w", pady=(8, 0))
    waha_var = tk.StringVar(value="")
    ttk.Entry(controls, textvariable=waha_var).grid(row=3, column=1, columnspan=4, sticky="ew", pady=(8, 0))

    actions = ttk.Frame(content, style="Card.TFrame", padding=14)
    actions.grid(row=2, column=0, sticky="ew", padx=14, pady=8)
    actions.columnconfigure(1, weight=1)
    ttk.Label(actions, text="Manual script buttons", style="Title.TLabel").grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 8))

    def open_folder(path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        opener = os.environ.get("PC_OPEN_FOLDER_COMMAND", "xdg-open")
        subprocess.Popen([opener, str(path)], cwd=BASE_DIR, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def run_manual_action(action: ManualAction) -> None:
        MANUAL_ACTION_LOG.parent.mkdir(parents=True, exist_ok=True)
        with MANUAL_ACTION_LOG.open("a", encoding="utf-8") as log_file:
            log_file.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} | {action.zone} / {action.label} =====\n")
            log_file.write("Command: " + " ".join(shlex.quote(part) for part in action.command) + "\n")
            proc = subprocess.Popen(action.command, cwd=BASE_DIR, stdout=log_file, stderr=subprocess.STDOUT)

        if action.open_after is not None:
            def wait_then_open() -> None:
                proc.wait()
                root.after(0, open_folder, action.open_after)
            import threading
            threading.Thread(target=wait_then_open, daemon=True).start()
        button_status_var.set(f"Started: {action.label}. Output: {MANUAL_ACTION_LOG.relative_to(BASE_DIR)}")

    row = 1
    for zone in dict.fromkeys(action.zone for action in MANUAL_ACTIONS):
        ttk.Label(actions, text=zone, style="Message.TLabel").grid(row=row, column=0, columnspan=4, sticky="w", pady=(8, 3))
        row += 1
        zone_actions = [action for action in MANUAL_ACTIONS if action.zone == zone]
        for offset, action in enumerate(zone_actions):
            col = 0 if offset % 2 == 0 else 2
            if offset and offset % 2 == 0:
                row += 1
            ttk.Button(actions, text=action.label, command=lambda selected=action: run_manual_action(selected)).grid(row=row, column=col, sticky="ew", padx=(0, 8), pady=3)
            ttk.Label(actions, text=action.comment, style="Card.TLabel", wraplength=360).grid(row=row, column=col + 1, sticky="w", pady=3)
        row += 1

    diag = ttk.Frame(content, style="Card.TFrame", padding=14)
    diag.grid(row=3, column=0, sticky="ew", padx=14, pady=8)
    for col in range(4):
        diag.columnconfigure(col, weight=1)

    fields = [
        ("Phase", "PHASE"), ("Status", "STATUS"), ("Mode", "MODE"), ("Step", "STEP"), ("Item", "ITEM"),
        ("Detail limit", "DETAIL_LIMIT"), ("Started", "STARTED_AT"), ("Updated", "UPDATED_AT"),
        ("Found", "RECORDS_FOUND"), ("New", "RECORDS_NEW"), ("Existing", "RECORDS_EXISTING"),
        ("Saved/skipped", "RECORDS_SAVED"), ("Failures", "RECORDS_FAILED"),
        ("Pending", "RECORDS_PENDING"), ("Test", "RECORDS_TEST"), ("Extra", "EXTRA"),
    ]
    diag_vars: dict[str, tk.StringVar] = {}
    for idx, (label, key) in enumerate(fields):
        row = idx // 2
        col = (idx % 2) * 2
        ttk.Label(diag, text=f"{label}:", style="Card.TLabel").grid(row=row, column=col, sticky="w", padx=(0, 6), pady=2)
        var = tk.StringVar(value="-")
        diag_vars[key] = var
        ttk.Label(diag, textvariable=var, style="Card.TLabel", wraplength=320).grid(row=row, column=col + 1, sticky="w", pady=2)

    logs = ttk.Frame(content, style="TFrame")
    logs.grid(row=5, column=0, sticky="nsew", padx=14, pady=(8, 14))
    logs.columnconfigure(0, weight=1)
    logs.columnconfigure(1, weight=1)
    logs.rowconfigure(1, weight=1)
    ttk.Label(logs, text="Recent worker log").grid(row=0, column=0, sticky="w")
    ttk.Label(logs, text="Current action log").grid(row=0, column=1, sticky="w")
    worker_text = tk.Text(logs, height=18, bg="#020617", fg="#e5e7eb", insertbackground="#e5e7eb", wrap="word")
    current_text = tk.Text(logs, height=18, bg="#020617", fg="#e5e7eb", insertbackground="#e5e7eb", wrap="word")
    worker_text.grid(row=1, column=0, sticky="nsew", padx=(0, 7))
    current_text.grid(row=1, column=1, sticky="nsew", padx=(7, 0))

    done_since: float | None = None

    def set_text(widget: tk.Text, value: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", value)
        widget.configure(state="disabled")

    def refresh() -> None:
        nonlocal done_since
        snap = status_snapshot()
        progress = snap["progress"]
        percent = int(snap["percent"])
        progress_var.set(percent)
        meta_var.set(f"Time: {snap['time']} · Low-power refresh: {snap['refresh_seconds']}s · Progress: {percent}%")
        message_var.set(str(progress.get("MESSAGE", "")))
        processes_var.set("  ".join(f"{name}: {'RUNNING' if value else 'off'}" for name, value in snap["processes"].items()))

        for key, var in diag_vars.items():
            if key == "STEP":
                var.set(f"{progress.get('STEP_CURRENT', '-')} / {progress.get('STEP_TOTAL', '-')}")
            elif key == "ITEM":
                var.set(f"{progress.get('ITEM_CURRENT', '-')} / {progress.get('ITEM_TOTAL', '-')}")
            else:
                var.set(str(progress.get(key, "-")))

        if not waha_var.get():
            waha_var.set(str(snap.get("waha_chat_id", "")))

        set_text(worker_text, str(snap["worker_log"]))
        set_text(current_text, str(snap["current_log"]))

        if snap["done"]:
            if done_since is None:
                done_since = time.monotonic()
            wait = int(snap["auto_close_seconds"]) if snap.get("auto_close_enabled") else 0
            remaining = max(0, wait - int(time.monotonic() - done_since))
            if wait:
                done_var.set(f"Live run finished. This window will close in {remaining} seconds.")
            else:
                done_var.set("Run finished. Auto-close is disabled for test zone and manual desktop actions.")
            if wait and remaining <= 0:
                root.destroy()
                return
        else:
            done_since = None
            done_var.set("")

        root.after(int(snap["refresh_seconds"]) * 1000, refresh)

    refresh()
    root.mainloop()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="PanamaCompra native Tk progress monitor")
    parser.add_argument("--snapshot", action="store_true", help="print one JSON status snapshot and exit")
    args = parser.parse_args()

    if args.snapshot:
        print(json.dumps(status_snapshot(), ensure_ascii=False, indent=2))
        return 0

    return run_tk()


if __name__ == "__main__":
    raise SystemExit(main())
