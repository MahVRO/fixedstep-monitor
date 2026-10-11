from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from importlib import import_module
import math
from typing import Any

from .timeutil import epoch_to_iso, iso_utc, utc_now


READ_METHODS = frozenset(
    {
        "initialize",
        "shutdown",
        "terminal_info",
        "account_info",
        "positions_get",
        "history_deals_get",
        "history_orders_get",
    }
)

# The quote monitor intentionally has a smaller surface than the legacy account
# monitor.  Keep this allowlist separate so adding an information call to the
# legacy adapter cannot silently broaden the process that runs every 15 seconds.
QUOTE_READ_METHODS = frozenset(
    {
        "initialize",
        "shutdown",
        "terminal_info",
        "symbol_info",
        "symbol_info_tick",
    }
)


class ReadOnlyMT5Error(RuntimeError):
    pass


def _mapping(record: Any) -> dict[str, Any]:
    if record is None:
        return {}
    if isinstance(record, Mapping):
        return dict(record)
    converter = getattr(record, "_asdict", None)
    if converter is not None:
        return dict(converter())
    return {
        name: getattr(record, name)
        for name in dir(record)
        if not name.startswith("_") and not callable(getattr(record, name, None))
    }


def _number(record: Mapping[str, Any], name: str) -> int | float | None:
    value = record.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(float(value)) else None


def _text(record: Mapping[str, Any], name: str) -> str | None:
    value = record.get(name)
    return value if isinstance(value, str) and value else None


def _put_if(output: dict[str, Any], key: str, value: Any) -> None:
    if value is not None:
        output[key] = value


class QuoteOnlyMT5:
    """Read one exact symbol quote without touching account or trading APIs."""

    def __init__(
        self,
        module: Any | None = None,
        *,
        symbol: str = "FixedStep0.5",
        clock=utc_now,
    ) -> None:
        if symbol != "FixedStep0.5":
            raise ValueError("quote monitor requires the exact FixedStep0.5 symbol")
        self._module = module
        self.symbol = str(symbol)
        self._clock = clock
        self._initialized = False

    @property
    def scope(self) -> dict[str, Any]:
        return {"symbol": self.symbol, "quote_only": True, "filtered": True}

    def _load(self) -> Any:
        if self._module is None:
            try:
                self._module = import_module("MetaTrader5")
            except ImportError as exc:
                raise ReadOnlyMT5Error(
                    "MetaTrader5 is unavailable; install the optional 'mt5' extra on the MT5 host"
                ) from exc
        return self._module

    def _call(self, name: str, *args: Any) -> Any:
        if name not in QUOTE_READ_METHODS:
            raise ReadOnlyMT5Error(
                f"MT5 method is not in the quote-only allowlist: {name!r}"
            )
        method = getattr(self._load(), name, None)
        if not callable(method):
            raise ReadOnlyMT5Error(f"Required MT5 quote method is unavailable: {name}")
        return method(*args)

    def connect(self) -> None:
        if self._initialized:
            return
        if not self._call("initialize"):
            raise ReadOnlyMT5Error("MT5 initialization failed")
        self._initialized = True
        terminal = _mapping(self._call("terminal_info"))
        if terminal and not bool(terminal.get("connected", True)):
            self.close()
            raise ReadOnlyMT5Error("MT5 terminal reports disconnected")

    def close(self) -> None:
        if self._initialized:
            try:
                self._call("shutdown")
            finally:
                self._initialized = False

    def __enter__(self) -> "QuoteOnlyMT5":
        self.connect()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def collect(self, *, history_start: datetime | None = None) -> dict[str, Any]:
        del history_start  # Kept only for compatibility with the collector protocol.
        collected_at = self._clock().astimezone(UTC)
        info = _mapping(self._call("symbol_info", self.symbol))
        if not info:
            raise ReadOnlyMT5Error(f"MT5 symbol information is unavailable for {self.symbol!r}")
        reported_name = _text(info, "name")
        if reported_name is not None and reported_name != self.symbol:
            raise ReadOnlyMT5Error("MT5 returned a different symbol than requested")

        tick = _mapping(self._call("symbol_info_tick", self.symbol))
        if not tick:
            raise ReadOnlyMT5Error(f"MT5 quote is unavailable for {self.symbol!r}")
        bid = _number(tick, "bid")
        ask = _number(tick, "ask")
        if bid is None or ask is None or float(bid) <= 0 or float(ask) <= 0:
            raise ReadOnlyMT5Error("MT5 bid/ask quote is unavailable")
        if float(ask) < float(bid):
            raise ReadOnlyMT5Error("MT5 quote has ask below bid")

        time_msc = _number(tick, "time_msc")
        if time_msc is not None and float(time_msc) <= 0:
            time_msc = None
        tick_seconds = _number(tick, "time")
        if tick_seconds is not None and float(tick_seconds) <= 0:
            tick_seconds = None
        tick_at = epoch_to_iso(
            tick_seconds, int(time_msc) if time_msc is not None else None
        )
        if tick_at is None:
            raise ReadOnlyMT5Error("MT5 quote timestamp is unavailable")

        point = _number(info, "point")
        if point is not None and float(point) <= 0:
            point = None
        tick_size = _number(info, "trade_tick_size")
        if tick_size is not None and float(tick_size) <= 0:
            tick_size = None
        digits_value = _number(info, "digits")
        digits = None
        if (
            digits_value is not None
            and float(digits_value).is_integer()
            and 0 <= int(digits_value) <= 10
        ):
            digits = int(digits_value)

        bid_value = float(bid)
        ask_value = float(ask)
        spread = ask_value - bid_value
        quote: dict[str, Any] = {
            "symbol": self.symbol,
            "collected_at": iso_utc(collected_at),
            "tick_at": tick_at,
            "tick_time_msc": int(time_msc) if time_msc is not None else int(
                datetime.fromisoformat(tick_at.replace("Z", "+00:00")).timestamp() * 1000
            ),
            "bid": bid_value,
            "ask": ask_value,
            "mid": (bid_value + ask_value) / 2.0,
            "spread": spread,
        }
        last = _number(tick, "last")
        # This broker reports 0.0 when Last is unavailable.  It is not a real
        # positive-price observation and must remain absent/null publicly.
        if last is not None and float(last) > 0:
            quote["last"] = float(last)
        if point is not None:
            quote["point"] = float(point)
            quote["spread_points"] = spread / float(point)
        if tick_size is not None:
            quote["trade_tick_size"] = float(tick_size)
        if digits is not None:
            quote["digits"] = digits
        return {"observed_at": iso_utc(collected_at), "quote": quote}


class ReadOnlyMT5:
    """Small, auditable wrapper around the MT5 information API.

    It deliberately accepts no credentials or terminal path. The existing terminal
    session must already be authenticated. Every callable name is checked against
    ``READ_METHODS`` immediately before invocation.
    """

    def __init__(
        self,
        module: Any | None = None,
        *,
        symbols: Iterable[str] = ("FixedStep0.5",),
        magic_numbers: Iterable[int] = (),
        clock=utc_now,
    ) -> None:
        self._module = module
        self._symbols = frozenset(str(symbol) for symbol in symbols if str(symbol))
        self._magic_numbers = frozenset(int(number) for number in magic_numbers)
        self._clock = clock
        self._initialized = False

    @property
    def scope(self) -> dict[str, Any]:
        return {
            "symbols": sorted(self._symbols),
            "magic_filter_configured": bool(self._magic_numbers),
            "filtered": bool(self._symbols or self._magic_numbers),
        }

    def _in_scope(self, record: Mapping[str, Any]) -> bool:
        if self._symbols and record.get("symbol") not in self._symbols:
            return False
        if self._magic_numbers and record.get("magic") not in self._magic_numbers:
            return False
        return True

    def _load(self) -> Any:
        if self._module is None:
            try:
                self._module = import_module("MetaTrader5")
            except ImportError as exc:
                raise ReadOnlyMT5Error(
                    "MetaTrader5 is unavailable; install the optional 'mt5' extra on the MT5 host"
                ) from exc
        return self._module

    def _call(self, name: str, *args: Any) -> Any:
        if name not in READ_METHODS:
            raise ReadOnlyMT5Error(f"MT5 method is not in the read-only allowlist: {name!r}")
        method = getattr(self._load(), name, None)
        if not callable(method):
            raise ReadOnlyMT5Error(f"Required MT5 information method is unavailable: {name}")
        return method(*args)

    def connect(self) -> None:
        if self._initialized:
            return
        if not self._call("initialize"):
            raise ReadOnlyMT5Error("MT5 initialization failed")
        self._initialized = True
        terminal = _mapping(self._call("terminal_info"))
        if terminal and not bool(terminal.get("connected", True)):
            self.close()
            raise ReadOnlyMT5Error("MT5 terminal reports disconnected")

    def close(self) -> None:
        if self._initialized:
            try:
                self._call("shutdown")
            finally:
                self._initialized = False

    def __enter__(self) -> "ReadOnlyMT5":
        self.connect()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def account_snapshot(self, observed_at: datetime | None = None) -> dict[str, Any]:
        record = _mapping(self._call("account_info"))
        if not record:
            raise ReadOnlyMT5Error("MT5 account information is unavailable")
        output: dict[str, Any] = {"observed_at": iso_utc(observed_at or self._clock())}
        for source, target in (
            ("balance", "balance"),
            ("equity", "equity"),
            ("profit", "unrealized_pnl"),
            ("margin", "margin"),
            ("margin_free", "free_margin"),
            ("margin_level", "margin_level"),
        ):
            _put_if(output, target, _number(record, source))
        _put_if(output, "currency", _text(record, "currency"))
        return output

    def open_positions(self, observed_at: datetime | None = None) -> list[dict[str, Any]]:
        records = self._call("positions_get")
        if records is None:
            raise ReadOnlyMT5Error("MT5 open-position information is unavailable")
        timestamp = iso_utc(observed_at or self._clock())
        positions: list[dict[str, Any]] = []
        for raw in records:
            record = _mapping(raw)
            if not self._in_scope(record):
                continue
            ticket = record.get("ticket")
            if not isinstance(ticket, int):
                continue
            item: dict[str, Any] = {"ticket": ticket, "observed_at": timestamp}
            position_type = _number(record, "type")
            if position_type == 0:
                item["direction"] = "buy"
            elif position_type == 1:
                item["direction"] = "sell"
            for source, target in (
                ("symbol", "symbol"),
                ("volume", "volume"),
                ("price_open", "entry_price"),
                ("price_current", "current_price"),
                ("sl", "stop_loss"),
                ("tp", "take_profit"),
                ("profit", "unrealized_pnl"),
                ("swap", "swap"),
                ("magic", "magic"),
            ):
                value = _text(record, source) if source == "symbol" else _number(record, source)
                _put_if(item, target, value)
            time_msc = _number(record, "time_msc")
            opened = epoch_to_iso(
                _number(record, "time"), int(time_msc) if time_msc is not None else None
            )
            _put_if(item, "entry_time", opened)
            positions.append(item)
        return positions

    def closed_trades(
        self,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[dict[str, Any]]:
        end_utc = (end or self._clock()).astimezone(UTC)
        start_utc = (start or (end_utc - timedelta(days=3650))).astimezone(UTC)
        raw_records = self._call("history_deals_get", start_utc, end_utc)
        if raw_records is None:
            raise ReadOnlyMT5Error("MT5 deal history is unavailable")
        records = [_mapping(record) for record in raw_records]
        records = [record for record in records if self._in_scope(record)]
        raw_orders = self._call("history_orders_get", start_utc, end_utc)
        if raw_orders is None:
            raise ReadOnlyMT5Error("MT5 order history is unavailable")
        orders = [_mapping(record) for record in raw_orders]
        orders = [record for record in orders if self._in_scope(record)]
        orders_by_ticket = {
            record["ticket"]: record for record in orders if isinstance(record.get("ticket"), int)
        }
        orders_by_position: dict[int, list[dict[str, Any]]] = {}
        for order in orders:
            position_id = order.get("position_id")
            if isinstance(position_id, int):
                orders_by_position.setdefault(position_id, []).append(order)
        entries_by_position: dict[int, list[dict[str, Any]]] = {}
        for record in records:
            position_id = record.get("position_id")
            if isinstance(position_id, int) and record.get("entry") == 0 and record.get("type") in (0, 1):
                entries_by_position.setdefault(position_id, []).append(record)

        closed: list[dict[str, Any]] = []
        for record in records:
            deal_id = record.get("ticket")
            position_id = record.get("position_id")
            if not isinstance(deal_id, int) or record.get("entry") not in (1, 2, 3):
                continue
            if record.get("type") not in (0, 1):
                continue
            trade: dict[str, Any] = {"deal_id": deal_id}
            if isinstance(position_id, int):
                trade["position_id"] = position_id
            order_id = record.get("order")
            if isinstance(order_id, int):
                trade["order_id"] = order_id
            magic = record.get("magic")
            if isinstance(magic, int):
                trade["magic"] = magic
            for source, target in (("symbol", "symbol"), ("volume", "volume"), ("price", "exit_price")):
                value = _text(record, source) if source == "symbol" else _number(record, source)
                _put_if(trade, target, value)
            exit_msc = _number(record, "time_msc")
            exit_time = epoch_to_iso(
                _number(record, "time"), int(exit_msc) if exit_msc is not None else None
            )
            _put_if(trade, "exit_time", exit_time)

            related_entries = entries_by_position.get(position_id, []) if isinstance(position_id, int) else []
            if related_entries:
                ordered = sorted(related_entries, key=lambda row: row.get("time_msc", row.get("time", 0)))
                entry_msc = _number(ordered[0], "time_msc")
                entry_time = epoch_to_iso(
                    _number(ordered[0], "time"), int(entry_msc) if entry_msc is not None else None
                )
                _put_if(trade, "entry_time", entry_time)
                entry_type = ordered[0].get("type")
                if entry_type == 0:
                    trade["direction"] = "buy"
                elif entry_type == 1:
                    trade["direction"] = "sell"
                weighted: list[tuple[float, float]] = []
                for entry in ordered:
                    price = _number(entry, "price")
                    volume = _number(entry, "volume")
                    if price is not None and volume is not None and float(volume) > 0:
                        weighted.append((float(price), float(volume)))
                if weighted:
                    total_volume = sum(volume for _, volume in weighted)
                    trade["entry_price"] = sum(price * volume for price, volume in weighted) / total_volume

            related_order = orders_by_ticket.get(order_id) if isinstance(order_id, int) else None
            if related_order is None and isinstance(position_id, int):
                candidates = orders_by_position.get(position_id, [])
                related_order = candidates[-1] if candidates else None
            if related_order:
                for source, target in (("sl", "stop_loss"), ("tp", "take_profit")):
                    value = _number(related_order, source)
                    if value is not None and float(value) != 0.0:
                        trade[target] = float(value)

            components: list[float] = []
            for source, target in (
                ("profit", "profit"),
                ("commission", "commission"),
                ("swap", "swap"),
                ("fee", "fee"),
            ):
                value = _number(record, source)
                if value is not None:
                    numeric = float(value)
                    trade[target] = numeric
                    components.append(numeric)
            if components:
                trade["net_profit"] = sum(components)
            if trade.get("entry_time") and trade.get("exit_time"):
                entry_dt = datetime.fromisoformat(str(trade["entry_time"]).replace("Z", "+00:00"))
                exit_dt = datetime.fromisoformat(str(trade["exit_time"]).replace("Z", "+00:00"))
                trade["duration_seconds"] = max(0.0, (exit_dt - entry_dt).total_seconds())
            closed.append(trade)
        return closed

    def collect(self, *, history_start: datetime | None = None) -> dict[str, Any]:
        observed_at = self._clock()
        return {
            "observed_at": iso_utc(observed_at),
            "account": self.account_snapshot(observed_at),
            "positions": self.open_positions(observed_at),
            "closed_trades": self.closed_trades(start=history_start, end=observed_at),
        }
