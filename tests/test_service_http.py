"""Tests of the HTTP server: real sockets on the loopback interface.

What only shows over a real connection: the limits on what a request may
weigh, connections kept open between requests, errors of the transport given
as JSON like every other error, and a database replaced while requests are
being answered.
"""

from __future__ import annotations

import http.client
import json
import shutil
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from conftest import quiet
from service_tools import PRODUCT, TOKEN, Running, start_service

from kratheos_food_data.build import BuildOptions, Workspace
from kratheos_food_data.service import maintenance
from kratheos_food_data.service.httpd import FoodServer

NUTELLA = "gtin:3017620422003"


@pytest.fixture
def server(built_repository: Workspace, tmp_path: Path) -> Iterator[tuple[FoodServer, Running]]:
    """Run the HTTP server over a copy of the miniature repository."""
    running = start_service(built_repository, tmp_path)
    httpd = FoodServer(("127.0.0.1", 0), running.api, log=None)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd, running
    finally:
        httpd.shutdown()
        assert httpd.drain(5)
        httpd.server_close()
        thread.join(timeout=5)
        running.close()


def request(
    connection: http.client.HTTPConnection,
    method: str,
    target: str,
    body: bytes | None = None,
    headers: dict | None = None,
) -> tuple[int, dict, http.client.HTTPResponse]:
    """Send a request and read its JSON answer.

    Args:
        connection: An open connection to the server.
        method: HTTP method.
        target: Path and query.
        body: Body to send, if any.
        headers: Headers to send.

    Returns:
        The status, the JSON object and the response itself.
    """
    connection.request(method, target, body=body, headers=headers or {})
    response = connection.getresponse()
    raw = response.read()
    return response.status, json.loads(raw) if raw else {}, response


def test_requests_over_one_connection_and_json_everywhere(
    server: tuple[FoodServer, Running],
) -> None:
    """Several requests on one connection; every answer is JSON, errors included."""
    httpd, _ = server
    connection = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=10)
    status, food, response = request(connection, "GET", f"/v1/foods/{NUTELLA}?lang=fr")
    assert (status, food["name"]) == (200, "Nutella")
    assert response.getheader("Content-Type") == "application/json; charset=utf-8"
    assert response.version == 11 and response.getheader("Connection") is None
    socket_used = connection.sock
    status, found, _ = request(connection, "GET", "/v1/foods/search?q=m%C3%BCesli")
    assert status == 200 and found["items"][0]["name"] == "Bircher Müesli"
    assert connection.sock is socket_used  # the connection was kept
    # An identifier with a reserved character, written as the address requires.
    status, food, _ = request(connection, "GET", "/v1/foods/gtin%3A3017620422003")
    assert (status, food["id"]) == (200, NUTELLA)
    status, error, _ = request(connection, "GET", "/v1/foods/nothing:here")
    assert (status, error["code"]) == (404, "not_found")
    status, error, _ = request(connection, "DELETE", f"/v1/foods/{NUTELLA}")
    assert (status, error["code"]) == (405, "method_not_allowed")
    connection.request("HEAD", "/v1/health")
    response = connection.getresponse()
    assert response.status == 200 and response.read() == b""
    connection.close()


def test_writes_over_http_and_the_limits_on_a_request(server: tuple[FoodServer, Running]) -> None:
    """A body is bounded, must announce its length, and the token is a header."""
    httpd, running = server
    port = httpd.server_address[1]
    authorized = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    body = json.dumps(PRODUCT).encode("utf-8")
    status, created, _ = request(connection, "POST", "/v1/products", body, authorized)
    assert status == 201 and created["names"]["fr"] == "Burger maison du test"
    status, error, response = request(connection, "POST", "/v1/products", body)
    assert (status, error["code"]) == (401, "unauthorized")
    assert response.getheader("WWW-Authenticate") == "Bearer"
    status, error, _ = request(connection, "POST", "/v1/products", b"{", authorized)
    assert (status, error["code"]) == (400, "invalid_json")
    connection.close()

    # Too large: refused on its announced length, before a byte of it is read.
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    large = b'{"note": "' + b"x" * running.settings.max_body_bytes + b'"}'
    connection.putrequest("POST", "/v1/products")
    connection.putheader("Authorization", f"Bearer {TOKEN}")
    connection.putheader("Content-Length", str(len(large)))
    connection.endheaders()
    response = connection.getresponse()
    assert response.status == 413 and json.loads(response.read())["code"] == "payload_too_large"
    assert response.getheader("Connection") == "close"
    connection.close()

    for headers in ({"Transfer-Encoding": "chunked"}, {}):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        connection.putrequest("POST", "/v1/products")
        for name, value in headers.items():
            connection.putheader(name, value)
        connection.endheaders()
        response = connection.getresponse()
        assert response.status == 400 and json.loads(response.read())["code"] == "invalid_request"
        connection.close()

    # Something that is not HTTP is answered as an error of the API, not a page.
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    connection.connect()
    connection.sock.sendall(b"NOT-HTTP\r\n\r\n")
    answer = connection.sock.recv(4096).decode("utf-8")
    assert json.loads(answer.splitlines()[-1])["code"] == "invalid_request"
    connection.close()


def test_database_is_replaced_without_losing_a_request(server: tuple[FoodServer, Running]) -> None:
    """Requests keep being answered while a rebuild swaps the main database in."""
    httpd, running = server
    port = httpd.server_address[1]
    authorized = {"Authorization": f"Bearer {TOKEN}"}
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    body = json.dumps(PRODUCT).encode("utf-8")
    food_id = request(connection, "POST", "/v1/products", body, authorized)[1]["id"]
    connection.close()
    before = running.catalogue.current()
    failures: list[object] = []
    answered = [0]
    stop = threading.Event()

    def keep_asking() -> None:
        """Ask for foods, in a loop, on one connection, until told to stop."""
        client = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        targets = (f"/v1/foods/{NUTELLA}", f"/v1/foods/{food_id}", "/v1/foods/search?q=burger")
        while not stop.is_set():
            for target in targets:
                try:
                    status, payload, _ = request(client, "GET", target)
                except Exception as error:  # noqa: BLE001 - any failure is the point
                    failures.append(error)
                    return
                if status != 200:
                    failures.append((target, status, payload))
                answered[0] += 1
        client.close()

    threads = [threading.Thread(target=keep_asking) for _ in range(4)]
    for thread in threads:
        thread.start()
    try:
        for _ in range(2):
            maintenance.rebuild(
                running.workspace,
                BuildOptions(from_mirror=True),
                running.settings.database,
                running.settings.live,
                log=quiet,
            )
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=20)
    assert failures == [] and answered[0] > 50
    after = running.catalogue.current()
    assert after is not before and after.foods == before.foods + 1
    assert before._retired and not after._retired

    # A file that is not a database never takes the place of the one served.
    database = running.settings.database
    good = database.with_name("good.sqlite")
    shutil.copy(database, good)
    broken = database.with_name("broken.sqlite")
    broken.write_bytes(b"this is not a database")
    broken.replace(database)
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    status, food, _ = request(connection, "GET", f"/v1/foods/{NUTELLA}")
    assert (status, food["id"]) == (200, NUTELLA)
    assert running.catalogue.current() is after
    good.replace(database)
    status, food, _ = request(connection, "GET", f"/v1/foods/{NUTELLA}")
    assert status == 200 and running.catalogue.current() is not after
    connection.close()


def test_without_a_database_reads_say_so(tmp_path: Path) -> None:
    """Before a database is installed the service runs and answers 503."""
    from kratheos_food_data.service.httpd import open_service
    from kratheos_food_data.service.settings import load_settings

    settings = load_settings(tmp_path, environment={"FOOD_DATA_TOKEN": TOKEN})
    logged: list[str] = []
    api, catalogue, store = open_service(settings, log=logged.append)
    assert any("no main database" in line for line in logged)
    response = api.handle("GET", "/v1/health", {}, b"")
    assert (response.status, response.payload["code"]) == (503, "database_unavailable")
    assert api.handle("GET", f"/v1/foods/{NUTELLA}", {}, b"").status == 503
    api.close()
    catalogue.close()
    store.close()
