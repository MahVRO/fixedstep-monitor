from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from .collector import Collector, CollectorConfig
from .exporter import StaticExporter
from .mt5_readonly import ReadOnlyMT5
from .publisher import GitPublisher, PublishError
from .storage import MonitorStore
from .strategy_state import StrategyStateReader


DEFAULT_PUBLIC_BASE_URL = "https://mahvro.github.io/fixedstep-monitor"


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
    if not arguments or arguments[0] not in {"collect", "export", "publish"}:
        arguments.insert(0, "collect")
    args = parser.parse_args(arguments)
    command = args.command

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
