"""Replaces the main database being served: by a rebuild, or by a ready-made file.

Both ways end the same: the new `food.sqlite` is checked, then moved over
the one being served in one step (a rename on the same disk), so the service
never sees half a file and picks the new one up at its next request
(`catalogue.py`). The manifest of the build is put in place just before, so
that the build date served matches the file.

The periodic rebuild (`rebuild`):

1. writes the public layer from the live database, so that the build takes
   in everything entered until now;
2. builds into a directory of its own (`work/rebuild/dist`), never into the
   files being served;
3. compares the result with the database being served: number of foods, of
   foods per source, and the identifiers (the comparison of
   `sqlite_export.compare_identifiers`);
4. refuses the result when it loses a suspicious share of what was there
   (by default more than 2 % of the foods or of the identifiers, or more
   than 20 % of the foods of one source): the old file stays, the new one is
   left in `work/rebuild/` to be looked at;
5. otherwise swaps it in.

A ready-made database (`install_database`): someone who hosts the service
does not need the 8 GB of originals. `database_files` lays out what to
publish under a base address (the database, whole or in parts below the
2 GiB a release accepts, its manifest, the identifier list and the layer it
was built with, indexed with their checksums by `DATABASE.json`), and
`install_database` downloads them, verifies every checksum, applies the same
comparison and swaps the file in.
"""

from __future__ import annotations

import dataclasses
import gzip
import json
import os
import shutil
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from .. import kratheos, mirror, sqlite_export
from ..build import (
    FOOD_DATABASE_NAME,
    IDENTIFIERS_NAME,
    BuildOptions,
    Workspace,
    require_free_space,
    run_build,
)
from ..download import ChecksumMismatchError, DownloadError, fetch, hash_file
from . import catalogue, export
from .live import LiveStore
from .settings import MANIFEST_NAME

Logger = Callable[[str], None]
DEFAULT_MAX_LOSS = 0.02
MAX_SOURCE_LOSS = 0.20
MIN_SOURCE_FOODS = 100
INDEX_NAME = "DATABASE.json"
INDEX_FORMAT = 1
REPORT_NAME = "rebuild.json"
LISTED_IDENTIFIERS = 20
# Files that travel with the database, next to it.
COMPANIONS = (MANIFEST_NAME, IDENTIFIERS_NAME)


class Refused(RuntimeError):
    """A new database loses too much of the one being served; nothing was replaced.

    Attributes:
        report: The comparison that led to the refusal.
    """

    def __init__(self, report: dict) -> None:
        """Remember the comparison."""
        super().__init__("; ".join(report["reasons"]))
        self.report = report


def move_into_place(source: Path, dest: Path) -> None:
    """Put a file at its final place in one step.

    On the same disk the file is renamed over the old one. Across disks it
    is first copied next to its destination, flushed, then renamed: whoever
    opens the destination sees the old file or the new one, never a part.

    Args:
        source: The new file.
        dest: Where it must be.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(source, dest)
        return
    except OSError:
        pass
    partial = dest.with_name(dest.name + ".part")
    with source.open("rb") as reader, partial.open("wb") as writer:
        shutil.copyfileobj(reader, writer, 8 * 1024 * 1024)
        writer.flush()
        os.fsync(writer.fileno())
    os.replace(partial, dest)
    source.unlink()


def write_identifier_list(database: Path, out: Path) -> int:
    """Write the identifier list of a main database that came without one.

    Args:
        database: Path of a `food.sqlite`.
        out: Where to write the list (`.gz`), as the build writes it.

    Returns:
        The number of identifiers written.
    """
    connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
    written = 0
    try:
        cursor = connection.execute("SELECT id FROM foods ORDER BY id")
        with out.open("wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as handle:
            while rows := cursor.fetchmany(200_000):
                handle.write(("\n".join(row[0] for row in rows) + "\n").encode("utf-8"))
                written += len(rows)
    finally:
        connection.close()
    return written


def _source_counts(database: Path) -> dict[str, int]:
    """Read the number of foods per source of a main database.

    Args:
        database: Path of a `food.sqlite`.

    Returns:
        The number of foods by source identifier.
    """
    connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
    try:
        return dict(connection.execute("SELECT id, foods FROM sources"))
    finally:
        connection.close()


def compare(old: Path, new: Path, new_identifiers: Path, scratch: Path, max_loss: float) -> dict:
    """Compare a new main database with the one being served.

    Args:
        old: The database being served.
        new: The database that would replace it.
        new_identifiers: The identifier list of the new one.
        scratch: Directory for the identifier list of the old one, when it
            has to be written.
        max_loss: Largest share of the foods, or of the identifiers, that
            may disappear.

    Returns:
        The figures of both, the identifiers added and removed (counted,
        with a few listed), and the reasons to refuse the new one, if any.
    """
    old_identifiers = old.with_name(IDENTIFIERS_NAME)
    if not old_identifiers.exists():
        old_identifiers = scratch / "identifiers-of-the-served-database.txt.gz"
        write_identifier_list(old, old_identifiers)
    identifiers = sqlite_export.compare_identifiers(old_identifiers, new_identifiers)
    before, after = _source_counts(old), _source_counts(new)
    foods_before, foods_after = sum(before.values()), sum(after.values())
    reasons = []
    if foods_before and foods_after < foods_before * (1 - max_loss):
        reasons.append(
            f"{foods_before - foods_after:,} foods fewer than the {foods_before:,} being"
            f" served: more than {max_loss:.1%}"
        )
    removed = len(identifiers["removed"])
    if identifiers["old"] and removed > identifiers["old"] * max_loss:
        reasons.append(
            f"{removed:,} of the {identifiers['old']:,} identifiers being served are gone:"
            f" more than {max_loss:.1%}"
        )
    for source, count in sorted(before.items()):
        if count >= MIN_SOURCE_FOODS and after.get(source, 0) < count * (1 - MAX_SOURCE_LOSS):
            reasons.append(
                f"source {source} goes from {count:,} foods to {after.get(source, 0):,}:"
                f" more than {MAX_SOURCE_LOSS:.0%} fewer"
            )
    return {
        "foods": {"served": foods_before, "new": foods_after},
        "by_source": {
            source: {"served": before.get(source, 0), "new": after.get(source, 0)}
            for source in sorted(set(before) | set(after))
        },
        "identifiers": {
            "served": identifiers["old"],
            "new": identifiers["new"],
            "added": len(identifiers["added"]),
            "removed": removed,
            "first_added": identifiers["added"][:LISTED_IDENTIFIERS],
            "first_removed": identifiers["removed"][:LISTED_IDENTIFIERS],
        },
        "max_loss": max_loss,
        "reasons": reasons,
    }


def swap_in(new_dir: Path, database: Path) -> None:
    """Replace the database being served by a new one, with its companions.

    Args:
        new_dir: Directory holding the new `food.sqlite` and, when they
            exist, its manifest and its identifier list.
        database: Path of the database being served.
    """
    for name in COMPANIONS:
        if (new_dir / name).exists():
            move_into_place(new_dir / name, database.with_name(name))
    move_into_place(new_dir / FOOD_DATABASE_NAME, database)


def rebuild(
    workspace: Workspace,
    options: BuildOptions,
    database: Path,
    live_path: Path | None,
    max_loss: float = DEFAULT_MAX_LOSS,
    log: Logger = print,
) -> dict:
    """Rebuild the main database and swap it in when it holds up.

    Args:
        workspace: The directories of the repository.
        options: Where the build takes its inputs from.
        database: Path of the database being served.
        live_path: Path of the live database, exported first; None to build
            with the layer as it is in the repository.
        max_loss: Largest share of the foods, or of the identifiers, that
            may disappear.
        log: Function receiving progress lines.

    Returns:
        The report of the rebuild: the export, the comparison, the timings.

    Raises:
        Refused: When the new database loses too much; the one being served
            is untouched.
    """
    report: dict = {"started_at": datetime.now(UTC).isoformat(timespec="seconds")}
    if live_path is not None and live_path.exists():
        report["export"] = export.export_layer(live_path, workspace.kratheos)
        log(
            f"Exported the live database to {workspace.kratheos.name}:"
            f" {report['export']['products']} products, {report['export']['versions']} versions,"
            f" {report['export']['votes']} votes, {report['export']['translations']} names"
        )
    staging = workspace.work / "rebuild"
    shutil.rmtree(staging, ignore_errors=True)
    staging_workspace = dataclasses.replace(workspace, dist=staging / "dist")
    build_manifest = run_build(staging_workspace, options, log)
    new_dir = staging_workspace.dist
    new_database = new_dir / FOOD_DATABASE_NAME
    catalogue.read_generation(new_database, catalogue.file_signature(new_database)).retire()
    report["foods"] = build_manifest["statistics"]["foods"]["foods"]
    report["kratheos"] = build_manifest["kratheos"]["statistics"]
    if database.exists():
        log("Comparing with the database being served")
        comparison = compare(database, new_database, new_dir / IDENTIFIERS_NAME, staging, max_loss)
        report["comparison"] = comparison
        log(
            f"  foods: {comparison['foods']['served']:,} -> {comparison['foods']['new']:,};"
            f" identifiers added: {comparison['identifiers']['added']:,},"
            f" removed: {comparison['identifiers']['removed']:,}"
        )
        if comparison["reasons"]:
            report["swapped"] = False
            (staging / REPORT_NAME).write_text(
                json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
            raise Refused(comparison)
    require_free_space(database.parent, options.min_free_gb)
    for path in sorted(new_dir.iterdir()):
        if path.name not in (FOOD_DATABASE_NAME, *COMPANIONS):
            move_into_place(path, workspace.dist / path.name)
    swap_in(new_dir, database)
    shutil.rmtree(staging, ignore_errors=True)
    report["swapped"] = True
    report["finished_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    database.with_name(REPORT_NAME).write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    log(f"Swapped in {database}: {report['foods']:,} foods")
    return report


def database_files(
    database: Path, layer: Path | None, out_dir: Path, log: Logger = print
) -> list[dict]:
    """Lay out in a directory what to publish so that others can install the database.

    Args:
        database: Path of the `food.sqlite` to publish; its manifest and its
            identifier list are taken next to it when they are there.
        layer: The `kratheos.sqlite` it was built with, published with it.
        out_dir: Directory receiving the files.
        log: Function receiving progress lines.

    Returns:
        One entry per file to publish: name, size and SHA-256.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    generation = catalogue.read_generation(database, catalogue.file_signature(database))
    generation.retire()
    sha256, _ = hash_file(database)
    size = database.stat().st_size
    index: dict = {
        "format": INDEX_FORMAT,
        "file": FOOD_DATABASE_NAME,
        "size": size,
        "sha256": sha256,
        "layout_version": int(generation.meta["layout_version"]),
        "foods": generation.foods,
        "built_at": generation.built_at,
        "companions": {},
    }
    published = []
    if size > mirror.MAX_ASSET_BYTES:
        log(f"  splitting {FOOD_DATABASE_NAME} into parts a release accepts")
        index["parts"] = mirror.split_file(database, out_dir)
        published += index["parts"]
    else:
        shutil.copyfile(database, out_dir / FOOD_DATABASE_NAME)
        published.append({"asset": FOOD_DATABASE_NAME, "size": size, "sha256": sha256})
    companions = {name: database.with_name(name) for name in COMPANIONS}
    if layer is not None:
        companions[kratheos.FILE_NAME] = layer
    for name, path in companions.items():
        if not path.exists():
            continue
        shutil.copyfile(path, out_dir / name)
        entry = {"size": path.stat().st_size, "sha256": hash_file(path)[0]}
        index["companions"][name] = entry
        published.append({"asset": name, **entry})
    text = json.dumps(index, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    (out_dir / INDEX_NAME).write_text(text, encoding="utf-8")
    index_entry = {
        "asset": INDEX_NAME,
        "size": (out_dir / INDEX_NAME).stat().st_size,
        "sha256": hash_file(out_dir / INDEX_NAME)[0],
    }
    return [index_entry, *published]


def _fetch_checked(url: str, dest: Path, entry: dict, min_free_gb: float, log: Logger) -> None:
    """Download a published file and verify its checksum.

    Args:
        url: Address of the file.
        dest: Where to store it.
        entry: Its size and SHA-256, from the index.
        min_free_gb: Free disk space that must remain, in GiB.
        log: Function receiving progress lines.

    Raises:
        DownloadError: When the file cannot be obtained or is not the one
            the index describes.
    """

    def check(size: int) -> None:
        """Refuse a download that would leave too little free space."""
        require_free_space(dest.parent, min_free_gb, size)

    fetch(url, dest, expected_sha256=entry["sha256"], on_size=check, log=log)
    if dest.stat().st_size != entry["size"]:
        raise ChecksumMismatchError(f"{dest.name}: size differs from the index")


def install_database(
    base_url: str,
    database: Path,
    live_path: Path | None = None,
    key_secret: bytes = b"",
    max_loss: float = DEFAULT_MAX_LOSS,
    force: bool = False,
    min_free_gb: float = 5.0,
    log: Logger = print,
) -> dict:
    """Download a ready-made main database, verify it and swap it in.

    Args:
        base_url: Address under which `database_files` was published.
        database: Path of the database being served.
        live_path: Path of the live database: the layer the database was
            built with is taken over by it, for what it lacks.
        key_secret: Secret of the contributor keys, needed to open the live
            database.
        max_loss: Largest share of the foods being served that may disappear.
        force: Install even when the comparison says too much is lost.
        min_free_gb: Free disk space that must remain, in GiB.
        log: Function receiving progress lines.

    Returns:
        What was installed, and the comparison with what was being served.

    Raises:
        DownloadError: When a file cannot be obtained or fails its checksum.
        Refused: When the database loses too much and `force` is not set.
    """
    incoming = database.with_name("incoming")
    incoming.mkdir(parents=True, exist_ok=True)
    fetch(mirror.asset_url(base_url, INDEX_NAME), incoming / INDEX_NAME, force=True, log=log)
    try:
        index = json.loads((incoming / INDEX_NAME).read_text(encoding="utf-8"))
        wanted = (index["format"], index["file"], index["sha256"], index["size"])
    except (ValueError, KeyError, TypeError) as error:
        raise DownloadError(f"{base_url}: {INDEX_NAME} cannot be read") from error
    if wanted[0] != INDEX_FORMAT or wanted[1] != FOOD_DATABASE_NAME:
        raise DownloadError(f"{base_url}: {INDEX_NAME} is of a format this version does not read")
    new_database = incoming / FOOD_DATABASE_NAME
    if database.exists() and hash_file(database)[0] == index["sha256"]:
        shutil.rmtree(incoming, ignore_errors=True)
        log(f"{database} is already this database")
        return {"installed": False, "sha256": index["sha256"]}
    parts = index.get("parts") or []
    if parts:
        paths = []
        for part in parts:
            path = incoming / part["asset"]
            _fetch_checked(mirror.asset_url(base_url, part["asset"]), path, part, min_free_gb, log)
            paths.append(path)
        log(f"  joining {len(paths)} parts")
        require_free_space(incoming, min_free_gb, index["size"])
        mirror.join_parts(paths, new_database, index["sha256"])
        for path in paths:
            path.unlink()
    else:
        _fetch_checked(
            mirror.asset_url(base_url, FOOD_DATABASE_NAME), new_database, index, min_free_gb, log
        )
    for name, entry in (index.get("companions") or {}).items():
        if name in (*COMPANIONS, kratheos.FILE_NAME):
            _fetch_checked(
                mirror.asset_url(base_url, name), incoming / name, entry, min_free_gb, log
            )
    try:
        generation = catalogue.read_generation(new_database, catalogue.file_signature(new_database))
    except catalogue.DatabaseRejected as error:
        raise DownloadError(str(error)) from error
    generation.retire()
    report: dict = {"installed": True, "sha256": index["sha256"], "foods": generation.foods}
    if database.exists():
        new_identifiers = incoming / IDENTIFIERS_NAME
        if not new_identifiers.exists():
            write_identifier_list(new_database, new_identifiers)
        report["comparison"] = compare(database, new_database, new_identifiers, incoming, max_loss)
        if report["comparison"]["reasons"] and not force:
            raise Refused(report["comparison"])
    layer_file = incoming / kratheos.FILE_NAME
    if layer_file.exists():
        move_into_place(layer_file, database.with_name(kratheos.FILE_NAME))
        if live_path is not None:
            store = LiveStore(live_path, key_secret)
            try:
                report["layer"] = store.import_layer(
                    kratheos.read_layer(database.with_name(kratheos.FILE_NAME))
                )
            finally:
                store.close()
    swap_in(incoming, database)
    shutil.rmtree(incoming, ignore_errors=True)
    log(f"Swapped in {database}: {generation.foods:,} foods, built {generation.built_at}")
    return report
