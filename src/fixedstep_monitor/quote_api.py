"""Small, loopback-only HTTP API for sanitized FixedStep quote exports."""

from __future__ import annotations

import ipaddress
import socket
import threading
import time
from collections import deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_CONCURRENT_REQUESTS = 8
MAX_REQUESTS_PER_MINUTE = 240
REQUEST_TIMEOUT_SECONDS = 10.0

_ROUTES = {
    "/v1/quotes": "quotes.json",
    "/v1/latest": "latest.json",
}
_ALLOWED_PREFLIGHT_HEADERS = {"cache-control", "pragma"}
_HEALTH_PAYLOAD = b'{"status":"ok"}\n'


def _loopback_host(host: str) -> str:
    candidate = str(host).strip()
    if candidate.lower() == "localhost":
        return "127.0.0.1"
    if candidate.startswith("[") and candidate.endswith("]"):
        candidate = candidate[1:-1]
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError as error:
        raise ValueError("quote API host must be a loopback IP address") from error
    if not address.is_loopback:
        raise ValueError("quote API host must be a loopback IP address")
    return str(address)


def _allowed_origin(origin: str) -> str:
    candidate = str(origin).strip()
    parsed = urlsplit(candidate)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("allowed_origin must be an HTTP origin without a path")
    return f"{parsed.scheme}://{parsed.netloc}"


class _QuoteHttpServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        data_dir: Path,
        allowed_origin: str,
    ) -> None:
        self.data_dir = data_dir
        self.allowed_origin = allowed_origin
        self._request_slots = threading.BoundedSemaphore(MAX_CONCURRENT_REQUESTS)
        self._rate_lock = threading.Lock()
        self._request_times: deque[float] = deque()
        super().__init__(server_address, _QuoteRequestHandler)

    def _within_rate_limit(self) -> bool:
        now = time.monotonic()
        cutoff = now - 60.0
        with self._rate_lock:
            while self._request_times and self._request_times[0] <= cutoff:
                self._request_times.popleft()
            if len(self._request_times) >= MAX_REQUESTS_PER_MINUTE:
                return False
            self._request_times.append(now)
            return True

    @staticmethod
    def _reject_socket(request: socket.socket, status: HTTPStatus) -> None:
        reason = status.phrase.encode("ascii", errors="replace")
        response = (
            b"HTTP/1.1 "
            + str(status.value).encode("ascii")
            + b" "
            + reason
            + b"\r\nConnection: close\r\nContent-Length: 0\r\n"
            + b"Cache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\n\r\n"
        )
        try:
            request.sendall(response)
        except OSError:
            pass

    def process_request(self, request: socket.socket, client_address: object) -> None:
        request.settimeout(REQUEST_TIMEOUT_SECONDS)
        if not self._request_slots.acquire(blocking=False):
            self._reject_socket(request, HTTPStatus.SERVICE_UNAVAILABLE)
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._request_slots.release()
            raise

    def process_request_thread(self, request: socket.socket, client_address: object) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_slots.release()


class _QuoteHttpServerV6(_QuoteHttpServer):
    address_family = socket.AF_INET6


class _QuoteRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "FixedStepQuoteAPI"
    sys_version = ""

    @property
    def quote_server(self) -> _QuoteHttpServer:
        return self.server  # type: ignore[return-value]

    def log_message(self, format: str, *args: object) -> None:
        # Requests can contain untrusted values. The surrounding service owns logging.
        return

    def parse_request(self) -> bool:
        """Parse and rate-limit each HTTP request, including keep-alive requests."""

        if not super().parse_request():
            return False
        if self.quote_server._within_rate_limit():
            return True
        # Returning False prevents dispatch to a do_* handler. Close after the
        # response so a client cannot keep a rate-limited connection occupied.
        self.close_connection = True
        self._send_empty(HTTPStatus.TOO_MANY_REQUESTS, close=True)
        return False

    def _cors_headers(self) -> None:
        self.send_header("Vary", "Origin")
        if self.headers.get("Origin") == self.quote_server.allowed_origin:
            self.send_header("Access-Control-Allow-Origin", self.quote_server.allowed_origin)

    def _common_headers(self) -> None:
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
        self._cors_headers()

    def _send_payload(
        self,
        status: HTTPStatus,
        payload: bytes,
        *,
        include_body: bool,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self._common_headers()
        self.end_headers()
        if include_body and payload:
            self.wfile.write(payload)

    def _send_empty(self, status: HTTPStatus, *, close: bool = False) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        if close:
            self.send_header("Connection", "close")
        self._common_headers()
        self.end_headers()

    def _path(self) -> str | None:
        if len(self.path) > 2048:
            return None
        try:
            return urlsplit(self.path).path
        except ValueError:
            return None

    def _read_export(self, filename: str) -> bytes | None:
        candidate = self.quote_server.data_dir / filename
        try:
            if candidate.is_symlink():
                return None
            resolved = candidate.resolve(strict=True)
            if resolved.parent != self.quote_server.data_dir or not resolved.is_file():
                return None
            if resolved.stat().st_size > MAX_RESPONSE_BYTES:
                return None
            with resolved.open("rb") as exported:
                payload = exported.read(MAX_RESPONSE_BYTES + 1)
        except (OSError, RuntimeError):
            return None
        return payload if len(payload) <= MAX_RESPONSE_BYTES else None

    def _serve(self, *, include_body: bool) -> None:
        path = self._path()
        if path == "/healthz":
            self._send_payload(HTTPStatus.OK, _HEALTH_PAYLOAD, include_body=include_body)
            return
        filename = _ROUTES.get(path or "")
        if filename is None:
            self._send_empty(HTTPStatus.NOT_FOUND)
            return
        payload = self._read_export(filename)
        if payload is None:
            self._send_empty(HTTPStatus.SERVICE_UNAVAILABLE)
            return
        self._send_payload(HTTPStatus.OK, payload, include_body=include_body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler interface
        self._serve(include_body=True)

    def do_HEAD(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler interface
        self._serve(include_body=False)

    def do_OPTIONS(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler interface
        path = self._path()
        if path not in {*_ROUTES, "/healthz"}:
            self._send_empty(HTTPStatus.NOT_FOUND)
            return
        if self.headers.get("Origin") != self.quote_server.allowed_origin:
            self._send_empty(HTTPStatus.FORBIDDEN)
            return
        requested_header_value = self.headers.get("Access-Control-Request-Headers", "")
        requested_headers = {
            item.strip().lower() for item in requested_header_value.split(",") if item.strip()
        }
        if not requested_headers.issubset(_ALLOWED_PREFLIGHT_HEADERS):
            self._send_empty(HTTPStatus.FORBIDDEN)
            return
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Content-Length", "0")
        self.send_header("Allow", "GET, HEAD, OPTIONS")
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
        if requested_headers:
            self.send_header(
                "Access-Control-Allow-Headers",
                ", ".join(sorted(requested_headers)),
            )
        self._common_headers()
        self.end_headers()

    def _method_not_allowed(self) -> None:
        self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
        self.send_header("Content-Length", "0")
        self.send_header("Allow", "GET, HEAD, OPTIONS")
        self._common_headers()
        self.end_headers()

    do_POST = _method_not_allowed  # noqa: N815 - BaseHTTPRequestHandler interface
    do_PUT = _method_not_allowed  # noqa: N815 - BaseHTTPRequestHandler interface
    do_PATCH = _method_not_allowed  # noqa: N815 - BaseHTTPRequestHandler interface
    do_DELETE = _method_not_allowed  # noqa: N815 - BaseHTTPRequestHandler interface
    do_TRACE = _method_not_allowed  # noqa: N815 - BaseHTTPRequestHandler interface
    do_CONNECT = _method_not_allowed  # noqa: N815 - BaseHTTPRequestHandler interface


class QuoteApiServer:
    """Serve two known sanitized JSON exports from a loopback socket."""

    def __init__(
        self,
        data_dir: str | Path,
        host: str = "127.0.0.1",
        port: int = 8765,
        allowed_origin: str = "https://mahvro.github.io",
    ) -> None:
        self.data_dir = Path(data_dir).expanduser().resolve()
        if self.data_dir.exists() and not self.data_dir.is_dir():
            raise ValueError("data_dir must be a directory")
        self.host = _loopback_host(host)
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise ValueError("port must be an integer between 0 and 65535")
        self.port = port
        self.allowed_origin = _allowed_origin(allowed_origin)
        self._server: _QuoteHttpServer | None = None
        self._thread: threading.Thread | None = None
        self._lifecycle_lock = threading.Lock()

    def start(self) -> QuoteApiServer:
        """Start serving in a daemon thread and return immediately."""

        with self._lifecycle_lock:
            if self._server is not None:
                return self
            server_class = _QuoteHttpServerV6 if ":" in self.host else _QuoteHttpServer
            server = server_class(
                (self.host, self.port),
                data_dir=self.data_dir,
                allowed_origin=self.allowed_origin,
            )
            thread = threading.Thread(
                target=server.serve_forever,
                name="fixedstep-quote-api",
                daemon=True,
            )
            self._server = server
            self._thread = thread
            self.port = int(server.server_address[1])
            thread.start()
        return self

    def shutdown(self) -> None:
        """Stop the background server. Repeated calls are safe."""

        with self._lifecycle_lock:
            server = self._server
            thread = self._thread
            self._server = None
            self._thread = None
        if server is None:
            return
        server.shutdown()
        server.server_close()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5.0)
