from __future__ import annotations

import json
import unittest
from pathlib import Path


class DeploymentScriptTests(unittest.TestCase):
    def test_installer_uses_distinct_quote_runtime_and_ten_second_default(self):
        root = Path(__file__).resolve().parents[1]
        installer = (root / "scripts" / "install_scheduled_task.ps1").read_text(
            encoding="utf-8"
        )
        config = json.loads((root / "config.example.json").read_text(encoding="utf-8"))

        self.assertIn("'runtime\\fixedstep-quotes.sqlite3'", installer)
        self.assertIn("[string]$TaskName = 'FixedStep0.5 Quote Monitor'", installer)
        self.assertIn("[ValidateRange(10, 60)]", installer)
        self.assertIn("[int]$IntervalSeconds = 10", installer)
        self.assertEqual(
            config["database_path"], "runtime/fixedstep-quotes.sqlite3"
        )
        self.assertEqual(config["api_data_dir"], "runtime/quote-api")
        self.assertIn("'runtime\\quote-api'", installer)
        self.assertIn('/v1/latest"', installer)
        self.assertIn('/v1/quotes"', installer)
        self.assertEqual(config["collection_interval_seconds"], 10)

    def test_runner_uses_quote_live_and_a_distinct_stop_file(self):
        root = Path(__file__).resolve().parents[1]
        runner = (root / "scripts" / "run_monitor.ps1").read_text(encoding="utf-8")

        self.assertIn("'-m', 'fixedstep_monitor', 'quote-live'", runner)
        self.assertIn("'stop-quote-monitor'", runner)
        self.assertNotIn("'stop-monitor'", runner)
        self.assertIn("[int]$IntervalSeconds = 10", runner)
        self.assertIn("[int]$PublishIntervalSeconds = 600", runner)
        self.assertIn("'--api-data-dir', $ApiDataDir", runner)
        self.assertIn("[Math]::Max(45, $IntervalSeconds * 3)", runner)

    def test_realtime_credential_is_dpapi_protected_and_never_a_task_argument(self):
        root = Path(__file__).resolve().parents[1]
        installer = (root / "scripts" / "install_scheduled_task.ps1").read_text(
            encoding="utf-8"
        )
        runner = (root / "scripts" / "run_monitor.ps1").read_text(encoding="utf-8")
        saver = (root / "scripts" / "save_realtime_secret.ps1").read_text(
            encoding="utf-8"
        )

        self.assertIn("[string]$RealtimeIngestUrl", installer)
        self.assertIn("[string]$RealtimeTokenPath", installer)
        self.assertIn("existing DPAPI-protected token file", installer)
        self.assertIn("Realtime publication requires IntervalSeconds 10.", installer)
        self.assertIn("[Uri]::TryCreate", installer)
        self.assertIn("$realtimeUri.AbsolutePath -ne '/v1/ingest'", installer)
        self.assertIn("fixedstep-quote-relay.fixedstep-quote-relay.workers.dev", installer)
        self.assertIn("valid token decryptable by the current Windows user", installer)
        self.assertIn("ConvertTo-SecureString -String $protectedToken", installer)
        self.assertIn("ZeroFreeBSTR", installer)
        self.assertIn("'-RealtimeTokenPath'", installer)
        self.assertNotIn("'-RealtimeToken'", installer)
        self.assertNotIn("FIXEDSTEP_REALTIME_TOKEN", installer)

        self.assertIn("ConvertTo-SecureString -String $protectedToken", runner)
        self.assertIn("SecureStringToBSTR", runner)
        self.assertIn("ZeroFreeBSTR", runner)
        self.assertIn("$env:FIXEDSTEP_REALTIME_TOKEN = $realtimeToken", runner)
        self.assertIn("$env:FIXEDSTEP_REALTIME_TOKEN = $priorRealtimeToken", runner)
        self.assertIn("'--realtime-ingest-url', $RealtimeIngestUrl", runner)
        self.assertNotIn("'--realtime-token'", runner)
        self.assertIn(
            "realtime_publish_disabled category=ProtectedTokenUnavailable", runner
        )

        self.assertIn("[Security.SecureString]$Secret", saver)
        self.assertIn("ConvertFrom-SecureString -SecureString $Secret", saver)
        self.assertIn("32-256 visible ASCII characters", saver)
        self.assertIn("SetAccessRuleProtection($true, $false)", saver)
        self.assertIn("SecurityIdentifier('S-1-5-18')", saver)

    def test_task_has_logon_start_and_a_five_minute_watchdog(self):
        root = Path(__file__).resolve().parents[1]
        installer = (root / "scripts" / "install_scheduled_task.ps1").read_text(
            encoding="utf-8"
        )

        self.assertIn("New-ScheduledTaskTrigger -AtLogOn", installer)
        self.assertIn("$watchdogTrigger = New-ScheduledTaskTrigger", installer)
        self.assertIn("-RepetitionInterval (New-TimeSpan -Minutes 5)", installer)
        self.assertIn("-Trigger @($logonTrigger, $watchdogTrigger)", installer)
        self.assertIn("-MultipleInstances IgnoreNew", installer)

    def test_runner_passes_a_bounded_git_timeout_through_the_environment(self):
        root = Path(__file__).resolve().parents[1]
        runner = (root / "scripts" / "run_monitor.ps1").read_text(encoding="utf-8")

        self.assertIn("[int]$GitTimeoutSeconds = 120", runner)
        self.assertIn(
            "$env:FIXEDSTEP_MONITOR_GIT_TIMEOUT_SECONDS = [string]$GitTimeoutSeconds",
            runner,
        )

    def test_runner_normalizes_runtime_output_to_bounded_utf8_json_logs(self):
        root = Path(__file__).resolve().parents[1]
        runner = (root / "scripts" / "run_monitor.ps1").read_text(encoding="utf-8")

        self.assertNotIn("*>>", runner)
        self.assertIn("Initialize-Utf8Log", runner)
        self.assertIn("mixed-encoding", runner)
        self.assertIn("ConvertFrom-Json", runner)
        self.assertIn("reason=non_json", runner)
        self.assertIn("if ($line.Length -gt 4096)", runner)
        self.assertIn("-Encoding UTF8", runner)
        self.assertIn("$priorErrorActionPreference = $ErrorActionPreference", runner)
        self.assertIn("$ErrorActionPreference = 'Continue'", runner)
        self.assertIn("runner_pipeline_failed category=", runner)
        self.assertIn("if ($null -eq $LASTEXITCODE)", runner)
        self.assertIn("category=NativeInvocationFailed", runner)
        self.assertIn("$exitCode = $LASTEXITCODE", runner)


if __name__ == "__main__":
    unittest.main()
