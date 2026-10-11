from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fixedstep_monitor.collector import Collector, CollectorConfig
from fixedstep_monitor.exporter import StaticExporter, _compact_equity_history
from fixedstep_monitor.storage import MonitorStore


NOW = datetime(2026, 10, 11, 10, 2, tzinfo=UTC)


def populated_store(path: Path) -> MonitorStore:
    store = MonitorStore(path)
    store.set_metadata("initial_capital", 100.0)
    store.set_metadata("trade_scope", {"filtered": True, "symbols": ["FixedStep0.5"]})
    first = store.begin_run("2026-10-11T10:00:00.000Z", "run-1")
    store.record_success(
        first,
        {
            "observed_at": "2026-10-11T10:00:00.000Z",
            "account": {
                "balance": 105.0,
                "equity": 104.0,
                "unrealized_pnl": -1.0,
                "currency": "EUR",
            },
            "positions": [
                {
                    "ticket": 123456,
                    "magic": 99,
                    "symbol": "FixedStep0.5",
                    "direction": "buy",
                    "volume": 0.1,
                    "entry_time": "2026-10-11T09:30:00.000Z",
                    "entry_price": 10.0,
                    "stop_loss": 9.0,
                    "take_profit": 11.0,
                }
            ],
            "closed_trades": [
                {
                    "deal_id": 700,
                    "order_id": 701,
                    "position_id": 702,
                    "magic": 99,
                    "symbol": "FixedStep0.5",
                    "direction": "buy",
                    "volume": 0.1,
                    "entry_time": "2026-10-11T08:00:00.000Z",
                    "exit_time": "2026-10-11T09:00:00.000Z",
                    "entry_price": 9.0,
                    "exit_price": 10.0,
                    "commission": -0.1,
                    "swap": -0.05,
                    "net_profit": 4.85,
                    "duration_seconds": 3600.0,
                }
            ],
        },
        strategy={
            "observed_at": "2026-10-11T10:00:00.000Z",
            "status": "running",
            "symbol": "FixedStep0.5",
            "step": 2,
            "internal_state": {
                "mode": "observe",
                "password": "must-not-publish",
                "config_path": r"C:\private\strategy.json",
            },
            "events": [
                {
                    "event_id": "private-event-id",
                    "timestamp": "2026-10-11T09:45:00.000Z",
                    "type": "step_change",
                    "message": "Step moved to 2",
                    "server": "private-broker",
                }
            ],
        },
    )
    second = store.begin_run("2026-10-11T10:01:00.000Z", "run-2")
    store.record_success(
        second,
        {
            "observed_at": "2026-10-11T10:01:00.000Z",
            "account": {"balance": 105.0, "equity": 103.0, "unrealized_pnl": -2.0, "currency": "EUR"},
            "positions": [],
            "closed_trades": [],
        },
        strategy={"observed_at": "2026-10-11T10:01:00.000Z", "status": "running", "symbol": "FixedStep0.5", "step": 3},
    )
    return store


class ExporterTests(unittest.TestCase):
    def test_public_equity_compaction_preserves_recent_and_last_observed_buckets(self):
        samples = [
            {"observed_at": "2026-10-10T10:00:00.000Z", "equity": 1.0},
            {"observed_at": "2026-10-10T10:01:00.000Z", "equity": 2.0},
            {"observed_at": "2026-10-01T10:05:00.000Z", "equity": 3.0},
            {"observed_at": "2026-10-01T10:55:00.000Z", "equity": 4.0},
            {"observed_at": "2026-06-01T08:00:00.000Z", "equity": 5.0},
            {"observed_at": "2026-06-01T20:00:00.000Z", "equity": 6.0},
        ]
        compacted = _compact_equity_history(samples, now=NOW)
        self.assertEqual([row["equity"] for row in compacted], [6.0, 4.0, 1.0, 2.0])

    def test_stable_public_contract_no_script_hashes_and_sanitization(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = populated_store(root / "monitor.sqlite3")
            try:
                exporter = StaticExporter(store, root / "docs", clock=lambda: NOW)
                latest = exporter.export()

                required = (
                    "latest.json",
                    "trades.json",
                    "equity.json",
                    "statistics.json",
                    "strategy.json",
                )
                payloads = [json.loads((root / "docs" / "ai" / "data" / name).read_text(encoding="utf-8")) for name in required]
                self.assertTrue(all(payload["snapshot_id"] == latest["snapshot_id"] for payload in payloads))
                self.assertEqual(latest["collection"]["status"], "live")
                self.assertEqual(latest["account"]["net_return"], 3.0)
                self.assertEqual(latest["counts"]["open_positions"], 0)
                self.assertEqual(latest["links"]["latest_json"], "https://mahvro.github.io/fixedstep-monitor/ai/data/latest.json")
                self.assertEqual(latest["scope"]["account"], "account-wide MT5 account observations")

                public_text = "\n".join(path.read_text(encoding="utf-8") for path in (root / "docs" / "ai").rglob("*.*"))
                for forbidden in ("123456", '"deal_id"', '"order_id"', '"position_id"', '"magic"', "must-not-publish", r"C:\private", "private-broker"):
                    self.assertNotIn(forbidden, public_text)
                index = (root / "docs" / "ai" / "index.html").read_text(encoding="utf-8").lower()
                self.assertNotIn("<script", index)
                self.assertIn("https://mahvro.github.io/fixedstep-monitor/ai/manifest.json", index)
                self.assertTrue((root / "docs" / "ai" / "charts" / "equity-balance.svg").exists())
                self.assertTrue((root / "docs" / "ai" / "charts" / "step-progression.svg").exists())
                self.assertTrue((root / "docs" / "ai" / "charts" / "trade-entry-exit.svg").exists())

                manifest = json.loads((root / "docs" / "ai" / "manifest.json").read_text(encoding="utf-8"))
                latest_entry = next(item for item in manifest["datasets"] if item["url"].endswith("/latest.json"))
                latest_bytes = (root / "docs" / "ai" / "data" / "latest.json").read_bytes()
                self.assertEqual(latest_entry["sha256"], hashlib.sha256(latest_bytes).hexdigest())
                self.assertEqual(latest_entry["size_bytes"], len(latest_bytes))
                self.assertIn("available_fields", latest_entry)
                self.assertFalse(list((root / "docs" / "ai").rglob("*.tmp")))
            finally:
                store.close()

    def test_disconnected_export_preserves_last_good_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = populated_store(root / "monitor.sqlite3")
            try:
                failed = store.begin_run("2026-10-11T10:03:00.000Z", "failed")
                store.record_failure(failed, r"RuntimeError: C:\private\terminal server 10.0.0.1")
                latest = StaticExporter(
                    store,
                    root / "docs",
                    clock=lambda: datetime(2026, 10, 11, 10, 4, tzinfo=UTC),
                ).export()
                self.assertEqual(latest["collection"]["status"], "disconnected")
                self.assertEqual(latest["collection"]["error"], "source_unavailable")
                self.assertEqual(latest["account"]["equity"], 103.0)
                equity = json.loads((root / "docs" / "ai" / "data" / "equity.json").read_text(encoding="utf-8"))
                self.assertEqual(len(equity["samples"]), 2)
            finally:
                store.close()

    def test_no_data_omits_unknown_values_instead_of_zeroing_them(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with MonitorStore(root / "monitor.sqlite3") as store:
                latest = StaticExporter(store, root / "docs", clock=lambda: NOW).export()
            self.assertEqual(latest["collection"]["status"], "no_data")
            self.assertEqual(latest["account"], {})
            self.assertEqual(latest["counts"], {})
            self.assertEqual(latest["periods"], {})
            self.assertNotIn("source_observed_at", latest)


class FakeAdapter:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.closed = False

    @property
    def scope(self):
        return {"filtered": True, "symbols": ["FixedStep0.5"], "magic_filter_configured": False}

    def connect(self):
        if self.fail:
            raise RuntimeError(r"private failure C:\terminal")

    def collect(self, *, history_start=None):
        return {
            "observed_at": "2026-10-11T10:00:00.000Z",
            "account": {"balance": 50.0, "equity": 50.0, "currency": "EUR"},
            "positions": [],
            "closed_trades": [],
        }

    def close(self):
        self.closed = True


class CollectorTests(unittest.TestCase):
    def test_empty_trade_history_uses_last_scan_high_water_mark(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with MonitorStore(root / "monitor.sqlite3") as store:
                exporter = StaticExporter(store, root / "docs", clock=lambda: NOW)
                collector = Collector(
                    store,
                    exporter,
                    lambda: FakeAdapter(),
                    config=CollectorConfig(interval_seconds=60, history_overlap_hours=24),
                    clock=lambda: NOW,
                )
                collector.run_once()
                self.assertEqual(store.get_metadata("history_scanned_through"), "2026-10-11T10:00:00.000Z")
                next_start = collector._history_start(NOW + timedelta(minutes=1))
                self.assertEqual(next_start, datetime(2026, 10, 10, 10, 0, tzinfo=UTC))

    def test_success_then_failure_is_restart_safe_and_exports_disconnected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with MonitorStore(root / "monitor.sqlite3") as store:
                exporter = StaticExporter(store, root / "docs", clock=lambda: NOW)
                collector = Collector(
                    store,
                    exporter,
                    lambda: FakeAdapter(),
                    config=CollectorConfig(interval_seconds=60),
                    clock=lambda: NOW,
                )
                self.assertEqual(collector.run_once()["collection"]["status"], "live")
                failed = Collector(
                    store,
                    exporter,
                    lambda: FakeAdapter(fail=True),
                    config=CollectorConfig(interval_seconds=60),
                    clock=lambda: datetime(2026, 10, 11, 10, 3, tzinfo=UTC),
                )
                latest = failed.run_once()
                self.assertEqual(latest["collection"]["status"], "disconnected")
                self.assertEqual(latest["account"]["balance"], 50.0)
                self.assertNotIn("terminal", json.dumps(latest).lower())
            with MonitorStore(root / "monitor.sqlite3") as reopened:
                self.assertEqual(reopened.counts()["account_samples"], 1)


if __name__ == "__main__":
    unittest.main()
