from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "src/40_monitor/monitor_common.py"
SPEC = importlib.util.spec_from_file_location("monitor_common_directory_test", MODULE_PATH)
assert SPEC and SPEC.loader
MONITOR_COMMON = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MONITOR_COMMON
SPEC.loader.exec_module(MONITOR_COMMON)


class WahaDirectorySearchTests(unittest.TestCase):
    def test_search_filters_before_display_limit(self) -> None:
        rows = [
            {"session": "default", "id": f"{index}@c.us", "name": f"Contact {index}", "kind": "contact"}
            for index in range(300)
        ]
        rows.append({
            "session": "default",
            "id": "target@g.us",
            "name": "Target Client",
            "kind": "group",
        })

        matches = MONITOR_COMMON.waha_filter_matches(rows, "target", limit=100)

        self.assertEqual(["target@g.us"], [match["id"] for match in matches])

    def test_multi_word_search_is_accent_insensitive(self) -> None:
        rows = [{
            "session": "default",
            "id": "120363000@g.us",
            "name": "(Chiriquí) Contratistas e Ingeniería",
            "kind": "group",
        }]

        matches = MONITOR_COMMON.waha_filter_matches(rows, "chiriqui ingenieria")

        self.assertEqual(rows, matches)

    def test_unlimited_result_mode_keeps_complete_directory(self) -> None:
        rows = [
            {"session": "default", "id": f"{index}@c.us", "name": f"Contact {index}", "kind": "contact"}
            for index in range(301)
        ]

        self.assertEqual(301, len(MONITOR_COMMON.waha_filter_matches(rows, limit=None)))


if __name__ == "__main__":
    unittest.main()
