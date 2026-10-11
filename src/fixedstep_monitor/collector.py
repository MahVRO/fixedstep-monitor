from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Callable, Protocol

from .exporter import StaticExporter
from .storage import MonitorStore
from .strategy_state import StrategyStateError, StrategyStateReader
from .timeutil import iso_utc, parse_utc, utc_now


class SourceAdapter(Protocol):
    @property
    def scope(self) -> dict[str, Any]: ...

    def connect(self) -> None: ...

    def collect(self, *, history_start: datetime | None = None) -> dict[str, Any]: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class CollectorConfig:
    interval_seconds: int = 60
    initial_history_days: int = 3650
    history_overlap_hours: int = 24
    initial_capital: float | None = None

    def __post_init__(self) -> None:
        if not 30 <= self.interval_seconds <= 60:
            raise ValueError("interval_seconds must be between 30 and 60")
        if self.initial_history_days < 1:
            raise ValueError("initial_history_days must be positive")
        if self.history_overlap_hours < 1:
            raise ValueError("history_overlap_hours must be positive")


class Collector:
    def __init__(
        self,
        store: MonitorStore,
        exporter: StaticExporter,
        adapter_factory: Callable[[], SourceAdapter],
        *,
        strategy_reader: StrategyStateReader | None = None,
        config: CollectorConfig | None = None,
        clock=utc_now,
    ) -> None:
        self.store = store
        self.exporter = exporter
        self.adapter_factory = adapter_factory
        self.strategy_reader = strategy_reader
        self.config = config or CollectorConfig()
        self.clock = clock
        if self.config.initial_capital is not None:
            self.store.set_metadata("initial_capital", float(self.config.initial_capital))

    def _history_start(self, now: datetime) -> datetime:
        candidates = [self.store.latest_trade_exit(), self.store.get_metadata("history_scanned_through")]
        parsed: list[datetime] = []
        for candidate in candidates:
            if not candidate:
                continue
            try:
                parsed.append(parse_utc(str(candidate)))
            except (TypeError, ValueError):
                pass
        if parsed:
            return max(parsed) - timedelta(hours=self.config.history_overlap_hours)
        return now.astimezone(UTC) - timedelta(days=self.config.initial_history_days)

    def run_once(self) -> dict[str, Any]:
        attempted = self.clock().astimezone(UTC)
        run_id = self.store.begin_run(attempted_at=iso_utc(attempted))
        adapter: SourceAdapter | None = None
        try:
            adapter = self.adapter_factory()
            self.store.set_metadata("trade_scope", adapter.scope)
            adapter.connect()
            snapshot = adapter.collect(history_start=self._history_start(attempted))
            strategy: dict[str, Any] | None = None
            if self.strategy_reader is not None:
                try:
                    strategy = self.strategy_reader.read()
                    self.store.set_metadata("strategy_state_status", "available" if strategy else "not_found")
                except StrategyStateError:
                    self.store.set_metadata("strategy_state_status", "unreadable")
            self.store.record_success(run_id, snapshot, strategy=strategy)
            observed_at = snapshot.get("observed_at")
            if isinstance(observed_at, str):
                self.store.set_metadata("history_scanned_through", observed_at)
        except Exception as exc:
            category = f"{type(exc).__name__}: source collection failed"
            self.store.record_failure(run_id, category)
        finally:
            if adapter is not None:
                try:
                    adapter.close()
                except Exception:
                    pass
        return self.exporter.export()

    def run_forever(self, stop_event: threading.Event | None = None) -> None:
        stop = stop_event or threading.Event()
        while not stop.is_set():
            started = time.monotonic()
            try:
                self.run_once()
            except Exception:
                # Export failures are independent of both the source and the next run.
                pass
            remaining = max(0.0, self.config.interval_seconds - (time.monotonic() - started))
            stop.wait(remaining)
