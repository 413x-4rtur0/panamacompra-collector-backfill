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
# "fecha" is the raw index-scrape field, stored in the portal's native
# DD-MM-YYYY format (e.g. "07-07-2026 10:13 AM") -- unlike every other date
# column here, which is already normalized to YYYY-MM-DD. The "start" field
# falls back to it for brand-new records whose detail page (which computes
# start_date_guess) has not been downloaded yet. Without reordering it to
# YYYY-MM-DD first, its first 10 characters ("07-07-2026") sort/compare
# nothing like the YYYY-MM-DD range bounds used below, so those records were
# silently invisible from every day/week/month/year view. The AM/PM time
# suffix is left as-is (a cosmetic-only quirk for that narrow fallback case;
# it does not affect which day a record is grouped under).
_FECHA_TO_ISO = (
    "CASE WHEN substr(fecha,3,1)='-' AND substr(fecha,6,1)='-' AND length(fecha)>=10 "
    "THEN substr(fecha,7,4) || '-' || substr(fecha,4,2) || '-' || substr(fecha,1,2) || substr(fecha,11) "
    "ELSE fecha END"
)
FIELDS = {
    "end": ("COALESCE(finish_date_guess, '')", "deadline (end date)"),
    "start": (f"COALESCE(NULLIF(start_date_guess, ''), {_FECHA_TO_ISO}, '')", "start date"),
    "downloaded": ("COALESCE(detail_saved_at, '')", "local download date"),
    # The portal's index FECHA is the publication/listing date. Closed
    # backfill records are historical, so their local detail_saved_at is often
    # much later than the date they were published on PanamaCompra.
    "listed": (f"COALESCE({_FECHA_TO_ISO}, '')", "published / index date"),
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


def fetch_events(conn, field: str, start: date, end: date, *, filter_fn=None) -> dict[str, list]:
    """Events keyed by 'YYYY-MM-DD' within the inclusive range.

    ``filter_fn(row) -> bool``, when given, keeps only matching rows — used
    by the web monitor's client-scoped calendar (GET /api/client-calendar-grid)
    to show each client only opportunities matching their own filters. The
    extra columns beyond day_block()/event_line()'s needs (entidad,
    dependencia, modalidad, detail_json_path) exist so filter_fn can build the
    same keyword-match haystack notify_whatsapp.row_filter_haystack() uses.
    """
    expr = FIELDS[field][0]
    rows = conn.execute(
        f"SELECT numero, descripcion, short_description, estado, grupo, entidad, dependencia, "
        f"modalidad, detail_json_path, link, {expr} AS event_date "
        f"FROM opportunities WHERE REPLACE(REPLACE(SUBSTR({expr}, 1, 10), '_', '-'), 'T', '') "
        f"BETWEEN ? AND ? ORDER BY {expr}, numero",
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    if filter_fn is not None:
        rows = [row for row in rows if filter_fn(row)]
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


def day_block(day_key: str, events: list, *, hourly: bool = False) -> list[str]:
    weekday = WEEKDAYS[date.fromisoformat(day_key).weekday()]
    lines = [f"{weekday} {day_key} — {len(events)} opportunity(ies):"]
    if not hourly:
        lines.extend(event_line(row) for row in events)
        return lines
    buckets: dict[str, list] = {}
    for row in events:
        value = normalize_value(row["event_date"])
        hour = value[11:13] if len(value) >= 16 else ""
        buckets.setdefault(hour, []).append(row)
    for hour in sorted(h for h in buckets if h):
        lines.append(f"  -- {hour}:00 --")
        lines.extend(event_line(row) for row in buckets[hour])
    if "" in buckets:
        lines.append("  -- no time --")
        lines.extend(event_line(row) for row in buckets[""])
    return lines


def render_view(conn, view: str, anchor: date, field: str, *, hourly: bool = False, filter_fn=None) -> str:
    start, end = view_range(view, anchor)
    grouped = fetch_events(conn, field, start, end, filter_fn=filter_fn)
    total = sum(len(v) for v in grouped.values())
    label = FIELDS[field][1]
    lines: list[str] = []

    if view == "day":
        lines.append(f"Opportunities by {label} — {anchor.isoformat()}")
        lines.append("")
        events = grouped.get(anchor.isoformat(), [])
        lines.extend(day_block(anchor.isoformat(), events, hourly=hourly) if events else ["(no opportunities on this day)"])

    elif view == "week":
        lines.append(f"Opportunities by {label} — week of {start.isoformat()} to {end.isoformat()} ({total} total)")
        for offset in range(7):
            day_key = (start + timedelta(days=offset)).isoformat()
            lines.append("")
            events = grouped.get(day_key, [])
            if events:
                lines.extend(day_block(day_key, events, hourly=hourly))
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
    parser.add_argument("--field", default="end", choices=sorted(FIELDS), help="date driving the view: end (deadline, default), start, downloaded, listed (published/index date)")
    args = parser.parse_args(argv)

    conn = pc_common.init_db()
    print(render_view(conn, args.view, parse_anchor(args.date), args.field, hourly=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
