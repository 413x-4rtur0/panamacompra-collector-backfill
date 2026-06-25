#!/usr/bin/env python3
"""Review and refresh PanamaCompra archive database metadata.

This is intentionally browser-free. It applies schema migrations through
``pc_common.init_db()``, then backfills columns that describe the on-disk record
layout (folder leaf, split detail/table counts, and layout version). The updater
runs it after pulling new code so older local databases are ready before the next
collector task starts.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pc_common

CURRENT_FILES_LAYOUT_VERSION = 2


def _count_mapping(value: object) -> int:
    if isinstance(value, dict):
        return len([k for k in value if k != "_all"])
    if isinstance(value, list):
        return len(value)
    return 0


def _layout_version(record_folder: Path, detail_data: dict) -> int:
    numero = detail_data.get("numero") or record_folder.name
    n = pc_common.safe_name(numero)
    has_new_detail = (record_folder / "detail_sections" / f"{n}-DETAIL-000-ALL.json").exists()
    has_new_table = (record_folder / "tables" / f"{n}-TABLE-000-ALL.json").exists()
    if has_new_detail or has_new_table:
        return CURRENT_FILES_LAYOUT_VERSION
    return int(detail_data.get("files_layout_version") or 1)


def _date_from_detail(detail_data: dict, *names: str) -> str:
    for name in names:
        value = detail_data.get(name)
        if value:
            return str(value)
    summary = detail_data.get("summary") if isinstance(detail_data.get("summary"), dict) else {}
    calendar = detail_data.get("calendar") if isinstance(detail_data.get("calendar"), dict) else {}
    for name in names:
        value = summary.get(name) or calendar.get(name)
        if value:
            return str(value)
    return ""


def refresh_row(conn, row, *, apply: bool) -> bool:
    record_folder = Path(row["record_folder"] or "")
    detail_json_path = Path(row["detail_json_path"] or "")
    if not detail_json_path.is_absolute():
        detail_json_path = pc_common.BASE_DIR / detail_json_path

    detail_data: dict = {}
    if detail_json_path.exists():
        try:
            loaded = json.loads(detail_json_path.read_text(encoding="utf-8"))
            detail_data = loaded if isinstance(loaded, dict) else {}
        except (OSError, json.JSONDecodeError):
            detail_data = {}

    detail_sections_count = _count_mapping(detail_data.get("detail_sections"))
    tables_count = int(detail_data.get("tables_count") or _count_mapping(detail_data.get("tables")))
    layout_version = _layout_version(record_folder, detail_data) if record_folder else 1
    folder_leaf = record_folder.name if str(record_folder) else ""
    start_date_guess = _date_from_detail(detail_data, "start_date_guess", "date_start_opportunity", "dtstart")
    finish_date_guess = _date_from_detail(detail_data, "finish_date_guess", "date_end_opportunity", "dtend")

    changed = (
        (row["record_folder_leaf"] or "") != folder_leaf
        or int(row["files_layout_version"] or 0) != layout_version
        or int(row["detail_sections_count"] or 0) != detail_sections_count
        or int(row["tables_count"] or 0) != tables_count
        or (start_date_guess and (row["start_date_guess"] or "") != start_date_guess)
        or (finish_date_guess and (row["finish_date_guess"] or "") != finish_date_guess)
    )
    if apply and changed:
        conn.execute(
            """
            UPDATE opportunities
            SET record_folder_leaf = ?,
                files_layout_version = ?,
                detail_sections_count = ?,
                tables_count = ?,
                start_date_guess = COALESCE(NULLIF(?, ''), start_date_guess),
                finish_date_guess = COALESCE(NULLIF(?, ''), finish_date_guess),
                db_reviewed_at = ?
            WHERE numero = ?
            """,
            (
                folder_leaf,
                layout_version,
                detail_sections_count,
                tables_count,
                start_date_guess,
                finish_date_guess,
                pc_common.now_iso(),
                row["numero"],
            ),
        )
    return changed


def main() -> int:
    parser = argparse.ArgumentParser(description="Review/backfill archive DB metadata columns.")
    parser.add_argument("--apply", action="store_true", help="write metadata backfills (default: dry-run)")
    args = parser.parse_args()

    conn = pc_common.init_db()
    rows = conn.execute("SELECT * FROM opportunities ORDER BY first_seen, numero").fetchall()
    changed = 0
    for row in rows:
        if refresh_row(conn, row, apply=args.apply):
            changed += 1
    if args.apply:
        conn.commit()
    print(f"DB maintenance scanned={len(rows)} changed={changed} mode={'APPLY' if args.apply else 'DRY-RUN'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
