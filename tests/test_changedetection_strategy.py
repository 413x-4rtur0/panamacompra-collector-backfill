from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "config" / "changedetection-browser-steps.js"


class ChangedetectionStrategyTests(unittest.TestCase):
    def test_crawls_abiertas_before_programadas(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        statuses_start = source.index("statuses: [")
        statuses_end = source.index("\n    ],", statuses_start)
        groups = re.findall(
            r'group:\s*"([^"]+)"',
            source[statuses_start:statuses_end],
        )

        self.assertEqual(["Abiertas", "Programadas"], groups)


if __name__ == "__main__":
    unittest.main()
