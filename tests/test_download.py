"""Tests of the downloader against a local web server, and of the DNS fallback."""

from __future__ import annotations

import hashlib
import socket
import struct
import threading
from pathlib import Path

import pytest
from conftest import FileServer

from kratheos_food_data import dns_fallback, download
from kratheos_food_data.download import (
    ChecksumMismatchError,
    DownloadError,
    fetch,
    meta_path,
    probe,
    read_record,
    user_agent,
)

CONTENT = bytes(range(256)) * 4096  # 1 MiB of non-trivial bytes


def _quiet(message: str) -> None:
    """Discard a progress line."""


def _serve(server: FileServer, name: str = "data.bin", content: bytes = CONTENT) -> str:
    """Put a file on the test server.

    Args:
        server: The test server.
        name: File name.
        content: File content.

    Returns:
        The address of the file.
    """
    (server.root / name).write_bytes(content)
    return f"{server.url}/{name}"


def test_download_writes_file_and_sidecar(file_server: FileServer, tmp_path: Path) -> None:
    """A first download stores the file, its checksums and its validators."""
    url = _serve(file_server)
    dest = tmp_path / "downloads" / "data.bin"
    record = fetch(url, dest, log=_quiet)
    assert record.status == "downloaded"
    assert dest.read_bytes() == CONTENT
    assert record.sha256 == hashlib.sha256(CONTENT).hexdigest()
    assert record.size == len(CONTENT)
    assert meta_path(dest).exists()
    assert read_record(dest) == record
    assert not dest.with_name("data.bin.part").exists()


def test_unchanged_file_is_not_downloaded_again(file_server: FileServer, tmp_path: Path) -> None:
    """A second run only probes the server."""
    url = _serve(file_server)
    dest = tmp_path / "data.bin"
    fetch(url, dest, log=_quiet)
    file_server.requests.clear()
    record = fetch(url, dest, log=_quiet)
    assert record.status == "unchanged"
    assert [method for method, _, _ in file_server.requests] == ["HEAD"]


def test_changed_file_is_downloaded_again(file_server: FileServer, tmp_path: Path) -> None:
    """A new remote content replaces the local copy."""
    url = _serve(file_server)
    dest = tmp_path / "data.bin"
    fetch(url, dest, log=_quiet)
    _serve(file_server, content=b"new content")
    record = fetch(url, dest, log=_quiet)
    assert record.status == "downloaded"
    assert dest.read_bytes() == b"new content"


def test_interrupted_download_resumes(file_server: FileServer, tmp_path: Path) -> None:
    """A connection cut half-way is resumed with a Range request."""
    url = _serve(file_server)
    dest = tmp_path / "data.bin"
    file_server.cut_next_get_after = 300_000
    record = fetch(url, dest, log=_quiet)
    assert record.status == "resumed"
    assert dest.read_bytes() == CONTENT
    ranges = [requested for method, _, requested in file_server.requests if method == "GET"]
    assert ranges[0] is None
    assert ranges[1] is not None and ranges[1].startswith("bytes=")
    assert int(ranges[1].removeprefix("bytes=").rstrip("-")) > 0


def test_partial_file_from_a_previous_run_is_resumed(
    file_server: FileServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `.part` file left by a killed run is completed, not restarted."""
    url = _serve(file_server)
    dest = tmp_path / "data.bin"
    file_server.cut_next_get_after = 500_000
    with monkeypatch.context() as patch:
        # One attempt only: the run "dies" right after the cut.
        patch.setattr(download, "MAX_ATTEMPTS", 1)
        with pytest.raises(DownloadError):
            fetch(url, dest, log=_quiet)
    part = dest.with_name("data.bin.part")
    offset = part.stat().st_size
    assert 0 < offset < len(CONTENT)
    file_server.requests.clear()
    record = fetch(url, dest, log=_quiet)
    assert record.status == "resumed"
    assert dest.read_bytes() == CONTENT
    resumed = [requested for method, _, requested in file_server.requests if method == "GET"]
    assert resumed == [f"bytes={offset}-"]


def test_checksum_mismatch_is_refused(file_server: FileServer, tmp_path: Path) -> None:
    """A file that does not have the pinned checksum is not kept."""
    url = _serve(file_server)
    dest = tmp_path / "data.bin"
    with pytest.raises(ChecksumMismatchError):
        fetch(url, dest, expected_sha256="0" * 64, log=_quiet)
    assert not dest.exists()
    with pytest.raises(ChecksumMismatchError):
        fetch(url, dest, expected_md5="0" * 32, log=_quiet)
    record = fetch(url, dest, expected_md5=hashlib.md5(CONTENT).hexdigest(), log=_quiet)  # noqa: S324
    assert record.status == "downloaded"


def test_pinned_file_survives_a_changing_etag(file_server: FileServer, tmp_path: Path) -> None:
    """With a pinned checksum, a server that changes its ETag triggers nothing."""
    url = _serve(file_server)
    dest = tmp_path / "data.bin"
    pinned = hashlib.sha256(CONTENT).hexdigest()
    fetch(url, dest, expected_sha256=pinned, log=_quiet)
    sidecar = meta_path(dest)
    sidecar.write_text(sidecar.read_text().replace('"etag": "\\"', '"etag": "\\"other-node-'))
    file_server.requests.clear()
    assert fetch(url, dest, expected_sha256=pinned, log=_quiet).status == "unchanged"
    assert fetch(url, dest, log=_quiet).status == "downloaded"


def test_offline_mode_never_touches_the_network(file_server: FileServer, tmp_path: Path) -> None:
    """Offline, a present file is used and a missing one is an error."""
    url = _serve(file_server)
    dest = tmp_path / "data.bin"
    with pytest.raises(DownloadError, match="offline"):
        fetch(url, dest, offline=True, log=_quiet)
    fetch(url, dest, log=_quiet)
    file_server.requests.clear()
    assert fetch(url, dest, offline=True, log=_quiet).status == "offline"
    assert file_server.requests == []


def test_missing_remote_file_is_an_error(file_server: FileServer, tmp_path: Path) -> None:
    """A 404 is reported, not retried for ever."""
    with pytest.raises(DownloadError, match="404"):
        fetch(f"{file_server.url}/absent.bin", tmp_path / "absent.bin", log=_quiet)


def test_probe_reads_size_and_etag(file_server: FileServer) -> None:
    """The probe learns the size and the ETag without downloading."""
    state = probe(_serve(file_server))
    assert state.size == len(CONTENT)
    assert state.etag
    assert [method for method, _, _ in file_server.requests] == ["HEAD"]


def test_user_agent_names_the_project(monkeypatch: pytest.MonkeyPatch) -> None:
    """Requests identify the project, and a contact when one is configured."""
    monkeypatch.delenv("FOOD_DATA_CONTACT", raising=False)
    assert user_agent().startswith("kratheos-food-data/")
    monkeypatch.setenv("FOOD_DATA_CONTACT", "someone@example.org")
    assert "someone@example.org" in user_agent()


def _dns_response(query: bytes, addresses: list[str]) -> bytes:
    """Build a DNS response answering a query with a CNAME and A records.

    Args:
        query: The query packet.
        addresses: IPv4 addresses to answer with.

    Returns:
        The response packet.
    """
    query_id = struct.unpack("!H", query[:2])[0]
    header = struct.pack("!HHHHHH", query_id, 0x8180, 1, 1 + len(addresses), 0, 0)
    question = query[12:]
    pointer = b"\xc0\x0c"
    alias = b"\x05alias\xc0\x0c"
    answers = pointer + struct.pack("!HHIH", 5, 1, 300, len(alias)) + alias
    for address in addresses:
        answers += pointer + struct.pack("!HHIH", 1, 1, 60, 4) + socket.inet_aton(address)
    return header + question + answers


def test_dns_query_and_answer_parsing() -> None:
    """A query is well-formed and A records are read behind a CNAME."""
    query = dns_fallback.build_query("fdc.nal.usda.gov", 0x1234)
    assert query[:2] == b"\x12\x34"
    assert b"\x03fdc\x03nal\x04usda\x03gov\x00" in query
    response = _dns_response(query, ["192.0.2.10", "192.0.2.11"])
    assert dns_fallback.parse_a_records(response, 0x1234) == ["192.0.2.10", "192.0.2.11"]
    assert dns_fallback.parse_a_records(response, 0x9999) == []
    assert dns_fallback.parse_a_records(b"garbage", 0x1234) == []


def test_nameservers_are_read_from_resolv_conf(tmp_path: Path) -> None:
    """Name servers are read, zone identifiers removed, junk ignored."""
    conf = tmp_path / "resolv.conf"
    conf.write_text("# comment\nnameserver 192.0.2.1\nnameserver fe80::1%en0\nnameserver junk\n")
    assert dns_fallback.system_nameservers(conf) == ["192.0.2.1", "fe80::1"]
    assert dns_fallback.system_nameservers(tmp_path / "absent") == []


def test_resolver_falls_back_to_a_records() -> None:
    """The fallback resolver asks a name server over UDP and returns its answer."""
    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.bind(("127.0.0.1", 0))
    server.settimeout(5)
    port = server.getsockname()[1]

    def answer() -> None:
        """Answer one query."""
        query, client = server.recvfrom(4096)
        server.sendto(_dns_response(query, ["192.0.2.42"]), client)

    thread = threading.Thread(target=answer, daemon=True)
    thread.start()
    original = dns_fallback.DNS_PORT
    dns_fallback.DNS_PORT = port
    try:
        assert dns_fallback.resolve_ipv4("example.org", ["127.0.0.1"]) == ["192.0.2.42"]
    finally:
        dns_fallback.DNS_PORT = original
        thread.join(timeout=5)
        server.close()
    assert dns_fallback.resolve_ipv4("example.org", []) == []
