#!/usr/bin/env python3
"""Rename record folders to the scheme [finish]-{numero}-{desc}.

Old leaf:  records/YY-MM-DD/<NUMERO>/
New leaf:  records/YY-MM-DD/[2022-10-11_12:00]-{<NUMERO>}-{FRS-126--CMPRS-D-CJ-PLSTC}/

The finish stamp is when proposals stop being accepted (end of the
"presentación de cotizaciones" window, or the delivery date at 12:00 for older
records). The description token is the request description with accents and
vowels removed, shortened. Both are computed from already-saved data
(tables/*.json and <numero>.detail.txt) - this tool does not hit the network.

Dry-run by default; pass --apply to actually rename. The DB path columns are
updated, the detail JSON "files" block is repointed, and inner files keep their
<numero>.* names. Existing targets are never overwritten.
"""
import argparse
import json
import re
from pathlib import Path

from pc_common import (
    RECORDS_DIR,
    build_record_folder_leaf,
    compute_finish_stamp,
    desc_slug,
    find_kv,
    init_db,
    key_values_from_rows,
    rename_record_folder,
    safe_name,
)


def find_numero(folder):
    """Read the NUMERO from the saved JSON inside a record folder."""
    candidates = list(folder.glob("*.detail.json")) + [
        p for p in folder.glob("*.json") if not p.name.endswith(".detail.json")
    ]
    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if data.get("numero"):
            return str(data["numero"])
    return ""


def gather_kv_and_text(folder, numero):
    """Collect key/values from saved tables and the saved detail text."""
    n = safe_name(numero)
    text = ""
    txt_path = folder / f"{n}.detail.txt"
    if txt_path.exists():
        text = txt_path.read_text(encoding="utf-8", errors="ignore")

    agg_kv = {}
    tables_dir = folder / "tables"
    if tables_dir.exists():
        for table_path in sorted(tables_dir.glob("*.json")):
            try:
                table = json.loads(table_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            kv = table.get("key_values") or key_values_from_rows(table.get("raw_rows"))
            if kv:
                agg_kv.update(kv)
    return agg_kv, text


def description_for(agg_kv, text, db_desc):
    desc = find_kv(agg_kv, "descripcion")
    if not desc:
        m = re.search(r"Descripci[oó]n[^:\n]*:\s*(.+)", text)
        if m:
            desc = m.group(1).strip()
    return desc or db_desc or ""


def iter_record_folders(records_dir):
    if not records_dir.exists():
        return
    for date_dir in sorted(records_dir.iterdir()):
        if not date_dir.is_dir():
            continue
        for folder in sorted(date_dir.iterdir()):
            if folder.is_dir():
                yield folder


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="Actually rename (default: dry-run preview).")
    parser.add_argument("--limit", type=int, default=0, help="Process at most N folders (0 = all).")
    parser.add_argument("--records-dir", default=str(RECORDS_DIR), help="Records directory to scan.")
    args = parser.parse_args()

    records_dir = Path(args.records_dir)
    conn = init_db()

    counts = {"renamed": 0, "already": 0, "skipped": 0, "conflict": 0, "no_numero": 0}
    processed = 0

    print(f"{'APPLY' if args.apply else 'DRY-RUN'} | scanning {records_dir}")
    print("-" * 80)

    for folder in iter_record_folders(records_dir):
        if args.limit and processed >= args.limit:
            break
        processed += 1

        numero = find_numero(folder)
        if not numero:
            counts["no_numero"] += 1
            print(f"NO-NUMERO  {folder}")
            continue

        db_row = conn.execute(
            "SELECT descripcion FROM opportunities WHERE numero = ?", (numero,)
        ).fetchone()
        db_desc = db_row["descripcion"] if db_row else ""

        agg_kv, text = gather_kv_and_text(folder, numero)
        finish_stamp = compute_finish_stamp(agg_kv, text)
        slug = desc_slug(description_for(agg_kv, text, db_desc))
        new_leaf = build_record_folder_leaf(finish_stamp, numero, slug)

        if folder.name == new_leaf:
            counts["already"] += 1
            continue
        if not finish_stamp and not slug:
            counts["skipped"] += 1
            print(f"SKIP       {folder.name}  (no finish date or description found)")
            continue

        target = folder.parent / new_leaf
        if target.exists():
            counts["conflict"] += 1
            print(f"CONFLICT   {folder.name}\n        -> {new_leaf} (target already exists)")
            continue

        print(f"RENAME     {folder.name}\n        -> {new_leaf}")
        if args.apply:
            status, _ = rename_record_folder(conn, numero, folder, new_leaf)
            if status == "renamed":
                counts["renamed"] += 1
            elif status == "conflict":
                counts["conflict"] += 1
            else:
                counts["skipped"] += 1

    print("-" * 80)
    print(f"Scanned: {processed}")
    if args.apply:
        print(f"Renamed: {counts['renamed']}")
    else:
        planned = processed - counts["already"] - counts["skipped"] - counts["conflict"] - counts["no_numero"]
        print(f"Would rename: {planned}  (run again with --apply)")
    print(
        f"Already named: {counts['already']} | skipped: {counts['skipped']} | "
        f"conflicts: {counts['conflict']} | no numero: {counts['no_numero']}"
    )


if __name__ == "__main__":
    main()
