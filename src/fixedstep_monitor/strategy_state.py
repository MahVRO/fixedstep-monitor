from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .timeutil import iso_utc


class StrategyStateError(RuntimeError):
    pass


def _first(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return None


class StrategyStateReader:
    """Read an optional strategy-owned JSON status file without modifying it."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def read(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise StrategyStateError("strategy-state JSON could not be read") from exc
        if not isinstance(raw, dict):
            raise StrategyStateError("strategy-state JSON must contain an object")

        state: dict[str, Any] = {}
        observed_at = _first(raw, "observed_at", "updated_at", "timestamp")
        if not isinstance(observed_at, str) or not observed_at:
            try:
                observed_at = iso_utc(datetime.fromtimestamp(self.path.stat().st_mtime, UTC))
            except OSError as exc:
                raise StrategyStateError("strategy-state timestamp is unavailable") from exc
        state["observed_at"] = observed_at

        status = _first(raw, "status", "strategy_status")
        symbol = _first(raw, "symbol", "active_symbol")
        step = _first(raw, "step", "current_step")
        level = _first(raw, "level", "current_level")
        if isinstance(status, str) and status:
            state["status"] = status
        if isinstance(symbol, str) and symbol:
            state["symbol"] = symbol
        if isinstance(step, (int, float)) and not isinstance(step, bool) and math.isfinite(float(step)):
            state["step"] = step
        if isinstance(level, (int, float)) and not isinstance(level, bool) and math.isfinite(float(level)):
            state["level"] = level

        internal = _first(raw, "internal_state", "state")
        if isinstance(internal, dict):
            state["internal_state"] = internal
        events = raw.get("events")
        if isinstance(events, list):
            state["events"] = [dict(event) for event in events if isinstance(event, dict)]
        return state
