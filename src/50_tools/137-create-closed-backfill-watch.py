#!/usr/bin/env python3
"""Create or refresh the dedicated changedetection Closed backfill watch.

The live Closed novelty watch is the source template.  We transform only the
identity, webhook route, output selector, and pagination guard, so the new
watch cannot silently drift from the tested Cerradas/Canceladas extraction.
The tool refuses to restart changedetection while the host backfill service is
running; retrying after that service finishes is deliberate and safe.
"""
from __future__ import annotations

import datetime
import glob
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import uuid
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[2]
TOKEN_FILE = APP_ROOT / ".webhook_token_closed_backfill"
BACKFILL_TITLE = "PANAMACOMPRA_MONITOR_CERRADAS_BACKFILL"
BACKFILL_MARKER = "PANAMACOMPRA_MONITOR_CLOSED_BACKFILL"
BACKFILL_ROUTE = "panamacompra-closed-backfill"
DISPLAY_ACTIVE_TITLE = "PanamaCompra - Programadas y Abiertas"
DISPLAY_CLOSED_TITLE = "PanamaCompra - Cerradas y Canceladas - Novedades"
DISPLAY_BACKFILL_TITLE = "PanamaCompra - Cerradas - Backfill"
BACKFILL_OUTPUT_SELECTOR = "#pc-monitor-output-closed-backfill"


def run(command: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout)


def monitor_date_setting(name: str) -> str:
    path = APP_ROOT / "var" / "data" / "config" / "monitor_settings.env"
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    for line in lines:
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, raw = line.split("=", 1)
        if key.strip() != name:
            continue
        try:
            value = shlex.split(raw, posix=True)[0]
        except (ValueError, IndexError):
            value = raw.strip().strip("'").strip('"')
        try:
            datetime.date.fromisoformat(value)
        except ValueError:
            return ""
        return value
    return ""


def transform_script(script: str, date_start: str = "", date_end: str = "") -> str:
    script, removed = re.subn(
        r',\s*\{\s*group:\s*"Cancelled".*?\n\s*\}\s*\n\s*\]',
        "\n    ]",
        script,
        count=1,
        flags=re.S,
    )
    if removed != 1 and 'group: "Cancelled"' in script:
        raise ValueError("Closed script has no Cancelled status block to remove")
    script = script.replace(
        'outputSelector: "#pc-monitor-output-closed"',
        f'outputSelector: "{BACKFILL_OUTPUT_SELECTOR}"',
        1,
    )
    script = script.replace(
        'monitorTitle: "PANAMACOMPRA_MONITOR_CLOSED"',
        f'monitorTitle: "{BACKFILL_MARKER}"',
        1,
    )
    script, count = re.subn(
        r"maxPagesSafety:\s*\d+",
        "maxPagesSafety: 0",
        script,
        count=1,
    )
    if count != 1:
        raise ValueError("Closed script has no maxPagesSafety setting")
    loop = "for (let page = 1; page <= CONFIG.maxPagesSafety; page++)"
    unlimited_loop = (
        "for (let page = 1; CONFIG.maxPagesSafety <= 0 || "
        "page <= CONFIG.maxPagesSafety; page++)"
    )
    if unlimited_loop in script:
        pass
    if loop not in script:
        if unlimited_loop not in script:
            raise ValueError("Closed script has no bounded pagination loop")
    else:
        script = script.replace(loop, unlimited_loop, 1)
    config = f'    dateStart: "{date_start}",\n    dateEnd: "{date_end}",'
    script, _ = re.subn(r'\s*dateStart:\s*"[^"]*",\s*\n\s*dateEnd:\s*"[^"]*",', "\n" + config, script, count=1)
    if "dateStart:" not in script:
        script = script.replace("maxPagesSafety: 0,", "maxPagesSafety: 0,\n" + config, 1)
    return script


def backfill_notification(url: str, token: str) -> str:
    if f"{BACKFILL_ROUTE}/" in url:
        return url
    route = f"{BACKFILL_ROUTE}/{token}"
    replaced, count = re.subn(r"panamacompra-closed/[^?]+", route, url, count=1)
    if count != 1:
        raise ValueError(f"cannot convert Closed notification URL: {url}")
    return replaced


def main() -> int:
    try:
        token = TOKEN_FILE.read_text(encoding="utf-8").strip()
    except OSError as exc:
        print(json.dumps({"ok": False, "error": f"cannot read {TOKEN_FILE}: {exc}"}))
        return 1
    if not token:
        print(json.dumps({"ok": False, "error": f"empty token file: {TOKEN_FILE}"}))
        return 1

    background = run([
        "systemctl", "--user", "is-active", "--quiet",
        "panamacompra-closed-backfill.service",
    ])
    if background.returncode == 0:
        print(json.dumps({
            "ok": False,
            "error": "panamacompra-closed-backfill.service is active; retry after it finishes",
        }))
        return 2

    ps = run(["docker", "ps", "--filter", "name=changedetection", "--format", "{{.ID}}"])
    container = ps.stdout.strip().splitlines()[0] if ps.stdout.strip() else ""
    if not container:
        print(json.dumps({"ok": False, "error": "changedetection container is not running"}))
        return 1

    patch_script = r"""
import copy, datetime, glob, json, os, re, shutil, uuid

BACKFILL_TITLE = "PANAMACOMPRA_MONITOR_CERRADAS_BACKFILL"
BACKFILL_MARKER = "PANAMACOMPRA_MONITOR_CLOSED_BACKFILL"
BACKFILL_OUTPUT_SELECTOR = "#pc-monitor-output-closed-backfill"
BACKFILL_ROUTE = "panamacompra-closed-backfill"
DISPLAY_ACTIVE_TITLE = "PanamaCompra - Programadas y Abiertas"
DISPLAY_CLOSED_TITLE = "PanamaCompra - Cerradas y Canceladas - Novedades"
DISPLAY_BACKFILL_TITLE = "PanamaCompra - Cerradas - Backfill"
TOKEN = os.environ["PC_BACKFILL_TOKEN"]
DATE_START = os.environ.get("PC_BACKFILL_DATE_START", "")
DATE_END = os.environ.get("PC_BACKFILL_DATE_END", "")

def transform(script):
    script, removed = re.subn(r',\s*\{\s*group:\s*"Cancelled".*?\n\s*\}\s*\n\s*\]', "\n    ]", script, count=1, flags=re.S)
    if removed != 1 and 'group: "Cancelled"' in script:
        raise ValueError("Closed script has no Cancelled status block to remove")
    script = script.replace('outputSelector: "#pc-monitor-output-closed"', 'outputSelector: "#pc-monitor-output-closed-backfill"', 1)
    script = script.replace('monitorTitle: "PANAMACOMPRA_MONITOR_CLOSED"', f'monitorTitle: "{BACKFILL_MARKER}"', 1)
    script, count = re.subn(r"maxPagesSafety:\s*\d+", "maxPagesSafety: 0", script, count=1)
    if count != 1:
        raise ValueError("Closed script has no maxPagesSafety setting")
    bounded = "for (let page = 1; page <= CONFIG.maxPagesSafety; page++)"
    unlimited = "for (let page = 1; CONFIG.maxPagesSafety <= 0 || page <= CONFIG.maxPagesSafety; page++)"
    if bounded not in script:
        if unlimited in script:
            pass
        else:
            raise ValueError("Closed script has no bounded pagination loop")
    else:
        script = script.replace(bounded, unlimited, 1)
    config = f'    dateStart: "{DATE_START}",\n    dateEnd: "{DATE_END}",'
    script, _ = re.subn(r'\s*dateStart:\s*"[^"]*",\s*\n\s*dateEnd:\s*"[^"]*",', "\n" + config, script, count=1)
    if "dateStart:" not in script:
        script = script.replace("maxPagesSafety: 0,", "maxPagesSafety: 0,\n" + config, 1)
    date_helpers = '''
  function parseBackfillDate(row) {
    const raw = clean(row && row.fecha ? row.fecha : "");
    const match = raw.match(/^(\\d{1,2})[\\/-](\\d{1,2})[\\/-](\\d{4})/);
    if (!match) return "";
    return `${match[3]}-${match[2].padStart(2, "0")}-${match[1].padStart(2, "0")}`;
  }

  function backfillDateInRange(row) {
    const value = parseBackfillDate(row);
    if (!value) return true;
    if (CONFIG.dateStart && value < CONFIG.dateStart) return false;
    if (CONFIG.dateEnd && value > CONFIG.dateEnd) return false;
    return true;
  }

  function pagePassedBackfillStart(rows) {
    return Boolean(CONFIG.dateStart) && rows.some(row => {
      const value = parseBackfillDate(row);
      return value && value < CONFIG.dateStart;
    });
  }

  function extractAllExpectedRows(statusConfig) {
    const wanted = normalize(statusConfig.expectedEstado);
    return getCurrentRowsRaw().filter(row => normalize(row.estado).startsWith(wanted));
  }

  function extractOnlyExpectedRows(statusConfig) {
    return extractAllExpectedRows(statusConfig).filter(backfillDateInRange);
  }
'''
    start = script.index("  function extractOnlyExpectedRows(statusConfig)")
    end = script.index("\n\n  async function crawlStatus", start)
    script = script[:start] + date_helpers.rstrip() + script[end:]
    old_rows = "const expectedRows = extractOnlyExpectedRows(statusConfig);"
    new_rows = "const allExpectedRows = extractAllExpectedRows(statusConfig);\n      const expectedRows = allExpectedRows.filter(backfillDateInRange);\n      const passedBackfillStart = pagePassedBackfillStart(allExpectedRows);"
    script = script.replace(old_rows, new_rows, 1)
    old_consistent = "(expectedItems === 0 || crawledItems + duplicateCount >= expectedItems)"
    script = script.replace(old_consistent, "(CONFIG.dateStart || CONFIG.dateEnd || expectedItems === 0 || crawledItems + duplicateCount >= expectedItems)", 1)
    old_next = "      const next = await goNextPage();"
    if "RANGE_COMPLETE: reached start date" not in script:
        new_next = "      if (passedBackfillStart) {\n        complete = true;\n        pageCounts.push(`${statusConfig.group} RANGE_COMPLETE: reached start date ${CONFIG.dateStart}`);\n        break;\n      }\n\n" + old_next
        script = script.replace(old_next, new_next, 1)
    return script

def convert_url(url):
    if f"{BACKFILL_ROUTE}/" in url:
        return url
    converted, count = re.subn(r"panamacompra-closed/[^?]+", f"{BACKFILL_ROUTE}/{TOKEN}", url, count=1)
    if count != 1:
        raise ValueError(f"cannot convert notification URL: {url}")
    return converted

stamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
watches = []
for path in glob.glob("/datastore/*/watch.json"):
    try:
        data = json.load(open(path, encoding="utf-8"))
    except Exception:
        continue
    js = str(data.get("webdriver_js_execute_code") or "")
    title = str(data.get("title") or data.get("name") or "")
    if BACKFILL_MARKER in js or BACKFILL_TITLE in title:
        watches.append((path, data))

if watches:
    target_path, data = watches[0]
    old_uuid = os.path.basename(os.path.dirname(target_path))
else:
    template = None
    for path in glob.glob("/datastore/*/watch.json"):
        try:
            data = json.load(open(path, encoding="utf-8"))
        except Exception:
            continue
        js = str(data.get("webdriver_js_execute_code") or "")
        if "PANAMACOMPRA_MONITOR_CLOSED" in js and BACKFILL_MARKER not in js:
            template = data
            break
    if template is None:
        raise RuntimeError("no Closed novelty watch found to use as template")
    old_uuid = ""
    new_uuid = str(uuid.uuid4())
    target_path = f"/datastore/{new_uuid}/watch.json"
    os.makedirs(os.path.dirname(target_path), exist_ok=False)
    data = copy.deepcopy(template)
    data["uuid"] = new_uuid

data["title"] = DISPLAY_BACKFILL_TITLE
data["name"] = DISPLAY_BACKFILL_TITLE
data["webdriver_js_execute_code"] = transform(str(data.get("webdriver_js_execute_code") or ""))
# The browser script writes only this stable marker. Keep ChangeDetection's
# CSS/include filter synchronized with it so a stale selector can never make
# the watcher silently skip change detection.
data["include_filters"] = [BACKFILL_OUTPUT_SELECTOR]
data["consecutive_filter_failures"] = 0
data["notification_urls"] = [convert_url(str(url)) for url in (data.get("notification_urls") or [])]
data["last_checked"] = 0
data["last_error"] = ""
backup = target_path + ".bak-pc-backfill-" + stamp
if os.path.exists(target_path):
    shutil.copy2(target_path, backup)
with open(target_path, "w", encoding="utf-8") as fh:
    json.dump(data, fh, ensure_ascii=False)

for path in glob.glob("/datastore/*/watch.json"):
    if path == target_path:
        continue
    try:
        other = json.load(open(path, encoding="utf-8"))
    except Exception:
        continue
    other_js = str(other.get("webdriver_js_execute_code") or "")
    if BACKFILL_MARKER in other_js:
        display = DISPLAY_BACKFILL_TITLE
    elif "PANAMACOMPRA_MONITOR_CLOSED" in other_js:
        display = DISPLAY_CLOSED_TITLE
    elif "PANAMACOMPRA_MONITOR" in other_js:
        display = DISPLAY_ACTIVE_TITLE
    else:
        continue
    if other.get("title") == display and other.get("name") == display:
        continue
    other["title"] = display
    other["name"] = display
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(other, fh, ensure_ascii=False)
print(json.dumps({"path": target_path, "old_uuid": old_uuid, "backup": backup if os.path.exists(backup) else "", "title": data["title"], "marker": BACKFILL_MARKER}, ensure_ascii=False))
"""
    patch = run([
        "docker", "exec", "-e", f"PC_BACKFILL_TOKEN={token}",
        "-e", f"PC_BACKFILL_DATE_START={monitor_date_setting('PC_CLOSED_BACKFILL_START_DATE')}",
        "-e", f"PC_BACKFILL_DATE_END={monitor_date_setting('PC_CLOSED_BACKFILL_END_DATE')}",
        container, "python3", "-c", patch_script,
    ], timeout=30)
    if patch.returncode:
        print(json.dumps({"ok": False, "error": patch.stderr.strip() or patch.stdout.strip()}))
        return 1
    try:
        result = json.loads(patch.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        print(json.dumps({"ok": False, "error": f"invalid in-container result: {patch.stdout!r}"}))
        return 1

    restart = run(["docker", "restart", container], timeout=30)
    if restart.returncode:
        print(json.dumps({"ok": False, "watch": result, "error": restart.stderr.strip()}))
        return 1
    result.update({"ok": True, "container": container})
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
