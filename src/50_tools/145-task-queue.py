#!/usr/bin/env python3
"""Persistent task queue for operations that must wait on a DB-state
condition (not just "the active lock is free") before starting -- e.g. a
historical Closed backfill for a specific date range that should only begin
once the current pending-detail backlog has fully drained, so it never
competes with that drain for the same shared detail/cotizacion queue.

Distinct from 125-run-priority.sh's queue: that one drains a job the moment
the active lock releases (seconds to minutes); this one can wait hours to
days for a DB condition, checked once per tick rather than held open.

Ticked from 039-run-closed-backfill.sh (already on its own 20-minute
systemd --user timer) right after it confirms priority 1/2 are free and
acquires its own lock, so activating a task can never race an in-flight
backfill segment. No new timer or lock file needed.

Task shape (JSON list at var/data/queue/task_queue.json):
  id, label, kind, params, wait_condition, wait_params, status
  (queued|running|done|cancelled), created_at, started_at, finished_at

Supported kind: "closed_backfill_range" -- params {start_date, end_date}
(YYYY-MM-DD). Activating writes PC_CLOSED_BACKFILL_START_DATE/END_DATE into
monitor_settings.env (same file/format the monitor's own Save button uses)
and resets closed_crawl_state (backfill_page=1, backfill_complete=0), then
the existing 20-minute timer's normal 037-collect-closed-index.py call picks
up the new range on its very next tick. Completion is
closed_crawl_state.backfill_complete == 1, checked on a later tick.

Supported wait_condition: "closed_pending_below" -- wait_params {threshold}
(default 0): COUNT(*) FROM opportunities WHERE grupo='Closed' AND
detail_status='pending' must be <= threshold.
"""
import argparse
import json
import shlex
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import *

QUEUE_PATH = QUEUE_DIR / "task_queue.json"
MONITOR_SETTINGS_PATH = DATA_CONFIG_DIR / "monitor_settings.env"


def load_queue() -> list:
    if not QUEUE_PATH.exists():
        return []
    try:
        return json.loads(QUEUE_PATH.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return []


def save_queue(tasks: list) -> None:
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = QUEUE_PATH.with_suffix(QUEUE_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(tasks, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(QUEUE_PATH)


def add_task(label, kind, params, wait_condition, wait_params) -> dict:
    tasks = load_queue()
    task = {
        "id": uuid.uuid4().hex[:12],
        "label": label,
        "kind": kind,
        "params": params,
        "wait_condition": wait_condition,
        "wait_params": wait_params,
        "status": "queued",
        "created_at": now_iso(),
        "started_at": None,
        "finished_at": None,
    }
    tasks.append(task)
    save_queue(tasks)
    return task


def remove_task(task_id: str) -> bool:
    tasks = load_queue()
    remaining = [t for t in tasks if t["id"] != task_id]
    if len(remaining) == len(tasks):
        return False
    save_queue(remaining)
    return True


def _apply_monitor_setting(key: str, value: str) -> None:
    """Same KEY='value' shlex-quoted line format save_monitor_setting() in
    001b-monitor-web.py writes, so either side can edit the file without the
    other misparsing it."""
    settings = {}
    if MONITOR_SETTINGS_PATH.exists():
        for line in MONITOR_SETTINGS_PATH.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line or line.lstrip().startswith("#") or "=" not in line:
                continue
            k, raw = line.split("=", 1)
            k = k.strip()
            if not k:
                continue
            try:
                parsed = shlex.split(raw, posix=True)
                settings[k] = parsed[0] if parsed else ""
            except ValueError:
                settings[k] = raw.strip().strip("'").strip('"')
    settings[key] = value
    MONITOR_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    MONITOR_SETTINGS_PATH.write_text(
        "# PanamaCompra monitor settings (KEY=VALUE).\n"
        "# Edited from the web/native monitor; same-named environment variables override these at startup.\n"
        + "\n".join(f"{name}={shlex.quote(settings[name])}" for name in sorted(settings))
        + "\n",
        encoding="utf-8",
    )


def check_wait_condition(conn, wait_condition: str, wait_params: dict) -> bool:
    if wait_condition == "closed_pending_below":
        threshold = int(wait_params.get("threshold", 0))
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM opportunities WHERE grupo='Closed' AND detail_status='pending'"
        ).fetchone()["c"]
        return count <= threshold
    # Unknown condition: never auto-satisfy, so a typo in wait_condition
    # can't silently skip straight to running.
    return False


def activate_task(conn, task: dict) -> None:
    if task["kind"] == "closed_backfill_range":
        params = task["params"]
        _apply_monitor_setting("PC_CLOSED_BACKFILL_START_DATE", params.get("start_date", ""))
        _apply_monitor_setting("PC_CLOSED_BACKFILL_END_DATE", params.get("end_date", ""))
        update_closed_crawl_state(conn, backfill_page=1, backfill_complete=0)
    task["status"] = "running"
    task["started_at"] = now_iso()


def check_completion(conn, task: dict) -> bool:
    if task["kind"] == "closed_backfill_range":
        state = get_closed_crawl_state(conn)
        return bool(state["backfill_complete"])
    return False


def tick() -> dict:
    """One dispatcher pass: advance a running task to done if its
    completion condition is met, else -- if nothing is running -- activate
    the first queued task whose wait condition is satisfied. Only the head
    of the queue is ever considered (strict FIFO), so a later task can never
    jump ahead just because its own condition happens to be met sooner.
    Never raises on a bad/unknown task so one malformed entry can't wedge
    every future tick."""
    conn = init_db()
    tasks = load_queue()

    running = [t for t in tasks if t["status"] == "running"]
    if running:
        task = running[0]
        try:
            if check_completion(conn, task):
                task["status"] = "done"
                task["finished_at"] = now_iso()
                save_queue(tasks)
                return {"action": "completed", "task": task["label"]}
        except Exception as exc:  # noqa: BLE001
            return {"action": "completion_check_failed", "task": task["label"], "error": str(exc)}
        return {"action": "running", "task": task["label"]}

    for task in tasks:
        if task["status"] != "queued":
            continue
        try:
            if check_wait_condition(conn, task["wait_condition"], task.get("wait_params") or {}):
                activate_task(conn, task)
                save_queue(tasks)
                return {"action": "activated", "task": task["label"]}
        except Exception as exc:  # noqa: BLE001
            return {"action": "wait_check_failed", "task": task["label"], "error": str(exc)}
        return {"action": "waiting", "task": task["label"]}

    return {"action": "none"}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Persistent task queue for conditional Closed-backfill work")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("tick", help="Run one dispatcher pass")
    sub.add_parser("list", help="List all queued/running/done tasks")

    p_add = sub.add_parser("add", help="Add a closed_backfill_range task")
    p_add.add_argument("--label", required=True)
    p_add.add_argument("--start-date", required=True, help="YYYY-MM-DD")
    p_add.add_argument("--end-date", required=True, help="YYYY-MM-DD")
    p_add.add_argument("--wait-condition", default="closed_pending_below")
    p_add.add_argument("--threshold", type=int, default=0)

    p_rm = sub.add_parser("remove", help="Remove a task by id")
    p_rm.add_argument("task_id")

    args = parser.parse_args(argv)

    if args.cmd == "tick":
        print(json.dumps(tick(), ensure_ascii=False))
    elif args.cmd == "list":
        for t in load_queue():
            print(f"{t['id']}  [{t['status']:9}]  {t['label']}  ({t['kind']}: {t['params']})")
    elif args.cmd == "add":
        task = add_task(
            label=args.label,
            kind="closed_backfill_range",
            params={"start_date": args.start_date, "end_date": args.end_date},
            wait_condition=args.wait_condition,
            wait_params={"threshold": args.threshold},
        )
        print(json.dumps({"ok": True, "task": task}, ensure_ascii=False))
    elif args.cmd == "remove":
        removed = remove_task(args.task_id)
        print(json.dumps({"ok": removed}, ensure_ascii=False))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
