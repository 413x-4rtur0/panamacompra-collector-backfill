#!/usr/bin/env python3
"""Low-resource background collector: the full detail-page archive (HTML,
text, tables, calendar/.ics, items, folder rename) for closed (Cerradas)
opportunities -- the same per-record processing 030-collect-details.py does
for every Abiertas/Programada record, deliberately excluded there so a
background Closed discovery never floods the priority-1 detail queue (see
that file's own comment on detail_pending_rows()). This script gives
Closed records that same full archive, on its own low-resource,
non-competing schedule -- called alongside 037/038 from the Closed-only
orchestrators (priority 2 new-closures, priority 3 backfill), never
priority 1.

Queue: Closed or Cancelled rows with detail_attempts under the limit and
either no saved detail yet, or a stale/incomplete archive on disk -- the
exact same "is this row really done" gate 030-collect-details.py uses
(row_archive_is_current + archive_complete via process_detail's own
completeness check), just scoped to those two groups instead of excluding
them. Reuses process_detail() itself unmodified (imported, not duplicated),
so a closed or cancelled record's detail page is scraped/archived
identically to an open one -- same tables, same calendar/.ics, same
folder-rename logic. A cancelled record simply has no award/cotización
section for process_detail() to find, same as it handles a record at any
other incomplete stage.

Deliberately does NOT reuse 030's own main() loop: that loop's inline
WhatsApp "Detalles Completos" notification is intentionally skipped here
(process_detail() itself never sends anything -- only 030's main() loop
does, via a separate call this script never makes), since Closed stays
completely out of WhatsApp by design, same as 037/038.
"""
import sys
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import *

collect_details = load_script("src/20_pipeline/030-collect-details.py", "collect_details_lib")


def closed_fulldetail_limit() -> int:
    return env_int("PC_CLOSED_FULLDETAIL_LIMIT", "10", minimum=1)


def closed_fulldetail_max_attempts() -> int:
    return env_int("PC_CLOSED_FULLDETAIL_MAX_ATTEMPTS", "3", minimum=1)


def closed_detail_pending_rows(conn, limit: int, max_attempts: int):
    """Same shape/gate as 030-collect-details.py's detail_pending_rows(),
    scoped to grupo IN ('Closed', 'Cancelled') instead of excluding them."""
    candidates = conn.execute("""
    SELECT *
    FROM opportunities
    WHERE detail_attempts < ? AND COALESCE(grupo, '') IN ('Closed', 'Cancelled')
    ORDER BY
      CASE WHEN detail_status = 'saved' THEN 1 ELSE 0 END,
      detail_attempts ASC,
      first_seen ASC
    """, (max_attempts,)).fetchall()
    pending = []
    for row in candidates:
        if row["detail_status"] != "saved" or not collect_details.row_archive_is_current(row):
            pending.append(row)
            if limit > 0 and len(pending) >= limit:
                break
    return pending


def main():
    conn = init_db()
    limit = closed_fulldetail_limit()
    max_attempts = closed_fulldetail_max_attempts()
    rows = closed_detail_pending_rows(conn, limit, max_attempts)
    run_started = now_iso()

    if not rows:
        print("No Closed records pending a full detail fetch.")
        return

    saved = skipped = failed = 0
    with sync_playwright() as p:
        browser = p.firefox.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--window-size=1280,720"],
        )
        for row in rows:
            numero = row["numero"]
            try:
                result = collect_details.process_detail(browser, conn, row)
            except Exception as exc:  # noqa: BLE001 - never let one bad record stop the run
                result = "failed"
                print(f"{numero}: FAILED — {exc}", file=sys.stderr)
            if result == "saved":
                saved += 1
                print(f"{numero}: full detail saved")
            elif result in ("skipped_complete", "refreshed_links"):
                skipped += 1
            else:
                failed += 1
                print(f"{numero}: FAILED", file=sys.stderr)
        browser.close()

    summary = (
        f"CLOSED FULL-DETAIL RUN started: {run_started}\n"
        f"CLOSED FULL-DETAIL RUN finished: {now_iso()}\n"
        f"Queue size this run: {len(rows)} (limit={limit})\n"
        f"Saved: {saved}  Skipped/complete: {skipped}  Failed: {failed}\n"
    )
    log_path = LOG_DIR / f"closed_fulldetail_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    log_path.write_text(summary, encoding="utf-8")
    print(summary)


if __name__ == "__main__":
    main()
