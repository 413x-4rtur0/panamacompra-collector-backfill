#!/usr/bin/env python3
"""Import the PanamaCompra index from a changedetection.io snapshot.

changedetection already crawls the Cotizaciones table with our own browser-steps
script (config/changedetection-browser-steps.js) and stores the rendered report
as a Brotli-compressed snapshot in its datastore. That report already contains
every field the index step needs — NUMERO, ESTADO, description, entity, and the
opaque detail LINK token that cannot be rebuilt from the NUMERO alone.

This importer parses the most recent snapshot and feeds each record through the
SAME database path the browser crawler uses (``insert_or_update_index``), so the
NUMERO-primary-key dedup, the Programada→Abierta transition flag, and the immutable
index JSON all behave identically — only without opening a second Firefox to
re-crawl what changedetection already saw.

It NEVER deletes records: a record missing from a (possibly partial) snapshot is
simply not touched, exactly as in the crawler. Per-group health is reported so the
worker can decide whether to still browser-crawl a group the snapshot failed on
(the uploaded reference snapshot, for example, has Abiertas failing with "rows not
ready"). Selection, source and freshness are configurable:

  PC_INDEX_SNAPSHOT_FILE        explicit snapshot path (``.txt`` or ``.txt.br``),
                                 skips datastore discovery (used by manual imports/tests)
  PC_CHANGEDETECTION_DATASTORE  datastore root (default $PC_INTEGRATIONS_DIR/changedetection)
  PC_INDEX_SNAPSHOT_MAX_AGE_SECONDS  refuse a snapshot older than this (0 = no limit)

Exit codes let the worker branch:
  0  imported, and every expected group was healthy in the snapshot
  3  imported, but at least one group was unhealthy/partial -> crawl the rest
  4  no usable snapshot found (or too old) -> fall back to the browser crawler

A machine-readable result is also written to data/queue/index_snapshot_result.env
(SNAPSHOT_STATUS, SNAPSHOT_UNHEALTHY_GROUPS, counters) so the run-all worker can
decide which groups still need the browser crawler without parsing stdout.
"""
import gzip
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import *

SNAPSHOT_MARKER = "PANAMACOMPRA_MONITOR"
# ESTADO value (from the snapshot) -> portal group, so the same
# insert_or_update_index transition logic (Programadas -> Abiertas) applies.
ESTADO_TO_GROUP = (("programad", "Programadas"), ("abiert", "Abiertas"))
EXPECTED_GROUPS = ("Programadas", "Abiertas")


def datastore_dir():
    integrations = env_path("PC_INTEGRATIONS_DIR", STATE_DIR / "integrations")
    return env_path("PC_CHANGEDETECTION_DATASTORE", integrations / "changedetection")


def _read_datastore_bytes(path):
    """Read a datastore file, falling back to changedetection's container."""
    path = Path(path)
    try:
        return path.read_bytes(), ""
    except PermissionError as host_error:
        try:
            relative = path.resolve().relative_to(Path(datastore_dir()).resolve())
        except ValueError:
            return b"", f"cannot read {path}: {host_error}"
        try:
            done = subprocess.run(
                ["docker", "compose", "exec", "-T", "changedetection", "cat",
                 f"/datastore/{relative.as_posix()}"],
                cwd=APP_ROOT, capture_output=True, timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return b"", f"cannot read {path} on host or through changedetection: {exc}"
        if done.returncode == 0:
            return done.stdout, ""
        detail = done.stderr.decode("utf-8", "replace").strip()
        return b"", f"cannot read {path}; container fallback exit {done.returncode}: {detail}"
    except OSError as exc:
        return b"", f"cannot read {path}: {exc}"


def _decompress_bytes(raw, path):
    """Return snapshot text from raw file bytes, decompressing when needed.

    changedetection compresses history snapshots with Brotli (``.txt.br``). Brotli
    is not in the Python stdlib, so try, in order: the ``brotli`` module, the
    ``brotli`` CLI, then gzip / plain text for uncompressed datastores. Returns
    ('' , reason) on failure instead of raising, so a missing codec degrades to a
    browser-crawl fallback rather than crashing the run."""
    name = str(path).lower()
    if name.endswith(".br"):
        try:
            import brotli  # type: ignore
            return brotli.decompress(raw).decode("utf-8", "replace"), ""
        except ModuleNotFoundError:
            pass
        except Exception as exc:  # noqa: BLE001 - corrupt/partial snapshot
            return "", f"brotli module failed: {exc}"
        try:
            done = subprocess.run(
                ["brotli", "--decompress", "--stdout"],
                input=raw, capture_output=True, timeout=30,
            )
            if done.returncode == 0:
                return done.stdout.decode("utf-8", "replace"), ""
            return "", f"brotli CLI exit {done.returncode}"
        except FileNotFoundError:
            try:
                done = subprocess.run(
                    ["docker", "compose", "exec", "-T", "changedetection", "python", "-c",
                     "import sys,brotli;sys.stdout.buffer.write(brotli.decompress(sys.stdin.buffer.read()))"],
                    cwd=APP_ROOT, input=raw, capture_output=True, timeout=30,
                )
                if done.returncode == 0:
                    return done.stdout.decode("utf-8", "replace"), ""
                detail = done.stderr.decode("utf-8", "replace").strip()
                return "", f"changedetection brotli fallback exit {done.returncode}: {detail}"
            except (OSError, subprocess.SubprocessError) as exc:
                return "", f"cannot decompress .br snapshot with host or container: {exc}"
        except Exception as exc:  # noqa: BLE001
            return "", f"brotli CLI failed: {exc}"
    if name.endswith(".gz"):
        try:
            return gzip.decompress(raw).decode("utf-8", "replace"), ""
        except Exception as exc:  # noqa: BLE001
            return "", f"gzip failed: {exc}"
    return raw.decode("utf-8", "replace"), ""


def read_snapshot_text(path):
    raw, reason = _read_datastore_bytes(path)
    if reason:
        return "", reason
    return _decompress_bytes(raw, path)


def _latest_history_entry(watch_dir):
    """Newest snapshot filename for a watch, from its history.txt index.

    changedetection appends 'epoch,filepath' per detected change; the last line is
    the most recent. Falls back to the newest *.txt.br / *.txt by mtime when the
    index is absent."""
    history = watch_dir / "history.txt"
    if history.exists():
        raw, reason = _read_datastore_bytes(history)
        lines = [ln for ln in raw.decode("utf-8", "replace").splitlines() if ln.strip()] if not reason else []
        if lines:
            field = lines[-1].split(",")[-1].strip()
            candidate = Path(field)
            if not candidate.is_absolute():
                candidate = watch_dir / candidate.name
            if candidate.exists():
                return candidate
    snapshots = sorted(
        list(watch_dir.glob("*.txt.br")) + list(watch_dir.glob("*.txt")),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return snapshots[0] if snapshots else None


def discover_latest_snapshot(datastore):
    """Find the newest PanamaCompra snapshot across every watch folder.

    Returns (path, text, ''). Prefers, among watches whose latest snapshot carries
    our PANAMACOMPRA_MONITOR marker, the one with the newest file mtime. Returns
    ('', '', reason) when none is usable."""
    root = Path(datastore)
    if not root.is_dir():
        return None, "", f"datastore not found: {root}"
    best = None  # (mtime, path, text)
    reasons = []
    for watch_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        latest = _latest_history_entry(watch_dir)
        if latest is None:
            continue
        text, reason = read_snapshot_text(latest)
        if not text:
            if reason:
                reasons.append(f"{watch_dir.name}: {reason}")
            continue
        if SNAPSHOT_MARKER not in text[:200]:
            continue  # a different watch (not our PanamaCompra monitor)
        mtime = latest.stat().st_mtime
        if best is None or mtime > best[0]:
            best = (mtime, latest, text)
    if best is None:
        return None, "", "; ".join(reasons) or f"no {SNAPSHOT_MARKER} snapshot in {root}"
    return best[1], best[2], ""


def _group_for_estado(estado):
    norm = strip_accents(estado or "").lower()
    for needle, group in ESTADO_TO_GROUP:
        if norm.startswith(needle):
            return group
    return estado or ""


def parse_snapshot(text):
    """Parse the browser-steps report into {marker_ok, groups, records}.

    ``groups`` maps each portal group to {'collected', 'healthy', 'reason'} derived
    from the PAGE_COUNTS section, so a group that failed inside changedetection
    ('rows not ready...', 'radio switch failed') is reported unhealthy. ``records``
    is a list of field dicts with a ``grupo`` derived from ESTADO."""
    lines = text.splitlines()
    marker_ok = bool(lines) and lines[0].strip() == SNAPSHOT_MARKER

    groups = {g: {"collected": 0, "healthy": False, "reason": "not present in snapshot", "recovery_page": 0} for g in EXPECTED_GROUPS}
    page_seen = {g: False for g in EXPECTED_GROUPS}
    pagination_consistent = {g: None for g in EXPECTED_GROUPS}
    section = "header"
    records = []
    current = {}

    def flush():
        if current.get("numero") and current.get("link"):
            current["grupo"] = _group_for_estado(current.get("estado", ""))
            records.append(dict(current))

    for raw in lines:
        line = raw.rstrip("\n")
        stripped = line.strip()
        if stripped == "PAGE_COUNTS":
            section = "pages"
            continue
        if stripped == "RECORDS":
            section = "records"
            continue

        if section == "header":
            for group in EXPECTED_GROUPS:
                if stripped.startswith(f"{group} collected:"):
                    try:
                        groups[group]["collected"] = int(stripped.split(":", 1)[1].strip())
                    except ValueError:
                        pass
        elif section == "pages":
            for group in EXPECTED_GROUPS:
                if stripped.startswith(f"{group} PAGINATION:"):
                    # Machine-readable health marker from the browser script:
                    # "... first_bad_page=N consistent=yes|no". consistent=no
                    # overrides a COMPLETE line (short crawls still paginate to
                    # a disabled Next); first_bad_page tells the crawler where
                    # to resume so it does not redo the healthy leading pages.
                    tokens = dict(re.findall(r"(\w+)=(\S+)", stripped.split(":", 1)[1]))
                    pagination_consistent[group] = tokens.get("consistent") == "yes"
                    try:
                        groups[group]["recovery_page"] = max(0, int(tokens.get("first_bad_page", "0")))
                    except ValueError:
                        pass
                    continue
                if stripped.startswith(f"{group} page "):
                    page_seen[group] = True
                    continue
                elif stripped.startswith(f"{group} COMPLETE:"):
                    groups[group]["healthy"] = True
                    groups[group]["reason"] = ""
                elif stripped.startswith(f"{group}:"):
                    # A failure line, e.g. "Abiertas: rows not ready after 50 change".
                    groups[group]["healthy"] = False
                    groups[group]["reason"] = stripped.split(":", 1)[1].strip()
        elif section == "records":
            if stripped == "---":
                flush()
                current = {}
                continue
            if ":" not in line:
                continue
            label, value = line.split(":", 1)
            key = {
                "NUMERO": "numero", "ESTADO": "estado", "DESCRIPCION": "descripcion",
                "ENTIDAD": "entidad", "DEPENDENCIA": "dependencia", "FECHA": "fecha",
                "MODALIDAD": "modalidad", "LINK": "link",
            }.get(label.strip())
            if key:
                current[key] = value.strip()
    flush()  # last block may not be followed by '---'

    parsed_counts = {group: 0 for group in EXPECTED_GROUPS}
    for record in records:
        group = record.get("grupo", "")
        if group in parsed_counts:
            parsed_counts[group] += 1

    for group, info in groups.items():
        if not info["healthy"] and page_seen[group] and info["reason"] == "not present in snapshot":
            info["reason"] = "missing explicit pagination completion marker"
        if info["healthy"] and parsed_counts[group] < info["collected"]:
            info["healthy"] = False
            info["reason"] = f"parsed {parsed_counts[group]} of {info['collected']} records"
        if pagination_consistent[group] is False:
            # consistent=no wins over COMPLETE: the crawl reached a disabled
            # Next, but pages were short/skipped along the way.
            info["healthy"] = False
            info["reason"] = info["reason"] or f"pagination inconsistent (first bad page {info['recovery_page'] or 1})"
    return {"marker_ok": marker_ok, "groups": groups, "records": records}


def import_records(conn, records):
    """Insert/refresh each snapshot record through the shared index DB path.

    Mirrors 010-collect-index.py's per-row handling exactly (same helpers, same
    immutable index JSON, same new/existing accounting) so dedup and status
    transitions are identical whether the row came from the crawler or a snapshot."""
    seen = set()
    new_records = 0
    existing_records = 0
    json_written = 0
    skipped_no_link = 0

    for r in records:
        numero = r.get("numero")
        link = r.get("link")
        if not numero or not link:
            skipped_no_link += 1
            continue
        if numero in seen:
            continue
        seen.add(numero)

        existing = find_existing_opportunity(conn, numero)
        existing_on_disk = False
        if existing:
            date_folder = existing["date_folder"]
            record_folder = Path(existing["record_folder"])
            index_json_path = Path(existing["index_json_path"])
        else:
            disk_folder, disk_index_json = find_existing_record_archive(numero)
            if disk_folder and disk_index_json:
                existing_on_disk = True
                date_folder = disk_folder.parent.name
                record_folder = disk_folder
                index_json_path = disk_index_json
            else:
                date_folder = date_folder_from_fecha(r.get("fecha", "")) or date_folder_name()
                record_folder = get_record_folder(date_folder, numero)
                index_json_path = archive_index_json_path(record_folder, numero)

        record_folder.mkdir(parents=True, exist_ok=True)
        row = {
            "numero": numero,
            "grupo": r.get("grupo") or _group_for_estado(r.get("estado", "")),
            "tipo_url": detect_url_type(link),
            "estado": r.get("estado", ""),
            "descripcion": r.get("descripcion", ""),
            "short_description": short_description(r.get("descripcion", "")),
            "entidad": r.get("entidad", ""),
            "dependencia": r.get("dependencia", ""),
            "fecha": r.get("fecha", ""),
            "modalidad": r.get("modalidad", ""),
            "link": link,
            "first_seen": existing["first_seen"] if existing else now_iso(),
            "last_seen": now_iso(),
            "date_folder": date_folder,
            "record_folder": str(record_folder),
            "index_json_path": str(index_json_path),
            "detail_status": (
                existing["detail_status"] if existing
                else "saved" if existing_on_disk and archive_complete(record_folder, numero)
                else "pending"
            ),
            "finish_date_guess": existing["finish_date_guess"] if existing else "",
            "source_page_first_seen_or_last_seen": "changedetection-snapshot",
            "visual_row_first_seen_or_last_seen": "",
        }
        result = insert_or_update_index(conn, row)
        if result == "new" and not existing_on_disk:
            new_records += 1
        else:
            existing_records += 1
        if write_json_once(index_json_path, row):
            json_written += 1

    return {
        "unique": len(seen),
        "new": new_records,
        "existing": existing_records,
        "json_written": json_written,
        "skipped_no_link": skipped_no_link,
    }


def _max_age_seconds():
    return env_int("PC_INDEX_SNAPSHOT_MAX_AGE_SECONDS", "0", minimum=0)


RESULT_ENV_PATH = QUEUE_DIR / "index_snapshot_result.env"


def write_result_env(status, groups=None, stats=None, snapshot_path=None, reason=""):
    """Publish the import outcome for the run-all worker.

    ``SNAPSHOT_UNHEALTHY_GROUPS`` names the groups the browser crawler must still
    cover (comma-separated). Written atomically on every exit path so a stale
    result from a previous run can never steer the current one."""
    ensure_dirs()
    unhealthy = ",".join(g for g, info in (groups or {}).items() if not info["healthy"])
    # "Group:page" pairs for unhealthy groups whose leading pages were imported
    # fine — the crawler starts there (PC_INDEX_START_PAGES) instead of page 1.
    recovery = ",".join(
        f"{g}:{info['recovery_page']}"
        for g, info in (groups or {}).items()
        if not info["healthy"] and info.get("recovery_page", 0) > 0
    )
    stats = stats or {}
    fields = {
        "SNAPSHOT_STATUS": status,
        "SNAPSHOT_UNHEALTHY_GROUPS": unhealthy,
        "SNAPSHOT_RECOVERY_PAGES": recovery,
        "SNAPSHOT_PATH": str(snapshot_path or ""),
        "SNAPSHOT_REASON": reason,
        "SNAPSHOT_NEW": stats.get("new", 0),
        "SNAPSHOT_EXISTING": stats.get("existing", 0),
        "SNAPSHOT_UNIQUE": stats.get("unique", 0),
        "SNAPSHOT_WRITTEN_AT": now_iso(),
    }
    tmp = RESULT_ENV_PATH.with_suffix(RESULT_ENV_PATH.suffix + ".tmp")
    tmp.write_text("".join(f"{key}={shell_quote(value)}\n" for key, value in fields.items()), encoding="utf-8")
    tmp.replace(RESULT_ENV_PATH)


def resolve_snapshot():
    """Return (path, text, reason). Explicit PC_INDEX_SNAPSHOT_FILE wins; else the
    newest marked snapshot in the datastore."""
    explicit = os.environ.get("PC_INDEX_SNAPSHOT_FILE", "").strip()
    if explicit:
        path = Path(explicit).expanduser()
        text, reason = read_snapshot_text(path)
        return (path if text else None), text, reason
    return discover_latest_snapshot(datastore_dir())


def main(argv=None):
    import argparse
    import time

    parser = argparse.ArgumentParser(description="Import the PanamaCompra index from a changedetection snapshot")
    parser.add_argument("--dry-run", action="store_true", help="parse and report, but do not touch the database")
    args = parser.parse_args(argv)

    path, text, reason = resolve_snapshot()
    if not text:
        msg = f"No usable index snapshot: {reason}"
        print(msg)
        write_result_env("unavailable", reason=reason)
        write_run_progress("INDEX", "DONE", 5, f"Snapshot import skipped: {reason}",
                           step_current=1, step_total=7, extra="snapshot=unavailable")
        return 4

    max_age = _max_age_seconds()
    if path is not None and max_age and Path(path).exists():
        age = time.time() - Path(path).stat().st_mtime
        if age > max_age:
            print(f"Snapshot {path} is {int(age)}s old (> {max_age}s); falling back to crawler.")
            write_result_env("unavailable", snapshot_path=path, reason=f"stale: {int(age)}s > {max_age}s")
            return 4

    parsed = parse_snapshot(text)
    if not parsed["marker_ok"]:
        print(f"Snapshot {path} is missing the {SNAPSHOT_MARKER} marker; not importing.")
        write_result_env("unavailable", snapshot_path=path, reason="missing PANAMACOMPRA_MONITOR marker")
        return 4

    groups = parsed["groups"]
    unhealthy = [g for g, info in groups.items() if not info["healthy"]]

    if args.dry_run:
        print(f"[dry-run] snapshot: {path}")
        print(f"[dry-run] records parsed: {len(parsed['records'])}")
        for g, info in groups.items():
            state = "healthy" if info["healthy"] else f"UNHEALTHY ({info['reason']})"
            print(f"[dry-run]   {g}: collected={info['collected']} {state}")
        return 3 if unhealthy else 0

    conn = init_db()
    stats = import_records(conn, parsed["records"])
    write_result_env("partial" if unhealthy else "imported", groups=groups, stats=stats, snapshot_path=path)
    db_total = conn.execute("SELECT COUNT(*) AS c FROM opportunities").fetchone()["c"]
    pending = conn.execute("SELECT COUNT(*) AS c FROM opportunities WHERE detail_status != 'saved'").fetchone()["c"]

    group_summary = "; ".join(
        f"{g}={info['collected']}" + ("" if info["healthy"] else f"(UNHEALTHY:{info['reason']})")
        for g, info in groups.items()
    )
    summary = (
        f"SNAPSHOT INDEX IMPORT\n"
        f"Snapshot: {path}\n"
        f"Records parsed: {len(parsed['records'])}\n"
        f"Unique imported: {stats['unique']}\n"
        f"New DB records: {stats['new']}\n"
        f"Existing updated: {stats['existing']}\n"
        f"Index JSON written: {stats['json_written']}\n"
        f"Skipped (no link): {stats['skipped_no_link']}\n"
        f"Groups: {group_summary}\n"
        f"DB total: {db_total}; pending details: {pending}\n"
    )
    print(summary)
    log_path = LOG_DIR / f"index_snapshot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    log_path.write_text(summary, encoding="utf-8")

    write_run_progress(
        "INDEX", "DONE", 50,
        f"Step 1/7 (snapshot): new={stats['new']}, existing={stats['existing']}, pending details={pending}."
        + (f" Unhealthy groups: {', '.join(unhealthy)} — crawler will cover them." if unhealthy else ""),
        step_current=1, step_total=7, records_found=len(parsed["records"]),
        records_new=stats["new"], records_existing=stats["existing"], records_pending=pending,
        extra=f"source=snapshot; {group_summary}",
    )
    return 3 if unhealthy else 0


if __name__ == "__main__":
    raise SystemExit(main())
