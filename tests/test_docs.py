"""Tests that the documentation and the licence files follow the code.

The README is a contract for the consumers of the database: every column,
flag and table it should describe must be named in it, and the credits of
every enabled source must be in ATTRIBUTION.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from conftest import REPOSITORY

from kratheos_food_data import cli, kratheos, schema, sqlite_export, versions
from kratheos_food_data.config import Source
from kratheos_food_data.service import api, foods, settings

README = (REPOSITORY / "README.md").read_text(encoding="utf-8")
API = (REPOSITORY / "docs" / "api.md").read_text(encoding="utf-8")


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
    assert layer.rows == 0
    assert not (layer.products or layer.versions or layer.votes or layer.translations)
    forbidden = ("user", "account", "handle", "email", "author", "device", "person", "contributor")
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


def test_api_reference_holds_the_contract_and_follows_the_code() -> None:
    """Every endpoint, every error code and every field is in docs/api.md."""
    assert "## Contract between the food service and the Kratheos backend (version 2)" in API
    assert api.API_VERSION == 2
    for endpoint in (
        "GET /v1/foods/search?q=<text>",
        "GET /v1/foods/{id}?lang=fr&country=FR",
        "GET /v1/foods/{id}/versions?country=FR",
        "GET /v1/barcodes/{code}",
        "GET /v1/meta",
        "GET /v1/health",
        "POST /v1/products",
        "POST /v1/foods/{id}/versions",
        "POST /v1/foods/{id}/votes",
        "POST /v1/foods/{id}/translations",
        "POST /v1/foods/{id}/reports",
        "GET /v1/moderation/reports?state=open",
        "POST /v1/moderation/foods/{id}/hide",
        "POST /v1/moderation/reports/{report_id}/close",
        "GET /v1/moderation/foods?state=open",
        "POST /v1/moderation/foods/{id}/reports/close",
        "GET /v1/moderation/foods/{id}/contributors",
        "POST /v1/moderation/contributors/lookup",
        "POST /v1/moderation/contributors/block",
        "POST /v1/moderation/contributors/unblock",
    ):
        assert endpoint in API, endpoint
    for code, status in api.ERROR_CODES.items():
        assert f"| {status} | `{code}` |" in API, code
    for field in ("version_id", "version_origin", "votes", "versions_count", "name_origin"):
        assert f"`{field}`" in API, field
    assert f"`{versions.FLAG_VERSIONS_DISAGREE}`" in API
    assert f"`{versions.FLAG_VERSIONS_DISAGREE}`" in README
    assert f"`{foods.FLAG_IN_STORE_BARCODE}`" in API
    assert f"`{foods.FLAG_IN_STORE_BARCODE}`" in README
    assert "`service/ranking.py`" in API and "ranking.py" in README
    for flag in (*schema.INFO_FLAGS, *schema.QUALITY_FLAGS):
        assert f"`{flag}`" in API, flag
    assert f"at most {api.MAX_USER_VERSIONS} versions proposed by users" in API


def test_readme_documents_the_service_its_settings_and_its_commands() -> None:
    """The settings read from the environment and the commands are in the README."""
    source = (REPOSITORY / "src" / "kratheos_food_data" / "service" / "settings.py").read_text(
        encoding="utf-8"
    )
    variables = set(re.findall(r"FOOD_DATA_[A-Z_]+", source))
    assert {"FOOD_DATA_TOKEN", "FOOD_DATA_DIR", "FOOD_DATA_KEY_SECRET"} <= variables
    for variable in variables:
        assert f"`{variable}`" in README, variable
    assert f"`{settings.DEFAULT_PORT}`" in README and f"`{settings.DEFAULT_HOST}`" in README
    commands = cli._parser()._subparsers._group_actions[0].choices
    for command in ("serve", "export-layer", "import-layer", "rebuild", "database-files"):
        assert command in commands and f"food-data {command}" in README, command
    assert "food-data install-database --url" in README
    for name in (settings.LIVE_NAME, settings.KEY_NAME, settings.MANIFEST_NAME):
        assert f"`{name}`" in README, name


def test_requirements_file_pins_what_the_lock_file_pins() -> None:
    """The package and the image install exactly the locked dependencies."""
    import tomllib

    with (REPOSITORY / "uv.lock").open("rb") as handle:
        locked = {
            package["name"]: package["version"] for package in tomllib.load(handle)["package"]
        }
    pinned = {}
    for line in (REPOSITORY / "requirements.txt").read_text(encoding="utf-8").splitlines():
        if line and not line.startswith(("#", " ")):
            name, _, version = line.split(";")[0].rstrip(" \\").partition("==")
            pinned[name] = version.strip()
    assert set(pinned) == {"duckdb", "openpyxl", "et-xmlfile"}  # what the code runs on
    for name, version in pinned.items():
        assert locked[name] == version, name
    assert "--hash=sha256:" in (REPOSITORY / "requirements.txt").read_text(encoding="utf-8")
