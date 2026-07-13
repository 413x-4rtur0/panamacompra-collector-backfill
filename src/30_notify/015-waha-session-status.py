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
import sys
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parent.parent
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))
import common as pc_common

waha = pc_common.load_script("src/30_notify/010-waha-client.py", "waha_client_status")
session_status = waha.waha_session_status


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
