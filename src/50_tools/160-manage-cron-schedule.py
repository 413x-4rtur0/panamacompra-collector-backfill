#!/usr/bin/env python3
"""Install/update/remove the PanamaCompra automatic-scheduler crontab entry.

Single source of truth for turning a (days, start, end, interval) schedule
into a real crontab line, so both monitors just pass the raw parameters here
instead of each building the cron expression itself.

Usage:
  160-manage-cron-schedule.py install --days daily|weekdays|weekends|custom
      [--custom-days 1,2,3,4,5] --start HH:MM --end HH:MM --interval MINUTES
  160-manage-cron-schedule.py remove
  160-manage-cron-schedule.py show
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import common as pc_common

# Only this exact marker is ever touched in the user's crontab; every other
# line (including entries unrelated to this project) is left untouched.
MARKER = "# PANAMACOMPRA-CRON-SCHEDULE"
RUNNER = pc_common.APP_ROOT / "src" / "20_pipeline" / "115-cron-run.sh"
LOG_FILE = pc_common.LOG_DIR / "cron_schedule.log"

DAY_FIELDS = {
    "daily": "*",
    "weekdays": "1-5",
    "weekends": "0,6",
}


def day_field(days: str, custom_days: str) -> str:
    if days in DAY_FIELDS:
        return DAY_FIELDS[days]
    if days == "custom":
        parts = [p.strip() for p in custom_days.split(",") if p.strip()]
        for part in parts:
            if not part.isdigit() or not (0 <= int(part) <= 6):
                raise SystemExit(f"ERROR: invalid custom day {part!r}; use 0-6 (0=Sunday).")
        if not parts:
            raise SystemExit("ERROR: --days custom requires --custom-days (0=Sunday .. 6=Saturday).")
        return ",".join(parts)
    raise SystemExit(f"ERROR: unknown --days value {days!r}.")


def parse_hm(value: str, label: str) -> tuple[int, int]:
    try:
        hh, mm = value.split(":")
        hh, mm = int(hh), int(mm)
    except (ValueError, AttributeError):
        raise SystemExit(f"ERROR: {label} must be HH:MM, got {value!r}.")
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise SystemExit(f"ERROR: {label} out of range: {value!r}.")
    return hh, mm


def build_expression(days: str, custom_days: str, start: str, end: str, interval: int) -> str:
    dow = day_field(days, custom_days)
    start_h, start_m = parse_hm(start, "--start")
    end_h, end_m = parse_hm(end, "--end")
    if (end_h, end_m) <= (start_h, start_m):
        raise SystemExit("ERROR: --end must be after --start (overnight windows are not supported).")
    if interval < 1:
        raise SystemExit("ERROR: --interval must be at least 1 minute.")

    if interval < 60:
        minute_field = f"*/{interval}"
        hour_field = str(start_h) if start_h == end_h else f"{start_h}-{end_h}"
        # cron has no sub-hour window boundary, so runs can fire up to
        # `interval` minutes past --end within its final hour; a known,
        # documented cron limitation rather than something worth
        # over-engineering with wrapper scripts.
    else:
        if interval % 60 != 0:
            raise SystemExit("ERROR: --interval over 59 must be a whole number of hours (60, 120, ...).")
        step_hours = interval // 60
        minute_field = str(start_m)
        hour_field = f"{start_h}-{end_h}/{step_hours}" if end_h > start_h else str(start_h)

    return f"{minute_field} {hour_field} * * {dow}"


def current_crontab() -> list[str]:
    result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line.strip()]


def install(expression: str) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    line = f'{expression} "{RUNNER}" >> "{LOG_FILE}" 2>&1 {MARKER}'
    lines = [existing for existing in current_crontab() if MARKER not in existing]
    lines.append(line)
    subprocess.run(["crontab", "-"], input="\n".join(lines) + "\n", text=True, check=True)
    print(f"Installed cron schedule: {expression}")


def remove() -> None:
    lines = [existing for existing in current_crontab() if MARKER not in existing]
    payload = ("\n".join(lines) + "\n") if lines else ""
    subprocess.run(["crontab", "-"], input=payload, text=True, check=True)
    print("Removed PanamaCompra cron schedule entry (if any).")


def show() -> None:
    matches = [line for line in current_crontab() if MARKER in line]
    print(matches[0] if matches else "(no schedule installed)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="action", required=True)

    install_p = sub.add_parser("install")
    install_p.add_argument("--days", required=True, choices=["daily", "weekdays", "weekends", "custom"])
    install_p.add_argument("--custom-days", default="", help="Comma-separated 0-6 (0=Sunday) when --days=custom")
    install_p.add_argument("--start", required=True, help="HH:MM")
    install_p.add_argument("--end", required=True, help="HH:MM")
    install_p.add_argument("--interval", required=True, type=int, help="Minutes between runs")

    sub.add_parser("remove")
    sub.add_parser("show")

    args = parser.parse_args()
    if args.action == "install":
        expr = build_expression(args.days, args.custom_days, args.start, args.end, args.interval)
        install(expr)
    elif args.action == "remove":
        remove()
    else:
        show()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
