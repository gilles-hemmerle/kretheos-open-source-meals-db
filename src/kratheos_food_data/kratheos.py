"""The Kratheos layer: what Kratheos and its users add on top of the sources.

The sources are stored untouched. Everything that comes from Kratheos lives
in one small SQLite file, `kratheos.sqlite`, committed at the root of the
repository, and is applied by the build after the sources are aggregated:

* `products`: foods added in Kratheos, with or without a barcode (a
  restaurant item has none). They become records of the source `kratheos`;
* `versions`: other values proposed by users for a food of any source. A
  version never replaces anything: it stands beside the records of the
  sources, and people vote for the one they find right;
* `votes`: how many people voted for a version, whether it is a record of a
  source (`off:...`), a product added in Kratheos (`kratheos:<id>`) or a
  version proposed by a user (`kratheos:v:<id>`);
* `translations`: names Kratheos adds to a food in a language its source
  does not give. A translation never replaces a name given by the source;
* `meta`: version of this layout and date of the last export from Kratheos.

Never who: the file holds no account, no handle and no identifier of a
person, only counts. The layout below is closed: the build refuses a file
with a table or a column that is not listed here, so nothing of the kind can
slip in.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import duckdb

from . import schema
from .config import MIRROR_GIT, Source

SCHEMA_VERSION = 2
SOURCE_ID = "kratheos"
FILE_NAME = "kratheos.sqlite"
TRANSLATION_ORIGINS = ("machine", "human")
# Identifier of a version proposed by a user: `kratheos:v:<id>`. The record of
# a product added in Kratheos is `kratheos:<id>`, so a product id never starts
# with `v:`.
VERSION_PREFIX = f"{SOURCE_ID}:v:"
LICENCE = "ODbL-1.0"

NUTRIENT_COLUMNS = (
    ("energy_kcal", "REAL"),
    ("protein_g", "REAL"),
    ("carbohydrate_g", "REAL"),
    ("sugars_g", "REAL"),
    ("fat_g", "REAL"),
    ("saturated_fat_g", "REAL"),
    ("fiber_g", "REAL"),
    ("salt_g", "REAL"),
    ("alcohol_g", "REAL"),
)
SERVING_COLUMNS = (
    ("serving_size", "REAL"),
    ("serving_unit", "TEXT"),
    ("serving_text", "TEXT"),
)
# What a product carries besides its names: identity, values and one portion.
VALUE_COLUMNS = (
    ("brand", "TEXT"),
    ("category", "TEXT"),
    ("basis", "TEXT"),
    *NUTRIENT_COLUMNS,
    *SERVING_COLUMNS,
    ("is_drink", "INTEGER"),
)
# What a version carries: values and one portion. A version has no name.
VERSION_VALUE_COLUMNS = (("basis", "TEXT"), *NUTRIENT_COLUMNS, *SERVING_COLUMNS)

# Table -> ordered (column, declared type). This is the whole layout.
TABLES: dict[str, tuple[tuple[str, str], ...]] = {
    "meta": (("key", "TEXT"), ("value", "TEXT")),
    "translations": (
        ("record_id", "TEXT"),
        ("lang", "TEXT"),
        ("name", "TEXT"),
        ("origin", "TEXT"),
        ("updated_at", "TEXT"),
    ),
    "products": (
        ("id", "TEXT"),
        ("kind", "TEXT"),
        ("barcode", "TEXT"),
        ("name", "TEXT"),
        ("name_lang", "TEXT"),
        ("names", "TEXT"),
        *VALUE_COLUMNS,
        ("countries", "TEXT"),
        ("updated_at", "TEXT"),
    ),
    "versions": (
        ("version_id", "TEXT"),
        ("food_id", "TEXT"),
        *VERSION_VALUE_COLUMNS,
        ("countries", "TEXT"),
        ("updated_at", "TEXT"),
    ),
    "votes": (
        ("version_id", "TEXT"),
        ("food_id", "TEXT"),
        ("votes", "INTEGER"),
        ("updated_at", "TEXT"),
    ),
}

_VALUE_DDL = ",\n    ".join(f"{name} {kind}" for name, kind in VALUE_COLUMNS)
_VERSION_VALUE_DDL = ",\n    ".join(
    f"{name} {kind}" + (" NOT NULL" if name in ("basis", "energy_kcal") else "")
    for name, kind in VERSION_VALUE_COLUMNS
)

DDL = f"""
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
) WITHOUT ROWID;

-- A name Kratheos adds to a food, in a language its source does not give.
CREATE TABLE translations (
    record_id TEXT NOT NULL,   -- identifier of a food, e.g. ciqual:13000
    lang TEXT NOT NULL,        -- language code of the name
    name TEXT NOT NULL,
    origin TEXT NOT NULL CHECK (origin IN ('machine', 'human')),
    updated_at TEXT NOT NULL,  -- ISO 8601 date
    PRIMARY KEY (record_id, lang)
) WITHOUT ROWID;

-- A food added in Kratheos. Its record id is kratheos:<id>. Values are per
-- 100 g or 100 ml (basis); carbohydrate_g is available carbohydrate, fibre
-- excluded, as on European labels.
CREATE TABLE products (
    id TEXT PRIMARY KEY CHECK (id NOT LIKE 'v:%'),
    kind TEXT NOT NULL DEFAULT 'product' CHECK (kind IN ('generic', 'product')),
    barcode TEXT,              -- optional; a restaurant item has none
    name TEXT NOT NULL,
    name_lang TEXT NOT NULL,   -- language code of name
    names TEXT,                -- JSON object: other names by language code
    {_VALUE_DDL},
    countries TEXT,            -- JSON array of ISO 3166-1 alpha-2 codes, given at creation
    updated_at TEXT NOT NULL,
    CHECK (basis IN ('100g', '100ml'))
) WITHOUT ROWID;

-- Other values proposed by a user for a food. Nothing is replaced: the
-- version stands beside the records of the sources. Never who proposed it.
CREATE TABLE versions (
    version_id TEXT PRIMARY KEY CHECK (version_id LIKE '{VERSION_PREFIX}%'),
    food_id TEXT NOT NULL,     -- identifier of the food in food.sqlite, e.g. gtin:3017620422003
    {_VERSION_VALUE_DDL},
    countries TEXT,            -- JSON array of ISO 3166-1 alpha-2 codes the values apply to
    updated_at TEXT NOT NULL,
    CHECK (basis IN ('100g', '100ml'))
) WITHOUT ROWID;

-- How many people voted for a version: a record of a source (off:...), a
-- product added in Kratheos (kratheos:<id>) or a version proposed by a user
-- (kratheos:v:<id>). Never who.
CREATE TABLE votes (
    version_id TEXT PRIMARY KEY,
    food_id TEXT NOT NULL,     -- the food the version belongs to
    votes INTEGER NOT NULL CHECK (votes >= 0),
    updated_at TEXT NOT NULL
) WITHOUT ROWID;
"""

KRATHEOS_SOURCE = Source(
    id=SOURCE_ID,
    name="Kratheos",
    publisher="Kratheos and its users",
    homepage="",
    adapter="",
    version="",
    released="",
    kinds=(schema.KIND_GENERIC, schema.KIND_PRODUCT),
    priority=1000,
    licence=LICENCE,
    licence_name="Open Database License 1.0",
    licence_url="https://opendatacommons.org/licenses/odbl/1-0/",
    terms_url="",
    terms_quote="",
    terms_checked="",
    attribution="Contains data added by the users of Kratheos, available under the"
    " Open Database License (ODbL) 1.0.",
    share_alike=True,
    enabled=True,
    reason="",
    mirror=MIRROR_GIT,
    country="",
    files=(),
)


class LayerError(ValueError):
    """`kratheos.sqlite` does not have the documented layout."""


@dataclass
class Layer:
    """The content of `kratheos.sqlite`.

    Attributes:
        meta: The key and value pairs of the `meta` table.
        products: Rows of `products`, as dictionaries.
        versions: Rows of `versions`, as dictionaries.
        votes: Rows of `votes`, as dictionaries.
        translations: Rows of `translations`, as dictionaries.
    """

    meta: dict[str, str] = field(default_factory=dict)
    products: list[dict] = field(default_factory=list)
    versions: list[dict] = field(default_factory=list)
    votes: list[dict] = field(default_factory=list)
    translations: list[dict] = field(default_factory=list)

    @property
    def rows(self) -> int:
        """Return the number of rows the layer holds, every table together."""
        return len(self.products) + len(self.versions) + len(self.votes) + len(self.translations)


# Table -> column its rows are read in the order of, so that a layer always
# reads the same way.
ROW_ORDER = {
    "products": "id",
    "versions": "version_id",
    "votes": "version_id",
    "translations": "record_id, lang",
}


def create_database(path: Path) -> None:
    """Create an empty `kratheos.sqlite` with the documented layout.

    Args:
        path: Where to create the file.

    Raises:
        FileExistsError: When a file is already there; it is never replaced.
    """
    if path.exists():
        raise FileExistsError(f"{path} already exists")
    connection = sqlite3.connect(path)
    try:
        connection.executescript(DDL)
        connection.executemany(
            "INSERT INTO meta (key, value) VALUES (?, ?)",
            [("schema_version", str(SCHEMA_VERSION)), ("exported_at", "")],
        )
        connection.commit()
    finally:
        connection.close()


def check_layout(connection: sqlite3.Connection) -> None:
    """Verify that a database has exactly the documented tables and columns.

    Args:
        connection: A connection to a `kratheos.sqlite` file.

    Raises:
        LayerError: When a table or a column is missing or unexpected, or
            when the layout version is not the one this code reads.
    """
    tables = {
        name
        for (name,) in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    if tables != set(TABLES):
        unexpected = sorted(tables - set(TABLES))
        missing = sorted(set(TABLES) - tables)
        raise LayerError(f"unexpected tables {unexpected}, missing tables {missing}")
    for table, columns in TABLES.items():
        found = [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
        expected = [name for name, _ in columns]
        if found != expected:
            raise LayerError(f"table {table} has columns {found}, expected {expected}")
    version = connection.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    if version is None or version[0] != str(SCHEMA_VERSION):
        raise LayerError(f"schema_version is {version and version[0]}, expected {SCHEMA_VERSION}")


def read_layer(path: Path) -> Layer:
    """Read `kratheos.sqlite`, after checking its layout.

    The file is opened read-only: the build never writes to it.

    Args:
        path: Path of the file.

    Returns:
        The content of the layer; an empty layer when the file is missing.

    Raises:
        LayerError: When the file does not have the documented layout, or a
            row breaks the rules on identifiers.
    """
    if not path.exists():
        return Layer()
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        check_layout(connection)
        connection.row_factory = sqlite3.Row
        layer = Layer(meta=dict(connection.execute("SELECT key, value FROM meta").fetchall()))
        for table, order in ROW_ORDER.items():
            rows = connection.execute(f"SELECT * FROM {table} ORDER BY {order}").fetchall()
            getattr(layer, table).extend(dict(row) for row in rows)
    finally:
        connection.close()
    for product in layer.products:
        if str(product["id"]).startswith("v:"):
            raise LayerError(f"product {product['id']}: an id may not start with 'v:'")
    for version in layer.versions:
        if not str(version["version_id"]).startswith(VERSION_PREFIX):
            raise LayerError(
                f"version {version['version_id']}: the id must start with {VERSION_PREFIX}"
            )
    return layer


def read_names(text: str | None, what: str) -> dict[str, str]:
    """Read a JSON object of language code to name.

    Args:
        text: The JSON text, or None.
        what: What is being read, for the error message.

    Returns:
        The names, without empty ones.

    Raises:
        LayerError: When the text is not a JSON object of strings.
    """
    if not text:
        return {}
    try:
        value = json.loads(text)
    except ValueError as error:
        raise LayerError(f"{what}: names is not valid JSON") from error
    if not isinstance(value, dict) or not all(isinstance(name, str) for name in value.values()):
        raise LayerError(f"{what}: names must be a JSON object of language code to name")
    return {lang: " ".join(name.split()) for lang, name in value.items() if name.strip()}


def read_countries(text: str | None, what: str) -> list[str] | None:
    """Read a JSON array of country codes.

    Args:
        text: The JSON text, or None.
        what: What is being read, for the error message.

    Returns:
        The codes in upper case, or None when there is none.

    Raises:
        LayerError: When the text is not a JSON array of strings.
    """
    if not text:
        return None
    try:
        value = json.loads(text)
    except ValueError as error:
        raise LayerError(f"{what}: countries is not valid JSON") from error
    if not isinstance(value, list) or not all(isinstance(code, str) for code in value):
        raise LayerError(f"{what}: countries must be a JSON array of country codes")
    return [code.upper() for code in value] or None


def product_staging_rows(layer: Layer) -> list[dict]:
    """Turn the Kratheos products into staging rows, like an adapter does.

    Args:
        layer: The Kratheos layer.

    Returns:
        One staging row per product.

    Raises:
        LayerError: When a product has malformed names or countries.
    """
    rows = []
    for product in layer.products:
        what = f"product {product['id']}"
        names = read_names(product["names"], what)
        names[product["name_lang"]] = " ".join(product["name"].split())
        is_drink = product["is_drink"]
        rows.append(
            {
                "source_dataset": None,
                "source_record_id": product["id"],
                "kind": product["kind"],
                "barcode_raw": product["barcode"],
                "names": names,
                "main_lang": product["name_lang"],
                "brand": product["brand"],
                "category": product["category"],
                "basis": product["basis"],
                "basis_assumed": False,
                "energy_kcal": product["energy_kcal"],
                "protein_g": product["protein_g"],
                "carbohydrate_g": product["carbohydrate_g"],
                "carbohydrate_includes_fiber": False,
                "sugars_g": product["sugars_g"],
                "fat_g": product["fat_g"],
                "saturated_fat_g": product["saturated_fat_g"],
                "fiber_g": product["fiber_g"],
                "salt_g": product["salt_g"],
                "alcohol_g": product["alcohol_g"],
                "serving_size": product["serving_size"],
                "serving_unit": product["serving_unit"],
                "serving_text": product["serving_text"],
                "is_drink": None if is_drink is None else bool(is_drink),
                "countries": read_countries(product["countries"], what),
                "source_url": None,
                "updated_at": product["updated_at"],
            }
        )
    return rows


def load_tables(con: duckdb.DuckDBPyConnection, layer: Layer) -> None:
    """Load versions, votes and translations into DuckDB tables.

    Creates `kratheos_versions`, `kratheos_votes` and `kratheos_translations`,
    empty when the layer is.

    Args:
        con: DuckDB connection of the build.
        layer: The Kratheos layer.

    Raises:
        LayerError: When a version has malformed countries.
    """
    duck_types = {"TEXT": "VARCHAR", "REAL": "DOUBLE", "INTEGER": "BIGINT"}
    value_names = [name for name, _ in VERSION_VALUE_COLUMNS]
    value_ddl = ", ".join(f"{name} {duck_types[kind]}" for name, kind in VERSION_VALUE_COLUMNS)
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE kratheos_versions (
            version_id VARCHAR, food_id VARCHAR, {value_ddl}, countries VARCHAR[],
            updated_at DATE
        )
        """
    )
    placeholders = ", ".join("?" for _ in range(len(value_names) + 2))
    for version in layer.versions:
        countries = read_countries(version["countries"], f"version {version['version_id']}")
        con.execute(
            f"""
            INSERT INTO kratheos_versions
            SELECT version_id, food_id, {", ".join(value_names)},
                   CAST(? AS VARCHAR[]), CAST(? AS DATE)
            FROM (VALUES ({placeholders})) AS v(version_id, food_id, {", ".join(value_names)})
            """,
            [
                countries,
                str(version["updated_at"])[:10],
                version["version_id"],
                version["food_id"],
                *(version[name] for name in value_names),
            ],
        )
    con.execute("CREATE OR REPLACE TEMP TABLE kratheos_votes (version_id VARCHAR, votes BIGINT)")
    votes = [(row["version_id"], row["votes"]) for row in layer.votes if row["votes"] > 0]
    if votes:
        con.executemany("INSERT INTO kratheos_votes VALUES (?, ?)", votes)
    con.execute(
        "CREATE OR REPLACE TEMP TABLE kratheos_translations"
        " (record_id VARCHAR, lang VARCHAR, name VARCHAR, origin VARCHAR)"
    )
    translations = [
        (row["record_id"], row["lang"], " ".join(row["name"].split()), row["origin"])
        for row in layer.translations
        if row["name"].strip()
    ]
    if translations:
        con.executemany("INSERT INTO kratheos_translations VALUES (?, ?, ?, ?)", translations)
