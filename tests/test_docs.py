"""Tests that the documentation and the licence files follow the code.

The README is a contract for the consumers of the database: every column,
flag and table it should describe must be named in it, and the credits of
every enabled source must be in ATTRIBUTION.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from conftest import REPOSITORY

from kratheos_food_data import kratheos, schema, sqlite_export
from kratheos_food_data.config import Source

README = (REPOSITORY / "README.md").read_text(encoding="utf-8")


def test_readme_names_every_column_and_flag() -> None:
    """Every output column and every flag is documented."""
    for column in (*schema.OUTPUT_COLUMNS, *schema.BARCODE_EXTRA_COLUMNS):
        assert f"`{column.name}`" in README, column.name
    for flag in (*schema.INFO_FLAGS, *schema.QUALITY_FLAGS):
        assert f"`{flag}`" in README, flag


def test_readme_names_every_table(tmp_path: Path) -> None:
    """Every table of food.sqlite and of kratheos.sqlite is documented."""
    database = sqlite3.connect(":memory:")
    database.executescript(sqlite_export._schema_sql())
    tables = [
        name
        for (name,) in database.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            " AND name NOT LIKE 'foods_fts_%' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    assert "foods" in tables and "display_names" in tables
    for table in (*tables, *kratheos.TABLES):
        assert f"`{table}`" in README, table
    for columns in kratheos.TABLES.values():
        for name, _ in columns:
            assert f"`{name}`" in README, name


def test_committed_kratheos_layer_is_empty_and_has_the_documented_layout() -> None:
    """The committed layer has the layout of the code and holds no row."""
    layer = kratheos.read_layer(REPOSITORY / kratheos.FILE_NAME)
    assert layer.meta == {"schema_version": str(kratheos.SCHEMA_VERSION), "exported_at": ""}
    assert not (layer.products or layer.corrections or layer.confirmations or layer.translations)
    forbidden = ("user", "account", "handle", "email", "author", "device", "person")
    for table, columns in kratheos.TABLES.items():
        for name, _ in columns:
            assert not any(word in name for word in forbidden), (table, name)


def test_credits_of_every_enabled_source_are_given(declared_sources: dict[str, Source]) -> None:
    """ATTRIBUTION reproduces the credit of every enabled source."""
    attribution = (REPOSITORY / "ATTRIBUTION").read_text(encoding="utf-8")
    licence = (REPOSITORY / "LICENSE-DATA").read_text(encoding="utf-8")
    for source in declared_sources.values():
        if source.enabled:
            assert source.attribution in attribution, source.id
            assert f"\n  {source.id} " in licence, source.id
            assert f"`{source.id}`" in README, source.id
    assert kratheos.KRATHEOS_SOURCE.attribution in attribution
    assert "Open Database License" in licence


def test_stored_small_sources_match_their_pinned_checksums(
    declared_sources: dict[str, Source],
) -> None:
    """The committed source files are the ones pinned in the source list."""
    from kratheos_food_data.download import hash_file, read_record

    for source in declared_sources.values():
        if not (source.enabled and source.mirror == "git"):
            continue
        for entry in source.files:
            path = REPOSITORY / "sources" / source.id / entry.name
            assert path.exists(), path
            sha256, md5 = hash_file(path)
            record = read_record(path)
            assert record is not None and record.sha256 == sha256, path
            assert record.url == entry.url
            assert entry.sha256 in (None, sha256)
            assert entry.md5 in (None, md5)
