"""Builds `manifest.json`, the description of one build of the database.

The manifest is what makes a published database traceable: when it was built,
from which version of which source file (with checksums), under which licence
each source is used and how to attribute it, how many records came out, and
the checksum of every output file.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from . import __version__, aggregate, normalize, schema
from .aggregate import OutputFile
from .config import Source
from .download import FileRecord, hash_file

SCHEMA_VERSION = 1
DATABASE_LICENCE = {
    "id": "ODbL-1.0",
    "name": "Open Database License 1.0",
    "url": "https://opendatacommons.org/licenses/odbl/1-0/",
    "contents": "Database Contents License 1.0 for the records taken from Open Food Facts; "
    "the licence of its source for every other record (column `licence`).",
    "notice": "See LICENSE-DATA.md and ATTRIBUTION in the repository.",
}


def source_entry(
    source: Source,
    files: list[FileRecord],
    version: str,
    released: str,
    adapter_stats: dict,
    normalize_stats: dict,
    records: dict,
) -> dict:
    """Describe one built source.

    Args:
        source: The source, as declared.
        files: The downloaded files it was built from.
        version: Version of the data that was built.
        released: Date of that version.
        adapter_stats: Counts reported by the adapter.
        normalize_stats: Counts of records read, kept and dropped.
        records: Statistics of the records in the aggregated database.

    Returns:
        The manifest entry of the source.
    """
    return {
        "id": source.id,
        "name": source.name,
        "publisher": source.publisher,
        "homepage": source.homepage,
        "version": version,
        "released": released,
        "licence": source.licence,
        "licence_name": source.licence_name,
        "licence_url": source.licence_url,
        "terms_url": source.terms_url,
        "terms_checked": source.terms_checked,
        "share_alike": source.share_alike,
        "attribution": source.attribution,
        "files": [
            {
                "name": record.name,
                "url": record.url,
                "size": record.size,
                "sha256": record.sha256,
                "md5": record.md5,
                "etag": record.etag,
                "last_modified": record.last_modified,
                "revision": record.revision,
                "downloaded_at": record.downloaded_at,
            }
            for record in files
        ],
        "adapter": adapter_stats,
        "normalization": normalize_stats,
        "records": records,
    }


def output_entry(output: OutputFile) -> dict:
    """Describe one output file, with its size and checksum.

    Args:
        output: The file written by the aggregation.

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
    }


def build_manifest(
    source_entries: list[dict],
    disabled: list[Source],
    outputs: list[OutputFile],
    statistics: dict,
    timings: dict[str, float],
) -> dict:
    """Assemble the manifest of a build.

    Args:
        source_entries: Entries of the built sources (`source_entry`).
        disabled: Sources that are listed but were not built.
        outputs: Files written by the aggregation.
        statistics: Overall statistics of the database.
        timings: Duration of each stage, in seconds.

    Returns:
        The manifest, ready to be serialised.
    """
    return {
        "name": "kratheos-food-data",
        "schema_version": SCHEMA_VERSION,
        "tool_version": __version__,
        "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
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
            for source in disabled
        ],
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
