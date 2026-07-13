from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "src/20_pipeline/020-notify-whatsapp.py"
SPEC = importlib.util.spec_from_file_location("notify_whatsapp_open_now_test", MODULE_PATH)
assert SPEC and SPEC.loader
NOTIFY = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = NOTIFY
SPEC.loader.exec_module(NOTIFY)


class NotifyWhatsappOpenNowTests(unittest.TestCase):
    def test_programada_to_abierta_uses_open_now_destination(self) -> None:
        row = {
            "pending_status_change": "abierta",
            "last_notified_status": "Programada",
            "detail_json_path": "",
        }
        with (
            mock.patch.object(NOTIFY, "waha_enabled", return_value=True),
            mock.patch.object(NOTIFY, "waha_destination", return_value=True),
            mock.patch.object(NOTIFY, "fetch_row", return_value=row),
            mock.patch.object(NOTIFY, "load_detail_summary", return_value={}),
            mock.patch.object(NOTIFY, "allowed_match_line_for", return_value="Sin filtro (todas las entradas)"),
            mock.patch.object(NOTIFY, "build_status_change_message", return_value="test message"),
            mock.patch.object(NOTIFY, "send_text", return_value=True) as send_text,
            mock.patch.object(NOTIFY, "export_record_calendar", return_value=""),
            mock.patch.object(NOTIFY, "mark_snapshot"),
            mock.patch.object(NOTIFY, "clear_status_change"),
        ):
            sent = NOTIFY.notify_status_change(mock.Mock(), "2026-1-1-1-1-CM-1")

        self.assertTrue(sent)
        self.assertEqual("open_now", send_text.call_args.kwargs["purpose"])


if __name__ == "__main__":
    unittest.main()
