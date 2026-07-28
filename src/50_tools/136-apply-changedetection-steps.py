#!/usr/bin/env python3
"""Apply both repository Browser Steps scripts to the live changedetection watches.

The changedetection REST API rejects these scripts because they exceed its
webdriver_js_execute_code size limit. This tool patches the matching watch
datastore files inside the running container, creates a timestamped backup,
and restarts only changedetection.
"""
from __future__ import annotations

import datetime
import glob
import json
import shutil
import subprocess
import sys
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[2]
ACTIVE_JS = APP_ROOT / "config" / "changedetection-browser-steps.js"
CLOSED_JS = APP_ROOT / "config" / "changedetection-browser-steps-closed.js"


def run(command: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout)


def main() -> int:
    for path in (ACTIVE_JS, CLOSED_JS):
        if not path.exists():
            print(json.dumps({"ok": False, "error": f"missing script: {path}"}))
            return 1

    ps = run(["docker", "ps", "--filter", "name=changedetection", "--format", "{{.ID}}"])
    container = ps.stdout.strip().splitlines()[0] if ps.stdout.strip() else ""
    if not container:
        print(json.dumps({"ok": False, "error": "changedetection container is not running"}))
        return 1

    for local, target in ((ACTIVE_JS, "/tmp/pc-active.js"), (CLOSED_JS, "/tmp/pc-closed.js")):
        cp = run(["docker", "cp", str(local), f"{container}:{target}"], timeout=15)
        if cp.returncode:
            print(json.dumps({"ok": False, "error": f"docker cp failed: {cp.stderr.strip()}"}))
            return 1

    patch_script = r"""
import datetime, glob, json, os, shutil

active = open('/tmp/pc-active.js', encoding='utf-8').read()
closed = open('/tmp/pc-closed.js', encoding='utf-8').read()
stamp = datetime.datetime.now().strftime('%Y%m%d%H%M%S')
patched = []

for path in glob.glob('/datastore/*/watch.json'):
    try:
        with open(path, encoding='utf-8') as fh:
            data = json.load(fh)
    except Exception:
        continue
    title = str(data.get('title') or data.get('name') or '').upper()
    urls = [str(value) for value in (data.get('notification_urls') or [])]
    closed_watch = 'PANAMACOMPRA_MONITOR_CLOSED' in title or any('panamacompra-closed' in url for url in urls)
    active_watch = ('PANAMACOMPRA_MONITOR' in title and not closed_watch) or any(
        'panamacompra' in url and 'panamacompra-closed' not in url for url in urls
    )
    script = closed if closed_watch else active if active_watch else None
    role = 'closed' if closed_watch else 'active' if active_watch else ''
    if not script:
        continue
    backup = path + '.bak-pc-steps-' + stamp
    shutil.copy2(path, backup)
    data['webdriver_js_execute_code'] = script
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(data, fh, ensure_ascii=False)
    patched.append({'role': role, 'watch_path': path, 'backup': backup})

print(json.dumps({'ok': bool(patched), 'patched': patched}))
"""
    patch = run(["docker", "exec", container, "python3", "-c", patch_script], timeout=20)
    if patch.returncode:
        print(json.dumps({"ok": False, "error": f"in-container patch failed: {patch.stderr.strip()}"}))
        return 1

    try:
        result = json.loads(patch.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        print(json.dumps({"ok": False, "error": f"invalid patch result: {patch.stdout!r}"}))
        return 1
    if not result.get("ok"):
        print(json.dumps(result))
        return 1

    restart = run(["docker", "restart", container], timeout=30)
    if restart.returncode:
        print(json.dumps({
            "ok": False,
            "patched": result.get("patched", []),
            "error": f"docker restart failed: {restart.stderr.strip()}",
        }))
        return 1

    print(json.dumps({"ok": True, "container": container, "patched": result["patched"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
