#!/usr/bin/env python3
"""Customize the WhatsApp message formats (index / details / status).

    pcc format                       list the three formats (custom or default)
    pcc format show index            print the active template
    pcc format preview index         render the active template with sample data
    pcc format set index --file f.txt   save a custom template (or pipe stdin)
    pcc format reset index           back to the built-in layout
    pcc format placeholders          list the available {placeholders}

Templates use Python-style {placeholder} fields; unknown placeholders are kept
literally, so a typo shows up in the message instead of breaking the send. The
same templates are editable from both monitors; everything is stored in
data/config/waha_format_<kind>.txt.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SRC_DIR))
sys.path.insert(0, str(_SRC_DIR / "pipeline"))
import notify_new_records as nnr

KINDS = nnr.FORMAT_KINDS
KIND_LABELS = {
    "index": "index alert (🔔 Nueva Oportunidad, sent right after the scan)",
    "details": "detail follow-up (📥 Detalles Completos, sent after download)",
    "status": "status change (cambios de estado / cancelaciones / items)",
}


def check_kind(kind: str) -> str:
    if kind not in KINDS:
        raise SystemExit(f"Unknown format kind: {kind!r} (use {'|'.join(KINDS)})")
    return kind


def cmd_list(_args) -> int:
    for kind in KINDS:
        state = "custom" if nnr.load_custom_format(kind) else "default"
        print(f"  {kind:<8} [{state}]  {KIND_LABELS[kind]}")
    print("\nShow one with: pcc format show <kind> · preview: pcc format preview <kind>")
    print("Customize with: pcc format set <kind> --file plantilla.txt (or pipe stdin)")
    return 0


def cmd_show(args) -> int:
    kind = check_kind(args.kind)
    custom = nnr.load_custom_format(kind)
    print(f"# {kind} format [{'custom — ' + str(nnr.format_path(kind)) if custom else 'built-in default'}]")
    print(custom or nnr.DEFAULT_FORMATS[kind])
    return 0


def cmd_preview(args) -> int:
    kind = check_kind(args.kind)
    print(nnr.render_format(kind))
    return 0


def cmd_set(args) -> int:
    kind = check_kind(args.kind)
    if args.file:
        template = Path(args.file).expanduser().read_text(encoding="utf-8")
    elif not sys.stdin.isatty():
        template = sys.stdin.read()
    else:
        raise SystemExit("Provide the template with --file PATH or pipe it on stdin.")
    template = template.strip("\n")
    if not template.strip():
        raise SystemExit("Template is empty; use 'pcc format reset' to go back to the default.")
    path = nnr.format_path(kind)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(template + "\n", encoding="utf-8")
    print(f"Saved custom {kind} format to {path}. Preview:")
    print("-" * 60)
    print(nnr.render_format(kind))
    return 0


def cmd_reset(args) -> int:
    kind = check_kind(args.kind)
    path = nnr.format_path(kind)
    if path.exists():
        path.unlink()
        print(f"Custom {kind} format removed; the built-in layout is active again.")
    else:
        print(f"The {kind} format was already the built-in default.")
    return 0


def cmd_placeholders(_args) -> int:
    print("Available {placeholders} (unknown ones are kept literally):\n")
    for name, description in nnr.PLACEHOLDERS.items():
        print(f"  {{{name}}}".ljust(22) + description)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Customize the WhatsApp message formats.")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("list", help="list the three formats and whether each is customized").set_defaults(func=cmd_list)
    p = sub.add_parser("show", help="print the active template for a kind")
    p.add_argument("kind", choices=KINDS)
    p.set_defaults(func=cmd_show)
    p = sub.add_parser("preview", help="render the active template with sample data")
    p.add_argument("kind", choices=KINDS)
    p.set_defaults(func=cmd_preview)
    p = sub.add_parser("set", help="save a custom template from --file or stdin")
    p.add_argument("kind", choices=KINDS)
    p.add_argument("--file", default="", help="file containing the template")
    p.set_defaults(func=cmd_set)
    p = sub.add_parser("reset", help="remove the custom template (back to the default)")
    p.add_argument("kind", choices=KINDS)
    p.set_defaults(func=cmd_reset)
    sub.add_parser("placeholders", help="list the available placeholders").set_defaults(func=cmd_placeholders)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        return cmd_list(args)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
