"""The HTTP server of the food service: a thin shell around `api.Api`.

Only the standard library is used. One thread serves each connection, kept
open between the requests of the same client (HTTP/1.1); the connections to
the databases are pooled, so the number of requests working at the same time
is bounded whatever the number of connections.

What a request may weigh: 64 KiB for its body (answered 413 beyond, without
reading it), 64 KiB for its first line and for each header, 100 headers. A
connection that sends nothing for 30 seconds is closed. The body must come
with a `Content-Length`; a chunked body is refused.

Stopping (SIGTERM or Ctrl-C): the server stops accepting connections, lets
the requests being answered finish, then closes the databases.
"""

from __future__ import annotations

import json
import signal
import sys
import threading
import time
import traceback
from collections.abc import Callable
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .. import __version__, kratheos
from .api import Api, Response, error_response
from .catalogue import Catalogue, DatabaseUnavailable
from .live import LiveStore
from .settings import Settings
from .validation import ApiError

IDLE_TIMEOUT_SECONDS = 30
DRAIN_SECONDS = 10
METHODS_WITH_BODY = ("POST", "PUT", "PATCH")
Logger = Callable[[str], None]


def log_line(message: str) -> None:
    """Print a line of the service log, with the time, at once.

    Args:
        message: The line.
    """
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"{stamp} {message}", flush=True)


class FoodServer(ThreadingHTTPServer):
    """The HTTP server, which knows the endpoints and counts the requests running.

    Attributes:
        api: The endpoints.
        log: Function receiving one line per request; None to stay silent.
    """

    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128

    def __init__(self, address: tuple[str, int], api: Api, log: Logger | None = log_line) -> None:
        """Bind the server to its address and its endpoints.

        Args:
            address: Host and port to listen on.
            api: The endpoints.
            log: Function receiving one line per request; None to stay silent.
        """
        self.api = api
        self.log = log
        self._running = 0
        self._changed = threading.Condition()
        super().__init__(address, Handler)

    def begin_request(self) -> None:
        """Count a request that starts being answered."""
        with self._changed:
            self._running += 1

    def end_request(self) -> None:
        """Count a request that was answered."""
        with self._changed:
            self._running -= 1
            self._changed.notify_all()

    def drain(self, seconds: float = DRAIN_SECONDS) -> bool:
        """Wait for the requests being answered to finish.

        Args:
            seconds: Longest time to wait.

        Returns:
            True when no request is running any more.
        """
        deadline = time.monotonic() + seconds
        with self._changed:
            while self._running and time.monotonic() < deadline:
                self._changed.wait(deadline - time.monotonic())
            return not self._running


class Handler(BaseHTTPRequestHandler):
    """Reads one HTTP request, hands it to the endpoints and writes the answer."""

    protocol_version = "HTTP/1.1"
    server_version = f"kratheos-food-data/{__version__}"
    sys_version = ""
    timeout = IDLE_TIMEOUT_SECONDS

    def log_message(self, *args: object) -> None:
        """Silence the default log of the standard library: `_answer` logs."""

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        """Answer an error found by the standard library as the API gives errors.

        Args:
            code: HTTP status.
            message: Short description.
            explain: Longer description, unused.
        """
        codes = {400: "invalid_request", 405: "method_not_allowed", 413: "payload_too_large"}
        self.close_connection = True
        self._write(
            Response(
                code,
                {
                    "code": codes.get(code, "invalid_request"),
                    "message": message or "the request cannot be read",
                },
            )
        )

    def _write(self, response: Response) -> None:
        """Write an answer on the connection.

        Args:
            response: The answer.
        """
        body = json.dumps(response.payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        self.send_response(response.status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for name, value in response.headers.items():
            self.send_header(name, value)
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _read_body(self) -> bytes:
        """Read the body of the request, within the size allowed.

        Returns:
            The body, empty for a request that has none.

        Raises:
            ApiError: When the body is too large, has no length or is chunked.
                The connection is then closed after the answer, since what
                the client still sends cannot be told from a new request.
        """
        api: Api = self.server.api
        length_header = self.headers.get("Content-Length")
        if self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            raise ApiError(400, "invalid_request", "a chunked body is not accepted")
        if length_header is None:
            if self.command in METHODS_WITH_BODY:
                self.close_connection = True
                raise ApiError(400, "invalid_request", "Content-Length is required")
            return b""
        if not length_header.isdigit():
            self.close_connection = True
            raise ApiError(400, "invalid_request", "Content-Length must be a number")
        length = int(length_header)
        if length > api.settings.max_body_bytes:
            self.close_connection = True
            raise ApiError(
                413,
                "payload_too_large",
                f"the body may not exceed {api.settings.max_body_bytes} bytes",
            )
        body = self.rfile.read(length)
        if len(body) != length:
            self.close_connection = True
            raise ApiError(400, "invalid_request", "the body is shorter than announced")
        return body

    def _answer(self) -> None:
        """Answer the request that was just read."""
        server: FoodServer = self.server
        started = time.perf_counter()
        server.begin_request()
        try:
            try:
                body = self._read_body()
                headers = {name.lower(): value for name, value in self.headers.items()}
                # HEAD is GET without the body, which `_write` leaves out.
                method = "GET" if self.command == "HEAD" else self.command
                response = server.api.handle(method, self.path, headers, body)
            except ApiError as error:
                response = error_response(error)
            except Exception:  # noqa: BLE001 - the last line of defence of a request
                traceback.print_exc(file=sys.stderr)
                response = Response(
                    500, {"code": "internal_error", "message": "the service failed on this request"}
                )
            self._write(response)
            if server.log is not None:
                elapsed = (time.perf_counter() - started) * 1000
                path = self.path.split("?", 1)[0]
                server.log(f"{self.command} {path} {response.status} {elapsed:.1f}ms")
        finally:
            server.end_request()

    do_GET = _answer  # noqa: N815 - names imposed by http.server
    do_HEAD = _answer  # noqa: N815
    do_POST = _answer  # noqa: N815
    do_PUT = _answer  # noqa: N815
    do_PATCH = _answer  # noqa: N815
    do_DELETE = _answer  # noqa: N815


def open_service(settings: Settings, log: Logger = log_line) -> tuple[Api, Catalogue, LiveStore]:
    """Open both databases and bind the endpoints to them.

    A live database that does not exist yet is created; when a
    `kratheos.sqlite` is at hand, what it holds is taken over, so that the
    next export still carries it.

    Args:
        settings: The settings of the service.
        log: Function receiving what happens.

    Returns:
        The endpoints, the main database and the live database.
    """
    store = LiveStore(settings.live, settings.key_secret)
    catalogue = Catalogue(settings.database, settings.readers, log)
    if store.created:
        log(f"created the live database {settings.live}")
        layer = kratheos.read_layer(settings.layer) if settings.layer else kratheos.Layer()
        if layer.rows:
            added = store.import_layer(layer)
            log(f"took over {settings.layer}: {added}")
    try:
        generation = catalogue.current()
    except DatabaseUnavailable:
        log(f"no main database at {settings.database} yet: reads answer 503 until one is there")
    else:
        folded = ((generation.manifest or {}).get("kratheos") or {}).get("statistics") or {}
        in_build = sum(folded.get(name, 0) for name in ("products", "versions", "translations"))
        if store.created and in_build and not (settings.layer and settings.layer.exists()):
            log(
                "warning: the main database was built with a Kratheos layer this new live"
                " database does not hold; import it (food-data import-layer) or the next"
                " export will not carry it"
            )
    if not settings.token:
        log("no FOOD_DATA_TOKEN: writes and moderation are disabled")
    return Api(settings, catalogue, store), catalogue, store


def serve(settings: Settings, log: Logger = log_line) -> None:
    """Run the service until it is told to stop.

    Args:
        settings: The settings of the service.
        log: Function receiving what happens.
    """
    api, catalogue, store = open_service(settings, log)
    server = FoodServer((settings.host, settings.port), api, log)
    host, port = server.server_address[:2]
    log(f"listening on http://{host}:{port}")

    def stop(*_: object) -> None:
        """Ask the server to stop, from a signal."""
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever()
    finally:
        if not server.drain():
            log("stopping although requests were still running")
        server.server_close()
        api.close()
        catalogue.close()
        store.close()
        log("stopped")
