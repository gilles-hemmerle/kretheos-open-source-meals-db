"""Tests of the storage of the originals: split, join, fallback, overrides."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import FileServer, quiet

from kratheos_food_data import mirror
from kratheos_food_data.config import Source, SourceFile
from kratheos_food_data.download import ChecksumMismatchError, DownloadError, read_record

CONTENT = bytes(range(256)) * 1000  # 256 000 bytes
PART_BYTES = 100_000


def _source(declared: dict[str, Source], kind: str, url: str, **pins: str) -> Source:
    """Build a one-file source stored the given way.

    Args:
        declared: The real source list, used as a template.
        kind: Storage kind: git or release.
        url: Address of the file.
        **pins: Pinned checksums of the file.

    Returns:
        The source.
    """
    entry = SourceFile(name="data.bin", url=url, **pins)
    return replace(declared["ciqual"], id="demo", mirror=kind, files=(entry,))


def _locations(tmp_path: Path) -> mirror.Locations:
    """Give the storage directories of a test repository."""
    return mirror.Locations(sources=tmp_path / "sources", mirror=tmp_path / "mirror")


def test_split_and_join_give_back_the_same_file(tmp_path: Path) -> None:
    """Parts are below the limit, listed with checksums, and join back exactly."""
    whole = tmp_path / "big.bin"
    whole.write_bytes(CONTENT)
    before = hashlib.sha256(whole.read_bytes()).hexdigest()
    parts = mirror.split_file(whole, tmp_path / "parts", PART_BYTES)
    assert [part["asset"] for part in parts] == [
        "big.bin.part-01-of-03",
        "big.bin.part-02-of-03",
        "big.bin.part-03-of-03",
    ]
    assert [part["size"] for part in parts] == [100_000, 100_000, 56_000]
    assert parts == mirror.part_checksums(whole, PART_BYTES)
    for part in parts:
        data = (tmp_path / "parts" / part["asset"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == part["sha256"]
    assert hashlib.sha256(whole.read_bytes()).hexdigest() == before  # the original is only read

    joined = tmp_path / "joined.bin"
    paths = [tmp_path / "parts" / part["asset"] for part in parts]
    mirror.join_parts(paths, joined, before)
    assert joined.read_bytes() == CONTENT
    with pytest.raises(ChecksumMismatchError):
        mirror.join_parts(list(reversed(paths)), tmp_path / "wrong.bin", before)
    assert not (tmp_path / "wrong.bin").exists()


def test_files_are_stored_by_kind(
    file_server: FileServer, tmp_path: Path, declared_sources: dict[str, Source]
) -> None:
    """A small source goes to sources/<id>/, a large one to mirror/ under a flat name."""
    (file_server.root / "data.bin").write_bytes(CONTENT)
    url = f"{file_server.url}/data.bin"
    locations = _locations(tmp_path)
    index = mirror.empty_index()
    for kind, expected in (
        ("git", tmp_path / "sources" / "demo" / "data.bin"),
        ("release", tmp_path / "mirror" / "demo.data.bin"),
    ):
        source = _source(declared_sources, kind, url)
        item = mirror.acquire(source, source.files[0], locations, index=index, log=quiet)
        assert item.path == expected and expected.read_bytes() == CONTENT
        assert (item.origin, item.detail) == (mirror.ORIGIN_UPSTREAM, "downloaded")
        again = mirror.acquire(source, source.files[0], locations, index=index, log=quiet)
        assert (again.origin, again.detail) == (mirror.ORIGIN_UPSTREAM, "unchanged")


def test_stored_copy_is_used_when_upstream_fails(
    file_server: FileServer, tmp_path: Path, declared_sources: dict[str, Source]
) -> None:
    """Upstream gone: the stored copy is used and the manifest will say so."""
    (file_server.root / "data.bin").write_bytes(CONTENT)
    source = _source(declared_sources, "git", f"{file_server.url}/data.bin")
    locations = _locations(tmp_path)
    index = mirror.empty_index()
    mirror.acquire(source, source.files[0], locations, index=index, log=quiet)
    (file_server.root / "data.bin").unlink()  # the publisher disappears
    item = mirror.acquire(source, source.files[0], locations, index=index, log=quiet)
    assert item.origin == mirror.ORIGIN_MIRROR
    assert "upstream failed" in item.detail and "404" in item.detail
    assert item.path.read_bytes() == CONTENT

    gone = _source(declared_sources, "git", f"{file_server.url}/never-there.bin")
    gone = replace(gone, id="other")
    with pytest.raises(DownloadError, match="no stored copy"):
        mirror.acquire(gone, gone.files[0], locations, index=index, log=quiet)


def test_mirror_mode_never_contacts_the_publisher_and_verifies(
    file_server: FileServer, tmp_path: Path, declared_sources: dict[str, Source]
) -> None:
    """Forced mirror: no request upstream, and a tampered copy is refused."""
    (file_server.root / "data.bin").write_bytes(CONTENT)
    source = _source(declared_sources, "git", f"{file_server.url}/data.bin")
    locations = _locations(tmp_path)
    index = mirror.empty_index()
    first = mirror.acquire(source, source.files[0], locations, index=index, log=quiet)
    file_server.requests.clear()
    item = mirror.acquire(
        source, source.files[0], locations, index=index, from_mirror=True, log=quiet
    )
    assert item.origin == mirror.ORIGIN_MIRROR and file_server.requests == []

    first.path.write_bytes(CONTENT[:-1] + b"!")
    with pytest.raises(ChecksumMismatchError, match="modified"):
        mirror.acquire(source, source.files[0], locations, index=index, from_mirror=True, log=quiet)


def test_override_replaces_a_file_for_one_run(
    file_server: FileServer, tmp_path: Path, declared_sources: dict[str, Source]
) -> None:
    """An override is used as it is; the stored original is not touched."""
    (file_server.root / "data.bin").write_bytes(CONTENT)
    source = _source(declared_sources, "git", f"{file_server.url}/data.bin")
    locations = _locations(tmp_path)
    index = mirror.empty_index()
    stored = mirror.acquire(source, source.files[0], locations, index=index, log=quiet)
    checksum = hashlib.sha256(stored.path.read_bytes()).hexdigest()

    changed = tmp_path / "changed.bin"
    changed.write_bytes(CONTENT + b"one more line\n")
    overrides = mirror.parse_overrides([f"demo/data.bin={changed}"])
    item = mirror.acquire(
        source, source.files[0], locations, index=index, overrides=overrides, log=quiet
    )
    assert item.origin == mirror.ORIGIN_OVERRIDE and item.path == changed
    assert item.record.sha256 == hashlib.sha256(changed.read_bytes()).hexdigest()
    assert hashlib.sha256(stored.path.read_bytes()).hexdigest() == checksum
    assert read_record(stored.path).sha256 == checksum

    with pytest.raises(ValueError, match="override"):
        mirror.parse_overrides(["no-equal-sign"])
    with pytest.raises(ValueError, match="does not exist"):
        mirror.parse_overrides([f"demo/data.bin={tmp_path / 'absent'}"])


def test_large_file_is_restored_from_a_release_in_parts(
    file_server: FileServer,
    tmp_path: Path,
    declared_sources: dict[str, Source],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A file above the limit goes to a release in parts and comes back whole."""
    monkeypatch.setattr(mirror, "MAX_ASSET_BYTES", PART_BYTES)
    (file_server.root / "data.bin").write_bytes(CONTENT)
    (file_server.root / "small.bin").write_bytes(b"small file")
    files = (
        SourceFile(name="data.bin", url=f"{file_server.url}/data.bin"),
        SourceFile(name="small.bin", url=f"{file_server.url}/small.bin"),
    )
    source = replace(declared_sources["ciqual"], id="demo", mirror="release", files=files)

    # A first repository downloads from the publisher and prepares a release.
    first = mirror.Locations(sources=tmp_path / "a" / "sources", mirror=tmp_path / "a" / "mirror")
    index = mirror.empty_index()
    acquired = [mirror.acquire(source, entry, first, index=index, log=quiet) for entry in files]
    mirror.update_index(first.mirror, index, source, acquired)
    mirror.save_index(first.mirror, index)
    listed = index["files"]["demo.data.bin"]
    assert [part["size"] for part in listed["parts"]] == [100_000, 100_000, 56_000]
    assert "parts" not in index["files"]["demo.small.bin"]
    described = mirror.describe(acquired[0], index, source)
    assert described["stored_as"] == "mirror/demo.data.bin"
    assert described["parts"] == listed["parts"] and described["sha256"] == listed["sha256"]

    release = file_server.root / "release"
    attached = mirror.prepare_release(first.mirror, release, log=quiet)
    assert [entry["asset"] for entry in attached] == [
        "MIRROR.json",
        "demo.data.bin.part-01-of-03",
        "demo.data.bin.part-02-of-03",
        "demo.data.bin.part-03-of-03",
        "demo.small.bin",
    ]
    assert all((release / entry["asset"]).stat().st_size == entry["size"] for entry in attached)
    assert not (release / "demo.data.bin").exists()  # only its parts are attached

    # The publisher disappears; a second repository rebuilds from the release.
    (file_server.root / "data.bin").unlink()
    (file_server.root / "small.bin").unlink()
    second = mirror.Locations(sources=tmp_path / "b" / "sources", mirror=tmp_path / "b" / "mirror")
    mirror_url = f"{file_server.url}/release"
    fetched = mirror.fetch_index(mirror_url, second.mirror, quiet)
    for entry in files:
        item = mirror.acquire(
            source, entry, second, index=fetched, from_mirror=True, mirror_url=mirror_url, log=quiet
        )
        assert item.origin == mirror.ORIGIN_MIRROR
        assert item.record.url == entry.url  # the sidecar still names the publisher
    assert (second.mirror / "demo.data.bin").read_bytes() == CONTENT
    assert (second.mirror / "demo.small.bin").read_bytes() == b"small file"
    assert not list(second.mirror.glob("*.part-*"))  # the parts are removed once joined
    record = json.loads((second.mirror / "demo.data.bin.meta.json").read_text())
    assert record["sha256"] == hashlib.sha256(CONTENT).hexdigest()
