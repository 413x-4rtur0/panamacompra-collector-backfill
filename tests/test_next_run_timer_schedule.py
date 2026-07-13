#!/usr/bin/env python3
from __future__ import annotations

import runpy
import unittest
from datetime import datetime, timezone
from pathlib import Path


MODULE = runpy.run_path(str(Path(__file__).parents[1] / "src/40_monitor/002-next-run-timer.py"))
next_allowed = MODULE["_next_allowed_schedule_time"]


def daily_schedule(start: str = "04:00", hours: int = 18) -> dict[str, object]:
    schedule: dict[str, object] = {"enabled": True, "timezone": "America/Panama"}
    for day in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"):
        schedule[day] = {
            "enabled": True,
            "start_time": start,
            "duration": {"hours": str(hours), "minutes": "0"},
        }
    return schedule


class ChangedetectionScheduleProjectionTests(unittest.TestCase):
    def test_before_window_moves_to_start(self) -> None:
        candidate = datetime(2026, 7, 13, 7, 1, 30, tzinfo=timezone.utc)  # 02:01 Panama
        target, note = next_allowed(candidate, daily_schedule(), "America/Panama")
        self.assertEqual(target, datetime(2026, 7, 13, 9, 0, tzinfo=timezone.utc))
        self.assertIn("04:00–22:00", note)

    def test_inside_window_keeps_interval_candidate(self) -> None:
        candidate = datetime(2026, 7, 13, 17, 30, tzinfo=timezone.utc)  # 12:30 Panama
        target, _note = next_allowed(candidate, daily_schedule(), "America/Panama")
        self.assertEqual(target, candidate)

    def test_after_window_moves_to_next_day(self) -> None:
        candidate = datetime(2026, 7, 14, 4, 0, tzinfo=timezone.utc)  # 23:00 Panama
        target, _note = next_allowed(candidate, daily_schedule(), "America/Panama")
        self.assertEqual(target, datetime(2026, 7, 14, 9, 0, tzinfo=timezone.utc))

    def test_disabled_limit_keeps_candidate(self) -> None:
        candidate = datetime(2026, 7, 13, 7, 1, 30, tzinfo=timezone.utc)
        target, note = next_allowed(candidate, {"enabled": False}, "America/Panama")
        self.assertEqual(target, candidate)
        self.assertEqual(note, "")


if __name__ == "__main__":
    unittest.main()
