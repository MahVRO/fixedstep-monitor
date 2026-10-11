from __future__ import annotations

import hashlib
import html
import json
import math
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from .exporter import _atomic_write, _csv_text, _line_svg
from .storage import MonitorStore
from .timeutil import iso_utc, parse_utc, utc_now


QUOTE_SCHEMA_VERSION = "fixedstep-quotes-v1"
MAX_PUBLIC_SAMPLES = 18_000
PUBLIC_HISTORY_DAYS = 90
COVERAGE_METADATA_KEY = "quote_public_coverage_v1"
MT5_TICK_TIME_BASIS = "mt5_reported_unverified"
COLLECTOR_CLOCK_BASIS = "utc_serialized_host_clock_synchronization_not_asserted"
CHART_TIME_BASIS = "collector_first_observation_host_clock"
MATERIAL_CLOCK_OFFSET_SECONDS = 300.0
# quote_api.py refuses responses above 4 MiB. Leave headroom for metadata and
# small schema additions while guaranteeing the history endpoint stays usable.
MAX_PUBLIC_JSON_BYTES = 3_500_000
QUOTE_FIELDS = (
    "collected_at",
    "tick_at",
    "bid",
    "ask",
    "last",
    "mid",
    "spread",
    "spread_points",
)
LEGACY_PUBLIC_FILES = (
    "data/account_history.json",
    "data/deals.csv",
    "data/deals.json",
    "data/equity.csv",
    "data/equity.json",
    "data/events.csv",
    "data/events.json",
    "data/positions.csv",
    "data/positions.json",
    "data/snapshot.json",
    "data/statistics.json",
    "data/strategy.csv",
    "data/strategy.json",
    "data/trades.csv",
    "data/trades.json",
    "charts/cumulative-realized-pnl.svg",
    "charts/drawdown.svg",
    "charts/equity-balance.svg",
    "charts/step-progression.svg",
    "charts/trade-entry-exit.svg",
    "charts/trade-results.svg",
)


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _empty_chart_svg(title: str, message: str) -> str:
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="480" viewBox="0 0 1200 480" role="img" aria-labelledby="title desc">
<title id="title">{html.escape(title)}</title><desc id="desc">{html.escape(message)}</desc>
<rect width="1200" height="480" rx="16" fill="#0b1020"/><text x="92" y="62" fill="#f5f8ff" font-family="system-ui,sans-serif" font-size="22" font-weight="600">{html.escape(title)}</text>
<text x="600" y="244" text-anchor="middle" fill="#aebbd0" font-family="system-ui,sans-serif" font-size="18">{html.escape(message)}</text></svg>"""


def _public_quote(row: dict[str, Any], *, include_metadata: bool) -> dict[str, Any] | None:
    bid = _finite(row.get("bid"))
    ask = _finite(row.get("ask"))
    tick_at = row.get("tick_at")
    collected_at = row.get("collected_at")
    if (
        bid is None
        or ask is None
        or bid <= 0
        or ask <= 0
        or ask < bid
        or not isinstance(tick_at, str)
        or not isinstance(collected_at, str)
    ):
        return None
    spread = ask - bid
    item: dict[str, Any] = {
        "collected_at": collected_at,
        "tick_at": tick_at,
        "bid": bid,
        "ask": ask,
        "last": None,
        "mid": (bid + ask) / 2.0,
        "spread": spread,
        "spread_points": None,
    }
    last = _finite(row.get("last"))
    if last is not None and last > 0:
        item["last"] = last
    point = _finite(row.get("point"))
    if point is not None and point > 0:
        item["spread_points"] = spread / point
    if include_metadata:
        digits = row.get("digits")
        if isinstance(digits, int) and not isinstance(digits, bool) and 0 <= digits <= 10:
            item["digits"] = digits
        item["point"] = point if point is not None and point > 0 else None
        tick_size = _finite(row.get("trade_tick_size"))
        item["trade_tick_size"] = (
            tick_size if tick_size is not None and tick_size > 0 else None
        )
        item["tick_time_basis"] = MT5_TICK_TIME_BASIS
        try:
            reported_offset = (
                parse_utc(tick_at) - parse_utc(collected_at)
            ).total_seconds()
        except (TypeError, ValueError):
            reported_offset = None
        if reported_offset is not None and math.isfinite(reported_offset):
            item["mt5_reported_offset_seconds"] = round(reported_offset, 3)
            item["mt5_reported_offset_material"] = (
                abs(reported_offset) >= MATERIAL_CLOCK_OFFSET_SECONDS
            )
    return item


def compact_quote_history(
    samples: list[dict[str, Any]], *, now: datetime
) -> list[dict[str, Any]]:
    """Bound public history without interpolating or altering local samples.

    The latest 24 hours remain at the configured source cadence. Days 2-7 keep
    the last first-observed tick in each collector-host-clock minute, and days
    8-90 keep the last first-observed tick in each collector-host-clock hour.
    Raw MT5 time never controls retention, bucketing, or ordering. The host
    values are UTC-serialized, but synchronization is not asserted.
    """

    full_cutoff = now - timedelta(hours=24)
    minute_cutoff = now - timedelta(days=7)
    public_cutoff = now - timedelta(days=90)
    full: list[dict[str, Any]] = []
    minute: dict[str, dict[str, Any]] = {}
    hourly: dict[str, dict[str, Any]] = {}
    for sample in samples:
        collected_at = sample.get("collected_at")
        if not isinstance(collected_at, str):
            continue
        try:
            observed = parse_utc(collected_at)
        except (TypeError, ValueError):
            continue
        if observed < public_cutoff:
            continue
        if observed >= full_cutoff:
            full.append(sample)
        elif observed >= minute_cutoff:
            minute[observed.strftime("%Y-%m-%dT%H:%M")] = sample
        else:
            hourly[observed.strftime("%Y-%m-%dT%H")] = sample
    return sorted(
        list(hourly.values()) + list(minute.values()) + full,
        key=lambda row: (
            str(row.get("collected_at") or ""),
            str(row.get("tick_at") or ""),
        ),
    )


class QuoteStaticExporter:
    def __init__(
        self,
        store: MonitorStore,
        site_root: str | Path,
        *,
        symbol: str = "FixedStep0.5",
        source_stale_after_seconds: int = 45,
        expected_collection_interval_seconds: int = 10,
        expected_publish_interval_seconds: int = 600,
        public_base_url: str = "https://mahvro.github.io/fixedstep-monitor",
        clock=utc_now,
        write_lock: Any | None = None,
        live_latest_path: str | Path | None = None,
    ) -> None:
        if symbol != "FixedStep0.5":
            raise ValueError("quote exporter requires the exact FixedStep0.5 symbol")
        self.store = store
        self.site_root = Path(site_root)
        self.symbol = symbol
        self.source_stale_after_seconds = int(source_stale_after_seconds)
        self.expected_collection_interval_seconds = int(
            expected_collection_interval_seconds
        )
        self.expected_publish_interval_seconds = int(expected_publish_interval_seconds)
        parsed = urlsplit(public_base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "public_base_url must be an absolute HTTP(S) URL without credentials, query, or fragment"
            )
        self.public_base_url = public_base_url.rstrip("/")
        self.clock = clock
        self.write_lock = write_lock or threading.RLock()
        self.live_latest_path = (
            Path(live_latest_path)
            if live_latest_path is not None
            else self.site_root / "ai" / "data" / "latest.json"
        )

    def public_url(self, path: str) -> str:
        return f"{self.public_base_url}/{path.lstrip('/')}"

    def _remove_legacy_public_files(self) -> None:
        ai_root = self.site_root / "ai"
        for relative in LEGACY_PUBLIC_FILES:
            path = ai_root / relative
            if path.is_file():
                path.unlink()

    @staticmethod
    def _encoded(value: dict[str, Any]) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"

    def _links(self) -> dict[str, str]:
        return {
            "dashboard": self.public_url("/dashboard/"),
            "ai": self.public_url("/ai/"),
            "manifest": self.public_url("/ai/manifest.json"),
            "latest_json": self.public_url("/ai/data/latest.json"),
            "quotes_json": self.public_url("/ai/data/quotes.json"),
            "quotes_csv": self.public_url("/ai/data/quotes.csv"),
            "price_chart": self.public_url("/ai/charts/fixedstep-price.svg"),
            "spread_chart": self.public_url("/ai/charts/fixedstep-spread.svg"),
        }

    def _base_coverage(self) -> dict[str, Any]:
        return {
            "quote_samples": 0,
            "stored_quote_samples": 0,
            "maximum_public_samples": MAX_PUBLIC_SAMPLES,
            "maximum_public_json_bytes": MAX_PUBLIC_JSON_BYTES,
            "sampling_policy": (
                f"{self.expected_collection_interval_seconds}-second source samples for "
                "the latest 24 hours; last observed sample "
                "per collector first-observation host-clock minute through day 7; last "
                "observed sample per collector first-observation host-clock hour through "
                "day 90; older samples are not "
                "published; raw MT5 time does not control retention; no interpolation"
            ),
            "chart_time_basis": CHART_TIME_BASIS,
        }

    def _saved_coverage(self) -> dict[str, Any]:
        saved = self.store.get_metadata(COVERAGE_METADATA_KEY)
        if not isinstance(saved, dict):
            return self._base_coverage()
        coverage = self._base_coverage()
        for key in (
            "quote_samples",
            "stored_quote_samples",
            "collection_start_at",
            "collection_end_at",
            "mt5_reported_start_at",
            "mt5_reported_end_at",
            "history_generated_at",
            "history_snapshot_id",
        ):
            value = saved.get(key)
            if value is not None:
                coverage[key] = value
        return coverage

    def _live_state(
        self, now: datetime
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        latest_run = self.store.latest_run()
        last_success = self.store.last_successful_quote_run()
        latest_stored = self.store.latest_quote(self.symbol)
        quote = (
            _public_quote(latest_stored, include_metadata=True)
            if latest_stored is not None
            else None
        )

        collected_at = last_success.get("attempted_at") if last_success else None
        collection_age: float | None = None
        if isinstance(collected_at, str):
            try:
                collection_age = max(
                    0.0, (now - parse_utc(collected_at)).total_seconds()
                )
            except (TypeError, ValueError):
                collected_at = None
        if latest_run and latest_run.get("connected") == 0:
            status = "disconnected"
        elif quote is None or last_success is None:
            status = "no_data"
        elif collection_age is not None and collection_age > self.source_stale_after_seconds:
            status = "stale"
        else:
            status = "live"

        collection: dict[str, Any] = {
            "status": status,
            "connected": bool(latest_run and latest_run.get("connected") == 1),
            "expected_collection_interval_seconds": self.expected_collection_interval_seconds,
            "expected_publish_interval_seconds": self.expected_publish_interval_seconds,
            "source_stale_after_seconds": self.source_stale_after_seconds,
        }
        if collected_at is not None:
            collection["collected_at"] = collected_at
        if collection_age is not None:
            collection["collection_age_seconds"] = collection_age
        if status == "disconnected":
            collection["error"] = "source_unavailable"
        return collection, quote

    def _payload(
        self,
        *,
        now: datetime,
        snapshot_id: str,
        collection: dict[str, Any],
        quote: dict[str, Any] | None,
        coverage: dict[str, Any],
        samples: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "schema_version": QUOTE_SCHEMA_VERSION,
            "snapshot_id": snapshot_id,
            "generated_at": iso_utc(now),
            "timezone": "UTC",
            "collector_clock_basis": COLLECTOR_CLOCK_BASIS,
            "timezone_applies_to": [
                "generated_at",
                "collection.collected_at",
                "quote.collected_at",
                "samples[].collected_at",
            ],
            "tick_time_basis": MT5_TICK_TIME_BASIS,
            "chart_time_basis": CHART_TIME_BASIS,
            "symbol": self.symbol,
            "collection": collection,
            "quote": quote,
            "coverage": coverage,
            "samples": samples,
            "links": self._links(),
        }

    def export_latest(self) -> dict[str, Any]:
        """Atomically refresh only the small live quote payload.

        This is the frequent collection path. It performs constant-size lookups and does
        not read quote history or rewrite CSV, charts, manifest, or the AI page.
        Coverage describes the most recently rebuilt static history bundle.
        """

        now = self.clock().astimezone(UTC)
        collection, quote = self._live_state(now)
        with self.write_lock:
            payload = self._payload(
                now=now,
                snapshot_id=str(uuid.uuid4()),
                collection=collection,
                quote=quote,
                coverage=self._saved_coverage(),
                samples=[],
            )
            _atomic_write(
                self.live_latest_path,
                self._encoded(payload),
            )
        return payload

    def export(
        self, *, after_write: Callable[[], None] | None = None
    ) -> dict[str, Any]:
        now = self.clock().astimezone(UTC)
        generated_at = iso_utc(now)
        snapshot_id = str(uuid.uuid4())
        public_cutoff = iso_utc(now - timedelta(days=PUBLIC_HISTORY_DAYS))
        raw_samples = self.store.quote_samples_for_public_window(
            public_cutoff=public_cutoff,
            minute_cutoff=iso_utc(now - timedelta(days=7)),
            full_cutoff=iso_utc(now - timedelta(hours=24)),
            symbol=self.symbol,
        )
        stored_quote_samples = self.store.quote_sample_count(self.symbol)
        safe_samples = [
            safe
            for row in raw_samples
            if (safe := _public_quote(row, include_metadata=False)) is not None
        ]
        samples = compact_quote_history(safe_samples, now=now)
        if len(samples) > MAX_PUBLIC_SAMPLES:
            samples = samples[-MAX_PUBLIC_SAMPLES:]
        collection, quote = self._live_state(now)
        coverage = self._base_coverage()
        coverage.update(
            {
                "quote_samples": len(samples),
                "stored_quote_samples": stored_quote_samples,
                "history_generated_at": generated_at,
                "history_snapshot_id": snapshot_id,
            }
        )
        if samples:
            coverage["collection_start_at"] = samples[0]["collected_at"]
            coverage["collection_end_at"] = samples[-1]["collected_at"]
            coverage["mt5_reported_start_at"] = samples[0]["tick_at"]
            coverage["mt5_reported_end_at"] = samples[-1]["tick_at"]

        payload = self._payload(
            now=now,
            snapshot_id=snapshot_id,
            collection=collection,
            quote=quote,
            coverage=coverage,
            samples=samples,
        )
        links = payload["links"]

        # A count bound alone cannot guarantee the HTTP response bound because
        # numeric and timestamp strings have variable lengths. Trim only the
        # oldest public samples until the full encoded contract has headroom.
        history_text = self._encoded(payload)
        while len(history_text.encode("utf-8")) > MAX_PUBLIC_JSON_BYTES and samples:
            remove_count = max(1, len(samples) // 10)
            del samples[:remove_count]
            coverage["quote_samples"] = len(samples)
            if samples:
                coverage["collection_start_at"] = samples[0]["collected_at"]
                coverage["collection_end_at"] = samples[-1]["collected_at"]
                coverage["mt5_reported_start_at"] = samples[0]["tick_at"]
                coverage["mt5_reported_end_at"] = samples[-1]["tick_at"]
            else:
                coverage.pop("collection_start_at", None)
                coverage.pop("collection_end_at", None)
                coverage.pop("mt5_reported_start_at", None)
                coverage.pop("mt5_reported_end_at", None)
            history_text = self._encoded(payload)

        latest_payload = {**payload, "samples": []}
        latest_text = self._encoded(latest_payload)

        data_dir = self.site_root / "ai" / "data"
        charts_dir = self.site_root / "ai" / "charts"
        quote_csv = _csv_text(samples) if samples else ",".join(QUOTE_FIELDS) + "\n"

        price_series = []
        for key, label, color in (
            ("bid", "Bid", "#53a8ff"),
            ("mid", "Mid", "#55d98b"),
            ("ask", "Ask", "#f4bd62"),
        ):
            points = [
                (str(row["collected_at"]), float(row[key]))
                for row in samples
                if _finite(row.get(key)) is not None
            ]
            price_series.append((label, color, points))
        digits = quote.get("digits") if quote else None
        price_chart = (
            _line_svg(
                "FixedStep0.5 bid, mid and ask",
                price_series,
                y_label="Quoted price",
                y_decimals=digits if isinstance(digits, int) else 3,
                time_description=(
                    "collector host-clock (UTC-serialized; synchronization not asserted)"
                ),
            )
            or _empty_chart_svg(
                "FixedStep0.5 bid, mid and ask", "No quote samples are available."
            )
        )
        spread_points = [
            (str(row["collected_at"]), float(row["spread"]))
            for row in samples
            if _finite(row.get("spread")) is not None
        ]
        spread_chart = (
            _line_svg(
                "FixedStep0.5 spread",
                [("Ask - bid", "#b797ff", spread_points)],
                y_label="Price units",
                y_decimals=digits if isinstance(digits, int) else 3,
                time_description=(
                    "collector host-clock (UTC-serialized; synchronization not asserted)"
                ),
            )
            or _empty_chart_svg(
                "FixedStep0.5 spread", "No quote samples are available."
            )
        )

        chart_contents = (
            (
                "fixedstep-price.svg",
                "FixedStep0.5 bid, mid and ask",
                price_chart,
            ),
            ("fixedstep-spread.svg", "FixedStep0.5 spread", spread_chart),
        )
        chart_entries: list[dict[str, Any]] = []
        for filename, title, content in chart_contents:
            encoded_chart = content.encode("utf-8")
            chart_entries.append(
                {
                    "title": title,
                    "format": "svg",
                    "url": self.public_url(f"/ai/charts/{filename}"),
                    "size_bytes": len(encoded_chart),
                    "sha256": hashlib.sha256(encoded_chart).hexdigest(),
                }
            )

        dataset_entries: list[dict[str, Any]] = []
        for title, file_format, filename, row_count, fields, content in (
            (
                "Latest quote snapshot",
                "json",
                "latest.json",
                1,
                sorted(latest_payload),
                latest_text,
            ),
            (
                "Quote history",
                "json",
                "quotes.json",
                len(samples),
                list(QUOTE_FIELDS),
                history_text,
            ),
            (
                "Quote history",
                "csv",
                "quotes.csv",
                len(samples),
                list(QUOTE_FIELDS),
                quote_csv,
            ),
        ):
            encoded_content = content.encode("utf-8")
            dataset_entries.append(
                {
                    "title": title,
                    "format": file_format,
                    "url": self.public_url(f"/ai/data/{filename}"),
                    "row_count": row_count,
                    "available_fields": fields,
                    "size_bytes": len(encoded_content),
                    "sha256": hashlib.sha256(encoded_content).hexdigest(),
                }
            )
        manifest = {
            "schema_version": QUOTE_SCHEMA_VERSION,
            "snapshot_id": snapshot_id,
            "generated_at": generated_at,
            "timezone": "UTC",
            "collector_clock_basis": COLLECTOR_CLOCK_BASIS,
            "timezone_applies_to": payload["timezone_applies_to"],
            "tick_time_basis": MT5_TICK_TIME_BASIS,
            "chart_time_basis": CHART_TIME_BASIS,
            "symbol": self.symbol,
            "collection": collection,
            "coverage": coverage,
            "page": links["ai"],
            "datasets": dataset_entries,
            "charts": chart_entries,
        }
        manifest_text = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
        ai_html = self._ai_html(latest_payload, chart_entries)

        # All expensive selection, validation, serialization, and chart work
        # occurs before this static-bundle lock.  The live API file has a
        # separate owner and path, so neither rendering nor optional Git
        # staging can delay or overwrite the frequent live snapshot.
        with self.write_lock:
            self._remove_legacy_public_files()
            _atomic_write(data_dir / "latest.json", latest_text)
            _atomic_write(data_dir / "quotes.json", history_text)
            _atomic_write(data_dir / "quotes.csv", quote_csv)
            for filename, _title, content in chart_contents:
                _atomic_write(charts_dir / filename, content)
            _atomic_write(self.site_root / "ai" / "manifest.json", manifest_text)
            _atomic_write(self.site_root / "ai" / "index.html", ai_html)
            # Persist only the small public-history summary. The collection path
            # reads this metadata instead of scanning or decoding history.
            self.store.set_metadata(COVERAGE_METADATA_KEY, coverage)
            if after_write is not None:
                after_write()
        return latest_payload

    def _ai_html(
        self, payload: dict[str, Any], charts: list[dict[str, Any]]
    ) -> str:
        quote = payload.get("quote") or {}
        collection = payload["collection"]

        def value(name: str) -> str:
            number = _finite(quote.get(name))
            if number is None:
                return "Unavailable"
            digits = quote.get("digits")
            decimals = digits if isinstance(digits, int) else 3
            return f"{number:.{decimals}f}"

        reported_offset = _finite(quote.get("mt5_reported_offset_seconds"))
        offset_text = (
            f"{reported_offset:+.3f} seconds"
            if reported_offset is not None
            else "Unavailable"
        )
        offset_material = bool(quote.get("mt5_reported_offset_material"))
        offset_note = (
            "comparison unavailable"
            if reported_offset is None
            else "material clock difference"
            if offset_material
            else "no material clock difference detected"
        )

        chart_html = "".join(
            f'<figure><img src="{html.escape(item["url"])}" alt="{html.escape(item["title"])}"><figcaption>{html.escape(item["title"])}</figcaption></figure>'
            for item in charts
        )
        return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>FixedStep0.5 quote data</title><style>body{{margin:0 auto;max-width:1100px;padding:32px;font:16px system-ui,sans-serif;background:#0a0e14;color:#eef4ff}}a{{color:#69b7ff}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}}.card,figure{{background:#121925;border:1px solid #26334d;border-radius:12px;padding:16px}}strong{{font-size:1.4rem}}img{{max-width:100%;height:auto}}code{{color:#c9d8ee}}</style></head>
<body><header><h1>FixedStep0.5 quote data</h1><p>Read-only sampled bid and ask. No account, position, trade or P&amp;L data is collected by this runtime.</p></header>
<section class="grid"><div class="card">Bid<br><strong>{html.escape(value("bid"))}</strong></div><div class="card">Mid<br><strong>{html.escape(value("mid"))}</strong></div><div class="card">Ask<br><strong>{html.escape(value("ask"))}</strong></div><div class="card">Spread<br><strong>{html.escape(value("spread"))}</strong></div></section>
<section><h2>Freshness and time basis</h2><ul><li>Collection status: <code>{html.escape(str(collection.get("status", "unknown")))}</code></li><li>Backend collection age: <code>{html.escape(str(collection.get("collection_age_seconds", "Unavailable")))}</code> seconds</li><li>Latest successful collector observation: <code>{html.escape(str(collection.get("collected_at", "Unavailable")))}</code> (host clock, UTC-serialized; synchronization not asserted)</li><li>Quote first observed by collector: <code>{html.escape(str(quote.get("collected_at", "Unavailable")))}</code> (host clock; chart axis)</li><li>MT5-reported tick timestamp: <code>{html.escape(str(quote.get("tick_at", "Unavailable")))}</code> (preserved raw; time basis unverified; never used for freshness)</li><li>MT5-reported minus collector first-observation time: <code>{html.escape(offset_text)}</code> ({html.escape(offset_note)})</li><li>Generated: <code>{html.escape(str(payload["generated_at"]))}</code> (collector host clock)</li></ul></section>
<section><h2>Machine-readable data</h2><ul><li><a href="{html.escape(payload["links"]["latest_json"])}">Latest JSON</a></li><li><a href="{html.escape(payload["links"]["quotes_json"])}">Quote history JSON</a></li><li><a href="{html.escape(payload["links"]["quotes_csv"])}">Quote history CSV</a></li><li><a href="{html.escape(payload["links"]["manifest"])}">Manifest</a></li></ul></section>
<section><h2>Static charts</h2><p>Chart axes use collector first-observation host time, not the unverified MT5-reported timestamp. Values are UTC-serialized, but host synchronization is not asserted.</p>{chart_html or '<p>No quote samples are available yet.</p>'}</section>
<footer><p><a href="{html.escape(payload["links"]["dashboard"])}">Interactive dashboard</a> · Source sampling target: {self.expected_collection_interval_seconds}s · Static fallback publication target: {self.expected_publish_interval_seconds}s.</p></footer></body></html>
"""
