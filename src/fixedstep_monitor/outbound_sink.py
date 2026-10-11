from __future__ import annotations

import json
import math
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from .timeutil import parse_utc


TOKEN_ENVIRONMENT_VARIABLE = "FIXEDSTEP_REALTIME_TOKEN"
INGEST_HOSTNAME = "fixedstep-quote-relay.fixedstep-quote-relay.workers.dev"
MAX_REQUEST_BYTES = 16_384
MAX_RESPONSE_BYTES = 4_096


class OutboundPublishError(RuntimeError):
    """A deliberately sanitized outbound-publication failure."""


@dataclass(frozen=True)
class OutboundPublishResult:
    snapshot_id: str
    status_code: int


@dataclass(frozen=True)
class LatestWinsWorkerSnapshot:
    requested: int
    started: int
    completed: int
    coalesced: int
    failures: int
    running: bool
    pending: bool
    stopping: bool
    last_error: str | None


def _finite_number(value: Any, field: str, *, positive: bool = False) -> int | float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise OutboundPublishError(f"invalid_payload:{field}")
    numeric = float(value)
    if not math.isfinite(numeric) or (positive and numeric <= 0):
        raise OutboundPublishError(f"invalid_payload:{field}")
    return value


def _timestamp(value: Any, field: str, *, required: bool = True) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value:
        raise OutboundPublishError(f"invalid_payload:{field}")
    try:
        parse_utc(value)
    except (TypeError, ValueError, OverflowError):
        raise OutboundPublishError(f"invalid_payload:{field}") from None
    return value


def build_outbound_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Rebuild the outbound body from a fixed quote-only allowlist."""

    if not isinstance(payload, dict):
        raise OutboundPublishError("invalid_payload:root")
    if payload.get("schema_version") != "fixedstep-quotes-v1":
        raise OutboundPublishError("invalid_payload:schema_version")
    if payload.get("symbol") != "FixedStep0.5":
        raise OutboundPublishError("invalid_payload:symbol")

    raw_snapshot_id = payload.get("snapshot_id")
    try:
        snapshot_id = str(uuid.UUID(str(raw_snapshot_id)))
    except (ValueError, TypeError, AttributeError):
        raise OutboundPublishError("invalid_payload:snapshot_id") from None
    if snapshot_id != raw_snapshot_id:
        raise OutboundPublishError("invalid_payload:snapshot_id")

    collection = payload.get("collection")
    if not isinstance(collection, dict):
        raise OutboundPublishError("invalid_payload:collection")
    status = collection.get("status")
    if status != "live":
        raise OutboundPublishError("invalid_payload:collection.status")
    connected = collection.get("connected")
    if connected is not True:
        raise OutboundPublishError("invalid_payload:collection.connected")
    interval = _finite_number(
        collection.get("expected_collection_interval_seconds"),
        "collection.expected_collection_interval_seconds",
        positive=True,
    )
    stale_after = _finite_number(
        collection.get("source_stale_after_seconds"),
        "collection.source_stale_after_seconds",
        positive=True,
    )
    age = _finite_number(
        collection.get("collection_age_seconds"),
        "collection.collection_age_seconds",
    )
    if age is not None and age < 0:
        raise OutboundPublishError("invalid_payload:collection.collection_age_seconds")
    if interval != 10:
        raise OutboundPublishError(
            "invalid_payload:collection.expected_collection_interval_seconds"
        )
    if (
        not isinstance(stale_after, int)
        or isinstance(stale_after, bool)
        or not 20 <= stale_after <= 300
    ):
        raise OutboundPublishError("invalid_payload:collection.source_stale_after_seconds")
    if age is None or age > stale_after:
        raise OutboundPublishError("invalid_payload:collection.collection_age_seconds")

    quote = payload.get("quote")
    if not isinstance(quote, dict):
        raise OutboundPublishError("invalid_payload:quote")
    clean_quote: dict[str, Any]
    if quote is not None:
        bid = _finite_number(quote.get("bid"), "quote.bid")
        ask = _finite_number(quote.get("ask"), "quote.ask")
        last = _finite_number(quote.get("last"), "quote.last")
        mid = _finite_number(quote.get("mid"), "quote.mid")
        spread = _finite_number(quote.get("spread"), "quote.spread")
        spread_points = _finite_number(quote.get("spread_points"), "quote.spread_points")
        digits = _finite_number(quote.get("digits"), "quote.digits")
        point = _finite_number(quote.get("point"), "quote.point", positive=True)
        trade_tick_size = _finite_number(
            quote.get("trade_tick_size"), "quote.trade_tick_size", positive=True
        )
        offset = _finite_number(
            quote.get("mt5_reported_offset_seconds"),
            "quote.mt5_reported_offset_seconds",
        )
        offset_material = quote.get("mt5_reported_offset_material")
        if offset_material is not None and not isinstance(offset_material, bool):
            raise OutboundPublishError("invalid_payload:quote.mt5_reported_offset_material")
        if bid is not None and ask is not None and ask < bid:
            raise OutboundPublishError("invalid_payload:quote.ask")
        if bid is None or bid <= 0 or ask is None:
            raise OutboundPublishError("invalid_payload:quote.bid")
        if last is not None and last <= 0:
            raise OutboundPublishError("invalid_payload:quote.last")
        if mid is None or not math.isclose(mid, (bid + ask) / 2, rel_tol=1e-10, abs_tol=1e-9):
            raise OutboundPublishError("invalid_payload:quote.mid")
        if spread is not None and spread < 0:
            raise OutboundPublishError("invalid_payload:quote.spread")
        if spread is None or not math.isclose(spread, ask - bid, rel_tol=1e-10, abs_tol=1e-9):
            raise OutboundPublishError("invalid_payload:quote.spread")
        if spread_points is not None and spread_points < 0:
            raise OutboundPublishError("invalid_payload:quote.spread_points")
        if digits is not None and (not isinstance(digits, int) or not 0 <= digits <= 10):
            raise OutboundPublishError("invalid_payload:quote.digits")
        if digits is None or point is None or trade_tick_size is None:
            raise OutboundPublishError("invalid_payload:quote.metadata")
        if spread_points is not None and not math.isclose(
            spread_points, spread / point, rel_tol=1e-10, abs_tol=1e-9
        ):
            raise OutboundPublishError("invalid_payload:quote.spread_points")
        if offset_material is None:
            raise OutboundPublishError("invalid_payload:quote.mt5_reported_offset_material")
        clean_quote = {
            "collected_at": _timestamp(quote.get("collected_at"), "quote.collected_at"),
            "tick_at": _timestamp(quote.get("tick_at"), "quote.tick_at"),
            "bid": bid,
            "ask": ask,
            "last": last,
            "mid": mid,
            "spread": spread,
            "spread_points": spread_points,
            "digits": digits,
            "point": point,
            "trade_tick_size": trade_tick_size,
            "tick_time_basis": "mt5_reported_unverified",
            "mt5_reported_offset_seconds": offset,
            "mt5_reported_offset_material": offset_material,
        }

    clean = {
        "schema_version": "fixedstep-quotes-v1",
        "snapshot_id": snapshot_id,
        "generated_at": _timestamp(payload.get("generated_at"), "generated_at"),
        "symbol": "FixedStep0.5",
        "collection": {
            "status": status,
            "connected": connected,
            "expected_collection_interval_seconds": interval,
            "source_stale_after_seconds": stale_after,
            "collected_at": _timestamp(
                collection.get("collected_at"), "collection.collected_at", required=False
            ),
            "collection_age_seconds": age,
        },
        "quote": clean_quote,
    }
    encoded = json.dumps(clean, allow_nan=False, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )
    if len(encoded) > MAX_REQUEST_BYTES:
        raise OutboundPublishError("invalid_payload:oversized")
    return clean


def _with_elapsed_queue_age(
    payload: dict[str, Any], elapsed_seconds: float
) -> dict[str, Any]:
    """Copy a snapshot and account for monotonic time spent awaiting publication."""

    adjusted = dict(payload)
    collection = payload.get("collection")
    if not isinstance(collection, dict):
        return adjusted
    age = collection.get("collection_age_seconds")
    if (
        isinstance(age, (int, float))
        and not isinstance(age, bool)
        and math.isfinite(float(age))
    ):
        adjusted_collection = dict(collection)
        adjusted_collection["collection_age_seconds"] = float(age) + max(
            0.0, elapsed_seconds
        )
        adjusted["collection"] = adjusted_collection
    return adjusted


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


class OutboundQuotePublisher:
    """Send one allowlisted quote snapshot to an exact HTTPS ingest endpoint."""

    def __init__(
        self,
        ingest_url: str,
        token: str,
        *,
        timeout_seconds: float = 5.0,
        opener: Any | None = None,
    ) -> None:
        parsed = urllib.parse.urlsplit(ingest_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path != "/v1/ingest"
            or parsed.query
            or parsed.fragment
            or parsed.port is not None
            or parsed.hostname != INGEST_HOSTNAME
        ):
            raise OutboundPublishError("invalid_configuration:ingest_url")
        if (
            not isinstance(token, str)
            or not 32 <= len(token) <= 256
            or any(not 0x21 <= ord(character) <= 0x7E for character in token)
        ):
            raise OutboundPublishError("invalid_configuration:token")
        if not 0.5 <= timeout_seconds <= 10.0:
            raise OutboundPublishError("invalid_configuration:timeout")
        self.ingest_url = urllib.parse.urlunsplit(parsed)
        self._token = token
        self.timeout_seconds = float(timeout_seconds)
        self._opener = opener or urllib.request.build_opener(_NoRedirect())

    @classmethod
    def from_environment(
        cls, ingest_url: str, *, timeout_seconds: float = 5.0
    ) -> "OutboundQuotePublisher":
        # Consume the one-shot inherited value before later Git/SSH
        # subprocesses can inherit it from this long-running process.
        token = os.environ.pop(TOKEN_ENVIRONMENT_VARIABLE, "")
        return cls(ingest_url, token, timeout_seconds=timeout_seconds)

    def publish(self, payload: dict[str, Any]) -> OutboundPublishResult:
        clean = build_outbound_payload(payload)
        body = json.dumps(
            clean, allow_nan=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        request = urllib.request.Request(
            self.ingest_url,
            data=body,
            method="POST",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "User-Agent": "fixedstep-quote-monitor/1",
            },
        )
        try:
            with self._opener.open(request, timeout=self.timeout_seconds) as response:
                status = int(response.status)
                if status not in {200, 201}:
                    raise OutboundPublishError("remote_rejected:unexpected_status")
                response_body = response.read(MAX_RESPONSE_BYTES + 1)
                if len(response_body) > MAX_RESPONSE_BYTES:
                    raise OutboundPublishError("remote_rejected:oversized_response")
                try:
                    acknowledgement = json.loads(response_body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    raise OutboundPublishError(
                        "remote_rejected:invalid_acknowledgement"
                    ) from None
                if (
                    not isinstance(acknowledgement, dict)
                    or acknowledgement.get("status") not in {"accepted", "duplicate"}
                    or acknowledgement.get("snapshot_id") != clean["snapshot_id"]
                ):
                    raise OutboundPublishError(
                        "remote_rejected:invalid_acknowledgement"
                    )
        except urllib.error.HTTPError as exc:
            if exc.code in {401, 403}:
                category = "remote_rejected:authentication"
            elif exc.code == 429:
                category = "remote_rejected:rate_limited"
            elif 500 <= exc.code <= 599:
                category = "remote_rejected:server_error"
            else:
                category = "remote_rejected:http_error"
            raise OutboundPublishError(category) from None
        except OutboundPublishError:
            raise
        except Exception as exc:
            raise OutboundPublishError(f"{type(exc).__name__}: outbound request failed") from None
        return OutboundPublishResult(
            snapshot_id=str(clean["snapshot_id"]), status_code=status
        )


class LatestWinsPublishWorker:
    """Publish off-thread with one active request and one replaceable pending payload."""

    def __init__(
        self,
        publish: Callable[[dict[str, Any]], OutboundPublishResult],
        *,
        on_complete: Callable[
            [OutboundPublishResult | None, str | None], None
        ]
        | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._publish = publish
        self._on_complete = on_complete
        self._monotonic = monotonic
        self._condition = threading.Condition()
        self._pending: tuple[dict[str, Any], float] | None = None
        self._running = False
        self._stopping = False
        self._drain = True
        self._requested = 0
        self._started = 0
        self._completed = 0
        self._coalesced = 0
        self._failures = 0
        self._last_error: str | None = None
        self._thread = threading.Thread(
            target=self._run,
            name="fixedstep-realtime-publisher",
            daemon=True,
        )
        self._thread.start()

    def request(self, payload: dict[str, Any]) -> bool:
        with self._condition:
            if self._stopping:
                return False
            self._requested += 1
            queued = self._pending is None
            if not queued:
                self._coalesced += 1
            self._pending = (payload, self._monotonic())
            self._condition.notify()
            return queued

    @property
    def snapshot(self) -> LatestWinsWorkerSnapshot:
        with self._condition:
            return LatestWinsWorkerSnapshot(
                requested=self._requested,
                started=self._started,
                completed=self._completed,
                coalesced=self._coalesced,
                failures=self._failures,
                running=self._running,
                pending=self._pending is not None,
                stopping=self._stopping,
                last_error=self._last_error,
            )

    def shutdown(self, *, drain: bool = True, timeout: float | None = None) -> bool:
        with self._condition:
            self._stopping = True
            self._drain = bool(drain)
            if not self._drain:
                self._pending = None
            self._condition.notify_all()
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def _run(self) -> None:
        while True:
            with self._condition:
                self._condition.wait_for(
                    lambda: self._pending is not None or self._stopping
                )
                if self._stopping and (not self._drain or self._pending is None):
                    return
                pending = self._pending
                self._pending = None
                self._running = True
                self._started += 1

            result: OutboundPublishResult | None = None
            error: str | None = None
            try:
                if pending is not None:
                    payload, enqueued_at = pending
                    elapsed_seconds = self._monotonic() - enqueued_at
                    if not math.isfinite(elapsed_seconds) or elapsed_seconds < 0:
                        elapsed_seconds = 0.0
                    prepared = build_outbound_payload(
                        _with_elapsed_queue_age(payload, elapsed_seconds)
                    )
                    result = self._publish(prepared)
            except OutboundPublishError as exc:
                error = str(exc)
            except Exception as exc:
                error = f"{type(exc).__name__}: outbound publication failed"

            with self._condition:
                self._running = False
                self._completed += 1
                if error is not None:
                    self._failures += 1
                self._last_error = error
                self._condition.notify_all()

            if self._on_complete is not None:
                try:
                    self._on_complete(result, error)
                except Exception:
                    pass
