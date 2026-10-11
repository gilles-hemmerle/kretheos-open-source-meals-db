"""Stores every source file exactly as downloaded, and rebuilds from the copies.

Open data can disappear: a publisher closes a site, replaces a file, changes
an address. So that the database can still be rebuilt, every source file is
kept, untouched, in one of two places declared per source in `sources.toml`
(`mirror`):

* `git`: small files are downloaded straight into `sources/<id>/`, next to
  their `.meta.json` sidecar, and committed with the repository;
* `release`: files too large for git are downloaded into the git-ignored
  `mirror/` directory under the flat name `<id>.<file name>`, to be attached
  to a release (a release has no folders).

Nothing ever rewrites a stored original: adapters only read them, and what
Kratheos adds or corrects lives in `kratheos.sqlite`. To try a changed source,
a build can be given an override, a path used in place of one file for that
run only.

`mirror/MIRROR.json` indexes what `mirror/` holds: for each file its upstream
address, download date, size and checksum. A release refuses a file above
2 GiB, so a larger file (the Open Food Facts export is about 8 GB) is attached
in parts: the index lists the size and checksum of every part next to those
of the whole file, `prepare_release` writes the parts, and `join_parts` puts
them back together and verifies the result.

A build takes each file from upstream and falls back to the stored copy when
upstream fails. `from_mirror` forces the stored copies and never contacts a
publisher, which makes a rebuild reproducible. Copies missing from a local
`mirror/` are fetched from a base address (`mirror_url`), typically the
download address of a release, whole or in parts.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from .config import MIRROR_GIT, MIRROR_RELEASE, Source, SourceFile
from .download import (
    CHUNK_SIZE,
    ChecksumMismatchError,
    DownloadError,
    FileRecord,
    fetch,
    hash_file,
    read_record,
    write_record,
)

INDEX_NAME = "MIRROR.json"
INDEX_FORMAT = 1
# A release asset may not exceed 2 GiB; parts stay safely below.
MAX_ASSET_BYTES = 1_900_000_000
ORIGIN_UPSTREAM = "upstream"
ORIGIN_MIRROR = "mirror"
ORIGIN_OVERRIDE = "override"

Logger = Callable[[str], None]
Overrides = dict[tuple[str, str], Path]


@dataclass(frozen=True)
class Locations:
    """The directories source files are stored in.

    Attributes:
        sources: Committed copies of small sources (`sources/`).
        mirror: Copies of large sources, attached to releases (`mirror/`).
    """

    sources: Path
    mirror: Path


@dataclass(frozen=True)
class Acquired:
    """A source file made available to the build.

    Attributes:
        name: File name as declared in the source list.
        path: Where the file is.
        record: Its description: upstream address, checksums, download date.
        origin: Where this build took it from: upstream, mirror or override.
        detail: What happened, in words (unchanged, downloaded, why a stored
            copy was used...).
    """

    name: str
    path: Path
    record: FileRecord
    origin: str
    detail: str


def asset_name(source_id: str, file_name: str) -> str:
    """Return the flat name of a file stored in the mirror.

    Args:
        source_id: Identifier of the source.
        file_name: File name as declared in the source list.

    Returns:
        The name of the file in `mirror/` and on a release.
    """
    return f"{source_id}.{file_name}"


def part_name(asset: str, number: int, count: int) -> str:
    """Return the name of one part of a file too large for a release.

    Args:
        asset: Name of the whole file.
        number: Position of the part, starting at 1.
        count: Number of parts.

    Returns:
        The name of the part.
    """
    return f"{asset}.part-{number:02d}-of-{count:02d}"


def stored_path(locations: Locations, source: Source, entry: SourceFile) -> Path:
    """Return where a source file is downloaded and stored.

    Args:
        locations: The directories of the workspace.
        source: The source the file belongs to.
        entry: The file.

    Returns:
        The path of the file for the storage kind of the source.
    """
    if source.mirror == MIRROR_GIT:
        return locations.sources / source.storage / entry.name
    return locations.mirror / asset_name(source.id, entry.name)


def parse_overrides(values: list[str] | None) -> Overrides:
    """Read the overrides given on the command line.

    Args:
        values: Items of the form `<source id>/<file name>=<path>`.

    Returns:
        A mapping of (source id, file name) to the path to use instead.

    Raises:
        ValueError: When an item is malformed or its path does not exist.
    """
    overrides: Overrides = {}
    for value in values or []:
        target, separator, path = value.partition("=")
        source_id, slash, name = target.partition("/")
        if not separator or not slash or not source_id or not name or not path:
            raise ValueError(f"override must look like <source>/<file>=<path>, got {value!r}")
        replacement = Path(path).expanduser().resolve()
        if not replacement.is_file():
            raise ValueError(f"override file does not exist: {replacement}")
        overrides[(source_id, name)] = replacement
    return overrides


def empty_index() -> dict:
    """Return a mirror index that lists nothing.

    Returns:
        The index structure, without any file.
    """
    return {"format": INDEX_FORMAT, "generated_at": "", "files": {}}


def load_index(mirror_dir: Path) -> dict:
    """Read `MIRROR.json`.

    Args:
        mirror_dir: The mirror directory.

    Returns:
        The index, empty when the file is missing or unreadable.
    """
    try:
        data = json.loads((mirror_dir / INDEX_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty_index()
    index = empty_index()
    index.update({key: data[key] for key in index if key in data})
    return index


def save_index(mirror_dir: Path, index: dict) -> None:
    """Write `MIRROR.json`, sorted so that it is stable from run to run.

    The file is left alone when only its generation time would change.

    Args:
        mirror_dir: The mirror directory.
        index: The index to write.
    """
    mirror_dir.mkdir(parents=True, exist_ok=True)
    if load_index(mirror_dir)["files"] == index["files"] and (mirror_dir / INDEX_NAME).exists():
        return
    document = dict(index, generated_at=datetime.now(UTC).isoformat(timespec="seconds"))
    text = json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    (mirror_dir / INDEX_NAME).write_text(text, encoding="utf-8")


def asset_url(mirror_url: str, asset: str) -> str:
    """Return the address of a mirrored file under a base address.

    Args:
        mirror_url: Base address of the mirror.
        asset: Flat name of the file.

    Returns:
        The address to download the file from.
    """
    return mirror_url.rstrip("/") + "/" + quote(asset)


def fetch_index(mirror_url: str, mirror_dir: Path, log: Logger) -> dict:
    """Download the index of a remote mirror into the local mirror directory.

    Args:
        mirror_url: Base address of the mirror.
        mirror_dir: The local mirror directory.
        log: Function receiving progress lines.

    Returns:
        The downloaded index.
    """
    fetch(asset_url(mirror_url, INDEX_NAME), mirror_dir / INDEX_NAME, force=True, log=log)
    return load_index(mirror_dir)


def part_checksums(path: Path, part_bytes: int | None = None) -> list[dict]:
    """Compute the size and checksum of each part of a file, without writing them.

    Args:
        path: The whole file.
        part_bytes: Largest size of a part; by default the largest a release
            accepts.

    Returns:
        One entry per part, in order: name, size and SHA-256.
    """
    part_bytes = part_bytes or MAX_ASSET_BYTES
    sizes_and_hashes = []
    with path.open("rb") as handle:
        while True:
            digest = hashlib.sha256()
            size = 0
            while size < part_bytes:
                chunk = handle.read(min(CHUNK_SIZE * 4, part_bytes - size))
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
            if size == 0:
                break
            sizes_and_hashes.append((size, digest.hexdigest()))
    count = len(sizes_and_hashes)
    return [
        {"asset": part_name(path.name, number + 1, count), "size": size, "sha256": sha256}
        for number, (size, sha256) in enumerate(sizes_and_hashes)
    ]


def split_file(path: Path, out_dir: Path, part_bytes: int | None = None) -> list[dict]:
    """Write a file as numbered parts, each small enough for a release.

    The original is only read. A part already present with the right size is
    kept as it is.

    Args:
        path: The whole file.
        out_dir: Directory receiving the parts.
        part_bytes: Largest size of a part; by default the largest a release
            accepts.

    Returns:
        One entry per part, in order: name, size and SHA-256.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    parts = part_checksums(path, part_bytes)
    with path.open("rb") as handle:
        for part in parts:
            target = out_dir / part["asset"]
            if target.exists() and target.stat().st_size == part["size"]:
                handle.seek(part["size"], os.SEEK_CUR)
                continue
            remaining = part["size"]
            with target.open("wb") as out:
                while remaining:
                    chunk = handle.read(min(CHUNK_SIZE * 4, remaining))
                    out.write(chunk)
                    remaining -= len(chunk)
    return parts


def join_parts(part_paths: list[Path], dest: Path, expected_sha256: str) -> None:
    """Put the parts of a file back together and verify the result.

    Args:
        part_paths: The parts, in order.
        dest: Where to write the whole file.
        expected_sha256: Checksum the whole file must have.

    Raises:
        ChecksumMismatchError: When the joined file is not the expected one;
            nothing is left at `dest` in that case.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + ".joining")
    digest = hashlib.sha256()
    with partial.open("wb") as out:
        for part in part_paths:
            with part.open("rb") as handle:
                while chunk := handle.read(CHUNK_SIZE * 4):
                    digest.update(chunk)
                    out.write(chunk)
    if digest.hexdigest() != expected_sha256.lower():
        partial.unlink()
        raise ChecksumMismatchError(f"{dest.name}: the joined parts do not give the expected file")
    os.replace(partial, dest)


def _verified_local_copy(dest: Path, entry: SourceFile) -> FileRecord | None:
    """Return the record of a complete local copy after checking its content.

    Args:
        dest: Where the copy should be.
        entry: The declared file, whose pinned checksum must match, if any.

    Returns:
        The record, or None when there is no complete copy.

    Raises:
        ChecksumMismatchError: When the copy is not what its record says or
            not the file pinned in the source list.
    """
    record = read_record(dest)
    if record is None:
        return None
    sha256, md5 = hash_file(dest)
    if sha256 != record.sha256:
        raise ChecksumMismatchError(f"{dest} was modified since it was downloaded")
    pinned_sha256 = (entry.sha256 or "").lower()
    pinned_md5 = (entry.md5 or "").lower()
    if (pinned_sha256 and sha256 != pinned_sha256) or (pinned_md5 and md5 != pinned_md5):
        raise ChecksumMismatchError(f"{dest} is not the file pinned in the source list")
    return record


def _restore_from_mirror(
    dest: Path,
    listed: dict,
    mirror_url: str | None,
    on_size: Callable[[int], None] | None,
    log: Logger,
) -> str | None:
    """Rebuild a missing stored file from its parts or from a remote mirror.

    Args:
        dest: Where the whole file must be.
        listed: The entry of the file in the mirror index.
        mirror_url: Base address of a remote mirror, if any.
        on_size: Called with the number of bytes about to be downloaded.
        log: Function receiving progress lines.

    Returns:
        Words saying where the file came from, or None when it could not be
        restored (no parts at hand and no remote mirror).
    """
    parts = listed.get("parts") or []
    if not parts:
        if not mirror_url:
            return None
        address = asset_url(mirror_url, dest.name)
        fetch(address, dest, expected_sha256=listed["sha256"], on_size=on_size, log=log)
        return f"downloaded from {address}"
    paths = []
    downloaded = False
    for part in parts:
        path = dest.with_name(part["asset"])
        if not (path.exists() and path.stat().st_size == part["size"]):
            if not mirror_url:
                return None
            address = asset_url(mirror_url, part["asset"])
            fetch(address, path, expected_sha256=part["sha256"], on_size=on_size, log=log)
            downloaded = True
        paths.append(path)
    log(f"  joining {len(paths)} parts into {dest.name}")
    if on_size is not None:
        on_size(listed["size"])
    join_parts(paths, dest, listed["sha256"])
    for path in paths:
        path.unlink()
        path.with_name(path.name + ".meta.json").unlink(missing_ok=True)
    return f"joined from parts downloaded from {mirror_url}" if downloaded else "joined from parts"


def acquire(
    source: Source,
    entry: SourceFile,
    locations: Locations,
    *,
    index: dict,
    overrides: Overrides | None = None,
    from_mirror: bool = False,
    mirror_url: str | None = None,
    force: bool = False,
    on_size: Callable[[int], None] | None = None,
    log: Logger = print,
) -> Acquired:
    """Make one source file available: override, upstream, or stored copy.

    Args:
        source: The source the file belongs to.
        entry: The file.
        locations: The directories of the workspace.
        index: The mirror index, used to restore a file of the mirror.
        overrides: Paths to use instead of some files, for this run only.
        from_mirror: Never contact the publisher; use the stored copy.
        mirror_url: Base address of a remote mirror, for missing copies.
        force: Download from upstream even when the local copy looks current.
        on_size: Called with the number of bytes about to be written.
        log: Function receiving progress lines.

    Returns:
        The file, with where it came from.

    Raises:
        DownloadError: When neither upstream nor a stored copy can provide it.
    """
    override = (overrides or {}).get((source.id, entry.name))
    if override is not None:
        sha256, md5 = hash_file(override)
        modified = datetime.fromtimestamp(override.stat().st_mtime, UTC)
        record = FileRecord(
            url=override.as_uri(),
            name=entry.name,
            size=override.stat().st_size,
            sha256=sha256,
            md5=md5,
            downloaded_at=modified.isoformat(timespec="seconds"),
            status=ORIGIN_OVERRIDE,
        )
        return Acquired(entry.name, override, record, ORIGIN_OVERRIDE, f"override: {override}")

    dest = stored_path(locations, source, entry)
    failure = ""
    if not from_mirror:
        try:
            record = fetch(
                entry.url,
                dest,
                expected_sha256=entry.sha256,
                expected_md5=entry.md5,
                force=force,
                on_size=on_size,
                log=log,
            )
            return Acquired(entry.name, dest, record, ORIGIN_UPSTREAM, record.status)
        except DownloadError as error:
            failure = f" (upstream failed: {error})"
            log(f"  warning: {entry.name}: upstream failed: {error}")

    record = _verified_local_copy(dest, entry)
    if record is not None:
        detail = f"copy stored in {dest.parent.name}/{failure}"
        return Acquired(entry.name, dest, record, ORIGIN_MIRROR, detail)
    listed = index["files"].get(dest.name) if source.mirror == MIRROR_RELEASE else None
    if listed:
        restored = _restore_from_mirror(dest, listed, mirror_url, on_size, log)
        if restored:
            sha256, md5 = hash_file(dest)
            # The sidecar describes the publisher's file, not the mirror.
            record = FileRecord(
                url=listed["url"],
                name=dest.name,
                size=dest.stat().st_size,
                sha256=sha256,
                md5=md5,
                revision=listed.get("revision"),
                downloaded_at=listed["downloaded_at"],
                status=ORIGIN_MIRROR,
            )
            if sha256 != listed["sha256"]:
                dest.unlink()
                raise ChecksumMismatchError(f"{dest.name} is not the file listed in {INDEX_NAME}")
            write_record(dest, record)
            return Acquired(entry.name, dest, record, ORIGIN_MIRROR, restored + failure)
    raise DownloadError(f"{entry.name}: no stored copy at {dest}{failure}")


def directory_files(source: Source, locations: Locations, pattern: str = "*.csv") -> list[Acquired]:
    """List the files of a source that is read from a directory of `sources/`.

    Such a source is not downloaded: whoever commits a file to the directory
    made it available. The files are taken as they are, in name order, and
    the source is simply empty when the directory is missing or holds none.

    Args:
        source: A source declaring a `directory`.
        locations: The directories of the workspace.
        pattern: Which files of the directory belong to the source.

    Returns:
        The files, each described with its size and checksums.
    """
    folder = locations.sources / source.storage
    if not folder.is_dir():
        return []
    files = []
    for path in sorted(folder.glob(pattern)):
        if not path.is_file():
            continue
        sha256, md5 = hash_file(path)
        record = FileRecord(
            url=f"sources/{source.storage}/{path.name}",
            name=path.name,
            size=path.stat().st_size,
            sha256=sha256,
            md5=md5,
            status=ORIGIN_MIRROR,
        )
        detail = f"file stored in sources/{source.storage}/"
        files.append(Acquired(path.name, path, record, ORIGIN_MIRROR, detail))
    return files


def update_index(mirror_dir: Path, index: dict, source: Source, acquired: list[Acquired]) -> dict:
    """Record in the index the mirrored files a build just used.

    Part checksums of a file too large for a release are computed once and
    reused as long as the file keeps the same checksum.

    Args:
        mirror_dir: The mirror directory.
        index: The current index.
        source: The source the files belong to.
        acquired: Its files, as acquired by the build.

    Returns:
        The index, updated in place.
    """
    if source.mirror != MIRROR_RELEASE:
        return index
    for item in acquired:
        if item.origin == ORIGIN_OVERRIDE:
            continue
        record = item.record
        asset = asset_name(source.id, item.name)
        previous = index["files"].get(asset, {})
        entry = {
            "source": source.id,
            "name": item.name,
            "url": record.url,
            "size": record.size,
            "sha256": record.sha256,
            "md5": record.md5,
            "revision": record.revision,
            "downloaded_at": record.downloaded_at,
        }
        if record.size > MAX_ASSET_BYTES:
            if previous.get("sha256") == record.sha256 and previous.get("parts"):
                entry["parts"] = previous["parts"]
            else:
                entry["parts"] = part_checksums(mirror_dir / asset)
        index["files"][asset] = entry
    return index


def prepare_release(mirror_dir: Path, out_dir: Path, log: Logger = print) -> list[dict]:
    """Lay out in a directory exactly what a release must carry.

    Files small enough are hard-linked (copied when linking is impossible);
    larger ones are written as parts. The index goes with them.

    Args:
        mirror_dir: The mirror directory.
        out_dir: Directory receiving the files to attach.
        log: Function receiving progress lines.

    Returns:
        One entry per file to attach: name, size and SHA-256.

    Raises:
        FileNotFoundError: When a file listed in the index is missing.
    """
    index = load_index(mirror_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    attached = []
    for asset in sorted(index["files"]):
        entry = index["files"][asset]
        source = mirror_dir / asset
        if not source.exists():
            raise FileNotFoundError(f"{source} is listed in {INDEX_NAME} but missing")
        if entry.get("parts"):
            log(f"  splitting {asset} into {len(entry['parts'])} parts")
            written = split_file(source, out_dir)
            if written != entry["parts"]:
                raise ChecksumMismatchError(f"{asset}: parts differ from those of {INDEX_NAME}")
            attached += written
            continue
        target = out_dir / asset
        if not (target.exists() and target.stat().st_size == entry["size"]):
            target.unlink(missing_ok=True)
            try:
                os.link(source, target)
            except OSError:
                shutil.copyfile(source, target)
        attached.append({"asset": asset, "size": entry["size"], "sha256": entry["sha256"]})
    shutil.copyfile(mirror_dir / INDEX_NAME, out_dir / INDEX_NAME)
    sha256, _ = hash_file(out_dir / INDEX_NAME)
    size = (out_dir / INDEX_NAME).stat().st_size
    attached.insert(0, {"asset": INDEX_NAME, "size": size, "sha256": sha256})
    return attached


def describe(acquired: Acquired, index: dict, source: Source) -> dict:
    """Describe an acquired file for the manifest.

    Args:
        acquired: The file, as acquired by the build.
        index: The mirror index, which knows the parts of large files.
        source: The source the file belongs to.

    Returns:
        Name, addresses, size, checksums, date, origin and, for a file too
        large for a release, the size and checksum of each part.
    """
    record = acquired.record
    entry = {
        "name": acquired.name,
        "url": record.url,
        "size": record.size,
        "sha256": record.sha256,
        "md5": record.md5,
        "etag": record.etag,
        "last_modified": record.last_modified,
        "revision": record.revision,
        "downloaded_at": record.downloaded_at,
        "origin": acquired.origin,
        "origin_detail": acquired.detail,
    }
    if acquired.origin != ORIGIN_OVERRIDE:
        if source.mirror == MIRROR_GIT:
            entry["stored_as"] = f"sources/{source.storage}/{acquired.name}"
        else:
            asset = asset_name(source.id, acquired.name)
            entry["stored_as"] = f"mirror/{asset}"
            parts = index["files"].get(asset, {}).get("parts")
            if parts:
                entry["parts"] = parts
    return entry
