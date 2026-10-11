from __future__ import annotations

import json
import os
import threading
import unittest
from typing import Any
from unittest.mock import patch

from fixedstep_monitor.outbound_sink import (
    LatestWinsPublishWorker,
    OutboundPublishError,
    OutboundPublishResult,
    OutboundQuotePublisher,
    TOKEN_ENVIRONMENT_VARIABLE,
    build_outbound_payload,
)


TOKEN = "a" * 48
SNAPSHOT_ID = "247d7e6f-7bcb-497c-8c54-d4bc7c3b32d5"


def quote_payload(*, snapshot_id: str = SNAPSHOT_ID, bid: float = 5973.3) -> dict[str, Any]:
    return {
        "schema_version": "fixedstep-quotes-v1",
        "snapshot_id": snapshot_id,
        "generated_at": "2026-10-11T12:00:00.000Z",
        "timezone": "UTC",
        "collector_clock_basis": "utc_serialized_host_clock_synchronization_not_asserted",
        "tick_time_basis": "mt5_reported_unverified",
        "chart_time_basis": "collector_first_observation_host_clock",
        "symbol": "FixedStep0.5",
        "collection": {
            "status": "live",
            "connected": True,
            "expected_collection_interval_seconds": 10,
            "source_stale_after_seconds": 45,
            "collected_at": "2026-10-11T12:00:00.000Z",
            "collection_age_seconds": 0.25,
        },
        "quote": {
            "collected_at": "2026-10-11T12:00:00.000Z",
            "tick_at": "2026-10-11T11:59:59.900Z",
            "bid": bid,
            "ask": bid + 0.5,
            "last": None,
            "mid": bid + 0.25,
            "spread": 0.5,
            "spread_points": 5.0,
            "digits": 1,
            "point": 0.1,
            "trade_tick_size": 0.1,
            "tick_time_basis": "mt5_reported_unverified",
            "mt5_reported_offset_seconds": 0.1,
            "mt5_reported_offset_material": False,
        },
        "samples": [{"bid": 1, "ask": 2}],
    }


def all_keys(value: Any) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, nested in value.items():
            keys.add(str(key).lower())
            keys.update(all_keys(nested))
    elif isinstance(value, list):
        for nested in value:
            keys.update(all_keys(nested))
    return keys


class _Response:
    status = 201

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self, size: int) -> bytes:
        return json.dumps(
            {"status": "accepted", "snapshot_id": SNAPSHOT_ID}
        ).encode("utf-8")


class _RecordingOpener:
    def __init__(self, *, failure: Exception | None = None) -> None:
        self.failure = failure
        self.calls: list[tuple[Any, float]] = []

    def open(self, request, *, timeout: float):
        self.calls.append((request, timeout))
        if self.failure is not None:
            raise self.failure
        return _Response()


class OutboundPayloadTests(unittest.TestCase):
    def test_fixed_allowlist_drops_account_trade_and_unbounded_history_fields(self) -> None:
        payload = quote_payload()
        payload.update(
            {
                "account": {"balance": 10_000, "login": 123},
                "balance": 10_000,
                "equity": 9_900,
                "pnl": -100,
                "positions": [{"ticket": 1}],
                "orders": [{"ticket": 2}],
                "deals": [{"ticket": 3}],
                "server": "private-broker",
                "secret": TOKEN,
            }
        )
        payload["collection"].update({"profit": 12, "private_path": r"C:\\private"})
        payload["quote"].update({"order_id": 7, "account_id": 8})

        clean = build_outbound_payload(payload)

        self.assertEqual(clean["symbol"], "FixedStep0.5")
        self.assertEqual(clean["schema_version"], "fixedstep-quotes-v1")
        self.assertEqual(clean["collection"]["expected_collection_interval_seconds"], 10)
        self.assertNotIn("samples", clean)
        forbidden = {
            "account",
            "account_id",
            "balance",
            "equity",
            "pnl",
            "profit",
            "positions",
            "orders",
            "order_id",
            "deals",
            "login",
            "server",
            "secret",
            "private_path",
        }
        self.assertTrue(forbidden.isdisjoint(all_keys(clean)))
        self.assertNotIn(TOKEN, json.dumps(clean))

    def test_payload_rejects_wrong_contract_and_non_finite_or_crossed_quotes(self) -> None:
        cases = []

        wrong_symbol = quote_payload()
        wrong_symbol["symbol"] = "FixedStep0.5x"
        cases.append(wrong_symbol)

        wrong_schema = quote_payload()
        wrong_schema["schema_version"] = "fixedstep-quotes-v2"
        cases.append(wrong_schema)

        infinite = quote_payload()
        infinite["quote"]["bid"] = float("inf")
        cases.append(infinite)

        crossed = quote_payload()
        crossed["quote"]["ask"] = crossed["quote"]["bid"] - 0.1
        cases.append(crossed)

        for payload in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(OutboundPublishError):
                    build_outbound_payload(payload)


class OutboundPublisherTests(unittest.TestCase):
    def test_https_endpoint_is_exact_and_token_is_only_an_authorization_header(self) -> None:
        opener = _RecordingOpener()
        publisher = OutboundQuotePublisher(
            "https://fixedstep-quote-relay.fixedstep-quote-relay.workers.dev/v1/ingest",
            TOKEN,
            timeout_seconds=3.5,
            opener=opener,
        )

        result = publisher.publish(quote_payload())

        self.assertEqual(result, OutboundPublishResult(SNAPSHOT_ID, 201))
        self.assertEqual(len(opener.calls), 1)
        request, timeout = opener.calls[0]
        self.assertEqual(
            request.full_url,
            "https://fixedstep-quote-relay.fixedstep-quote-relay.workers.dev/v1/ingest",
        )
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(timeout, 3.5)
        self.assertEqual(request.get_header("Authorization"), f"Bearer {TOKEN}")
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertNotIn(TOKEN.encode(), request.data)
        self.assertNotIn(TOKEN, request.full_url)
        transmitted = json.loads(request.data)
        self.assertEqual(transmitted["symbol"], "FixedStep0.5")
        self.assertNotIn("account", all_keys(transmitted))

    def test_non_https_or_non_exact_ingest_urls_are_rejected(self) -> None:
        invalid_urls = (
            "http://example.workers.dev/v1/ingest",
            "https://user:pass@example.workers.dev/v1/ingest",
            "https://example.workers.dev/",
            "https://example.workers.dev/v1/ingest/",
            "https://example.workers.dev/v1/ingest?token=bad",
            "https://example.workers.dev/v1/ingest#fragment",
            "https://fixedstep-quote-relay.fixedstep-quote-relay.workers.dev:444/v1/ingest",
            "https://different-worker.example.workers.dev/v1/ingest",
        )
        for url in invalid_urls:
            with self.subTest(url=url):
                with self.assertRaisesRegex(
                    OutboundPublishError, "invalid_configuration:ingest_url"
                ):
                    OutboundQuotePublisher(url, TOKEN)

    def test_token_contract_matches_worker_and_environment_is_consumed(self) -> None:
        for invalid in ("a" * 31, "a" * 257, "a" * 31 + " ", "a" * 31 + "é"):
            with self.subTest(length=len(invalid), tail=invalid[-1]):
                with self.assertRaisesRegex(
                    OutboundPublishError, "invalid_configuration:token"
                ):
                    OutboundQuotePublisher(
                        "https://fixedstep-quote-relay.fixedstep-quote-relay.workers.dev/v1/ingest",
                        invalid,
                    )

        with patch.dict(
            os.environ, {TOKEN_ENVIRONMENT_VARIABLE: TOKEN}, clear=False
        ):
            OutboundQuotePublisher.from_environment(
                "https://fixedstep-quote-relay.fixedstep-quote-relay.workers.dev/v1/ingest"
            )
            self.assertNotIn(TOKEN_ENVIRONMENT_VARIABLE, os.environ)

    def test_transport_exception_does_not_leak_token_or_exception_message(self) -> None:
        opener = _RecordingOpener(
            failure=RuntimeError(f"socket failed while using {TOKEN}")
        )
        publisher = OutboundQuotePublisher(
            "https://fixedstep-quote-relay.fixedstep-quote-relay.workers.dev/v1/ingest",
            TOKEN,
            opener=opener,
        )

        with self.assertRaises(OutboundPublishError) as raised:
            publisher.publish(quote_payload())

        public_error = str(raised.exception)
        self.assertEqual(public_error, "RuntimeError: outbound request failed")
        self.assertNotIn(TOKEN, public_error)
        self.assertNotIn("socket failed", public_error)

    def test_acknowledgement_must_confirm_the_exact_snapshot(self) -> None:
        class WrongAcknowledgement(_Response):
            def read(self, size: int) -> bytes:
                return b'{"status":"accepted","snapshot_id":"wrong"}'

        class WrongOpener(_RecordingOpener):
            def open(self, request, *, timeout: float):
                self.calls.append((request, timeout))
                return WrongAcknowledgement()

        publisher = OutboundQuotePublisher(
            "https://fixedstep-quote-relay.fixedstep-quote-relay.workers.dev/v1/ingest",
            TOKEN,
            opener=WrongOpener(),
        )
        with self.assertRaisesRegex(
            OutboundPublishError, "remote_rejected:invalid_acknowledgement"
        ):
            publisher.publish(quote_payload())


class LatestWinsPublishWorkerTests(unittest.TestCase):
    def test_stale_payload_waiting_in_queue_is_not_published(self) -> None:
        started = threading.Event()
        release = threading.Event()
        published: list[str] = []
        now = [100.0]

        def monotonic() -> float:
            return now[0]

        def publish(payload: dict[str, Any]) -> OutboundPublishResult:
            published.append(payload["snapshot_id"])
            if len(published) == 1:
                started.set()
                self.assertTrue(release.wait(2), "test publication was not released")
            return OutboundPublishResult(payload["snapshot_id"], 202)

        worker = LatestWinsPublishWorker(publish, monotonic=monotonic)
        first = quote_payload(snapshot_id="46e234b4-ddae-4775-958a-d3bac5f8600e")
        delayed = quote_payload(snapshot_id="b94435f7-292f-45fb-9468-d5d745139a28")
        try:
            self.assertTrue(worker.request(first))
            self.assertTrue(started.wait(1), "worker did not start")
            self.assertTrue(worker.request(delayed))
            now[0] += 46.0
            release.set()
            self.assertTrue(worker.shutdown(drain=True, timeout=3))
        finally:
            release.set()
            worker.shutdown(drain=False, timeout=1)

        self.assertEqual(published, [first["snapshot_id"]])
        state = worker.snapshot
        self.assertEqual(state.completed, 2)
        self.assertEqual(state.failures, 1)
        self.assertEqual(
            state.last_error,
            "invalid_payload:collection.collection_age_seconds",
        )

    def test_slow_publication_keeps_only_the_latest_pending_snapshot(self) -> None:
        started = threading.Event()
        release = threading.Event()
        published: list[str] = []

        def publish(payload: dict[str, Any]) -> OutboundPublishResult:
            published.append(payload["snapshot_id"])
            if len(published) == 1:
                started.set()
                self.assertTrue(release.wait(2), "test publication was not released")
            return OutboundPublishResult(payload["snapshot_id"], 202)

        worker = LatestWinsPublishWorker(publish)
        first = quote_payload(snapshot_id="4b611384-68f8-4b67-bc28-cbfc10df9255")
        second = quote_payload(snapshot_id="85dc519e-202f-42d9-9eca-81537c689b45")
        latest = quote_payload(snapshot_id="ae2fa24f-c9f6-4777-81c5-6659ba194366")
        try:
            self.assertTrue(worker.request(first))
            self.assertTrue(started.wait(1), "worker did not start")
            self.assertTrue(worker.request(second))
            self.assertFalse(worker.request(latest))
            release.set()
            self.assertTrue(worker.shutdown(drain=True, timeout=3))
        finally:
            release.set()
            worker.shutdown(drain=False, timeout=1)

        self.assertEqual(
            published,
            [first["snapshot_id"], latest["snapshot_id"]],
        )
        state = worker.snapshot
        self.assertEqual(state.requested, 3)
        self.assertEqual(state.started, 2)
        self.assertEqual(state.completed, 2)
        self.assertEqual(state.coalesced, 1)
        self.assertEqual(state.failures, 0)

    def test_worker_recovers_after_one_publication_failure(self) -> None:
        completed = threading.Event()
        completions: list[tuple[OutboundPublishResult | None, str | None]] = []
        calls = 0

        def publish(payload: dict[str, Any]) -> OutboundPublishResult:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OutboundPublishError("remote_rejected:server_error")
            return OutboundPublishResult(payload["snapshot_id"], 202)

        def on_complete(
            result: OutboundPublishResult | None, error: str | None
        ) -> None:
            completions.append((result, error))
            completed.set()

        worker = LatestWinsPublishWorker(publish, on_complete=on_complete)
        try:
            self.assertTrue(worker.request(quote_payload()))
            self.assertTrue(completed.wait(1), "failure callback was not delivered")
            completed.clear()
            recovered_id = "dd9ce036-0d35-4ce3-883e-17f1e630b2c7"
            self.assertTrue(worker.request(quote_payload(snapshot_id=recovered_id)))
            self.assertTrue(completed.wait(1), "success callback was not delivered")
            self.assertTrue(worker.shutdown(drain=True, timeout=2))
        finally:
            worker.shutdown(drain=False, timeout=1)

        self.assertEqual(completions[0], (None, "remote_rejected:server_error"))
        self.assertEqual(completions[1][0], OutboundPublishResult(recovered_id, 202))
        self.assertIsNone(completions[1][1])
        state = worker.snapshot
        self.assertEqual(state.completed, 2)
        self.assertEqual(state.failures, 1)
        self.assertIsNone(state.last_error)


if __name__ == "__main__":
    unittest.main()
