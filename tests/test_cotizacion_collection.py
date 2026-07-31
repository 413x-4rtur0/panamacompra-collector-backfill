from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import common


MODULE_PATH = ROOT / "src" / "20_pipeline" / "038-collect-cotizaciones.py"
if "playwright.sync_api" not in sys.modules:
    playwright_module = types.ModuleType("playwright")
    sync_api_module = types.ModuleType("playwright.sync_api")
    sync_api_module.sync_playwright = lambda: None
    playwright_module.sync_api = sync_api_module
    sys.modules["playwright"] = playwright_module
    sys.modules["playwright.sync_api"] = sync_api_module
SPEC = importlib.util.spec_from_file_location("collect_cotizaciones_test", MODULE_PATH)
COLLECTOR = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(COLLECTOR)


class FakePage:
    def __init__(self):
        self.stage = 0
        self.clicked = []

    def evaluate(self, script, argument=None):
        if argument is not None and isinstance(argument, str):
            return 2
        if argument is not None and isinstance(argument, dict):
            self.stage = int(argument["index"]) + 1
            self.clicked.append(self.stage)
            return True
        return 2

    def wait_for_timeout(self, _milliseconds):
        return None


class CotizacionCollectionTests(unittest.TestCase):
    def test_queue_includes_closed_and_cancelled_only(self):
        conn = common.init_db(":memory:")
        try:
            for numero, grupo in (
                ("CLOSED-1", "Closed"),
                ("CANCELLED-1", "Cancelled"),
                ("OPEN-1", "Abiertas"),
            ):
                conn.execute(
                    "INSERT INTO opportunities (numero, grupo, estado, cotizacion_attempts) "
                    "VALUES (?, ?, ?, 0)",
                    (numero, grupo, "Cerrada" if grupo != "Abiertas" else "Abierta"),
                )
            conn.commit()
            rows = COLLECTOR.cotizacion_pending_rows(conn, 0, 3)
            self.assertEqual(
                {"CLOSED-1", "CANCELLED-1"},
                {row["numero"] for row in rows},
            )
        finally:
            conn.close()

    def test_extract_cuadro_merges_visible_tabs_without_duplicates(self):
        page = FakePage()
        original = COLLECTOR.extract_cuadro
        payloads = [
            {"summary": {"Número": "TEST-1"}, "providers": [
                {"name": "Provider A", "items": [
                    {"item_index": 1, "item_descripcion": "Sillas", "precio_unitario": "10"}
                ]}
            ]},
            {"summary": {}, "providers": [
                {"name": "Provider A", "items": [
                    {"item_index": 1, "item_descripcion": "Sillas", "precio_unitario": "10"},
                    {"item_index": 2, "item_descripcion": "Mesas", "precio_unitario": "20"},
                ]}
            ]},
            {"summary": {}, "providers": [
                {"name": "Provider B", "items": [
                    {"item_index": 1, "item_descripcion": "Sillas", "precio_unitario": "11"}
                ]}
            ]},
        ]
        try:
            COLLECTOR.extract_cuadro = lambda _page: payloads[page.stage]
            result = COLLECTOR.extract_cuadro_all_tabs(page)
        finally:
            COLLECTOR.extract_cuadro = original

        self.assertEqual(result["tabs_visited"], 2)
        self.assertEqual(result["summary"]["Número"], "TEST-1")
        self.assertEqual(
            [(p["name"], len(p["items"])) for p in result["providers"]],
            [("Provider A", 2), ("Provider B", 1)],
        )


if __name__ == "__main__":
    unittest.main()
