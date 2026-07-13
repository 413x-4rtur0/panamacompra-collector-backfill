from __future__ import annotations

import importlib.util
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "src/50_tools/170-waha-directory.py"
SPEC = importlib.util.spec_from_file_location("waha_directory_cli_test", MODULE_PATH)
assert SPEC and SPEC.loader
CLI = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = CLI
SPEC.loader.exec_module(CLI)


class WahaDirectoryCliTests(unittest.TestCase):
    def test_plural_kind_aliases(self) -> None:
        self.assertEqual(
            {"contact", "group", "community", "channel"},
            CLI.normalize_kinds(["contacts,groups", "communities", "channels"]),
        )

    def test_refresh_writes_private_cache_then_reuses_it(self) -> None:
        rows = [{
            "session": "default",
            "id": "120363000@g.us",
            "name": "Client Group",
            "kind": "group",
        }]
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_file = Path(temp_dir) / "directory.json"
            with (
                mock.patch.object(CLI, "CACHE_FILE", cache_file),
                mock.patch.object(CLI, "waha_fetch_all", return_value=rows) as fetch,
            ):
                loaded, source = CLI.load_directory(refresh=True)
                cached, cached_source = CLI.load_directory()

            self.assertEqual(rows, loaded)
            self.assertEqual(rows, cached)
            self.assertEqual("WAHA", source)
            self.assertEqual("cache", cached_source)
            fetch.assert_called_once()
            self.assertEqual(0o600, stat.S_IMODE(cache_file.stat().st_mode))


if __name__ == "__main__":
    unittest.main()
