#!/usr/bin/env python3
"""Manually re-download every record in one day folder as the current version.

The normal detail downloader writes each record once and then skips it; the
saved HTML/text therefore stays frozen at whatever the portal showed the day it
was first pulled. This tool force re-fetches the live detail page for every
record in a chosen ``YY-MM-DD`` day folder and overwrites the saved HTML, text,
detail JSON and table JSONs (re-deriving the summary/items/calendar views and
re-naming the folder if the finish date or description changed).

Use it when records pulled earlier — e.g. yesterday's folder, captured from a
previous portal version — need to be refreshed to the current one.

    ./src/50_tools/080-update-day-folder.py                 # prompt for the day (default: today), then confirm
    ./src/50_tools/080-update-day-folder.py --date 26-06-18 # target a specific day folder
    ./src/50_tools/080-update-day-folder.py --date yesterday --apply
    ./src/50_tools/080-update-day-folder.py --apply          # today's folder, no confirmation

By default it lists the records and asks before downloading; pass --apply to skip
the confirmation. Targeting works from the database's ``date_folder`` (the day a
record was first seen). It also syncs index-only folders from disk before
listing so records with only ``<NUMERO>.json`` can be fetched. Tip: run an
index scan first if links may have changed.
"""
import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SRC_DIR))
sys.path.insert(0, str(_SRC_DIR / "20_pipeline"))
from common import (
    APP_ROOT,
    RECORDS_DIR,
    archive_complete,
    browser_executable,
    date_folder_name,
    find_existing_opportunity,
    init_db,
    insert_or_update_index,
    now_iso,
)


def normalize_date_folder(value):
    """Normalize user input to the stored ``YY-MM-DD`` day-folder form.

    Accepts 'today'/'yesterday' (and Spanish 'hoy'/'ayer'), YYYY-MM-DD or
    YY-MM-DD with '-' or '/' separators. Unrecognized input is returned as-is.
    """
    s = (value or "").strip().lower()
    if s in ("", "today", "hoy"):
        return date_folder_name()
    if s in ("yesterday", "ayer"):
        return (datetime.now() - timedelta(days=1)).strftime("%y-%m-%d")
    m = re.match(r"^(\d{2}|\d{4})[-/](\d{1,2})[-/](\d{1,2})$", s)
    if m:
        year, month, day = m.groups()
        if len(year) == 4:
            year = year[2:]
        return f"{int(year):02d}-{int(month):02d}-{int(day):02d}"
    return s


def sync_day_folder_indexes(conn, date_folder, records_dir=RECORDS_DIR):
    """Register index-only folders in SQLite so updates can fetch details.

    Some archives may contain only the immutable index JSON because detail
    download did not run yet, or because the SQLite DB was rebuilt after files
    were already on disk.  Before selecting rows for a day update, scan the
    day folder for ``<NUMERO>.json`` files and insert any missing NUMERO into
    the DB with its existing folder path.  This lets ``080-update-day-folder.py``
    catch those records and prevents the next index scan from creating a
    duplicate plain ``NUMERO`` folder when a renamed folder already exists.
    """
    day_dir = Path(records_dir) / date_folder
    if not day_dir.is_dir():
        return 0

    synced = 0
    for index_json_path in sorted(day_dir.glob("*/*.json")):
        if index_json_path.name.endswith(".detail.json"):
            continue
        try:
            data = json.loads(index_json_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue

        numero = str(data.get("numero") or "").strip()
        if not numero or find_existing_opportunity(conn, numero):
            continue

        record_folder = index_json_path.parent
        row = {
            "numero": numero,
            "grupo": data.get("grupo", ""),
            "tipo_url": data.get("tipo_url", ""),
            "estado": data.get("estado", ""),
            "descripcion": data.get("descripcion", ""),
            "short_description": data.get("short_description", data.get("descripcion", "")),
            "entidad": data.get("entidad", ""),
            "dependencia": data.get("dependencia", ""),
            "fecha": data.get("fecha", ""),
            "modalidad": data.get("modalidad", ""),
            "link": data.get("link", ""),
            "first_seen": data.get("first_seen") or now_iso(),
            "last_seen": data.get("last_seen") or now_iso(),
            "date_folder": date_folder,
            "record_folder": str(record_folder),
            "index_json_path": str(index_json_path),
            "detail_status": "saved" if archive_complete(record_folder, numero) else "pending",
            "finish_date_guess": data.get("finish_date_guess", ""),
        }
        insert_or_update_index(conn, row)
        synced += 1
    return synced

def available_date_folders(conn):
    """[(date_folder, count)] for every day present in the database."""
    return [
        (r["date_folder"], r["c"])
        for r in conn.execute(
            "SELECT date_folder, COUNT(*) AS c FROM opportunities "
            "WHERE date_folder IS NOT NULL AND date_folder != '' "
            "GROUP BY date_folder ORDER BY date_folder"
        ).fetchall()
    ]


def rows_for_date(conn, date_folder):
    return conn.execute(
        "SELECT * FROM opportunities WHERE date_folder = ? ORDER BY numero",
        (date_folder,),
    ).fetchall()


def resolve_target_date(args, conn):
    if args.date:
        return normalize_date_folder(args.date)
    today = date_folder_name()
    if sys.stdin.isatty():
        folders = available_date_folders(conn)
        if folders:
            print("Available day folders:")
            for folder, count in folders:
                print(f"  {folder}  ({count} record{'s' if count != 1 else ''})")
        answer = input(f"Which day folder to update? [default: {today}]: ").strip()
        return normalize_date_folder(answer or today)
    return today


def python_has_playwright():
    """Return True when the active Python can import Playwright's sync API."""
    return (
        importlib.util.find_spec("playwright") is not None
        and importlib.util.find_spec("playwright.sync_api") is not None
    )


def project_venv_python():
    """Return this checkout's venv Python path when it exists."""
    candidate = APP_ROOT / ".venv" / "bin" / "python"
    return candidate if candidate.exists() else None


def python_executable_is_current(executable):
    try:
        return Path(sys.executable).resolve() == Path(executable).resolve()
    except OSError:
        return False


def python_can_import_playwright(executable):
    probe = (
        "import importlib.util, sys; "
        "sys.exit(0 if importlib.util.find_spec('playwright.sync_api') else 1)"
    )
    return subprocess.run(
        [str(executable), "-c", probe],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode == 0


def ensure_playwright_available():
    """Use project .venv automatically when it has Playwright, else explain fix."""
    if python_has_playwright():
        return

    venv_python = project_venv_python()
    if venv_python and not python_executable_is_current(venv_python):
        if python_can_import_playwright(venv_python):
            print(f"Re-running with project virtualenv Python: {venv_python}", flush=True)
            os.execv(str(venv_python), [str(venv_python), *sys.argv])

    detail = [
        "Playwright is required only when --apply actually downloads details.",
        f"Current Python does not have Playwright: {sys.executable}",
    ]
    if venv_python:
        detail.append(f"Project virtualenv checked: {venv_python}")
        detail.append(
            "If that virtualenv is broken or incomplete, run ./update-local-copy.sh "
            "so it can move .venv aside and recreate it."
        )
    else:
        detail.append("No project .venv was found in this checkout.")
    detail.extend([
        "Fix options:",
        "  ./update-local-copy.sh",
        "  source .venv/bin/activate && python -m pip install -r requirements.txt",
        "If pip reports ModuleNotFoundError: _posixsubprocess, install python3-venv "
        "and python3-full, then rerun ./update-local-copy.sh.",
    ])
    raise SystemExit("\n".join(detail))


def redownload(conn, rows):
    """Force re-fetch and overwrite the saved detail for each row."""
    # Playwright is only needed for the actual download. Keep dry-run/listing
    # usable without it, but make --apply self-correct to the project .venv when
    # possible and print actionable setup guidance otherwise.
    ensure_playwright_available()
    from playwright.sync_api import sync_playwright

    from collect_detail import process_detail

    saved = failed = 0
    total = len(rows)
    with sync_playwright() as p:
        browser = p.firefox.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--window-size=1280,720",
            ],
        )
        try:
            for index, row in enumerate(rows, start=1):
                print(f"[{index}/{total}] {row['numero']} ...", flush=True)
                result = process_detail(browser, conn, row, force=True)
                if result == "saved":
                    saved += 1
                    print("    OK")
                else:
                    failed += 1
                    print(f"    {result}")
        finally:
            browser.close()
    print(f"\nDone. Re-downloaded: {saved} | failed: {failed}")


def main():
    parser = argparse.ArgumentParser(
        description="Force re-download every record in one YY-MM-DD day folder.",
    )
    parser.add_argument(
        "--date",
        help="day folder to update: YY-MM-DD / YYYY-MM-DD, or 'today'/'yesterday'. "
        "Omit to be prompted (defaults to today; today when non-interactive).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="re-download without the confirmation prompt (default: list, then ask).",
    )
    args = parser.parse_args()

    conn = init_db()
    target = resolve_target_date(args, conn)
    synced = sync_day_folder_indexes(conn, target)
    rows = rows_for_date(conn, target)

    print(f"\nDay folder: {target}  ->  {len(rows)} record(s)")
    if synced:
        print(f"Synced index-only/on-disk record(s) into DB: {synced}")
    print("-" * 80)
    for row in rows:
        print(f"  {row['numero']:38}  {row['detail_status']:8}  {row['record_folder']}")
    print("-" * 80)

    if not rows:
        print("No records found for that day folder. Nothing to do.")
        return

    proceed = args.apply
    if not proceed and sys.stdin.isatty():
        answer = input(
            f"Re-download (force) these {len(rows)} record(s) as the current version? [y/N]: "
        ).strip().lower()
        proceed = answer in ("y", "yes")

    if not proceed:
        print("\nDry-run only. Re-run with --apply (or confirm at the prompt) to re-download.")
        return

    redownload(conn, rows)


if __name__ == "__main__":
    main()
