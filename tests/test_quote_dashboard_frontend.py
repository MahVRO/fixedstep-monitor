from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "docs" / "dashboard"


class QuoteDashboardFrontendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = (DASHBOARD / "index.html").read_text(encoding="utf-8")
        cls.javascript = (DASHBOARD / "app.js").read_text(encoding="utf-8")
        cls.styles = (DASHBOARD / "app.css").read_text(encoding="utf-8")

    def test_dashboard_is_quote_only(self) -> None:
        for required in (
            'id="latest-bid"',
            'id="latest-mid"',
            'id="latest-ask"',
            'id="latest-spread"',
            'id="quote-chart"',
            'id="spread-chart"',
        ):
            self.assertIn(required, self.html)

        for removed in (
            "kpi-equity",
            "kpi-balance",
            "drawdown-chart",
            "open-positions",
            "closed-trades",
            "Daily realized",
            "Profit factor",
        ):
            self.assertNotIn(removed, self.html)

    def test_live_poll_and_static_fallback_are_explicit(self) -> None:
        self.assertIn('const POLL_MS = 15_000;', self.javascript)
        self.assertIn('const REQUEST_TIMEOUT_MS = 8_000;', self.javascript)
        self.assertIn('`${LIVE_ORIGIN}/v1/quotes`', self.javascript)
        self.assertIn('`${LIVE_ORIGIN}/v1/latest`', self.javascript)
        self.assertIn('const STATIC_HISTORY_URL = "../ai/data/quotes.json";', self.javascript)
        self.assertIn('cache: "no-store"', self.javascript)
        self.assertIn('credentials: "omit"', self.javascript)
        self.assertIn("incomingCollected > currentCollected", self.javascript)

    def test_contract_and_clock_safe_freshness_are_validated(self) -> None:
        self.assertIn('const SYMBOL = "FixedStep0.5";', self.javascript)
        self.assertIn('const SCHEMA_VERSION = "fixedstep-quotes-v1";', self.javascript)
        self.assertIn("if (!isFiniteNumber(value))", self.javascript)
        self.assertIn("ask < bid", self.javascript)
        self.assertIn("collection.collected_at", self.javascript)
        self.assertIn("collection.collection_age_seconds", self.javascript)
        self.assertIn('raw.tick_at, `${fieldPrefix}.tick_at`, { required: true }', self.javascript)
        self.assertIn('raw.collected_at, `${fieldPrefix}.collected_at`, { required: true }', self.javascript)
        self.assertIn("const receivedMonotonicMs = window.performance.now();", self.javascript)
        self.assertIn("normalized._received_monotonic_ms", self.javascript)
        self.assertIn("window.performance.now() - state.latestReceiptMonotonicMs", self.javascript)
        self.assertIn("mt5_reported_unverified", self.javascript)
        self.assertIn("utc_serialized_host_clock_synchronization_not_asserted", self.javascript)
        self.assertIn("Clock offset detected", self.javascript)
        self.assertIn("Last known live", self.javascript)
        self.assertNotIn("Date.now() - collectedMs", self.javascript)
        self.assertNotIn("Date.now() - quote._tick_ms", self.javascript)
        self.assertNotIn("Recent tick", self.javascript)
        self.assertIn("time basis is unverified", self.html)

    def test_all_collector_time_ranges_are_present(self) -> None:
        for milliseconds in (
            300_000,
            900_000,
            3_600_000,
            21_600_000,
            86_400_000,
            604_800_000,
            2_592_000_000,
            7_776_000_000,
        ):
            self.assertIn(f'data-range-ms="{milliseconds}"', self.html)
        self.assertIn("const MAX_HISTORY_MS = 90 * 24 * 60 * 60 * 1000;", self.javascript)
        self.assertIn("const MAX_HISTORY_SAMPLES = 18_000;", self.javascript)
        self.assertIn("const anchor = timed.reduce", self.javascript)
        self.assertIn("sample._collected_ms >= cutoff", self.javascript)
        self.assertIn("const times = rows.map(row => row._collected_ms);", self.javascript)
        self.assertNotIn("const times = rows.map(row => row._tick_ms);", self.javascript)
        self.assertIn("Collector first-observation host clock", self.html)
        self.assertIn("synchronization and absolute accuracy are not asserted", self.html)

    def test_layout_has_responsive_and_accessible_states(self) -> None:
        self.assertIn('aria-describedby="quote-chart-summary"', self.html)
        self.assertIn('aria-describedby="spread-chart-summary"', self.html)
        self.assertIn("@media (max-width: 620px)", self.styles)
        self.assertIn(".chart-wrap.has-data .empty-chart", self.styles)
        self.assertIn("prefers-reduced-motion", self.styles)


if __name__ == "__main__":
    unittest.main()
