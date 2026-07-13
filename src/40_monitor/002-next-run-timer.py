#!/usr/bin/env python3
"""Tiny fixed-size Tk dashboard for the next scheduled live run.

It stays near the top-center of the desktop while the collector is idle and
withdraws while a live run is active (reappearing when the run ends). Besides the
countdown to the next run it shows context the operator usually wants at a
glance: the current git branch, the latest collected records, and a summary of
the last run (new/saved counts and total archive size).
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import tkinter as tk
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import common as pc_common

BASE_DIR = pc_common.APP_ROOT
PROGRESS_FILE = pc_common.PROGRESS_PATH
LAST_SUMMARY_FILE = pc_common.LOG_DIR / "run_all_last_summary.env"
ARCHIVE_DB = pc_common.DB_PATH
SETTINGS_PATH = pc_common.DATA_CONFIG_DIR / "monitor_settings.env"
BOOTSTRAP_ENV_PATH = BASE_DIR / ".env"
REQUEST_FLAG = pc_common.QUEUE_DIR / "run_all_requested.flag"
UPDATE_QUEUE_FLAG = pc_common.QUEUE_DIR / "update_monitor_requested.flag"
UPDATE_IN_PROGRESS_FLAG = pc_common.QUEUE_DIR / "update_monitor_in_progress.flag"


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key, raw = line.split("=", 1)
        try:
            parsed = shlex.split(raw, posix=True)
            values[key.strip()] = parsed[0] if parsed else ""
        except ValueError:
            values[key.strip()] = raw.strip().strip("\'\"")
    return values


def settings_file() -> dict[str, str]:
    return read_env_file(SETTINGS_PATH)


_BOOTSTRAP_SETTINGS = read_env_file(BOOTSTRAP_ENV_PATH)
_SETTINGS = settings_file()


def setting(name: str, default: str) -> str:
    return os.environ.get(name) or _BOOTSTRAP_SETTINGS.get(name) or _SETTINGS.get(name) or default


def setting_int(name: str, default: str, minimum: int = 0) -> int:
    try:
        return max(minimum, int(setting(name, default)))
    except ValueError:
        return max(minimum, int(default))


def setting_float(name: str, default: str, minimum: float, maximum: float) -> float:
    try:
        return max(minimum, min(maximum, float(setting(name, default))))
    except ValueError:
        return max(minimum, min(maximum, float(default)))


def setting_bool(name: str, default: str) -> bool:
    return setting(name, default).strip().lower() not in {"0", "false", "no", "off", ""}


INTERVAL_MINUTES = setting_int("PC_NEXT_RUN_INTERVAL_MINUTES", "30", 1)
AUTORUN_SOURCE = setting("PC_AUTORUN_SOURCE", "changedetection").strip().lower()
CHANGEDETECTION_BASE_URL = setting("CHANGEDETECTION_BASE_URL", "http://localhost:5000").rstrip("/")
CHANGEDETECTION_API_KEY = setting("CHANGEDETECTION_API_KEY", "").strip()
# Synced from changedetection's datastore by 010-docker-stack.sh. A watch-level
# override returned by the API always takes precedence over this global value.
CHANGEDETECTION_CHECK_INTERVAL_SECONDS = setting_int(
    "PC_CHANGEDETECTION_CHECK_INTERVAL_SECONDS", str(INTERVAL_MINUTES * 60), 1
)
CHANGEDETECTION_REFRESH_SECONDS = setting_int("PC_CHANGEDETECTION_TIMER_REFRESH_SECONDS", "30", 5)
CHANGEDETECTION_SCHEDULE_REFRESH_SECONDS = setting_int(
    "PC_CHANGEDETECTION_SCHEDULE_REFRESH_SECONDS", "300", 30
)
_integrations_dir = Path(setting("PC_INTEGRATIONS_DIR", str(pc_common.STATE_DIR / "integrations"))).expanduser()
if not _integrations_dir.is_absolute():
    _integrations_dir = BASE_DIR / _integrations_dir
_changedetection_datastore_dir = Path(
    setting("PC_CHANGEDETECTION_DATASTORE", str(_integrations_dir / "changedetection"))
).expanduser()
if not _changedetection_datastore_dir.is_absolute():
    _changedetection_datastore_dir = BASE_DIR / _changedetection_datastore_dir
CHANGEDETECTION_DATASTORE_FILE = _changedetection_datastore_dir / "changedetection.json"
# Fixed window size. Bigger by default than the old timer because it now carries
# the branch, latest records and last-run summary; still pinned (resizable off).
WINDOW_WIDTH = setting_int("PC_NEXT_RUN_TIMER_WIDTH", "380", 240)
WINDOW_HEIGHT = setting_int("PC_NEXT_RUN_TIMER_HEIGHT", "360", 220)
WINDOW_TOP = setting_int("PC_NEXT_RUN_TIMER_TOP", "30", 0)
# How many of the most recent records to list. The "Latest records" field is
# scrollable, so this can comfortably be larger than the few rows that fit.
RECORDS_SHOWN = max(1, setting_int("PC_NEXT_RUN_TIMER_RECORDS", "20", 1))
# Refresh the cheap countdown every second; the heavier git/DB reads less often.
DATA_REFRESH_TICKS = max(1, setting_int("PC_NEXT_RUN_TIMER_DATA_REFRESH_SECONDS", "10", 1))
# Window opacity (1.0 = fully opaque). Applied best-effort: X11 setups without a
# compositor may ignore it. Default is a subtle translucency so the overlay is a
# little less obtrusive while staying clearly legible; lower it toward 0.2 for a
# more see-through window. Clamped to [0.2, 1.0] so the countdown can never be
# made invisible.
WINDOW_ALPHA = setting_float("PC_NEXT_RUN_TIMER_ALPHA", "0.75", 0.2, 1.0)
# Keep the timer above other windows (default on, matching the previous behaviour).
# Set PC_NEXT_RUN_TIMER_TOPMOST=0 to let it fall behind focused windows.
WINDOW_TOPMOST = setting_bool("PC_NEXT_RUN_TIMER_TOPMOST", "1")

ACTIVE_PHASES = {"STARTING", "UPDATE", "INDEX", "DETAIL", "CALENDAR", "MESSAGING", "TEST"}
ACTIVE_STATUSES = {"RUNNING"}


def progress_values() -> dict[str, str]:
    values: dict[str, str] = {}
    if not PROGRESS_FILE.exists():
        return values
    for line in PROGRESS_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


def last_summary_values() -> dict[str, str]:
    values: dict[str, str] = {}
    if not LAST_SUMMARY_FILE.exists():
        return values
    for line in LAST_SUMMARY_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


def duration_parts_text(values: dict[str, str]) -> str:
    if not values:
        return "Prev duration: —"
    total = values.get("TOTAL_TEXT") or "—"
    index = values.get("INDEX_SECONDS", "0")
    detail = values.get("DETAIL_SECONDS", "0")
    views = values.get("VIEW_SECONDS", "0")
    calendar = values.get("CALENDAR_SECONDS", "0")
    messaging = values.get("MESSAGING_SECONDS", "0")
    return f"Prev duration: {total} (idx {index}s · det {detail}s · store {views}s · cal {calendar}s · msg {messaging}s)"


def is_live_run_active() -> bool:
    if subprocess.run(["pgrep", "-f", "[r]un-worker.sh"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
        return True
    values = progress_values()
    # The worker process is the authoritative signal; the progress file is the
    # fallback for the brief windows around start/stop. A sandbox TEST run never
    # hides the "next live run" countdown — it does not touch the real schedule.
    if values.get("MODE", "").strip().upper() == "TEST":
        return False
    return values.get("PHASE") in ACTIVE_PHASES or values.get("STATUS") in ACTIVE_STATUSES


def _parse_progress_timestamp(text: str) -> datetime | None:
    text = (text or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def last_live_run_start() -> datetime | None:
    """When the most recent real run started, taken from the progress file.

    The countdown is anchored to the real previous run, so it reflects when the
    next run is actually due (last start + interval) rather than an arbitrary
    wall-clock boundary. A sandbox TEST run is ignored: it does not advance the
    real collection schedule, so the countdown keeps using the last real run (or
    the clock fallback) instead.
    """
    values = progress_values()
    if values.get("MODE", "").strip().upper() == "TEST":
        return None
    return _parse_progress_timestamp(values.get("STARTED_AT", "")) or _parse_progress_timestamp(values.get("UPDATED_AT", ""))


def clock_bucket_next_run(now: datetime) -> datetime:
    minute_bucket = (now.minute // INTERVAL_MINUTES + 1) * INTERVAL_MINUTES
    if minute_bucket >= 60:
        return (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    return now.replace(minute=minute_bucket, second=0, microsecond=0)


def _duration_seconds(parts: object) -> int:
    """Convert changedetection's {weeks,days,hours,minutes,seconds} value."""
    if not isinstance(parts, dict):
        return 0
    multipliers = {
        "weeks": 7 * 24 * 3600,
        "days": 24 * 3600,
        "hours": 3600,
        "minutes": 60,
        "seconds": 1,
    }
    total = 0
    for name, multiplier in multipliers.items():
        try:
            total += int(parts.get(name) or 0) * multiplier
        except (TypeError, ValueError):
            continue
    return max(0, total)


_CHANGEDETECTION_CACHE: tuple[float, datetime | None, str] = (0.0, None, "")
_CHANGEDETECTION_SCHEDULE_CACHE: tuple[float, dict[str, object], str] = (0.0, {}, "")
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def _changedetection_global_schedule() -> tuple[dict[str, object], str]:
    """Read and cache changedetection's global time window/timezone.

    changedetection writes its datastore as root with mode 600 in the current
    container image. Prefer a direct host read when permissions allow it;
    otherwise ask the already-running container for only these two non-secret
    fields. That fallback runs at most once every five minutes by default.
    """
    global _CHANGEDETECTION_SCHEDULE_CACHE

    now_epoch = datetime.now().timestamp()
    cached_at, cached_schedule, cached_timezone = _CHANGEDETECTION_SCHEDULE_CACHE
    if now_epoch - cached_at < CHANGEDETECTION_SCHEDULE_REFRESH_SECONDS:
        return cached_schedule, cached_timezone

    data: dict[str, object] | None = None
    try:
        loaded = json.loads(CHANGEDETECTION_DATASTORE_FILE.read_text(encoding="utf-8"))
        data = loaded if isinstance(loaded, dict) else None
    except (OSError, ValueError):
        pass

    if data is None:
        container_reader = (
            "import json; "
            "d=json.load(open('/datastore/changedetection.json',encoding='utf-8')); "
            "s=d.get('settings',{}); "
            "print(json.dumps({'schedule':s.get('requests',{}).get('time_schedule_limit',{}),"
            "'timezone':s.get('application',{}).get('scheduler_timezone_default','')}))"
        )
        try:
            result = subprocess.run(
                ["docker", "compose", "exec", "-T", "changedetection", "python", "-c", container_reader],
                cwd=BASE_DIR, capture_output=True, text=True, timeout=8,
            )
            payload = json.loads(result.stdout) if result.returncode == 0 else {}
        except (OSError, ValueError, subprocess.SubprocessError):
            payload = {}
        schedule = payload.get("schedule") if isinstance(payload, dict) else {}
        default_tz = payload.get("timezone") if isinstance(payload, dict) else ""
        schedule = schedule if isinstance(schedule, dict) else {}
        timezone_name = str(default_tz or "")
        _CHANGEDETECTION_SCHEDULE_CACHE = (now_epoch, schedule, timezone_name)
        return schedule, timezone_name

    settings = data.get("settings") if isinstance(data, dict) else {}
    requests = settings.get("requests") if isinstance(settings, dict) else {}
    application = settings.get("application") if isinstance(settings, dict) else {}
    schedule = requests.get("time_schedule_limit") if isinstance(requests, dict) else {}
    default_tz = application.get("scheduler_timezone_default") if isinstance(application, dict) else ""
    schedule = schedule if isinstance(schedule, dict) else {}
    timezone_name = str(default_tz or "")
    _CHANGEDETECTION_SCHEDULE_CACHE = (now_epoch, schedule, timezone_name)
    return schedule, timezone_name


def _schedule_timezone(schedule: dict[str, object], default_tz: str) -> tuple[ZoneInfo, str]:
    timezone_name = str(schedule.get("timezone") or default_tz or "UTC").strip()
    try:
        return ZoneInfo(timezone_name), timezone_name
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC"), "UTC"


def _schedule_duration_minutes(day: dict[str, object]) -> int:
    duration = day.get("duration")
    if not isinstance(duration, dict):
        return 0
    try:
        return max(0, int(duration.get("hours") or 0) * 60 + int(duration.get("minutes") or 0))
    except (TypeError, ValueError):
        return 0


def _schedule_start(day_date, day: dict[str, object], tz: ZoneInfo) -> datetime | None:
    try:
        hour, minute = (int(part) for part in str(day.get("start_time") or "").split(":", 1))
        return datetime(day_date.year, day_date.month, day_date.day, hour, minute, tzinfo=tz)
    except (TypeError, ValueError):
        return None


def _schedule_summary(schedule: dict[str, object], default_tz: str) -> str:
    if not schedule.get("enabled"):
        return ""
    tz, timezone_name = _schedule_timezone(schedule, default_tz)
    enabled: list[tuple[str, dict[str, object]]] = []
    for name in _WEEKDAYS:
        day = schedule.get(name)
        if isinstance(day, dict) and day.get("enabled"):
            enabled.append((name, day))
    if not enabled:
        return f"schedule has no enabled days · {timezone_name}"
    signatures = {
        (str(day.get("start_time") or ""), _schedule_duration_minutes(day))
        for _name, day in enabled
    }
    if len(enabled) == 7 and len(signatures) == 1:
        start_text, duration_minutes = next(iter(signatures))
        try:
            hour, minute = (int(part) for part in start_text.split(":", 1))
            start = datetime(2000, 1, 3, hour, minute, tzinfo=tz)
            end = start + timedelta(minutes=duration_minutes)
            end_text = end.strftime("%H:%M") + ("+1d" if end.date() != start.date() else "")
            return f"allowed daily {start.strftime('%H:%M')}–{end_text} · {timezone_name}"
        except ValueError:
            pass
    return f"allowed on {len(enabled)} scheduled day{'s' if len(enabled) != 1 else ''} · {timezone_name}"


def _next_allowed_schedule_time(
    candidate: datetime, schedule: dict[str, object], default_tz: str
) -> tuple[datetime | None, str]:
    """Move an interval candidate into changedetection's next allowed window.

    ``candidate`` is timezone-aware. The returned timestamp stays aware and is
    ``None`` only when scheduling is enabled but no valid day window exists.
    """
    if not schedule.get("enabled"):
        return candidate, ""
    tz, _timezone_name = _schedule_timezone(schedule, default_tz)
    local_candidate = candidate.astimezone(tz)
    summary = _schedule_summary(schedule, default_tz)
    for offset in range(8):
        day_date = local_candidate.date() + timedelta(days=offset)
        day = schedule.get(_WEEKDAYS[day_date.weekday()])
        if not isinstance(day, dict) or not day.get("enabled"):
            continue
        start = _schedule_start(day_date, day, tz)
        duration_minutes = _schedule_duration_minutes(day)
        if start is None or duration_minutes <= 0:
            continue
        end = start + timedelta(minutes=duration_minutes)
        if start <= local_candidate <= end:
            return candidate, summary
        if local_candidate < start:
            return start.astimezone(timezone.utc), summary
    return None, summary


def changedetection_next_check() -> tuple[datetime | None, str]:
    """Return the earliest active changedetection watch check from its local API.

    The API supplies each watch's authoritative ``last_checked`` epoch and any
    watch-level interval override. The global interval is synchronized into the
    local environment by the Docker-stack helper because changedetection 0.55.x
    does not expose global settings through its public API.
    """
    global _CHANGEDETECTION_CACHE

    if AUTORUN_SOURCE != "changedetection" or not CHANGEDETECTION_API_KEY:
        return None, ""

    now_epoch = datetime.now().timestamp()
    cached_at, cached_target, cached_note = _CHANGEDETECTION_CACHE
    if now_epoch - cached_at < CHANGEDETECTION_REFRESH_SECONDS:
        return cached_target, cached_note

    request = urllib.request.Request(
        f"{CHANGEDETECTION_BASE_URL}/api/v1/watch",
        headers={"x-api-key": CHANGEDETECTION_API_KEY, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            payload = json.load(response)
    except (OSError, ValueError, urllib.error.URLError):
        _CHANGEDETECTION_CACHE = (now_epoch, None, "")
        return None, ""

    if isinstance(payload, dict):
        watches = payload.items()
    elif isinstance(payload, list):
        watches = ((str(watch.get("uuid") or index), watch) for index, watch in enumerate(payload) if isinstance(watch, dict))
    else:
        watches = ()
    global_schedule, scheduler_timezone = _changedetection_global_schedule()
    candidates: list[tuple[datetime, str]] = []
    for watch_id, watch in watches:
        if not isinstance(watch, dict) or watch.get("paused"):
            continue
        # The list API intentionally returns a compact watch summary in
        # changedetection 0.55.x. Fetch the detail only when the scheduling
        # fields are absent so watch-specific interval/window overrides remain
        # authoritative. This stays inside the same 30-second API cache cycle.
        if watch_id and not {
            "time_between_check_use_default", "time_between_check", "time_schedule_limit"
        }.issubset(watch):
            detail_request = urllib.request.Request(
                f"{CHANGEDETECTION_BASE_URL}/api/v1/watch/{watch_id}",
                headers={"x-api-key": CHANGEDETECTION_API_KEY, "Accept": "application/json"},
            )
            try:
                with urllib.request.urlopen(detail_request, timeout=2) as response:
                    detail = json.load(response)
                if isinstance(detail, dict):
                    watch = {**watch, **detail}
            except (OSError, ValueError, urllib.error.URLError):
                pass
        try:
            last_checked = float(watch.get("last_checked") or 0)
        except (TypeError, ValueError):
            continue
        if last_checked <= 0:
            continue
        interval = _duration_seconds(watch.get("time_between_check"))
        if interval <= 0:
            interval = CHANGEDETECTION_CHECK_INTERVAL_SECONDS
        candidate = datetime.fromtimestamp(last_checked + interval, tz=timezone.utc)
        uses_default = bool(watch.get("time_between_check_use_default", True))
        watch_schedule = watch.get("time_schedule_limit")
        schedule = global_schedule if uses_default else (watch_schedule if isinstance(watch_schedule, dict) else {})
        candidate, schedule_note = _next_allowed_schedule_time(candidate, schedule, scheduler_timezone)
        if candidate is not None:
            candidates.append((candidate, schedule_note))

    selected = min(candidates, key=lambda item: item[0]) if candidates else None
    target = selected[0].astimezone().replace(tzinfo=None) if selected else None
    schedule_note = selected[1] if selected else _schedule_summary(global_schedule, scheduler_timezone)
    note = f"changedetection API · {len(candidates)} active watch{'es' if len(candidates) != 1 else ''}"
    if schedule_note:
        note += f" · {schedule_note}"
    if target is None:
        note = schedule_note
    _CHANGEDETECTION_CACHE = (now_epoch, target, note)
    return target, note


def interval_next_run_time() -> datetime:
    now = datetime.now()
    started = last_live_run_start()
    if started is not None:
        target = started + timedelta(minutes=INTERVAL_MINUTES)
        # If the expected run is overdue, roll forward in whole intervals so the
        # countdown always points at the next upcoming slot rather than the past.
        while target <= now:
            target += timedelta(minutes=INTERVAL_MINUTES)
        return target
    return clock_bucket_next_run(now)


def next_run_schedule() -> tuple[datetime, str, bool]:
    target, note = changedetection_next_check()
    if target is not None:
        return target, note, True
    started = last_live_run_start()
    basis = "after last run" if started is not None else "on the clock"
    return interval_next_run_time(), f"Every {INTERVAL_MINUTES} min ({basis}; API fallback)", False


def next_run_time() -> datetime:
    """Backward-compatible timestamp-only helper used by older callers/tests."""
    return next_run_schedule()[0]


def countdown_string(target: datetime) -> str:
    total_seconds = max(0, int((target - datetime.now()).total_seconds()))
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    if hours:
        return f"{hours}h {minutes:02d}m {seconds:02d}s"
    return f"{minutes:02d}m {seconds:02d}s"


def git_branch() -> str:
    """Current git branch of the checkout (or '-' if it cannot be read)."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=BASE_DIR, capture_output=True, text=True, timeout=3,
        )
        return result.stdout.strip() or "-"
    except (OSError, subprocess.SubprocessError):
        return "-"


def finish_stamp_from_folder(record_folder: str) -> str:
    name = os.path.basename((record_folder or "").rstrip("/"))
    if name.startswith("(") and ")" in name:
        name = name[1:name.index(")")]
    match = re.search(r"(\d{4}-\d{2}-\d{2})(?:[ _T]?(\d{2})[_:-](\d{2}))?", name)
    if not match:
        return ""
    return f"{match.group(1)} {match.group(2)}:{match.group(3)}" if match.group(2) and match.group(3) else match.group(1)


def finish_stamp_from_detail_json(detail_json_path: str) -> str:
    if not detail_json_path:
        return ""
    try:
        data = json.loads(Path(detail_json_path).read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return ""
    calendar = data.get("calendar") if isinstance(data.get("calendar"), dict) else {}
    summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
    return str(calendar.get("dtend") or data.get("finish_date_guess") or data.get("date_end_opportunity") or summary.get("date_end_opportunity") or "")


def queue_text() -> str:
    collector = "pending" if REQUEST_FLAG.exists() else "none"
    if UPDATE_IN_PROGRESS_FLAG.exists():
        update = "running"
    elif UPDATE_QUEUE_FLAG.exists():
        update = "pending"
    else:
        update = "none"
    return f"Queue: collector {collector} · update {update}"

def archive_snapshot(limit: int = RECORDS_SHOWN) -> tuple[int, dict[str, int], list[tuple[str, str, str, str, str]]]:
    """Return archive totals and latest records with end date/status."""
    empty_counts = {"saved": 0, "pending": 0, "failed": 0}
    if not ARCHIVE_DB.exists():
        return 0, empty_counts, []
    try:
        conn = sqlite3.connect(f"file:{ARCHIVE_DB}?mode=ro", uri=True, timeout=2)
    except sqlite3.Error:
        return 0, empty_counts, []
    try:
        conn.row_factory = sqlite3.Row
        total = conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]
        counts = {
            "saved": conn.execute("SELECT COUNT(*) FROM opportunities WHERE detail_status = 'saved'").fetchone()[0],
            "pending": conn.execute("SELECT COUNT(*) FROM opportunities WHERE detail_status = 'pending'").fetchone()[0],
            "failed": conn.execute("SELECT COUNT(*) FROM opportunities WHERE detail_status = 'failed'").fetchone()[0],
        }
        rows = conn.execute(
            "SELECT numero, first_seen, finish_date_guess, record_folder, detail_json_path, detail_status, "
            "COALESCE(NULLIF(short_description, ''), descripcion, '') AS d "
            "FROM opportunities ORDER BY first_seen DESC, numero DESC LIMIT ?",
            (limit,),
        ).fetchall()
    except sqlite3.Error:
        return 0, empty_counts, []
    finally:
        conn.close()
    latest = []
    for r in rows:
        end = str(r["finish_date_guess"] or "") or finish_stamp_from_folder(str(r["record_folder"] or "")) or finish_stamp_from_detail_json(str(r["detail_json_path"] or "")) or "no end"
        latest.append((_short_date(r["first_seen"]), str(r["numero"] or ""), end[:16].replace("_", " "), str(r["detail_status"] or ""), str(r["d"] or "")))
    return total, counts, latest


def _short_date(first_seen: str) -> str:
    """'YYYY-MM-DD' from an ISO ``first_seen`` value, or '----------' when unknown."""
    text = (first_seen or "").strip()
    if not text:
        return "----------"
    datepart = text[:10]  # leading YYYY-MM-DD of an ISO timestamp or bare date
    try:
        return datetime.strptime(datepart, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        return datepart


def _truncate(text: str, width: int) -> str:
    text = text or ""
    return (text[: width - 1] + "…") if len(text) > width else text


def main() -> int:
    root = tk.Tk()
    root.title("Next Live Run")
    root.configure(bg="#1e293b")
    root.resizable(False, False)
    root.attributes("-topmost", WINDOW_TOPMOST)
    transparency_var = tk.StringVar(value="Transparency: checking…")

    def apply_window_alpha() -> None:
        if WINDOW_ALPHA >= 1.0:
            transparency_var.set("Transparency: disabled (alpha=1.00)")
            return
        try:
            root.attributes("-alpha", WINDOW_ALPHA)
            root.update_idletasks()
            actual = float(root.attributes("-alpha"))
        except (tk.TclError, ValueError):
            transparency_var.set("Transparency unavailable: enable an X11 compositor or set PC_NEXT_RUN_TIMER_ALPHA=1")
            return
        if abs(actual - WINDOW_ALPHA) <= 0.03:
            transparency_var.set(f"Transparency active: alpha={actual:.2f}")
        else:
            transparency_var.set(f"Transparency may be ignored by this desktop (requested {WINDOW_ALPHA:.2f}, got {actual:.2f})")

    apply_window_alpha()

    root.update_idletasks()
    x = max(0, (root.winfo_screenwidth() - WINDOW_WIDTH) // 2)
    # Pin the window to a fixed size and position so it never grows with content.
    root.geometry(f"{WINDOW_WIDTH}x{WINDOW_HEIGHT}+{x}+{WINDOW_TOP}")
    root.after(300, apply_window_alpha)

    timer_title = "Next changedetection check" if AUTORUN_SOURCE == "changedetection" else "Next live run"
    tk.Label(root, text=timer_title, font=("Sans", 12, "bold"), bg="#1e293b", fg="#fbbf24").pack(pady=(10, 1))
    tk.Label(root, textvariable=transparency_var, font=("Sans", 8), bg="#1e293b", fg="#93c5fd", wraplength=WINDOW_WIDTH - 24).pack(pady=(0, 1))
    next_var = tk.StringVar(value="Loading...")
    tk.Label(root, textvariable=next_var, font=("Sans", 11), bg="#1e293b", fg="#e5e7eb").pack(pady=1)
    count_var = tk.StringVar(value="")
    count_label = tk.Label(root, textvariable=count_var, font=("Mono", 18, "bold"), bg="#1e293b", fg="#22c55e")
    count_label.pack(pady=2)

    tk.Frame(root, bg="#334155", height=1).pack(fill="x", padx=14, pady=(4, 4))

    branch_var = tk.StringVar(value="Branch: …")
    tk.Label(root, textvariable=branch_var, font=("Sans", 9, "bold"), bg="#1e293b", fg="#93c5fd").pack(pady=0)
    summary_var = tk.StringVar(value="")
    tk.Label(root, textvariable=summary_var, font=("Sans", 9), bg="#1e293b", fg="#e5e7eb").pack(pady=0)
    lastrun_var = tk.StringVar(value="")
    tk.Label(root, textvariable=lastrun_var, font=("Sans", 8), bg="#1e293b", fg="#94a3b8").pack(pady=0)
    duration_var = tk.StringVar(value="Prev duration: —")
    tk.Label(root, textvariable=duration_var, font=("Sans", 8), bg="#1e293b", fg="#cbd5e1", wraplength=WINDOW_WIDTH - 24).pack(pady=0)

    tk.Label(root, text="Latest records", font=("Sans", 8, "bold"), bg="#1e293b", fg="#fbbf24").pack(pady=(4, 0))

    status_var = tk.StringVar(value="")
    tk.Label(
        root, textvariable=status_var, font=("Sans", 8), bg="#1e293b", fg="#94a3b8",
        wraplength=WINDOW_WIDTH - 24, justify="center",
    ).pack(side="bottom", pady=(0, 6))

    # The latest-record list lives in a scrollable, read-only Text so the window
    # can stay fixed-size yet show many recent entries — the operator scrolls the
    # field (wheel or scrollbar) to walk through the newest collected records.
    latest_frame = tk.Frame(root, bg="#1e293b")
    latest_frame.pack(fill="both", expand=True, padx=12, pady=(0, 2))
    latest_scroll = tk.Scrollbar(latest_frame, orient="vertical")
    latest_scroll.pack(side="right", fill="y")
    latest_text = tk.Text(
        latest_frame, font=("Mono", 8), bg="#1e293b", fg="#bbf7d0",
        bd=0, highlightthickness=0, wrap="none", cursor="arrow",
        yscrollcommand=latest_scroll.set,
    )
    latest_text.pack(side="left", fill="both", expand=True)
    latest_scroll.config(command=latest_text.yview)
    latest_text.insert("1.0", "…")
    latest_text.configure(state="disabled")

    def set_latest(text: str) -> None:
        latest_text.configure(state="normal")
        latest_text.delete("1.0", "end")
        latest_text.insert("1.0", text)
        latest_text.configure(state="disabled")

    def on_latest_wheel(event: tk.Event) -> str:
        # X11 delivers wheel as Button-4/5 (no delta); other platforms use delta.
        if getattr(event, "num", None) == 4:
            step = -1
        elif getattr(event, "num", None) == 5:
            step = 1
        else:
            step = -1 if event.delta > 0 else 1
        latest_text.yview_scroll(step, "units")
        return "break"

    latest_text.bind("<MouseWheel>", on_latest_wheel)
    latest_text.bind("<Button-4>", on_latest_wheel)
    latest_text.bind("<Button-5>", on_latest_wheel)

    state = {"was_active": False, "tick": 0, "branch": "-"}

    def refresh_data() -> None:
        state["branch"] = git_branch()
        branch_var.set(f"Branch: {_truncate(state['branch'], 38)}")
        values = progress_values()
        total, counts, latest = archive_snapshot()
        new = values.get("RECORDS_NEW", "-")
        saved = values.get("RECORDS_SAVED", "-")
        summary_var.set(f"New: {new} · Saved this run: {saved} · Archive: {total}")
        last_start = values.get("STARTED_AT", "") or "—"
        last_status = values.get("STATUS", "") or "—"
        lastrun_var.set(f"Last run: {last_start} · {last_status} · DB saved/pending/failed: {counts['saved']}/{counts['pending']}/{counts['failed']}")
        duration_var.set(_truncate(duration_parts_text(last_summary_values()) + " · " + queue_text(), 110))
        if latest:
            # Newest first: each entry leads with its download date (YY-MM-DD) so
            # the list reads latest -> oldest at a glance.
            set_latest("\n".join(
                f"{date}  {num}  end {end}  {status}\n        {_truncate(desc, 34) or '(sin descripción)'}"
                for date, num, end, status, desc in latest
            ))
        else:
            set_latest("(sin registros todavía)")

    def refresh() -> None:
        active = is_live_run_active()
        if active:
            state["was_active"] = True
            if root.state() != "withdrawn":
                root.withdraw()
        else:
            if root.state() == "withdrawn":
                root.deiconify()
                root.lift()
            next_dt, schedule_note, changedetection_synced = next_run_schedule()
            remaining = int((next_dt - datetime.now()).total_seconds())
            next_format = "%H:%M:%S" if next_dt.date() == datetime.now().date() else "%a %Y-%m-%d %H:%M:%S"
            next_var.set(next_dt.strftime(next_format))
            count_var.set(countdown_string(next_dt))
            # Turn the countdown amber when the next run is imminent (< 60s).
            count_label.configure(fg="#f59e0b" if remaining <= 60 else "#22c55e")
            overdue = " · check overdue/queued" if changedetection_synced and remaining <= 0 else ""
            status_var.set(schedule_note + overdue + (" · run finished" if state["was_active"] else ""))
            # Refresh the heavier branch/DB data periodically (and right after a run).
            if state["tick"] % DATA_REFRESH_TICKS == 0 or state["was_active"]:
                refresh_data()
            state["was_active"] = False
        state["tick"] += 1
        root.after(1000, refresh)

    refresh_data()
    refresh()
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
