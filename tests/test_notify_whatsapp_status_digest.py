from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "src/20_pipeline/020-notify-whatsapp.py"
SPEC = importlib.util.spec_from_file_location("notify_whatsapp_status_digest_test", MODULE_PATH)
assert SPEC and SPEC.loader
NOTIFY = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = NOTIFY
SPEC.loader.exec_module(NOTIFY)


def _row(numero: str) -> dict:
    return {
        "numero": numero,
        "entidad": "Test Entity",
        "fecha": "2026-07-24",
        "finish_date_guess": None,
        "link": "-",
        "detail_json_path": "",
        "pending_status_change": "abierta",
    }


class StatusDigestThresholdTests(unittest.TestCase):
    def test_default_threshold_is_one(self) -> None:
        with (
            mock.patch.object(NOTIFY, "load_client_profiles", return_value=[]),
            mock.patch.object(NOTIFY, "cfg_int", return_value=None),
        ):
            self.assertEqual(1, NOTIFY.status_digest_threshold())

    def test_open_now_scoped_profile_disables_digest(self) -> None:
        with mock.patch.object(
            NOTIFY, "load_client_profiles",
            return_value=[{"purposes": ["open_now"]}],
        ):
            self.assertEqual(0, NOTIFY.status_digest_threshold())

    def test_status_scoped_profile_disables_digest(self) -> None:
        with mock.patch.object(
            NOTIFY, "load_client_profiles",
            return_value=[{"purposes": ["status"]}],
        ):
            self.assertEqual(0, NOTIFY.status_digest_threshold())

    def test_details_only_profile_does_not_disable_digest(self) -> None:
        with (
            mock.patch.object(NOTIFY, "load_client_profiles", return_value=[{"purposes": ["details"]}]),
            mock.patch.object(NOTIFY, "cfg_int", return_value=None),
        ):
            self.assertEqual(1, NOTIFY.status_digest_threshold())


class AnnounceOpenNowDigestTests(unittest.TestCase):
    def test_success_marks_every_record_once(self) -> None:
        rows = [_row("2026-1"), _row("2026-2"), _row("2026-3")]
        with (
            mock.patch.object(NOTIFY, "send_text", return_value=True) as send_text,
            mock.patch.object(NOTIFY, "record_send_outcome") as record_outcome,
            mock.patch.object(NOTIFY, "export_record_calendar", return_value=""),
            mock.patch.object(NOTIFY, "mark_snapshot") as mark_snapshot,
            mock.patch.object(NOTIFY, "clear_status_change") as clear_status,
            mock.patch.object(NOTIFY, "pace_after_send"),
        ):
            sent_messages, announced = NOTIFY.announce_open_now_digest(mock.Mock(), rows)

        self.assertEqual(1, sent_messages)
        self.assertEqual(3, announced)
        self.assertEqual(1, send_text.call_count)
        self.assertEqual("open_now", send_text.call_args.kwargs["purpose"])
        self.assertEqual(3, mark_snapshot.call_count)
        self.assertEqual(3, clear_status.call_count)
        self.assertEqual(3, record_outcome.call_count)
        for call in record_outcome.call_args_list:
            self.assertTrue(call.args[2])  # (conn, numero, True)

    def test_failed_send_leaves_records_unmarked(self) -> None:
        rows = [_row("2026-1"), _row("2026-2")]
        with (
            mock.patch.object(NOTIFY, "send_text", return_value=False),
            mock.patch.object(NOTIFY, "record_send_outcome") as record_outcome,
            mock.patch.object(NOTIFY, "export_record_calendar") as export_calendar,
            mock.patch.object(NOTIFY, "mark_snapshot") as mark_snapshot,
            mock.patch.object(NOTIFY, "clear_status_change") as clear_status,
            mock.patch.object(NOTIFY, "pace_after_send"),
        ):
            sent_messages, announced = NOTIFY.announce_open_now_digest(mock.Mock(), rows)

        self.assertEqual(0, sent_messages)
        self.assertEqual(0, announced)
        export_calendar.assert_not_called()
        mark_snapshot.assert_not_called()
        clear_status.assert_not_called()
        self.assertEqual(2, record_outcome.call_count)
        for call in record_outcome.call_args_list:
            self.assertFalse(call.args[2])


class AnnounceStatusDigestTests(unittest.TestCase):
    def test_only_update_kind_clears_pending_status_flag(self) -> None:
        entries = [
            ("update", _row("2026-1"), "🔄 test"),
            ("status", _row("2026-2"), "🟡 test"),
            ("items", _row("2026-3"), "🔵 test"),
        ]
        with (
            mock.patch.object(NOTIFY, "send_text", return_value=True) as send_text,
            mock.patch.object(NOTIFY, "record_send_outcome"),
            mock.patch.object(NOTIFY, "export_record_calendar", return_value=""),
            mock.patch.object(NOTIFY, "mark_snapshot") as mark_snapshot,
            mock.patch.object(NOTIFY, "clear_status_change") as clear_status,
            mock.patch.object(NOTIFY, "pace_after_send"),
        ):
            sent_messages, announced = NOTIFY.announce_status_digest(mock.Mock(), entries)

        self.assertEqual(1, sent_messages)
        self.assertEqual(3, announced)
        self.assertEqual("status", send_text.call_args.kwargs["purpose"])
        self.assertEqual(3, mark_snapshot.call_count)
        clear_status.assert_called_once_with(mock.ANY, "2026-1")


class BuildStatusDigestMessagesTests(unittest.TestCase):
    def test_chunks_respect_records_per_message(self) -> None:
        entries = [(f"2026-{i}", _row(f"2026-{i}"), f"label {i}") for i in range(5)]
        with mock.patch.object(NOTIFY, "index_digest_records_per_message", return_value=2):
            chunks = NOTIFY.build_status_digest_messages(entries, "🟢 *Test")

        self.assertEqual(3, len(chunks))
        self.assertEqual(2, len(chunks[0][0]))
        self.assertEqual(2, len(chunks[1][0]))
        self.assertEqual(1, len(chunks[2][0]))
        self.assertIn("parte 1/3", chunks[0][1])
        self.assertIn("(5)", chunks[0][1])


if __name__ == "__main__":
    unittest.main()
