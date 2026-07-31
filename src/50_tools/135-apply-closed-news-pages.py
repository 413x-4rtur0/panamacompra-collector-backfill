#!/usr/bin/env python3
"""Rewrite the Closed new-closures changedetection watch's own page-sampling
cap (maxPagesSafety in config/changedetection-browser-steps-closed.js) and
push it into the live watch.

No REST API path exists for pushing the script itself: changedetection's
/api/v1/watch PUT enforces a hard 5000-char maxLength on
webdriver_js_execute_code and this script is ~20KB (confirmed by hand this
session). So this patches the on-disk watch.json directly inside the
changedetection container (scanning for the watch whose notification_urls
contains "panamacompra-closed", rather than a hardcoded UUID, so a
recreated watch is still found) and restarts just that one container to
pick it up — the same steps done manually once this session, now scripted
so the monitor's Settings tab can trigger it without a shell session.

Usage: 135-apply-closed-news-pages.py PAGES
Prints one line of JSON: {"ok": bool, ...}. Exit code 0 either way — callers
should check the "ok" field, not the process exit code, since a clean
JSON-formatted failure is still a successful run of this script.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent.parent
JS_PATH = APP_ROOT / "config" / "changedetection-browser-steps-closed.js"


def result(ok: bool, **fields) -> None:
    print(json.dumps({"ok": ok, **fields}, ensure_ascii=False))


def main() -> None:
    if len(sys.argv) != 2:
        result(False, error="usage: 135-apply-closed-news-pages.py PAGES")
        return

    try:
        pages = int(sys.argv[1])
    except ValueError:
        result(False, error=f"PAGES must be an integer, got {sys.argv[1]!r}")
        return

    if not 1 <= pages <= 50:
        result(False, error=f"PAGES must be between 1 and 50, got {pages}")
        return

    if not JS_PATH.exists():
        result(False, error=f"script not found: {JS_PATH}")
        return

    content = JS_PATH.read_text(encoding="utf-8")
    new_content, count = re.subn(r"maxPagesSafety:\s*\d+,", f"maxPagesSafety: {pages},", content, count=1)
    if count != 1:
        result(False, error="maxPagesSafety line not found in the local script")
        return
    JS_PATH.write_text(new_content, encoding="utf-8")

    # docker ps, not docker compose ps: the latter re-interpolates the whole
    # compose file (including unrelated services like waha) and fails if any
    # required var is blank in the caller's merged environment, even though
    # we only need this one already-running container's id.
    docker_ps = subprocess.run(
        ["docker", "ps", "--filter", "name=changedetection", "--format", "{{.ID}}"],
        capture_output=True, text=True, timeout=10,
    )
    container = docker_ps.stdout.strip().splitlines()[0] if docker_ps.stdout.strip() else ""
    if not container:
        result(False, error="changedetection container is not running (local script file updated anyway)", local_only=True)
        return

    cp = subprocess.run(
        ["docker", "cp", str(JS_PATH), f"{container}:/tmp/closed-script.js"],
        capture_output=True, text=True, timeout=15,
    )
    if cp.returncode != 0:
        result(False, error=f"docker cp failed: {cp.stderr.strip()}")
        return

    patch_script = r"""
import json, glob, datetime, shutil

target = None
for path in glob.glob('/datastore/*/watch.json'):
    try:
        d = json.load(open(path, encoding='utf-8'))
    except Exception:
        continue
    urls = d.get('notification_urls') or []
    # Exact route segment, not a bare substring: '/panamacompra-closed-
    # backfill/...' also contains 'panamacompra-closed', which would
    # otherwise match this too and overwrite the backfill watch's own
    # dateStart/dateEnd-injected script with this priority-2 one.
    if any('/panamacompra-closed/' in u for u in urls):
        target = path
        break

if not target:
    print(json.dumps({'ok': False, 'error': 'no watch with a /panamacompra-closed/ notification URL was found'}))
    raise SystemExit(0)

backup = target + '.bak-news-pages-' + datetime.datetime.now().strftime('%Y%m%d%H%M%S')
shutil.copy(target, backup)

d = json.load(open(target, encoding='utf-8'))
script = open('/tmp/closed-script.js', encoding='utf-8').read()
d['webdriver_js_execute_code'] = script
json.dump(d, open(target, 'w', encoding='utf-8'), ensure_ascii=False)

print(json.dumps({'ok': True, 'watch_path': target, 'backup': backup}))
"""
    patch = subprocess.run(
        ["docker", "exec", container, "python3", "-c", patch_script],
        capture_output=True, text=True, timeout=15,
    )
    if patch.returncode != 0:
        result(False, error=f"in-container patch failed: {patch.stderr.strip()}")
        return

    try:
        patch_result = json.loads(patch.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        result(False, error=f"could not parse patch result: {patch.stdout!r}")
        return

    if not patch_result.get("ok"):
        result(False, error=patch_result.get("error", "unknown patch failure"))
        return

    restart = subprocess.run(["docker", "restart", container], capture_output=True, text=True, timeout=30)
    if restart.returncode != 0:
        result(False, error=f"docker restart failed: {restart.stderr.strip()}", patched_before_restart=True)
        return

    result(True, pages=pages, container=container, watch_path=patch_result.get("watch_path"))


if __name__ == "__main__":
    main()
