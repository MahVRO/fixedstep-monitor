from __future__ import annotations

import json
import unittest
from pathlib import Path


class DeploymentScriptTests(unittest.TestCase):
    def test_installer_database_default_matches_documented_runtime_path(self):
        root = Path(__file__).resolve().parents[1]
        installer = (root / "scripts" / "install_scheduled_task.ps1").read_text(
            encoding="utf-8"
        )
        config = json.loads((root / "config.example.json").read_text(encoding="utf-8"))

        self.assertIn("'runtime\\fixedstep-monitor.sqlite3'", installer)
        self.assertNotIn("'data\\fixedstep-monitor.sqlite3'", installer)
        self.assertEqual(
            config["database_path"], "runtime/fixedstep-monitor.sqlite3"
        )

    def test_publish_failures_wait_for_the_next_attempt_interval(self):
        root = Path(__file__).resolve().parents[1]
        runner = (root / "scripts" / "run_monitor.ps1").read_text(encoding="utf-8")

        self.assertIn("$lastPublishAttempt = [DateTimeOffset]::MinValue", runner)
        due = runner.index("$publishDue =")
        attempted = runner.index(
            "$lastPublishAttempt = [DateTimeOffset]::UtcNow", due
        )
        invoked = runner.index("& $resolvedPython @publishArguments", attempted)
        self.assertLess(due, attempted)
        self.assertLess(attempted, invoked)
        self.assertNotIn("$lastPublish =", runner)

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
