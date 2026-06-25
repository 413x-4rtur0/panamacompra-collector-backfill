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
import re
from datetime import datetime, timedelta
from pathlib import Path

from pc_common import (
    RECORDS_DIR,
    VIEWS_SCHEMA_VERSION,
    build_detail_views,
    safe_name,
    save_table_jsons,
    save_detail_section_jsons,
    write_calendar_ics,
)

DETAIL_SUFFIX = ".detail.json"

def load_tables(detail_json_path):
    """Reassemble full table dicts from disk (for item parsing and migration).

    Merges the split layout (``.table.<ident>.NNN.json`` + ``.raw`` + ``.raw_wL``)
    by table_index, and also reads the legacy single ``.table_NNN.json`` files, so
    items keep their códigos and a migration can rewrite the split files.
    """
    tables_dir = detail_json_path.parent / "tables"
    if not tables_dir.is_dir():
        return []
    merged = {}
    for path in sorted(tables_dir.glob("*.json")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(doc.get("tables"), list):
            for table_doc in doc["tables"]:
                idx = table_doc.get("table_index")
                if idx is None:
                    continue
                table = merged.setdefault(int(idx), {"table_index": int(idx)})
                for key in ("section", "identifier", "headers", "rows", "key_values",
                            "links", "links_count", "raw_rows", "rows_with_links",
                            "raw_rows_with_links"):
                    if key in table_doc and not table.get(key):
                        table[key] = table_doc[key]
            continue
        idx = doc.get("table_index")
        if idx is None:
            if "-TABLE-000-ALL" in path.name:
                continue
            m = re.search(r"(\d+)\.json$", path.name)
            idx = int(m.group(1)) if m else len(merged) + 1
        table = merged.setdefault(int(idx), {"table_index": int(idx)})
        for key in ("section", "identifier", "headers", "rows", "key_values",
                    "links", "links_count", "raw_rows", "rows_with_links",
                    "raw_rows_with_links"):
            if key in doc and not table.get(key):
                table[key] = doc[key]
    return [merged[k] for k in sorted(merged)]

def parse_since(value: str | None):
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


def iter_detail_jsons(records_dir, *, since=None):
    threshold = (since - timedelta(seconds=1)).timestamp() if since else None
    for path in sorted(Path(records_dir).rglob(f"*{DETAIL_SUFFIX}")):
        if not path.is_file():
            continue
        if threshold is not None and path.stat().st_mtime < threshold:
            continue
        yield path

def rebuild_one(detail_json_path):
    """Return (data, items_count, tables) with rebuilt views, or (None, 0, []) on error."""
    try:
        data = json.loads(detail_json_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None, 0, []

    base = detail_json_path.name[: -len(DETAIL_SUFFIX)]
    txt_path = detail_json_path.parent / f"{base}.detail.txt"
    text = txt_path.read_text(encoding="utf-8", errors="ignore") if txt_path.exists() else ""

    numero = data.get("numero") or base
    tables = load_tables(detail_json_path)
    summary, items, calendar, fields = build_detail_views(
        text, tables, numero, dtstamp=data.get("saved_at"), link=data.get("link"),
    )

    data.update({
        "summary": summary,
        "items_count": len(items),
        "items": items,
        "calendar": calendar,
        "fields_detected": fields,
        "views_schema_version": VIEWS_SCHEMA_VERSION,
    })
    return data, len(items), tables

def main():
    parser = argparse.ArgumentParser(description="Backfill detail.json summary/items/calendar views.")
    parser.add_argument("--records-dir", default=str(RECORDS_DIR), help="records tree to scan")
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry-run preview)")
    parser.add_argument("--since", default="", help="only process detail JSON files modified since this timestamp")
    args = parser.parse_args()

    since = parse_since(args.since)
    mode = "APPLY" if args.apply else "DRY-RUN"
    since_note = f" | since {args.since}" if since else ""
    print(f"{mode} | scanning {args.records_dir}{since_note}")
    print("-" * 80)

    counts = {"scanned": 0, "updated": 0, "no_text": 0, "errors": 0}
    for detail_json_path in iter_detail_jsons(args.records_dir, since=since):
        counts["scanned"] += 1
        data, n_items, tables = rebuild_one(detail_json_path)
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
            numero = data.get("numero") or detail_json_path.name[: -len(DETAIL_SUFFIX)]
            # Migrate table files to the split layout and record the tables index.
            _, descriptors = save_table_jsons(detail_json_path.parent, numero, tables, overwrite=True)
            section_written, section_descriptors = save_detail_section_jsons(
                detail_json_path.parent,
                numero,
                {
                    "summary": data.get("summary", {}),
                    "items": data.get("items", []),
                    "calendar": data.get("calendar", {}),
                    "fields_detected": data.get("fields_detected", {}),
                },
                overwrite=True,
            )
            data["tables"] = descriptors
            data["tables_count"] = len(descriptors)
            data["detail_sections"] = section_descriptors
            data["detail_sections_count"] = len(section_descriptors)
            data["detail_sections_written_now"] = section_written
            detail_json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            write_calendar_ics(detail_json_path.parent / f"{safe_name(numero)}.calendar.ics", data.get("calendar"))

    print("-" * 80)
    print(f"Scanned: {counts['scanned']}")
    print(f"Updated: {counts['updated']}" + ("" if args.apply else " (dry-run, nothing written)"))
    print(f"No text: {counts['no_text']} | errors: {counts['errors']}")
    if not args.apply and counts["updated"]:
        print("\nRe-run with --apply to write these views into detail.json.")

if __name__ == "__main__":
    main()
