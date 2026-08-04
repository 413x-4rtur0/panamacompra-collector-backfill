#!/usr/bin/env python3
"""Audit and order downloaded PanamaCompra record archives.

Dry-run by default.  Index-only records are placed under their publication
date.  Records with saved detail data are placed under their end date and get
the canonical (end-stamp)-(numero)-(description) leaf.  Existing targets are
never overwritten; --apply updates DB and JSON path metadata.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import RECORDS_DIR, date_folder_from_fecha, desc_slug, init_db, safe_name

DAY_DIR_RE = re.compile(r"^\d{2}-\d{2}-\d{2}$")
ISO_DATE_RE = re.compile(r"(20\d{2})[-/](\d{1,2})[-/](\d{1,2})")
DMY_DATE_RE = re.compile(r"(\d{1,2})[-/](\d{1,2})[-/](20\d{2})")
TIME_RE = re.compile(r"[T ](\d{1,2}):?(\d{2})")


def load_json(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def find_index(folder):
    for path in sorted(folder.glob("*.json")):
        if ".detail." in path.name or path.name.endswith(".error.json"):
            continue
        data = load_json(path)
        if data and data.get("numero"):
            return path, data, str(data["numero"])
    return None, None, ""


def find_detail(folder, numero):
    candidate = folder / f"{safe_name(numero)}.detail.json"
    if candidate.exists():
        return load_json(candidate) or {}
    for path in sorted(folder.glob("*.detail.json")):
        data = load_json(path)
        if data:
            return data
    return {}


def date_parts(value):
    text = str(value or "").strip()
    match = ISO_DATE_RE.search(text)
    if match:
        year, month, day = (int(x) for x in match.groups())
    else:
        match = DMY_DATE_RE.search(text)
        if not match:
            return None
        day, month, year = (int(x) for x in match.groups())
    try:
        return datetime(year, month, day)
    except ValueError:
        return None


def parent_from_date(value):
    parsed = date_parts(value)
    return parsed.strftime("%y-%m-%d") if parsed else ""


def finish_stamp(value):
    parsed = date_parts(value)
    if not parsed:
        return ""
    match = TIME_RE.search(str(value or ""))
    hour, minute = (int(match.group(1)), int(match.group(2))) if match else (12, 0)
    if hour > 23 or minute > 59:
        hour, minute = 12, 0
    return f"{parsed:%Y-%m-%d}_{hour:02d}-{minute:02d}"


def first_value(data, keys):
    for key in keys:
        value = data.get(key) if isinstance(data, dict) else None
        if value:
            return value
    return ""


def end_stamp(index_data, detail_data):
    candidates = [
        first_value(detail_data, ("finish_stamp", "date_name_finish_stamp",
                                  "finish_date_guess", "date_end_opportunity")),
        first_value(detail_data.get("summary") or {},
                    ("date_end_opportunity", "finish_date_guess")),
        first_value(detail_data.get("calendar") or {}, ("dtend", "end")),
        first_value(index_data, ("finish_date_guess", "date_end_opportunity",
                                "endate")),
    ]
    for value in candidates:
        normalized = finish_stamp(value)
        if normalized:
            return normalized
    return ""


def record_leaf(numero, description, end):
    stamp = safe_name(end or "NO-DATE").replace(":", "-")
    return f"({stamp})-({safe_name(numero)})-({safe_name(desc_slug(description) or 'NO-DESC')})"


def iter_record_folders(records_dir):
    if not records_dir.exists():
        return
    for child in sorted(records_dir.iterdir()):
        if not child.is_dir() or child.name == ".backfill-staging":
            continue
        if DAY_DIR_RE.fullmatch(child.name):
            for leaf in sorted(child.iterdir()):
                if leaf.is_dir() and leaf.name != ".backfill-staging":
                    yield leaf
        elif find_index(child)[0]:
            yield child


def rewrite_json_paths(folder, old_folder, new_folder, numero, date_folder):
    old_text, new_text = str(old_folder), str(new_folder)
    n = safe_name(numero)
    expected = {
        "record_folder": new_text,
        "record_folder_leaf": new_folder.name,
        "date_folder": date_folder,
        "index_json_path": str(new_folder / f"{n}.json"),
        "detail_json_path": str(new_folder / f"{n}.detail.json"),
    }
    changed = 0
    for path in sorted(folder.rglob("*.json")):
        data = load_json(path)
        if not data:
            continue
        touched = False

        def replace(value):
            nonlocal touched
            if isinstance(value, dict):
                result = {}
                for key, item in value.items():
                    replacement = replace(item)
                    if key in expected and isinstance(replacement, str):
                        replacement = expected[key]
                    if replacement != item:
                        touched = True
                    result[key] = replacement
                return result
            if isinstance(value, list):
                return [replace(item) for item in value]
            if isinstance(value, str) and value == old_text:
                touched = True
                return new_text
            return value

        updated = replace(data)
        if touched:
            try:
                path.write_text(json.dumps(updated, ensure_ascii=False, indent=2),
                                encoding="utf-8")
                changed += 1
            except OSError:
                pass
    return changed


def update_db(conn, numero, target):
    row = conn.execute("SELECT numero FROM opportunities WHERE numero = ?",
                       (numero,)).fetchone()
    if not row:
        return False
    n = safe_name(numero)
    conn.execute(
        "UPDATE opportunities SET record_folder = ?, index_json_path = ?, "
        "detail_json_path = ?, record_folder_leaf = ?, date_folder = ?, "
        "db_reviewed_at = datetime('now') WHERE numero = ?",
        (str(target), str(target / f"{n}.json"),
         str(target / f"{n}.detail.json"), target.name, target.parent.name,
         numero),
    )
    conn.commit()
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="perform planned moves and metadata updates")
    parser.add_argument("--limit", type=int, default=0,
                        help="process at most N folders (0 = all)")
    parser.add_argument("--records-dir", default=str(RECORDS_DIR))
    args = parser.parse_args()

    records_dir = Path(args.records_dir)
    # A dry-run must remain filesystem-read-only. init_db() may apply schema
    # migrations, which is an unintended write even when --apply is absent.
    conn = init_db() if args.apply else None
    counts = {"scanned": 0, "ordered": 0, "already": 0, "conflict": 0,
              "missing_index": 0, "bad_index": 0, "no_pubdate": 0,
              "index_only": 0, "with_detail": 0, "db_updated": 0}
    print(f"{'APPLY' if args.apply else 'DRY-RUN'} | scanning {records_dir}")
    print("-" * 100)

    for folder in iter_record_folders(records_dir):
        if args.limit and counts["scanned"] >= args.limit:
            break
        counts["scanned"] += 1
        index_path, index_data, numero = find_index(folder)
        if not index_path:
            counts["missing_index"] += 1
            print(f"MISSING-INDEX {folder}")
            continue
        if not numero:
            counts["bad_index"] += 1
            print(f"BAD-INDEX     {index_path}")
            continue

        publication = parent_from_date(first_value(
            index_data, ("fecha", "fecha_index", "date_start_opportunity",
                         "first_seen")))
        if not publication:
            publication = parent_from_date(first_value(
                (load_json(folder / f"{safe_name(numero)}.detail.json") or {}),
                ("fecha_index", "date_start_opportunity", "start_date_guess")))
        if not publication:
            publication = date_folder_from_fecha(index_data.get("fecha")) or ""
        if not publication:
            counts["no_pubdate"] += 1
            print(f"NO-PUBDATE    {folder} ({numero})")
            continue

        detail_data = find_detail(folder, numero)
        end = end_stamp(index_data, detail_data)
        description = (index_data.get("descripcion")
                       or index_data.get("short_description")
                       or detail_data.get("descripcion_index")
                       or detail_data.get("short_description") or "")
        if end:
            counts["with_detail"] += 1
            target_parent = records_dir / parent_from_date(end)
            target_leaf = record_leaf(numero, description, end)
        else:
            counts["index_only"] += 1
            target_parent = records_dir / publication
            target_leaf = folder.name

        target = target_parent / target_leaf
        if target == folder:
            counts["already"] += 1
            continue
        if target.exists():
            counts["conflict"] += 1
            print(f"CONFLICT     {folder}\n  -> {target}")
            continue

        print(f"ORDER        {folder}\n  -> {target}\n"
              f"  state={'detail/end-date' if end else 'index/publication-date'}")
        if not args.apply:
            counts["ordered"] += 1
            continue
        target_parent.mkdir(parents=True, exist_ok=True)
        try:
            folder.rename(target)
            changed = rewrite_json_paths(target, folder, target, numero,
                                         target.parent.name)
            if update_db(conn, numero, target):
                counts["db_updated"] += 1
            counts["ordered"] += 1
            print(f"APPLIED      {target} (json={changed})")
        except OSError as exc:
            counts["conflict"] += 1
            print(f"ERROR        {folder}: {exc}")

    print("-" * 100)
    for key in ("scanned", "ordered", "already", "conflict", "missing_index",
                "bad_index", "no_pubdate", "index_only", "with_detail",
                "db_updated"):
        print(f"{key}: {counts[key]}")
    if not args.apply:
        print("No files changed. Review this plan, then rerun with --apply.")


if __name__ == "__main__":
    main()
