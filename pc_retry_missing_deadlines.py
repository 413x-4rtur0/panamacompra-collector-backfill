#!/usr/bin/env python3
"""Re-download records whose folder/DB has no DTEND/deadline and rename them.

Use this when old archives were saved as ``(NO-DATE)-(...)`` or when the
``finish_date_guess`` column is blank.  The tool lists matching records by
default. With ``--apply`` it force re-fetches each live detail page, rewrites the
saved detail archive, recalculates DTSTART/DTEND from the current parser, and
lets the detail downloader rename the folder to the current
``(<finish>)-(<numero>)-(<desc>)`` convention.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from pc_common import init_db
from pc_update_day_folder import ensure_playwright_available


def _text(value) -> str:
    return str(value or "").strip()


def folder_needs_deadline(record_folder: str) -> bool:
    """True when the folder leaf explicitly carries no close date/deadline."""
    leaf = Path(record_folder or "").name.upper()
    if not leaf:
        return True
    return leaf.startswith("(NO-DATE)") or leaf.startswith("NO-DATE")


def row_needs_deadline(row) -> bool:
    """True when SQLite or the current folder name is missing DTEND/deadline."""
    return not _text(row["finish_date_guess"]) or folder_needs_deadline(row["record_folder"])


def rows_missing_deadline(conn, limit: int = 0):
    sql = """
    SELECT *
    FROM opportunities
    WHERE COALESCE(link, '') <> ''
      AND COALESCE(record_folder, '') <> ''
      AND (
        COALESCE(finish_date_guess, '') = ''
        OR UPPER(COALESCE(record_folder_leaf, '')) LIKE '(NO-DATE)%'
        OR UPPER(COALESCE(record_folder, '')) LIKE '%/(NO-DATE)%'
      )
    ORDER BY COALESCE(detail_saved_at, first_seen, '') DESC, numero DESC
    """
    if limit > 0:
        sql += " LIMIT ?"
        return conn.execute(sql, (limit,)).fetchall()
    return conn.execute(sql).fetchall()


def redownload_missing_deadlines(conn, rows) -> tuple[int, int, int]:
    """Force redownload selected rows. Returns (saved, renamed_or_fixed, failed)."""
    ensure_playwright_available()
    from playwright.sync_api import sync_playwright
    from pc_detail_downloader import process_detail

    saved = fixed = failed = 0
    total = len(rows)
    with sync_playwright() as p:
        browser = p.firefox.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--window-size=1280,720"],
        )
        try:
            for index, row in enumerate(rows, start=1):
                before_folder = _text(row["record_folder"])
                before_finish = _text(row["finish_date_guess"])
                print(f"[{index}/{total}] {row['numero']}  finish={before_finish or 'NO-DATE'}  folder={before_folder}", flush=True)
                result = process_detail(browser, conn, row, force=True)
                refreshed = conn.execute(
                    "SELECT finish_date_guess, record_folder FROM opportunities WHERE numero = ?",
                    (row["numero"],),
                ).fetchone()
                after_finish = _text(refreshed["finish_date_guess"] if refreshed else "")
                after_folder = _text(refreshed["record_folder"] if refreshed else "")
                if result == "saved":
                    saved += 1
                    if after_finish and not folder_needs_deadline(after_folder):
                        fixed += 1
                    print(f"    OK finish={after_finish or 'NO-DATE'} folder={after_folder or before_folder}", flush=True)
                else:
                    failed += 1
                    print(f"    {result}", flush=True)
        finally:
            browser.close()
    return saved, fixed, failed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="Actually re-download and rename matching records (default: dry-run list).")
    parser.add_argument("--limit", type=int, default=0, help="Process/list at most N records (0 = all).")
    args = parser.parse_args()

    conn = init_db()
    rows = rows_missing_deadline(conn, args.limit)
    print(f"{'APPLY' if args.apply else 'DRY-RUN'} | records missing DTEND/deadline: {len(rows)}")
    print("-" * 100)
    for row in rows:
        marker = "folder+db" if folder_needs_deadline(row["record_folder"]) and not _text(row["finish_date_guess"]) else "folder" if folder_needs_deadline(row["record_folder"]) else "db"
        print(f"{marker:9}  {row['numero']:38}  {row['detail_status']:8}  {row['finish_date_guess'] or 'NO-DATE':19}  {row['record_folder']}")
    print("-" * 100)

    if not rows:
        print("No records need deadline repair.")
        return 0
    if not args.apply:
        print("Dry-run only. Re-run with --apply to re-download details and rename fixed folders.")
        return 0

    saved, fixed, failed = redownload_missing_deadlines(conn, rows)
    print("-" * 100)
    print(f"Re-downloaded: {saved} | fixed deadline+folder: {fixed} | failed: {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
