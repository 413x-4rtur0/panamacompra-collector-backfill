#!/usr/bin/env python3
"""Build one combined .ics from every record's calendar event (for Thunderbird).

Scans the records tree for ``*.detail.json``, collects each record's ``calendar``
object, and writes a single VCALENDAR containing all events to
``data/calendar/panamacompra.ics`` (override with --out).

Subscribe to that file ONCE in Thunderbird as a local calendar (New Calendar →
On My Computer is for manual events; to auto-show these, use New Calendar →
On the Network → iCalendar (ICS) with a ``file://`` URL to the path printed
below, or "On My Computer" + periodic import). Re-running this refreshes the file
with any new events, so the run-all worker calls it automatically after the
detail step.
"""
import argparse
import json
from pathlib import Path

from pc_common import COMBINED_CALENDAR_PATH, RECORDS_DIR, calendars_to_ics


def iter_calendars(records_dir):
    """Collect every record's calendar object, sorted by start time."""
    calendars = []
    for path in sorted(Path(records_dir).rglob("*.detail.json")):
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        calendar = data.get("calendar")
        if calendar and (calendar.get("dtstart") or calendar.get("summary")):
            calendars.append(calendar)
    calendars.sort(key=lambda c: (c.get("dtstart") or "", c.get("uid") or ""))
    return calendars


def main():
    parser = argparse.ArgumentParser(
        description="Build a combined Thunderbird .ics from all record calendars.",
    )
    parser.add_argument("--records-dir", default=str(RECORDS_DIR), help="records tree to scan")
    parser.add_argument("--out", default=str(COMBINED_CALENDAR_PATH), help="combined .ics output path")
    args = parser.parse_args()

    calendars = iter_calendars(args.records_dir)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(calendars_to_ics(calendars).encode("utf-8"))

    count = len(calendars)
    print(f"Combined calendar written: {out}  ({count} event{'s' if count != 1 else ''})")
    print("Subscribe to it once in Thunderbird; re-running refreshes it with new events.")


if __name__ == "__main__":
    main()
