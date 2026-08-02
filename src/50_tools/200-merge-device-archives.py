#!/usr/bin/env python3
"""Merge independent device archives into one canonical archive.

Each ``--source`` is ``DEVICE_ID|DB_PATH|RECORDS_DIR``. Sources are opened
read-only; callers should stop source collectors and use stable snapshots
before starting a merge.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


IMPORTER = Path(__file__).with_name("190-import-remote-archive.py")


def parse_source(value: str) -> tuple[str, str, str]:
    parts = value.split("|", 2)
    if len(parts) != 3 or not all(part.strip() for part in parts):
        raise argparse.ArgumentTypeError("source must be DEVICE_ID|DB_PATH|RECORDS_DIR")
    return tuple(part.strip() for part in parts)  # type: ignore[return-value]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-db", required=True)
    parser.add_argument("--target-records-dir", required=True)
    parser.add_argument("--source", action="append", type=parse_source, required=True,
                        metavar="DEVICE_ID|DB_PATH|RECORDS_DIR")
    parser.add_argument("--group", default="Closed,Cancelled")
    parser.add_argument("--target-journal-mode", choices=("delete", "wal"), default="delete")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    status = 0
    for device_id, db_path, records_dir in args.source:
        print(f"=== merging source {device_id} ===", flush=True)
        command = [
            sys.executable, str(IMPORTER),
            "--db", db_path,
            "--records-dir", records_dir,
            "--target-db", args.target_db,
            "--target-records-dir", args.target_records_dir,
            "--group", args.group,
            "--target-journal-mode", args.target_journal_mode,
        ]
        if args.dry_run:
            command.append("--dry-run")
        completed = subprocess.run(command, check=False)
        if completed.returncode != 0:
            print(f"ERROR: source {device_id} merge failed", file=sys.stderr)
            status = completed.returncode
            break
    return status


if __name__ == "__main__":
    raise SystemExit(main())
