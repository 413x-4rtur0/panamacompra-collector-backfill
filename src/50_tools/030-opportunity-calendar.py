#!/usr/bin/env python3
"""Opportunity calendar: see collected opportunities by day, week, month or year.

    pcc calendar                       current month, grouped by deadline
    pcc calendar day  --date 2026-07-15
    pcc calendar week --date 2026-07-15
    pcc calendar month --date 2026-07 --field start
    pcc calendar year --date 2026 --field downloaded

``--field`` picks which date drives the view:
  end         finish_date_guess (deadline / DTEND, default)
  start       start_date_guess (falling back to the index 'fecha')
  downloaded  detail_saved_at (when the record was saved locally)

The same renderer backs the CLI, the native monitor's Calendar panel and the
web monitor's Calendar card (both import this file as a module), so the three
views always agree.
"""
from __future__ import annotations

import argparse
import calendar as _calendar
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SRC_DIR))
import common as pc_common

VIEWS = ("day", "week", "month", "year")
FIELDS = {
    "end": ("COALESCE(finish_date_guess, '')", "deadline (end date)"),
    "start": ("COALESCE(NULLIF(start_date_guess, ''), fecha, '')", "start date"),
    "downloaded": ("COALESCE(detail_saved_at, '')", "local download date"),
}
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def parse_anchor(raw: str) -> date:
    """Accept YYYY, YYYY-MM or YYYY-MM-DD (plus time suffixes); default today."""
    text = (raw or "").strip().replace("_", " ").replace("T", " ")
    if not text:
        return date.today()
    for fmt, take in (("%Y-%m-%d", 10), ("%Y-%m", 7), ("%Y", 4)):
        try:
            return datetime.strptime(text[:take], fmt).date()
        except ValueError:
            continue
    raise SystemExit(f"Unrecognized --date value: {raw!r} (use YYYY, YYYY-MM or YYYY-MM-DD)")


def shift_anchor(view: str, anchor: date, delta: int) -> date:
    """Anchor moved by ``delta`` units of the view (for prev/next buttons)."""
    if view == "day":
        return anchor + timedelta(days=delta)
    if view == "week":
        return anchor + timedelta(weeks=delta)
    if view == "year":
        return anchor.replace(year=anchor.year + delta, day=1)
    month_index = anchor.month - 1 + delta
    year = anchor.year + month_index // 12
    return date(year, month_index % 12 + 1, 1)


def view_range(view: str, anchor: date) -> tuple[date, date]:
    """Inclusive [start, end] date range covered by a view."""
    if view == "day":
        return anchor, anchor
    if view == "week":
        start = anchor - timedelta(days=anchor.weekday())
        return start, start + timedelta(days=6)
    if view == "month":
        last = _calendar.monthrange(anchor.year, anchor.month)[1]
        return date(anchor.year, anchor.month, 1), date(anchor.year, anchor.month, last)
    return date(anchor.year, 1, 1), date(anchor.year, 12, 31)


def normalize_value(value: str) -> str:
    return (value or "").strip().replace("_", " ").replace("T", " ")


def fetch_events(conn, field: str, start: date, end: date) -> dict[str, list]:
    """Events keyed by 'YYYY-MM-DD' within the inclusive range."""
    expr = FIELDS[field][0]
    rows = conn.execute(
        f"SELECT numero, descripcion, short_description, estado, grupo, {expr} AS event_date "
        f"FROM opportunities WHERE REPLACE(REPLACE(SUBSTR({expr}, 1, 10), '_', '-'), 'T', '') "
        f"BETWEEN ? AND ? ORDER BY {expr}, numero",
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    grouped: dict[str, list] = {}
    for row in rows:
        value = normalize_value(row["event_date"])
        grouped.setdefault(value[:10], []).append(row)
    return grouped


def event_line(row) -> str:
    value = normalize_value(row["event_date"])
    clock = value[11:16] if len(value) >= 16 else "--:--"
    desc = (row["descripcion"] or row["short_description"] or "").strip()
    if len(desc) > 64:
        desc = desc[:63] + "…"
    status = (row["estado"] or row["grupo"] or "").strip()
    return f"  {clock}  {row['numero']}  {desc}" + (f"  [{status}]" if status else "")


def day_block(day_key: str, events: list) -> list[str]:
    weekday = WEEKDAYS[date.fromisoformat(day_key).weekday()]
    lines = [f"{weekday} {day_key} — {len(events)} opportunity(ies):"]
    lines.extend(event_line(row) for row in events)
    return lines


def render_view(conn, view: str, anchor: date, field: str) -> str:
    start, end = view_range(view, anchor)
    grouped = fetch_events(conn, field, start, end)
    total = sum(len(v) for v in grouped.values())
    label = FIELDS[field][1]
    lines: list[str] = []

    if view == "day":
        lines.append(f"Opportunities by {label} — {anchor.isoformat()}")
        lines.append("")
        events = grouped.get(anchor.isoformat(), [])
        lines.extend(day_block(anchor.isoformat(), events) if events else ["(no opportunities on this day)"])

    elif view == "week":
        lines.append(f"Opportunities by {label} — week of {start.isoformat()} to {end.isoformat()} ({total} total)")
        for offset in range(7):
            day_key = (start + timedelta(days=offset)).isoformat()
            lines.append("")
            events = grouped.get(day_key, [])
            if events:
                lines.extend(day_block(day_key, events))
            else:
                lines.append(f"{WEEKDAYS[offset]} {day_key} — —")

    elif view == "month":
        lines.append(f"Opportunities by {label} — {_calendar.month_name[anchor.month]} {anchor.year} ({total} total)")
        lines.append("")
        lines.append(" ".join(f"{name:^7}" for name in WEEKDAYS))
        for week in _calendar.monthcalendar(anchor.year, anchor.month):
            cells = []
            for day in week:
                if day == 0:
                    cells.append(" " * 7)
                    continue
                count = len(grouped.get(date(anchor.year, anchor.month, day).isoformat(), []))
                cells.append(f"{day:>3}({count:>2})" if count else f"{day:>3}    ")
            lines.append(" ".join(cells))
        for day_key in sorted(grouped):
            lines.append("")
            lines.extend(day_block(day_key, grouped[day_key]))

    else:  # year
        lines.append(f"Opportunities by {label} — {anchor.year} ({total} total)")
        lines.append("")
        by_month = [0] * 13
        for day_key, events in grouped.items():
            by_month[int(day_key[5:7])] += len(events)
        peak = max(by_month) or 1
        for month in range(1, 13):
            bar = "█" * round(24 * by_month[month] / peak)
            lines.append(f"{anchor.year}-{month:02d}  {by_month[month]:>4}  {bar}")
        lines.append("")
        lines.append("Drill down with: pcc calendar month --date YYYY-MM")

    if view != "year" and total == 0 and view != "day":
        lines.append("")
        lines.append("(no opportunities in this range)")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Show collected opportunities by day, week, month or year.")
    parser.add_argument("view", nargs="?", default="month", choices=VIEWS, help="calendar granularity (default month)")
    parser.add_argument("--date", default="", help="anchor date: YYYY, YYYY-MM or YYYY-MM-DD (default today)")
    parser.add_argument("--field", default="end", choices=sorted(FIELDS), help="date driving the view: end (deadline, default), start, downloaded")
    args = parser.parse_args(argv)

    conn = pc_common.init_db()
    print(render_view(conn, args.view, parse_anchor(args.date), args.field))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
