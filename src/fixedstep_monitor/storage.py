from __future__ import annotations

import json
import math
import sqlite3
import uuid
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .timeutil import iso_utc, utc_now


_SCHEMA = """
CREATE TABLE IF NOT EXISTS collection_runs (
    run_id TEXT PRIMARY KEY,
    attempted_at TEXT NOT NULL,
    completed_at TEXT,
    connected INTEGER,
    error TEXT
);

CREATE TABLE IF NOT EXISTS account_samples (
    observed_at TEXT PRIMARY KEY,
    run_id TEXT NOT NULL UNIQUE,
    balance REAL,
    equity REAL,
    unrealized_pnl REAL,
    margin REAL,
    free_margin REAL,
    margin_level REAL,
    currency TEXT,
    FOREIGN KEY(run_id) REFERENCES collection_runs(run_id)
);

CREATE TABLE IF NOT EXISTS position_snapshots (
    run_id TEXT NOT NULL,
    ticket TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    data_json TEXT NOT NULL,
    PRIMARY KEY(run_id, ticket),
    FOREIGN KEY(run_id) REFERENCES collection_runs(run_id)
);

CREATE TABLE IF NOT EXISTS closed_trades (
    deal_id TEXT PRIMARY KEY,
    exit_time TEXT,
    net_profit REAL,
    duration_seconds REAL,
    data_json TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS strategy_samples (
    observed_at TEXT PRIMARY KEY,
    run_id TEXT NOT NULL UNIQUE,
    state_json TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES collection_runs(run_id)
);

CREATE TABLE IF NOT EXISTS strategy_events (
    event_id TEXT PRIMARY KEY,
    observed_at TEXT,
    event_json TEXT NOT NULL,
    first_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS monitor_metadata (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS quote_samples (
    symbol TEXT NOT NULL,
    tick_time_msc INTEGER NOT NULL,
    tick_at TEXT NOT NULL,
    first_collected_at TEXT NOT NULL,
    last_collected_at TEXT NOT NULL,
    data_json TEXT NOT NULL,
    PRIMARY KEY(symbol, tick_time_msc)
);

CREATE INDEX IF NOT EXISTS idx_runs_attempted ON collection_runs(attempted_at);
CREATE INDEX IF NOT EXISTS idx_account_observed ON account_samples(observed_at);
CREATE INDEX IF NOT EXISTS idx_trades_exit ON closed_trades(exit_time);
CREATE INDEX IF NOT EXISTS idx_strategy_observed ON strategy_samples(observed_at);
CREATE INDEX IF NOT EXISTS idx_events_observed ON strategy_events(observed_at);
CREATE INDEX IF NOT EXISTS idx_quotes_tick_at ON quote_samples(symbol, tick_at);
"""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _loads(value: str) -> Any:
    return json.loads(value)


def _numeric(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    return numeric if math.isfinite(numeric) else None


class MonitorStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=30.0)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA busy_timeout=30000")
        self.connection.executescript(_SCHEMA)
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "MonitorStore":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    @contextmanager
    def transaction(self):
        with self.connection:
            yield

    @property
    def journal_mode(self) -> str:
        row = self.connection.execute("PRAGMA journal_mode").fetchone()
        return str(row[0]).lower()

    def begin_run(self, attempted_at: str | None = None, run_id: str | None = None) -> str:
        identifier = run_id or str(uuid.uuid4())
        timestamp = attempted_at or iso_utc(utc_now())
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO collection_runs(run_id, attempted_at) VALUES (?, ?)",
                (identifier, timestamp),
            )
        return identifier

    def record_success(
        self,
        run_id: str,
        snapshot: Mapping[str, Any],
        *,
        strategy: Mapping[str, Any] | None = None,
        completed_at: str | None = None,
    ) -> None:
        observed_at = str(snapshot["observed_at"])
        account = dict(snapshot.get("account") or {})
        positions = list(snapshot.get("positions") or [])
        trades = list(snapshot.get("closed_trades") or [])
        finished = completed_at or iso_utc(utc_now())
        with self.connection:
            self.connection.execute(
                """
                INSERT OR IGNORE INTO account_samples(
                    observed_at, run_id, balance, equity, unrealized_pnl,
                    margin, free_margin, margin_level, currency
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observed_at,
                    run_id,
                    _numeric(account.get("balance")),
                    _numeric(account.get("equity")),
                    _numeric(account.get("unrealized_pnl")),
                    _numeric(account.get("margin")),
                    _numeric(account.get("free_margin")),
                    _numeric(account.get("margin_level")),
                    account.get("currency") if isinstance(account.get("currency"), str) else None,
                ),
            )
            for position in positions:
                ticket = position.get("ticket")
                if ticket is None:
                    continue
                item = {key: value for key, value in dict(position).items() if value is not None}
                self.connection.execute(
                    """
                    INSERT OR REPLACE INTO position_snapshots(run_id, ticket, observed_at, data_json)
                    VALUES (?, ?, ?, ?)
                    """,
                    (run_id, str(ticket), observed_at, _json(item)),
                )
            for trade in trades:
                deal_id = trade.get("deal_id")
                if deal_id is None:
                    continue
                item = {key: value for key, value in dict(trade).items() if value is not None}
                existing = self.connection.execute(
                    "SELECT data_json FROM closed_trades WHERE deal_id = ?", (str(deal_id),)
                ).fetchone()
                if existing:
                    item = {**_loads(existing["data_json"]), **item}
                self.connection.execute(
                    """
                    INSERT INTO closed_trades(
                        deal_id, exit_time, net_profit, duration_seconds, data_json,
                        first_seen_at, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(deal_id) DO UPDATE SET
                        exit_time = COALESCE(excluded.exit_time, closed_trades.exit_time),
                        net_profit = COALESCE(excluded.net_profit, closed_trades.net_profit),
                        duration_seconds = COALESCE(excluded.duration_seconds, closed_trades.duration_seconds),
                        data_json = excluded.data_json,
                        last_seen_at = excluded.last_seen_at
                    """,
                    (
                        str(deal_id),
                        item.get("exit_time"),
                        _numeric(item.get("net_profit")),
                        _numeric(item.get("duration_seconds")),
                        _json(item),
                        observed_at,
                        observed_at,
                    ),
                )
            if strategy:
                state = {key: value for key, value in dict(strategy).items() if key != "events" and value is not None}
                strategy_observed = str(state.get("observed_at") or observed_at)
                state["observed_at"] = strategy_observed
                self.connection.execute(
                    """
                    INSERT OR REPLACE INTO strategy_samples(observed_at, run_id, state_json)
                    VALUES (?, ?, ?)
                    """,
                    (strategy_observed, run_id, _json(state)),
                )
                for event in strategy.get("events") or []:
                    if not isinstance(event, Mapping):
                        continue
                    event_item = {key: value for key, value in dict(event).items() if value is not None}
                    event_id = event_item.get("event_id")
                    if not event_id:
                        event_id = str(uuid.uuid5(uuid.NAMESPACE_URL, _json(event_item)))
                        event_item["event_id"] = event_id
                    event_time = event_item.get("timestamp") or event_item.get("observed_at") or strategy_observed
                    self.connection.execute(
                        """
                        INSERT OR IGNORE INTO strategy_events(event_id, observed_at, event_json, first_seen_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        (str(event_id), str(event_time), _json(event_item), observed_at),
                    )
            self.connection.execute(
                """
                UPDATE collection_runs
                SET completed_at = ?, connected = 1, error = NULL
                WHERE run_id = ?
                """,
                (finished, run_id),
            )

    def record_failure(self, run_id: str, error: str, *, completed_at: str | None = None) -> None:
        finished = completed_at or iso_utc(utc_now())
        with self.connection:
            self.connection.execute(
                """
                UPDATE collection_runs
                SET completed_at = ?, connected = 0, error = ?
                WHERE run_id = ?
                """,
                (finished, str(error)[:2000], run_id),
            )

    def record_quote_success(
        self,
        run_id: str,
        snapshot: Mapping[str, Any],
        *,
        completed_at: str | None = None,
    ) -> None:
        """Persist one sampled quote and mark its collection run successful.

        The primary key is the broker tick timestamp, not the polling timestamp.
        Re-reading an unchanged weekend/closed-market tick therefore updates its
        last-seen time without inventing another market observation.
        """

        observed_at = str(snapshot["observed_at"])
        quote = dict(snapshot.get("quote") or {})
        symbol = quote.get("symbol")
        tick_at = quote.get("tick_at")
        tick_time_msc = quote.get("tick_time_msc")
        if not isinstance(symbol, str) or not symbol:
            raise ValueError("quote symbol is required")
        if not isinstance(tick_at, str) or not tick_at:
            raise ValueError("quote tick_at is required")
        if isinstance(tick_time_msc, bool) or not isinstance(tick_time_msc, int):
            raise ValueError("quote tick_time_msc is required")

        item = {
            key: value
            for key, value in quote.items()
            if key != "tick_time_msc" and value is not None
        }
        item["collected_at"] = observed_at
        finished = completed_at or iso_utc(utc_now())
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO quote_samples(
                    symbol, tick_time_msc, tick_at, first_collected_at,
                    last_collected_at, data_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, tick_time_msc) DO UPDATE SET
                    tick_at = excluded.tick_at,
                    last_collected_at = excluded.last_collected_at,
                    data_json = excluded.data_json
                """,
                (
                    symbol,
                    tick_time_msc,
                    tick_at,
                    observed_at,
                    observed_at,
                    _json(item),
                ),
            )
            self.connection.execute(
                """
                UPDATE collection_runs
                SET completed_at = ?, connected = 1, error = NULL
                WHERE run_id = ?
                """,
                (finished, run_id),
            )

    def set_metadata(self, key: str, value: Any) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO monitor_metadata(key, value_json) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json
                """,
                (key, _json(value)),
            )

    def get_metadata(self, key: str, default: Any = None) -> Any:
        row = self.connection.execute(
            "SELECT value_json FROM monitor_metadata WHERE key = ?", (key,)
        ).fetchone()
        return default if row is None else _loads(row["value_json"])

    def latest_run(self) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM collection_runs ORDER BY attempted_at DESC, run_id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    def last_successful_quote_run(self) -> dict[str, Any] | None:
        row = self.connection.execute(
            """
            SELECT * FROM collection_runs
            WHERE connected = 1
            ORDER BY attempted_at DESC, run_id DESC LIMIT 1
            """
        ).fetchone()
        return dict(row) if row else None

    def last_successful_run(self) -> dict[str, Any] | None:
        row = self.connection.execute(
            """
            SELECT r.*, a.observed_at
            FROM collection_runs AS r
            JOIN account_samples AS a ON a.run_id = r.run_id
            WHERE r.connected = 1
            ORDER BY a.observed_at DESC, r.run_id DESC LIMIT 1
            """
        ).fetchone()
        return dict(row) if row else None

    def account_samples(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT observed_at, balance, equity, unrealized_pnl, margin,
                   free_margin, margin_level, currency
            FROM account_samples ORDER BY observed_at, run_id
            """
        ).fetchall()
        return [
            {key: row[key] for key in row.keys() if row[key] is not None}
            for row in rows
        ]

    def latest_account(self) -> dict[str, Any] | None:
        rows = self.account_samples()
        return rows[-1] if rows else None

    def latest_positions(self) -> list[dict[str, Any]]:
        run = self.last_successful_run()
        if not run:
            return []
        rows = self.connection.execute(
            "SELECT data_json FROM position_snapshots WHERE run_id = ? ORDER BY ticket",
            (run["run_id"],),
        ).fetchall()
        return [_loads(row["data_json"]) for row in rows]

    def closed_trades(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT data_json FROM closed_trades
            ORDER BY COALESCE(exit_time, first_seen_at), deal_id
            """
        ).fetchall()
        return [_loads(row["data_json"]) for row in rows]

    def latest_trade_exit(self) -> str | None:
        row = self.connection.execute(
            "SELECT MAX(exit_time) AS exit_time FROM closed_trades WHERE exit_time IS NOT NULL"
        ).fetchone()
        return str(row["exit_time"]) if row and row["exit_time"] else None

    def latest_strategy(self) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT state_json FROM strategy_samples ORDER BY observed_at DESC LIMIT 1"
        ).fetchone()
        return _loads(row["state_json"]) if row else None

    def strategy_samples(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT state_json FROM strategy_samples ORDER BY observed_at"
        ).fetchall()
        return [_loads(row["state_json"]) for row in rows]

    def strategy_events(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT event_json FROM strategy_events ORDER BY observed_at, event_id"
        ).fetchall()
        return [_loads(row["event_json"]) for row in rows]

    def quote_samples(self, symbol: str = "FixedStep0.5") -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT tick_at, first_collected_at, last_collected_at, data_json
            FROM quote_samples
            WHERE symbol = ?
            ORDER BY tick_time_msc
            """,
            (symbol,),
        ).fetchall()
        output: list[dict[str, Any]] = []
        for row in rows:
            item = _loads(row["data_json"])
            item["tick_at"] = row["tick_at"]
            item["collected_at"] = row["last_collected_at"]
            output.append(item)
        return output

    def quote_samples_for_public_window(
        self,
        *,
        public_cutoff: str,
        minute_cutoff: str,
        full_cutoff: str,
        symbol: str = "FixedStep0.5",
    ) -> list[dict[str, Any]]:
        """Return only public-resolution rows from the bounded 90-day window.

        SQL selects every tick from the newest 24 hours, the last observed tick
        in each UTC minute for days 2-7, and the last observed tick in each UTC
        hour for days 8-90.  Python therefore decodes only publishable rows,
        rather than hundreds of thousands of full-cadence local observations.
        """

        rows = self.connection.execute(
            """
            WITH selected_ticks(tick_time_msc) AS (
                SELECT tick_time_msc
                FROM quote_samples
                WHERE symbol = ? AND tick_at >= ?

                UNION

                SELECT MAX(tick_time_msc)
                FROM quote_samples
                WHERE symbol = ? AND tick_at >= ? AND tick_at < ?
                GROUP BY substr(tick_at, 1, 16)

                UNION

                SELECT MAX(tick_time_msc)
                FROM quote_samples
                WHERE symbol = ? AND tick_at >= ? AND tick_at < ?
                GROUP BY substr(tick_at, 1, 13)
            )
            SELECT q.tick_at, q.first_collected_at, q.last_collected_at, q.data_json
            FROM quote_samples AS q
            JOIN selected_ticks AS selected
              ON selected.tick_time_msc = q.tick_time_msc
            WHERE q.symbol = ?
            ORDER BY q.tick_time_msc
            """,
            (
                symbol,
                full_cutoff,
                symbol,
                minute_cutoff,
                full_cutoff,
                symbol,
                public_cutoff,
                minute_cutoff,
                symbol,
            ),
        ).fetchall()
        output: list[dict[str, Any]] = []
        for row in rows:
            item = _loads(row["data_json"])
            item["tick_at"] = row["tick_at"]
            item["collected_at"] = row["last_collected_at"]
            output.append(item)
        return output

    def quote_sample_count(self, symbol: str = "FixedStep0.5") -> int:
        """Count retained local quotes without materializing their JSON."""

        row = self.connection.execute(
            "SELECT COUNT(*) FROM quote_samples WHERE symbol = ?",
            (symbol,),
        ).fetchone()
        return int(row[0]) if row is not None else 0

    def latest_quote(self, symbol: str = "FixedStep0.5") -> dict[str, Any] | None:
        row = self.connection.execute(
            """
            SELECT tick_at, last_collected_at, data_json
            FROM quote_samples
            WHERE symbol = ?
            ORDER BY tick_time_msc DESC LIMIT 1
            """,
            (symbol,),
        ).fetchone()
        if row is None:
            return None
        item = _loads(row["data_json"])
        item["tick_at"] = row["tick_at"]
        item["collected_at"] = row["last_collected_at"]
        return item

    def counts(self) -> dict[str, int]:
        tables = {
            "collection_runs": "collection_runs",
            "account_samples": "account_samples",
            "closed_trades": "closed_trades",
            "strategy_samples": "strategy_samples",
            "events": "strategy_events",
            "quote_samples": "quote_samples",
        }
        output: dict[str, int] = {}
        for key, table in tables.items():
            row = self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            output[key] = int(row[0])
        output["open_positions"] = len(self.latest_positions())
        return output
