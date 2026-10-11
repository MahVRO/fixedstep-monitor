from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from fixedstep_monitor.metrics import (
    account_summary,
    drawdown_metrics,
    equity_with_drawdown,
    period_realized_pnl,
    trade_statistics,
)
from fixedstep_monitor.storage import MonitorStore


class StorageTests(unittest.TestCase):
    def test_wal_dedupe_and_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "monitor.sqlite3"
            snapshot = {
                "observed_at": "2026-10-11T10:00:00.000Z",
                "account": {"balance": 100.0, "equity": 99.0, "currency": "EUR"},
                "positions": [{"ticket": 99, "symbol": "FixedStep0.5", "volume": 0.1}],
                "closed_trades": [
                    {
                        "deal_id": 123,
                        "position_id": 456,
                        "exit_time": "2026-10-11T09:00:00.000Z",
                        "net_profit": 2.5,
                    }
                ],
            }
            with MonitorStore(path) as store:
                self.assertEqual(store.journal_mode, "wal")
                first = store.begin_run("2026-10-11T10:00:00.000Z", "first")
                store.record_success(first, snapshot)
                second = store.begin_run("2026-10-11T10:01:00.000Z", "second")
                later = dict(snapshot, observed_at="2026-10-11T10:01:00.000Z")
                later["closed_trades"] = [dict(snapshot["closed_trades"][0], commission=-0.2)]
                store.record_success(second, later)
                self.assertEqual(store.counts()["closed_trades"], 1)
                self.assertEqual(store.counts()["account_samples"], 2)
            with MonitorStore(path) as reopened:
                self.assertEqual(reopened.counts()["closed_trades"], 1)
                self.assertEqual(reopened.closed_trades()[0]["commission"], -0.2)
                self.assertEqual(reopened.latest_positions()[0]["ticket"], 99)


class MetricTests(unittest.TestCase):
    def test_drawdown_periods_and_trade_statistics(self):
        accounts = [
            {"observed_at": "2026-10-09T00:00:00.000Z", "balance": 100.0, "equity": 100.0},
            {"observed_at": "2026-10-10T00:00:00.000Z", "balance": 95.0, "equity": 80.0},
            {"observed_at": "2026-10-11T00:00:00.000Z", "balance": 110.0, "equity": 110.0},
        ]
        trades = [
            {"deal_id": 1, "exit_time": "2026-10-10T10:00:00.000Z", "net_profit": 10.0, "duration_seconds": 60},
            {"deal_id": 2, "exit_time": "2026-10-11T10:00:00.000Z", "net_profit": -5.0, "duration_seconds": 120},
            {"deal_id": 3, "exit_time": "2026-10-11T11:00:00.000Z", "net_profit": 20.0, "duration_seconds": 180},
        ]
        enriched = equity_with_drawdown(accounts)
        self.assertEqual(enriched[1]["drawdown_abs"], 20.0)
        self.assertEqual(drawdown_metrics(accounts)["max_pct"], 20.0)
        periods = period_realized_pnl(trades, now=datetime(2026, 10, 11, 12, tzinfo=UTC))
        self.assertEqual(periods["daily"]["realized_pnl"], 15.0)
        self.assertEqual(periods["all_time"]["realized_pnl"], 25.0)
        stats = trade_statistics(trades)
        self.assertAlmostEqual(stats["win_rate"], 2 / 3 * 100)
        self.assertEqual(stats["profit_factor"], 6.0)
        self.assertEqual(stats["consecutive_wins"], 1)
        self.assertEqual(stats["max_consecutive_wins"], 1)
        self.assertEqual(stats["avg_duration_seconds"], 120.0)
        summary = account_summary(accounts[-1], initial_capital=100.0)
        self.assertEqual(summary["net_return"], 10.0)
        self.assertEqual(summary["net_return_pct"], 10.0)

    def test_win_rate_excludes_breakeven_records(self):
        stats = trade_statistics(
            [
                {"net_profit": 1.0},
                {"net_profit": 2.0},
                {"net_profit": -1.0},
                {"net_profit": 0.0},
            ]
        )
        self.assertAlmostEqual(stats["win_rate"], 2 / 3 * 100)
        self.assertEqual(stats["breakeven"], 1)


if __name__ == "__main__":
    unittest.main()
