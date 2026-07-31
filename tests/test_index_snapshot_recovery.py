from __future__ import annotations

import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


SNAPSHOT = load_module(
    "index_snapshot_recovery_test",
    "src/20_pipeline/015-import-index-snapshot.py",
)
PLAYWRIGHT = types.ModuleType("playwright")
PLAYWRIGHT_SYNC = types.ModuleType("playwright.sync_api")
PLAYWRIGHT_SYNC.sync_playwright = object
with mock.patch.dict(
    sys.modules,
    {"playwright": PLAYWRIGHT, "playwright.sync_api": PLAYWRIGHT_SYNC},
):
    COLLECTOR = load_module(
        "index_collector_recovery_test",
        "src/20_pipeline/010-collect-index.py",
    )


def record(numero: str, estado: str) -> str:
    return (
        f"NUMERO: {numero}\n"
        f"ESTADO: {estado}\n"
        "DESCRIPCION: Test\n"
        "ENTIDAD: Test\n"
        "DEPENDENCIA: Test\n"
        "FECHA: 13/07/2026\n"
        "MODALIDAD: Test\n"
        f"LINK: https://example.test/{numero}\n"
        "---"
    )


class SnapshotPaginationRecoveryTests(unittest.TestCase):
    def test_pagination_marker_preserves_first_bad_page(self) -> None:
        text = "\n".join([
            "PANAMACOMPRA_MONITOR",
            "URL: cotizaciones-en-linea",
            "Programadas collected: 2",
            "Abiertas collected: 1",
            "",
            "PAGE_COUNTS",
            "Programadas PAGINATION: expected_items=3 expected_pages=2 rows_per_page=50 crawled_items=2 crawled_pages=2 first_bad_page=2 consistent=no",
            "Abiertas PAGINATION: expected_items=1 expected_pages=1 rows_per_page=50 crawled_items=1 crawled_pages=1 first_bad_page=0 consistent=yes",
            "Programadas page 1: 2",
            "Programadas COMPLETE: Next disabled",
            "Abiertas page 1: 1",
            "Abiertas COMPLETE: Next disabled",
            "",
            "RECORDS",
            record("2026-1-1-1-1-CM-1", "Programada"),
            record("2026-1-1-1-1-CM-2", "Programada"),
            record("2026-1-1-1-1-CM-3", "Abierta"),
        ])

        parsed = SNAPSHOT.parse_snapshot(text)

        self.assertFalse(parsed["groups"]["Programadas"]["healthy"])
        self.assertEqual(2, parsed["groups"]["Programadas"]["recovery_page"])
        self.assertTrue(parsed["groups"]["Abiertas"]["healthy"])
        self.assertEqual(3, len(parsed["records"]))

    def test_result_env_exports_recovery_mapping(self) -> None:
        groups = {
            "Programadas": {"healthy": False, "recovery_page": 3},
            "Abiertas": {"healthy": True, "recovery_page": 1},
        }
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            SNAPSHOT, "RESULT_ENV_PATH", Path(tmp) / "result.env"
        ), mock.patch.object(SNAPSHOT, "ensure_dirs"):
            SNAPSHOT.write_result_env("partial", groups=groups)
            result = SNAPSHOT.RESULT_ENV_PATH.read_text(encoding="utf-8")

        self.assertIn("SNAPSHOT_UNHEALTHY_GROUPS='Programadas'", result)
        self.assertIn("SNAPSHOT_RECOVERY_PAGES='Programadas:3'", result)

    def test_collector_start_page_mapping_is_safe_and_case_insensitive(self) -> None:
        starts = COLLECTOR.index_start_pages(
            "programadas:4, ABIERTAS:2,unknown:99,Programadas:not-a-number"
        )
        self.assertEqual({"Programadas": 4, "Abiertas": 2}, starts)


if __name__ == "__main__":
    unittest.main()
