"""Read-only health and bounded controls for the second collector server.

The web monitor runs on HP23.  HP15 is deliberately reached through an
allow-listed SSH command set so a monitor request can never become an
arbitrary remote shell command.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import threading
import time
from pathlib import Path


HP15_HOST = os.environ.get("PC_MONITOR_HP15_HOST", "192.168.10.40")
HP15_USER = os.environ.get("PC_MONITOR_HP15_USER", "a2gutierrezmora")
HP15_ROOT = os.environ.get(
    "PC_MONITOR_HP15_ROOT",
    "/media/a2gutierrezmora/pcc-data/panamacompra-collector-2025backfill",
)
_CACHE_LOCK = threading.Lock()
_CACHE: tuple[float, dict[str, object]] | None = None


def _ssh_base() -> list[str]:
    return [
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=4",
        "-o", "ServerAliveInterval=2", "-o", "ServerAliveCountMax=1",
        "-i", os.path.expanduser(os.environ.get("PC_MONITOR_HP15_IDENTITY", "~/.ssh/id_ed25519_hp15")),
        f"{HP15_USER}@{HP15_HOST}",
    ]


def _remote_probe() -> str:
    root = shlex.quote(HP15_ROOT)
    # Fixed command; only the configured, quoted root is interpolated.
    return f"""python3 - <<'PY'
import json, os, platform, shutil, sqlite3, subprocess, time
root = {root}
procs = []
for line in subprocess.run(['ps','-eo','pid=,etime=,args='], capture_output=True, text=True).stdout.splitlines():
    if 'python3 - <<' not in line and any(x in line for x in ('039-run-closed-backfill.sh','PC_CLOSED_MODE=backfill','037-collect-closed-index.py')):
        procs.append(line.strip()[:500])
dbs = [os.path.join(root, 'data', 'panamacompra_archive.db'), os.path.join(root, 'var', 'data', 'panamacompra_archive.db')]
db = next((p for p in dbs if os.path.exists(p)), '')
state = {{}}
if db:
    try:
        con = sqlite3.connect('file:' + db + '?mode=ro', uri=True, timeout=2)
        try:
            tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            table = 'closed_crawl_state' if 'closed_crawl_state' in tables else ''
            if table:
                cols = [r[1] for r in con.execute("PRAGMA table_info(closed_crawl_state)").fetchall()]
                row = con.execute("SELECT * FROM closed_crawl_state LIMIT 1").fetchone()
                state = dict(zip(cols, row)) if row else {{}}
            else:
                state = {{'error': 'closed_crawl_state table not found'}}
        finally:
            con.close()
    except Exception as exc:
        state = {{'error': str(exc)[:240]}}
du = shutil.disk_usage(root) if os.path.exists(root) else None
print(json.dumps({{
    'ok': True, 'host': platform.node(), 'root': root, 'db': db,
    'backfill_active': bool(procs), 'processes': procs, 'backfill_state': state,
    'disk_percent': round((du.used / du.total) * 100, 1) if du else None,
    'checked_at': time.strftime('%Y-%m-%d %H:%M:%S'),
}}, ensure_ascii=False))
PY"""


def hp15_status() -> dict[str, object]:
    global _CACHE
    with _CACHE_LOCK:
        if _CACHE and time.time() - _CACHE[0] < 15:
            return dict(_CACHE[1])
    try:
        result = subprocess.run(
            _ssh_base() + [_remote_probe()], capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        payload = {"server": "HP15", "host": HP15_HOST, "reachable": False, "error": str(exc)}
        with _CACHE_LOCK: _CACHE = (time.time(), payload)
        return payload
    if result.returncode:
        payload = {"server": "HP15", "host": HP15_HOST, "reachable": False,
                   "error": (result.stderr or result.stdout or "ssh failed").strip()[-500:]}
        with _CACHE_LOCK: _CACHE = (time.time(), payload)
        return payload
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    try:
        payload = json.loads(lines[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        payload = {"server": "HP15", "host": HP15_HOST, "reachable": False,
                   "error": f"invalid remote probe response: {exc}"}
        with _CACHE_LOCK: _CACHE = (time.time(), payload)
        return payload
    payload.update({"server": "HP15", "host": HP15_HOST, "reachable": True})
    with _CACHE_LOCK: _CACHE = (time.time(), payload)
    return payload


def hp15_backfill_action(action: str) -> dict[str, object]:
    commands = {
        "start-backfill": (
            f"cd {shlex.quote(HP15_ROOT)} && "
            "if pgrep -af '039-run-closed-backfill.sh|PC_CLOSED_MODE=backfill' >/dev/null; "
            "then echo 'backfill already active'; "
            "else nohup env PC_CLOSED_MODE=backfill ./src/20_pipeline/039-run-closed-backfill.sh "
            ">> var/logs/monitor-backfill.log 2>&1 </dev/null & echo 'backfill started'; fi"
        ),
        "stop-backfill": (
            f"cd {shlex.quote(HP15_ROOT)} && ./src/20_pipeline/120c-stop-closed-backfill.sh"
        ),
        "check": _remote_probe(),
    }
    if action not in commands:
        return {"ok": False, "error": "unsupported HP15 action"}
    try:
        result = subprocess.run(_ssh_base() + [commands[action]], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "server": "HP15", "error": str(exc)}
    return {"ok": result.returncode == 0, "server": "HP15", "action": action,
            "output": (result.stdout or result.stderr).strip()[-1000:]}


def servers_payload(local: dict[str, object]) -> dict[str, object]:
    hp15 = hp15_status()
    return {"primary": "HP23", "servers": [
        {"server": "HP23", "role": "primary", "reachable": True, **local},
        {"role": "secondary", **hp15},
    ]}
