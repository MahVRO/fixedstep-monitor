from __future__ import annotations

import ast
import unittest
from collections import namedtuple
from datetime import UTC, datetime
from pathlib import Path

from fixedstep_monitor.mt5_readonly import (
    QUOTE_READ_METHODS,
    READ_METHODS,
    QuoteOnlyMT5,
    ReadOnlyMT5,
    ReadOnlyMT5Error,
)


def record(**values):
    row = namedtuple("Row", values.keys())
    return row(**values)


class FakeMT5:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def initialize(self):
        self.calls.append("initialize")
        return True

    def shutdown(self):
        self.calls.append("shutdown")

    def terminal_info(self):
        self.calls.append("terminal_info")
        return record(connected=True, path=r"C:\private\terminal", community_account=True)

    def account_info(self):
        self.calls.append("account_info")
        return record(
            login=123456,
            server="private-broker",
            balance=100.0,
            equity=104.0,
            profit=4.0,
            margin=10.0,
            margin_free=94.0,
            margin_level=1040.0,
            currency="EUR",
        )

    def positions_get(self):
        self.calls.append("positions_get")
        return (
            record(ticket=11, symbol="FixedStep0.5", magic=7, type=0, volume=0.1, price_open=10.0, price_current=10.5, sl=9.0, tp=11.0, profit=0.5, swap=-0.1, time=1_700_000_000, time_msc=1_700_000_000_000),
            record(ticket=12, symbol="Other", magic=7, type=1, volume=1.0, price_open=2.0, price_current=1.0, sl=0.0, tp=0.0, profit=1.0, swap=0.0, time=1_700_000_000, time_msc=1_700_000_000_000),
        )

    def history_deals_get(self, start, end):
        self.calls.append("history_deals_get")
        return (
            record(ticket=20, order=30, position_id=40, symbol="FixedStep0.5", magic=7, entry=0, type=0, volume=0.1, price=10.0, profit=0.0, commission=-0.1, swap=0.0, fee=0.0, time=1_700_000_000, time_msc=1_700_000_000_000),
            record(ticket=21, order=31, position_id=40, symbol="FixedStep0.5", magic=7, entry=1, type=1, volume=0.1, price=11.0, profit=1.0, commission=-0.1, swap=-0.05, fee=0.0, time=1_700_000_060, time_msc=1_700_000_060_000),
            record(ticket=22, order=32, position_id=41, symbol="Other", magic=7, entry=1, type=1, volume=1.0, price=5.0, profit=99.0, commission=0.0, swap=0.0, fee=0.0, time=1_700_000_060, time_msc=1_700_000_060_000),
        )

    def history_orders_get(self, start, end):
        self.calls.append("history_orders_get")
        return (
            record(ticket=31, position_id=40, symbol="FixedStep0.5", magic=7, sl=9.0, tp=11.5),
        )


class FakeQuoteMT5:
    def __init__(self, *, ask=5973.8, last=0.0) -> None:
        self.calls: list[str] = []
        self.ask = ask
        self.last = last

    def initialize(self):
        self.calls.append("initialize")
        return True

    def shutdown(self):
        self.calls.append("shutdown")

    def terminal_info(self):
        self.calls.append("terminal_info")
        return record(connected=True, path=r"C:\private\terminal")

    def symbol_info(self, symbol):
        self.calls.append("symbol_info")
        return record(name=symbol, digits=1, point=0.1, trade_tick_size=0.1)

    def symbol_info_tick(self, symbol):
        self.calls.append("symbol_info_tick")
        return record(
            time=1_791_725_274,
            time_msc=1_791_725_274_368,
            bid=5973.3,
            ask=self.ask,
            last=self.last,
        )


class ReadOnlyMT5Tests(unittest.TestCase):
    def test_collects_only_allowlisted_symbol_without_identity_fields_from_account(self):
        module = FakeMT5()
        adapter = ReadOnlyMT5(
            module,
            symbols=("FixedStep0.5",),
            magic_numbers=(7,),
            clock=lambda: datetime(2026, 10, 11, 12, 0, tzinfo=UTC),
        )
        adapter.connect()
        snapshot = adapter.collect(history_start=datetime(2020, 1, 1, tzinfo=UTC))
        adapter.close()

        self.assertEqual(snapshot["account"]["balance"], 100.0)
        self.assertNotIn("login", snapshot["account"])
        self.assertNotIn("server", snapshot["account"])
        self.assertEqual(len(snapshot["positions"]), 1)
        self.assertEqual(len(snapshot["closed_trades"]), 1)
        self.assertEqual(snapshot["closed_trades"][0]["stop_loss"], 9.0)
        self.assertEqual(snapshot["closed_trades"][0]["take_profit"], 11.5)
        self.assertEqual(snapshot["closed_trades"][0]["net_profit"], 0.85)
        self.assertEqual(set(module.calls), READ_METHODS)

    def test_source_ast_uses_only_read_allowlist_and_has_no_execution_call(self):
        package = Path(__file__).parents[1] / "src" / "fixedstep_monitor"
        forbidden = {
            "order_send",
            "order_check",
            "order_modify",
            "order_delete",
            "position_close",
            "position_modify",
            "close_position",
        }
        seen_allowlist_calls: set[str] = set()
        for source in package.glob("*.py"):
            tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    self.assertNotIn(node.func.attr, forbidden, str(source))
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "_call"
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)
                ):
                    seen_allowlist_calls.add(node.args[0].value)
        self.assertLessEqual(seen_allowlist_calls, READ_METHODS | QUOTE_READ_METHODS)
        self.assertEqual(seen_allowlist_calls, READ_METHODS | QUOTE_READ_METHODS)

    def test_quote_only_adapter_uses_exact_allowlist_and_treats_zero_last_as_missing(self):
        self.assertEqual(
            QUOTE_READ_METHODS,
            {
                "initialize",
                "shutdown",
                "terminal_info",
                "symbol_info",
                "symbol_info_tick",
            },
        )
        module = FakeQuoteMT5(last=0.0)
        adapter = QuoteOnlyMT5(
            module,
            symbol="FixedStep0.5",
            clock=lambda: datetime(2026, 10, 11, 12, 0, tzinfo=UTC),
        )
        adapter.connect()
        snapshot = adapter.collect()
        adapter.close()

        quote = snapshot["quote"]
        self.assertEqual(quote["symbol"], "FixedStep0.5")
        self.assertEqual(quote["bid"], 5973.3)
        self.assertEqual(quote["ask"], 5973.8)
        self.assertAlmostEqual(quote["mid"], 5973.55)
        self.assertAlmostEqual(quote["spread"], 0.5)
        self.assertAlmostEqual(quote["spread_points"], 5.0)
        self.assertNotIn("last", quote)
        self.assertEqual(set(module.calls), QUOTE_READ_METHODS)

    def test_quote_only_adapter_rejects_crossed_or_missing_quote(self):
        adapter = QuoteOnlyMT5(FakeQuoteMT5(ask=5973.2))
        adapter.connect()
        with self.assertRaisesRegex(ReadOnlyMT5Error, "ask below bid"):
            adapter.collect()
        adapter.close()
        with self.assertRaisesRegex(ValueError, "exact FixedStep0.5"):
            QuoteOnlyMT5(FakeQuoteMT5(), symbol="Other")


if __name__ == "__main__":
    unittest.main()
