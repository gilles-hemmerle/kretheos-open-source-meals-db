"""Writes the main database, `food.sqlite`.

Layout of the file:

* `foods`: one row per food, with the columns of the common schema. `id` is
  the public identifier, the only one to keep outside the file: for a food
  with a barcode it is `gtin:<barcode>`, for any other food the identifier of
  its source record. `record_id` is the source record behind the row.
* `names`: every name of every food, one row per language, with its origin:
  `source` when the source of the record gives it, `kratheos` when Kratheos
  added it (`kratheos_origin` then says `machine`, `human` or `correction`).
* `foods_fts`: full-text index (FTS5) over the names of a food in every
  language it has, and its brand. Diacritics are ignored and prefixes of two
  and three letters are indexed, for search-as-you-type.
* `languages`: the language codes found in `names`.
* `food_countries`: one row per food and country its values apply to, keyed
  by country first: listing the foods of a country, or telling whether a food
  is of the person's country, is one index lookup. `countries` counts them.
* `display_names`: view applying the rule for the name to show (the person's
  language, else English, else the main name of the record).
* `source_values`: the foods a Kratheos correction changed, as their source
  gives them.
* `sources`: the sources, with their version, licence and attribution.
* `meta`: version of the layout, licence, helper queries.

`foods.pk`, `names.food_pk` and the row ids of `foods_fts` are the same
number. It only links the tables inside the file and changes from one build
to the next: nothing outside the file may keep it.

The file holds no build date, so two builds of the same inputs give the same
bytes.
"""

from __future__ import annotations

import gzip
import sqlite3
from pathlib import Path

import duckdb

from . import __version__
from .names import DISPLAY_NAME_SQL, DISPLAY_NAMES_VIEW_SQL

LAYOUT_VERSION = 1
BATCH_ROWS = 50_000
FTS_TOKENIZER = "unicode61 remove_diacritics 2"
FTS_PREFIXES = "2 3"

# Column of `foods` -> (SQLite declaration, DuckDB expression giving it).
FOOD_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("id", "TEXT NOT NULL", "id"),
    ("record_id", "TEXT NOT NULL", "record_id"),
    ("source", "TEXT NOT NULL", "source"),
    ("source_dataset", "TEXT", "source_dataset"),
    ("source_record_id", "TEXT NOT NULL", "source_record_id"),
    ("kind", "TEXT NOT NULL", "kind"),
    ("barcode", "TEXT", "barcode"),
    ("name", "TEXT NOT NULL", "name"),
    ("name_lang", "TEXT NOT NULL", "name_lang"),
    ("brand", "TEXT", "brand"),
    ("category", "TEXT", "category"),
    ("basis", "TEXT NOT NULL", "basis"),
    ("energy_kcal", "REAL", "energy_kcal"),
    ("protein_g", "REAL", "protein_g"),
    ("carbohydrate_g", "REAL", "carbohydrate_g"),
    ("carbohydrate_total_g", "REAL", "carbohydrate_total_g"),
    ("sugars_g", "REAL", "sugars_g"),
    ("fat_g", "REAL", "fat_g"),
    ("saturated_fat_g", "REAL", "saturated_fat_g"),
    ("fiber_g", "REAL", "fiber_g"),
    ("salt_g", "REAL", "salt_g"),
    ("alcohol_g", "REAL", "alcohol_g"),
    ("serving_size", "REAL", "serving_size"),
    ("serving_unit", "TEXT", "serving_unit"),
    ("serving_text", "TEXT", "serving_text"),
    ("is_drink", "INTEGER", "CAST(is_drink AS INTEGER)"),
    ("countries", "TEXT", "CAST(to_json(countries) AS VARCHAR)"),
    ("popularity", "INTEGER", "popularity"),
    ("confirmations", "INTEGER", "confirmations"),
    ("has_macros", "INTEGER NOT NULL", "CAST(has_macros AS INTEGER)"),
    ("plausible", "INTEGER NOT NULL", "CAST(plausible AS INTEGER)"),
    ("flags", "TEXT NOT NULL", "CAST(to_json(flags) AS VARCHAR)"),
    ("licence", "TEXT NOT NULL", "licence"),
    ("source_url", "TEXT", "source_url"),
    ("updated_at", "TEXT", "CAST(updated_at AS VARCHAR)"),
    ("source_count", "INTEGER", "source_count"),
    ("sources", "TEXT", "CAST(to_json(sources) AS VARCHAR)"),
    ("other_records", "TEXT", "CAST(to_json(other_records) AS VARCHAR)"),
    ("sources_agree", "INTEGER", "CAST(sources_agree AS INTEGER)"),
)

# Parameters: :query (an FTS5 expression) and :country (ISO 3166-1 alpha-2).
# Two steps keep it fast while a person types: the index ranks the matches and
# keeps the best 200, which are then ordered with those of the person's
# country first. Start at three letters: a shorter prefix matches hundreds of
# thousands of foods and takes about a second.
SEARCH_SQL = """
SELECT f.id, f.name, f.brand, f.countries
FROM (
    SELECT rowid, rank FROM foods_fts WHERE foods_fts MATCH :query ORDER BY rank LIMIT 200
) AS m
JOIN foods AS f ON f.pk = m.rowid
ORDER BY
    EXISTS (
        SELECT 1 FROM food_countries AS c WHERE c.country = :country AND c.food_pk = f.pk
    ) DESC,
    m.rank,
    f.popularity DESC
LIMIT 20
""".strip()


def _schema_sql() -> str:
    """Build the statements creating the tables of the main database.

    Returns:
        The SQL script.
    """
    food_columns = ",\n    ".join(f"{name} {kind}" for name, kind, _ in FOOD_COLUMNS)
    value_columns = ",\n    ".join(
        f"{name} {kind}" for name, kind, _ in FOOD_COLUMNS if name != "confirmations"
    )
    return f"""
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
) WITHOUT ROWID;

CREATE TABLE sources (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    publisher TEXT NOT NULL,
    homepage TEXT,
    version TEXT,
    released TEXT,
    licence TEXT NOT NULL,
    licence_name TEXT,
    licence_url TEXT,
    attribution TEXT NOT NULL,
    foods INTEGER NOT NULL
) WITHOUT ROWID;

CREATE TABLE foods (
    pk INTEGER PRIMARY KEY,
    {food_columns}
);

CREATE TABLE names (
    food_pk INTEGER NOT NULL REFERENCES foods (pk),
    lang TEXT NOT NULL,
    name TEXT NOT NULL,
    origin TEXT NOT NULL CHECK (origin IN ('source', 'kratheos')),
    kratheos_origin TEXT CHECK (kratheos_origin IN ('machine', 'human', 'correction')),
    PRIMARY KEY (food_pk, lang)
) WITHOUT ROWID;

CREATE TABLE food_countries (
    country TEXT NOT NULL,
    food_pk INTEGER NOT NULL REFERENCES foods (pk),
    PRIMARY KEY (country, food_pk)
) WITHOUT ROWID;

CREATE TABLE countries (
    country TEXT PRIMARY KEY,
    foods INTEGER NOT NULL
) WITHOUT ROWID;

CREATE TABLE languages (
    lang TEXT PRIMARY KEY,
    names INTEGER NOT NULL
) WITHOUT ROWID;

CREATE TABLE source_values (
    {value_columns},
    PRIMARY KEY (id)
) WITHOUT ROWID;

CREATE VIRTUAL TABLE foods_fts USING fts5(
    names, brand, content = '', tokenize = '{FTS_TOKENIZER}', prefix = '{FTS_PREFIXES}'
);

{DISPLAY_NAMES_VIEW_SQL};
"""


def _copy(
    con: duckdb.DuckDBPyConnection, database: sqlite3.Connection, select: str, insert: str
) -> int:
    """Copy the rows of a DuckDB query into a SQLite table, in batches.

    Args:
        con: DuckDB connection.
        database: SQLite connection.
        select: DuckDB query; its columns must be plain numbers and text.
        insert: SQLite INSERT statement with one placeholder per column.

    Returns:
        The number of rows copied.
    """
    cursor = con.execute(select)
    copied = 0
    while rows := cursor.fetchmany(BATCH_ROWS):
        database.executemany(insert, rows)
        copied += len(rows)
    return copied


def write_food_database(
    con: duckdb.DuckDBPyConnection, path: Path, sources: list[dict], licence: dict
) -> int:
    """Write `food.sqlite` from the tables created by `aggregate.build_foods`.

    Args:
        con: DuckDB connection holding `foods`, `food_names`, `source_values`.
        path: Where to write the database.
        sources: One description per source present in the foods: id, name,
            publisher, homepage, version, released, licence, licence_name,
            licence_url, attribution.
        licence: Licence of the database: id, name, url, notice.

    Returns:
        The number of foods written.

    Raises:
        RuntimeError: When the SQLite library of this Python lacks FTS5.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".part")
    partial.unlink(missing_ok=True)
    database = sqlite3.connect(partial, isolation_level=None)
    try:
        database.execute("PRAGMA journal_mode = OFF")
        database.execute("PRAGMA synchronous = OFF")
        database.execute("PRAGMA locking_mode = EXCLUSIVE")
        database.execute("PRAGMA cache_size = -262144")
        try:
            database.executescript(_schema_sql())
        except sqlite3.OperationalError as error:
            if "fts5" in str(error).lower():
                raise RuntimeError("this Python's SQLite was built without FTS5") from error
            raise
        database.execute("BEGIN")
        names = [name for name, _, _ in FOOD_COLUMNS]
        expressions = ", ".join(f"{expression} AS {name}" for name, _, expression in FOOD_COLUMNS)
        placeholders = ", ".join("?" for _ in range(len(names) + 1))
        foods = _copy(
            con,
            database,
            f"SELECT food_pk, {expressions} FROM foods ORDER BY food_pk",
            f"INSERT INTO foods (pk, {', '.join(names)}) VALUES ({placeholders})",
        )
        _copy(
            con,
            database,
            "SELECT food_pk, lang, name, origin, kratheos_origin FROM food_names"
            " ORDER BY food_pk, lang",
            "INSERT INTO names (food_pk, lang, name, origin, kratheos_origin)"
            " VALUES (?, ?, ?, ?, ?)",
        )
        _copy(
            con,
            database,
            """
            SELECT f.food_pk, n.names, f.brand
            FROM foods AS f
            JOIN (
                SELECT food_pk, string_agg(DISTINCT name, ' | ' ORDER BY name) AS names
                FROM food_names GROUP BY food_pk
            ) AS n ON n.food_pk = f.food_pk
            ORDER BY f.food_pk
            """,
            "INSERT INTO foods_fts (rowid, names, brand) VALUES (?, ?, ?)",
        )
        value_names = [name for name in names if name != "confirmations"]
        value_expressions = ", ".join(
            f"{expression} AS {name}"
            for name, _, expression in FOOD_COLUMNS
            if name != "confirmations"
        )
        _copy(
            con,
            database,
            f"SELECT {value_expressions} FROM source_values ORDER BY id",
            f"INSERT INTO source_values ({', '.join(value_names)})"
            f" VALUES ({', '.join('?' for _ in value_names)})",
        )
        _copy(
            con,
            database,
            "SELECT country, food_pk FROM (SELECT unnest(countries) AS country, food_pk FROM foods)"
            " ORDER BY country, food_pk",
            "INSERT INTO food_countries (country, food_pk) VALUES (?, ?)",
        )
        database.execute(
            "INSERT INTO countries (country, foods)"
            " SELECT country, count(*) FROM food_countries GROUP BY country"
        )
        database.execute(
            "INSERT INTO languages (lang, names) SELECT lang, count(*) FROM names GROUP BY lang"
        )
        counts = dict(database.execute("SELECT source, count(*) FROM foods GROUP BY source"))
        database.executemany(
            "INSERT INTO sources VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    source["id"],
                    source["name"],
                    source["publisher"],
                    source["homepage"],
                    source["version"],
                    source["released"],
                    source["licence"],
                    source["licence_name"],
                    source["licence_url"],
                    source["attribution"],
                    counts.get(source["id"], 0),
                )
                for source in sorted(sources, key=lambda item: item["id"])
            ],
        )
        database.executemany(
            "INSERT INTO meta (key, value) VALUES (?, ?)",
            [
                ("layout_version", str(LAYOUT_VERSION)),
                ("tool_version", __version__),
                ("licence", licence["id"]),
                ("licence_name", licence["name"]),
                ("licence_url", licence["url"]),
                ("licence_notice", licence["notice"]),
                ("display_name_query", DISPLAY_NAME_SQL),
                ("search_query", SEARCH_SQL),
            ],
        )
        database.execute("CREATE UNIQUE INDEX foods_id ON foods (id)")
        database.execute(
            "CREATE UNIQUE INDEX foods_barcode ON foods (barcode) WHERE barcode IS NOT NULL"
        )
        database.execute("COMMIT")
        database.execute("INSERT INTO foods_fts (foods_fts) VALUES ('optimize')")
        database.execute("ANALYZE")
        database.execute("PRAGMA journal_mode = DELETE")
    finally:
        database.close()
    partial.replace(path)
    return foods


def write_identifiers(con: duckdb.DuckDBPyConnection, path: Path) -> int:
    """Write the sorted list of the public identifiers, one per line, gzipped.

    The archive carries no timestamp, so the same identifiers give the same
    file.

    Args:
        con: DuckDB connection holding the `foods` table.
        path: Where to write the list (`.gz`).

    Returns:
        The number of identifiers written.
    """
    cursor = con.execute("SELECT id FROM foods ORDER BY id")
    written = 0
    with path.open("wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as out:
        while rows := cursor.fetchmany(BATCH_ROWS * 4):
            out.write(("\n".join(row[0] for row in rows) + "\n").encode("utf-8"))
            written += len(rows)
    return written


def read_identifiers(path: Path) -> set[str]:
    """Read a list of identifiers written by `write_identifiers`.

    Args:
        path: The list, gzipped or plain text.

    Returns:
        The identifiers.
    """
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as handle:
        return {line.rstrip("\n") for line in handle if line.strip()}


def compare_identifiers(old: Path, new: Path) -> dict:
    """Compare two lists of identifiers.

    Args:
        old: List of the earlier build.
        new: List of the later build.

    Returns:
        The counts of both lists and the sorted identifiers that were added
        and removed.
    """
    before = read_identifiers(old)
    after = read_identifiers(new)
    return {
        "old": len(before),
        "new": len(after),
        "added": sorted(after - before),
        "removed": sorted(before - after),
    }
