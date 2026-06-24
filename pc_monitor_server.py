#!/usr/bin/env python3
"""Low-power local web monitor for PanamaCompra run-all progress.

The page is loaded once and then polls a small JSON endpoint. That avoids full
browser reloads every few seconds while preserving the same dashboard UI.
"""
from __future__ import annotations

import json
import os
import shlex
import sqlite3
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

BASE_DIR = Path(__file__).resolve().parent
PROGRESS_FILE = BASE_DIR / "data" / "logs" / "run_all_progress.env"
WORKER_LOG = BASE_DIR / "data" / "logs" / "run_all_worker.log"
CURRENT_LOG = BASE_DIR / "data" / "logs" / "run_all_current.log"
REQUEST_FLAG = BASE_DIR / "data" / "queue" / "run_all_requested.flag"
WAHA_CHAT_ID_PATH = BASE_DIR / "data" / "config" / "waha_chat_id.txt"
MANUAL_ACTION_LOG = BASE_DIR / "data" / "logs" / "manual_actions.log"
ARCHIVE_DB = BASE_DIR / "data" / "panamacompra_archive.db"
HOST = os.environ.get("PC_MONITOR_HOST", "127.0.0.1")
PORT = int(os.environ.get("PC_MONITOR_PORT", "8766"))
REFRESH_SECONDS = max(3, int(os.environ.get("PC_MONITOR_WEB_REFRESH_SECONDS", "3")))
IDLE_REFRESH_SECONDS = max(REFRESH_SECONDS, int(os.environ.get("PC_MONITOR_WEB_IDLE_REFRESH_SECONDS", "30")))
AUTO_CLOSE_SECONDS = max(0, int(os.environ.get("PC_MONITOR_WEB_AUTO_CLOSE_SECONDS", "20")))


class ManualAction(tuple):
    __slots__ = ()
    zone = property(lambda self: self[0])
    label = property(lambda self: self[1])
    command = property(lambda self: self[2])
    comment = property(lambda self: self[3])
    open_after = property(lambda self: self[4])

    def __new__(cls, zone: str, label: str, command: tuple[str, ...], comment: str, open_after: Path | None = None):
        return tuple.__new__(cls, (zone, label, command, comment, open_after))


RECORDS_TEST_PARENT = BASE_DIR / "records_test"
MANUAL_ACTIONS = [
    ManualAction("Runners", "Run full collector", ("./pc_request_run_all.sh", "99", "RESTART", "20"), "Queues a manual restart run and opens/reuses this monitor."),
    ManualAction("Runners", "Run collector now", ("./pc_run_all_now.sh", "99", "20", "MANUAL"), "Starts the run-all worker immediately for up to 20 index pages per group and 99 detail pages."),
    ManualAction("Runners", "Stop active run", ("./pc_stop_run_all.sh",), "Stops worker/index/detail processes and clears the queued run flag."),
    ManualAction("Runners", "Show run status", ("./pc_run_all_status.sh",), "Writes a process/log status snapshot to the manual action log."),
    ManualAction("Tests", "Test zone", ("./pc_test_zone.py", "--limit", "5", "--apply"), "Re-runs the latest five records in records_test, then opens that sandbox folder.", RECORDS_TEST_PARENT),
    ManualAction("Tests", "Review system", ("./review_panamacompra_system.sh",), "Runs the repository health review and troubleshooting summary."),
    ManualAction("Updater / Migration", "Update local copy", ("./pc_update_loader.py", "--open-monitor-after"), "Opens the centered updater loader, refreshes this checkout/dependencies, then reopens the monitor."),
    ManualAction("Updater / Migration", "Pre-run update only", ("./pc_update_before_run.sh",), "Runs the lightweight git/dependency refresh normally used before worker iterations."),
    ManualAction("Updater / Migration", "Rename folders", ("./pc_rename_record_folders.py", "--apply"), "Normalizes existing record folder names."),
    ManualAction("Updater / Migration", "Migrate records", ("./migrate_previous_records.sh",), "Imports/migrates previous record archives."),
    ManualAction("Settings", "Build detail views", ("./pc_build_detail_views.py", "--apply"), "Rebuilds saved record views, ICS files, and split tables."),
    ManualAction("Settings", "Build calendars", ("./pc_build_calendar.py", "--all"), "Rebuilds calendar import packages."),
    ManualAction("Settings", "Import generated calendars", ("bash", "-lc", "PC_CALENDAR_AUTO_IMPORT=1 ./pc_build_calendar.py --all"), "Rebuilds and opens generated ICS files."),
    ManualAction("Settings", "Webhook listener", ("./pc_start_webhook_listener.sh", "--replace-port-owner"), "Starts/restarts the local webhook listener."),
    ManualAction("Settings", "Open web monitor", ("bash", "-lc", "PC_MONITOR_MODE=web ./pc_open_monitor.sh"), "Starts/opens the browser monitor."),
]

DEFAULT_PROGRESS = {
    "PHASE": "IDLE",
    "STATUS": "DONE",
    "PERCENT": "100",
    "MESSAGE": "No active process.",
    "INDEX_LIMIT": "-",
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


def load_record_index(limit: int = 500) -> list[dict[str, str]]:
    """Read collected records (NUMERO + description + folder/link) from the
    archive DB for the record-index selector. Newest first; never raises."""
    if not ARCHIVE_DB.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{ARCHIVE_DB}?mode=ro", uri=True, timeout=2)
    except sqlite3.Error:
        return []
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT numero, "
            "COALESCE(NULLIF(short_description, ''), descripcion, '') AS descripcion, "
            "COALESCE(record_folder, '') AS record_folder, "
            "COALESCE(link, '') AS link, "
            "COALESCE(detail_status, '') AS detail_status, "
            "COALESCE(detail_saved_at, '') AS detail_saved_at, "
            "COALESCE(finish_date_guess, '') AS finish_date_guess "
            "FROM opportunities "
            "ORDER BY COALESCE(first_seen, '') DESC, numero DESC "
            "LIMIT ?",
            (limit,),
        ).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        conn.close()
    return [
        {
            "numero": str(row["numero"] or ""),
            "descripcion": str(row["descripcion"] or ""),
            "record_folder": str(row["record_folder"] or ""),
            "link": str(row["link"] or ""),
            "detail_status": str(row["detail_status"] or ""),
            "detail_saved_at": str(row["detail_saved_at"] or ""),
            "finish_date_guess": str(row["finish_date_guess"] or ""),
        }
        for row in rows
    ]


def running(pattern: str) -> bool:
    return subprocess.run(["pgrep", "-f", pattern], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def webhook_running() -> bool:
    if running("[w]ebhook_listener.py") or running("[p]ython3? -u ./webhook_listener.py"):
        return True
    try:
        result = subprocess.run(["docker", "compose", "ps", "--status", "running", "webhook"], cwd=BASE_DIR, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=3)
        return "webhook" in result.stdout.lower()
    except Exception:
        return False

def process_snapshot() -> dict[str, bool]:
    worker = running("[p]c_run_all_worker.sh")
    test = running("[p]ython(3)? -u ./pc_test_zone.py")
    webhook = webhook_running()
    return {
        "normal_run": worker and not test,
        "test_run": test,
        "worker": worker,
        "index": running("[p]ython(3)? -u ./pc_index_collector.py"),
        "detail": running("[p]ython(3)? -u ./pc_detail_downloader.py"),
        "calendar": running("[p]ython(3)? -u ./pc_build_calendar.py"),
        "messaging": running("[p]c_notify_new_records.py"),
        "webhook": webhook,
        "request": REQUEST_FLAG.exists(),
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
            open_folder(action.open_after)
        threading.Thread(target=wait_then_open, daemon=True).start()

def status_payload() -> dict[str, object]:
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
        "worker_log": tail(WORKER_LOG, 20),
        "current_log": tail(CURRENT_LOG, 35),
        "server_time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "waha_chat_id": WAHA_CHAT_ID_PATH.read_text(encoding="utf-8", errors="replace").strip() if WAHA_CHAT_ID_PATH.exists() else "",
    }

ACTIONS_JSON = json.dumps([
    {"zone": action.zone, "label": action.label, "comment": action.comment}
    for action in MANUAL_ACTIONS
], ensure_ascii=False)

HTML = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>PanamaCompra Monitor</title>
<style>
body {{ font-family: system-ui, -apple-system, Segoe UI, sans-serif; margin: 24px; background: #0f172a; color: #e5e7eb; }}
a {{ color: #93c5fd; }}
.card {{ background: #111827; border: 1px solid #334155; border-radius: 12px; padding: 18px; margin: 0 0 16px; box-shadow: 0 8px 24px #0004; }}
h1 {{ margin-top: 0; }}
.bar {{ height: 30px; background: #334155; border-radius: 999px; overflow: hidden; border: 1px solid #64748b; }}
.fill {{ height: 100%; width: 0%; background: linear-gradient(90deg, #22c55e, #38bdf8); display: flex; align-items: center; justify-content: center; color: #020617; font-weight: 700; transition: width .4s ease; }}
table {{ border-collapse: collapse; width: 100%; }}
th, td {{ text-align: left; border-bottom: 1px solid #334155; padding: 7px 10px; vertical-align: top; }}
th {{ width: 220px; color: #93c5fd; }}
pre {{ white-space: pre-wrap; background: #020617; border: 1px solid #334155; border-radius: 8px; padding: 12px; max-height: 360px; overflow: auto; }}
/* Process status is a tidy flex grid of small chips instead of one crowded
   wrapped line: green = RUNNING, gray = off, even gaps. */
.proc-wrap {{ display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }}
.pill {{ display: inline-block; padding: 4px 10px; border-radius: 999px; font-weight: 700; font-size: .8rem; }}
.on {{ background: #14532d; color: #bbf7d0; }} .off {{ background: #1f2937; color: #9ca3af; }}
.message {{ font-size: 1.15rem; color: #fef3c7; }}
.small {{ color: #94a3b8; }}
.done {{ color: #bbf7d0; font-weight: 700; }}
button {{ background: #334155; color: #e5e7eb; border: 0; border-radius: 8px; padding: 9px 14px; font-weight: 700; cursor: pointer; margin: 0 8px 8px 0; transition: background .15s ease, transform .05s ease; }}
button:hover {{ background: #475569; }}
button:active {{ transform: translateY(1px); }}
button:disabled {{ background: #1f2937; color: #6b7280; cursor: not-allowed; transform: none; }}
button.primary {{ background: #2563eb; color: #fff; }}
button.primary:hover {{ background: #1d4ed8; }}
button.primary:disabled {{ background: #1e293b; color: #6b7280; }}
.zone {{ margin-top: 14px; padding-top: 8px; border-top: 1px solid #334155; }}
.zone h3 {{ margin: 0 0 8px; color: #fef3c7; }}
.danger {{ background: #dc2626; color: #fff; }} .danger:hover {{ background: #b91c1c; }}
textarea {{ width: 100%; min-height: 80px; border-radius: 8px; border: 1px solid #475569; background: #020617; color: #e5e7eb; padding: 10px; }}
select, input {{ border-radius: 8px; border: 1px solid #475569; background: #020617; color: #e5e7eb; padding: 6px 8px; font-size: 1rem; }}
input:disabled {{ opacity: .5; cursor: not-allowed; }}
/* Run mode as radio toggles. */
.mode-group {{ display: inline-flex; gap: 4px; vertical-align: middle; }}
.mode-group label {{ display: inline-flex; align-items: center; gap: 6px; padding: 6px 12px; border: 1px solid #475569; border-radius: 8px; background: #020617; cursor: pointer; font-weight: 700; color: #cbd5e1; }}
.mode-group input {{ accent-color: #2563eb; margin: 0; }}
.mode-group label:has(input:checked) {{ border-color: #2563eb; color: #93c5fd; background: #0b1220; }}
.mode-group input:disabled + span, .mode-group label:has(input:disabled) {{ opacity: .5; cursor: not-allowed; }}
select#record-index {{ min-width: 60%; max-width: 100%; }}
#diagnostics td {{ font-variant-numeric: tabular-nums; word-break: break-word; user-select: text; }}
/* Slim dark scrollbars for the log panes. */
pre::-webkit-scrollbar {{ width: 10px; height: 10px; }}
pre::-webkit-scrollbar-track {{ background: #0f172a; border-radius: 8px; }}
pre::-webkit-scrollbar-thumb {{ background: #334155; border-radius: 8px; }}
pre::-webkit-scrollbar-thumb:hover {{ background: #475569; }}
</style>
</head>
<body>
<div class="card">
  <h1>PanamaCompra Progress Monitor</h1>
  <p class="small"><span id="server-time">Loading...</span> · Low-power polling every <span id="refresh-label">{REFRESH_SECONDS}</span>s while running · JSON: <a href="/api/status">/api/status</a></p>
  <div class="bar"><div class="fill" id="fill">0%</div></div>
  <p class="message" id="message">Loading...</p>
  <p id="done-note" class="done" hidden></p>
  <div id="processes" class="proc-wrap"></div><p class="small">Process pills show live OS processes: detail is off except during STEP 2; webhook should stay RUNNING when the host listener is active.</p>
</div>
<div class="card"><h2>Monitor buttons</h2><p><span class="small" style="margin-right:8px">Mode</span><span class="mode-group" id="run-mode"><label><input type="radio" name="run-mode" value="auto" disabled><span>automatic</span></label><label><input type="radio" name="run-mode" value="restart" checked><span>restart pending</span></label><label><input type="radio" name="run-mode" value="manual"><span>manual run</span></label><label><input type="radio" name="run-mode" value="test"><span>test run</span></label></span> <label class="small">Index limit <input id="index-limit" value="20" size="4"></label> <label class="small">Detail limit <input id="detail-limit" value="99" size="4"></label> <button id="run-button" class="primary" onclick="requestRun()">Request selected run</button><button class="danger" onclick="stopRun()">Stop active run</button><button onclick="saveWaha()">Save WhatsApp destination</button><span id="button-status" class="small"></span></p><p class="small" id="run-hint"><strong>Mode:</strong> automatic is shown for changedetection/webhook runs only; restart pending queues the normal collector; manual run starts the worker now; test run uses the isolated test zone. Index limit controls index pages per status group; detail limit controls detail/test records.</p><textarea id="waha-message" placeholder="WhatsApp group/channel chat ID destination"></textarea><div id="action-zones"></div></div>
<div class="card"><h2>Diagnostics</h2><table id="diagnostics"></table></div>
<div class="card"><h2>Record index</h2><p class="small">Collected records as “[DTEND status] NUMERO — description”, sorted by DTEND (soonest deadline first). Pick one to open its archive folder (on the monitor host) or its portal page.</p><p><label class="small">Status <select id="record-status"><option value="all">All</option><option value="soon">Next to expire</option><option value="expired">Expired</option><option value="upcoming">Upcoming</option></select></label> <label class="small">DTEND on/after <input type="date" id="record-mindate"></label> <span class="small">Legend: <span style="color:#86efac;font-weight:700">upcoming</span> · <span style="color:#fcd34d;font-weight:700">next to expire</span> · <span style="color:#fca5a5;font-weight:700">expired</span></span></p><p><select id="record-index"></select> <button onclick="refreshRecordIndex()">Refresh list</button> <button onclick="openRecordFolder()">Open record folder</button> <button onclick="openRecordPortal()">Open in portal</button></p><p id="record-detail" class="small">Loading record index…</p></div>
<div class="card"><h2>Recent worker log</h2><pre id="worker-log"></pre></div>
<div class="card"><h2>Current action log</h2><pre id="current-log"></pre></div>
<script>
let doneSince = null;
let timer = null;
const actionZones = {ACTIONS_JSON};
const labels = [
  ['Phase', 'PHASE'], ['Status', 'STATUS'], ['Mode', 'MODE'], ['Index limit', 'INDEX_LIMIT'], ['Detail limit', 'DETAIL_LIMIT'],
  ['Step', 'STEP'], ['Item', 'ITEM'], ['Started', 'STARTED_AT'], ['Updated', 'UPDATED_AT'],
  ['Found rows', 'RECORDS_FOUND'], ['New records', 'RECORDS_NEW'], ['Existing records', 'RECORDS_EXISTING'],
  ['Details saved/skipped', 'RECORDS_SAVED'], ['Detail failures', 'RECORDS_FAILED'],
  ['Pending details', 'RECORDS_PENDING'], ['Test records', 'RECORDS_TEST'], ['Extra', 'EXTRA']
];
function esc(value) {{
  return String(value ?? '').replace(/[&<>"']/g, ch => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[ch]));
}}
function render(data) {{
  const p = data.progress || {{}};
  const percent = data.percent || 0;
  document.getElementById('server-time').textContent = 'Server time: ' + (data.server_time || '-');
  document.getElementById('refresh-label').textContent = data.refresh_seconds || {REFRESH_SECONDS};
  const fill = document.getElementById('fill');
  fill.style.width = percent + '%';
  fill.textContent = percent + '%';
  document.getElementById('message').textContent = p.MESSAGE || '';
  const rows = labels.map(([label, key]) => {{
    let value = p[key] ?? '-';
    if (key === 'STEP') value = `${{p.STEP_CURRENT ?? '-'}} / ${{p.STEP_TOTAL ?? '-'}}`;
    if (key === 'ITEM') value = `${{p.ITEM_CURRENT ?? '-'}} / ${{p.ITEM_TOTAL ?? '-'}}`;
    return `<tr><th>${{esc(label)}}</th><td>${{esc(value)}}</td></tr>`;
  }}).join('');
  document.getElementById('diagnostics').innerHTML = rows;
  document.getElementById('processes').innerHTML = Object.entries(data.processes || {{}}).map(([name, value]) =>
    `<span class="pill ${{value ? 'on' : 'off'}}">${{esc(name)}}: ${{value ? 'RUNNING' : 'off'}}</span>`
  ).join('');
  updateRunControls(data);
  document.getElementById('worker-log').textContent = data.worker_log || '';
  document.getElementById('current-log').textContent = data.current_log || '';
  const waha = document.getElementById('waha-message');
  if (waha && document.activeElement !== waha) waha.value = data.waha_chat_id || '';
  const note = document.getElementById('done-note');
  if (data.done) {{
    if (!doneSince) doneSince = Date.now();
    const wait = data.auto_close_enabled ? Number(data.auto_close_seconds || 0) : 0;
    const remaining = Math.max(0, wait - Math.floor((Date.now() - doneSince) / 1000));
    note.hidden = false;
    note.textContent = wait > 0 ? `Live run finished. This monitor will auto-close in about ${{remaining}} seconds.` : 'Run finished. Auto-close is disabled for test zone and manual desktop actions.';
    if (wait > 0 && remaining <= 0) {{
      window.close();
      document.body.innerHTML = '<div class="card"><h1>PanamaCompra monitor finished</h1><p>The run is done. You can close this tab.</p></div>';
      return;
    }}
  }} else {{
    doneSince = null;
    note.hidden = true;
  }}
}}
async function postForm(path, body) {{
  const response = await fetch(path, {{method: 'POST', headers: {{'Content-Type': 'application/x-www-form-urlencoded'}}, body}});
  const text = await response.text();
  document.getElementById('button-status').textContent = text.trim();
  poll();
}}
// Keys that mean real collection work is happening. A webhook-triggered run
// shows up here, so the mode toggle + Request button lock while any is active.
const RUN_WORK_KEYS = ['worker', 'index', 'detail', 'calendar', 'messaging', 'test_run', 'request'];
function updateRunControls(data) {{
  const procs = data.processes || {{}};
  const busy = RUN_WORK_KEYS.some(k => procs[k]);
  document.querySelectorAll('input[name="run-mode"]').forEach(el => {{ el.disabled = busy; }});
  ['index-limit', 'detail-limit'].forEach(id => {{ const el = document.getElementById(id); if (el) el.disabled = busy; }});
  const mode = String(data.MODE || '').toUpperCase();
  if (busy) {{
    const modeValue = mode === 'AUTO' ? 'auto' : mode === 'MANUAL' ? 'manual' : mode === 'TEST' ? 'test' : 'restart';
    const modeInput = document.querySelector(`input[name="run-mode"][value="${{modeValue}}"]`);
    if (modeInput) modeInput.checked = true;
  }} else {{
    const autoInput = document.querySelector('input[name="run-mode"][value="auto"]');
    const restartInput = document.querySelector('input[name="run-mode"][value="restart"]');
    if (autoInput && autoInput.checked && restartInput) restartInput.checked = true;
  }}
  document.querySelector('input[name="run-mode"][value="auto"]').disabled = true;
  const runBtn = document.getElementById('run-button');
  if (runBtn) {{ runBtn.disabled = busy; runBtn.textContent = busy ? 'Run in progress…' : 'Request selected run'; }}
}}
function requestRun() {{
  const checked = document.querySelector('input[name="run-mode"]:checked');
  const mode = encodeURIComponent(checked ? checked.value : 'restart');
  const limit = encodeURIComponent(document.getElementById('run-limit').value || '99');
  postForm('/api/request-run', `mode=${{mode}}&detail_limit=${{limit}}`);
}}
function importCalendars() {{ postForm('/api/import-calendars', ''); }}
function stopRun() {{ postForm('/api/manual-action', 'label=' + encodeURIComponent('Stop active run')); }}
function runAction(label) {{ postForm('/api/manual-action', 'label=' + encodeURIComponent(label)); }}
function renderActionZones() {{
  const root = document.getElementById('action-zones');
  const zones = [...new Set(actionZones.map(a => a.zone))];
  root.innerHTML = zones.map(zone => `<div class="zone"><h3>${{esc(zone)}}</h3>` + actionZones.filter(a => a.zone === zone).map(a => `<button onclick="runAction('${{esc(a.label)}}')">${{esc(a.label)}}</button><span class="small">${{esc(a.comment)}}</span><br>`).join('') + `</div>`).join('');
}}
function saveWaha() {{ postForm('/api/waha-destination', 'chat_id=' + encodeURIComponent(document.getElementById('waha-message').value)); }}
let recordIndex = [];
let recordFiltered = [];
const RECORD_SOON_DAYS = 7;  // DTEND within this many days = "next to expire".
const STATUS_COLOR = {{expired: '#fca5a5', soon: '#fcd34d', upcoming: '#86efac', unknown: '#94a3b8'}};
const STATUS_TAG = {{expired: 'EXPIRED', soon: 'SOON', upcoming: 'ok', unknown: 'no date'}};
function parseDeadline(rec) {{
  const raw = (rec.finish_date_guess || '').trim().replace('_', ' ');
  const m = raw.match(/^(\\d{{4}})-(\\d{{2}})-(\\d{{2}})(?:[ T](\\d{{2}}):(\\d{{2}}))?/);
  if (!m) return null;
  return new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]), Number(m[4] || 12), Number(m[5] || 0));
}}
function expiryStatus(rec) {{
  const dt = parseDeadline(rec);
  if (!dt) return 'unknown';
  const now = new Date();
  if (dt < now) return 'expired';
  if (dt <= new Date(now.getTime() + RECORD_SOON_DAYS * 86400000)) return 'soon';
  return 'upcoming';
}}
function deadlineText(rec) {{ return parseDeadline(rec) ? (rec.finish_date_guess || '').replace('_', ' ') : '—'; }}
function downloadedText(rec) {{ const raw = (rec.detail_saved_at || '').trim(); return raw ? raw.slice(0, 16).replace('T', ' ') : '—'; }}
const FAR_FUTURE = new Date(8640000000000000);
function selectedRecord() {{
  const sel = document.getElementById('record-index');
  const idx = sel ? Number(sel.value) : -1;
  return (idx >= 0 && idx < recordFiltered.length) ? recordFiltered[idx] : null;
}}
function renderRecordDetail() {{
  const rec = selectedRecord();
  const node = document.getElementById('record-detail');
  if (!rec) {{ node.textContent = recordIndex.length ? 'No records match the filter.' : 'No records collected yet. Run the collector, then Refresh list.'; return; }}
  const st = expiryStatus(rec);
  const detail = rec.detail_status ? '   ·   detail: ' + esc(rec.detail_status) : '';
  node.innerHTML = '<span style="color:' + STATUS_COLOR[st] + ';font-weight:700">' + st.toUpperCase() + '</span>  ·  NUMERO: ' + esc(rec.numero) + detail
    + '<br>' + esc(rec.descripcion || '-')
    + '<br>Downloaded: ' + esc(downloadedText(rec)) + '   ·   DTEND (deadline): ' + esc(deadlineText(rec));
}}
function applyRecordFilter() {{
  const status = (document.getElementById('record-status') || {{}}).value || 'all';
  const minRaw = (document.getElementById('record-mindate') || {{}}).value || '';
  const minDate = minRaw ? new Date(minRaw + 'T00:00') : null;
  recordFiltered = recordIndex.filter(r => {{
    if (status !== 'all' && expiryStatus(r) !== status) return false;
    if (minDate) {{ const dt = parseDeadline(r); if (!dt || dt < minDate) return false; }}
    return true;
  }});
  recordFiltered.sort((a, b) => (parseDeadline(a) || FAR_FUTURE) - (parseDeadline(b) || FAR_FUTURE));
  const sel = document.getElementById('record-index');
  sel.innerHTML = recordFiltered.map((r, i) => {{
    const st = expiryStatus(r);
    const tag = parseDeadline(r) ? (r.finish_date_guess || '').slice(2, 10) : 'no date';
    const label = '[' + tag + ' ' + STATUS_TAG[st] + '] ' + (r.numero || '(sin número)') + ' — ' + (r.descripcion || '(sin descripción)');
    return `<option value="${{i}}" style="color:${{STATUS_COLOR[st]}}">${{esc(label)}}</option>`;
  }}).join('');
  renderRecordDetail();
}}
async function refreshRecordIndex() {{
  try {{
    const response = await fetch('/api/record-index', {{cache: 'no-store'}});
    recordIndex = await response.json();
  }} catch (err) {{ recordIndex = []; }}
  applyRecordFilter();
}}
function openRecordFolder() {{
  const rec = selectedRecord();
  if (!rec) {{ document.getElementById('button-status').textContent = 'Select a record first.'; return; }}
  postForm('/api/open-record-folder', 'numero=' + encodeURIComponent(rec.numero));
}}
function openRecordPortal() {{
  const rec = selectedRecord();
  if (!rec || !rec.link) {{ document.getElementById('button-status').textContent = 'No portal link for the selected record.'; return; }}
  window.open(rec.link, '_blank', 'noopener');
}}
async function poll() {{
  try {{
    const response = await fetch('/api/status', {{cache: 'no-store'}});
    const data = await response.json();
    render(data);
    timer = setTimeout(poll, Math.max(3, Number(data.refresh_seconds || {REFRESH_SECONDS})) * 1000);
  }} catch (err) {{
    document.getElementById('message').textContent = 'Monitor connection error: ' + err;
    timer = setTimeout(poll, {IDLE_REFRESH_SECONDS} * 1000);
  }}
}}
window.addEventListener('beforeunload', () => {{ if (timer) clearTimeout(timer); }});
renderActionZones();
document.getElementById('record-index').addEventListener('change', renderRecordDetail);
document.getElementById('record-status').addEventListener('change', applyRecordFilter);
document.getElementById('record-mindate').addEventListener('change', applyRecordFilter);
refreshRecordIndex();
poll();
</script>
</body>
</html>"""


class MonitorHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: object) -> None:
        return

    def send_text(self, status: int, body: str, content_type: str) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", "0") or "0")
        form = parse_qs(self.rfile.read(length).decode("utf-8", errors="replace"))
        if path == "/api/request-run":
            raw_detail = form.get("detail_limit", ["99"])[0].strip()
            raw_index = form.get("index_limit", ["20"])[0].strip()
            detail_limit = raw_detail if raw_detail.isdigit() and int(raw_detail) > 0 else "99"
            index_limit = raw_index if raw_index.isdigit() and int(raw_index) > 0 else "20"
            mode = form.get("mode", ["restart"])[0].strip().lower()
            if mode == "test":
                subprocess.Popen([str(BASE_DIR / "pc_test_zone.py"), "--limit", detail_limit, "--apply"], cwd=BASE_DIR, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.send_text(202, f"Test-zone run requested with detail limit {detail_limit}.\n", "text/plain; charset=utf-8")
                return
            if mode == "manual":
                subprocess.Popen([str(BASE_DIR / "pc_run_all_now.sh"), detail_limit, index_limit, "MANUAL"], cwd=BASE_DIR, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.send_text(202, f"Manual run started with index limit {index_limit}, detail limit {detail_limit}.\n", "text/plain; charset=utf-8")
                return
            subprocess.Popen([str(BASE_DIR / "pc_request_run_all.sh"), detail_limit, "RESTART", index_limit], cwd=BASE_DIR, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.send_text(202, f"Restart-pending run requested with index limit {index_limit}, detail limit {detail_limit}.\n", "text/plain; charset=utf-8")
            return
        if path == "/api/manual-action":
            label = form.get("label", [""])[0].strip()
            for action in MANUAL_ACTIONS:
                if action.label == label:
                    run_manual_action(action)
                    self.send_text(202, f"Started: {action.label}. Output: data/logs/manual_actions.log\n", "text/plain; charset=utf-8")
                    return
            self.send_text(404, "unknown manual action\n", "text/plain; charset=utf-8")
            return
        if path == "/api/import-calendars":
            action = next(action for action in MANUAL_ACTIONS if action.label == "Import generated calendars")
            run_manual_action(action)
            self.send_text(202, "Calendar import started. Output: data/logs/manual_actions.log\n", "text/plain; charset=utf-8")
            return
        if path == "/api/open-record-folder":
            numero = form.get("numero", [""])[0].strip()
            for record in load_record_index():
                if record["numero"] == numero:
                    folder = record["record_folder"]
                    if not folder or not Path(folder).exists():
                        self.send_text(404, f"Record folder not found on disk for {numero}.\n", "text/plain; charset=utf-8")
                        return
                    opener = os.environ.get("PC_OPEN_FOLDER_COMMAND", "xdg-open")
                    subprocess.Popen([opener, folder], cwd=BASE_DIR, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    self.send_text(202, f"Opened record folder for {numero}.\n", "text/plain; charset=utf-8")
                    return
            self.send_text(404, f"Unknown record: {numero}\n", "text/plain; charset=utf-8")
            return
        if path in {"/api/waha-destination", "/api/waha-message"}:
            WAHA_CHAT_ID_PATH.parent.mkdir(parents=True, exist_ok=True)
            chat_id = form.get("chat_id", form.get("message", [""]))[0].strip()
            WAHA_CHAT_ID_PATH.write_text(chat_id + "\n", encoding="utf-8")
            self.send_text(200, "WhatsApp destination saved.\n", "text/plain; charset=utf-8")
            return
        self.send_text(404, "not found\n", "text/plain; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        path = urlparse(self.path).path
        if path == "/health":
            self.send_text(200, "ok\n", "text/plain; charset=utf-8")
            return
        if path == "/api/status":
            self.send_text(200, json.dumps(status_payload(), ensure_ascii=False, indent=2), "application/json; charset=utf-8")
            return
        if path == "/api/record-index":
            self.send_text(200, json.dumps(load_record_index(), ensure_ascii=False), "application/json; charset=utf-8")
            return
        if path in ("/", "/index.html"):
            self.send_text(200, HTML, "text/html; charset=utf-8")
            return
        self.send_text(404, "not found\n", "text/plain; charset=utf-8")


def main() -> None:
    (BASE_DIR / "data" / "logs").mkdir(parents=True, exist_ok=True)
    (BASE_DIR / "data" / "queue").mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((HOST, PORT), MonitorHandler)
    print(f"PanamaCompra web monitor: http://{HOST}:{PORT}/", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
