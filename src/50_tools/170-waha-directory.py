#!/usr/bin/env python3
"""Search the WAHA contact/group/community/channel directory from the CLI."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

MONITOR_DIR = Path(__file__).resolve().parent.parent / "40_monitor"
if str(MONITOR_DIR) not in sys.path:
    sys.path.insert(0, str(MONITOR_DIR))

from monitor_common import setting, waha_fetch_all, waha_filter_matches  # noqa: E402


CACHE_FILE = Path(os.environ.get("PC_RUN_DIR", "data/run")) / "waha_directory_cache.json"
PUBLIC_KEYS = ("session", "id", "name", "kind")
KIND_ALIASES = {
    "contacts": "contact",
    "groups": "group",
    "communities": "community",
    "channels": "channel",
    "chats": "chat",
    "broadcasts": "broadcast",
}


def cache_ttl() -> float:
    try:
        return max(15.0, float(setting("PC_WAHA_DIRECTORY_CACHE_SECONDS", "120") or 120))
    except ValueError:
        return 120.0


def read_cache(*, allow_stale: bool = False) -> tuple[list[dict], bool]:
    try:
        payload = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        loaded_at = float(payload.get("loaded_at") or 0)
        matches = payload.get("matches")
        if not isinstance(matches, list):
            return [], False
        fresh = time.time() - loaded_at < cache_ttl()
        return (matches, fresh) if fresh or allow_stale else ([], False)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return [], False


def write_cache(matches: list[dict]) -> None:
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "loaded_at": time.time(),
        "matches": [{key: row.get(key, "") for key in PUBLIC_KEYS} for row in matches],
    }
    fd, temp_name = tempfile.mkstemp(prefix=".waha-directory-", suffix=".json", dir=CACHE_FILE.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
        os.replace(temp_name, CACHE_FILE)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def load_directory(*, refresh: bool = False) -> tuple[list[dict], str]:
    if not refresh:
        cached, fresh = read_cache()
        if fresh:
            return cached, "cache"
    try:
        matches = waha_fetch_all("", include_chats=True, raise_on_connection_error=True, limit=None)
        write_cache(matches)
        return matches, "WAHA"
    except Exception:
        stale, _fresh = read_cache(allow_stale=True)
        if stale:
            return stale, "stale cache"
        raise


def normalize_kinds(raw_values: list[str]) -> set[str]:
    kinds = set()
    for raw in raw_values:
        for value in raw.split(","):
            value = value.strip().lower()
            if value:
                kinds.add(KIND_ALIASES.get(value, value))
    return kinds


def clipped(value: object, width: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= width:
        return text
    return text[:max(1, width - 1)] + "…"


def print_table(matches: list[dict], *, total: int, directory_count: int, source: str) -> None:
    width = max(80, shutil.get_terminal_size((120, 24)).columns)
    kind_width = 10
    id_width = min(32, max(20, width // 4))
    session_width = 12
    name_width = max(20, width - kind_width - id_width - session_width - 9)
    print(f"WAHA directory: {directory_count} destinations · {total} match(es) · source: {source}")
    print(f"{'TYPE':<{kind_width}} {'NAME':<{name_width}} {'CHAT ID':<{id_width}} {'SESSION':<{session_width}}")
    print("─" * min(width, kind_width + name_width + id_width + session_width + 3))
    for match in matches:
        print(
            f"{clipped(match.get('kind'), kind_width):<{kind_width}} "
            f"{clipped(match.get('name'), name_width):<{name_width}} "
            f"{clipped(match.get('id'), id_width):<{id_width}} "
            f"{clipped(match.get('session'), session_width):<{session_width}}"
        )
    if not matches:
        print("No matching destinations.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Search WAHA contacts, groups, communities, channels and chats.",
    )
    parser.add_argument("query", nargs="*", help="partial/multi-word name or chat ID")
    parser.add_argument("--kind", action="append", default=[],
                        help="contact, group, community, channel, chat or broadcast (comma-separated allowed)")
    parser.add_argument("--limit", type=int, default=50, help="maximum displayed results (default: 50; 0 = all)")
    parser.add_argument("--refresh", action="store_true", help="bypass the short-lived local directory cache")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.limit < 0:
        raise SystemExit("--limit must be 0 or greater")
    query = " ".join(args.query).strip()
    kinds = normalize_kinds(args.kind)
    directory, source = load_directory(refresh=args.refresh)
    filtered = waha_filter_matches(directory, query, limit=None)
    if kinds:
        filtered = [row for row in filtered if str(row.get("kind") or "").lower() in kinds]
    total = len(filtered)
    shown = filtered if args.limit == 0 else filtered[:args.limit]
    public = [{key: row.get(key, "") for key in PUBLIC_KEYS} for row in shown]
    if args.json:
        print(json.dumps({
            "query": query,
            "kinds": sorted(kinds),
            "directory_count": len(directory),
            "match_count": total,
            "shown_count": len(public),
            "source": source,
            "matches": public,
        }, ensure_ascii=False, indent=2))
    else:
        print_table(public, total=total, directory_count=len(directory), source=source)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
