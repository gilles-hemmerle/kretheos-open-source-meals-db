"""Aggregates the sources, then applies the Kratheos layer on top.

Step 1, the sources alone. Rules, as agreed for this database:

* generic foods are never merged across sources and nothing is averaged: each
  record stays a record of its source;
* every product record of every source is kept in `products`;
* `products_by_barcode` gives one preferred record per barcode, chosen by, in
  order: plausible values, complete macros (energy, protein, carbohydrate,
  fat), most recent update, then the fixed source order of `sources.toml`.
  The preferred record lists the other sources and records that know the
  barcode, and says whether the sources agree.

Two sources "agree" on a barcode when, between the best record of each, energy
differs by at most 10 kcal or 10 %, and protein, carbohydrate and fat each by
at most 2 g or 20 % (the larger of the two bounds each time). Records are only
compared when both have the four values on the same basis (100 g or 100 ml).

The Parquet, CSV and JSON Lines files are written from this step: they hold
the sources as they are, and nothing from Kratheos.

Step 2, the main database (`food.sqlite`). Its `foods` are the generic foods,
one row per barcode and the products that have no valid barcode, after the
Kratheos layer was applied.

A food can have several versions of its values: one per record of a source
that knows it (the records sharing its barcode), the products added in
Kratheos under that barcode, and the versions proposed by users. Nothing is
averaged and nothing is overwritten: every version is kept, with the number
of people who voted for it, and one of them is served.

* Who the food is (its names, brand, category, kind, barcode) comes from one
  record, chosen by the rule above among the records of the sources; a
  product added in Kratheos only gives them when no source knows the barcode.
* Whose values are served: the version with the most votes, except that a
  version coming from a user (a product added in Kratheos, a version
  proposed by a user) needs a minimum number of votes to be served instead
  of a record of a source. A tie, or no vote at all, falls back to the rule
  above: plausible values, complete macros, most recent, fixed source order.
  With an empty layer this is the preferred record, as before.
* Translations give a food the names it lacks. A translation never replaces
  a name the food already has; every name records where it comes from.

The row of a food in `foods` holds the values served when no country is
asked for; the table `versions` holds every version of the foods that have
more than one. A service that knows the country of a person applies the same
rule to the versions listing that country (`versions.choose_served`).

Identifiers. A food that has a barcode is identified by the barcode itself,
`gtin:<canonical barcode>`, whichever version is served; the record behind
the values keeps its own identifier (`record_id`). Every other food is
identified by its source record, `<source>:<id in the source>`. No identifier
depends on a counter, a row number or the order of processing.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import duckdb

from . import kratheos, schema
from .config import Source
from .normalize import VALUE_DECIMALS, has_macros_sql, quality_flag_cases, quality_list_sql
from .versions import (
    AGREE_ENERGY_ABSOLUTE_KCAL,
    AGREE_ENERGY_RELATIVE,
    AGREE_MACRO_ABSOLUTE_G,
    AGREE_MACRO_RELATIVE,
    COMPARED_VALUES,
    DEFAULT_USER_VERSION_VOTES,
    ORIGIN_SOURCE,
    ORIGIN_USER,
)

GENERIC_NAME = "generic_foods"
PRODUCTS_NAME = "products"
BY_BARCODE_NAME = "products_by_barcode"
PRODUCT_ID_PREFIX = "gtin:"
PREFERENCE_ORDER = (
    "p.plausible DESC, p.has_macros DESC, p.updated_at DESC NULLS LAST, o.priority ASC, p.id ASC"
)
# For the main database, who a food is comes from a record of a source when
# one knows the barcode: a product added in Kratheos comes after them.
SOURCES_FIRST_ORDER = f"(p.source = '{kratheos.SOURCE_ID}') ASC, {PREFERENCE_ORDER}"
NAME_ORIGIN_SOURCE = "source"
NAME_ORIGIN_KRATHEOS = "kratheos"
# Columns of a food that belong to the version served; the others say who the
# food is and never change with the votes.
VERSION_COLUMNS = (
    "source",
    "source_dataset",
    "source_record_id",
    "basis",
    "energy_kcal",
    "protein_g",
    "carbohydrate_g",
    "carbohydrate_total_g",
    "sugars_g",
    "fat_g",
    "saturated_fat_g",
    "fiber_g",
    "salt_g",
    "alcohol_g",
    "serving_size",
    "serving_unit",
    "serving_text",
    "countries",
    "has_macros",
    "plausible",
    "flags",
    "licence",
    "source_url",
    "updated_at",
)


@dataclass(frozen=True)
class OutputFile:
    """A file written to the output directory.

    Attributes:
        path: Where the file is.
        table: Logical table it holds: generic_foods, products, ...
        format: parquet, csv, jsonl, sqlite or text.
        rows: Number of records.
        description: What the file is.
    """

    path: Path
    table: str
    format: str
    rows: int
    description: str


DESCRIPTIONS = {
    GENERIC_NAME: "Generic foods of every source, one record per food and source.",
    PRODUCTS_NAME: "Branded products of every source, one record per product and source.",
    BY_BARCODE_NAME: "One preferred source record per barcode, with the other sources.",
}


def _sql_string(value: str | Path) -> str:
    """Quote a value as a SQL string literal.

    Args:
        value: Text or path to quote.

    Returns:
        The SQL literal.
    """
    return "'" + str(value).replace("'", "''") + "'"


def _agreement_sql(name: str, absolute: float, relative: float) -> str:
    """Build the SQL test that sources agree on one value.

    Args:
        name: Compared column.
        absolute: Largest accepted difference, in the unit of the column.
        relative: Largest accepted difference, as a share of the highest value.

    Returns:
        A boolean SQL expression over the `compared` CTE.
    """
    return f"(max_{name} - min_{name} <= greatest({absolute}, {relative} * max_{name}))"


def select_by_barcode(
    con: duckdb.DuckDBPyConnection, table: str, relation: str, order: str = PREFERENCE_ORDER
) -> None:
    """Create the table of the preferred record of each barcode.

    The table has the output columns of the preferred record, `product_id`
    (the identifier of the product itself) and the columns describing the
    other sources.

    Args:
        con: DuckDB connection; the table `source_order` must exist.
        table: Name of the table to create.
        relation: Table or view holding the candidate records.
        order: ORDER BY clause ranking the records of a barcode, over the
            aliases `p` (the record) and `o` (the order of its source).
    """
    spread = " AND ".join(
        _agreement_sql(name, AGREE_ENERGY_ABSOLUTE_KCAL, AGREE_ENERGY_RELATIVE)
        if name == "energy_kcal"
        else _agreement_sql(name, AGREE_MACRO_ABSOLUTE_G, AGREE_MACRO_RELATIVE)
        for name in COMPARED_VALUES
    )
    extremes = ",\n                ".join(
        f"max({name}) FILTER (WHERE has_macros) AS max_{name},\n"
        f"                min({name}) FILTER (WHERE has_macros) AS min_{name}"
        for name in COMPARED_VALUES
    )
    columns = ", ".join(f'r."{name}"' for name in schema.output_names())
    compared_columns = ", ".join(f"p.{name}" for name in COMPARED_VALUES)
    # Ranking only needs a few columns: it is done on a narrow table, and the
    # full records of the preferred rows are fetched afterwards by id.
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE ranked AS
        SELECT
            p.id,
            p.barcode,
            p.source,
            p.has_macros,
            p.basis,
            {compared_columns},
            row_number() OVER (PARTITION BY p.barcode ORDER BY {order}) AS position,
            row_number() OVER (PARTITION BY p.barcode, p.source ORDER BY {order})
                AS position_in_source
        FROM {relation} AS p
        JOIN source_order AS o ON o.source = p.source
        WHERE p.kind = '{schema.KIND_PRODUCT}' AND p.barcode IS NOT NULL
        """
    )
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE {table} AS
        WITH known AS (
            SELECT
                barcode,
                count(DISTINCT source) AS source_count,
                list(DISTINCT source ORDER BY source) AS sources,
                coalesce(list(id ORDER BY position) FILTER (WHERE position > 1), [])
                    AS other_records
            FROM ranked
            GROUP BY barcode
        ),
        compared AS (
            SELECT
                barcode,
                count(*) FILTER (WHERE has_macros) AS with_macros,
                count(DISTINCT basis) FILTER (WHERE has_macros) AS bases,
                {extremes}
            FROM ranked
            WHERE position_in_source = 1
            GROUP BY barcode
        )
        SELECT
            {columns},
            '{PRODUCT_ID_PREFIX}' || x.barcode AS product_id,
            CAST(k.source_count AS INTEGER) AS source_count,
            k.sources,
            k.other_records,
            CASE
                WHEN k.source_count < 2 OR c.with_macros < 2 OR c.bases > 1 THEN NULL
                ELSE {spread}
            END AS sources_agree
        FROM ranked AS x
        JOIN {relation} AS r ON r.id = x.id
        JOIN known AS k ON k.barcode = x.barcode
        JOIN compared AS c ON c.barcode = x.barcode
        WHERE x.position = 1
        """
    )
    con.execute("DROP TABLE ranked")


def create_source_views(
    con: duckdb.DuckDBPyConnection, sources: list[Source], normalized: dict[str, Path]
) -> None:
    """Create what the outputs of the sources are written from.

    Creates the view `source_records`, the table `source_order` and the table
    `by_barcode` (preferred source record of each barcode).

    Args:
        con: DuckDB connection.
        sources: The sources that were built.
        normalized: Normalised Parquet file of each source, by source id.
    """
    files = ", ".join(_sql_string(normalized[source.id]) for source in sources)
    con.execute(f"CREATE OR REPLACE VIEW source_records AS SELECT * FROM read_parquet([{files}])")
    con.execute("CREATE OR REPLACE TEMP TABLE source_order (source VARCHAR, priority INTEGER)")
    con.executemany(
        "INSERT INTO source_order VALUES (?, ?)",
        [(source.id, source.priority) for source in sources],
    )
    select_by_barcode(con, "by_barcode", "source_records")


def _export_select(relation: str, columns: list[str], order: str, target: str) -> str:
    """Build the SELECT used for the CSV and JSON Lines exports.

    In CSV, nested columns are written as JSON text so that the file stays
    readable by any CSV reader. In JSON Lines, lists are native arrays and the
    name map becomes an object keyed by language code.

    Args:
        relation: Table or sub-query to export.
        columns: Columns to write, in order.
        order: ORDER BY clause.
        target: `csv` or `jsonl`.

    Returns:
        The SQL SELECT.
    """
    nested = {"names", "countries", "flags", "sources", "other_records"}
    parts = []
    for name in columns:
        if target == "csv" and name in nested:
            parts.append(f'CAST(to_json("{name}") AS VARCHAR) AS "{name}"')
        elif target == "jsonl" and name == "names":
            parts.append(f'to_json("{name}") AS "{name}"')
        else:
            parts.append(f'"{name}"')
    return f"SELECT {', '.join(parts)} FROM {relation} ORDER BY {order}"


def write_source_outputs(
    con: duckdb.DuckDBPyConnection, out_dir: Path, all_formats: bool = False
) -> list[OutputFile]:
    """Write the files that hold the sources as they are.

    Parquet is always written. Generic foods, a small table, are also written
    as gzip-compressed CSV and JSON Lines; the product tables only are when
    `all_formats` is set, because they are large.

    Args:
        con: DuckDB connection on which `create_source_views` was called.
        out_dir: Output directory.
        all_formats: Also write the product tables as CSV and JSON Lines.

    Returns:
        The files written.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    base = schema.output_names()
    extra = [column.name for column in schema.BARCODE_EXTRA_COLUMNS]
    tables = [
        (
            GENERIC_NAME,
            f"(SELECT * FROM source_records WHERE kind = '{schema.KIND_GENERIC}')",
            base,
            "source, id",
            True,
        ),
        (
            PRODUCTS_NAME,
            f"(SELECT * FROM source_records WHERE kind = '{schema.KIND_PRODUCT}')",
            base,
            "barcode NULLS LAST, id",
            all_formats,
        ),
        (BY_BARCODE_NAME, "by_barcode", base + extra, "barcode", all_formats),
    ]
    written: list[OutputFile] = []
    for name, relation, columns, order, text_formats in tables:
        rows = con.execute(f"SELECT count(*) FROM {relation}").fetchone()[0]
        selected = ", ".join(f'"{column}"' for column in columns)
        parquet = out_dir / f"{name}.parquet"
        con.execute(
            f"""
            COPY (SELECT {selected} FROM {relation} ORDER BY {order})
            TO {_sql_string(parquet)} (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 100000)
            """
        )
        written.append(OutputFile(parquet, name, "parquet", rows, DESCRIPTIONS[name]))
        if not text_formats:
            continue
        csv_path = out_dir / f"{name}.csv.gz"
        csv_select = _export_select(relation, columns, order, "csv")
        con.execute(
            f"COPY ({csv_select}) TO {_sql_string(csv_path)} (FORMAT CSV, HEADER, COMPRESSION GZIP)"
        )
        written.append(OutputFile(csv_path, name, "csv", rows, DESCRIPTIONS[name]))
        jsonl_path = out_dir / f"{name}.jsonl.gz"
        jsonl_select = _export_select(relation, columns, order, "jsonl")
        con.execute(
            f"COPY ({jsonl_select}) TO {_sql_string(jsonl_path)} (FORMAT JSON, COMPRESSION GZIP)"
        )
        written.append(OutputFile(jsonl_path, name, "jsonl", rows, DESCRIPTIONS[name]))
    return written


def _user_versions_select() -> str:
    """Build the SELECT turning the versions proposed by users into versions.

    A version is kept when the food it names exists. Its values are rounded
    like those of a source and go through the same quality checks.

    Returns:
        A SELECT over `kratheos_versions` joined to `foods_base`, with the
        column `food_id`, `version_id` and the columns of `VERSION_COLUMNS`.
    """
    decimals = VALUE_DECIMALS
    nutrients = ",\n            ".join(
        f"round(v.{name}, {decimals}) AS {name}" for name, _ in kratheos.NUTRIENT_COLUMNS
    )
    quality = "[flag FOR flag IN [" + ", ".join(quality_flag_cases()) + "] IF flag IS NOT NULL]"
    prefix = len(kratheos.SOURCE_ID) + 2
    return f"""
    WITH staged AS (
        SELECT
            v.food_id,
            v.version_id,
            '{kratheos.SOURCE_ID}' AS source,
            CAST(NULL AS VARCHAR) AS source_dataset,
            substr(v.version_id, {prefix}) AS source_record_id,
            v.basis,
            {nutrients},
            CAST(NULL AS DOUBLE) AS carbohydrate_total_g,
            CAST(NULL AS DOUBLE) AS polyols_g,
            round(v.serving_size, {decimals}) AS serving_size,
            v.serving_unit,
            nullif(trim(v.serving_text), '') AS serving_text,
            CASE WHEN len(v.countries) > 0
                 THEN list_sort(list_distinct([upper(code) FOR code IN v.countries]))
            END AS countries,
            '{kratheos.LICENCE}' AS licence,
            CAST(NULL AS VARCHAR) AS source_url,
            v.updated_at
        FROM kratheos_versions AS v
        JOIN foods_base AS f ON f.id = v.food_id
    ),
    checked AS (
        SELECT *, {has_macros_sql()} AS has_macros FROM staged
    ),
    flagged AS (
        SELECT *, {quality} AS flags FROM checked
    )
    SELECT
        * EXCLUDE (polyols_g),
        NOT list_has_any(flags, {quality_list_sql()}) AS plausible
    FROM flagged
    """


def _food_versions_select(relation: str, user_version_votes: int) -> str:
    """Build the SELECT listing every version of the foods that have several.

    The versions of a food are its own record, the other records sharing its
    barcode and the versions proposed by users. Each gets its votes, its
    position in the order of the build, and whether it is the one served
    when no country is asked for.

    Args:
        relation: Table or view holding the records of every source.
        user_version_votes: Votes a version coming from a user needs to be
            served instead of a record of a source.

    Returns:
        A SELECT with `food_id`, `version_id`, `origin`, `votes`, `position`,
        `served` and the columns of `VERSION_COLUMNS`.
    """
    own = ", ".join(f"f.{name}" for name in VERSION_COLUMNS)
    other = ", ".join(f"r.{name}" for name in VERSION_COLUMNS)
    eligible = (
        f"(origin = '{ORIGIN_SOURCE}' OR votes >= {int(user_version_votes)} OR NOT has_source)"
    )
    return f"""
    WITH several AS (
        SELECT id FROM foods_base WHERE len(other_records) > 0
        UNION
        SELECT food_id FROM user_versions
    ),
    listed AS (
        SELECT f.id AS food_id, f.record_id AS version_id, {own}
        FROM foods_base AS f
        WHERE f.id IN (SELECT id FROM several)
        UNION ALL BY NAME
        SELECT x.food_id, r.id AS version_id, {other}
        FROM (
            SELECT id AS food_id, unnest(other_records) AS record_id
            FROM foods_base WHERE len(other_records) > 0
        ) AS x
        JOIN {relation} AS r ON r.id = x.record_id
        UNION ALL BY NAME
        SELECT * FROM user_versions
    ),
    voted AS (
        SELECT
            p.*,
            CASE WHEN p.source = '{kratheos.SOURCE_ID}' THEN '{ORIGIN_USER}'
                 ELSE '{ORIGIN_SOURCE}' END AS origin,
            coalesce(k.votes, 0) AS votes,
            row_number() OVER (
                PARTITION BY p.food_id
                ORDER BY p.plausible DESC, p.has_macros DESC, p.updated_at DESC NULLS LAST,
                         o.priority ASC, p.version_id ASC
            ) AS position
        FROM listed AS p
        JOIN source_order AS o ON o.source = p.source
        LEFT JOIN kratheos_votes AS k ON k.version_id = p.version_id
    ),
    judged AS (
        SELECT *, bool_or(origin = '{ORIGIN_SOURCE}') OVER (PARTITION BY food_id) AS has_source
        FROM voted
    )
    SELECT
        * EXCLUDE (has_source),
        row_number() OVER (
            PARTITION BY food_id ORDER BY {eligible} DESC, votes DESC, position ASC
        ) = 1 AS served
    FROM judged
    """


def build_foods(
    con: duckdb.DuckDBPyConnection,
    kratheos_records: Path | None,
    user_version_votes: int = DEFAULT_USER_VERSION_VOTES,
) -> dict:
    """Create the tables of the main database, Kratheos layer applied.

    Creates `foods` (one row per food, with `food_pk`, a number used only to
    link the tables inside the SQLite file), `food_names` (every name of
    every food, with its origin) and `food_versions` (every version of the
    foods that have more than one).

    Args:
        con: DuckDB connection on which `create_source_views` was called and
            the Kratheos tables were loaded (`kratheos.load_tables`).
        kratheos_records: Normalised records of the Kratheos products, or
            None when there is none.
        user_version_votes: Votes a version coming from a user needs to be
            served instead of a record of a source.

    Returns:
        Counts of what the Kratheos layer added and changed.
    """
    base = schema.output_names()
    columns = ", ".join(f'"{name}"' for name in base)
    relation = "source_records"
    barcodes = "by_barcode"
    added = 0
    added_with_barcode = 0
    if kratheos_records is not None:
        added, added_with_barcode = con.execute(
            f"SELECT count(*), count(barcode) FROM read_parquet({_sql_string(kratheos_records)})"
        ).fetchone()
    if added:
        con.execute(
            f"""
            CREATE OR REPLACE VIEW all_records AS
            SELECT {columns} FROM source_records
            UNION ALL
            SELECT {columns} FROM read_parquet({_sql_string(kratheos_records)})
            """
        )
        relation = "all_records"
        if added_with_barcode:
            select_by_barcode(con, "by_barcode_all", relation, SOURCES_FIRST_ORDER)
            barcodes = "by_barcode_all"
    without_id = ", ".join(f'"{name}"' for name in base if name != "id")
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE foods_base AS
        SELECT product_id AS id, id AS record_id, {without_id},
               source_count, sources, other_records, sources_agree
        FROM {barcodes}
        UNION ALL
        SELECT id, id AS record_id, {without_id},
               CAST(NULL AS INTEGER), CAST(NULL AS VARCHAR[]), CAST(NULL AS VARCHAR[]),
               CAST(NULL AS BOOLEAN)
        FROM {relation}
        WHERE NOT (kind = '{schema.KIND_PRODUCT}' AND barcode IS NOT NULL)
        """
    )
    con.execute(f"CREATE OR REPLACE TEMP TABLE user_versions AS {_user_versions_select()}")
    con.execute(
        "CREATE OR REPLACE TEMP TABLE food_versions AS "
        + _food_versions_select(relation, user_version_votes)
    )
    identity = [
        name
        for name in ("id", *base[1:], "source_count", "sources", "other_records", "sources_agree")
        if name not in VERSION_COLUMNS
    ]
    served = ",\n            ".join(
        f"CASE WHEN s.food_id IS NULL THEN f.{name} ELSE s.{name} END AS {name}"
        for name in VERSION_COLUMNS
    )
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE foods AS
        SELECT
            row_number() OVER (ORDER BY f.id) AS food_pk,
            {", ".join("f." + name for name in identity)},
            coalesce(s.version_id, f.record_id) AS record_id,
            {served},
            CAST(coalesce(s.votes, k.votes, 0) AS INTEGER) AS votes,
            CAST(coalesce(c.versions, 1) AS INTEGER) AS versions_count
        FROM foods_base AS f
        LEFT JOIN (SELECT * FROM food_versions WHERE served) AS s ON s.food_id = f.id
        LEFT JOIN (
            SELECT food_id, count(*) AS versions FROM food_versions GROUP BY food_id
        ) AS c ON c.food_id = f.id
        LEFT JOIN kratheos_votes AS k ON k.version_id = f.record_id
        """
    )
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE food_names AS
        SELECT
            food_pk,
            unnest(map_keys(names)) AS lang,
            unnest(map_values(names)) AS name,
            '{NAME_ORIGIN_SOURCE}' AS origin,
            CAST(NULL AS VARCHAR) AS kratheos_origin
        FROM foods
        UNION ALL
        SELECT f.food_pk, t.lang, t.name, '{NAME_ORIGIN_KRATHEOS}', t.origin
        FROM kratheos_translations AS t
        JOIN foods AS f ON f.id = t.record_id
        WHERE map_extract(f.names, t.lang)[1] IS NULL
        """
    )
    versions, versions_applied = con.execute(
        "SELECT (SELECT count(*) FROM kratheos_versions), (SELECT count(*) FROM user_versions)"
    ).fetchone()
    votes, votes_applied = con.execute(
        """
        SELECT
            (SELECT count(*) FROM kratheos_votes),
            (SELECT count(*) FROM kratheos_votes AS k
             WHERE k.version_id IN (SELECT version_id FROM food_versions)
                OR k.version_id IN (SELECT record_id FROM foods WHERE versions_count = 1))
        """
    ).fetchone()
    several, from_user = con.execute(
        f"""
        SELECT count(*) FILTER (WHERE versions_count > 1),
               count(*) FILTER (WHERE starts_with(record_id, '{kratheos.VERSION_PREFIX}'))
        FROM foods
        """
    ).fetchone()
    translations, translations_applied, translations_known = con.execute(
        f"""
        SELECT
            (SELECT count(*) FROM kratheos_translations),
            (SELECT count(*) FROM food_names WHERE origin = '{NAME_ORIGIN_KRATHEOS}'),
            (SELECT count(*) FROM kratheos_translations AS t JOIN foods AS f ON f.id = t.record_id
             WHERE map_extract(f.names, t.lang)[1] IS NOT NULL)
        """
    ).fetchone()
    con.execute("DROP TABLE foods_base")
    con.execute("DROP TABLE user_versions")
    return {
        "products": added,
        "products_with_valid_barcode": added_with_barcode,
        "versions": versions,
        "versions_applied": versions_applied,
        "versions_without_target": versions - versions_applied,
        "votes": votes,
        "votes_applied": votes_applied,
        "votes_without_target": votes - votes_applied,
        "translations": translations,
        "translations_applied": translations_applied,
        "translations_ignored_name_already_given": translations_known,
        "translations_without_target": translations - translations_applied - translations_known,
        "user_version_votes": int(user_version_votes),
        "foods_with_several_versions": several,
        "foods_served_from_a_user_version": from_user,
    }


def _counts(con: duckdb.DuckDBPyConnection, sql: str) -> dict:
    """Run a two-column query and return it as an ordered mapping.

    Args:
        con: DuckDB connection.
        sql: Query yielding a key and a count.

    Returns:
        A mapping of key to count, in query order.
    """
    return {str(key): value for key, value in con.execute(sql).fetchall()}


def records_statistics(con: duckdb.DuckDBPyConnection, relation: str, where: str = "") -> dict:
    """Count the records of a relation holding output records.

    Args:
        con: DuckDB connection.
        relation: Table, view or `read_parquet` call.
        where: Optional WHERE clause, keyword included.

    Returns:
        Counts by kind, dataset, name language and flag, plus barcode and
        completeness figures.
    """
    total, with_macros, plausible, barcodes, distinct_barcodes = con.execute(
        f"""
        SELECT count(*), count(*) FILTER (WHERE has_macros), count(*) FILTER (WHERE plausible),
               count(barcode), count(DISTINCT barcode)
        FROM {relation} {where}
        """
    ).fetchone()
    return {
        "records": total,
        "by_kind": _counts(
            con, f"SELECT kind, count(*) FROM {relation} {where} GROUP BY 1 ORDER BY 1"
        ),
        "by_dataset": _counts(
            con,
            f"SELECT coalesce(source_dataset, 'main'), count(*) FROM {relation} {where}"
            " GROUP BY 1 ORDER BY 1",
        ),
        "with_four_macros": with_macros,
        "plausible": plausible,
        "with_valid_barcode": barcodes,
        "distinct_barcodes": distinct_barcodes,
        "names_by_language": _counts(
            con,
            f"""
            SELECT lang, count(*) FROM (
                SELECT unnest(map_keys(names)) AS lang FROM {relation} {where}
            ) GROUP BY 1 ORDER BY 2 DESC, 1
            """,
        ),
        "flags": _counts(
            con,
            f"""
            SELECT flag, count(*) FROM (
                SELECT unnest(flags) AS flag FROM {relation} {where}
            ) GROUP BY 1 ORDER BY 2 DESC, 1
            """,
        ),
    }


def source_statistics(con: duckdb.DuckDBPyConnection, source_id: str) -> dict:
    """Count the records of one source in the aggregated sources.

    Args:
        con: DuckDB connection on which `create_source_views` was called.
        source_id: Identifier of the source.

    Returns:
        The statistics of `records_statistics` for that source.
    """
    return records_statistics(con, "source_records", f"WHERE source = {_sql_string(source_id)}")


def overall_statistics(con: duckdb.DuckDBPyConnection) -> dict:
    """Count what the aggregated sources hold as a whole.

    Args:
        con: DuckDB connection on which `create_source_views` was called.

    Returns:
        Totals for generic foods, products and barcodes, the quality flags and
        how often sources disagree on a shared barcode.
    """
    records, generic, products, flagged = con.execute(
        f"""
        SELECT count(*),
               count(*) FILTER (WHERE kind = '{schema.KIND_GENERIC}'),
               count(*) FILTER (WHERE kind = '{schema.KIND_PRODUCT}'),
               count(*) FILTER (WHERE NOT plausible)
        FROM source_records
        """
    ).fetchone()
    product_stats = con.execute(
        f"""
        SELECT count(*) FILTER (WHERE has_macros),
               count(barcode),
               count(*) FILTER (WHERE barcode IS NOT NULL AND has_macros),
               count(*) FILTER (WHERE list_contains(flags, '{schema.FLAG_BARCODE_INVALID}')),
               count(*) FILTER (WHERE barcode IS NULL
                   AND NOT list_contains(flags, '{schema.FLAG_BARCODE_INVALID}')),
               count(*) FILTER (WHERE list_contains(flags, '{schema.FLAG_BARCODE_RESTRICTED}'))
        FROM source_records WHERE kind = '{schema.KIND_PRODUCT}'
        """
    ).fetchone()
    barcode_stats = con.execute(
        """
        SELECT count(*),
               count(*) FILTER (WHERE has_macros),
               count(*) FILTER (WHERE plausible),
               count(*) FILTER (WHERE source_count > 1),
               count(*) FILTER (WHERE sources_agree IS NOT NULL),
               count(*) FILTER (WHERE sources_agree),
               count(*) FILTER (WHERE NOT sources_agree)
        FROM by_barcode
        """
    ).fetchone()
    return {
        "records": records,
        "generic_foods": generic,
        "products": {
            "records": products,
            "with_four_macros": product_stats[0],
            "with_valid_barcode": product_stats[1],
            "with_valid_barcode_and_four_macros": product_stats[2],
            "with_invalid_barcode_dropped": product_stats[3],
            "without_barcode_in_source": product_stats[4],
            "with_restricted_barcode": product_stats[5],
        },
        "barcodes": {
            "distinct": barcode_stats[0],
            "preferred_record_has_four_macros": barcode_stats[1],
            "preferred_record_is_plausible": barcode_stats[2],
            "known_to_several_sources": barcode_stats[3],
            "compared_between_sources": barcode_stats[4],
            "sources_agree": barcode_stats[5],
            "sources_disagree": barcode_stats[6],
            "preferred_record_by_source": _counts(
                con, "SELECT source, count(*) FROM by_barcode GROUP BY 1 ORDER BY 2 DESC"
            ),
        },
        "countries": {
            "by_source": {
                source: {
                    "records": total,
                    "with_a_country": known,
                    "share": round(known / total, 4) if total else 0.0,
                }
                for source, total, known in con.execute(
                    """
                    SELECT source, count(*), count(*) FILTER (WHERE len(countries) > 0)
                    FROM source_records GROUP BY 1 ORDER BY 1
                    """
                ).fetchall()
            },
            "barcoded_products_top_countries": _counts(
                con,
                """
                SELECT country, count(*) FROM (
                    SELECT unnest(countries) AS country FROM by_barcode
                ) GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 10
                """,
            ),
            "barcoded_products_with_a_country": con.execute(
                "SELECT count(*) FROM by_barcode WHERE len(countries) > 0"
            ).fetchone()[0],
        },
        "quality": {
            "records_flagged": flagged,
            "share_flagged": round(flagged / records, 5) if records else 0.0,
            "by_reason": _counts(
                con,
                f"""
                SELECT flag, count(*) FROM (
                    SELECT unnest(flags) AS flag FROM source_records
                ) WHERE list_contains({quality_list_sql()}, flag) GROUP BY 1 ORDER BY 2 DESC, 1
                """,
            ),
            "by_source": {
                source: {"records": total, "flagged": bad}
                for source, total, bad in con.execute(
                    """
                    SELECT source, count(*), count(*) FILTER (WHERE NOT plausible)
                    FROM source_records GROUP BY 1 ORDER BY 1
                    """
                ).fetchall()
            },
        },
    }


def foods_statistics(con: duckdb.DuckDBPyConnection) -> dict:
    """Count what the main database holds.

    Args:
        con: DuckDB connection on which `build_foods` was called.

    Returns:
        Number of foods by kind and by source, of names by origin, and of
        versions kept for the foods that have several.
    """
    foods, generic, with_barcode, without_barcode = con.execute(
        f"""
        SELECT count(*),
               count(*) FILTER (WHERE kind = '{schema.KIND_GENERIC}'),
               count(*) FILTER (WHERE kind = '{schema.KIND_PRODUCT}' AND barcode IS NOT NULL),
               count(*) FILTER (WHERE kind = '{schema.KIND_PRODUCT}' AND barcode IS NULL)
        FROM foods
        """
    ).fetchone()
    return {
        "foods": foods,
        "generic_foods": generic,
        "products_with_barcode": with_barcode,
        "products_without_valid_barcode": without_barcode,
        "by_source": _counts(con, "SELECT source, count(*) FROM foods GROUP BY 1 ORDER BY 1"),
        "names": con.execute("SELECT count(*) FROM food_names").fetchone()[0],
        "names_by_origin": _counts(
            con, "SELECT origin, count(*) FROM food_names GROUP BY 1 ORDER BY 1"
        ),
        "versions_kept": con.execute("SELECT count(*) FROM food_versions").fetchone()[0],
    }
