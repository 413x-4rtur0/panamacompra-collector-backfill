#!/usr/bin/env python3
"""Merge a secondary machine's already-downloaded Closed/Cancelled archive
into this server's own DB and records/ tree -- built for the HP-15-BW036NR-R
2025 backfill install, but general: any dedicated backfill machine's SQLite
copy + records/ tree can be merged in the same way.

Safe to re-run repeatedly while the remote backfill is still in progress, or
once at the end: every insert is INSERT OR IGNORE keyed on the same
uniqueness this database already relies on --
  opportunities:              numero (PRIMARY KEY)
  opportunity_status_history: an explicit dedup check (no UNIQUE constraint
                               exists on this table), since (numero,
                               observed_at, change_code) is a fine natural
                               key for a change actually being the same event
  cotizacion_bids:             the existing UNIQUE(numero, item_index,
                               proponente)
so a partial or repeated import never duplicates data, and only files that
don't already exist locally get copied.

The remote SQLite file and its records/ tree must already be reachable on
this machine's filesystem (e.g. via rsync/SMB/a mounted share) -- this tool
only merges already-downloaded data; it never crawls or opens a browser.
Column lists are read from each database's own PRAGMA table_info rather than
using SELECT *, since the remote DB and this one may have accumulated the
same migrations in different historical order.

Usage:
  190-import-remote-archive.py --db /path/to/remote_copy.db \\
      --records-dir /path/to/remote/records [--group Closed,Cancelled] [--dry-run]
"""
import argparse
import os
import shutil
import sqlite3
import sys
from urllib.parse import quote
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import *


def table_columns(conn, table: str) -> list[str]:
    return [row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def staged_and_local_dirs(
    row_dict: dict, staged_records_dir: Path, local_records_dir: Path
) -> tuple[Path, Path] | tuple[None, None]:
    """A remote row's record_folder is an absolute path rooted at the REMOTE
    machine's own records dir -- meaningless as a filesystem path here, since
    the rsync'd copy lives at a different staging path on this machine.
    date_folder plus the leaf folder name from record_folder are enough to
    reconstruct the same relative tree under any records root, staged or
    local, without needing the remote machine's own path prefix at all."""
    date_folder = row_dict.get("date_folder")
    record_folder = row_dict.get("record_folder")
    if not date_folder or not record_folder:
        return None, None
    leaf = Path(record_folder).name
    return staged_records_dir / date_folder / leaf, local_records_dir / date_folder / leaf


def remap_path(value: str, record_folder: str, local_dest: Path) -> str:
    """value (index_json_path/detail_json_path/cotizacion_json_path) is an
    absolute path nested under this same row's record_folder on the remote
    machine -- e.g. NUMERO.json or tables/x.json underneath it. Replace the
    record_folder prefix with the new local destination so that relative
    filename/subpath survives the move to this machine."""
    if not value or not record_folder:
        return value
    try:
        relative = Path(value).relative_to(record_folder)
    except ValueError:
        return value  # not nested under record_folder (shouldn't happen)
    return str(local_dest / relative)


def copy_record_folder(staged_source: Path | None, local_dest: Path | None, dry_run: bool) -> bool:
    """Copy one record's whole folder tree (JSON/HTML/text/tables) from the
    staged rsync copy into this machine's records/ tree if not already
    present locally. Returns True if a copy happened (False if it already
    existed or there was nothing to copy), matching this project's
    immutable-archive rule: never overwrite an existing folder."""
    if staged_source is None or local_dest is None or not staged_source.is_dir():
        return False
    if local_dest.exists():
        return False
    if dry_run:
        return True
    local_dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(staged_source, local_dest)
    return True


def import_opportunities(
    conn,
    remote_conn,
    groups: list[str],
    staged_records_dir: Path,
    local_records_dir: Path,
    dry_run: bool,
) -> dict:
    local_columns = set(table_columns(conn, "opportunities"))
    remote_columns = [c for c in table_columns(remote_conn, "opportunities") if c in local_columns]
    other_path_columns = {"index_json_path", "detail_json_path", "cotizacion_json_path"}

    placeholders = ", ".join(f":{c}" for c in remote_columns)
    column_list = ", ".join(remote_columns)

    group_placeholders = ", ".join("?" for _ in groups)
    rows = remote_conn.execute(
        f"SELECT {column_list} FROM opportunities WHERE grupo IN ({group_placeholders})",
        groups,
    ).fetchall()

    inserted = 0
    skipped_existing = 0
    folders_copied = 0
    for row in rows:
        row_dict = dict(row)
        exists = conn.execute(
            "SELECT 1 FROM opportunities WHERE numero = ?", (row_dict["numero"],)
        ).fetchone()
        if exists:
            skipped_existing += 1
            continue

        original_record_folder = row_dict.get("record_folder")
        staged_source, local_dest = staged_and_local_dirs(
            row_dict, staged_records_dir, local_records_dir
        )

        for col in other_path_columns & set(row_dict):
            row_dict[col] = remap_path(row_dict[col], original_record_folder, local_dest)
        if local_dest is not None:
            row_dict["record_folder"] = str(local_dest)

        if copy_record_folder(staged_source, local_dest, dry_run):
            folders_copied += 1

        if not dry_run:
            conn.execute(
                f"INSERT OR IGNORE INTO opportunities ({column_list}) VALUES ({placeholders})",
                row_dict,
            )
        inserted += 1

    return {"inserted": inserted, "skipped_existing": skipped_existing, "folders_copied": folders_copied}


def import_status_history(conn, remote_conn, groups: list[str], dry_run: bool) -> dict:
    local_columns = [c for c in table_columns(conn, "opportunity_status_history") if c != "id"]
    remote_columns = [c for c in table_columns(remote_conn, "opportunity_status_history") if c in local_columns]
    column_list = ", ".join(remote_columns)
    placeholders = ", ".join(f":{c}" for c in remote_columns)

    group_placeholders = ", ".join("?" for _ in groups)
    rows = remote_conn.execute(
        f"""SELECT {column_list} FROM opportunity_status_history
            WHERE numero IN (SELECT numero FROM opportunities WHERE grupo IN ({group_placeholders}))""",
        groups,
    ).fetchall()

    inserted = 0
    skipped_existing = 0
    for row in rows:
        row_dict = dict(row)
        exists = conn.execute(
            """SELECT 1 FROM opportunity_status_history
               WHERE numero = ? AND observed_at = ? AND change_code = ?""",
            (row_dict["numero"], row_dict["observed_at"], row_dict["change_code"]),
        ).fetchone()
        if exists:
            skipped_existing += 1
            continue
        if not dry_run:
            conn.execute(f"INSERT INTO opportunity_status_history ({column_list}) VALUES ({placeholders})", row_dict)
        inserted += 1

    return {"inserted": inserted, "skipped_existing": skipped_existing}


def import_cotizacion_bids(conn, remote_conn, groups: list[str], dry_run: bool) -> dict:
    local_columns = [c for c in table_columns(conn, "cotizacion_bids") if c != "id"]
    remote_columns = [c for c in table_columns(remote_conn, "cotizacion_bids") if c in local_columns]
    column_list = ", ".join(remote_columns)
    placeholders = ", ".join(f":{c}" for c in remote_columns)

    group_placeholders = ", ".join("?" for _ in groups)
    rows = remote_conn.execute(
        f"""SELECT {column_list} FROM cotizacion_bids
            WHERE numero IN (SELECT numero FROM opportunities WHERE grupo IN ({group_placeholders}))""",
        groups,
    ).fetchall()

    before = conn.execute("SELECT COUNT(*) AS c FROM cotizacion_bids").fetchone()["c"]
    if not dry_run:
        for row in rows:
            conn.execute(
                f"INSERT OR IGNORE INTO cotizacion_bids ({column_list}) VALUES ({placeholders})", dict(row)
            )
    after = before if dry_run else conn.execute("SELECT COUNT(*) AS c FROM cotizacion_bids").fetchone()["c"]

    return {"candidates": len(rows), "inserted": after - before if not dry_run else None}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", required=True, help="Path to the source machine's SQLite archive (a local copy/mount)")
    parser.add_argument("--records-dir", required=True, help="Path to the source machine's records/ tree (local copy/mount)")
    parser.add_argument(
        "--target-db",
        default=str(DB_PATH),
        help="Canonical target SQLite archive (default: current PC_ARCHIVE_DB_PATH)",
    )
    parser.add_argument(
        "--target-records-dir",
        default=str(RECORDS_DIR),
        help="Canonical target records/ tree (default: current PC_RECORDS_DIR)",
    )
    parser.add_argument(
        "--target-journal-mode",
        choices=("delete", "wal"),
        default="delete",
        help="SQLite journal mode for the canonical target (default: delete; safer over SSHFS)",
    )
    parser.add_argument("--group", default="Closed,Cancelled", help="Comma-separated grupo values to import (default: Closed,Cancelled)")
    parser.add_argument("--dry-run", action="store_true", help="Report what would happen without writing anything")
    args = parser.parse_args(argv)

    remote_db_path = Path(args.db).expanduser().resolve()
    remote_records_dir = Path(args.records_dir).expanduser().resolve()
    target_db_path = Path(args.target_db).expanduser().resolve()
    target_records_dir = Path(args.target_records_dir).expanduser().resolve()
    if not remote_db_path.is_file():
        print(f"ERROR: remote DB not found: {remote_db_path}")
        return 1
    if not remote_records_dir.is_dir():
        print(f"ERROR: remote records dir not found: {remote_records_dir}")
        return 1
    if remote_db_path == target_db_path or (
        target_db_path.exists()
        and os.path.samefile(remote_db_path, target_db_path)
    ):
        print("ERROR: source and target DB must be different files")
        return 1

    groups = [g.strip() for g in args.group.split(",") if g.strip()]
    target_db_path.parent.mkdir(parents=True, exist_ok=True)
    target_records_dir.mkdir(parents=True, exist_ok=True)

    conn = init_db(str(target_db_path))
    conn.execute(f"PRAGMA journal_mode={args.target_journal_mode.upper()}")
    conn.commit()
    remote_uri = f"file:{quote(str(remote_db_path))}?mode=ro"
    remote_conn = sqlite3.connect(remote_uri, uri=True)
    remote_conn.row_factory = sqlite3.Row

    opp_result = import_opportunities(
        conn,
        remote_conn,
        groups,
        remote_records_dir,
        target_records_dir,
        args.dry_run,
    )
    history_result = import_status_history(conn, remote_conn, groups, args.dry_run)
    bids_result = import_cotizacion_bids(conn, remote_conn, groups, args.dry_run)

    if not args.dry_run:
        conn.commit()

    label = "DRY RUN — " if args.dry_run else ""
    print(f"{label}opportunities: inserted={opp_result['inserted']} "
          f"already_present={opp_result['skipped_existing']} folders_copied={opp_result['folders_copied']}")
    print(f"{label}opportunity_status_history: inserted={history_result['inserted']} "
          f"already_present={history_result['skipped_existing']}")
    print(f"{label}cotizacion_bids: candidates={bids_result['candidates']} inserted={bids_result['inserted']}")
    print(f"{label}target_db: {target_db_path}")
    print(f"{label}target_records_dir: {target_records_dir}")

    remote_conn.close()
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
