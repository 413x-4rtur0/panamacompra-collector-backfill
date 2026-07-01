#!/usr/bin/env python3
"""Export/open calendar ICS files for selected PanamaCompra record NUMEROs."""
import argparse
import os
import subprocess
import sys
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SRC_DIR))
sys.path.insert(0, str(_SRC_DIR / "pipeline"))
import common as pc_common
import notify_new_records as notifier


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Export/open calendar ICS files for selected records")
    parser.add_argument("numeros", nargs="+", help="Record NUMERO values to export/import")
    parser.add_argument("--open", action="store_true", help="Open each written ICS with the desktop calendar app")
    args = parser.parse_args(argv)

    conn = pc_common.init_db()
    opener = os.environ.get("PC_OPEN_CALENDAR_COMMAND", os.environ.get("PC_CALENDAR_AUTO_IMPORT_CMD", "xdg-open"))
    written = []
    for numero in args.numeros:
        row = notifier.fetch_row(conn, numero)
        if row is None:
            print(f"Missing record: {numero}", file=sys.stderr)
            continue
        path = notifier.export_record_calendar(conn, row)
        if not path:
            print(f"No calendar exported for: {numero}", file=sys.stderr)
            continue
        written.append(path)
        print(f"Calendar exported for {numero}: {path}")
        if args.open:
            subprocess.Popen([opener, path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"Selected calendar export complete: {len(written)} file(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
