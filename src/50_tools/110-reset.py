#!/usr/bin/env python3
"""Reset / "review from zero" helpers, shared by both monitors.

Each action is a separate subcommand so the Tk and web monitors can expose one
button per operation instead of a single ambiguous "reset" button. The two most
destructive actions (``wipe-db`` / ``wipe-all``) refuse to run unless ``--yes``
is passed, so an accidental click cannot erase the archive.

    ./src/50_tools/110-reset.py requeue-details   # re-download every detail page next run
    ./src/50_tools/110-reset.py reset-notify      # re-announce every record from zero (WAHA)
    ./src/50_tools/110-reset.py wipe-db    --yes  # delete the tracking DB (keeps record files)
    ./src/50_tools/110-reset.py wipe-all   --yes  # delete DB + every downloaded record/calendar

All actions print a one-line summary and append it to the manual-action log so
the operator can see what happened from either monitor.
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import (
    CALENDAR_DIR,
    CSV_PATH,
    DB_PATH,
    INDEX_HEADER,
    LOG_DIR,
    RECORDS_DIR,
    RECORDS_TEST_DIR,
    ensure_dirs,
)

MANUAL_ACTION_LOG = LOG_DIR / "manual_actions.log"

# Notification/review bookkeeping columns cleared by reset-notify so the WAHA
# "new record" announcements (and status-change updates) start again from zero.
_NOTIFY_COLUMNS = (
    "notified_at",
    "last_notified_status",
    "last_notified_items_hash",
    "last_notified_signature",
    "pending_status_change",
    "last_calendar_export_path",
)


def _log(message: str) -> None:
    ensure_dirs()
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] pc_reset: {message}"
    print(line)
    try:
        with MANUAL_ACTION_LOG.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def _existing_columns(conn: sqlite3.Connection) -> set[str]:
    return {row[1] for row in conn.execute("PRAGMA table_info(opportunities)").fetchall()}


def requeue_details() -> str:
    """Mark every record's detail as pending again (and clear the attempt count)
    so the next detail step re-downloads / re-reviews all of them."""
    if not DB_PATH.exists():
        return "requeue-details: no database found; nothing to do."
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        cur = conn.execute(
            "UPDATE opportunities SET detail_status = 'pending', detail_attempts = 0"
        )
        conn.commit()
        affected = cur.rowcount
    finally:
        conn.close()
    return f"requeue-details: {affected} record(s) re-queued for detail download."


def reset_notify() -> str:
    """Clear the WAHA notification baseline / per-record review flags so every
    record can be announced again from zero."""
    if not DB_PATH.exists():
        return "reset-notify: no database found; nothing to do."
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        columns = _existing_columns(conn)
        targets = [c for c in _NOTIFY_COLUMNS if c in columns]
        if not targets:
            return "reset-notify: no notification columns present; nothing to do."
        assignments = ", ".join(f"{col} = NULL" for col in targets)
        cur = conn.execute(f"UPDATE opportunities SET {assignments}")
        conn.commit()
        affected = cur.rowcount
    finally:
        conn.close()
    return f"reset-notify: cleared review/notify flags on {affected} record(s)."


def _remove_db_files() -> int:
    removed = 0
    # Remove the DB plus its WAL/SHM sidecars and the first-seen CSV.
    for path in (DB_PATH, Path(f"{DB_PATH}-wal"), Path(f"{DB_PATH}-shm"), CSV_PATH):
        if path.exists():
            path.unlink()
            removed += 1
    return removed


def wipe_db() -> str:
    """Delete the tracking database (and index CSV). Record folders on disk are
    kept; a later run re-links them via find_existing_record_archive."""
    removed = _remove_db_files()
    return f"wipe-db: removed {removed} database/CSV file(s); record folders kept."


def _clear_dir_contents(directory: Path) -> int:
    if not directory.exists():
        return 0
    removed = 0
    for child in directory.iterdir():
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            try:
                child.unlink()
            except OSError:
                continue
        removed += 1
    return removed


def wipe_all() -> str:
    """Delete the DB AND every downloaded record/calendar (a true from-scratch
    re-collection). The isolated test zone is left untouched."""
    db_removed = _remove_db_files()
    records_removed = _clear_dir_contents(RECORDS_DIR)
    calendar_removed = _clear_dir_contents(CALENDAR_DIR)
    ensure_dirs()  # recreate the now-empty skeleton so the next run starts clean
    return (
        f"wipe-all: removed {db_removed} DB/CSV file(s), {records_removed} record "
        f"day-folder(s), {calendar_removed} calendar item(s). "
        f"Test zone ({RECORDS_TEST_DIR.name}) left untouched."
    )


ACTIONS = {
    "requeue-details": (requeue_details, False),
    "reset-notify": (reset_notify, False),
    "wipe-db": (wipe_db, True),
    "wipe-all": (wipe_all, True),
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("action", choices=sorted(ACTIONS))
    parser.add_argument(
        "--yes", action="store_true",
        help="Required confirmation for the destructive wipe-db / wipe-all actions.",
    )
    args = parser.parse_args(argv)

    func, destructive = ACTIONS[args.action]
    if destructive and not args.yes:
        _log(f"{args.action}: refused (missing --yes confirmation).")
        print(f"Refusing to run destructive '{args.action}' without --yes.", file=sys.stderr)
        return 2

    summary = func()
    _log(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
