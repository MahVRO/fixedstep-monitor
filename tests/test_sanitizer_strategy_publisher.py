from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fixedstep_monitor.publisher import (
    GIT_TIMEOUT_ENVIRONMENT_VARIABLE,
    MAX_GIT_ERROR_CHARACTERS,
    GitPublisher,
    PublishError,
)
from fixedstep_monitor.sanitizer import sanitize_public_data
from fixedstep_monitor.strategy_state import StrategyStateError, StrategyStateReader


class SanitizerTests(unittest.TestCase):
    def test_removes_sensitive_keys_ids_paths_endpoints_and_tokens(self):
        source = {
            "login": 123,
            "ticket": 456,
            "comment": "broker-private-comment",
            "safe": "value",
            "nested": {
                "server": "broker",
                "message": r"failed at C:\Users\private\file.json",
                "endpoint": "10.0.0.1:443",
                "token_value": "sk-" + "proj-" + "abcdefghijklmnopqrstuvwxyz",
            },
        }
        result = sanitize_public_data(source)
        self.assertEqual(result.value["safe"], "value")
        self.assertNotIn("login", result.value)
        self.assertNotIn("ticket", result.value)
        self.assertNotIn("comment", result.value)
        self.assertNotIn("message", result.value["nested"])
        self.assertNotIn("endpoint", result.value["nested"])
        self.assertNotIn("token_value", result.value["nested"])
        self.assertGreaterEqual(len(result.omissions), 6)


class StrategyReaderTests(unittest.TestCase):
    def test_reads_only_supported_strategy_shape(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.json"
            path.write_text(
                json.dumps(
                    {
                        "status": "running",
                        "active_symbol": "FixedStep0.5",
                        "current_step": 4,
                        "state": {"phase": "wait"},
                        "events": [{"type": "step", "value": 4}],
                        "arbitrary_top_level_secret": "ignored",
                    }
                ),
                encoding="utf-8",
            )
            state = StrategyStateReader(path).read()
            self.assertEqual(state["status"], "running")
            self.assertEqual(state["symbol"], "FixedStep0.5")
            self.assertEqual(state["step"], 4)
            self.assertNotIn("arbitrary_top_level_secret", state)

    def test_invalid_json_is_a_bounded_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.json"
            path.write_text("{", encoding="utf-8")
            with self.assertRaisesRegex(StrategyStateError, "could not be read") as raised:
                StrategyStateReader(path).read()
            self.assertNotIn(str(path), str(raised.exception))


class PublisherTests(unittest.TestCase):
    def test_rolling_snapshot_amends_without_growing_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "docs" / "ai"
            data.mkdir(parents=True)
            (data / "latest.json").write_text('{"version":1}\n', encoding="utf-8")

            def git(*args: str) -> str:
                result = subprocess.run(
                    ["git", *args], cwd=root, text=True, capture_output=True, check=True
                )
                return result.stdout.strip()

            git("init", "-b", "live")
            git("config", "user.name", "Monitor test")
            git("config", "user.email", "monitor-test@example.invalid")
            git("add", "docs/ai")
            git("commit", "-m", "Initial snapshot")
            (data / "latest.json").write_text('{"version":2}\n', encoding="utf-8")
            result = GitPublisher(root).publish(
                commit=True, branch="live", rolling_snapshot=True
            )
            self.assertTrue(result.committed)
            self.assertEqual(git("rev-list", "--count", "HEAD"), "1")
            self.assertEqual(git("show", "HEAD:docs/ai/latest.json"), '{"version":2}')

    def test_push_requires_explicit_configuration(self):
        with tempfile.TemporaryDirectory() as temporary:
            publisher = GitPublisher(temporary)
            publisher._git = Mock()
            with self.assertRaisesRegex(PublishError, "disabled"):
                publisher.push()
            publisher._git.assert_not_called()

    def test_generated_path_cannot_escape_repository(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(ValueError):
                GitPublisher(temporary, generated_path="../outside")

    def test_git_is_noninteractive_and_has_a_configurable_timeout(self):
        completed = subprocess.CompletedProcess(["git", "status"], 0, "ok", "")
        with tempfile.TemporaryDirectory() as temporary, patch(
            "fixedstep_monitor.publisher.subprocess.run", return_value=completed
        ) as run:
            result = GitPublisher(temporary, git_timeout_seconds=7.5)._git("status")

        self.assertIs(result, completed)
        options = run.call_args.kwargs
        self.assertEqual(options["timeout"], 7.5)
        self.assertEqual(options["env"]["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(options["env"]["GCM_INTERACTIVE"], "Never")

    def test_timeout_can_be_configured_by_environment(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ, {GIT_TIMEOUT_ENVIRONMENT_VARIABLE: "9"}
        ):
            publisher = GitPublisher(temporary)
        self.assertEqual(publisher.git_timeout_seconds, 9)

    def test_git_timeout_has_a_bounded_safe_error(self):
        with tempfile.TemporaryDirectory() as temporary, patch(
            "fixedstep_monitor.publisher.subprocess.run",
            side_effect=subprocess.TimeoutExpired(["git", "push"], 3),
        ):
            with self.assertRaisesRegex(
                PublishError, "git operation timed out after 3 seconds"
            ) as raised:
                GitPublisher(temporary, git_timeout_seconds=3)._git("push")
        self.assertLessEqual(len(str(raised.exception)), MAX_GIT_ERROR_CHARACTERS)

    def test_git_failure_redacts_credentials_and_bounds_diagnostic(self):
        credential = "github_pat_" + "A" * 40
        completed = subprocess.CompletedProcess(
            ["git", "push"],
            128,
            "",
            (
                f"fatal: https://monitor:{credential}@github.com/private/repo "
                f"token={credential} "
                + "x" * 2000
            ),
        )
        with tempfile.TemporaryDirectory() as temporary, patch(
            "fixedstep_monitor.publisher.subprocess.run", return_value=completed
        ):
            with self.assertRaises(PublishError) as raised:
                GitPublisher(temporary)._git("push")

        message = str(raised.exception)
        self.assertNotIn(credential, message)
        self.assertIn("<redacted>", message)
        self.assertLessEqual(len(message), MAX_GIT_ERROR_CHARACTERS)


if __name__ == "__main__":
    unittest.main()
