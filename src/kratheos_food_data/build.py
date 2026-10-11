"""The build pipeline: download, adapt, normalise, aggregate, describe.

`run_build` is the one entry point that rebuilds the whole database from
nothing:

1. every file of every enabled source is downloaded into `downloads/`
   (resumed and verified, not downloaded again when unchanged);
2. the adapter of each source writes a staging file in `work/staging/`;
3. `normalize.py` turns each staging file into records of the output schema;
4. `aggregate.py` writes the output files in `dist/`;
5. `manifest.py` writes `dist/manifest.json`.

Free disk space is checked before every large step, so that a build stops
with a clear message instead of filling the disk.
"""

from __future__ import annotations

import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import duckdb

from . import aggregate, barcode, manifest
from .adapters import AdapterContext, get_adapter
from .config import Source, enabled_sources, load_sources
from .download import FileRecord, fetch, probe, read_record
from .normalize import normalize_source

GIB = 1024**3
DEFAULT_MIN_FREE_GB = 5.0
Logger = Callable[[str], None]


class DiskSpaceError(RuntimeError):
    """Going on would leave less free disk space than the configured minimum."""


@dataclass(frozen=True)
class Workspace:
    """The directories a build reads and writes.

    Attributes:
        sources_file: Path of `sources.toml`.
        downloads: Where source files are downloaded.
        work: Scratch space: extracted archives, staging and temporary files.
        dist: Where the database is written.
    """

    sources_file: Path
    downloads: Path
    work: Path
    dist: Path

    @classmethod
    def at(cls, root: Path) -> Workspace:
        """Build the default workspace of a repository checkout.

        Args:
            root: Directory holding `sources.toml`.

        Returns:
            The workspace with `downloads/`, `work/` and `dist/` under root.
        """
        return cls(root / "sources.toml", root / "downloads", root / "work", root / "dist")


def require_free_space(path: Path, min_free_gb: float, needed_bytes: int = 0) -> None:
    """Fail when writing `needed_bytes` would leave too little free space.

    Args:
        path: A path on the disk to check (created if missing).
        min_free_gb: Free space that must remain, in GiB.
        needed_bytes: Bytes about to be written.

    Raises:
        DiskSpaceError: When the remaining free space would be too small.
    """
    path.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(path).free
    if free - needed_bytes < min_free_gb * GIB:
        raise DiskSpaceError(
            f"only {free / GIB:.1f} GiB free on the disk of {path}; writing"
            f" {needed_bytes / GIB:.1f} GiB more would leave less than the"
            f" {min_free_gb:.0f} GiB required (--min-free-gb)"
        )


def connect(work: Path) -> duckdb.DuckDBPyConnection:
    """Open the in-memory DuckDB database used by a build.

    Large intermediate results spill to `work/tmp` instead of exhausting the
    memory, and the barcode macros are registered.

    Args:
        work: Scratch directory of the build.

    Returns:
        The connection.
    """
    temp = work / "tmp"
    temp.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(config={"temp_directory": str(temp), "preserve_insertion_order": "false"})
    barcode.register_macros(con)
    return con


def download_source(
    source: Source,
    downloads: Path,
    *,
    offline: bool,
    force: bool,
    min_free_gb: float,
    log: Logger,
) -> list[FileRecord]:
    """Download the files of one source, one after the other.

    Args:
        source: The source.
        downloads: Root of the download directory.
        offline: Use what is already downloaded and never touch the network.
        force: Download again even when a file looks current.
        min_free_gb: Free disk space that must remain, in GiB.
        log: Function receiving progress lines.

    Returns:
        The record of each file, in declaration order.
    """
    records = []
    for entry in source.files:
        dest = downloads / source.id / entry.name
        if not offline and (force or read_record(dest) is None):
            size = probe(entry.url).size or 0
            partial = dest.with_name(dest.name + ".part")
            already = partial.stat().st_size if partial.exists() else 0
            require_free_space(downloads, min_free_gb, max(size - already, 0))
        record = fetch(
            entry.url,
            dest,
            expected_sha256=entry.sha256,
            expected_md5=entry.md5,
            offline=offline,
            force=force,
            log=log,
        )
        log(f"  {entry.name}: {record.status} ({record.size:,} bytes)")
        records.append(record)
    return records


def run_download(
    workspace: Workspace,
    *,
    only: list[str] | None = None,
    offline: bool = False,
    force: bool = False,
    min_free_gb: float = DEFAULT_MIN_FREE_GB,
    log: Logger = print,
) -> dict[str, list[FileRecord]]:
    """Download the files of every enabled source.

    Args:
        workspace: The directories of the build.
        only: Restrict to these source ids.
        offline: Use what is already downloaded and never touch the network.
        force: Download again even when a file looks current.
        min_free_gb: Free disk space that must remain, in GiB.
        log: Function receiving progress lines.

    Returns:
        The file records of each source, by source id.
    """
    sources = enabled_sources(load_sources(workspace.sources_file), only)
    files = {}
    for source in sources:
        log(f"Downloading {source.name}")
        files[source.id] = download_source(
            source,
            workspace.downloads,
            offline=offline,
            force=force,
            min_free_gb=min_free_gb,
            log=log,
        )
    return files


def run_build(
    workspace: Workspace,
    *,
    only: list[str] | None = None,
    offline: bool = False,
    force_download: bool = False,
    all_formats: bool = False,
    min_free_gb: float = DEFAULT_MIN_FREE_GB,
    log: Logger = print,
) -> dict:
    """Rebuild the whole database.

    Args:
        workspace: The directories of the build.
        only: Restrict the build to these source ids.
        offline: Use what is already downloaded and never touch the network.
        force_download: Download every file again.
        all_formats: Also write the product tables as CSV and JSON Lines.
        min_free_gb: Free disk space that must remain, in GiB.
        log: Function receiving progress lines.

    Returns:
        The manifest that was written to `dist/manifest.json`.
    """
    timings: dict[str, float] = {}
    started = time.monotonic()
    all_sources = load_sources(workspace.sources_file)
    sources = enabled_sources(all_sources, only)
    not_built = [source for source in all_sources if source not in sources]

    files = run_download(
        workspace,
        only=only,
        offline=offline,
        force=force_download,
        min_free_gb=min_free_gb,
        log=log,
    )
    timings["download"] = time.monotonic() - started

    con = connect(workspace.work)
    normalized: dict[str, Path] = {}
    built: dict[str, dict] = {}
    for source in sources:
        stage_started = time.monotonic()
        require_free_space(workspace.work, min_free_gb)
        log(f"Adapting {source.name}")
        staging = workspace.work / "staging" / f"{source.id}.parquet"
        context = AdapterContext(
            con=con,
            download_dir=workspace.downloads / source.id,
            work_dir=workspace.work / source.id,
            staging_path=staging,
            log=log,
        )
        result = get_adapter(source.adapter)(source, context)
        normalized[source.id] = workspace.work / "normalized" / f"{source.id}.parquet"
        counts = normalize_source(con, source, staging, normalized[source.id])
        log(f"  {counts['kept']:,} records kept, dropped: {counts['dropped'] or 'none'}")
        built[source.id] = {
            "version": result.version or source.version,
            "released": result.released or source.released,
            "adapter": result.stats,
            "normalization": counts,
        }
        timings[f"adapt_{source.id}"] = time.monotonic() - stage_started

    stage_started = time.monotonic()
    require_free_space(workspace.dist, min_free_gb)
    log("Aggregating")
    aggregate.create_views(con, sources, normalized)
    outputs = aggregate.write_outputs(con, workspace.dist, all_formats=all_formats)
    for output in outputs:
        log(f"  {output.path.name}: {output.rows:,} records")
    statistics = aggregate.overall_statistics(con)
    entries = [
        manifest.source_entry(
            source,
            files[source.id],
            built[source.id]["version"],
            built[source.id]["released"],
            built[source.id]["adapter"],
            built[source.id]["normalization"],
            aggregate.source_statistics(con, source.id),
        )
        for source in sources
    ]
    con.close()
    timings["aggregate"] = time.monotonic() - stage_started
    timings["total"] = time.monotonic() - started
    document = manifest.build_manifest(entries, not_built, outputs, statistics, timings)
    manifest.write_manifest(document, workspace.dist / "manifest.json")
    log(f"Done in {timings['total']:.0f} s: {workspace.dist / 'manifest.json'}")
    return document
