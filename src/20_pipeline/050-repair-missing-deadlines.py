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
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

_SRC_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SRC_DIR))
sys.path.insert(0, str(_SRC_DIR / "50_tools"))
from common import init_db, load_script, safe_name
ensure_playwright_available = load_script("src/50_tools/080-update-day-folder.py").ensure_playwright_available

PORTAL_HOST = "www.panamacompra.gob.pa"
DNS_ERROR_MARKERS = ("NS_ERROR_UNKNOWN_HOST", "ERR_NAME_NOT_RESOLVED", "Name or service not known")


def portal_host_from_rows(rows) -> str:
    """Return the host that will be contacted for these repair downloads."""
    for row in rows:
        host = urlparse(_text(row["link"])).hostname
        if host:
            return host
    return PORTAL_HOST


def check_portal_reachable(host: str = PORTAL_HOST, url: str | None = None, timeout: int = 15) -> tuple[bool, str]:
    """Check DNS and a lightweight HTTPS request before mutating any records.

    The repair command force-downloads live details. If the portal host cannot be
    resolved (the Playwright error shown as NS_ERROR_UNKNOWN_HOST), every record
    would fail for an environment/network reason. Detect that up front so no
    folders are rewritten and no rows are marked failed just because DNS is down.
    """
    target_url = url or f"https://{host}/"
    try:
        socket.getaddrinfo(host, 443)
    except OSError as exc:
        return False, f"DNS lookup failed for {host}: {exc}"

    request = urllib.request.Request(target_url, headers={"User-Agent": "panamacompra-deadline-repair/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return True, f"{host} reachable (HTTP {response.status})"
    except urllib.error.HTTPError as exc:
        # HTTP errors still prove DNS/TLS/connectivity worked; the detail pages
        # may remain reachable even if the root path rejects the probe.
        return True, f"{host} reachable (HTTP {exc.code})"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return False, f"HTTPS probe failed for {target_url}: {exc}"


def error_text_for_row(row) -> str:
    """Read the latest per-record detail error text, if process_detail wrote one."""
    path = Path(row["record_folder"]) / f"{safe_name(row['numero'])}.error.txt"
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def looks_like_dns_error(text: str) -> bool:
    return any(marker in (text or "") for marker in DNS_ERROR_MARKERS)


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


def rows_missing_deadline(conn, limit: int = 0, *, include_failed: bool = False):
    predicates = [
        "COALESCE(finish_date_guess, '') = ''",
        "UPPER(COALESCE(record_folder_leaf, '')) LIKE '(NO-DATE)%'",
        "UPPER(COALESCE(record_folder, '')) LIKE '%/(NO-DATE)%'",
    ]
    if include_failed:
        predicates.append("detail_status = 'failed'")
    sql = f"""
    SELECT *
    FROM opportunities
    WHERE COALESCE(link, '') <> ''
      AND COALESCE(record_folder, '') <> ''
      AND ({' OR '.join(predicates)})
    ORDER BY
      CASE WHEN detail_status = 'failed' THEN 0 ELSE 1 END,
      COALESCE(detail_saved_at, first_seen, '') DESC,
      numero DESC
    """
    if limit > 0:
        sql += " LIMIT ?"
        return conn.execute(sql, (limit,)).fetchall()
    return conn.execute(sql).fetchall()


def redownload_missing_deadlines(conn, rows, *, skip_network_check: bool = False) -> tuple[int, int, int]:
    """Force redownload selected rows. Returns (saved, renamed_or_fixed, failed)."""
    if not skip_network_check:
        host = portal_host_from_rows(rows)
        ok, message = check_portal_reachable(host)
        print(f"Portal connectivity check: {message}", flush=True)
        if not ok:
            print("Aborting before any re-download, DB update, or folder rename. Fix DNS/network/VPN and run again.", flush=True)
            return 0, 0, len(rows)
    ensure_playwright_available()
    from playwright.sync_api import sync_playwright
    from collect_detail import process_detail

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
                    error_text = error_text_for_row(row)
                    print(f"    {result}", flush=True)
                    if looks_like_dns_error(error_text):
                        remaining = total - index
                        print(
                            "    Portal DNS/host resolution failed inside Playwright "
                            f"({error_text.splitlines()[0] if error_text else 'unknown host'}). "
                            f"Aborting remaining {remaining} record(s); fix DNS/network/VPN and run again.",
                            flush=True,
                        )
                        failed += remaining
                        break
        finally:
            browser.close()
    return saved, fixed, failed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="Actually re-download and rename matching records (default: dry-run list).")
    parser.add_argument("--limit", type=int, default=0, help="Process/list at most N records (0 = all).")
    parser.add_argument("--skip-network-check", action="store_true", help="Skip the portal DNS/HTTPS preflight before --apply (not recommended).")
    parser.add_argument("--include-failed", action="store_true", help="Also retry rows whose detail_status is failed, even when they already have a deadline.")
    args = parser.parse_args()

    conn = init_db()
    rows = rows_missing_deadline(conn, args.limit, include_failed=args.include_failed)
    label = "failed or missing DTEND/deadline" if args.include_failed else "missing DTEND/deadline"
    print(f"{'APPLY' if args.apply else 'DRY-RUN'} | records {label}: {len(rows)}")
    print("-" * 100)
    for row in rows:
        marker = "failed" if _text(row["detail_status"]) == "failed" else "folder+db" if folder_needs_deadline(row["record_folder"]) and not _text(row["finish_date_guess"]) else "folder" if folder_needs_deadline(row["record_folder"]) else "db"
        print(f"{marker:9}  {row['numero']:38}  {row['detail_status']:8}  {row['finish_date_guess'] or 'NO-DATE':19}  {row['record_folder']}")
    print("-" * 100)

    if not rows:
        print("No records need deadline repair.")
        return 0
    if not args.apply:
        print("Dry-run only. Re-run with --apply to re-download details and rename fixed folders.")
        return 0

    saved, fixed, failed = redownload_missing_deadlines(conn, rows, skip_network_check=args.skip_network_check)
    print("-" * 100)
    print(f"Re-downloaded: {saved} | fixed deadline+folder: {fixed} | failed: {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
