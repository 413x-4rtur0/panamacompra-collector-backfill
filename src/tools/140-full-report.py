#!/usr/bin/env python3
"""Generate a full PanamaCompra diagnostic report.

This is a read-only operator report. It inventories the checkout, runtime paths,
settings, integrations, queues, logs, database counters, key scripts, Docker
state and the changedetection Browser Steps asset, then writes a Markdown report
that can be shared for troubleshooting.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import shlex
import sqlite3
import subprocess
import sys
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parent.parent
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))
import common as pc_common

APP_ROOT = pc_common.APP_ROOT
CONFIG_DIR = pc_common.DATA_CONFIG_DIR
LOG_DIR = pc_common.LOG_DIR
QUEUE_DIR = pc_common.QUEUE_DIR
REPORT_DIR = pc_common.DATA_DIR / "reports"
SETTINGS_PATH = CONFIG_DIR / "monitor_settings.env"
SENSITIVE_MARKERS = ("TOKEN", "KEY", "PASSWORD", "SECRET", "CHAT_ID")

KEY_FILES = [
    "bin/pcc",
    "review-system.sh",
    "config/changedetection-browser-steps.js",
    "src/webhook/010-webhook-listener.py",
    "src/webhook/020-start-listener.sh",
    "src/webhook/040-diagnose-webhook.sh",
    "src/webhook/050-watch-queue-flag.sh",
    "src/pipeline/100-run-worker.sh",
    "src/pipeline/010-collect-index.py",
    "src/pipeline/030-collect-details.py",
    "src/pipeline/020-notify-whatsapp.py",
    "src/notify/010-waha-client.py",
    "src/monitor/001a-monitor-tk.py",
    "src/monitor/001b-monitor-web.py",
    "src/monitor/001c-monitor-terminal.sh",
    "src/tools/010-docker-stack.sh",
    "docker-compose.yml",
]

PROCESS_PATTERNS = {
    "worker": "[1]00-run-worker.sh",
    "index": "[0]10-collect-index.py",
    "detail": "[0]30-collect-details.py",
    "notify": "[0]20-notify-whatsapp.py",
    "webhook": "[0]10-webhook-listener.py",
    "web_monitor": "[0]01b-monitor-web.py",
    "tk_monitor": "[0]01a-monitor-tk.py",
    "terminal_monitor": "[0]01c-monitor-terminal.sh",
}


def run(cmd: list[str], timeout: int = 8) -> tuple[int, str]:
    try:
        proc = subprocess.run(cmd, cwd=APP_ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
        return proc.returncode, proc.stdout.strip()
    except FileNotFoundError:
        return 127, f"command not found: {cmd[0]}"
    except subprocess.TimeoutExpired as exc:
        return 124, (exc.stdout or "").strip() + f"\nTIMEOUT after {timeout}s"
    except Exception as exc:  # noqa: BLE001 - report should continue
        return 1, f"ERROR: {exc}"


def tail(path: Path, lines: int = 25) -> str:
    try:
        data = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "(missing)"
    return "\n".join(data[-lines:]) if data else "(empty)"


def redact_key_value(key: str, value: str) -> str:
    if any(marker in key.upper() for marker in SENSITIVE_MARKERS):
        return "(redacted)" if value else ""
    return value


def parse_settings() -> dict[str, str]:
    data: dict[str, str] = {}
    if not SETTINGS_PATH.exists():
        return data
    for line in SETTINGS_PATH.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        try:
            value = shlex.split(value)[0] if value else ""
        except ValueError:
            value = value.strip().strip('"').strip("'")
        data[key.strip()] = value
    return data


def md_table(rows: list[tuple[str, str]]) -> str:
    out = ["| Item | Value |", "| --- | --- |"]
    for key, value in rows:
        safe = str(value).replace("\n", "<br>")
        out.append(f"| {key} | {safe} |")
    return "\n".join(out)


def database_summary() -> str:
    if not pc_common.DB_PATH.exists():
        return "Database not found."
    try:
        conn = sqlite3.connect(pc_common.DB_PATH)
        conn.row_factory = sqlite3.Row
        cols = {r[1] for r in conn.execute("PRAGMA table_info(opportunities)").fetchall()}
        def count(where: str = "") -> int:
            sql = "SELECT COUNT(*) FROM opportunities" + (f" WHERE {where}" if where else "")
            return int(conn.execute(sql).fetchone()[0])
        rows = [
            ("Total opportunities", str(count())),
            ("Details saved", str(count("detail_status = 'saved'"))),
            ("Details pending", str(count("detail_status = 'pending'"))),
            ("Details failed", str(count("detail_status = 'failed'"))),
        ]
        if "notified_at" in cols:
            rows.append(("WAHA index notified", str(count("notified_at IS NOT NULL"))))
            rows.append(("WAHA index backlog", str(count("notified_at IS NULL"))))
        if "detail_notified_at" in cols:
            rows.append(("WAHA detail notified", str(count("detail_notified_at IS NOT NULL"))))
        by_group = conn.execute("SELECT COALESCE(grupo, '(blank)') AS grupo, COUNT(*) c FROM opportunities GROUP BY grupo ORDER BY c DESC LIMIT 10").fetchall() if "grupo" in cols else []
        conn.close()
        text = md_table(rows)
        if by_group:
            text += "\n\nTop groups:\n" + md_table([(str(r["grupo"]), str(r["c"])) for r in by_group])
        return text
    except sqlite3.Error as exc:
        return f"Database error: {exc}"


def build_report(include_logs: bool = True) -> str:
    now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    settings = parse_settings()
    report: list[str] = [
        "# PanamaCompra Full Diagnostic Report",
        "",
        f"Generated: {now}",
        f"Project: `{APP_ROOT}`",
        "",
        "## Runtime paths",
        md_table([
            ("APP_ROOT", str(APP_ROOT)),
            ("DATA_DIR", str(pc_common.DATA_DIR)),
            ("CONFIG_DIR", str(CONFIG_DIR)),
            ("LOG_DIR", str(LOG_DIR)),
            ("QUEUE_DIR", str(QUEUE_DIR)),
            ("DB_PATH", str(pc_common.DB_PATH)),
            ("RECORDS_DIR", str(pc_common.RECORDS_DIR)),
            ("CALENDAR_DIR", str(pc_common.CALENDAR_DIR)),
        ]),
        "",
        "## Git status",
    ]
    for title, cmd in [("Branch", ["git", "branch", "--show-current"]), ("Status", ["git", "status", "--short"]), ("Latest commit", ["git", "log", "-1", "--oneline"]), ("Remotes", ["git", "remote", "-v"] )]:
        code, out = run(cmd)
        report += [f"### {title}", "```text", out or f"(exit {code}, no output)", "```", ""]

    report += ["## Monitor settings", md_table([(k, redact_key_value(k, v)) for k, v in sorted(settings.items())]) if settings else "No monitor settings saved.", ""]

    token_path = APP_ROOT / ".webhook_token"
    token_exists = token_path.exists() and token_path.stat().st_size > 0
    public_host = os.environ.get("PC_WEBHOOK_PUBLIC_HOST") or settings.get("PC_WEBHOOK_PUBLIC_HOST") or "host.docker.internal"
    port = os.environ.get("PC_WEBHOOK_PORT") or settings.get("PC_WEBHOOK_PORT") or "8765"
    report += [
        "## Integrations",
        md_table([
            ("Webhook token", "present" if token_exists else "missing"),
            ("Recommended changedetection URL", f"json://{public_host}:{port}/panamacompra/<TOKEN>?method=POST&format=text&overflow=truncate&rto=15&cto=10"),
            ("Compose-only URL", "json://webhook:8765/panamacompra/<TOKEN>?method=POST&format=text&overflow=truncate&rto=15&cto=10"),
            ("WAHA enabled", settings.get("PC_WAHA_ENABLED", os.environ.get("PC_WAHA_ENABLED", "0"))),
            ("WAHA base URL", settings.get("PC_WAHA_BASE_URL", os.environ.get("PC_WAHA_BASE_URL", "http://127.0.0.1:3000"))),
            ("changedetection Browser Steps JS", "present" if (APP_ROOT / "config/changedetection-browser-steps.js").exists() else "missing"),
        ]),
        "",
        "## Processes",
    ]
    proc_rows = []
    for name, pattern in PROCESS_PATTERNS.items():
        code, out = run(["pgrep", "-af", pattern], timeout=3)
        proc_rows.append((name, "RUNNING" if code == 0 and out else "off"))
    report += [md_table(proc_rows), ""]

    report += ["## Docker compose", "```text"]
    code, out = run(["docker", "compose", "ps"], timeout=8)
    report += [out or f"(exit {code}, no output)", "```", ""]

    report += ["## Queue flags", md_table([
        ("Collector request", "pending" if (QUEUE_DIR / "run_all_requested.flag").exists() else "none"),
        ("Update request", "pending" if (QUEUE_DIR / "update_monitor_requested.flag").exists() else "none"),
        ("Update in progress", "yes" if (QUEUE_DIR / "update_monitor_in_progress.flag").exists() else "no"),
    ]), ""]

    report += ["## Database summary", database_summary(), ""]

    report += ["## Key files", md_table([(path, "OK" if (APP_ROOT / path).exists() else "MISSING") for path in KEY_FILES]), ""]

    if include_logs:
        report += ["## Recent logs"]
        for label, path in [
            ("run_all_worker.log", LOG_DIR / "run_all_worker.log"),
            ("run_all_current.log", LOG_DIR / "run_all_current.log"),
            ("manual_actions.log", LOG_DIR / "manual_actions.log"),
            ("webhook_listener.log", LOG_DIR / "webhook_listener.log"),
            ("run_all_requests.log", LOG_DIR / "run_all_requests.log"),
        ]:
            report += [f"### {label}", "```text", tail(path, 30), "```", ""]

    report += ["## Quick recommendations", "- If changedetection cannot resolve `webhook`, use the Docker-to-host URL shown above.", "- If WAHA tests do not send, verify the status/default chat id and WAHA session pairing.", "- If logs are empty but a run is pending, check queue flags and process status above.", ""]
    return "\n".join(report)


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a full PanamaCompra diagnostic Markdown report.")
    parser.add_argument("--no-logs", action="store_true", help="Do not include recent log tails.")
    parser.add_argument("--output", help="Write report to this file instead of the default data/reports path.")
    parser.add_argument("--stdout", action="store_true", help="Print the full report to stdout too.")
    args = parser.parse_args()

    text = build_report(include_logs=not args.no_logs)
    if args.output:
        out_path = Path(args.output)
    else:
        stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        out_path = REPORT_DIR / f"full-diagnostic-report-{stamp}.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text + "\n", encoding="utf-8")
    if args.stdout:
        print(text)
    print(f"Full diagnostic report written to: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
