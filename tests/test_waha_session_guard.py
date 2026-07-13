from __future__ import annotations

import importlib.util
import io
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "src/30_notify/010-waha-client.py"
SPEC = importlib.util.spec_from_file_location("waha_client_test", MODULE_PATH)
assert SPEC and SPEC.loader
WAHA = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = WAHA
SPEC.loader.exec_module(WAHA)


class WahaSessionGuardTests(unittest.TestCase):
    def test_starting_is_not_a_qr_state(self) -> None:
        self.assertNotIn("STARTING", WAHA.QR_STATUSES)
        self.assertIn("SCAN_QR_CODE", WAHA.QR_STATUSES)

    def test_starting_flags_unsent_and_never_posts(self) -> None:
        result = {
            "session": "default",
            "status": "STARTING",
            "connected": False,
            "needs_qr": False,
            "dashboard_url": "http://127.0.0.1:3000",
        }
        with (
            mock.patch.object(WAHA, "waha_session_status", return_value=result),
            mock.patch.object(WAHA, "write_waha_warning") as warning,
            mock.patch.object(WAHA, "flag_unsent_message") as unsent,
        ):
            with self.assertRaises(WAHA.WahaSessionNotReadyError):
                WAHA.require_session_ready(
                    base_url="http://127.0.0.1:3000",
                    session="default",
                    api_key="secret",
                    purpose="index",
                    chat_id="group@g.us",
                    text="test",
                )
        warning.assert_called_once()
        unsent.assert_called_once()
        self.assertIn("WAHA_NOT_READY", unsent.call_args.kwargs["reason"])

    def test_qr_state_uses_qr_warning_and_flags_unsent(self) -> None:
        result = {
            "session": "default",
            "status": "SCAN_QR_CODE",
            "connected": False,
            "needs_qr": True,
            "dashboard_url": "http://127.0.0.1:3000",
        }
        with (
            mock.patch.object(WAHA, "waha_session_status", return_value=result),
            mock.patch.object(WAHA, "write_qr_warning") as warning,
            mock.patch.object(WAHA, "flag_unsent_message") as unsent,
        ):
            with self.assertRaises(WAHA.WahaQrRequiredError):
                WAHA.require_session_ready(
                    base_url="http://127.0.0.1:3000",
                    session="default",
                    api_key="secret",
                    purpose="summary",
                    chat_id="group@g.us",
                    text="test",
                )
        warning.assert_called_once_with(result)
        unsent.assert_called_once()
        self.assertIn("WAHA_QR_REQUIRED", unsent.call_args.kwargs["reason"])

    def test_http_error_reason_keeps_waha_detail(self) -> None:
        error = urllib.error.HTTPError(
            "http://127.0.0.1:3000/api/sendText",
            500,
            "Internal Server Error",
            {},
            io.BytesIO(b'{"message":"Cannot read getChat"}'),
        )
        self.assertIn("Cannot read getChat", WAHA.send_error_reason(error))


if __name__ == "__main__":
    unittest.main()
