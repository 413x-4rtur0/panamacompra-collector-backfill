#!/usr/bin/env python3
"""Build importable .ics calendar packages from record detail calendars.

By default this script exports only calendar events from detail files modified
since the current run started (``PC_RUN_STARTED_AT``), then splits them into
small timestamped packages such as
``data/calendar/26-06-20/26-06-20_14-35_panamacompra_calendar_001.ics``.

Use ``--all`` when you intentionally want to rebuild packages from every saved
record. Use ``--legacy-combined`` to additionally write the old single combined
``data/calendar/panamacompra.ics`` file.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from pc_common import CALENDAR_DIR, COMBINED_CALENDAR_PATH, RECORDS_DIR, calendars_to_ics, date_folder_name, write_run_progress

DEFAULT_PACKAGE_SIZE = 10


def parse_started_at(value: str | None) -> datetime | None:
    """Parse worker timestamps written as ISO or ``YYYY-MM-DD HH:MM:SS``."""
    if not value:
        return None
    value = value.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def iter_calendars(records_dir: str | Path, *, since: datetime | None = None):
    """Collect record calendar objects, sorted by start time.

    When ``since`` is supplied, only detail JSON files modified at or after that
    timestamp are included. A one-second tolerance avoids missing files on file
    systems with coarse modification times.
    """
    calendars = []
    threshold = (since - timedelta(seconds=1)).timestamp() if since else None
    for path in sorted(Path(records_dir).rglob("*.detail.json")):
        if not path.is_file():
            continue
        try:
            if threshold is not None and path.stat().st_mtime < threshold:
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        calendar = data.get("calendar")
        if calendar and (calendar.get("dtstart") or calendar.get("summary")):
            calendar = dict(calendar)
            calendar["_source_detail_json"] = str(path)
            calendars.append(calendar)
    calendars.sort(key=lambda c: (c.get("dtstart") or "", c.get("uid") or ""))
    return calendars


def clean_calendar(calendar: dict) -> dict:
    """Remove exporter-only keys before writing ICS."""
    return {key: value for key, value in calendar.items() if not key.startswith("_")}


def chunks(items: list[dict], size: int):
    for start in range(0, len(items), size):
        yield start // size + 1, items[start : start + size]


def write_packages(calendars: list[dict], out_dir: Path, package_size: int, prefix: str, *, date_subdir: str | None = None) -> list[Path]:
    if date_subdir:
        out_dir = out_dir / date_subdir
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for index, package in chunks(calendars, package_size):
        path = out_dir / f"{prefix}_{index:03d}.ics"
        path.write_bytes(calendars_to_ics(clean_calendar(c) for c in package).encode("utf-8"))
        written.append(path)
    return written


def default_auto_import_command() -> str:
    """Return a desktop opener command when simple calendar auto-import is enabled."""
    if os.environ.get("PC_CALENDAR_AUTO_IMPORT", "").strip().lower() not in {"1", "true", "yes", "on"}:
        return ""
    for candidate in (("xdg-open",), ("gio", "open"), ("open",)):
        if shutil.which(candidate[0]):
            return " ".join(shlex.quote(part) for part in candidate)
    return ""


def run_auto_import(paths: list[Path]) -> None:
    """Optionally hand written ICS packages to a user-configured import command."""
    if not paths:
        return
    command = os.environ.get("PC_CALENDAR_AUTO_IMPORT_CMD", "").strip() or default_auto_import_command()
    if not command:
        return
    for path in paths:
        print(f"Auto-import/open calendar package: {path}")
        subprocess.run(shlex.split(command) + [str(path)], check=False)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build timestamped PanamaCompra calendar import packages.")
    parser.add_argument("--records-dir", default=str(RECORDS_DIR), help="records tree to scan")
    parser.add_argument("--out-dir", default=str(CALENDAR_DIR), help="parent directory for timestamped calendar packages")
    parser.add_argument("--package-size", type=int, default=int(os.environ.get("PC_CALENDAR_PACKAGE_SIZE", DEFAULT_PACKAGE_SIZE)), help="VEVENTs per .ics package (default: 10)")
    parser.add_argument("--since", default=os.environ.get("PC_RUN_STARTED_AT", ""), help="only include detail JSON modified since this timestamp")
    parser.add_argument("--all", action="store_true", help="include every saved detail calendar instead of only new/changed ones")
    parser.add_argument("--legacy-combined", action="store_true", help=f"also write the old single combined file at {COMBINED_CALENDAR_PATH}")
    parser.add_argument("--flat", action="store_true", help="write packages directly in --out-dir instead of --out-dir/YY-MM-DD/")
    args = parser.parse_args()

    package_size = max(1, args.package_size)
    since = None if args.all else parse_started_at(args.since)
    stamp = datetime.now().strftime("%y-%m-%d_%H-%M")
    prefix = f"{stamp}_panamacompra_calendar"

    write_run_progress("CALENDAR", "RUNNING", 96, "Step 3/4: collecting new calendar events for ICS packages...", step_current=3, step_total=4, extra=f"package_size={package_size}")
    calendars = iter_calendars(args.records_dir, since=since)
    out_dir = Path(args.out_dir)
    date_subdir = None if args.flat else date_folder_name()
    written = write_packages(calendars, out_dir, package_size, prefix, date_subdir=date_subdir) if calendars else []

    if args.legacy_combined:
        out = Path(COMBINED_CALENDAR_PATH)
        out.parent.mkdir(parents=True, exist_ok=True)
        all_calendars = iter_calendars(args.records_dir, since=None)
        out.write_bytes(calendars_to_ics(clean_calendar(c) for c in all_calendars).encode("utf-8"))
        print(f"Legacy combined calendar written: {out} ({len(all_calendars)} events)")

    count = len(calendars)
    if written:
        print(f"Calendar packages written under {out_dir}: {len(written)} file(s), {count} event(s), package_size={package_size}")
        for path in written:
            print(f"  {path}")
        run_auto_import(written)
    else:
        print("No new calendar events found for this run; no import package written.")

    write_run_progress("CALENDAR", "DONE", 98, f"Step 3/4 complete. Wrote {len(written)} calendar package(s) with {count} new event(s).", step_current=3, step_total=4, item_current=count, item_total=count, extra=f"out_dir={out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
