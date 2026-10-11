"""Shared pytest fixtures: miniature sources, a DuckDB connection, a web server.

The web server serves a directory over HTTP on the loopback interface, with
the features the downloader relies on (HEAD, ETag, Range and If-Range), and
can cut a response short to simulate an interrupted download. No test touches
the real network.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import threading
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

import duckdb
import pytest
from builders import build_all

from kratheos_food_data import barcode, download, kratheos
from kratheos_food_data.adapters import AdapterContext
from kratheos_food_data.build import Workspace
from kratheos_food_data.config import Source, load_sources

REPOSITORY = Path(__file__).resolve().parent.parent


@dataclass
class FileServer:
    """A running test web server.

    Attributes:
        root: Directory being served.
        url: Base address, without a trailing slash.
        requests: Every request received, as (method, path, Range header).
        cut_next_get_after: When set, the next GET sends only that many bytes
            of the body and then drops the connection.
    """

    root: Path
    url: str
    requests: list[tuple[str, str, str | None]] = field(default_factory=list)
    cut_next_get_after: int | None = None


def _handler(server_state: FileServer) -> type[BaseHTTPRequestHandler]:
    """Build the request handler class bound to a server state.

    Args:
        server_state: The state shared with the test.

    Returns:
        A handler class serving files with ETag and Range support.
    """

    class Handler(BaseHTTPRequestHandler):
        """Serves the files of the test directory."""

        def log_message(self, *args: object) -> None:
            """Keep the test output quiet."""

        def do_HEAD(self) -> None:  # noqa: N802 - name imposed by http.server
            """Answer a HEAD request."""
            self._serve(with_body=False)

        def do_GET(self) -> None:  # noqa: N802 - name imposed by http.server
            """Answer a GET request, honouring Range and If-Range."""
            self._serve(with_body=True)

        def _serve(self, with_body: bool) -> None:
            """Send a file, whole or from an offset.

            Args:
                with_body: False for HEAD.
            """
            relative = unquote(self.path.lstrip("/"))
            requested_range = self.headers.get("Range")
            server_state.requests.append((self.command, relative, requested_range))
            path = server_state.root / relative
            if not path.is_file():
                self.send_error(404)
                return
            data = path.read_bytes()
            etag = '"' + hashlib.sha1(data, usedforsecurity=False).hexdigest()[:16] + '"'
            start = 0
            status = 200
            if_range = self.headers.get("If-Range")
            if requested_range and if_range in (None, etag):
                start = int(requested_range.removeprefix("bytes=").split("-")[0])
                status = 206
            body = data[start:]
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("ETag", etag)
            self.send_header("Accept-Ranges", "bytes")
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
            self.end_headers()
            if not with_body:
                return
            cut = server_state.cut_next_get_after
            if cut is not None:
                server_state.cut_next_get_after = None
                self.wfile.write(body[:cut])
                self.wfile.flush()
                self.connection.close()
                return
            self.wfile.write(body)

    return Handler


@pytest.fixture
def file_server(tmp_path: Path) -> Iterator[FileServer]:
    """Serve `tmp_path / "remote"` over HTTP for the duration of a test.

    Args:
        tmp_path: Temporary directory of the test.

    Yields:
        The running server.
    """
    root = tmp_path / "remote"
    root.mkdir()
    state = FileServer(root=root, url="")
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(state))
    state.url = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture(autouse=True)
def no_retry_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the downloader retry at once instead of waiting.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
    """
    monkeypatch.setattr(download.time, "sleep", lambda seconds: None)


@pytest.fixture
def con() -> Iterator[duckdb.DuckDBPyConnection]:
    """Open a DuckDB connection with the barcode macros registered.

    Yields:
        The connection.
    """
    connection = duckdb.connect()
    barcode.register_macros(connection)
    yield connection
    connection.close()


@pytest.fixture(scope="session")
def miniature_sources(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build the miniature source files once for the whole test session.

    Args:
        tmp_path_factory: The pytest temporary directory factory.

    Returns:
        A directory with one sub-directory of files per source id.
    """
    root = tmp_path_factory.mktemp("sources")
    build_all(root)
    return root


@pytest.fixture(scope="session")
def declared_sources() -> dict[str, Source]:
    """Load the real source list of the repository.

    Returns:
        The declared sources, by id.
    """
    return {source.id: source for source in load_sources(REPOSITORY / "sources.toml")}


@pytest.fixture
def adapter_context(
    con: duckdb.DuckDBPyConnection,
    miniature_sources: Path,
    declared_sources: dict[str, Source],
    tmp_path: Path,
) -> callable:
    """Give a factory of adapter contexts over the miniature sources.

    Args:
        con: DuckDB connection.
        miniature_sources: Directory of the miniature source files.
        declared_sources: The real source list, which names the files.
        tmp_path: Temporary directory of the test.

    Returns:
        A function taking a source id and returning its adapter context.
    """

    def make(source_id: str) -> AdapterContext:
        """Build the context of one source."""
        return AdapterContext(
            con=con,
            files={
                entry.name: miniature_sources / source_id / entry.name
                for entry in declared_sources[source_id].files
            },
            work_dir=tmp_path / "work" / source_id,
            staging_path=tmp_path / "staging" / f"{source_id}.parquet",
            log=lambda message: None,
        )

    return make


def staged_rows(con: duckdb.DuckDBPyConnection, staging_path: Path) -> dict[str, dict]:
    """Read a staging file as dictionaries keyed by source record id.

    Args:
        con: DuckDB connection.
        staging_path: Staging Parquet file written by an adapter.

    Returns:
        The staged rows, by `source_record_id`.
    """
    cursor = con.execute(f"SELECT * FROM read_parquet('{staging_path}')")
    names = [description[0] for description in cursor.description]
    rows = [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]
    return {row["source_record_id"]: row for row in rows}


def make_repository(root: Path, miniature_sources: Path, server: FileServer) -> Workspace:
    """Lay out a miniature repository whose publishers are the test server.

    The miniature source files are copied to the directory the server serves,
    a source list pointing at them is written, and an empty Kratheos layer is
    created, as in a fresh checkout before the first build.

    Args:
        root: Directory of the miniature repository.
        miniature_sources: Directory of the miniature source files.
        server: The test web server.

    Returns:
        The workspace of the repository.
    """
    shutil.copytree(miniature_sources, server.root, dirs_exist_ok=True)
    root.mkdir(parents=True, exist_ok=True)
    write_test_source_list(root / "sources.toml", server.url)
    workspace = Workspace.at(root)
    kratheos.create_database(workspace.kratheos)
    return workspace


def quiet(message: str) -> None:
    """Discard a progress line.

    Args:
        message: The line.
    """


def write_test_source_list(path: Path, base_url: str) -> None:
    """Write a source list equal to the real one but pointing at a test server.

    Every enabled source keeps its declaration; file addresses are replaced by
    `<base_url>/<source id>/<file name>` and pinned checksums are removed,
    since the miniature files are not the published ones.

    Args:
        path: Where to write the test `sources.toml`.
        base_url: Address of the test web server.
    """
    with (REPOSITORY / "sources.toml").open("rb") as handle:
        document = tomllib.load(handle)
    lines = []
    for source in document["source"]:
        lines.append("[[source]]")
        for key, value in source.items():
            if key != "files":
                lines.append(f"{key} = {json.dumps(value, ensure_ascii=False)}")
        for entry in source.get("files", []):
            lines.append("[[source.files]]")
            lines.append(f"name = {json.dumps(entry['name'])}")
            lines.append(f"url = {json.dumps(f'{base_url}/{source["id"]}/{entry["name"]}')}")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
