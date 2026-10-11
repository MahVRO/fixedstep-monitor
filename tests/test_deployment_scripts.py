from __future__ import annotations

import json
import unittest
from pathlib import Path


class DeploymentScriptTests(unittest.TestCase):
    def test_installer_uses_distinct_quote_runtime_and_fifteen_second_default(self):
        root = Path(__file__).resolve().parents[1]
        installer = (root / "scripts" / "install_scheduled_task.ps1").read_text(
            encoding="utf-8"
        )
        config = json.loads((root / "config.example.json").read_text(encoding="utf-8"))

        self.assertIn("'runtime\\fixedstep-quotes.sqlite3'", installer)
        self.assertIn("[string]$TaskName = 'FixedStep0.5 Quote Monitor'", installer)
        self.assertIn("[ValidateRange(15, 60)]", installer)
        self.assertIn("[int]$IntervalSeconds = 15", installer)
        self.assertEqual(
            config["database_path"], "runtime/fixedstep-quotes.sqlite3"
        )
        self.assertEqual(config["collection_interval_seconds"], 15)

    def test_runner_uses_quote_live_and_a_distinct_stop_file(self):
        root = Path(__file__).resolve().parents[1]
        runner = (root / "scripts" / "run_monitor.ps1").read_text(encoding="utf-8")

        self.assertIn("'-m', 'fixedstep_monitor', 'quote-live'", runner)
        self.assertIn("'stop-quote-monitor'", runner)
        self.assertNotIn("'stop-monitor'", runner)
        self.assertIn("[int]$IntervalSeconds = 15", runner)
        self.assertIn("[int]$PublishIntervalSeconds = 600", runner)

    def test_runner_passes_a_bounded_git_timeout_through_the_environment(self):
        root = Path(__file__).resolve().parents[1]
        runner = (root / "scripts" / "run_monitor.ps1").read_text(encoding="utf-8")

        self.assertIn("[int]$GitTimeoutSeconds = 120", runner)
        self.assertIn(
            "$env:FIXEDSTEP_MONITOR_GIT_TIMEOUT_SECONDS = [string]$GitTimeoutSeconds",
            runner,
        )


if __name__ == "__main__":
    unittest.main()
