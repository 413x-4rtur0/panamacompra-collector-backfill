#!/usr/bin/env python3
"""Lightweight native Tk monitor for PanamaCompra run-all progress.

This gives a real desktop window without starting Firefox or a local web server.
It uses only Python's standard library and refreshes on a timer.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import NamedTuple

BASE_DIR = Path(__file__).resolve().parent
CONFIG_DIR = BASE_DIR / "data" / "config"
PROGRESS_FILE = BASE_DIR / "data" / "logs" / "run_all_progress.env"
WORKER_LOG = BASE_DIR / "data" / "logs" / "run_all_worker.log"
CURRENT_LOG = BASE_DIR / "data" / "logs" / "run_all_current.log"
REQUEST_FLAG = BASE_DIR / "data" / "queue" / "run_all_requested.flag"
WAHA_CHAT_ID_PATH = CONFIG_DIR / "waha_chat_id.txt"
WAHA_API_KEY_PATH = CONFIG_DIR / "waha_api_key.txt"
WAHA_KEYWORDS_PATH = CONFIG_DIR / "waha_keywords.txt"
MANUAL_ACTION_LOG = BASE_DIR / "data" / "logs" / "manual_actions.log"
# Editable settings the user can change from the monitor's Settings panel. Saved
# here as KEY=VALUE and consulted at startup (and by the WAHA notifier) so the
# choices survive restarts. Precedence everywhere is: real environment variable >
# this file > built-in default.
SETTINGS_PATH = CONFIG_DIR / "monitor_settings.env"


def load_settings_file() -> dict[str, str]:
    data: dict[str, str] = {}
    if not SETTINGS_PATH.exists():
        return data
    for line in SETTINGS_PATH.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip().strip('"').strip("'")
    return data


_SETTINGS_FILE = load_settings_file()


def setting(name: str, default: str) -> str:
    """Resolve a setting: environment variable first, then the saved settings
    file, then the built-in default."""
    if name in os.environ:
        return os.environ[name]
    return _SETTINGS_FILE.get(name, default)


def setting_int(name: str, default: int, minimum: int | None = None) -> int:
    try:
        value = int(float(setting(name, str(default))))
    except (TypeError, ValueError):
        value = default
    if minimum is not None:
        value = max(minimum, value)
    return value


def setting_float(name: str, default: float, low: float, high: float) -> float:
    try:
        value = float(setting(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return min(high, max(low, value))


REFRESH_SECONDS = setting_int("PC_MONITOR_TK_REFRESH_SECONDS", 3, minimum=2)
IDLE_REFRESH_SECONDS = max(REFRESH_SECONDS, setting_int("PC_MONITOR_TK_IDLE_REFRESH_SECONDS", 15))
# After a LIVE run finishes the monitor shows a centered countdown and then
# closes itself. Default is 20 seconds; set PC_MONITOR_TK_AUTO_CLOSE_SECONDS=0 to
# keep the window open until you close it manually. The countdown only applies to
# completed LIVE runs — test-zone runs and idle/manual states never auto-close
# (see auto_close_enabled in status_snapshot).
AUTO_CLOSE_SECONDS = setting_int("PC_MONITOR_TK_AUTO_CLOSE_SECONDS", 20, minimum=0)
# Window transparency. Tk only supports whole-window opacity, so text/buttons
# share it; 0.85 keeps the window clearly translucent while staying readable.
# Lower it (e.g. 0.50) from the Settings panel for a more see-through look.
# Clamped so the window can never become unreadable/invisible.
ALPHA = setting_float("PC_MONITOR_TK_ALPHA", 0.85, 0.30, 1.0)
LAUNCH_CONTEXT = os.environ.get("PC_MONITOR_LAUNCH_CONTEXT", "manual").strip().lower()
AUTO_CLOSE_CONTEXTS = {"auto", "scheduled", "schedule", "request", "live"}
AUTO_CLOSE_ALLOWED = LAUNCH_CONTEXT in AUTO_CLOSE_CONTEXTS

class ManualAction(NamedTuple):
    zone: str
    label: str
    command: tuple[str, ...]
    comment: str
    open_after: Path | None = None


RECORDS_TEST_PARENT = BASE_DIR / "records_test"


# Manual buttons grouped by zone. Each action's `comment` is shown as a hover
# tooltip on its button (not as an always-visible label), so the grid stays
# compact and readable. Zones are ordered by how often they are used:
# run → keep code fresh → rebuild data → test → open folders. Keep the most
# common/safe action first in each zone and destructive ones clearly labelled.
MANUAL_ACTIONS = [
    # --- 1. Collector Runners: start/stop the live collection ----------------
    ManualAction("Collector Runners", "Request full collection", ("./pc_request_run_all.sh", "99"), "Queues a normal live run (up to 99 detail pages) for the background worker. Safe default action."),
    ManualAction("Collector Runners", "Run collection now", ("./pc_run_all_now.sh", "99"), "Starts the run-all worker immediately for up to 99 detail pages (does not wait for the queue)."),
    ManualAction("Collector Runners", "Show run status", ("./pc_run_all_status.sh",), "Writes a process/log status snapshot to the manual action log."),
    ManualAction("Collector Runners", "STOP all runners", ("./pc_stop_run_all.sh",), "DANGER: stops ALL processes — workers, test zone, calendar builder, monitors, webhook listener and updaters (this monitor closes too)."),

    # --- 2. Updater & Migration: keep code fresh, migrate old data -----------
    ManualAction("Updater & Migration", "Update local copy", ("./pc_update_loader.py", "--open-monitor-after"), "Opens the centered updater window, refreshes the checkout/dependencies (auto-picks latest branch vs main), then reopens the monitor."),
    ManualAction("Updater & Migration", "Pre-run update only", ("./pc_update_before_run.sh",), "Runs the lightweight git/dependency refresh used before worker iterations (no browser install)."),
    ManualAction("Updater & Migration", "Normalize folder names", ("./pc_rename_record_folders.py", "--apply"), "Normalizes existing record folder names using the current naming rules."),
    ManualAction("Updater & Migration", "Migrate old records", ("./migrate_previous_records.sh",), "Imports/migrates previous record archives into the current layout."),

    # --- 3. Data Tools: rebuild views/calendars and integrations -------------
    ManualAction("Data Tools", "Rebuild detail views", ("./pc_build_detail_views.py", "--apply"), "Rebuilds saved record views, ICS files and split tables from stored data (no browser)."),
    ManualAction("Data Tools", "Rebuild calendar packages", ("./pc_build_calendar.py", "--all"), "Rebuilds the calendar import packages (.ics) for all dated record folders."),
    ManualAction("Data Tools", "Import calendars to app", ("bash", "-lc", "PC_CALENDAR_AUTO_IMPORT=1 ./pc_build_calendar.py --all"), "Rebuilds all packages and opens each .ics with the desktop calendar app."),
    ManualAction("Data Tools", "Import to Thunderbird a2gutierrezmora", ("bash", "-lc", "PC_CALENDAR_AUTO_IMPORT=1 PC_CALENDAR_THUNDERBIRD_PROFILE=a2gutierrezmora ./pc_build_calendar.py --all"), "Rebuilds all calendar packages and opens each .ics using Thunderbird profile a2gutierrezmora."),
    ManualAction("Data Tools", "Start webhook listener", ("./webhook_listener.py",), "Starts the local webhook listener in the background; use STOP all runners to halt it."),
    ManualAction("Data Tools", "Open web monitor", ("bash", "-lc", "PC_MONITOR_MODE=web ./pc_open_monitor.sh"), "Starts/opens the optional browser-based monitor at the configured local URL."),

    # --- 4. Testing & Validation: sandbox runs and health checks -------------
    ManualAction("Testing & Validation", "Run test zone", ("./pc_test_zone.py", "--limit", "5", "--apply"), "Re-runs the latest 5 records in the isolated sandbox (records_test/); the real archive is left untouched.", RECORDS_TEST_PARENT),
    ManualAction("Testing & Validation", "Review system health", ("./review_panamacompra_system.sh",), "Runs the repository health checks and troubleshooting summary."),

    # --- 5. Folder Management: open data storage locations -------------------
    ManualAction("Folder Management", "Open index folder", ("bash", "-c", "xdg-open \"$(pwd)/data/index\""), "Opens the main index folder where collected records are stored."),
    ManualAction("Folder Management", "Open records folder", ("bash", "-c", "xdg-open \"$(pwd)/records\""), "Opens the records archive folder containing organized record subfolders."),
    ManualAction("Folder Management", "Open logs folder", ("bash", "-c", "xdg-open \"$(pwd)/data/logs\""), "Opens the logs folder containing worker and action logs."),
    ManualAction("Folder Management", "Open data root", ("bash", "-c", "xdg-open \"$(pwd)/data\""), "Opens the main data directory containing index, logs, queue and config."),
    ManualAction("Folder Management", "Open index parent folder", ("bash", "-c", "xdg-open \"$(dirname \"$(pwd)/data/index\")\""), "Opens the parent directory that contains the index folder."),
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


# Processes that represent actual collection/maintenance WORK. The "done" state
# (and the auto-close countdown) must only depend on these. The monitor window
# itself (monitor_tk), the optional web monitor, the always-on next-run timer and
# the passive webhook listener must NOT count — otherwise the monitor detects
# ITSELF as running and "done" is never reached, so the finish countdown never
# appears.
WORK_PROCESS_KEYS = ("worker", "index", "detail", "calendar", "test_run", "updater", "request")


def is_done(processes: dict[str, bool], progress: dict[str, str]) -> bool:
    if any(processes.get(key) for key in WORK_PROCESS_KEYS):
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
        "auto_close_enabled": AUTO_CLOSE_ALLOWED and done and progress.get("MODE", "LIVE").upper() == "LIVE" and not processes.get("test_run", False),
        "launch_context": LAUNCH_CONTEXT,
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
    geometry = setting("PC_MONITOR_TK_GEOMETRY", "980x760")
    root.geometry(geometry)
    root.configure(bg="#0f172a")

    # Runtime settings the Settings panel can change live without a restart.
    runtime = {
        "alpha": ALPHA,
        "auto_close": AUTO_CLOSE_SECONDS,
        "refresh": REFRESH_SECONDS,
        "idle_refresh": IDLE_REFRESH_SECONDS,
    }

    def apply_alpha(value: float) -> None:
        try:
            root.attributes("-alpha", value)
        except tk.TclError:
            pass

    apply_alpha(runtime["alpha"])

    # Simple hover tooltip: a small borderless popup shown under a widget while
    # the pointer is over it. Used to explain every manual button on hover.
    class Tooltip:
        def __init__(self, widget: tk.Widget, text: str) -> None:
            self.widget = widget
            self.text = text
            self.tip: tk.Toplevel | None = None
            widget.bind("<Enter>", self.show, add="+")
            widget.bind("<Leave>", self.hide, add="+")
            widget.bind("<ButtonPress>", self.hide, add="+")

        def show(self, _event: tk.Event | None = None) -> None:
            if self.tip is not None or not self.text:
                return
            try:
                x = self.widget.winfo_rootx() + 18
                y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
            except tk.TclError:
                return
            self.tip = tk.Toplevel(self.widget)
            self.tip.wm_overrideredirect(True)
            self.tip.wm_geometry(f"+{x}+{y}")
            try:
                self.tip.attributes("-topmost", True)
            except tk.TclError:
                pass
            tk.Label(
                self.tip,
                text=self.text,
                justify="left",
                background="#1f2937",
                foreground="#f8fafc",
                relief="solid",
                borderwidth=1,
                wraplength=380,
                padx=8,
                pady=6,
                font=("Sans", 9),
            ).pack()

        def hide(self, _event: tk.Event | None = None) -> None:
            if self.tip is not None:
                self.tip.destroy()
                self.tip = None

    def add_tooltip(widget: tk.Widget, text: str) -> None:
        Tooltip(widget, text)

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

    # ========================================================================
    # SECTION 1: RUN CONTROLS - request a live or test-zone run
    # ========================================================================
    controls = ttk.Frame(content, style="Card.TFrame", padding=14)
    controls.grid(row=1, column=0, sticky="ew", padx=14, pady=8)
    controls.columnconfigure(5, weight=1)
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

    ttk.Label(controls, text="Run controls", style="Title.TLabel").grid(row=0, column=0, columnspan=6, sticky="w", pady=(0, 8))
    ttk.Label(controls, text="Mode:", style="Card.TLabel").grid(row=1, column=0, sticky="w")
    mode_box = ttk.Combobox(controls, textvariable=run_mode_var, values=("live", "test"), width=8, state="readonly")
    mode_box.grid(row=1, column=1, sticky="w", padx=(0, 8))
    ttk.Label(controls, text="Limit:", style="Card.TLabel").grid(row=1, column=2, sticky="e")
    limit_entry = ttk.Entry(controls, textvariable=run_limit_var, width=8)
    limit_entry.grid(row=1, column=3, sticky="w", padx=(6, 8))
    run_button = ttk.Button(controls, text="Request selected run", command=request_run_now)
    run_button.grid(row=1, column=4, sticky="w")
    ttk.Label(controls, textvariable=button_status_var, style="Card.TLabel", wraplength=520).grid(row=2, column=0, columnspan=6, sticky="w", pady=(8, 0))
    add_tooltip(mode_box, "live = the normal collector pipeline (real archive). test = the isolated test zone (records_test/), real archive untouched.")
    add_tooltip(limit_entry, "Maximum detail pages (live) or sandbox records (test) to process this run.")
    add_tooltip(run_button, "Queue the selected run with the chosen mode and limit.")

    # ========================================================================
    # SECTION 2: SETTINGS - editable fields with defaults; leave as-is to keep
    # the defaults. Saved to data/config/monitor_settings.env and applied live.
    # ========================================================================
    settings = ttk.Frame(content, style="Card.TFrame", padding=14)
    settings.grid(row=2, column=0, sticky="ew", padx=14, pady=8)
    settings.columnconfigure(1, weight=1)
    settings.columnconfigure(3, weight=1)

    alpha_var = tk.StringVar(value=f"{runtime['alpha']:.2f}")
    autoclose_var = tk.StringVar(value=str(runtime["auto_close"]))
    refresh_var = tk.StringVar(value=str(runtime["refresh"]))
    idle_var = tk.StringVar(value=str(runtime["idle_refresh"]))
    source_var = tk.StringVar(value=setting("PC_WAHA_SOURCE", "Panamá Compra"))
    waha_var = tk.StringVar(value=(WAHA_CHAT_ID_PATH.read_text(encoding="utf-8", errors="replace").strip() if WAHA_CHAT_ID_PATH.exists() else "120363175324031424@g.us"))
    waha_api_key_var = tk.StringVar(value="")
    existing_keywords = []
    if WAHA_KEYWORDS_PATH.exists():
        existing_keywords = [k.strip() for k in WAHA_KEYWORDS_PATH.read_text(encoding="utf-8", errors="replace").splitlines() if k.strip() and not k.startswith("#")]
    keywords_var = tk.StringVar(value=", ".join(existing_keywords))

    def field(row: int, col: int, label: str, var: tk.StringVar, width: int, tip: str) -> None:
        ttk.Label(settings, text=label, style="Card.TLabel").grid(row=row, column=col, sticky="w", padx=(0, 6), pady=3)
        entry = ttk.Entry(settings, textvariable=var, width=width)
        entry.grid(row=row, column=col + 1, sticky="ew", pady=3, padx=(0, 12))
        add_tooltip(entry, tip)

    ttk.Label(settings, text="Settings (editable — leave a field unchanged to keep its default)", style="Title.TLabel").grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 8))
    field(1, 0, "Transparency 0.30–1.00:", alpha_var, 8, "Whole-window opacity (text shares it). Default 0.85 = lightly translucent and readable. Lower it toward 0.30 for a more see-through window; 1.00 = fully opaque. Applied live when you click Apply.")
    field(1, 2, "Auto-close seconds (0=off):", autoclose_var, 8, "Seconds to count down after a LIVE run finishes before this window closes. 0 keeps it open. Default 20.")
    field(2, 0, "Active refresh seconds:", refresh_var, 8, "How often (seconds) the monitor refreshes while a run is active. Minimum 2. Default 3.")
    field(2, 2, "Idle refresh seconds:", idle_var, 8, "How often the monitor refreshes when idle (low power). Default 15.")
    field(3, 0, "WhatsApp source label:", source_var, 8, "Text shown as '📌 Fuente:' in the WhatsApp messages (default 'Panamá Compra'). Every new record is announced in real time as its detail downloads.")
    ttk.Label(settings, text="WhatsApp destination chat id (…@g.us):", style="Card.TLabel").grid(row=4, column=0, sticky="w", pady=3)
    chat_entry = ttk.Entry(settings, textvariable=waha_var)
    chat_entry.grid(row=4, column=1, columnspan=3, sticky="ew", pady=3)
    add_tooltip(chat_entry, "Destination WhatsApp group/channel id for NEW and UPDATED alerts. Saved to data/config/waha_chat_id.txt.")
    ttk.Label(settings, text="WAHA API key (plain; blank keeps previous):", style="Card.TLabel").grid(row=5, column=0, sticky="w", pady=3)
    key_entry = ttk.Entry(settings, textvariable=waha_api_key_var, show="*")
    key_entry.grid(row=5, column=1, columnspan=3, sticky="ew", pady=3)
    add_tooltip(key_entry, "Optional X-Api-Key for WAHA. Saved locally to data/config/waha_api_key.txt; data/ is ignored by git.")
    ttk.Label(settings, text="WhatsApp keywords (comma separated; blank = all):", style="Card.TLabel").grid(row=6, column=0, sticky="w", pady=3)
    kw_entry = ttk.Entry(settings, textvariable=keywords_var)
    kw_entry.grid(row=6, column=1, columnspan=3, sticky="ew", pady=3)
    add_tooltip(kw_entry, "Only announce new records matching one of these keywords (title/description/entity). Blank announces every new record. Saved to data/config/waha_keywords.txt.")

    def apply_settings() -> None:
        def as_int(var: tk.StringVar, fallback: int, low: int) -> int:
            try:
                return max(low, int(float(var.get())))
            except (TypeError, ValueError):
                return fallback

        try:
            alpha = min(1.0, max(0.30, float(alpha_var.get())))
        except (TypeError, ValueError):
            alpha = runtime["alpha"]
        runtime["alpha"] = alpha
        apply_alpha(alpha)
        runtime["auto_close"] = as_int(autoclose_var, runtime["auto_close"], 0)
        runtime["refresh"] = as_int(refresh_var, runtime["refresh"], 2)
        runtime["idle_refresh"] = max(runtime["refresh"], as_int(idle_var, runtime["idle_refresh"], 2))

        # Reflect the normalized values back into the entries.
        alpha_var.set(f"{alpha:.2f}")
        autoclose_var.set(str(runtime["auto_close"]))
        refresh_var.set(str(runtime["refresh"]))
        idle_var.set(str(runtime["idle_refresh"]))

        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        WAHA_CHAT_ID_PATH.write_text((waha_var.get().strip() or "120363175324031424@g.us") + "\n", encoding="utf-8")
        if waha_api_key_var.get().strip():
            WAHA_API_KEY_PATH.write_text(waha_api_key_var.get().strip() + "\n", encoding="utf-8")
        keywords = [k.strip() for k in re.split(r"[,\n]", keywords_var.get()) if k.strip()]
        WAHA_KEYWORDS_PATH.write_text(("\n".join(keywords) + "\n") if keywords else "", encoding="utf-8")

        updates = {
            "PC_MONITOR_TK_ALPHA": f"{alpha:.2f}",
            "PC_MONITOR_TK_AUTO_CLOSE_SECONDS": str(runtime["auto_close"]),
            "PC_MONITOR_TK_REFRESH_SECONDS": str(runtime["refresh"]),
            "PC_MONITOR_TK_IDLE_REFRESH_SECONDS": str(runtime["idle_refresh"]),
            "PC_WAHA_SOURCE": source_var.get().strip() or "Panamá Compra",
        }
        merged = load_settings_file()
        merged.update(updates)
        lines = [
            "# PanamaCompra monitor settings (KEY=VALUE).",
            "# Edited from the monitor Settings panel; same-named environment",
            "# variables override these at startup.",
        ]
        lines += [f"{key}={merged[key]}" for key in sorted(merged)]
        SETTINGS_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
        _SETTINGS_FILE.clear()
        _SETTINGS_FILE.update(merged)
        button_status_var.set("Settings applied (transparency live) and saved to data/config/monitor_settings.env.")

    apply_button = ttk.Button(settings, text="Apply & save settings", command=apply_settings)
    apply_button.grid(row=7, column=0, sticky="w", pady=(10, 0))
    add_tooltip(apply_button, "Apply transparency immediately, persist all settings to data/config/monitor_settings.env, and save the WhatsApp destination/keywords files.")
    ttk.Label(settings, text="WhatsApp sending requires WAHA_ENABLED=true (or PC_WAHA_ENABLED=1) and a WAHA server (default port 3000). Source label, destination and keywords here are read by the notifier; every new record is sent in real time as its detail downloads.", style="Card.TLabel", wraplength=820).grid(row=8, column=0, columnspan=4, sticky="w", pady=(8, 0))

    # ========================================================================
    # SECTION 3: DIAGNOSTIC FIELDS - Phase, Mode, Item, Started, etc.
    # This section shows real-time status of the collector process
    # ========================================================================
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

    # ========================================================================
    # SECTION 4: MANUAL ACTION BUTTONS - grouped by zone in a tidy 3-column grid.
    # Each button's explanation is shown as a hover tooltip (not an inline label)
    # so the grid stays compact and easy to scan.
    # ========================================================================
    actions = ttk.Frame(content, style="Card.TFrame", padding=14)
    actions.grid(row=4, column=0, sticky="ew", padx=14, pady=8)
    button_columns = 3
    for col in range(button_columns):
        actions.columnconfigure(col, weight=1, uniform="actions")
    ttk.Label(actions, text="Manual script buttons  (hover a button for what it does)", style="Title.TLabel").grid(row=0, column=0, columnspan=button_columns, sticky="w", pady=(0, 8))

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

    grid_row = 1
    for zone in dict.fromkeys(action.zone for action in MANUAL_ACTIONS):
        ttk.Label(actions, text=zone, style="Message.TLabel").grid(row=grid_row, column=0, columnspan=button_columns, sticky="w", pady=(10, 4))
        grid_row += 1
        zone_actions = [action for action in MANUAL_ACTIONS if action.zone == zone]
        for offset, action in enumerate(zone_actions):
            col = offset % button_columns
            if offset and col == 0:
                grid_row += 1
            button = ttk.Button(actions, text=action.label, command=lambda selected=action: run_manual_action(selected))
            button.grid(row=grid_row, column=col, sticky="ew", padx=4, pady=4)
            add_tooltip(button, action.comment)
        grid_row += 1

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

    # Centered auto-close countdown overlay. It is placed in the exact middle of
    # the window (relx/rely 0.5, anchor center) only while a finished LIVE run is
    # counting down, and removed otherwise. Using place() keeps it on top of the
    # gridded canvas without disturbing the scrollable layout.
    overlay_var = tk.StringVar(value="")
    overlay = tk.Label(
        root,
        textvariable=overlay_var,
        bg="#020617",
        fg="#bbf7d0",
        font=("Sans", 26, "bold"),
        justify="center",
        padx=44,
        pady=30,
        bd=2,
        relief="solid",
        highlightbackground="#22c55e",
        highlightthickness=2,
    )

    done_since: float | None = None
    # Only auto-close after this monitor session has actually watched a run go
    # from active to finished. Opening the monitor straight into a pre-existing
    # idle/done state (e.g. right after an update with no run queued) must NOT
    # start the countdown, otherwise the window would close before any work runs.
    saw_active = False

    def set_text(widget: tk.Text, value: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", value)
        widget.configure(state="disabled")

    def refresh() -> None:
        nonlocal done_since, saw_active
        snap = status_snapshot()
        progress = snap["progress"]
        percent = int(snap["percent"])
        progress_var.set(percent)
        # Refresh cadence comes from the live runtime settings (editable via the
        # Settings panel), not the static snapshot values.
        active_delay = runtime["idle_refresh"] if snap["done"] else runtime["refresh"]
        meta_var.set(f"Time: {snap['time']} · Launch: {snap.get('launch_context', 'manual')} · Transparency: {runtime['alpha']:.2f} · Refresh: {active_delay}s · Progress: {percent}%")
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

        counting_down = False
        if not snap["done"]:
            # A run is active (or starting): remember it so the countdown is
            # allowed once it finishes, and clear any previous countdown state.
            saw_active = True
            done_since = None
            done_var.set("")
            overlay.place_forget()
        elif not saw_active:
            # Opened into a pre-existing idle/done state: show status, no countdown.
            done_since = None
            done_var.set("Idle. Auto-close starts only after a live run finishes while the monitor is open.")
            overlay.place_forget()
        else:
            if done_since is None:
                done_since = time.monotonic()
            wait = runtime["auto_close"] if snap.get("auto_close_enabled") else 0
            remaining = max(0, wait - int(time.monotonic() - done_since))
            if wait:
                counting_down = True
                done_var.set(f"Live run finished. This window will close in {remaining} seconds.")
                overlay_var.set(f"✅ Run finished\n\nClosing in {remaining} s")
                overlay.place(relx=0.5, rely=0.5, anchor="center")
            else:
                done_var.set("Run finished. Auto-close is disabled for manual monitor launches and test-zone/manual desktop actions.")
                overlay.place_forget()
            if wait and remaining <= 0:
                root.destroy()
                return

        # Tick once per second while the countdown is visible so it updates
        # smoothly; otherwise use the normal (slower, low-power) refresh cadence.
        next_delay_ms = 1000 if counting_down else active_delay * 1000
        root.after(next_delay_ms, refresh)

    # Re-apply transparency once the window is actually mapped. On many X11
    # window managers `-alpha` set before the window is visible is silently
    # ignored, so the early apply_alpha() above is not enough on its own. Wait for
    # visibility, then re-apply, and re-apply again shortly after in case a
    # compositor finishes initializing late.
    root.update_idletasks()
    try:
        root.wait_visibility(root)
    except tk.TclError:
        pass
    apply_alpha(runtime["alpha"])
    root.after(300, lambda: apply_alpha(runtime["alpha"]))

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
