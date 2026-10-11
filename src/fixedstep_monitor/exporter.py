from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import math
import os
import tempfile
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .metrics import (
    account_summary,
    drawdown_metrics,
    equity_with_drawdown,
    period_realized_pnl,
    trade_statistics,
)
from .sanitizer import sanitize_public_data
from .storage import MonitorStore
from .timeutil import iso_utc, parse_utc, utc_now


SCHEMA_VERSION = "1.0"
AI_PATHS = {
    "dashboard": "/dashboard/",
    "ai": "/ai/",
    "manifest": "/ai/manifest.json",
    "latest_json": "/ai/data/latest.json",
    "trades_json": "/ai/data/trades.json",
    "equity_json": "/ai/data/equity.json",
    "statistics_json": "/ai/data/statistics.json",
    "strategy_json": "/ai/data/strategy.json",
    "trades_csv": "/ai/data/trades.csv",
    "equity_csv": "/ai/data/equity.csv",
}


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False) + "\n"


def _flatten_for_csv(row: dict[str, Any]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, (dict, list)):
            output[key] = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        elif value is None:
            output[key] = ""
        else:
            output[key] = value
    return output


def _csv_text(rows: list[dict[str, Any]]) -> str:
    flattened = [_flatten_for_csv(row) for row in rows]
    fields: list[str] = []
    for row in flattened:
        for key in row:
            if key not in fields:
                fields.append(key)
    if not fields:
        return "\n"
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n", extrasaction="ignore")
    writer.writeheader()
    writer.writerows(flattened)
    return stream.getvalue()


def _float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _format_number(value: Any, decimals: int = 2) -> str:
    numeric = _float(value)
    if numeric is None:
        return "Unavailable"
    return f"{numeric:,.{decimals}f}"


def _format_percent(value: Any) -> str:
    numeric = _float(value)
    return "Unavailable" if numeric is None else f"{numeric:,.2f}%"


def _compact_equity_history(
    samples: list[dict[str, Any]], *, now: datetime
) -> list[dict[str, Any]]:
    """Bound public files without altering the full private SQLite history.

    The latest seven days remain at source cadence, days 8-90 retain the last
    observation in each UTC hour, and older history retains the last observation
    in each UTC day. No missing interval is interpolated.
    """

    recent_cutoff = now - timedelta(days=7)
    hourly_cutoff = now - timedelta(days=90)
    recent: list[dict[str, Any]] = []
    hourly: dict[str, dict[str, Any]] = {}
    daily: dict[str, dict[str, Any]] = {}
    undated: list[dict[str, Any]] = []
    for sample in samples:
        observed_at = sample.get("observed_at")
        if not isinstance(observed_at, str):
            undated.append(sample)
            continue
        try:
            observed = parse_utc(observed_at)
        except (TypeError, ValueError):
            undated.append(sample)
            continue
        if observed >= recent_cutoff:
            recent.append(sample)
        elif observed >= hourly_cutoff:
            hourly[observed.strftime("%Y-%m-%dT%H")] = sample
        else:
            daily[observed.strftime("%Y-%m-%d")] = sample
    compacted = list(daily.values()) + list(hourly.values()) + recent + undated
    return sorted(compacted, key=lambda row: str(row.get("observed_at") or ""))


def _line_svg(
    title: str,
    series: list[tuple[str, str, list[tuple[str, float]]]],
    *,
    y_label: str,
    y_decimals: int = 2,
    width: int = 1200,
    height: int = 480,
) -> str | None:
    populated = [(name, color, points) for name, color, points in series if points]
    if not populated:
        return None
    all_points = [point for _, _, points in populated for point in points]
    values = [value for _, value in all_points]
    minimum = min(values)
    maximum = max(values)
    if minimum == maximum:
        padding = max(abs(minimum) * 0.05, 1.0)
        minimum -= padding
        maximum += padding
    else:
        padding = (maximum - minimum) * 0.08
        minimum -= padding
        maximum += padding
    left, right, top, bottom = 92, 32, 62, 72
    plot_w = width - left - right
    plot_h = height - top - bottom
    labels = sorted({label for _, _, points in populated for label, _ in points})
    label_index = {label: index for index, label in enumerate(labels)}
    parsed_times: dict[str, float] = {}
    for label in labels:
        try:
            parsed_times[label] = parse_utc(label).timestamp()
        except (TypeError, ValueError):
            continue
    time_min = min(parsed_times.values()) if parsed_times else None
    time_max = max(parsed_times.values()) if parsed_times else None

    def x_at(label: str) -> float:
        if (
            label in parsed_times
            and time_min is not None
            and time_max is not None
            and time_max > time_min
        ):
            return left + (parsed_times[label] - time_min) / (time_max - time_min) * plot_w
        return left + (0 if len(labels) <= 1 else label_index[label] / (len(labels) - 1) * plot_w)

    def y_at(value: float) -> float:
        return top + (maximum - value) / (maximum - minimum) * plot_h

    grid: list[str] = []
    for index in range(6):
        value = minimum + (maximum - minimum) * index / 5
        y = y_at(value)
        grid.append(
            f'<line x1="{left}" y1="{y:.2f}" x2="{width-right}" y2="{y:.2f}" class="grid"/>'
            f'<text x="{left-12}" y="{y+4:.2f}" text-anchor="end" class="axis">{html.escape(f"{value:,.{y_decimals}f}")}</text>'
        )
    paths: list[str] = []
    legend: list[str] = []
    for series_index, (name, color, points) in enumerate(populated):
        coords = " ".join(
            f"{x_at(label):.2f},{y_at(value):.2f}"
            for label, value in points
        )
        paths.append(f'<polyline points="{coords}" fill="none" stroke="{color}" stroke-width="3"/>')
        legend_x = left + series_index * 190
        legend.append(
            f'<line x1="{legend_x}" y1="{height-24}" x2="{legend_x+26}" y2="{height-24}" stroke="{color}" stroke-width="4"/>'
            f'<text x="{legend_x+34}" y="{height-19}" class="legend">{html.escape(name)}</text>'
        )
    label_indices = sorted({0, max(0, len(labels) // 2), max(0, len(labels) - 1)})
    x_labels = [
        f'<text x="{x_at(labels[index]):.2f}" y="{height-bottom+28}" text-anchor="middle" class="axis">{html.escape(labels[index][:19])}</text>'
        for index in label_indices
    ]
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-labelledby="title desc">
<title id="title">{html.escape(title)}</title><desc id="desc">Static UTC time-series chart. {html.escape(y_label)}.</desc>
<style>.bg{{fill:#0b1020}}.grid{{stroke:#26334d;stroke-width:1}}.axis{{fill:#aebbd0;font:13px system-ui,sans-serif}}.legend{{fill:#dce6f5;font:14px system-ui,sans-serif}}.heading{{fill:#f5f8ff;font:600 22px system-ui,sans-serif}}.ylabel{{fill:#aebbd0;font:13px system-ui,sans-serif}}</style>
<rect class="bg" width="100%" height="100%" rx="16"/><text x="{left}" y="34" class="heading">{html.escape(title)}</text>
<text x="20" y="{top+plot_h/2:.2f}" transform="rotate(-90 20 {top+plot_h/2:.2f})" text-anchor="middle" class="ylabel">{html.escape(y_label)}</text>
{''.join(grid)}{''.join(paths)}{''.join(x_labels)}{''.join(legend)}</svg>"""


def _trade_results_svg(trades: list[dict[str, Any]], width: int = 1200, height: int = 480) -> str | None:
    values = [
        (str(trade.get("exit_time") or trade.get("deal_id") or ""), _float(trade.get("net_profit")))
        for trade in trades
    ]
    points = [(label, value) for label, value in values if value is not None]
    if not points:
        return None
    points = points[-80:]
    left, right, top, bottom = 92, 32, 62, 72
    plot_w = width - left - right
    plot_h = height - top - bottom
    maximum = max(max(value for _, value in points), 0.0)
    minimum = min(min(value for _, value in points), 0.0)
    if minimum == maximum:
        maximum, minimum = 1.0, -1.0
    padding = (maximum - minimum) * 0.08
    maximum += padding
    minimum -= padding

    def y_at(value: float) -> float:
        return top + (maximum - value) / (maximum - minimum) * plot_h

    zero = y_at(0.0)
    slot = plot_w / len(points)
    bars: list[str] = []
    for index, (_, value) in enumerate(points):
        x = left + index * slot + slot * 0.12
        y = min(zero, y_at(value))
        bar_h = max(abs(y_at(value) - zero), 1.0)
        color = "#32d583" if value >= 0 else "#f97066"
        bars.append(
            f'<rect x="{x:.2f}" y="{y:.2f}" width="{max(slot*0.76,1):.2f}" height="{bar_h:.2f}" fill="{color}"><title>{html.escape(f"{value:,.2f}")}</title></rect>'
        )
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-labelledby="title desc">
<title id="title">Individual closing-execution results</title><desc id="desc">Net profit for up to the latest 80 closing execution/deal records.</desc>
<style>.bg{{fill:#0b1020}}.axis{{fill:#aebbd0;font:13px system-ui,sans-serif}}.heading{{fill:#f5f8ff;font:600 22px system-ui,sans-serif}}</style>
<rect class="bg" width="100%" height="100%" rx="16"/><text x="{left}" y="34" class="heading">Individual closing-execution results</text>
<line x1="{left}" y1="{zero:.2f}" x2="{width-right}" y2="{zero:.2f}" stroke="#7889a7"/><text x="{left-12}" y="{zero+4:.2f}" text-anchor="end" class="axis">0</text>
{''.join(bars)}<text x="{left}" y="{height-28}" class="axis">Oldest</text><text x="{width-right}" y="{height-28}" text-anchor="end" class="axis">Newest (UTC order)</text></svg>"""


class StaticExporter:
    def __init__(
        self,
        store: MonitorStore,
        site_root: str | Path,
        *,
        source_stale_after_seconds: int = 180,
        public_stale_after_seconds: int = 1200,
        expected_collection_interval_seconds: int = 60,
        expected_publish_interval_seconds: int = 600,
        public_base_url: str = "https://mahvro.github.io/fixedstep-monitor",
        clock=utc_now,
    ) -> None:
        self.store = store
        self.site_root = Path(site_root)
        self.source_stale_after_seconds = int(source_stale_after_seconds)
        self.public_stale_after_seconds = int(public_stale_after_seconds)
        self.expected_collection_interval_seconds = int(expected_collection_interval_seconds)
        self.expected_publish_interval_seconds = int(expected_publish_interval_seconds)
        parsed_base = urlsplit(public_base_url)
        if (
            parsed_base.scheme not in {"http", "https"}
            or not parsed_base.netloc
            or parsed_base.username is not None
            or parsed_base.password is not None
            or parsed_base.query
            or parsed_base.fragment
        ):
            raise ValueError("public_base_url must be an absolute HTTP(S) URL without credentials, query, or fragment")
        self.public_base_url = public_base_url.rstrip("/")
        self.clock = clock

    def public_url(self, path: str) -> str:
        return f"{self.public_base_url}/{path.lstrip('/')}"

    def _coverage(
        self,
        accounts: list[dict[str, Any]],
        trades: list[dict[str, Any]],
        strategies: list[dict[str, Any]],
        events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        coverage: dict[str, Any] = {
            "account_samples": len(accounts),
            "closed_trade_records": len(trades),
            "strategy_samples": len(strategies),
            "strategy_events": len(events),
            "closed_trade_grain": "one MT5 closing execution/deal; partial closes may create multiple records",
        }
        if accounts:
            coverage["account_start_at"] = accounts[0].get("observed_at")
            coverage["account_end_at"] = accounts[-1].get("observed_at")
        timed_trades = [trade.get("exit_time") for trade in trades if isinstance(trade.get("exit_time"), str)]
        if timed_trades:
            coverage["trade_start_at"] = min(timed_trades)
            coverage["trade_end_at"] = max(timed_trades)
        return coverage

    def _omissions(
        self,
        account: dict[str, Any],
        trades: list[dict[str, Any]],
        strategy: dict[str, Any],
        sanitation: tuple[dict[str, str], ...],
    ) -> list[dict[str, str]]:
        omissions: list[dict[str, str]] = list(sanitation)

        def missing(field: str, reason: str) -> None:
            omissions.append({"field": field, "reason": reason})

        if "initial_capital" not in account:
            missing("account.initial_capital", "No explicit initial-capital evidence is available.")
            missing("account.net_return", "Requires explicit initial-capital evidence.")
            missing("account.net_return_pct", "Requires explicit initial-capital evidence.")
        for field in ("status", "symbol", "step", "level"):
            if field not in strategy:
                missing(f"strategy.{field}", "The optional strategy-state source did not provide this field.")
        if not any("stop_loss" in trade for trade in trades):
            missing("closed_trades.stop_loss", "MT5 closing deals do not expose the historical stop-loss value.")
        if not any("take_profit" in trade for trade in trades):
            missing("closed_trades.take_profit", "MT5 closing deals do not expose the historical take-profit value.")
        if not any("entry_price" in trade or "exit_price" in trade for trade in trades):
            missing(
                "charts.trade_entries_exits",
                "No paired execution-price observations are available for this chart.",
            )
        unique: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for item in omissions:
            marker = (item["field"], item["reason"])
            if marker not in seen:
                seen.add(marker)
                unique.append(item)
        return unique

    def export(self) -> dict[str, Any]:
        now = self.clock().astimezone(UTC)
        generated_at = iso_utc(now)
        snapshot_id = str(uuid.uuid4())
        latest_run = self.store.latest_run()
        last_success = self.store.last_successful_run()

        raw = {
            "account_samples": self.store.account_samples(),
            "open_positions": self.store.latest_positions(),
            "closed_trades": self.store.closed_trades(),
            "strategy": self.store.latest_strategy() or {},
            "strategy_samples": self.store.strategy_samples(),
            "events": self.store.strategy_events(),
            "trade_scope": self.store.get_metadata("trade_scope", {}),
        }
        sanitized = sanitize_public_data(raw)
        public = sanitized.value
        accounts = list(public["account_samples"])
        position_fields = {
            "observed_at",
            "symbol",
            "direction",
            "volume",
            "entry_time",
            "entry_price",
            "current_price",
            "stop_loss",
            "take_profit",
            "unrealized_pnl",
            "swap",
        }
        trade_fields = {
            "symbol",
            "direction",
            "volume",
            "entry_time",
            "exit_time",
            "entry_price",
            "exit_price",
            "stop_loss",
            "take_profit",
            "profit",
            "commission",
            "swap",
            "fee",
            "entry_costs_allocated",
            "net_profit",
            "duration_seconds",
        }
        strategy_fields = {"observed_at", "status", "symbol", "step", "level", "internal_state"}
        event_fields = {"timestamp", "observed_at", "type", "message", "data", "status", "symbol", "step", "level"}
        positions = [
            {key: value for key, value in row.items() if key in position_fields}
            for row in public["open_positions"]
        ]
        trades = [
            {key: value for key, value in row.items() if key in trade_fields}
            for row in public["closed_trades"]
        ]
        strategy = {
            key: value
            for key, value in dict(public["strategy"]).items()
            if key in strategy_fields
        }
        strategy_samples = [
            {key: value for key, value in row.items() if key in strategy_fields}
            for row in public["strategy_samples"]
        ]
        events = [
            {key: value for key, value in row.items() if key in event_fields}
            for row in public["events"]
        ]
        source_observed_at = last_success.get("observed_at") if last_success else None
        source_lag: float | None = None
        if isinstance(source_observed_at, str):
            try:
                source_lag = max(0.0, (now - parse_utc(source_observed_at)).total_seconds())
            except (TypeError, ValueError):
                source_observed_at = None

        if latest_run and latest_run.get("connected") == 0:
            status = "disconnected"
        elif not last_success:
            status = "no_data"
        elif source_lag is not None and source_lag > self.source_stale_after_seconds:
            status = "stale"
        else:
            status = "live"

        collection: dict[str, Any] = {
            "status": status,
            "generated_at": generated_at,
            "published_at": generated_at,
            "source_stale_after_seconds": self.source_stale_after_seconds,
            "public_stale_after_seconds": self.public_stale_after_seconds,
            "expected_collection_interval_seconds": self.expected_collection_interval_seconds,
            "expected_publish_interval_seconds": self.expected_publish_interval_seconds,
        }
        if source_observed_at:
            collection["collected_at"] = source_observed_at
            collection["source_stale_after_at"] = iso_utc(
                parse_utc(source_observed_at) + timedelta(seconds=self.source_stale_after_seconds)
            )
            collection["public_stale_after_at"] = iso_utc(
                now + timedelta(seconds=self.public_stale_after_seconds)
            )
        if source_lag is not None:
            collection["source_lag_seconds"] = source_lag
        if status == "disconnected":
            collection["error"] = "source_unavailable"

        initial_capital = self.store.get_metadata("initial_capital")
        account = account_summary(accounts[-1] if accounts else None, initial_capital=initial_capital)
        enriched_equity = equity_with_drawdown(accounts)
        public_equity = _compact_equity_history(enriched_equity, now=now)
        periods = period_realized_pnl(trades, now=now) if last_success else {}
        drawdown = drawdown_metrics(accounts)
        statistics = trade_statistics(trades) if last_success else {}
        counts = {"open_positions": len(positions), "closed_trades": len(trades)} if last_success else {}
        coverage = self._coverage(accounts, trades, strategy_samples, events)
        coverage["published_account_samples"] = len(public_equity)
        coverage["account_publication_policy"] = (
            "full source cadence for 7 days; last observed sample per UTC hour for days 8-90; "
            "last observed sample per UTC day thereafter; no interpolation"
        )
        omissions = self._omissions(account, trades, strategy, sanitized.omissions)
        if not last_success:
            for field in (
                "account.balance",
                "account.equity",
                "account.unrealized_pnl",
                "periods",
                "drawdown",
                "counts.open_positions",
                "counts.closed_trades",
            ):
                omissions.append(
                    {"field": field, "reason": "No successful MT5 source observation is available."}
                )
        meta = {
            "omissions": omissions,
            "actual_data_only": True,
            "closed_trade_grain": coverage["closed_trade_grain"],
            "field_definitions": {
                "realized_pnl": "Sum of available net_profit on MT5 closing deal records in the UTC period.",
                "unrealized_pnl": "MT5 account profit at collection time.",
                "drawdown_abs": "Running peak equity minus observed equity.",
                "drawdown_pct": "Drawdown_abs divided by running peak equity, when the peak is positive.",
                "win_rate": "Profitable closing deal records divided by profitable plus losing records; breakeven records are excluded.",
                "profit_factor": "Gross profitable net results divided by the absolute gross losing net results.",
            },
        }
        # Only publish an explicit safe summary of selector metadata. Raw magic
        # numbers remain private and are still processed by the sanitizer audit.
        trade_scope = raw.get("trade_scope", {})
        if isinstance(trade_scope, dict) and trade_scope.get("filtered"):
            public_trade_scope = "configured MT5 symbol/magic allowlist"
        elif isinstance(trade_scope, dict) and trade_scope:
            public_trade_scope = "unfiltered MT5 account trade observations"
        else:
            public_trade_scope = "not yet verified; no source collection has established the trade selector"
        scope = {
            "account": "account-wide MT5 account observations",
            "trades": public_trade_scope,
        }
        if isinstance(trade_scope, dict) and trade_scope.get("filtered"):
            selector: dict[str, Any] = {
                "magic_filter_configured": bool(trade_scope.get("magic_filter_configured"))
            }
            symbols = trade_scope.get("symbols")
            if isinstance(symbols, list) and all(isinstance(symbol, str) for symbol in symbols):
                selector["symbols"] = symbols
            scope["trade_selector"] = selector
        common: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "snapshot_id": snapshot_id,
            "generated_at": generated_at,
            "timezone": "UTC",
            "scope": scope,
            "coverage": coverage,
            "meta": meta,
            "collection": collection,
        }
        if source_observed_at:
            common["source_observed_at"] = source_observed_at

        links = {name: self.public_url(path) for name, path in AI_PATHS.items()}

        latest = {
            **common,
            "collection": collection,
            "account": account,
            "periods": periods,
            "drawdown": drawdown,
            "counts": counts,
            "strategy": strategy,
            "links": links,
        }
        equity_payload = {**common, "samples": public_equity}
        trades_payload = {
            **common,
            "grain": coverage["closed_trade_grain"],
            "open_positions": positions,
            "closed_trades": trades,
        }
        statistics_payload = {
            **common,
            "statistics": statistics,
            "periods": periods,
            "drawdown": drawdown,
            "counts": counts,
        }
        strategy_payload = {
            **common,
            **strategy,
            "events": events,
            "history": strategy_samples,
        }
        positions_payload = {**common, "positions": positions}
        deals_payload = {**common, "closed_trades": trades}
        events_payload = {**common, "events": events}

        data_dir = self.site_root / "ai" / "data"
        charts_dir = self.site_root / "ai" / "charts"
        payloads = {
            "latest.json": latest,
            "snapshot.json": latest,
            "equity.json": equity_payload,
            "account_history.json": equity_payload,
            "trades.json": trades_payload,
            "statistics.json": statistics_payload,
            "strategy.json": strategy_payload,
            "positions.json": positions_payload,
            "deals.json": deals_payload,
            "events.json": events_payload,
        }
        for name, payload in payloads.items():
            _atomic_write(data_dir / name, _json_text(payload))

        trade_rows = [dict(record_type="open_position", **row) for row in positions] + [
            dict(record_type="closed_trade", **row) for row in trades
        ]
        _atomic_write(data_dir / "equity.csv", _csv_text(public_equity))
        _atomic_write(data_dir / "trades.csv", _csv_text(trade_rows))
        _atomic_write(data_dir / "positions.csv", _csv_text(positions))
        _atomic_write(data_dir / "deals.csv", _csv_text(trades))
        _atomic_write(data_dir / "events.csv", _csv_text(events))
        strategy_csv_rows = [
            {key: value for key, value in row.items() if key != "internal_state"}
            for row in strategy_samples
        ]
        _atomic_write(data_dir / "strategy.csv", _csv_text(strategy_csv_rows))

        chart_entries: list[dict[str, Any]] = []

        def add_chart(filename: str, title: str, svg: str | None) -> None:
            if svg is None:
                return
            _atomic_write(charts_dir / filename, svg)
            chart_path = charts_dir / filename
            chart_entries.append(
                {
                    "title": title,
                    "format": "svg",
                    "url": self.public_url(f"/ai/charts/{filename}"),
                    "size_bytes": chart_path.stat().st_size,
                    "sha256": hashlib.sha256(chart_path.read_bytes()).hexdigest(),
                }
            )

        balance_points = [
            (str(row["observed_at"]), float(row["balance"]))
            for row in public_equity
            if _float(row.get("balance")) is not None and row.get("observed_at")
        ]
        equity_points = [
            (str(row["observed_at"]), float(row["equity"]))
            for row in public_equity
            if _float(row.get("equity")) is not None and row.get("observed_at")
        ]
        add_chart(
            "equity-balance.svg",
            "Balance and equity history",
            _line_svg(
                "Balance and equity history",
                [("Balance", "#53b1fd", balance_points), ("Equity", "#32d583", equity_points)],
                y_label=str(account.get("currency") or "Account units"),
            ),
        )
        drawdown_points = [
            (str(row["observed_at"]), float(row["drawdown_abs"]))
            for row in public_equity
            if _float(row.get("drawdown_abs")) is not None and row.get("observed_at")
        ]
        add_chart(
            "drawdown.svg",
            "Drawdown over time",
            _line_svg(
                "Drawdown over time",
                [("Drawdown", "#f97066", drawdown_points)],
                y_label=str(account.get("currency") or "Account units"),
            ),
        )
        cumulative = 0.0
        cumulative_points: list[tuple[str, float]] = []
        for trade in trades:
            net = _float(trade.get("net_profit"))
            timestamp = trade.get("exit_time")
            if net is not None and isinstance(timestamp, str):
                cumulative += net
                cumulative_points.append((timestamp, cumulative))
        add_chart(
            "cumulative-realized-pnl.svg",
            "Cumulative realized P&L",
            _line_svg(
                "Cumulative realized P&L",
                [("Realized P&L", "#b692f6", cumulative_points)],
                y_label=str(account.get("currency") or "Account units"),
            ),
        )
        add_chart(
            "trade-results.svg",
            "Individual closing-execution results",
            _trade_results_svg(trades),
        )
        entry_price_points = [
            (str(trade["exit_time"]), float(trade["entry_price"]))
            for trade in trades
            if isinstance(trade.get("exit_time"), str) and _float(trade.get("entry_price")) is not None
        ]
        exit_price_points = [
            (str(trade["exit_time"]), float(trade["exit_price"]))
            for trade in trades
            if isinstance(trade.get("exit_time"), str) and _float(trade.get("exit_price")) is not None
        ]
        add_chart(
            "trade-entry-exit.svg",
            "Entry and exit prices",
            _line_svg(
                "Entry and exit prices",
                [("Entry", "#53b1fd", entry_price_points), ("Exit", "#fdb022", exit_price_points)],
                y_label="Observed execution price",
                y_decimals=6,
            ),
        )
        step_points = [
            (str(row.get("observed_at", "")), float(row.get("step", row.get("level"))))
            for row in strategy_samples
            if _float(row.get("step", row.get("level"))) is not None and row.get("observed_at")
        ]
        add_chart(
            "step-progression.svg",
            "FixedStep0.5 step progression",
            _line_svg(
                "FixedStep0.5 step progression",
                [("Step / level", "#fdb022", step_points)],
                y_label="Step / level",
                y_decimals=0,
            ),
        )

        def fields(rows: list[dict[str, Any]]) -> list[str]:
            return sorted({key for row in rows for key in row})

        dataset_specs = [
            ("Latest snapshot", "json", "latest.json", 1, sorted(latest)),
            ("Trades", "json", "trades.json", len(positions) + len(trades), fields(positions + trades)),
            ("Equity history", "json", "equity.json", len(public_equity), fields(public_equity)),
            ("Statistics", "json", "statistics.json", 1 if statistics else 0, sorted(statistics)),
            ("Strategy state", "json", "strategy.json", len(strategy_samples), fields(strategy_samples)),
            ("Trades", "csv", "trades.csv", len(trade_rows), fields(trade_rows)),
            ("Equity history", "csv", "equity.csv", len(public_equity), fields(public_equity)),
            ("Open positions", "csv", "positions.csv", len(positions), fields(positions)),
            ("Closed deals", "csv", "deals.csv", len(trades), fields(trades)),
            ("Events", "csv", "events.csv", len(events), fields(events)),
            ("Strategy history", "csv", "strategy.csv", len(strategy_csv_rows), fields(strategy_csv_rows)),
        ]
        dataset_entries: list[dict[str, Any]] = []
        for title, file_format, filename, row_count, available_fields in dataset_specs:
            file_path = data_dir / filename
            dataset_entries.append(
                {
                    "title": title,
                    "format": file_format,
                    "url": self.public_url(f"/ai/data/{filename}"),
                    "row_count": row_count,
                    "available_fields": available_fields,
                    "size_bytes": file_path.stat().st_size,
                    "sha256": hashlib.sha256(file_path.read_bytes()).hexdigest(),
                }
            )
        manifest = {
            **common,
            "page": self.public_url("/ai/"),
            "datasets": dataset_entries,
            "charts": chart_entries,
            "freshness": collection,
        }
        _atomic_write(self.site_root / "ai" / "index.html", self._ai_html(latest, trades, public_equity, chart_entries))
        _atomic_write(self.site_root / "ai" / "manifest.json", _json_text(manifest))
        return latest

    def _ai_html(
        self,
        latest: dict[str, Any],
        trades: list[dict[str, Any]],
        equity: list[dict[str, Any]],
        charts: list[dict[str, str]],
    ) -> str:
        collection = latest["collection"]
        account = latest["account"]
        periods = latest["periods"]
        drawdown = latest["drawdown"]
        statistics = trade_statistics(trades) if collection.get("collected_at") else {}
        warning = ""
        if collection["status"] == "no_data":
            warning = (
                '<div class="warning"><strong>Data status: no_data.</strong> '
                "No successful source observation is available, so performance values are omitted.</div>"
            )
        elif collection["status"] != "live":
            warning = (
                f'<div class="warning"><strong>Data status: {html.escape(collection["status"])}</strong>. '
                "The last good history is retained; treat current-position and account values as last observed.</div>"
            )
        cards = [
            ("Balance", _format_number(account.get("balance"))),
            ("Equity", _format_number(account.get("equity"))),
            ("Unrealized P&L", _format_number(account.get("unrealized_pnl"))),
            ("Daily realized P&L", _format_number(periods.get("daily", {}).get("realized_pnl"))),
            ("Weekly realized P&L", _format_number(periods.get("weekly", {}).get("realized_pnl"))),
            ("Monthly realized P&L", _format_number(periods.get("monthly", {}).get("realized_pnl"))),
            ("All-available realized P&L", _format_number(periods.get("all_time", {}).get("realized_pnl"))),
            ("Initial capital", _format_number(account.get("initial_capital"))),
            ("Net return", _format_number(account.get("net_return"))),
            ("Net return percentage", _format_percent(account.get("net_return_pct"))),
            ("Current drawdown", _format_number(drawdown.get("current_abs"))),
            ("Current drawdown percentage", _format_percent(drawdown.get("current_pct"))),
            ("Maximum observed drawdown", _format_number(drawdown.get("max_abs"))),
            ("Maximum observed drawdown percentage", _format_percent(drawdown.get("max_pct"))),
            ("Closing execution records", _format_number(statistics.get("closed_trades"), 0)),
            ("Win rate", _format_percent(statistics.get("win_rate"))),
            ("Profit factor", _format_number(statistics.get("profit_factor"))),
            ("Average win", _format_number(statistics.get("avg_win"))),
            ("Average loss", _format_number(statistics.get("avg_loss"))),
            ("Best result", _format_number(statistics.get("best_trade"))),
            ("Worst result", _format_number(statistics.get("worst_trade"))),
            ("Current win streak", _format_number(statistics.get("consecutive_wins"), 0)),
            ("Current loss streak", _format_number(statistics.get("consecutive_losses"), 0)),
        ]
        card_html = "".join(
            f'<div class="card"><dt>{html.escape(label)}</dt><dd>{html.escape(value)}</dd></div>'
            for label, value in cards
        )
        strategy = latest.get("strategy") or {}
        strategy_items = [
            f"<li><strong>{html.escape(key.replace('_', ' ').title())}:</strong> {html.escape(str(value))}</li>"
            for key, value in strategy.items()
            if key in {"status", "symbol", "step", "level", "observed_at"}
        ]
        datasets_html = "".join(
            f'<li><a href="{html.escape(url)}">{html.escape(key.replace("_", " ").title())}</a></li>'
            for key, url in latest["links"].items()
            if key.endswith("json") or key.endswith("csv") or key == "manifest"
        )
        charts_html = "".join(
            f'<figure><img src="{html.escape(item["url"])}" alt="{html.escape(item["title"])}"><figcaption>{html.escape(item["title"])}</figcaption></figure>'
            for item in charts
        ) or "<p>No chart has enough observed data yet.</p>"
        rows: list[str] = []
        for trade in list(reversed(trades))[:20]:
            rows.append(
                "<tr>"
                f"<td>{html.escape(str(trade.get('exit_time', 'Unavailable')))}</td>"
                f"<td>{html.escape(str(trade.get('symbol', 'Unavailable')))}</td>"
                f"<td>{html.escape(str(trade.get('direction', 'Unavailable')))}</td>"
                f"<td>{html.escape(_format_number(trade.get('volume')))}</td>"
                f"<td>{html.escape(_format_number(trade.get('net_profit')))}</td>"
                "</tr>"
            )
        trades_html = "".join(rows) or '<tr><td colspan="5">No closed trade records are available.</td></tr>'
        drawdown_events = sorted(
            [row for row in equity if _float(row.get("drawdown_abs")) is not None],
            key=lambda row: float(row["drawdown_abs"]),
            reverse=True,
        )[:5]
        drawdown_html = "".join(
            f"<li>{html.escape(str(row.get('observed_at')))} — {html.escape(_format_number(row.get('drawdown_abs')))} ({html.escape(_format_number(row.get('drawdown_pct')))}%)</li>"
            for row in drawdown_events
        ) or "<li>No equity observations are available.</li>"
        omissions = latest["meta"]["omissions"]
        omissions_html = "".join(
            f"<li><code>{html.escape(item['field'])}</code>: {html.escape(item['reason'])}</li>"
            for item in omissions
        )
        return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>FixedStep0.5 AI monitoring view</title>
<style>
:root{{color-scheme:dark;font-family:Inter,ui-sans-serif,system-ui,sans-serif;background:#070b14;color:#e8eef8}}body{{margin:0}}main{{max-width:1180px;margin:auto;padding:32px 20px 64px}}a{{color:#84caff}}h1,h2{{letter-spacing:-.02em}}.muted{{color:#9aa9bf}}.warning{{padding:14px 16px;background:#4d2d09;border:1px solid #fdb022;border-radius:10px;margin:18px 0}}dl{{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px}}.card{{background:#11182a;border:1px solid #26334d;border-radius:12px;padding:16px}}dt{{color:#9aa9bf;font-size:.84rem}}dd{{font-size:1.5rem;font-weight:650;margin:8px 0 0}}figure{{margin:20px 0}}img{{display:block;width:100%;height:auto;border:1px solid #26334d;border-radius:14px}}figcaption{{color:#9aa9bf;margin-top:6px}}table{{width:100%;border-collapse:collapse;display:block;overflow-x:auto}}th,td{{text-align:left;padding:10px 12px;border-bottom:1px solid #26334d;white-space:nowrap}}code{{color:#d6bbfb}}section{{margin-top:34px}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:22px}}footer{{margin-top:44px;color:#9aa9bf}}
</style></head><body><main>
<header><h1>FixedStep0.5 monitoring data</h1><p class="muted">Static, JavaScript-free view. All timestamps are UTC. Snapshot {html.escape(latest['snapshot_id'])}.</p>
<p><strong>Collection status:</strong> {html.escape(collection['status'])}<br><strong>Last source observation:</strong> {html.escape(str(collection.get('collected_at', 'Unavailable')))}<br><strong>Artifact generated:</strong> {html.escape(latest['generated_at'])}<br><strong>Account coverage:</strong> {html.escape(str(latest['coverage'].get('account_start_at', 'Unavailable')))} to {html.escape(str(latest['coverage'].get('account_end_at', 'Unavailable')))}<br><strong>Execution coverage:</strong> {html.escape(str(latest['coverage'].get('trade_start_at', 'Unavailable')))} to {html.escape(str(latest['coverage'].get('trade_end_at', 'Unavailable')))}</p>{warning}</header>
<section aria-labelledby="summary"><h2 id="summary">Performance summary</h2><dl>{card_html}</dl></section>
<section class="grid"><div><h2>Strategy state</h2><ul>{''.join(strategy_items) or '<li>Not available from the optional strategy-state source.</li>'}</ul></div><div><h2>Machine-readable data</h2><ul>{datasets_html}</ul></div></section>
<section aria-labelledby="charts"><h2 id="charts">Static chart snapshots</h2>{charts_html}</section>
<section aria-labelledby="recent"><h2 id="recent">Recent closing-execution records</h2><p class="muted">Grain: one MT5 closing execution/deal; partial closes may create multiple rows.</p><table><thead><tr><th>Exit time</th><th>Symbol</th><th>Direction</th><th>Volume</th><th>Net profit</th></tr></thead><tbody>{trades_html}</tbody></table></section>
<section class="grid"><div><h2>Largest observed drawdowns</h2><ol>{drawdown_html}</ol></div><div><h2>Unavailable fields</h2><ul>{omissions_html}</ul></div></section>
<section><h2>Field definitions</h2><ul><li><strong>Realized P&amp;L:</strong> sum of available net profit on closing deal records in the stated UTC period.</li><li><strong>Drawdown:</strong> running peak equity minus observed equity.</li><li><strong>Win rate:</strong> profitable closing deal records divided by profitable plus losing records; breakeven records are excluded.</li></ul></section>
<footer><a href="{html.escape(latest['links']['dashboard'])}">Human dashboard</a> · <a href="{html.escape(latest['links']['manifest'])}">Manifest</a> · Refresh policy: source every {self.expected_collection_interval_seconds}s; public artifact target every {self.expected_publish_interval_seconds}s.</footer>
</main></body></html>"""
