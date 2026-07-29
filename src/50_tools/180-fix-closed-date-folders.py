#!/usr/bin/env python3
"""One-time repair: relocate Closed backfill records into the day folder
that matches the portal's own FECHA field, instead of the day they happened
to be imported.

Before date_folder_from_fecha() existed, 015-import-index-snapshot.py filed
every newly-discovered Closed backfill record under date_folder_name()
(today), so a January 2026 record imported on 2026-07-29 landed in
records/26-07-29/ instead of records/26-01-.../. This tool finds every
grupo='Closed' row whose stored date_folder disagrees with its fecha, moves
the on-disk record folder (leaf name unchanged -- only the YY-MM-DD parent
changes) into the correct day folder, and updates every DB column that
embeds the old parent path.

    ./src/50_tools/180-fix-closed-date-folders.py            # dry run, lists mismatches
    ./src/50_tools/180-fix-closed-date-folders.py --apply    # move folders + update the DB

Wrap --apply with the same lock the backfill runners use so this never races
a live download:

    flock -n /tmp/panamacompra_closed_backfill_worker.lock \\
        ./src/50_tools/180-fix-closed-date-folders.py --apply
"""
import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import RECORDS_DIR, date_folder_from_fecha, init_db

# DB columns that may hold an absolute path rooted at record_folder.
PATH_COLUMNS = ("index_json_path", "detail_json_path", "cotizacion_json_path", "last_calendar_export_path")


def mismatched_rows(conn):
    rows = conn.execute("SELECT * FROM opportunities WHERE COALESCE(grupo, '') = 'Closed'").fetchall()
    out = []
    for row in rows:
        correct = date_folder_from_fecha(row["fecha"])
        if correct and correct != row["date_folder"]:
            out.append((row, correct))
    return out


def relocate(row, correct_date_folder):
    """Move the record's folder on disk. Returns the new record_folder Path,
    or a short reason string if it could not be moved."""
    old_folder = Path(row["record_folder"]) if row["record_folder"] else None
    if not old_folder or not old_folder.exists():
        return "missing-on-disk"

    new_folder = RECORDS_DIR / correct_date_folder / old_folder.name
    if new_folder.exists():
        return "target-exists"

    new_folder.parent.mkdir(parents=True, exist_ok=True)
    old_folder.rename(new_folder)
    return new_folder


def updated_path(value, old_folder, new_folder):
    if not value:
        return value
    if value.startswith(old_folder):
        return new_folder + value[len(old_folder):]
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="move folders and update the DB (default: dry run)")
    args = parser.parse_args()

    conn = init_db()
    mismatches = mismatched_rows(conn)
    print(f"{len(mismatches)} Closed record(s) filed under the wrong day folder.")
    if not mismatches:
        return

    by_move = Counter((row["date_folder"], correct) for row, correct in mismatches)
    print("-" * 60)
    for (old, new), count in sorted(by_move.items()):
        print(f"  {old}  ->  {new}   ({count} record{'s' if count != 1 else ''})")
    print("-" * 60)

    if not args.apply:
        print("Dry-run only. Re-run with --apply to move folders and update the DB.")
        return

    moved = missing = conflicts = 0
    for row, correct in mismatches:
        old_folder = str(row["record_folder"]) if row["record_folder"] else ""
        result = relocate(row, correct)
        if result == "missing-on-disk":
            missing += 1
            continue
        if result == "target-exists":
            conflicts += 1
            continue

        new_folder = str(result)
        set_clauses = ["date_folder = ?", "record_folder = ?"]
        values = [correct, new_folder]
        for column in PATH_COLUMNS:
            new_value = updated_path(row[column], old_folder, new_folder)
            set_clauses.append(f"{column} = ?")
            values.append(new_value)
        values.append(row["numero"])

        conn.execute(f"UPDATE opportunities SET {', '.join(set_clauses)} WHERE numero = ?", values)
        conn.commit()
        moved += 1
        print(f"  moved {row['numero']}: {row['date_folder']} -> {correct}")

    print(f"\nMoved: {moved}  Missing on disk: {missing}  Target conflicts: {conflicts}")


if __name__ == "__main__":
    main()
