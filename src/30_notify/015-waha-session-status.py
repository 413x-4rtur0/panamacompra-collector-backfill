#!/usr/bin/env python3
"""Report whether the configured WAHA session is ready or needs a QR scan.

Exit codes are intentionally suitable for the Bash worker:
  0  session is connected
 10  WAHA is asking for a QR scan
 11  another non-connected/unreachable state
"""
from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request


QR_STATUSES = {"SCAN_QR_CODE", "STARTING"}
CONNECTED_STATUSES = {"WORKING", "RUNNING"}


def session_status() -> dict[str, object]:
    base_url = os.environ.get("PC_WAHA_BASE_URL", "http://127.0.0.1:3000").rstrip("/")
    session_name = os.environ.get("PC_WAHA_SESSION", "default").strip() or "default"
    api_key = (os.environ.get("PC_WAHA_API_KEY") or os.environ.get("WAHA_API_KEY", "")).strip()
    try:
        timeout = max(1.0, float(os.environ.get("PC_WAHA_STATUS_TIMEOUT_SECONDS", "8")))
    except ValueError:
        timeout = 8.0

    headers = {"Accept": "application/json"}
    if api_key:
        headers["X-Api-Key"] = api_key
    request = urllib.request.Request(f"{base_url}/api/sessions?all=true", headers=headers)

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - operator-configured local WAHA
            sessions = json.loads(response.read().decode("utf-8", "replace"))
    except (OSError, ValueError, urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
        return {
            "session": session_name,
            "status": "UNREACHABLE",
            "connected": False,
            "needs_qr": False,
            "message": f"WAHA unreachable: {exc}",
            "dashboard_url": base_url,
        }

    if not isinstance(sessions, list):
        sessions = []
    match = next(
        (item for item in sessions if isinstance(item, dict) and str(item.get("name") or "") == session_name),
        None,
    )
    if match is None:
        return {
            "session": session_name,
            "status": "NOT_FOUND",
            "connected": False,
            "needs_qr": False,
            "message": f"No WAHA session named '{session_name}'.",
            "dashboard_url": base_url,
        }

    status = str(match.get("status") or "UNKNOWN").upper()
    connected = status in CONNECTED_STATUSES
    needs_qr = status in QR_STATUSES
    return {
        "session": session_name,
        "status": status,
        "connected": connected,
        "needs_qr": needs_qr,
        "message": "" if connected else f"WAHA session '{session_name}' is {status}.",
        "dashboard_url": base_url,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Print a machine-readable status object.")
    args = parser.parse_args()
    result = session_status()
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(result["message"] or f"WAHA session '{result['session']}' is connected.")
    if result["connected"]:
        return 0
    if result["needs_qr"]:
        return 10
    return 11


if __name__ == "__main__":
    raise SystemExit(main())
