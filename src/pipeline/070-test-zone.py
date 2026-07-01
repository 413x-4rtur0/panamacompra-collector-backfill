#!/usr/bin/env python3
"""Testing zone: re-run the last N records through the full pipeline in a sandbox.

When a normal run has no new opportunities, there is nothing to verify current
code against. This re-downloads the most recent N records (default 5) from the
live portal into an ISOLATED sandbox — ``records_test/``, a throwaway in-memory
DB, and timestamped packages under ``records_test/calendar/YY-MM-DD/`` — leaving the real
archive and DB untouched, so you can see how the current code renders them and
diff against the real output. The run is published to the monitor as ``MODE=TEST``
so it is clearly distinct from new (live) records.

    ./src/pipeline/070-test-zone.py                 # list the last 5, then ask
    ./src/pipeline/070-test-zone.py --limit 5 --apply

The run-all worker invokes this automatically (optional STEP 8) only when a normal run
found no new records, so a "nothing new" run still exercises the latest code.
Disable by setting PC_TEST_ZONE_LIMIT=0.
"""
import argparse
import json
import os
import shutil
from datetime import datetime
import sys
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SRC_DIR))
sys.path.insert(0, str(_SRC_DIR / "tools"))
from common import (
    RECORDS_TEST_DIR,
    date_folder_name,
    init_db,
    now_iso,
    safe_name,
    write_run_progress,
)
from build_calendar import write_packages
from update_day_folder import ensure_playwright_available


def recent_rows(conn, limit):
    """The most recently processed records that still have a usable link."""
    if limit <= 0:
        return []
    return conn.execute(
        "SELECT * FROM opportunities WHERE COALESCE(link, '') != '' "
        "ORDER BY datetime(COALESCE(detail_saved_at, last_seen, first_seen)) DESC, numero DESC "
        "LIMIT ?",
        (limit,),
    ).fetchall()


def sandbox_row(real_row):
    """A copy of a record pointed at a fresh folder under records_test/."""
    numero = real_row["numero"]
    date_folder = "latest_5"
    folder = RECORDS_TEST_DIR / date_folder / safe_name(numero)
    folder.mkdir(parents=True, exist_ok=True)
    row = dict(real_row)
    row["record_folder"] = str(folder)
    row["index_json_path"] = str(folder / f"{safe_name(numero)}.json")
    row["date_folder"] = date_folder
    row["detail_status"] = "pending"
    row["detail_attempts"] = 0
    return row


def build_test_calendar(package_size=10):
    """Package sandbox events under records_test/calendar/YY-MM-DD/."""
    calendars = []
    for path in sorted(RECORDS_TEST_DIR.rglob("*.detail.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        calendar = data.get("calendar")
        if calendar:
            calendars.append(calendar)
    calendars.sort(key=lambda c: (c.get("dtstart") or "", c.get("uid") or ""))
    stamp = datetime.now().strftime("%y-%m-%d_%H-%M")
    out_dir = RECORDS_TEST_DIR / "calendar"
    written = write_packages(calendars, out_dir, max(1, int(package_size)), f"{stamp}_panamacompra_test_calendar", date_subdir=date_folder_name()) if calendars else []
    return len(calendars), written


def run_test_zone(rows):
    """Re-download each row into the sandbox and publish TEST progress."""
    ensure_playwright_available()
    from playwright.sync_api import sync_playwright

    from collect_detail import process_detail

    # Fresh sandbox each run so old leaves do not accumulate.
    shutil.rmtree(RECORDS_TEST_DIR, ignore_errors=True)
    RECORDS_TEST_DIR.mkdir(parents=True, exist_ok=True)
    sandbox_conn = init_db(":memory:")  # throwaway schema; the real DB is untouched

    total = len(rows)
    started = now_iso()
    package_size = max(1, int(os.environ.get("PC_CALENDAR_PACKAGE_SIZE", "10")))
    saved = failed = 0
    with sync_playwright() as p:
        browser = p.firefox.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--window-size=1280,720"],
        )
        try:
            for index, real_row in enumerate(rows, start=1):
                numero = real_row["numero"]
                write_run_progress(
                    "TEST", "RUNNING", 55 + int(40 * (index - 1) / max(total, 1)),
                    f"TEST {index}/{total}: re-downloading {numero} into sandbox...",
                    mode="TEST", started_at=started,
                    step_current=1, step_total=1, item_current=index, item_total=total,
                    records_saved=saved, records_failed=failed, records_test=total,
                    extra=f"test_numero={numero}",
                )
                result = process_detail(browser, sandbox_conn, sandbox_row(real_row), force=True)
                if result == "saved":
                    saved += 1
                    print(f"[{index}/{total}] {numero} OK")
                else:
                    failed += 1
                    print(f"[{index}/{total}] {numero} {result}")
        finally:
            browser.close()

    events, calendar_packages = build_test_calendar(package_size=package_size)
    write_run_progress(
        "TEST", "DONE", 100,
        f"Test zone done: re-ran {saved}/{total} in sandbox, {events} calendar events in {len(calendar_packages)} package(s). Real archive untouched.",
        mode="TEST", started_at=started, item_current=total, item_total=total,
        records_saved=saved, records_failed=failed, records_test=total,
    )
    print(f"\nTest zone: re-ran {saved}/{total} into {RECORDS_TEST_DIR} | failed {failed}")
    for path in calendar_packages:
        print(f"Test calendar package: {path}")
    return saved, failed


def main():
    parser = argparse.ArgumentParser(
        description="Re-run the last N records through the full pipeline in an isolated sandbox.",
    )
    parser.add_argument("--limit", type=int, default=5, help="most-recent records to re-run (default 5)")
    parser.add_argument("--apply", action="store_true", help="run without the confirmation prompt")
    args = parser.parse_args()

    conn = init_db()
    rows = recent_rows(conn, max(args.limit, 0))

    print(f"Testing zone: last {len(rows)} record(s) -> sandbox {RECORDS_TEST_DIR} (real archive untouched)")
    print("-" * 80)
    for row in rows:
        print(f"  {row['numero']:38}  {row['detail_status']:8}  {row['record_folder']}")
    print("-" * 80)

    if not rows:
        print("No records available to test.")
        return

    proceed = args.apply
    if not proceed and sys.stdin.isatty():
        answer = input(f"Re-download these {len(rows)} into the sandbox now? [y/N]: ").strip().lower()
        proceed = answer in ("y", "yes")

    if not proceed:
        print("\nDry-run only. Re-run with --apply (or confirm) to run the test zone.")
        return

    run_test_zone(rows)


if __name__ == "__main__":
    main()
