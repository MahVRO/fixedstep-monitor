from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path
from typing import Sequence

from .collector import Collector, CollectorConfig
from .exporter import StaticExporter
from .mt5_readonly import QuoteOnlyMT5, ReadOnlyMT5
from .publisher import CoalescingPublishWorker, GitPublisher, PublishError, PublishResult
from .quote_collector import QuoteCollector, QuoteCollectorConfig
from .quote_exporter import QuoteStaticExporter
from .storage import MonitorStore
from .strategy_state import StrategyStateReader


DEFAULT_PUBLIC_BASE_URL = "https://mahvro.github.io/fixedstep-monitor"


def _advance_periodic_deadline(
    deadline: float, now: float, interval_seconds: int
) -> tuple[float, bool]:
    """Advance an anchored cadence without creating catch-up bursts."""

    if now < deadline:
        return deadline, False
    missed_intervals = int((now - deadline) // interval_seconds)
    return deadline + (missed_intervals + 1) * interval_seconds, True


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--database", type=Path, default=Path("data/fixedstep-monitor.sqlite3"))
    parser.add_argument("--site-root", type=Path, default=Path("docs"))
    parser.add_argument("--public-base-url", default=DEFAULT_PUBLIC_BASE_URL)
    parser.add_argument("--source-stale-after", type=int, default=180)
    parser.add_argument("--public-stale-after", type=int, default=1200)
    parser.add_argument("--publish-interval", type=int, default=600)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fixedstep-monitor")
    subparsers = parser.add_subparsers(dest="command")

    collect = subparsers.add_parser("collect", help="collect read-only MT5 data and export static files")
    _common(collect)
    collect.add_argument("--loop", action="store_true", help="repeat until stopped")
    collect.add_argument("--interval", type=int, default=60, choices=range(30, 61))
    collect.add_argument("--history-days", type=int, default=3650)
    collect.add_argument("--history-overlap-hours", type=int, default=24)
    collect.add_argument("--strategy-state", type=Path)
    collect.add_argument("--initial-capital", type=float)
    collect.add_argument("--symbol", action="append", dest="symbols")
    collect.add_argument("--magic", action="append", dest="magic_numbers", type=int, default=[])

    export = subparsers.add_parser("export", help="rebuild static files from stored history")
    _common(export)

    publish = subparsers.add_parser("publish", help="stage generated files; commit/push only when requested")
    publish.add_argument("--repository", type=Path, default=Path.cwd())
    publish.add_argument("--generated-path", default="docs/ai")
    publish.add_argument("--commit", action="store_true")
    publish.add_argument("--push", action="store_true")
    publish.add_argument("--message", default="Update FixedStep0.5 monitoring snapshot")
    publish.add_argument("--remote", default="origin")
    publish.add_argument("--branch")
    publish.add_argument(
        "--rolling-snapshot",
        action="store_true",
        help="amend the checked-out snapshot branch and push with force-with-lease",
    )

    quote_live = subparsers.add_parser(
        "quote-live",
        help="sample the exact FixedStep0.5 quote, serve it locally, and maintain a static fallback",
    )
    quote_live.add_argument(
        "--database", type=Path, default=Path("runtime/fixedstep-quotes.sqlite3")
    )
    quote_live.add_argument("--site-root", type=Path, default=Path("docs"))
    quote_live.add_argument("--public-base-url", default=DEFAULT_PUBLIC_BASE_URL)
    quote_live.add_argument(
        "--symbol", default="FixedStep0.5", choices=("FixedStep0.5",)
    )
    quote_live.add_argument("--interval", type=int, default=15, choices=range(15, 61))
    quote_live.add_argument("--source-stale-after", type=int, default=45)
    quote_live.add_argument("--publish-interval", type=int, default=600)
    quote_live.add_argument("--api-host", default="127.0.0.1")
    quote_live.add_argument("--api-port", type=int, default=8765)
    quote_live.add_argument(
        "--api-data-dir", type=Path, default=Path("runtime/quote-api")
    )
    quote_live.add_argument(
        "--allowed-origin", default="https://mahvro.github.io"
    )
    quote_live.add_argument("--stop-file", type=Path)
    quote_live.add_argument("--enable-git-publish", action="store_true")
    quote_live.add_argument("--repository", type=Path, default=Path.cwd())
    quote_live.add_argument("--remote", default="origin")
    quote_live.add_argument("--branch", default="live")
    return parser


def _exporter(args: argparse.Namespace, store: MonitorStore) -> StaticExporter:
    interval = getattr(args, "interval", 60)
    return StaticExporter(
        store,
        args.site_root,
        source_stale_after_seconds=args.source_stale_after,
        public_stale_after_seconds=args.public_stale_after,
        expected_collection_interval_seconds=interval,
        expected_publish_interval_seconds=args.publish_interval,
        public_base_url=args.public_base_url,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = list(argv) if argv is not None else sys.argv[1:]
    if not arguments or arguments[0] not in {
        "collect",
        "export",
        "publish",
        "quote-live",
    }:
        arguments.insert(0, "collect")
    args = parser.parse_args(arguments)
    command = args.command

    if command == "quote-live":
        if args.source_stale_after < args.interval:
            parser.error("--source-stale-after must be at least --interval")
        if args.publish_interval < 600:
            parser.error("--publish-interval must be at least 600 seconds")
        if args.api_port < 0 or args.api_port > 65535:
            parser.error("--api-port must be between 0 and 65535")
        api_data_dir = args.api_data_dir.resolve()
        static_ai_root = (args.site_root / "ai").resolve()
        try:
            api_data_dir.relative_to(static_ai_root)
        except ValueError:
            pass
        else:
            parser.error("--api-data-dir must be outside the static docs/ai tree")

        # Imported only for the live command so static export and unit tests do
        # not acquire an inbound-server dependency.
        from .quote_api import QuoteApiServer

        stop_event = threading.Event()
        server = None
        with MonitorStore(args.database) as store:
            exporter = QuoteStaticExporter(
                store,
                args.site_root,
                symbol=args.symbol,
                source_stale_after_seconds=args.source_stale_after,
                expected_collection_interval_seconds=args.interval,
                expected_publish_interval_seconds=args.publish_interval,
                public_base_url=args.public_base_url,
                live_latest_path=api_data_dir / "latest.json",
            )
            exporter.export()
            exporter.export_latest()
            server = QuoteApiServer(
                host=args.api_host,
                port=args.api_port,
                allowed_origin=args.allowed_origin,
                latest_path=api_data_dir / "latest.json",
                quotes_path=args.site_root / "ai" / "data" / "quotes.json",
            ).start()
            collector = QuoteCollector(
                store,
                exporter,
                lambda: QuoteOnlyMT5(symbol=args.symbol),
                config=QuoteCollectorConfig(interval_seconds=args.interval),
            )
            publisher = (
                GitPublisher(
                    args.repository,
                    generated_path="docs/ai",
                    allow_push=True,
                )
                if args.enable_git_publish
                else None
            )
            publish_worker: CoalescingPublishWorker | None = None

            def perform_background_refresh() -> PublishResult:
                staged = False
                publication_error: PublishError | None = None

                if publisher is not None:
                    try:
                        publisher.validate_rolling_snapshot(
                            branch=args.branch,
                            rolling_snapshot=True,
                        )
                    except PublishError as exc:
                        # A Git configuration failure must not suppress the
                        # local 600-second static history refresh.
                        publication_error = exc

                # SQLite connections are thread-bound. The worker owns this
                # connection and all bounded history decoding/rendering.
                with MonitorStore(args.database) as publication_store:
                    background_exporter = QuoteStaticExporter(
                        publication_store,
                        args.site_root,
                        symbol=args.symbol,
                        source_stale_after_seconds=args.source_stale_after,
                        expected_collection_interval_seconds=args.interval,
                        expected_publish_interval_seconds=args.publish_interval,
                        public_base_url=args.public_base_url,
                    )

                    def stage_snapshot() -> None:
                        nonlocal publication_error, staged
                        if publisher is None or publication_error is not None:
                            return
                        try:
                            staged = publisher.stage()
                        except PublishError as exc:
                            # Files and coverage metadata are already atomically
                            # updated. Report Git separately after the lock.
                            publication_error = exc

                    history_payload = background_exporter.export(
                        after_write=stage_snapshot if publisher is not None else None
                    )
                print(
                    json.dumps(
                        {
                            "static_export": "completed",
                            "generated_at": history_payload["generated_at"],
                            "snapshot_id": history_payload["snapshot_id"],
                        },
                        sort_keys=True,
                    ),
                    flush=True,
                )

                if publication_error is not None:
                    raise publication_error
                if publisher is None:
                    return PublishResult(changed=False, committed=False, pushed=False)
                return publisher.finish_staged(
                    changed=staged,
                    commit=True,
                    push=True,
                    message="Update FixedStep0.5 quote snapshot",
                    remote=args.remote,
                    branch=args.branch,
                    rolling_snapshot=True,
                )

            def publication_complete(
                result: PublishResult | None, error: str | None
            ) -> None:
                if error is not None:
                    print(
                        json.dumps(
                            {"background_refresh": "failed", "error": error},
                            sort_keys=True,
                        ),
                        file=sys.stderr,
                        flush=True,
                    )
                    return
                detail = result.__dict__ if result is not None else {}
                print(
                    json.dumps(
                        {"background_refresh": "completed", **detail},
                        sort_keys=True,
                    ),
                    flush=True,
                )

            publish_worker = CoalescingPublishWorker(
                perform_background_refresh,
                on_complete=publication_complete,
            )

            next_history_at = time.monotonic() + args.publish_interval
            first_collected_refresh_pending = True

            def after_cycle(payload: dict[str, object]) -> None:
                nonlocal first_collected_refresh_pending, next_history_at
                print(
                    json.dumps(
                        {
                            "status": dict(payload["collection"])["status"],
                            "generated_at": payload["generated_at"],
                            "snapshot_id": payload["snapshot_id"],
                        },
                        sort_keys=True,
                    ),
                    flush=True,
                )
                if args.stop_file and args.stop_file.is_file():
                    stop_event.set()
                    return
                if first_collected_refresh_pending:
                    # Never publish the startup no-data seed on a fresh host.
                    # The first background bundle is rebuilt after MT5 has had
                    # one collection attempt, so a real quote is included when
                    # the source is available.
                    first_collected_refresh_pending = False
                    publish_worker.request()
                    return
                now_monotonic = time.monotonic()
                next_history_at, refresh_due = _advance_periodic_deadline(
                    next_history_at,
                    now_monotonic,
                    args.publish_interval,
                )
                if not refresh_due:
                    return
                # Queue only: history SQL/JSON/CSV/SVG work and all Git/network
                # operations stay off the 15-second collector thread.
                if not publish_worker.request():
                    print(
                        json.dumps(
                            {"publication": "coalesced"},
                            sort_keys=True,
                        ),
                        flush=True,
                    )

            try:
                collector.run_forever(stop_event, after_cycle=after_cycle)
            except KeyboardInterrupt:
                stop_event.set()
            finally:
                joined = publish_worker.shutdown(drain=True)
                worker_state = publish_worker.snapshot
                print(
                    json.dumps(
                        {
                            "publication_worker": "stopped" if joined else "still_running",
                            "requested": worker_state.requested,
                            "completed": worker_state.completed,
                            "coalesced": worker_state.coalesced,
                            "failures": worker_state.failures,
                            "last_error": worker_state.last_error,
                        },
                        sort_keys=True,
                    ),
                    file=sys.stderr if not joined else sys.stdout,
                    flush=True,
                )
                if server is not None:
                    server.shutdown()
        return 0

    if command == "publish":
        try:
            publisher = GitPublisher(
                args.repository,
                generated_path=args.generated_path,
                allow_push=args.push,
            )
            result = publisher.publish(
                commit=args.commit or args.push or args.rolling_snapshot,
                push=args.push,
                message=args.message,
                remote=args.remote,
                branch=args.branch,
                rolling_snapshot=args.rolling_snapshot,
            )
        except PublishError as exc:
            parser.error(str(exc))
        print(json.dumps(result.__dict__, sort_keys=True))
        return 0

    with MonitorStore(args.database) as store:
        exporter = _exporter(args, store)
        if command == "export":
            latest = exporter.export()
        else:
            symbols = tuple(args.symbols or ("FixedStep0.5",))
            reader = StrategyStateReader(args.strategy_state) if args.strategy_state else None
            collector = Collector(
                store,
                exporter,
                lambda: ReadOnlyMT5(symbols=symbols, magic_numbers=args.magic_numbers),
                strategy_reader=reader,
                config=CollectorConfig(
                    interval_seconds=args.interval,
                    initial_history_days=args.history_days,
                    history_overlap_hours=args.history_overlap_hours,
                    initial_capital=args.initial_capital,
                ),
            )
            if args.loop:
                try:
                    collector.run_forever()
                except KeyboardInterrupt:
                    return 0
                return 0
            latest = collector.run_once()
    print(
        json.dumps(
            {
                "status": latest["collection"]["status"],
                "generated_at": latest["generated_at"],
                "snapshot_id": latest["snapshot_id"],
            },
            sort_keys=True,
        )
    )
    return 0
