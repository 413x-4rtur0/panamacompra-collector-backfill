from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "src/40_monitor/002b-next-run-timer-cli.py"
SPEC = importlib.util.spec_from_file_location("next_run_cli_timer_test", MODULE_PATH)
assert SPEC and SPEC.loader
CLI = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = CLI
SPEC.loader.exec_module(CLI)

WEB_MODULE_PATH = ROOT / "src/40_monitor/001b-monitor-web.py"


class NextRunCliTimerTests(unittest.TestCase):
    def test_importing_timer_core_does_not_load_tkinter(self) -> None:
        code = (
            "import runpy,sys; "
            f"runpy.run_path({str(CLI.MODULE_PATH)!r}); "
            "print('yes' if 'tkinter' in sys.modules else 'no')"
        )
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
        self.assertEqual("no", result.stdout.strip())

    def test_snapshot_uses_shared_schedule_and_archive_helpers(self) -> None:
        target = datetime.now() + timedelta(minutes=5)
        with (
            mock.patch.object(CLI.CORE, "next_run_schedule", return_value=(target, "changedetection API", True)),
            mock.patch.object(CLI.CORE, "progress_values", return_value={"PHASE": "IDLE"}),
            mock.patch.object(CLI.CORE, "last_summary_values", return_value={"TOTAL_TEXT": "2m"}),
            mock.patch.object(CLI.CORE, "archive_snapshot", return_value=(9, {"saved": 7, "pending": 2, "failed": 0}, [])),
            mock.patch.object(CLI.CORE, "is_live_run_active", return_value=False),
            mock.patch.object(CLI.CORE, "git_branch", return_value="main"),
            mock.patch.object(CLI.CORE, "queue_text", return_value="Queue: collector none · update none"),
        ):
            snapshot = CLI.timer_snapshot()

        self.assertEqual(target, snapshot["target"])
        self.assertTrue(snapshot["authoritative"])
        self.assertEqual(9, snapshot["archive_total"])
        self.assertIn("changedetection API", CLI.render(snapshot))
        payload = CLI.json_payload(snapshot)
        self.assertEqual(CLI.CORE.AUTORUN_SOURCE, payload["autorun_source"])
        self.assertIn("progress", payload)
        self.assertIn("summary", payload)

    def test_desktop_and_direct_terminal_use_independent_singletons(self) -> None:
        self.assertNotEqual(CLI.lock_path("desktop"), CLI.lock_path("terminal"))
        self.assertEqual("panamacompra_cli_timer_desktop.lock", CLI.lock_path("desktop").name)

    def test_refresh_updates_only_changed_terminal_lines(self) -> None:
        first_updates, first_lines = CLI.changed_line_updates("static\ncountdown 10", [])
        second_updates, second_lines = CLI.changed_line_updates("static\ncountdown 09", first_lines)

        self.assertIn("static", first_updates)
        self.assertNotIn("static", second_updates)
        self.assertIn("\033[2;1H", second_updates)
        self.assertIn("countdown 09", second_updates)
        self.assertEqual(["static", "countdown 09"], second_lines)

    def test_other_cli_monitor_also_uses_changed_line_rendering(self) -> None:
        monitor = (ROOT / "src/40_monitor/001c-monitor-terminal.sh").read_text(encoding="utf-8")
        show_screen = monitor.split("show_screen() {", 1)[1].split("\n}\n\nenter_screen", 1)[0]
        self.assertIn("PREVIOUS_SCREEN_LINES", show_screen)
        self.assertIn("\\033[2K", show_screen)
        self.assertNotIn("\\033[H\\033[2J%s", show_screen)

    def test_timer_default_layout_matches_compact_terminal_geometry(self) -> None:
        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertIn('PC_NEXT_RUN_CLI_REFRESH_SECONDS", "2"', source)
        self.assertIn('PC_NEXT_RUN_CLI_DATA_REFRESH_SECONDS", "60"', source)
        snapshot = {
            "target": datetime.now() + timedelta(minutes=5),
            "countdown": "05:00",
            "source": "changedetection API",
            "authoritative": True,
            "progress": {},
            "summary": {},
            "archive_counts": {},
            "archive_total": 0,
            "latest": [],
            "active": False,
            "branch": "main",
            "queue": "Queue empty",
            "duration": "Last duration —",
        }
        with mock.patch.object(CLI, "LATEST_RECORDS", 4):
            self.assertEqual(22, len(CLI.render(snapshot).splitlines()))

    def test_changedetection_auto_forces_cli_monitor_and_timer(self) -> None:
        opener = ROOT / "src/40_monitor/000-open-monitor.sh"
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env.update({
                "PC_STATE_DIR": tmp,
                "PC_DATA_DIR": str(Path(tmp) / "data"),
                "PC_LOG_DIR": str(Path(tmp) / "data/logs"),
                "PC_RUN_MODE": "AUTO",
                "PC_AUTORUN_SOURCE": "changedetection",
                "PC_NEXT_RUN_TIMER": "0",
                "PC_MONITOR_RESOLVE_ONLY": "1",
            })
            env.pop("PC_MONITOR_MODE", None)
            env.pop("PC_NEXT_RUN_TIMER_MODE", None)
            result = subprocess.run(
                [str(opener)], cwd=ROOT, env=env, capture_output=True, text=True, check=True
            )
        self.assertIn("MONITOR_MODE=terminal", result.stdout)
        self.assertIn("NEXT_RUN_TIMER=1", result.stdout)
        self.assertIn("NEXT_RUN_TIMER_MODE=cli", result.stdout)

    def test_manual_tk_monitor_defaults_to_tk_timer(self) -> None:
        opener = ROOT / "src/40_monitor/000-open-monitor.sh"
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env.update({
                "PC_STATE_DIR": tmp,
                "PC_DATA_DIR": str(Path(tmp) / "data"),
                "PC_LOG_DIR": str(Path(tmp) / "data/logs"),
                "PC_MONITOR_MODE": "tk",
                "PC_MONITOR_RESOLVE_ONLY": "1",
            })
            env.pop("PC_RUN_MODE", None)
            env.pop("PC_NEXT_RUN_TIMER_MODE", None)
            result = subprocess.run(
                [str(opener)], cwd=ROOT, env=env, capture_output=True, text=True, check=True
            )
        self.assertIn("MONITOR_MODE=tk", result.stdout)
        self.assertIn("NEXT_RUN_TIMER_MODE=tk", result.stdout)

    def test_web_monitor_embeds_the_shared_timer_and_restored_renderer(self) -> None:
        code = (
            "import importlib.util,sys; "
            f"p={str(WEB_MODULE_PATH)!r}; "
            "s=importlib.util.spec_from_file_location('timer_web_test',p); "
            "m=importlib.util.module_from_spec(s); sys.modules[s.name]=m; s.loader.exec_module(m); "
            "t=m.web_timer_payload(); "
            "print('<script>' in m.HTML, 'function render(data)' in m.HTML, "
            "'web-timer-countdown' in m.HTML, 'target' in t, 'countdown' in t)"
        )
        env = os.environ.copy()
        env["PC_AUTORUN_SOURCE"] = "cron"
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=True, env=env, cwd=ROOT
        )
        self.assertEqual("True True True True True", result.stdout.strip())


if __name__ == "__main__":
    unittest.main()
