#!/usr/bin/env python3
"""Weekly visual snapshot of the PanamaCompra Cerradas listing for a given
date range. Reuses the low-level primitives (apply_native_date_filter,
click_group, prepare_base_page, extract_rows, parse_dmy_date) from
037-collect-closed-index.py verbatim (via importlib, since that module's
filename cannot be a normal import target) so the site's documented
date-filter ordering bug can never silently produce a wrong-range snapshot
here -- see apply_native_date_filter's docstring for the exact gotchas.

Deliberately does NOT depend on 037's own prepare_verified_backfill_page /
first_record_publication_date wrapper -- hp15bw's copy of 037 predates
those and doesn't have them, while hp23's does. Reimplementing the same
verify-and-retry loop here from the shared low-level primitives keeps this
script identical on both hosts regardless of which 037 version they run.

This is a read-only, DB-free companion to the structured backfill: it
writes one HTML capture + one extracted-rows JSON per result page into
PC_WEEKLY_SNAPSHOT_ROOT/[device]/[year]/[week_start]_to_[week_end]/, for
later manual review/download. No screenshots -- HTML + JSON only. It
never touches `opportunities` or any other pipeline state.

Required env:
  PC_WEEKLY_SNAPSHOT_ROOT   base snapshots dir, e.g. .../snapshots
  PC_WEEKLY_SNAPSHOT_DEVICE device name, e.g. hp23 / hp15bw
  PC_WEEKLY_SNAPSHOT_START_DATE / _END_DATE  YYYY-MM-DD
Optional:
  PC_WEEKLY_SNAPSHOT_WEEK_DAYS   default 7
"""
import importlib.util
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from playwright.sync_api import sync_playwright

PIPELINE_DIR = Path(__file__).resolve().parent

_spec = importlib.util.spec_from_file_location(
    "closed_index_reuse", PIPELINE_DIR / "037-collect-closed-index.py"
)
closed_index = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(closed_index)

SNAPSHOT_ROOT = Path(os.environ["PC_WEEKLY_SNAPSHOT_ROOT"])
DEVICE = os.environ["PC_WEEKLY_SNAPSHOT_DEVICE"]
START_DATE = date.fromisoformat(os.environ["PC_WEEKLY_SNAPSHOT_START_DATE"])
END_DATE = date.fromisoformat(os.environ["PC_WEEKLY_SNAPSHOT_END_DATE"])
WEEK_DAYS = int(os.environ.get("PC_WEEKLY_SNAPSHOT_WEEK_DAYS", "7"))

DEVICE_ROOT = SNAPSHOT_ROOT / DEVICE
PROGRESS_FILE = DEVICE_ROOT / ".progress.state"


def load_resume_start() -> date:
    """Same idea as 039-run-closed-backfill.sh's window-state file: resume
    after the last verified week instead of replaying the whole range on
    every restart. Identity includes the configured range, so changing the
    range starts fresh."""
    identity = f"{START_DATE.isoformat()}|{END_DATE.isoformat()}|{WEEK_DAYS}"
    if not PROGRESS_FILE.exists():
        return START_DATE
    try:
        saved_identity, next_start = PROGRESS_FILE.read_text().strip().split("|", 1)
    except ValueError:
        return START_DATE
    if saved_identity != identity:
        return START_DATE
    try:
        return date.fromisoformat(next_start)
    except ValueError:
        return START_DATE


def save_resume_start(next_start: date) -> None:
    identity = f"{START_DATE.isoformat()}|{END_DATE.isoformat()}|{WEEK_DAYS}"
    DEVICE_ROOT.mkdir(parents=True, exist_ok=True)
    PROGRESS_FILE.write_text(f"{identity}|{next_start.isoformat()}")


FILTER_VERIFY_ATTEMPTS = int(os.environ.get("PC_WEEKLY_SNAPSHOT_FILTER_ATTEMPTS", "3"))


def apply_verified_filter(page, week_start: date, week_end: date) -> tuple[bool, str]:
    """Same verify-and-retry shape as 037's prepare_verified_backfill_page,
    built only from primitives present in both hosts' 037 copies."""
    last_reason = "first record has no parseable publication date"
    for attempt in range(1, FILTER_VERIFY_ATTEMPTS + 1):
        if attempt > 1:
            closed_index.prepare_base_page(page)

        if not closed_index.apply_native_date_filter(page, week_start, week_end):
            last_reason = "native date filter could not be submitted"
            print(f"Closed: filter attempt {attempt}/{FILTER_VERIFY_ATTEMPTS} failed: {last_reason}")
            continue

        if not closed_index.click_group(page, closed_index.GROUP):
            last_reason = "Closed tab did not become active"
            print(f"Closed: filter attempt {attempt}/{FILTER_VERIFY_ATTEMPTS} failed: {last_reason}")
            continue

        closed_index.set_rows_to_50(page)
        closed_index.go_first_page(page)
        closed_index.wait_for_table(page)

        rows = closed_index.extract_rows(page, 1)
        first_date = closed_index.parse_dmy_date(rows[0]["fecha"]) if rows else None
        if first_date is not None and week_start <= first_date <= week_end:
            print(f"Closed: verified first record within {week_start.isoformat()} to {week_end.isoformat()}.")
            return True, ""

        last_reason = (
            "no rows found" if not rows
            else f"first record publication date {rows[0]['fecha']} is outside "
                 f"{week_start.isoformat()} to {week_end.isoformat()}"
        )
        print(f"Closed: filter attempt {attempt}/{FILTER_VERIFY_ATTEMPTS} failed: {last_reason}; restarting filter.")
    return False, last_reason


def snapshot_week(page, week_start: date, week_end: date) -> bool:
    ok, reason = apply_verified_filter(page, week_start, week_end)
    if not ok:
        print(f"Week {week_start} to {week_end}: verification failed ({reason}); skipping.")
        return False

    out_dir = DEVICE_ROOT / str(week_start.year) / f"{week_start.isoformat()}_to_{week_end.isoformat()}"
    out_dir.mkdir(parents=True, exist_ok=True)

    page_number = 1
    row_total = 0
    while True:
        closed_index.wait_for_table(page)
        rows = closed_index.extract_rows(page, page_number)
        row_total += len(rows)
        (out_dir / f"page_p{page_number}.html").write_text(page.content(), encoding="utf-8")
        (out_dir / f"rows_p{page_number}.json").write_text(
            json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        moved, why = closed_index.click_next(page, closed_index.GROUP)
        if not moved:
            break
        page_number += 1

    (out_dir / "manifest.json").write_text(
        json.dumps(
            {
                "device": DEVICE,
                "week_start": week_start.isoformat(),
                "week_end": week_end.isoformat(),
                "captured_at": datetime.now().isoformat(timespec="seconds"),
                "verified": True,
                "pages": page_number,
                "row_total": row_total,
            },
            indent=2,
        )
    )
    print(
        f"Week {week_start} to {week_end}: {page_number} page(s), {row_total} row(s) "
        f"saved to {out_dir}"
    )
    return True


WEEK_RETRY_ATTEMPTS = int(os.environ.get("PC_WEEKLY_SNAPSHOT_RETRY_ATTEMPTS", "3"))


def main():
    resume_start = load_resume_start()
    if resume_start > START_DATE:
        print(f"Resuming from persisted week: {resume_start.isoformat()}.")

    with sync_playwright() as p:
        browser = p.firefox.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--window-size=1280,720"],
        )
        page = browser.new_page(viewport={"width": 1280, "height": 720})
        closed_index.prepare_base_page(page)

        week_start = resume_start
        while week_start <= END_DATE:
            week_end = min(week_start + timedelta(days=WEEK_DAYS - 1), END_DATE)

            ok = False
            for attempt in range(1, WEEK_RETRY_ATTEMPTS + 1):
                if attempt > 1:
                    closed_index.prepare_base_page(page)
                ok = snapshot_week(page, week_start, week_end)
                if ok:
                    break
                print(
                    f"Week {week_start} to {week_end}: attempt {attempt}/"
                    f"{WEEK_RETRY_ATTEMPTS} failed."
                )

            if not ok:
                print(
                    f"Week {week_start} to {week_end}: gave up after "
                    f"{WEEK_RETRY_ATTEMPTS} attempts; stopping run without "
                    "advancing past this week (resume will retry it)."
                )
                break

            next_start = week_end + timedelta(days=1)
            save_resume_start(next_start)
            week_start = next_start

        browser.close()

    if load_resume_start() > END_DATE:
        PROGRESS_FILE.unlink(missing_ok=True)
        print(f"All weekly snapshots complete for {DEVICE}: {START_DATE} to {END_DATE}.")


if __name__ == "__main__":
    main()
