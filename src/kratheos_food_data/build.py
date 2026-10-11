"""The build pipeline: originals, adapters, aggregation, Kratheos layer, outputs.

`run_build` is the one entry point that rebuilds the whole database from
nothing:

1. every file of every enabled source is made available, untouched: taken
   from upstream (and stored in `sources/<id>/` or `mirror/`), or from the
   stored copy when upstream fails or when the build is told to use the
   mirror (`mirror.py`);
2. the adapter of each source reads its files and writes a staging file in
   `work/staging/`; `normalize.py` turns it into records of the output schema;
3. `aggregate.py` aggregates the sources and writes the Parquet, CSV and JSON
   Lines files, which hold the sources as they are;
4. the Kratheos layer (`kratheos.sqlite`) is applied on top and the main
   database, `dist/food.sqlite`, is written with the list of its identifiers;
5. `manifest.py` writes `dist/manifest.json`.

Free disk space is checked before every large step, so that a build stops
with a clear message instead of filling the disk.
"""

from __future__ import annotations

import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import duckdb

from . import aggregate, barcode, kratheos, manifest, mirror, sqlite_export
from .adapters import AdapterContext, get_adapter
from .adapters.base import write_staging_rows
from .config import Source, enabled_sources, load_sources
from .normalize import normalize_source

GIB = 1024**3
DEFAULT_MIN_FREE_GB = 5.0
FOOD_DATABASE_NAME = "food.sqlite"
IDENTIFIERS_NAME = "identifiers.txt.gz"
Logger = Callable[[str], None]


class DiskSpaceError(RuntimeError):
    """Going on would leave less free disk space than the configured minimum."""


@dataclass(frozen=True)
class Workspace:
    """The directories and files a build reads and writes.

    Attributes:
        sources_file: Path of `sources.toml`.
        sources_dir: Committed copies of the small sources.
        mirror: Copies of the large sources, attached to releases.
        work: Scratch space: extracted archives, staging and temporary files.
        dist: Where the database is written.
        kratheos: Path of `kratheos.sqlite`, the Kratheos layer.
    """

    sources_file: Path
    sources_dir: Path
    mirror: Path
    work: Path
    dist: Path
    kratheos: Path

    @classmethod
    def at(cls, root: Path) -> Workspace:
        """Build the default workspace of a repository checkout.

        Args:
            root: Directory holding `sources.toml`.

        Returns:
            The workspace with every directory under root.
        """
        return cls(
            sources_file=root / "sources.toml",
            sources_dir=root / "sources",
            mirror=root / "mirror",
            work=root / "work",
            dist=root / "dist",
            kratheos=root / kratheos.FILE_NAME,
        )

    @property
    def locations(self) -> mirror.Locations:
        """Return the directories source files are stored in."""
        return mirror.Locations(sources=self.sources_dir, mirror=self.mirror)


@dataclass(frozen=True)
class BuildOptions:
    """How a build obtains its inputs and what it writes.

    Attributes:
        only: Restrict the build to these source ids.
        from_mirror: Use the stored copies and never contact a publisher.
        mirror_url: Base address of a remote mirror, for missing copies.
        overrides: Paths used instead of some source files, for this run only.
        force_download: Download every file again from upstream.
        all_formats: Also write the product tables as CSV and JSON Lines.
        min_free_gb: Free disk space that must remain, in GiB.
    """

    only: list[str] | None = None
    from_mirror: bool = False
    mirror_url: str | None = None
    overrides: mirror.Overrides = field(default_factory=dict)
    force_download: bool = False
    all_formats: bool = False
    min_free_gb: float = DEFAULT_MIN_FREE_GB


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


def acquire_sources(
    workspace: Workspace, sources: list[Source], options: BuildOptions, log: Logger
) -> tuple[dict[str, list[mirror.Acquired]], dict]:
    """Make the files of the given sources available, one after the other.

    Args:
        workspace: The directories of the build.
        sources: The sources to obtain.
        options: Where to take the files from.
        log: Function receiving progress lines.

    Returns:
        The files of each source, by source id, and the mirror index.

    Raises:
        ValueError: When an override names a file no source declares.
    """
    declared = {(source.id, entry.name) for source in sources for entry in source.files}
    unknown = sorted(set(options.overrides) - declared)
    if unknown:
        raise ValueError(f"override of an unknown source file: {unknown[0][0]}/{unknown[0][1]}")
    locations = workspace.locations
    index = mirror.load_index(workspace.mirror)
    if options.mirror_url:
        index = mirror.fetch_index(options.mirror_url, workspace.mirror, log)
    acquired: dict[str, list[mirror.Acquired]] = {}
    for source in sources:
        log(f"Obtaining {source.name}")
        files = []
        for entry in source.files:
            target = mirror.stored_path(locations, source, entry).parent

            def check(size: int, target: Path = target) -> None:
                """Refuse a download that would leave too little free space."""
                require_free_space(target, options.min_free_gb, size)

            item = mirror.acquire(
                source,
                entry,
                locations,
                index=index,
                overrides=options.overrides,
                from_mirror=options.from_mirror,
                mirror_url=options.mirror_url,
                force=options.force_download,
                on_size=check,
                log=log,
            )
            log(f"  {entry.name}: {item.origin}, {item.detail} ({item.record.size:,} bytes)")
            files.append(item)
        acquired[source.id] = files
        mirror.update_index(workspace.mirror, index, source, files)
    if any(source.mirror == "release" for source in sources) and not options.mirror_url:
        mirror.save_index(workspace.mirror, index)
    return acquired, index


def run_download(workspace: Workspace, options: BuildOptions, log: Logger = print) -> dict:
    """Obtain the files of every enabled source, without building.

    Args:
        workspace: The directories of the build.
        options: Where to take the files from.
        log: Function receiving progress lines.

    Returns:
        The files of each source, by source id.
    """
    sources = enabled_sources(load_sources(workspace.sources_file), options.only)
    acquired, _ = acquire_sources(workspace, sources, options, log)
    return acquired


def run_build(workspace: Workspace, options: BuildOptions, log: Logger = print) -> dict:
    """Rebuild the whole database.

    Args:
        workspace: The directories of the build.
        options: Where to take the inputs from and what to write.
        log: Function receiving progress lines.

    Returns:
        The manifest that was written to `dist/manifest.json`.
    """
    timings: dict[str, float] = {}
    started = time.monotonic()
    all_sources = load_sources(workspace.sources_file)
    sources = enabled_sources(all_sources, options.only)
    not_built = [source for source in all_sources if source not in sources]
    layer = kratheos.read_layer(workspace.kratheos)

    acquired, index = acquire_sources(workspace, sources, options, log)
    timings["obtain"] = time.monotonic() - started

    con = connect(workspace.work)
    normalized: dict[str, Path] = {}
    built: dict[str, dict] = {}
    for source in sources:
        stage_started = time.monotonic()
        require_free_space(workspace.work, options.min_free_gb)
        log(f"Adapting {source.name}")
        staging = workspace.work / "staging" / f"{source.id}.parquet"
        context = AdapterContext(
            con=con,
            files={item.name: item.path for item in acquired[source.id]},
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
    require_free_space(workspace.dist, options.min_free_gb)
    log("Aggregating the sources")
    aggregate.create_source_views(con, sources, normalized)
    outputs = aggregate.write_source_outputs(con, workspace.dist, all_formats=options.all_formats)
    statistics = aggregate.overall_statistics(con)
    entries = [
        manifest.source_entry(
            source,
            [mirror.describe(item, index, source) for item in acquired[source.id]],
            built[source.id],
            aggregate.source_statistics(con, source.id),
        )
        for source in sources
    ]
    timings["aggregate"] = time.monotonic() - stage_started

    stage_started = time.monotonic()
    log("Applying the Kratheos layer")
    kratheos_records = None
    kratheos_counts = {"read": 0, "kept": 0, "dropped": {}}
    if layer.products:
        staging = workspace.work / "staging" / f"{kratheos.SOURCE_ID}.parquet"
        context = AdapterContext(
            con=con, files={}, work_dir=workspace.work, staging_path=staging, log=log
        )
        write_staging_rows(context, kratheos.product_staging_rows(layer))
        kratheos_records = workspace.work / "normalized" / f"{kratheos.SOURCE_ID}.parquet"
        kratheos_counts = normalize_source(con, kratheos.KRATHEOS_SOURCE, staging, kratheos_records)
    con.execute(
        "INSERT INTO source_order VALUES (?, ?)",
        [kratheos.SOURCE_ID, kratheos.KRATHEOS_SOURCE.priority],
    )
    kratheos.load_tables(con, layer)
    layer_statistics = aggregate.build_foods(con, kratheos_records)
    layer_statistics["products_dropped"] = kratheos_counts["dropped"]
    statistics["foods"] = aggregate.foods_statistics(con)
    timings["kratheos_layer"] = time.monotonic() - stage_started

    stage_started = time.monotonic()
    log(f"Writing {FOOD_DATABASE_NAME}")
    described = [manifest.source_description(source, built[source.id]) for source in sources]
    described.append(manifest.source_description(kratheos.KRATHEOS_SOURCE, {}))
    database = workspace.dist / FOOD_DATABASE_NAME
    foods = sqlite_export.write_food_database(con, database, described, manifest.DATABASE_LICENCE)
    outputs.append(
        aggregate.OutputFile(
            database,
            "foods",
            "sqlite",
            foods,
            "The main database: generic foods, one food per barcode and the products"
            " without a valid barcode, Kratheos layer applied, with a full-text index.",
        )
    )
    identifiers = workspace.dist / IDENTIFIERS_NAME
    count = sqlite_export.write_identifiers(con, identifiers)
    outputs.append(
        aggregate.OutputFile(
            identifiers,
            "identifiers",
            "text",
            count,
            "Sorted list of the identifiers of the foods of the main database.",
        )
    )
    con.close()
    timings["write_database"] = time.monotonic() - stage_started
    for output in outputs:
        log(f"  {output.path.name}: {output.rows:,} records")

    layer_entry = manifest.kratheos_entry(workspace.kratheos, layer, layer_statistics)
    timings["total"] = time.monotonic() - started
    document = manifest.build_manifest(
        entries, not_built, outputs, statistics, layer_entry, options, timings
    )
    manifest.write_manifest(document, workspace.dist / "manifest.json")
    log(f"Done in {timings['total']:.0f} s: {workspace.dist / 'manifest.json'}")
    return document
