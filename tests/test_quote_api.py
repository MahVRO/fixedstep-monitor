from __future__ import annotations

import http.client
import json
import tempfile
import unittest
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from fixedstep_monitor.quote_api import QuoteApiServer


ALLOWED_ORIGIN = "https://mahvro.github.io"
FIXED_NOW = datetime(2026, 10, 11, 12, 0, 0, tzinfo=UTC)


def live_payload(*, generated_at: datetime = FIXED_NOW, bid: float = 42.0) -> bytes:
    document = {
        "schema_version": "fixedstep-quotes-v1",
        "generated_at": generated_at.isoformat().replace("+00:00", "Z"),
        "symbol": "FixedStep0.5",
        "collection": {"source_stale_after_seconds": 45},
        "quote": {"bid": bid},
    }
    return (json.dumps(document, separators=(",", ":")) + "\n").encode("utf-8")


class QuoteApiServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temporary.name)
        self.quotes = b'{"schema_version":"fixedstep-quotes-v1","samples":[]}\n'
        self.latest = live_payload()
        (self.data_dir / "quotes.json").write_bytes(self.quotes)
        (self.data_dir / "latest.json").write_bytes(self.latest)
        self.server = QuoteApiServer(
            self.data_dir, port=0, clock=lambda: FIXED_NOW
        ).start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.shutdown()
        self.temporary.cleanup()

    def request(
        self,
        path: str,
        *,
        method: str = "GET",
        origin: str | None = ALLOWED_ORIGIN,
        headers: dict[str, str] | None = None,
    ) -> urllib.request.Request:
        request_headers = dict(headers or {})
        if origin is not None:
            request_headers["Origin"] = origin
        return urllib.request.Request(
            f"http://127.0.0.1:{self.server.port}{path}",
            method=method,
            headers=request_headers,
        )

    def test_serves_only_known_exports_with_strict_headers(self):
        with urllib.request.urlopen(self.request("/v1/quotes?v=123"), timeout=2) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), self.quotes)
            self.assertEqual(response.headers["Access-Control-Allow-Origin"], ALLOWED_ORIGIN)
            self.assertEqual(response.headers["Vary"], "Origin")
            self.assertEqual(response.headers["Cache-Control"], "no-store, max-age=0")
            self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
            self.assertIsNone(response.headers.get("Access-Control-Allow-Credentials"))

        with urllib.request.urlopen(self.request("/v1/latest"), timeout=2) as response:
            self.assertEqual(response.read(), self.latest)

        for forbidden_path in ("/", "/quotes.json", "/v1/../quotes", "/v1/quotes/"):
            with self.subTest(path=forbidden_path), self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(self.request(forbidden_path), timeout=2)
            self.assertEqual(raised.exception.code, 404)
            raised.exception.close()

    def test_explicit_routes_serve_separate_runtime_and_static_files(self):
        runtime_dir = self.data_dir / "runtime"
        static_dir = self.data_dir / "static"
        runtime_dir.mkdir()
        static_dir.mkdir()
        live_latest = live_payload(bid=200.0)
        static_quotes = b'{"source":"static-history","samples":[{"bid":100.0}]}\n'
        (runtime_dir / "latest.json").write_bytes(live_latest)
        (static_dir / "quotes.json").write_bytes(static_quotes)
        (static_dir / "latest.json").write_bytes(b'{"source":"stale-static"}\n')

        split = QuoteApiServer(
            port=0,
            latest_path=runtime_dir / "latest.json",
            quotes_path=static_dir / "quotes.json",
            clock=lambda: FIXED_NOW,
        ).start()
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{split.port}/v1/latest", timeout=2
            ) as response:
                self.assertEqual(response.read(), live_latest)
            with urllib.request.urlopen(
                f"http://127.0.0.1:{split.port}/v1/quotes", timeout=2
            ) as response:
                self.assertEqual(response.read(), static_quotes)

            replacement = runtime_dir / ".latest.replacement"
            updated_latest = live_payload(bid=300.0)
            replacement.write_bytes(updated_latest)
            replacement.replace(runtime_dir / "latest.json")
            with urllib.request.urlopen(
                f"http://127.0.0.1:{split.port}/v1/latest", timeout=2
            ) as response:
                self.assertEqual(response.read(), updated_latest)

            (runtime_dir / "latest.json").unlink()
            (runtime_dir / "latest.json").mkdir()
            with self.assertRaises(urllib.error.HTTPError) as unavailable:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{split.port}/v1/latest", timeout=2
                )
            self.assertEqual(unavailable.exception.code, 503)
            unavailable.exception.close()
        finally:
            split.shutdown()

    def test_explicit_routes_require_two_exact_existing_regular_files(self):
        with self.assertRaisesRegex(ValueError, "both required"):
            QuoteApiServer(latest_path=self.data_dir / "latest.json")
        with self.assertRaisesRegex(ValueError, "either data_dir"):
            QuoteApiServer(
                self.data_dir,
                latest_path=self.data_dir / "latest.json",
                quotes_path=self.data_dir / "quotes.json",
            )
        with self.assertRaisesRegex(ValueError, "existing regular file"):
            QuoteApiServer(
                latest_path=self.data_dir / "missing.json",
                quotes_path=self.data_dir / "quotes.json",
            )
        with self.assertRaisesRegex(ValueError, "existing regular file"):
            QuoteApiServer(
                latest_path=self.data_dir,
                quotes_path=self.data_dir / "quotes.json",
            )
        with self.assertRaisesRegex(ValueError, "distinct"):
            QuoteApiServer(
                latest_path=self.data_dir / "latest.json",
                quotes_path=self.data_dir / "latest.json",
            )

    def test_head_and_options_do_not_send_a_body(self):
        with urllib.request.urlopen(
            self.request("/v1/quotes", method="HEAD"), timeout=2
        ) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), b"")
            self.assertEqual(int(response.headers["Content-Length"]), len(self.quotes))

        with urllib.request.urlopen(
            self.request(
                "/v1/quotes",
                method="OPTIONS",
                headers={"Access-Control-Request-Headers": "Cache-Control, Pragma"},
            ),
            timeout=2,
        ) as response:
            self.assertEqual(response.status, 204)
            self.assertEqual(response.read(), b"")
            self.assertEqual(
                response.headers["Access-Control-Allow-Methods"], "GET, HEAD, OPTIONS"
            )
            self.assertEqual(
                response.headers["Access-Control-Allow-Headers"], "cache-control, pragma"
            )

        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(
                self.request("/v1/quotes", method="POST"), timeout=2
            )
        self.assertEqual(raised.exception.code, 405)
        self.assertEqual(raised.exception.headers["Allow"], "GET, HEAD, OPTIONS")
        raised.exception.close()

    def test_unapproved_origin_never_receives_cors_permission(self):
        with urllib.request.urlopen(
            self.request("/v1/quotes", origin="https://example.invalid"), timeout=2
        ) as response:
            self.assertIsNone(response.headers.get("Access-Control-Allow-Origin"))

        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(
                self.request(
                    "/v1/quotes", method="OPTIONS", origin="https://example.invalid"
                ),
                timeout=2,
            )
        self.assertEqual(raised.exception.code, 403)
        self.assertIsNone(raised.exception.headers.get("Access-Control-Allow-Origin"))
        raised.exception.close()

    def test_missing_or_oversized_export_is_not_exposed(self):
        (self.data_dir / "latest.json").unlink()
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(self.request("/v1/latest"), timeout=2)
        self.assertEqual(raised.exception.code, 503)
        raised.exception.close()

        with patch("fixedstep_monitor.quote_api.MAX_RESPONSE_BYTES", 4):
            with self.assertRaises(urllib.error.HTTPError) as oversized:
                urllib.request.urlopen(self.request("/v1/quotes"), timeout=2)
        self.assertEqual(oversized.exception.code, 503)
        oversized.exception.close()

    def test_frozen_future_or_malformed_live_snapshot_is_unavailable(self):
        cases = {
            "frozen": live_payload(generated_at=FIXED_NOW - timedelta(seconds=46)),
            "future": live_payload(generated_at=FIXED_NOW + timedelta(seconds=1)),
            "malformed": b'{"generated_at":"not-a-time","collection":{}}\n',
        }
        for name, payload in cases.items():
            with self.subTest(name=name):
                (self.data_dir / "latest.json").write_bytes(payload)
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    urllib.request.urlopen(self.request("/v1/latest"), timeout=2)
                self.assertEqual(raised.exception.code, 503)
                raised.exception.close()

        # The historical endpoint remains readable when its artifact is old;
        # only the live endpoint makes a freshness promise.
        with urllib.request.urlopen(self.request("/v1/quotes"), timeout=2) as response:
            self.assertEqual(response.status, 200)

    def test_health_is_minimal_and_non_loopback_bind_is_rejected(self):
        with urllib.request.urlopen(self.request("/healthz"), timeout=2) as response:
            self.assertEqual(response.read(), b'{"status":"ok"}\n')

        with self.assertRaisesRegex(ValueError, "loopback"):
            QuoteApiServer(self.data_dir, host="0.0.0.0")
        with self.assertRaisesRegex(ValueError, "loopback"):
            QuoteApiServer(self.data_dir, host="192.168.1.10")

    def test_rate_limit_counts_each_keep_alive_request(self):
        with patch("fixedstep_monitor.quote_api.MAX_REQUESTS_PER_MINUTE", 1):
            limited = QuoteApiServer(
                self.data_dir, port=0, clock=lambda: FIXED_NOW
            ).start()
            connection = http.client.HTTPConnection(
                "127.0.0.1", limited.port, timeout=2
            )
            try:
                connection.request("GET", "/healthz")
                first = connection.getresponse()
                self.assertEqual(first.status, 200)
                self.assertEqual(first.read(), b'{"status":"ok"}\n')

                # HTTPConnection reuses the same HTTP/1.1 socket here. The
                # limiter must count this request, not just the TCP connection.
                connection.request("GET", "/healthz")
                second = connection.getresponse()
                self.assertEqual(second.status, 429)
                self.assertEqual(second.getheader("Connection"), "close")
                self.assertEqual(second.read(), b"")
            finally:
                connection.close()
                limited.shutdown()


if __name__ == "__main__":
    unittest.main()
