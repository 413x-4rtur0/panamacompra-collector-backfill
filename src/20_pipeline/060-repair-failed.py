#!/usr/bin/env python3
"""Repair mode (F11): re-download every record stuck at detail_status='failed',
regardless of group, reusing process_detail() unmodified -- same code path
030/037b already use for a normal detail fetch.

Deliberately does NOT rotate user-agent or apply a multi-step backoff, despite
that being the originally proposed design (see docs/audits/MONITOR_AUDIT_REPORT.md
F11). A full manual repair pass against the 347 real failed records accumulated
in production on 2026-07-30 found that 100% were a wedged Playwright/Firefox
connection (WatchdogTimeout), not portal-side blocking -- a single retry on a
freshly launched browser fixed every one of them. So this reuses exactly the
run_with_watchdog + relaunch-on-timeout pattern already proven in
030-collect-details.py / 037b-collect-closed-details.py / 038-collect-
cotizaciones.py, instead of inventing a new retry engine for a failure mode
that doesn't occur.

Bypasses the normal detail_attempts < max_attempts gate on purpose: these rows
already exhausted that budget through the normal queues, which is exactly why
repair mode exists. force=True on process_detail() so a record is always
re-fetched from the live page rather than skipped as "complete" or merely
link-refreshed.

Flag/lock lifecycle (repair_requested.flag, repair_in_progress.flag,
repair_progress.env, repair_last_summary.env) lives in 060b-repair-failed.sh,
not here -- this script only does the DB work and reports progress, same
division of responsibility as run-worker.sh vs the collector steps it calls.
"""
import sys
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import *
from common import _write_env_file

collect_details = load_script("src/20_pipeline/030-collect-details.py", "collect_details_lib")

REPAIR_PROGRESS_PATH = LOG_DIR / "repair_progress.env"
REPAIR_SUMMARY_PATH = LOG_DIR / "repair_last_summary.env"


def repair_limit() -> int:
    """0 = no cap, repair the entire failed backlog in one run -- matches
    what actually worked in production (347/347 in a single pass)."""
    return env_int("PC_REPAIR_LIMIT", "0", minimum=0)


def repair_watchdog_seconds() -> int:
    return env_int("PC_REPAIR_WATCHDOG_SECONDS", "90", minimum=30)


def launch_repair_browser(p):
    return p.firefox.launch(
        headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage", "--window-size=1280,720"],
    )


def failed_detail_rows(conn, limit: int):
    """Every record stuck at detail_status='failed', any group -- Abiertas,
    Programadas, Closed, or Cancelled all share this same terminal state."""
    query = "SELECT * FROM opportunities WHERE detail_status = 'failed' ORDER BY first_seen ASC"
    if limit > 0:
        query += f" LIMIT {int(limit)}"
    return conn.execute(query).fetchall()


def write_progress(phase, status, total, processed, fixed, still_failed, started_at):
    _write_env_file(REPAIR_PROGRESS_PATH, {
        "PHASE": phase,
        "STATUS": status,
        "PERCENT": int(processed * 100 / total) if total else 100,
        "RECORDS_TOTAL": total,
        "RECORDS_PROCESSED": processed,
        "RECORDS_FIXED": fixed,
        "RECORDS_STILL_FAILED": still_failed,
        "STARTED_AT": started_at,
        "UPDATED_AT": now_iso(),
    })


def main():
    conn = init_db()
    rows = failed_detail_rows(conn, repair_limit())
    started_at = now_iso()
    total = len(rows)

    if not total:
        print("No failed detail records to repair.")
        write_progress("DONE", "DONE", 0, 0, 0, 0, started_at)
        return

    watchdog_seconds = repair_watchdog_seconds()
    fixed = still_failed = 0
    write_progress("RUNNING", "RUNNING", total, 0, 0, 0, started_at)

    with sync_playwright() as p:
        browser = launch_repair_browser(p)
        for processed, row in enumerate(rows, start=1):
            numero = row["numero"]
            try:
                result = run_with_watchdog(watchdog_seconds, collect_details.process_detail, browser, conn, row, True)
            except WatchdogTimeout:
                result = "failed"
                print(f"{numero}: FAILED — watchdog timeout after {watchdog_seconds}s, restarting browser",
                      file=sys.stderr)
                try:
                    browser.close()
                except Exception:  # noqa: BLE001
                    pass
                browser = launch_repair_browser(p)
            except Exception as exc:  # noqa: BLE001 - never let one bad record stop the run
                result = "failed"
                print(f"{numero}: FAILED — {exc}", file=sys.stderr)

            if result == "saved":
                fixed += 1
                print(f"{numero}: repaired")
            else:
                still_failed += 1
                print(f"{numero}: still failed", file=sys.stderr)

            write_progress("RUNNING", "RUNNING", total, processed, fixed, still_failed, started_at)
        browser.close()

    write_progress("DONE", "DONE", total, total, fixed, still_failed, started_at)

    summary = (
        f"REPAIR RUN started: {started_at}\n"
        f"REPAIR RUN finished: {now_iso()}\n"
        f"Candidates: {total}\n"
        f"Fixed: {fixed}  Still failed: {still_failed}\n"
    )
    _write_env_file(REPAIR_SUMMARY_PATH, {
        "STARTED_AT": started_at,
        "FINISHED_AT": now_iso(),
        "RECORDS_TOTAL": total,
        "RECORDS_FIXED": fixed,
        "RECORDS_STILL_FAILED": still_failed,
    })
    log_path = LOG_DIR / f"repair_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    log_path.write_text(summary, encoding="utf-8")
    print(summary)


if __name__ == "__main__":
    main()
