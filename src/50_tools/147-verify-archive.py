#!/usr/bin/env python3
"""Read-only, resumable HP15 verification of DB rows against record archives."""
import argparse
import datetime as dt
import json
import os
import sqlite3
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import DB_PATH, RECORDS_DIR, QUEUE_DIR, now_iso, safe_name, archive_complete

QUEUE_PATH = QUEUE_DIR / "task_queue.json"
REPORT_DIR = QUEUE_DIR.parent / "verification"
LATEST = REPORT_DIR / "archive_snapshot_records_latest.json"

def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)

def load_tasks():
    try:
        return json.loads(QUEUE_PATH.read_text(encoding="utf-8")) if QUEUE_PATH.exists() else []
    except (OSError, ValueError):
        return []

def save_task(task):
    tasks = load_tasks()
    found = False
    for i, old in enumerate(tasks):
        if old.get("id") == task["id"]:
            tasks[i] = task
            found = True
            break
    if not found:
        tasks.append(task)
    atomic_json(QUEUE_PATH, tasks)

def parse_day(value):
    return dt.date.fromisoformat(value)

def folder_day(name):
    try:
        yy, mm, dd = name.split("-", 2)
        return dt.date(2000 + int(yy), int(mm), int(dd))
    except (ValueError, AttributeError):
        return None

def folder_for_row(row):
    raw = row["record_folder"] or ""
    marker = "/records/"
    if marker in raw:
        rel = raw.split(marker, 1)[1]
        candidate = RECORDS_DIR / rel
        if candidate.exists():
            return candidate
    date_folder = row["date_folder"] or ""
    direct = RECORDS_DIR / date_folder / safe_name(row["numero"])
    if direct.exists():
        return direct
    return None

def index_path(folder, numero):
    if not folder:
        return None
    direct = folder / f"{safe_name(numero)}.json"
    return direct if direct.is_file() else None

def compare_row(row, folder):
    result = {"numero": row["numero"], "date_folder": row["date_folder"], "path": str(folder) if folder else None}
    if not folder:
        result["state"] = "missing_folder"
        return result
    idx = index_path(folder, row["numero"])
    if not idx:
        result["state"] = "missing_index"
        return result
    try:
        data = json.loads(idx.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        result.update(state="invalid_index", error=str(exc))
        return result
    mismatches = {}
    for key in ("numero", "link", "fecha", "descripcion"):
        expected = row[key]
        actual = data.get(key)
        if expected is not None and actual != expected:
            mismatches[key] = {"db": expected, "index": actual}
    result["index"] = str(idx)
    result["mismatches"] = mismatches
    result["detail_complete"] = bool(archive_complete(folder, row["numero"]))
    result["state"] = "ok" if not mismatches and result["detail_complete"] else ("index_mismatch" if mismatches else "detail_incomplete")
    return result

def run(task, start, end, checkpoint=None):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM opportunities WHERE date_folder IS NOT NULL ORDER BY date_folder, numero").fetchall()
    selected = [r for r in rows if (folder_day(r["date_folder"]) or dt.date.min) >= start and (folder_day(r["date_folder"]) or dt.date.max) <= end]
    counts = {"db_rows": len(selected), "checked": 0, "ok": 0, "missing_folder": 0, "missing_index": 0, "invalid_index": 0, "index_mismatch": 0, "detail_incomplete": 0, "orphan_index": 0}
    findings = []
    started = task.get("started_at") or now_iso()
    task.update(status="running", started_at=started, progress={"total": len(selected), "checked": 0, "counts": counts})
    save_task(task)
    resume = checkpoint or ""
    for row in selected:
        key = f"{row['date_folder']}/{row['numero']}"
        if resume and key <= resume:
            continue
        item = compare_row(row, folder_for_row(row))
        counts["checked"] += 1
        counts[item["state"]] = counts.get(item["state"], 0) + 1
        if item["state"] != "ok":
            findings.append(item)
        task["checkpoint"] = key
        task["progress"] = {"total": len(selected), "checked": counts["checked"], "counts": counts}
        if counts["checked"] % 100 == 0:
            save_task(task)
            report = {"status": "running", "task_id": task["id"], "range": {"start": start.isoformat(), "end": end.isoformat()}, "updated_at": now_iso(), "counts": counts, "findings": findings[-1000:]}
            atomic_json(LATEST, report)
    snapshot_candidates = []
    for root in (REPORT_DIR.parent / "snapshots", REPORT_DIR.parent / "changedetection"):
        if root.exists():
            snapshot_candidates.extend(str(p) for p in root.rglob("*") if p.is_file())
    task.update(status="done", finished_at=now_iso(), progress={"total": len(selected), "checked": counts["checked"], "counts": counts})
    report = {"status": "complete", "task_id": task["id"], "range": {"start": start.isoformat(), "end": end.isoformat()}, "started_at": started, "finished_at": task["finished_at"], "counts": counts, "findings": findings[-5000:], "snapshot": {"available": bool(snapshot_candidates), "candidates": snapshot_candidates[:20], "note": "No snapshot comparison performed when ChangeDetection snapshot is unavailable." if not snapshot_candidates else "Snapshot files detected; archive verification completed."}}
    atomic_json(LATEST, report)
    task["report_path"] = str(LATEST)
    task["summary"] = counts
    save_task(task)
    conn.close()

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start-date", default="2025-01-01")
    ap.add_argument("--end-date", default=dt.date.today().isoformat())
    ap.add_argument("--task-id")
    ap.add_argument("--checkpoint")
    args = ap.parse_args()
    start, end = parse_day(args.start_date), parse_day(args.end_date)
    tasks = load_tasks()
    task = next((t for t in tasks if t.get("id") == args.task_id), None) if args.task_id else None
    if task is None:
        task = {"id": uuid.uuid4().hex[:12], "label": f"HP15 - Verify snapshots vs records ({start} -> {end})", "kind": "archive_verification_range", "params": {"start_date": start.isoformat(), "end_date": end.isoformat()}, "wait_condition": "manual", "wait_params": {}, "status": "queued", "created_at": now_iso(), "started_at": None, "finished_at": None}
        save_task(task)
    run(task, start, end, args.checkpoint or task.get("checkpoint"))
    print(json.dumps({"ok": True, "task_id": task["id"], "report": str(LATEST)}, ensure_ascii=False))

if __name__ == "__main__":
    main()
