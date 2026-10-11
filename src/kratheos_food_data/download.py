"""Polite, resumable and checksummed file downloads.

Every source file is fetched with this module, one request at a time:

* a probe (HEAD) learns the size and the validators of the remote file;
* a complete local file is not downloaded again when it has the checksum
  pinned in `sources.toml`, or else when its HTTP validators still match;
* an interrupted download leaves a `.part` file that the next run resumes
  with an HTTP Range request, guarded by `If-Range` so that a remote file that
  changed in between restarts from zero;
* the finished file is hashed (SHA-256, and MD5 when the publisher only gives
  that), checked against the checksum the publisher advertises or the one
  pinned in `sources.toml`, and only then moved into place;
* a `<name>.meta.json` sidecar keeps what the manifest needs: URL, size,
  checksums, ETag, Last-Modified and the time of the download.

Only the standard library is used. Requests carry a User-Agent naming the
project, never run in parallel and are retried with an increasing pause.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

from . import __version__
from .dns_fallback import resolve_ipv4

CHUNK_SIZE = 1024 * 1024
MAX_ATTEMPTS = 6
MAX_REDIRECTS = 8
SOCKET_TIMEOUT_SECONDS = 60
PROGRESS_INTERVAL_SECONDS = 30
REDIRECT_CODES = {301, 302, 303, 307, 308}
RETRYABLE_HTTP_CODES = {408, 425, 429, 500, 502, 503, 504}
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")

Logger = Callable[[str], None]


class DownloadError(RuntimeError):
    """A file could not be downloaded or is missing in offline mode."""


class ChecksumMismatchError(DownloadError):
    """A downloaded file does not have the expected size or checksum."""


@dataclass(frozen=True)
class RemoteState:
    """What a probe learned about a remote file.

    Attributes:
        size: Length in bytes, when the server tells it.
        etag: ETag of the final response, quotes included.
        last_modified: Last-Modified header of the final response.
        sha256: SHA-256 advertised by the publisher (Hugging Face does), if any.
        revision: Repository revision advertised by the publisher, if any.
    """

    size: int | None = None
    etag: str | None = None
    last_modified: str | None = None
    sha256: str | None = None
    revision: str | None = None


@dataclass(frozen=True)
class FileRecord:
    """A downloaded file, as stored in its `.meta.json` sidecar.

    Attributes:
        url: Address the file was downloaded from.
        name: File name inside the source's download directory.
        size: Length in bytes.
        sha256: SHA-256 of the content, hexadecimal.
        md5: MD5 of the content, kept because some publishers only give that.
        etag: ETag seen at download time.
        last_modified: Last-Modified header seen at download time.
        revision: Repository revision seen at download time, if any.
        downloaded_at: UTC time the download finished, ISO 8601.
        status: What the last call did: downloaded, resumed, unchanged, offline.
    """

    url: str
    name: str
    size: int
    sha256: str
    md5: str
    etag: str | None = None
    last_modified: str | None = None
    revision: str | None = None
    downloaded_at: str = ""
    status: str = field(default="downloaded", compare=False)


def user_agent() -> str:
    """Return the User-Agent sent with every request.

    Returns:
        The project name and version, followed by the contact given in the
        FOOD_DATA_CONTACT environment variable when it is set.
    """
    contact = os.environ.get("FOOD_DATA_CONTACT", "").strip()
    suffix = f"; {contact}" if contact else ""
    return f"kratheos-food-data/{__version__} (open food database aggregator{suffix})"


def _create_connection(address: tuple[str, int], *args: object, **kwargs: object) -> socket.socket:
    """Open a TCP connection, resolving the host ourselves if the system cannot.

    Args:
        address: Host and port to connect to.
        *args: Passed to `socket.create_connection`.
        **kwargs: Passed to `socket.create_connection`.

    Returns:
        The connected socket.

    Raises:
        socket.gaierror: When neither the system resolver nor the fallback
            lookup can resolve the host.
    """
    try:
        return socket.create_connection(address, *args, **kwargs)
    except socket.gaierror as error:
        host, port = address
        for ip in resolve_ipv4(host):
            try:
                return socket.create_connection((ip, port), *args, **kwargs)
            except OSError:
                continue
        raise error


class _FallbackHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection that uses the fallback resolver.

    The TLS handshake still names and verifies the original host.
    """

    def __init__(self, *args: object, **kwargs: object) -> None:
        """Create the connection and plug the fallback resolver in."""
        super().__init__(*args, **kwargs)
        self._create_connection = _create_connection


class _FallbackHTTPConnection(http.client.HTTPConnection):
    """Plain HTTP connection that uses the fallback resolver."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        """Create the connection and plug the fallback resolver in."""
        super().__init__(*args, **kwargs)
        self._create_connection = _create_connection


class _HTTPSHandler(urllib.request.HTTPSHandler):
    """urllib handler that opens HTTPS URLs with the fallback connection."""

    def __init__(self) -> None:
        """Create the handler with a default, certificate-verifying context."""
        self._ssl_context = ssl.create_default_context()
        super().__init__(context=self._ssl_context)

    def https_open(self, req: urllib.request.Request) -> http.client.HTTPResponse:
        """Open an HTTPS request.

        Args:
            req: The request to send.

        Returns:
            The server response.
        """
        return self.do_open(_FallbackHTTPSConnection, req, context=self._ssl_context)


class _HTTPHandler(urllib.request.HTTPHandler):
    """urllib handler that opens HTTP URLs with the fallback connection."""

    def http_open(self, req: urllib.request.Request) -> http.client.HTTPResponse:
        """Open an HTTP request.

        Args:
            req: The request to send.

        Returns:
            The server response.
        """
        return self.do_open(_FallbackHTTPConnection, req)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Stops urllib from following redirects, so each hop can be inspected."""

    def redirect_request(self, *args: object, **kwargs: object) -> None:
        """Refuse the redirect; the caller follows it by hand."""
        return None


_OPENER = urllib.request.build_opener(_HTTPSHandler(), _HTTPHandler(), _NoRedirect())


def _open(
    url: str, headers: dict[str, str] | None = None, method: str = "GET"
) -> tuple[http.client.HTTPResponse, list[http.client.HTTPMessage]]:
    """Send a request and follow redirects by hand.

    Args:
        url: Address to request.
        headers: Extra request headers, sent again on every hop.
        method: HTTP method.

    Returns:
        The final response (to be closed by the caller) and the headers of
        every hop, final one last. Publishers such as Hugging Face put the
        checksum of the file on the redirecting hop.

    Raises:
        urllib.error.HTTPError: On a final status that is not a success.
        DownloadError: When there are too many redirects.
    """
    hops: list[http.client.HTTPMessage] = []
    current = url
    for _ in range(MAX_REDIRECTS):
        request = urllib.request.Request(current, method=method)
        request.add_header("User-Agent", user_agent())
        request.add_header("Accept-Encoding", "identity")
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            response = _OPENER.open(request, timeout=SOCKET_TIMEOUT_SECONDS)
        except urllib.error.HTTPError as error:
            location = error.headers.get("Location")
            if error.code in REDIRECT_CODES and location:
                hops.append(error.headers)
                error.close()
                current = urllib.parse.urljoin(current, location)
                continue
            raise
        hops.append(response.headers)
        return response, hops
    raise DownloadError(f"too many redirects for {url}")


def _remote_state(hops: list[http.client.HTTPMessage], total_size: int | None) -> RemoteState:
    """Build the remote state from the headers of every hop.

    Args:
        hops: Headers of each response, final one last.
        total_size: Size of the whole file as told by the final response.

    Returns:
        The validators of the final response, completed by the checksum and
        revision a redirecting hop may have advertised.
    """
    final = hops[-1]
    sha256 = None
    revision = None
    linked_size = None
    for headers in hops:
        linked = (headers.get("X-Linked-Etag") or "").strip('"').lower()
        if SHA256_PATTERN.match(linked):
            sha256 = linked
        revision = headers.get("X-Repo-Commit") or revision
        if (headers.get("X-Linked-Size") or "").isdigit():
            linked_size = int(headers["X-Linked-Size"])
    return RemoteState(
        size=total_size if total_size is not None else linked_size,
        etag=final.get("ETag"),
        last_modified=final.get("Last-Modified"),
        sha256=sha256,
        revision=revision,
    )


def probe(url: str) -> RemoteState:
    """Ask the server about a file without downloading it.

    A HEAD request is tried first; servers that refuse it get a one-byte
    ranged GET instead.

    Args:
        url: Address of the file.

    Returns:
        The size and validators of the remote file.

    Raises:
        DownloadError: When the server cannot be reached or answers an error.
    """
    last_error: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            try:
                response, hops = _open(url, method="HEAD")
                with response:
                    length = response.headers.get("Content-Length")
                    size = int(length) if length and length.isdigit() else None
                return _remote_state(hops, size)
            except urllib.error.HTTPError as error:
                if error.code not in {403, 405, 501}:
                    raise
            response, hops = _open(url, headers={"Range": "bytes=0-0"})
            with response:
                content_range = response.headers.get("Content-Range", "")
                match = re.search(r"/(\d+)$", content_range)
                if match:
                    size = int(match.group(1))
                else:
                    length = response.headers.get("Content-Length")
                    size = int(length) if response.status == 200 and length else None
            return _remote_state(hops, size)
        except urllib.error.HTTPError as error:
            if error.code not in RETRYABLE_HTTP_CODES:
                raise DownloadError(f"{url}: HTTP {error.code} {error.reason}") from error
            last_error = error
        except (OSError, http.client.HTTPException) as error:
            last_error = error
        if attempt < MAX_ATTEMPTS:
            time.sleep(min(60, 2**attempt))
    raise DownloadError(f"{url}: cannot reach the server ({last_error})")


def meta_path(dest: Path) -> Path:
    """Return the path of the sidecar that describes a downloaded file.

    Args:
        dest: Path of the downloaded file.

    Returns:
        The path of its `.meta.json` sidecar.
    """
    return dest.with_name(dest.name + ".meta.json")


def read_record(dest: Path) -> FileRecord | None:
    """Read the sidecar of a downloaded file.

    Args:
        dest: Path of the downloaded file.

    Returns:
        The stored record, or None when the file or its sidecar is missing or
        unreadable.
    """
    path = meta_path(dest)
    if not dest.exists() or not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        fields = [key for key in FileRecord.__dataclass_fields__ if key != "status"]
        return FileRecord(**{key: data.get(key) for key in fields})
    except (OSError, ValueError, TypeError):
        return None


def write_record(dest: Path, record: FileRecord) -> None:
    """Write the sidecar that describes a downloaded file.

    Args:
        dest: Path of the downloaded file.
        record: Its description; the status is not stored.
    """
    stored_fields = {key: value for key, value in asdict(record).items() if key != "status"}
    meta_path(dest).write_text(json.dumps(stored_fields, indent=2) + "\n", encoding="utf-8")


def hash_file(path: Path) -> tuple[str, str]:
    """Compute the SHA-256 and MD5 of a file in one pass.

    Args:
        path: File to hash.

    Returns:
        The hexadecimal SHA-256 and MD5 digests.
    """
    sha256 = hashlib.sha256()
    md5 = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK_SIZE * 4):
            sha256.update(chunk)
            md5.update(chunk)
    return sha256.hexdigest(), md5.hexdigest()


def _is_unchanged(stored: FileRecord, remote: RemoteState) -> bool:
    """Tell whether the local copy still matches the remote file.

    The strongest validator both sides know is used: the advertised SHA-256,
    then the ETag, then Last-Modified with the size, then the size alone.

    Args:
        stored: Record of the local copy.
        remote: Result of the probe.

    Returns:
        True when nothing indicates that the remote file changed.
    """
    if remote.size is not None and remote.size != stored.size:
        return False
    if remote.sha256:
        return remote.sha256 == stored.sha256
    if remote.etag and stored.etag:
        return remote.etag == stored.etag
    if remote.last_modified and stored.last_modified:
        return remote.last_modified == stored.last_modified
    return remote.size is not None


def _matches_pin(
    stored: FileRecord,
    remote: RemoteState,
    expected_sha256: str | None,
    expected_md5: str | None,
) -> bool:
    """Tell whether the local copy is the file pinned in the source list.

    A pinned checksum is a stronger proof than any HTTP validator. It also is
    the only reliable one for servers whose nodes disagree on the ETag and the
    Last-Modified date of the same file (seen on the USDA download server).

    Args:
        stored: Record of the local copy.
        remote: Result of the probe.
        expected_sha256: SHA-256 pinned in the source list, if any.
        expected_md5: MD5 pinned in the source list, if any.

    Returns:
        True when a checksum is pinned, the local copy has it and the remote
        file still has the same size.
    """
    if remote.size is not None and remote.size != stored.size:
        return False
    if expected_sha256:
        return stored.sha256 == expected_sha256.lower()
    if expected_md5:
        return stored.md5 == expected_md5.lower()
    return False


def _resume_token(remote: RemoteState) -> str | None:
    """Return the value to send in If-Range when resuming, if resuming is safe.

    Args:
        remote: Result of the probe.

    Returns:
        A strong ETag, else the Last-Modified date, else None (no safe resume).
    """
    if remote.etag and not remote.etag.startswith("W/"):
        return remote.etag
    return remote.last_modified


def _validator(remote: RemoteState) -> str:
    """Return a string that changes whenever the remote file changes.

    Args:
        remote: Result of the probe.

    Returns:
        A token stored next to a partial download to detect a remote change.
    """
    return "|".join(
        str(part) for part in (remote.sha256, remote.etag, remote.last_modified, remote.size)
    )


def _format_size(size: float) -> str:
    """Format a byte count for the log.

    Args:
        size: Number of bytes.

    Returns:
        The size with a binary unit, e.g. `7.4 GiB`.
    """
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GiB"


def _stream_to_part(url: str, part: Path, remote: RemoteState, log: Logger) -> bool:
    """Download what is missing of a file into its `.part` file.

    Args:
        url: Address of the file.
        part: Path of the partial file, appended to when it already has data.
        remote: Result of the probe.
        log: Function receiving progress lines.

    Returns:
        True when the request resumed an existing partial file.

    Raises:
        urllib.error.HTTPError: On an HTTP error status.
        OSError: On a network or disk error.
    """
    offset = part.stat().st_size if part.exists() else 0
    token = _resume_token(remote)
    headers: dict[str, str] = {}
    if offset and token:
        headers = {"Range": f"bytes={offset}-", "If-Range": token}
    else:
        offset = 0
    response, _ = _open(url, headers=headers)
    with response:
        resumed = offset > 0 and response.status == 206
        if not resumed:
            offset = 0
        started = last_log = time.monotonic()
        received = 0
        with part.open("ab" if resumed else "wb") as out:
            while chunk := response.read(CHUNK_SIZE):
                out.write(chunk)
                received += len(chunk)
                now = time.monotonic()
                if now - last_log >= PROGRESS_INTERVAL_SECONDS:
                    last_log = now
                    done = offset + received
                    speed = received / max(now - started, 1e-6)
                    total = f" of {_format_size(remote.size)}" if remote.size else ""
                    log(f"    {_format_size(done)}{total} ({_format_size(speed)}/s)")
    return resumed


def fetch(
    url: str,
    dest: Path,
    *,
    expected_sha256: str | None = None,
    expected_md5: str | None = None,
    offline: bool = False,
    force: bool = False,
    on_size: Callable[[int], None] | None = None,
    log: Logger = print,
) -> FileRecord:
    """Make sure a remote file is present locally, complete and verified.

    Args:
        url: Address of the file.
        dest: Where the file must be stored.
        expected_sha256: SHA-256 pinned in the source list, if any.
        expected_md5: MD5 pinned in the source list, if any.
        offline: Never touch the network; fail if the file is not there.
        force: Download again even when the local copy looks current.
        on_size: Called with the number of bytes about to be downloaded,
            before the download starts; it may raise to refuse it (the
            build uses it to protect the free disk space).
        log: Function receiving progress lines.

    Returns:
        The record of the file, whose status says what happened.

    Raises:
        DownloadError: When the file cannot be obtained.
        ChecksumMismatchError: When the content is not what was announced.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    stored = read_record(dest)
    if offline:
        if stored is None:
            raise DownloadError(f"{dest} is missing and the run is offline")
        return replace(stored, status="offline")

    remote = probe(url)
    if (
        not force
        and stored is not None
        and stored.url == url
        and dest.stat().st_size == stored.size
        and (
            _matches_pin(stored, remote, expected_sha256, expected_md5)
            or _is_unchanged(stored, remote)
        )
    ):
        return replace(stored, status="unchanged")

    part = dest.with_name(dest.name + ".part")
    part_meta = dest.with_name(dest.name + ".part.json")
    validator = _validator(remote)
    if part.exists():
        try:
            same = json.loads(part_meta.read_text(encoding="utf-8")).get("validator") == validator
        except (OSError, ValueError):
            same = False
        if force or not same or (remote.size is not None and part.stat().st_size > remote.size):
            part.unlink()
    part_meta.write_text(json.dumps({"url": url, "validator": validator}), encoding="utf-8")
    if on_size is not None and remote.size is not None:
        on_size(max(remote.size - (part.stat().st_size if part.exists() else 0), 0))

    resumed_any = False
    for attempt in range(1, MAX_ATTEMPTS + 1):
        have = part.stat().st_size if part.exists() else 0
        if remote.size is not None and have == remote.size:
            break
        try:
            resumed_any = _stream_to_part(url, part, remote, log) or resumed_any
        except urllib.error.HTTPError as error:
            if error.code == 416:
                part.unlink(missing_ok=True)
            elif error.code not in RETRYABLE_HTTP_CODES:
                raise DownloadError(f"{url}: HTTP {error.code} {error.reason}") from error
            failure: Exception = error
        except (OSError, http.client.HTTPException) as error:
            failure = error
        else:
            have = part.stat().st_size
            if remote.size is None or have == remote.size:
                break
            failure = DownloadError(f"got {have} bytes of {remote.size}")
        if attempt == MAX_ATTEMPTS:
            raise DownloadError(f"{url}: giving up after {attempt} attempts ({failure})")
        pause = min(120, 5 * 2 ** (attempt - 1))
        log(f"    interrupted ({failure}); retrying in {pause} s")
        time.sleep(pause)

    size = part.stat().st_size
    sha256, md5 = hash_file(part)
    problems = []
    if remote.size is not None and size != remote.size:
        problems.append(f"size {size}, expected {remote.size}")
    for label, actual, wanted in (
        ("sha256", sha256, expected_sha256),
        ("sha256 advertised by the publisher", sha256, remote.sha256),
        ("md5", md5, expected_md5),
    ):
        if wanted and actual != wanted.lower():
            problems.append(f"{label} {actual}, expected {wanted}")
    if problems:
        part.unlink()
        part_meta.unlink(missing_ok=True)
        raise ChecksumMismatchError(f"{url}: " + "; ".join(problems))

    os.replace(part, dest)
    part_meta.unlink(missing_ok=True)
    record = FileRecord(
        url=url,
        name=dest.name,
        size=size,
        sha256=sha256,
        md5=md5,
        etag=remote.etag,
        last_modified=remote.last_modified,
        revision=remote.revision,
        downloaded_at=datetime.now(UTC).isoformat(timespec="seconds"),
        status="resumed" if resumed_any else "downloaded",
    )
    write_record(dest, record)
    return record
