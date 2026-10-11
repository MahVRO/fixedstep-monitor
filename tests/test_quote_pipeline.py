from __future__ import annotations

import ast
import hashlib
import inspect
import json
import tempfile
import textwrap
import threading
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import fixedstep_monitor.cli as cli_module
import fixedstep_monitor.quote_exporter as quote_exporter_module
from fixedstep_monitor.cli import _advance_periodic_deadline, build_parser
from fixedstep_monitor.quote_collector import QuoteCollector, QuoteCollectorConfig
from fixedstep_monitor.quote_exporter import (
    MAX_PUBLIC_JSON_BYTES,
    QUOTE_SCHEMA_VERSION,
    QuoteStaticExporter,
    compact_quote_history,
)
from fixedstep_monitor.storage import MonitorStore


NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)


def snapshot(
    collected_at: str,
    tick_at: str,
    tick_time_msc: int,
    *,
    bid: float = 5973.3,
    ask: float = 5973.8,
    last: float | None = None,
) -> dict:
    quote = {
        "symbol": "FixedStep0.5",
        "collected_at": collected_at,
        "tick_at": tick_at,
        "tick_time_msc": tick_time_msc,
        "bid": bid,
        "ask": ask,
        "mid": (bid + ask) / 2,
        "spread": ask - bid,
        "spread_points": (ask - bid) / 0.1,
        "digits": 1,
        "point": 0.1,
        "trade_tick_size": 0.1,
    }
    if last is not None:
        quote["last"] = last
    return {"observed_at": collected_at, "quote": quote}


class QuoteStorageTests(unittest.TestCase):
    def test_duplicate_tick_updates_last_collection_without_inventing_a_sample(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "quotes.sqlite3"
            with MonitorStore(path) as store:
                first = store.begin_run("2026-10-11T10:00:00.000Z", "first")
                store.record_quote_success(
                    first,
                    snapshot(
                        "2026-10-11T10:00:00.000Z",
                        "2026-10-11T09:59:59.000Z",
                        1_791_700_799_000,
                    ),
                )
                second = store.begin_run("2026-10-11T10:00:15.000Z", "second")
                store.record_quote_success(
                    second,
                    snapshot(
                        "2026-10-11T10:00:15.000Z",
                        "2026-10-11T09:59:59.000Z",
                        1_791_700_799_000,
                    ),
                )
                self.assertEqual(store.counts()["quote_samples"], 1)
                self.assertEqual(
                    store.latest_quote()["collected_at"],
                    "2026-10-11T10:00:00.000Z",
                )
                row = store.connection.execute(
                    "SELECT last_collected_at FROM quote_samples"
                ).fetchone()
                self.assertEqual(row["last_collected_at"], "2026-10-11T10:00:15.000Z")
                self.assertEqual(store.last_successful_quote_run()["run_id"], "second")
            with MonitorStore(path) as reopened:
                self.assertEqual(len(reopened.quote_samples()), 1)

    def test_public_window_query_excludes_old_rows_but_count_keeps_them(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "quotes.sqlite3"
            with MonitorStore(path) as store:
                observations = (
                    (NOW - timedelta(days=91), 5900.0),
                    (NOW - timedelta(days=90), 5910.0),
                    (NOW - timedelta(minutes=1), 5920.0),
                )
                for index, (observed, bid) in enumerate(observations):
                    stamp = observed.isoformat(timespec="milliseconds").replace(
                        "+00:00", "Z"
                    )
                    run = store.begin_run(stamp, f"window-{index}")
                    store.record_quote_success(
                        run,
                        snapshot(
                            stamp,
                            (observed + timedelta(hours=3)).isoformat(
                                timespec="milliseconds"
                            ).replace("+00:00", "Z"),
                            int((observed + timedelta(hours=3)).timestamp() * 1000),
                            bid=bid,
                            ask=bid + 0.5,
                        ),
                    )

                cutoff = (NOW - timedelta(days=90)).isoformat(
                    timespec="milliseconds"
                ).replace("+00:00", "Z")
                public_rows = store.quote_samples_for_public_window(
                    public_cutoff=cutoff,
                    minute_cutoff=(NOW - timedelta(days=7)).isoformat(
                        timespec="milliseconds"
                    ).replace("+00:00", "Z"),
                    full_cutoff=(NOW - timedelta(hours=24)).isoformat(
                        timespec="milliseconds"
                    ).replace("+00:00", "Z"),
                )
                self.assertEqual([row["bid"] for row in public_rows], [5910.0, 5920.0])
                self.assertEqual(store.quote_sample_count(), 3)

                query_plan = store.connection.execute(
                    """
                    EXPLAIN QUERY PLAN
                    SELECT tick_at, first_collected_at, last_collected_at, data_json
                    FROM quote_samples
                    WHERE symbol = ? AND first_collected_at >= ?
                    ORDER BY first_collected_at, tick_time_msc
                    """,
                    ("FixedStep0.5", cutoff),
                ).fetchall()
                self.assertIn(
                    "idx_quotes_first_collected_at",
                    " ".join(str(column) for row in query_plan for column in row),
                )

    def test_sql_public_sampling_keeps_exact_cadence_buckets(self):
        with tempfile.TemporaryDirectory() as temporary:
            with MonitorStore(Path(temporary) / "quotes.sqlite3") as store:
                observations = (
                    (NOW - timedelta(days=91), 5800.0),
                    (NOW - timedelta(days=8, minutes=50), 5810.0),
                    (NOW - timedelta(days=8, minutes=10), 5820.0),
                    (NOW - timedelta(days=2, seconds=50), 5830.0),
                    (NOW - timedelta(days=2, seconds=10), 5840.0),
                    (NOW - timedelta(hours=23, seconds=30), 5850.0),
                    (NOW - timedelta(hours=23, seconds=10), 5860.0),
                )
                for index, (observed, bid) in enumerate(observations):
                    stamp = observed.isoformat(timespec="milliseconds").replace(
                        "+00:00", "Z"
                    )
                    run = store.begin_run(stamp, f"bucket-{index}")
                    store.record_quote_success(
                        run,
                        snapshot(
                            stamp,
                            (observed + timedelta(hours=3)).isoformat(
                                timespec="milliseconds"
                            ).replace("+00:00", "Z"),
                            int((observed + timedelta(hours=3)).timestamp() * 1000),
                            bid=bid,
                            ask=bid + 0.5,
                        ),
                    )

                def stamp(delta: timedelta) -> str:
                    return (NOW - delta).isoformat(timespec="milliseconds").replace(
                        "+00:00", "Z"
                    )

                rows = store.quote_samples_for_public_window(
                    public_cutoff=stamp(timedelta(days=90)),
                    minute_cutoff=stamp(timedelta(days=7)),
                    full_cutoff=stamp(timedelta(hours=24)),
                )
                self.assertEqual(
                    [row["bid"] for row in rows],
                    [5820.0, 5840.0, 5850.0, 5860.0],
                )
                self.assertEqual(store.quote_sample_count(), 7)
                exported = QuoteStaticExporter(
                    store,
                    Path(temporary) / "docs",
                    clock=lambda: NOW,
                ).export()
                self.assertEqual(exported["coverage"]["stored_quote_samples"], 7)
                history = json.loads(
                    (
                        Path(temporary)
                        / "docs"
                        / "ai"
                        / "data"
                        / "quotes.json"
                    ).read_text(encoding="utf-8")
                )
                self.assertEqual(len(history["samples"]), 4)


class QuoteExporterTests(unittest.TestCase):
    def _populated(self, store: MonitorStore) -> None:
        run = store.begin_run("2026-10-11T11:59:55.000Z", "quote-run")
        store.record_quote_success(
            run,
            snapshot(
                "2026-10-11T11:59:55.000Z",
                "2026-10-11T14:59:54.368Z",
                1_791_725_594_368,
                last=0.0,
            ),
        )

    def test_quote_only_contract_manifest_hashes_and_legacy_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy = root / "docs" / "ai" / "data" / "equity.json"
            legacy.parent.mkdir(parents=True)
            legacy.write_text('{"balance":999}', encoding="utf-8")
            with MonitorStore(root / "quotes.sqlite3") as store:
                self._populated(store)
                payload = QuoteStaticExporter(
                    store, root / "docs", clock=lambda: NOW
                ).export()

            self.assertEqual(payload["schema_version"], QUOTE_SCHEMA_VERSION)
            self.assertEqual(payload["symbol"], "FixedStep0.5")
            self.assertEqual(payload["collection"]["status"], "live")
            self.assertEqual(payload["tick_time_basis"], "mt5_reported_unverified")
            self.assertEqual(
                payload["chart_time_basis"],
                "collector_first_observation_host_clock",
            )
            self.assertEqual(
                payload["collector_clock_basis"],
                "utc_serialized_host_clock_synchronization_not_asserted",
            )
            self.assertIsNone(payload["quote"]["last"])
            self.assertAlmostEqual(payload["quote"]["mid"], 5973.55)
            self.assertEqual(payload["quote"]["tick_at"], "2026-10-11T14:59:54.368Z")
            self.assertTrue(payload["quote"]["mt5_reported_offset_material"])
            self.assertAlmostEqual(
                payload["quote"]["mt5_reported_offset_seconds"], 10799.368
            )
            self.assertEqual(payload["samples"], [])
            self.assertFalse(legacy.exists())

            data = root / "docs" / "ai" / "data"
            latest = json.loads((data / "latest.json").read_text(encoding="utf-8"))
            quotes = json.loads((data / "quotes.json").read_text(encoding="utf-8"))
            self.assertEqual(latest["snapshot_id"], quotes["snapshot_id"])
            self.assertEqual(latest["samples"], [])
            self.assertEqual(len(quotes["samples"]), 1)
            self.assertEqual(
                quotes["samples"][0]["collected_at"],
                "2026-10-11T11:59:55.000Z",
            )
            self.assertEqual(
                quotes["coverage"]["collection_start_at"],
                "2026-10-11T11:59:55.000Z",
            )
            self.assertLess((data / "latest.json").stat().st_size, 10_000)
            self.assertLessEqual(
                (data / "quotes.json").stat().st_size, MAX_PUBLIC_JSON_BYTES
            )
            self.assertNotIn(
                "private", latest["coverage"]["sampling_policy"].lower()
            )
            self.assertTrue((data / "quotes.csv").exists())
            self.assertTrue(
                (data / "quotes.csv").read_text(encoding="utf-8").startswith(
                    "collected_at,tick_at,bid,ask,last,mid,spread,spread_points"
                )
            )
            self.assertTrue(
                (root / "docs" / "ai" / "charts" / "fixedstep-price.svg").exists()
            )
            price_svg = (
                root / "docs" / "ai" / "charts" / "fixedstep-price.svg"
            ).read_text(encoding="utf-8")
            self.assertIn("2026-10-11T11:59:55", price_svg)
            self.assertNotIn("2026-10-11T14:59:54", price_svg)
            ai_html = (root / "docs" / "ai" / "index.html").read_text(
                encoding="utf-8"
            )
            self.assertNotIn("<script", ai_html.lower())
            self.assertIn("time basis unverified", ai_html)
            self.assertIn("never used for freshness", ai_html)
            for forbidden_key in (
                '"balance"',
                '"equity"',
                '"profit"',
                '"position_id"',
                '"deal_id"',
                '"ticket"',
                '"magic"',
            ):
                self.assertNotIn(forbidden_key, "\n".join(
                    path.read_text(encoding="utf-8")
                    for path in (root / "docs" / "ai").rglob("*")
                    if path.is_file() and path.suffix != ".svg"
                ))

            manifest = json.loads(
                (root / "docs" / "ai" / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                {entry["url"].rsplit("/", 1)[-1] for entry in manifest["datasets"]},
                {"latest.json", "quotes.json", "quotes.csv"},
            )
            row_counts = {
                entry["url"].rsplit("/", 1)[-1]: entry["row_count"]
                for entry in manifest["datasets"]
            }
            self.assertEqual(row_counts["latest.json"], 1)
            self.assertEqual(row_counts["quotes.json"], 1)
            self.assertEqual(row_counts["quotes.csv"], 1)
            for entry in manifest["datasets"]:
                file_path = data / entry["url"].rsplit("/", 1)[-1]
                self.assertEqual(
                    entry["sha256"], hashlib.sha256(file_path.read_bytes()).hexdigest()
                )

    def test_fast_export_only_rewrites_latest_and_never_reads_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with MonitorStore(root / "quotes.sqlite3") as store:
                self._populated(store)
                exporter = QuoteStaticExporter(store, root / "docs", clock=lambda: NOW)
                initial = exporter.export()
                static_paths = (
                    root / "docs" / "ai" / "data" / "quotes.json",
                    root / "docs" / "ai" / "data" / "quotes.csv",
                    root / "docs" / "ai" / "manifest.json",
                    root / "docs" / "ai" / "index.html",
                    root / "docs" / "ai" / "charts" / "fixedstep-price.svg",
                    root / "docs" / "ai" / "charts" / "fixedstep-spread.svg",
                )
                before = {path: path.read_bytes() for path in static_paths}

                run = store.begin_run("2026-10-11T12:00:00.000Z", "zz-fast")
                store.record_quote_success(
                    run,
                    snapshot(
                        "2026-10-11T12:00:00.000Z",
                        "2026-10-11T11:59:59.000Z",
                        1_791_725_599_000,
                        bid=6000.0,
                        ask=6000.5,
                    ),
                )
                with (
                    patch.object(
                        store,
                        "quote_samples",
                        side_effect=AssertionError("full history read on fast path"),
                    ),
                    patch.object(
                        store,
                        "quote_samples_for_public_window",
                        side_effect=AssertionError("public history read on fast path"),
                    ),
                    patch.object(
                        store,
                        "quote_sample_count",
                        side_effect=AssertionError("history count on fast path"),
                    ),
                ):
                    latest = exporter.export_latest()

                self.assertEqual(latest["quote"]["bid"], 6000.0)
                self.assertEqual(latest["samples"], [])
                self.assertEqual(
                    latest["coverage"]["history_snapshot_id"],
                    initial["snapshot_id"],
                )
                self.assertNotEqual(latest["snapshot_id"], initial["snapshot_id"])
                for path in static_paths:
                    self.assertEqual(path.read_bytes(), before[path], path.name)

    def test_runtime_latest_never_regresses_or_waits_for_static_worker(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "quotes.sqlite3"
            runtime_latest = root / "runtime" / "quote-api" / "latest.json"
            render_started = threading.Event()
            release_render = threading.Event()
            stage_started = threading.Event()
            release_stage = threading.Event()
            fast_done = threading.Event()
            blocked_once = threading.Event()
            background_errors: list[BaseException] = []
            fast_errors: list[BaseException] = []

            with MonitorStore(database) as main_store:
                self._populated(main_store)
                live_exporter = QuoteStaticExporter(
                    main_store,
                    root / "docs",
                    clock=lambda: NOW,
                    live_latest_path=runtime_latest,
                )
                live_exporter.export()
                live_exporter.export_latest()
                original_line_svg = quote_exporter_module._line_svg

                def blocking_line_svg(*args, **kwargs):
                    if not blocked_once.is_set():
                        blocked_once.set()
                        render_started.set()
                        if not release_render.wait(5):
                            raise TimeoutError("test renderer was not released")
                    return original_line_svg(*args, **kwargs)

                def blocked_stage() -> None:
                    stage_started.set()
                    if not release_stage.wait(5):
                        raise TimeoutError("test stage was not released")

                def static_job() -> None:
                    try:
                        with MonitorStore(database) as static_store:
                            QuoteStaticExporter(
                                static_store,
                                root / "docs",
                                clock=lambda: NOW,
                            ).export(after_write=blocked_stage)
                    except BaseException as exc:
                        background_errors.append(exc)

                def fast_job() -> None:
                    try:
                        with MonitorStore(database) as fast_store:
                            QuoteStaticExporter(
                                fast_store,
                                root / "docs",
                                clock=lambda: NOW,
                                live_latest_path=runtime_latest,
                            ).export_latest()
                    except BaseException as exc:
                        fast_errors.append(exc)
                    finally:
                        fast_done.set()

                with patch(
                    "fixedstep_monitor.quote_exporter._line_svg",
                    side_effect=blocking_line_svg,
                ):
                    background = threading.Thread(target=static_job)
                    background.start()
                    try:
                        self.assertTrue(render_started.wait(2))
                        second = main_store.begin_run(
                            "2026-10-11T12:00:00.000Z", "runtime-200"
                        )
                        main_store.record_quote_success(
                            second,
                            snapshot(
                                "2026-10-11T12:00:00.000Z",
                                "2026-10-11T11:59:59.000Z",
                                1_791_725_599_000,
                                bid=200.0,
                                ask=200.5,
                            ),
                        )
                        live_exporter.export_latest()
                        current = json.loads(runtime_latest.read_text(encoding="utf-8"))
                        self.assertEqual(current["quote"]["bid"], 200.0)

                        release_render.set()
                        self.assertTrue(stage_started.wait(2))
                        third = main_store.begin_run(
                            "2026-10-11T12:00:15.000Z", "runtime-300"
                        )
                        main_store.record_quote_success(
                            third,
                            snapshot(
                                "2026-10-11T12:00:15.000Z",
                                "2026-10-11T12:00:14.000Z",
                                1_791_725_614_000,
                                bid=300.0,
                                ask=300.5,
                            ),
                        )
                        fast = threading.Thread(target=fast_job)
                        fast.start()
                        self.assertTrue(
                            fast_done.wait(1),
                            "runtime latest waited for blocked static Git staging",
                        )
                        fast.join(1)
                        self.assertFalse(fast.is_alive())
                        self.assertEqual(fast_errors, [])
                        current = json.loads(runtime_latest.read_text(encoding="utf-8"))
                        self.assertEqual(current["quote"]["bid"], 300.0)
                    finally:
                        release_render.set()
                        release_stage.set()
                        background.join(5)

                self.assertFalse(background.is_alive())
                self.assertEqual(background_errors, [])
                final_live = json.loads(runtime_latest.read_text(encoding="utf-8"))
                self.assertEqual(final_live["quote"]["bid"], 300.0)

    def test_disconnected_export_preserves_last_quote_without_claiming_live(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with MonitorStore(root / "quotes.sqlite3") as store:
                self._populated(store)
                failed = store.begin_run("2026-10-11T12:00:00.000Z", "failed")
                store.record_failure(failed, r"RuntimeError: C:\private\terminal")
                payload = QuoteStaticExporter(
                    store, root / "docs", clock=lambda: NOW
                ).export()
            self.assertEqual(payload["collection"]["status"], "disconnected")
            self.assertEqual(payload["collection"]["error"], "source_unavailable")
            self.assertEqual(payload["quote"]["bid"], 5973.3)
            self.assertNotIn(r"c:\\private", json.dumps(payload).lower())

    def test_compaction_retains_recent_and_last_observed_older_buckets(self):
        def row(when: datetime, value: float) -> dict:
            collected_stamp = when.isoformat(timespec="milliseconds").replace(
                "+00:00", "Z"
            )
            mt5_stamp = (when + timedelta(hours=3)).isoformat(
                timespec="milliseconds"
            ).replace("+00:00", "Z")
            return {
                "tick_at": mt5_stamp,
                "collected_at": collected_stamp,
                "bid": value,
                "ask": value + 0.5,
            }

        samples = [
            row(NOW - timedelta(minutes=1), 1),
            row(NOW - timedelta(seconds=30), 2),
            row(NOW - timedelta(days=2, seconds=50), 3),
            row(NOW - timedelta(days=2, seconds=10), 4),
            row(NOW - timedelta(days=20, minutes=50), 5),
            row(NOW - timedelta(days=20, minutes=10), 6),
        ]
        compacted = compact_quote_history(samples, now=NOW)
        self.assertEqual([row["bid"] for row in compacted], [6, 4, 1, 2])


class FakeQuoteAdapter:
    @property
    def scope(self):
        return {"symbol": "FixedStep0.5", "quote_only": True}

    def connect(self):
        return None

    def collect(self, *, history_start=None):
        return snapshot(
            "2026-10-11T12:00:00.000Z",
            "2026-10-11T11:59:59.000Z",
            1_791_725_599_000,
        )

    def close(self):
        return None


class QuoteCollectorAndCliTests(unittest.TestCase):
    def test_collector_accepts_fifteen_seconds_and_exports(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with MonitorStore(root / "quotes.sqlite3") as store:
                exporter = QuoteStaticExporter(store, root / "docs", clock=lambda: NOW)
                collector = QuoteCollector(
                    store,
                    exporter,
                    FakeQuoteAdapter,
                    config=QuoteCollectorConfig(interval_seconds=15),
                    clock=lambda: NOW,
                )
                payload = collector.run_once()
                self.assertEqual(payload["collection"]["status"], "live")
                self.assertEqual(store.counts()["account_samples"], 0)
                self.assertEqual(store.counts()["quote_samples"], 1)
        with self.assertRaises(ValueError):
            QuoteCollectorConfig(interval_seconds=14)

    def test_quote_live_cli_defaults(self):
        args = build_parser().parse_args(["quote-live"])
        self.assertEqual(args.interval, 15)
        self.assertEqual(args.source_stale_after, 45)
        self.assertEqual(args.publish_interval, 600)
        self.assertEqual(args.database, Path("runtime/fixedstep-quotes.sqlite3"))
        self.assertEqual(args.api_data_dir, Path("runtime/quote-api"))
        self.assertEqual(args.symbol, "FixedStep0.5")

    def test_six_hundred_second_refresh_cadence_has_no_catch_up_burst(self):
        deadline = 600.0
        due_times: list[float] = []
        for now in range(0, 601, 15):
            deadline, due = _advance_periodic_deadline(deadline, float(now), 600)
            if due:
                due_times.append(float(now))
        self.assertEqual(due_times, [600.0])

        deadline, due = _advance_periodic_deadline(deadline, 2_405.0, 600)
        self.assertTrue(due)
        self.assertEqual(deadline, 3_000.0)

    def test_collector_callback_only_queues_heavy_background_work(self):
        tree = ast.parse(textwrap.dedent(inspect.getsource(cli_module.main)))
        nested = {
            node.name: node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

        def called_attributes(function_name: str) -> set[str]:
            return {
                call.func.attr
                for call in ast.walk(nested[function_name])
                if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
            }

        collector_calls = called_attributes("after_cycle")
        self.assertIn("request", collector_calls)
        self.assertTrue(
            {"export", "stage", "commit", "push", "publish"}.isdisjoint(
                collector_calls
            )
        )
        background_calls = called_attributes("perform_background_refresh")
        self.assertIn("export", background_calls)
        self.assertIn("stage", background_calls)
        self.assertIn("finish_staged", background_calls)


if __name__ == "__main__":
    unittest.main()
