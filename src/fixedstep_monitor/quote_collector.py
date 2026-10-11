from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from .quote_exporter import QuoteStaticExporter
from .storage import MonitorStore
from .timeutil import iso_utc, utc_now


class QuoteSource(Protocol):
    @property
    def scope(self) -> dict[str, Any]: ...

    def connect(self) -> None: ...

    def collect(self, *, history_start=None) -> dict[str, Any]: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class QuoteCollectorConfig:
    interval_seconds: int = 10

    def __post_init__(self) -> None:
        if not 10 <= self.interval_seconds <= 60:
            raise ValueError("interval_seconds must be between 10 and 60")


class QuoteCollector:
    """Collect one exact-symbol MT5 quote and refresh the live payload."""

    def __init__(
        self,
        store: MonitorStore,
        exporter: QuoteStaticExporter,
        adapter_factory: Callable[[], QuoteSource],
        *,
        config: QuoteCollectorConfig | None = None,
        clock=utc_now,
    ) -> None:
        self.store = store
        self.exporter = exporter
        self.adapter_factory = adapter_factory
        self.config = config or QuoteCollectorConfig()
        self.clock = clock

    def run_once(self) -> dict[str, Any]:
        attempted = self.clock()
        run_id = self.store.begin_run(attempted_at=iso_utc(attempted))
        adapter: QuoteSource | None = None
        try:
            adapter = self.adapter_factory()
            self.store.set_metadata("quote_scope", adapter.scope)
            adapter.connect()
            snapshot = adapter.collect()
            self.store.record_quote_success(run_id, snapshot)
        except Exception as exc:
            # Never serialize the source exception: it can contain terminal paths,
            # broker endpoints, or other private diagnostics.
            category = f"{type(exc).__name__}: quote collection failed"
            self.store.record_failure(run_id, category)
        finally:
            if adapter is not None:
                try:
                    adapter.close()
                except Exception:
                    pass
        return self.exporter.export_latest()

    def run_forever(
        self,
        stop_event: threading.Event | None = None,
        *,
        after_cycle: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        stop = stop_event or threading.Event()
        while not stop.is_set():
            started = time.monotonic()
            try:
                payload = self.run_once()
                if after_cycle is not None:
                    after_cycle(payload)
            except Exception:
                # Atomic export means a failed cycle leaves the previous complete
                # public snapshot available to both the API and static fallback.
                pass
            remaining = max(
                0.0, self.config.interval_seconds - (time.monotonic() - started)
            )
            stop.wait(remaining)
