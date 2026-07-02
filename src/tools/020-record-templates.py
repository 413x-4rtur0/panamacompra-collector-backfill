#!/usr/bin/env python3
"""Work templates for record folders.

The operator keeps reusable working files (bid forms, checklists,
spreadsheets, ...) in a template source folder. A selection of one or more of
those files is copied into a ``templates/`` subfolder inside every record's
detail folder, so each opportunity comes ready to work on.

    pcc templates source [PATH]        show or set the template source folder
    pcc templates list                 list source files, marking the selected ones
    pcc templates select FILE...       add file(s) to the selection
    pcc templates unselect FILE...     remove file(s) from the selection
    pcc templates clear                empty the selection
    pcc templates apply [options]      copy the selection into record folders

``apply`` is dry-run by default (repo convention); pass ``--apply`` to copy.
Existing files are never overwritten unless ``--overwrite`` is given, so work
already done inside a record's templates/ folder is safe. The run-all worker
auto-applies the selection to records downloaded in the current run
(``apply --since <run start> --apply``); disable with PC_TEMPLATES_AUTO=0.

Resolution of the source folder: PC_TEMPLATES_SRC_DIR environment variable,
then monitor_settings.env, then the self-contained default
``$PC_STATE_DIR/templates`` (var/templates in development/portable mode).
"""
from __future__ import annotations

import argparse
import os
import shlex
import shutil
import sys
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SRC_DIR))
import common as pc_common

SETTINGS_PATH = pc_common.DATA_CONFIG_DIR / "monitor_settings.env"
SELECTED_PATH = pc_common.DATA_CONFIG_DIR / "templates_selected.txt"
DEFAULT_SOURCE = pc_common.STATE_DIR / "templates"


def load_settings() -> dict[str, str]:
    data: dict[str, str] = {}
    if not SETTINGS_PATH.exists():
        return data
    for line in SETTINGS_PATH.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        try:
            parts = shlex.split(value)
            data[key.strip()] = " ".join(parts) if parts else ""
        except ValueError:
            data[key.strip()] = value.strip().strip('"').strip("'")
    return data


def save_setting(key: str, value: str) -> None:
    raw: dict[str, str] = {}
    if SETTINGS_PATH.exists():
        for line in SETTINGS_PATH.read_text(encoding="utf-8", errors="replace").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                raw[k.strip()] = v
    raw[key] = shlex.quote(value)
    lines = [
        "# PanamaCompra monitor settings (KEY=VALUE).",
        "# Edited from the monitor Settings panel or the pcc CLI; same-named",
        "# environment variables override these at startup.",
    ]
    lines += [f"{k}={raw[k]}" for k in sorted(raw)]
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def source_dir() -> Path:
    raw = os.environ.get("PC_TEMPLATES_SRC_DIR", "").strip()
    if not raw:
        raw = load_settings().get("PC_TEMPLATES_SRC_DIR", "").strip()
    path = Path(raw).expanduser() if raw else DEFAULT_SOURCE
    return path if path.is_absolute() else pc_common.APP_ROOT / path


def load_selection() -> list[str]:
    if not SELECTED_PATH.exists():
        return []
    names = []
    for line in SELECTED_PATH.read_text(encoding="utf-8", errors="replace").splitlines():
        token = line.strip()
        if token and not token.startswith("#"):
            names.append(token)
    return names


def save_selection(names: list[str]) -> None:
    SELECTED_PATH.parent.mkdir(parents=True, exist_ok=True)
    SELECTED_PATH.write_text(("\n".join(names) + "\n") if names else "", encoding="utf-8")


def source_files(src: Path) -> list[str]:
    """All files under the source folder as sorted relative paths."""
    if not src.is_dir():
        return []
    return sorted(
        str(path.relative_to(src))
        for path in src.rglob("*")
        if path.is_file() and not path.name.startswith(".")
    )


def normalize(names: list[str], available: list[str]) -> tuple[list[str], list[str]]:
    """Match requested names against source files (exact relative path or bare
    file name when unambiguous). Returns (matched, unmatched)."""
    matched: list[str] = []
    unmatched: list[str] = []
    by_basename: dict[str, list[str]] = {}
    for rel in available:
        by_basename.setdefault(Path(rel).name, []).append(rel)
    for name in names:
        name = name.strip().lstrip("./")
        if name in available:
            matched.append(name)
        elif len(by_basename.get(name, [])) == 1:
            matched.append(by_basename[name][0])
        else:
            unmatched.append(name)
    return matched, unmatched


def cmd_source(args) -> int:
    if args.path:
        path = Path(args.path).expanduser()
        if not path.is_absolute():
            path = (Path.cwd() / path).resolve()
        path.mkdir(parents=True, exist_ok=True)
        save_setting("PC_TEMPLATES_SRC_DIR", str(path))
        print(f"Template source folder set to {path} (saved to monitor_settings.env).")
    else:
        src = source_dir()
        print(f"Template source folder: {src}{'' if src.is_dir() else '  (does not exist yet — create it or set another with: pcc templates source PATH)'}")
    return 0


def cmd_list(_args) -> int:
    src = source_dir()
    files = source_files(src)
    selected = set(load_selection())
    print(f"Template source folder: {src}")
    if not files:
        print("(no template files found — drop your working files there or set another source with: pcc templates source PATH)")
        return 0
    for rel in files:
        print(f"  [{'x' if rel in selected else ' '}] {rel}")
    stale = sorted(selected - set(files))
    for rel in stale:
        print(f"  [!] {rel}  (selected but missing from the source folder)")
    print(f"{len(selected & set(files))} of {len(files)} selected. Select with: pcc templates select FILE...")
    return 0


def cmd_select(args) -> int:
    src = source_dir()
    available = source_files(src)
    matched, unmatched = normalize(args.files, available)
    for name in unmatched:
        print(f"Not found in {src}: {name}", file=sys.stderr)
    selection = load_selection()
    added = [name for name in matched if name not in selection]
    selection.extend(added)
    save_selection(selection)
    print(f"Selected {len(added)} new template(s); selection now has {len(selection)}.")
    return 1 if unmatched else 0


def cmd_unselect(args) -> int:
    selection = load_selection()
    matched, unmatched = normalize(args.files, selection)
    remaining = [name for name in selection if name not in set(matched)]
    save_selection(remaining)
    for name in unmatched:
        print(f"Was not selected: {name}", file=sys.stderr)
    print(f"Removed {len(selection) - len(remaining)} template(s); selection now has {len(remaining)}.")
    return 0


def cmd_clear(_args) -> int:
    save_selection([])
    print("Template selection cleared.")
    return 0


def target_records(conn, numeros: list[str], since: str):
    """(numero, record_folder) rows the selection should be applied to."""
    query = (
        "SELECT numero, record_folder FROM opportunities "
        "WHERE detail_status = 'saved' AND COALESCE(record_folder, '') != ''"
    )
    params: list[str] = []
    if numeros:
        query += f" AND numero IN ({','.join('?' * len(numeros))})"
        params.extend(numeros)
    if since:
        query += " AND COALESCE(detail_saved_at, '') >= ?"
        params.append(since)
    query += " ORDER BY numero"
    return conn.execute(query, params).fetchall()


def cmd_apply(args) -> int:
    src = source_dir()
    selection = load_selection()
    if not selection:
        print("No templates selected — nothing to apply. Select with: pcc templates select FILE...")
        return 0
    missing = [name for name in selection if not (src / name).is_file()]
    for name in missing:
        print(f"WARNING: selected template missing from source, skipped: {src / name}", file=sys.stderr)
    usable = [name for name in selection if name not in set(missing)]
    if not usable:
        print("None of the selected templates exist in the source folder; nothing to do.", file=sys.stderr)
        return 1

    conn = pc_common.init_db()
    rows = target_records(conn, args.numero, args.since or "")
    copied = skipped = folders = 0
    for row in rows:
        record_folder = Path(row["record_folder"])
        if not record_folder.is_dir():
            continue
        dest_dir = record_folder / "templates"
        folders += 1
        for name in usable:
            dest = dest_dir / name
            if dest.exists() and not args.overwrite:
                skipped += 1
                continue
            if args.apply:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src / name, dest)
            else:
                print(f"would copy: {name} -> {dest}")
            copied += 1

    mode = "" if args.apply else " (dry-run — pass --apply to copy)"
    print(
        f"Templates{mode}: {copied} file(s) {'copied' if args.apply else 'to copy'} into {folders} record folder(s); "
        f"{skipped} kept (already exist{'' if args.overwrite else '; use --overwrite to replace'})."
    )
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Manage work templates copied into each record's templates/ folder.")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("source", help="show or set the template source folder")
    p.add_argument("path", nargs="?", help="new source folder (created if missing)")
    p.set_defaults(func=cmd_source)

    p = sub.add_parser("list", help="list source template files, marking the selected ones")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("select", help="add template file(s) to the selection")
    p.add_argument("files", nargs="+", help="relative path or unambiguous file name inside the source folder")
    p.set_defaults(func=cmd_select)

    p = sub.add_parser("unselect", help="remove template file(s) from the selection")
    p.add_argument("files", nargs="+")
    p.set_defaults(func=cmd_unselect)

    p = sub.add_parser("clear", help="empty the selection")
    p.set_defaults(func=cmd_clear)

    p = sub.add_parser("apply", help="copy the selected templates into record folders (dry-run unless --apply)")
    p.add_argument("--numero", action="append", default=[], help="apply only to this record; repeat for several")
    p.add_argument("--since", default="", help="apply only to records whose detail was saved at/after this timestamp")
    p.add_argument("--overwrite", action="store_true", help="replace files that already exist in a record's templates/ folder")
    p.add_argument("--apply", action="store_true", help="actually copy (default is a dry-run listing)")
    p.set_defaults(func=cmd_apply)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
