#!/usr/bin/env python3
"""Backfill the summary / numbered items / calendar views into detail.json.

For every ``*.detail.json`` under the records tree this reads the sibling
``*.detail.txt`` (and any saved ``tables/*.json`` for item códigos), rebuilds
the structured ``summary``, numbered ``items`` and ``calendar`` (an ICS VEVENT
expressed as JSON), and writes them back into the detail JSON.

Browser-free and safe to re-run. The normal detail downloader produces the same
views for new records; this tool is for archives already on disk.

    ./pc_build_detail_views.py                 # dry-run: preview what would change
    ./pc_build_detail_views.py --apply         # write the views into detail.json
    ./pc_build_detail_views.py --records-dir /path --apply
"""
import argparse
import json
from pathlib import Path

from pc_common import RECORDS_DIR, VIEWS_SCHEMA_VERSION, build_detail_views, safe_name, write_calendar_ics

DETAIL_SUFFIX = ".detail.json"

def load_tables(detail_json_path):
    """Saved table JSONs for a record (used to enrich items with códigos)."""
    tables = []
    tables_dir = detail_json_path.parent / "tables"
    if not tables_dir.is_dir():
        return tables
    for path in sorted(tables_dir.glob("*.json")):
        try:
            tables.append(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            continue
    return tables

def iter_detail_jsons(records_dir):
    for path in sorted(Path(records_dir).rglob(f"*{DETAIL_SUFFIX}")):
        if path.is_file():
            yield path

def rebuild_one(detail_json_path):
    """Return (data, items_count) with rebuilt views, or (None, 0) on read error."""
    try:
        data = json.loads(detail_json_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None, 0

    base = detail_json_path.name[: -len(DETAIL_SUFFIX)]
    txt_path = detail_json_path.parent / f"{base}.detail.txt"
    text = txt_path.read_text(encoding="utf-8", errors="ignore") if txt_path.exists() else ""

    numero = data.get("numero") or base
    summary, items, calendar, fields = build_detail_views(
        text, load_tables(detail_json_path), numero, dtstamp=data.get("saved_at")
    )

    data.update({
        "summary": summary,
        "items_count": len(items),
        "items": items,
        "calendar": calendar,
        "fields_detected": fields,
        "views_schema_version": VIEWS_SCHEMA_VERSION,
    })
    return data, len(items)

def main():
    parser = argparse.ArgumentParser(description="Backfill detail.json summary/items/calendar views.")
    parser.add_argument("--records-dir", default=str(RECORDS_DIR), help="records tree to scan")
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry-run preview)")
    args = parser.parse_args()

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"{mode} | scanning {args.records_dir}")
    print("-" * 80)

    counts = {"scanned": 0, "updated": 0, "no_text": 0, "errors": 0}
    for detail_json_path in iter_detail_jsons(args.records_dir):
        counts["scanned"] += 1
        data, n_items = rebuild_one(detail_json_path)
        if data is None:
            counts["errors"] += 1
            print(f"ERROR      {detail_json_path}")
            continue

        if not (data["summary"].get("numero") or n_items):
            counts["no_text"] += 1
            print(f"SKIP       {detail_json_path.name} (no parseable detail text)")
            continue

        counts["updated"] += 1
        print(f"VIEWS      {detail_json_path.name}  items={n_items}  finish={data['calendar'].get('dtend') or '-'}")
        if args.apply:
            detail_json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            numero = safe_name(data.get("numero") or detail_json_path.name[: -len(DETAIL_SUFFIX)])
            write_calendar_ics(detail_json_path.parent / f"{numero}.calendar.ics", data.get("calendar"))

    print("-" * 80)
    print(f"Scanned: {counts['scanned']}")
    print(f"Updated: {counts['updated']}" + ("" if args.apply else " (dry-run, nothing written)"))
    print(f"No text: {counts['no_text']} | errors: {counts['errors']}")
    if not args.apply and counts["updated"]:
        print("\nRe-run with --apply to write these views into detail.json.")

if __name__ == "__main__":
    main()
