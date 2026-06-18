#!/usr/bin/env python3
"""Small local web monitor for PanamaCompra run-all progress.

This avoids relying on desktop terminal emulators. It serves a self-refreshing
page from localhost using only Python's standard library.
"""
from __future__ import annotations

import html
import json
import os
import shlex
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

BASE_DIR = Path(__file__).resolve().parent
PROGRESS_FILE = BASE_DIR / "data" / "logs" / "run_all_progress.env"
WORKER_LOG = BASE_DIR / "data" / "logs" / "run_all_worker.log"
CURRENT_LOG = BASE_DIR / "data" / "logs" / "run_all_current.log"
REQUEST_FLAG = BASE_DIR / "data" / "queue" / "run_all_requested.flag"
HOST = os.environ.get("PC_MONITOR_HOST", "127.0.0.1")
PORT = int(os.environ.get("PC_MONITOR_PORT", "8766"))

DEFAULT_PROGRESS = {
    "PHASE": "IDLE",
    "STATUS": "DONE",
    "PERCENT": "100",
    "MESSAGE": "No active process.",
    "DETAIL_LIMIT": "-",
    "STARTED_AT": "",
    "UPDATED_AT": "-",
    "WORKER_PID": "-",
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


def process_snapshot() -> dict[str, object]:
    return {
        "worker": running("[p]c_run_all_worker.sh"),
        "index": running("[p]ython -u ./pc_index_collector.py"),
        "detail": running("[p]ython -u ./pc_detail_downloader.py"),
        "request": REQUEST_FLAG.exists(),
    }


def percent_value(progress: dict[str, str]) -> int:
    try:
        return max(0, min(100, int(progress.get("PERCENT", "0"))))
    except ValueError:
        return 0


class MonitorHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: object) -> None:
        # Keep the monitor quiet when it is run by webhook/background scripts.
        return

    def send_text(self, status: int, body: str, content_type: str) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        path = urlparse(self.path).path
        if path == "/health":
            self.send_text(200, "ok\n", "text/plain; charset=utf-8")
            return
        if path == "/api/status":
            payload = {
                "progress": parse_progress_file(),
                "processes": process_snapshot(),
                "worker_log": tail(WORKER_LOG, 20),
                "current_log": tail(CURRENT_LOG, 35),
                "server_time": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            self.send_text(200, json.dumps(payload, ensure_ascii=False, indent=2), "application/json; charset=utf-8")
            return
        if path not in ("/", "/index.html"):
            self.send_text(404, "not found\n", "text/plain; charset=utf-8")
            return

        progress = parse_progress_file()
        processes = process_snapshot()
        percent = percent_value(progress)
        rows = "".join(
            f"<tr><th>{html.escape(label)}</th><td>{html.escape(str(value))}</td></tr>"
            for label, value in [
                ("Phase", progress["PHASE"]),
                ("Status", progress["STATUS"]),
                ("Step", f"{progress['STEP_CURRENT']} / {progress['STEP_TOTAL']}"),
                ("Item", f"{progress['ITEM_CURRENT']} / {progress['ITEM_TOTAL']}"),
                ("Detail limit", progress["DETAIL_LIMIT"]),
                ("Started", progress["STARTED_AT"]),
                ("Updated", progress["UPDATED_AT"]),
                ("Found rows", progress["RECORDS_FOUND"]),
                ("New records", progress["RECORDS_NEW"]),
                ("Existing records", progress["RECORDS_EXISTING"]),
                ("Details saved/skipped", progress["RECORDS_SAVED"]),
                ("Detail failures", progress["RECORDS_FAILED"]),
                ("Pending details", progress["RECORDS_PENDING"]),
                ("Extra", progress["EXTRA"]),
            ]
        )
        proc_cards = "".join(
            f"<span class='pill {'on' if value else 'off'}'>{html.escape(name)}: {'RUNNING' if value else 'off'}</span>"
            for name, value in processes.items()
        )
        body = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="2">
<title>PanamaCompra Monitor</title>
<style>
body {{ font-family: system-ui, -apple-system, Segoe UI, sans-serif; margin: 24px; background: #0f172a; color: #e5e7eb; }}
.card {{ background: #111827; border: 1px solid #334155; border-radius: 12px; padding: 18px; margin: 0 0 16px; box-shadow: 0 8px 24px #0004; }}
h1 {{ margin-top: 0; }}
.bar {{ height: 30px; background: #334155; border-radius: 999px; overflow: hidden; border: 1px solid #64748b; }}
.fill {{ height: 100%; width: {percent}%; background: linear-gradient(90deg, #22c55e, #38bdf8); display: flex; align-items: center; justify-content: center; color: #020617; font-weight: 700; }}
table {{ border-collapse: collapse; width: 100%; }}
th, td {{ text-align: left; border-bottom: 1px solid #334155; padding: 7px 10px; vertical-align: top; }}
th {{ width: 220px; color: #93c5fd; }}
pre {{ white-space: pre-wrap; background: #020617; border: 1px solid #334155; border-radius: 8px; padding: 12px; max-height: 360px; overflow: auto; }}
.pill {{ display: inline-block; margin: 4px 8px 4px 0; padding: 6px 10px; border-radius: 999px; font-weight: 700; }}
.on {{ background: #14532d; color: #bbf7d0; }} .off {{ background: #374151; color: #d1d5db; }}
.message {{ font-size: 1.15rem; color: #fef3c7; }}
.small {{ color: #94a3b8; }}
</style>
</head>
<body>
<div class="card">
  <h1>PanamaCompra Progress Monitor</h1>
  <p class="small">Server time: {html.escape(time.strftime('%Y-%m-%d %H:%M:%S'))} · Auto-refreshes every 2 seconds · JSON: <a href="/api/status">/api/status</a></p>
  <div class="bar"><div class="fill">{percent}%</div></div>
  <p class="message">{html.escape(progress['MESSAGE'])}</p>
  <div>{proc_cards}</div>
</div>
<div class="card"><h2>Diagnostics</h2><table>{rows}</table></div>
<div class="card"><h2>Recent worker log</h2><pre>{html.escape(tail(WORKER_LOG, 20))}</pre></div>
<div class="card"><h2>Current action log</h2><pre>{html.escape(tail(CURRENT_LOG, 35))}</pre></div>
</body>
</html>"""
        self.send_text(200, body, "text/html; charset=utf-8")


def main() -> None:
    (BASE_DIR / "data" / "logs").mkdir(parents=True, exist_ok=True)
    (BASE_DIR / "data" / "queue").mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((HOST, PORT), MonitorHandler)
    print(f"PanamaCompra web monitor: http://{HOST}:{PORT}/", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
