"""Builds `manifest.json`, the description of one build of the database.

The manifest is what makes a published database traceable: when it was built,
from which version of which source file (with the checksum of the file, and
of its parts when it is attached to a release in several pieces), whether the
file came from the publisher or from the stored copy, under which licence
each source is used and how to attribute it, what the Kratheos layer added,
how many records came out, and the checksum of every output file.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from . import __version__, aggregate, mirror, normalize, schema
from .aggregate import OutputFile
from .config import Source
from .download import hash_file
from .kratheos import Layer

if TYPE_CHECKING:
    from .build import BuildOptions

SCHEMA_VERSION = 1
DATABASE_LICENCE = {
    "id": "ODbL-1.0",
    "name": "Open Database License 1.0",
    "url": "https://opendatacommons.org/licenses/odbl/1-0/",
    "contents": "Database Contents License 1.0 for the records taken from Open Food Facts; "
    "the licence of its source for every other record (column `licence`).",
    "notice": "Contains data from Open Food Facts and other sources, made available under the"
    " Open Database License (ODbL) 1.0. See the table `sources` (or the files LICENSE-DATA"
    " and ATTRIBUTION of the repository) for the licence and attribution of each source.",
}


def source_description(source: Source, built: dict) -> dict:
    """Describe a source with the version that was built.

    Args:
        source: The source, as declared.
        built: What the build learned: `version` and `released`, if any.

    Returns:
        Identity, version, licence and attribution of the source.
    """
    return {
        "id": source.id,
        "name": source.name,
        "publisher": source.publisher,
        "homepage": source.homepage,
        "version": built.get("version") or source.version,
        "released": built.get("released") or source.released,
        "priority": source.priority,
        "licence": source.licence,
        "licence_name": source.licence_name,
        "licence_url": source.licence_url,
        "attribution": source.attribution,
    }


def source_entry(source: Source, files: list[dict], built: dict, records: dict) -> dict:
    """Describe one built source.

    Args:
        source: The source, as declared.
        files: Its files, as described by `mirror.describe`.
        built: Version, date, adapter counts and normalisation counts.
        records: Statistics of its records in the aggregated sources.

    Returns:
        The manifest entry of the source.
    """
    origins = {entry["origin"] for entry in files}
    if mirror.ORIGIN_OVERRIDE in origins:
        origin = mirror.ORIGIN_OVERRIDE
    elif mirror.ORIGIN_MIRROR in origins or source.directory:
        # A source read from a directory of the repository has no upstream.
        origin = mirror.ORIGIN_MIRROR
    else:
        origin = mirror.ORIGIN_UPSTREAM
    return {
        **source_description(source, built),
        "terms_url": source.terms_url,
        "terms_checked": source.terms_checked,
        "share_alike": source.share_alike,
        "stored_in": "sources/" if source.mirror == "git" else "mirror/",
        "origin": origin,
        "files": files,
        "adapter": built["adapter"],
        "normalization": built["normalization"],
        "records": records,
    }


def kratheos_entry(path: Path, layer: Layer, statistics: dict) -> dict:
    """Describe the Kratheos layer a build applied.

    Args:
        path: Path of `kratheos.sqlite`.
        layer: Its content.
        statistics: What the layer added and changed (`aggregate.build_foods`).

    Returns:
        The manifest entry of the layer.
    """
    present = path.exists()
    return {
        "file": path.name,
        "present": present,
        "size": path.stat().st_size if present else None,
        "sha256": hash_file(path)[0] if present else None,
        "schema_version": layer.meta.get("schema_version"),
        "exported_at": layer.meta.get("exported_at"),
        "licence": DATABASE_LICENCE["id"],
        "statistics": statistics,
    }


def output_entry(output: OutputFile) -> dict:
    """Describe one output file, with its size and checksum.

    Args:
        output: The file written by the build.

    Returns:
        The manifest entry of the file.
    """
    sha256, _ = hash_file(output.path)
    return {
        "file": output.path.name,
        "table": output.table,
        "format": output.format,
        "description": output.description,
        "records": output.rows,
        "size": output.path.stat().st_size,
        "sha256": sha256,
    }


def quality_rules() -> dict:
    """Return the thresholds a build applied, for the record.

    Returns:
        The constants of the normalisation and aggregation rules.
    """
    return {
        "kj_per_kcal": normalize.KJ_PER_KCAL,
        "salt_per_sodium": normalize.SALT_PER_SODIUM,
        "ethanol_g_per_ml": normalize.ETHANOL_G_PER_ML,
        "nutrient_max_g": normalize.NUTRIENT_MAX_G,
        "energy_max_kcal": normalize.ENERGY_MAX_KCAL,
        "part_excess_tolerance_g": normalize.PART_EXCESS_TOLERANCE_G,
        "macro_sum_max_g": normalize.MACRO_SUM_MAX_G,
        "atwater_relative_tolerance": normalize.ATWATER_RELATIVE_TOLERANCE,
        "atwater_absolute_tolerance_kcal": normalize.ATWATER_ABSOLUTE_TOLERANCE_KCAL,
        "agreement_energy_absolute_kcal": aggregate.AGREE_ENERGY_ABSOLUTE_KCAL,
        "agreement_energy_relative": aggregate.AGREE_ENERGY_RELATIVE,
        "agreement_macro_absolute_g": aggregate.AGREE_MACRO_ABSOLUTE_G,
        "agreement_macro_relative": aggregate.AGREE_MACRO_RELATIVE,
        "user_version_votes_default": aggregate.DEFAULT_USER_VERSION_VOTES,
    }


def build_manifest(
    source_entries: list[dict],
    not_built: list[Source],
    outputs: list[OutputFile],
    statistics: dict,
    layer: dict,
    options: BuildOptions,
    timings: dict[str, float],
) -> dict:
    """Assemble the manifest of a build.

    Args:
        source_entries: Entries of the built sources (`source_entry`).
        not_built: Sources that are listed but were not built.
        outputs: Files written by the build.
        statistics: Statistics of the aggregated sources and of the foods.
        layer: Entry of the Kratheos layer (`kratheos_entry`).
        options: How the build obtained its inputs.
        timings: Duration of each stage, in seconds.

    Returns:
        The manifest, ready to be serialised.
    """
    return {
        "name": "kratheos-food-data",
        "schema_version": SCHEMA_VERSION,
        "tool_version": __version__,
        "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "inputs": {
            "mode": "mirror" if options.from_mirror else "upstream",
            "mirror_url": options.mirror_url,
            "overrides": {
                f"{source_id}/{name}": str(path)
                for (source_id, name), path in sorted(options.overrides.items())
            },
        },
        "licence": DATABASE_LICENCE,
        "sources": source_entries,
        "sources_not_built": [
            {
                "id": source.id,
                "name": source.name,
                "publisher": source.publisher,
                "enabled": source.enabled,
                "reason": source.reason or "left out of this build (--only)",
            }
            for source in not_built
        ],
        "kratheos": layer,
        "outputs": [output_entry(output) for output in outputs],
        "columns": [
            {"name": column.name, "type": column.type, "description": column.description}
            for column in schema.OUTPUT_COLUMNS
        ],
        "barcode_columns": [
            {"name": column.name, "type": column.type, "description": column.description}
            for column in schema.BARCODE_EXTRA_COLUMNS
        ],
        "flags": {"informational": schema.INFO_FLAGS, "quality": schema.QUALITY_FLAGS},
        "rules": quality_rules(),
        "statistics": statistics,
        "timings_seconds": {stage: round(seconds, 1) for stage, seconds in timings.items()},
    }


def write_manifest(manifest: dict, path: Path) -> None:
    """Write the manifest as indented JSON.

    Args:
        manifest: The manifest built by `build_manifest`.
        path: Destination file.
    """
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
