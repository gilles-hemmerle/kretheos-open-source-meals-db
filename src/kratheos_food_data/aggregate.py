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
Kratheos layer was applied in this order:

1. the products added in Kratheos join the records (source `kratheos`); one
   with a barcode competes for that barcode under the rule above, Kratheos
   coming last in the fixed source order;
2. corrections replace the values of the food they name; the values of the
   source are kept aside, with their source (`source_values`);
3. confirmation counts are attached;
4. translations give a food the names it lacks. A translation never replaces
   a name the food already has; every name records where it comes from.

Identifiers. A food that has a barcode is identified by the barcode itself,
`gtin:<canonical barcode>`, whichever source is preferred at the time; the
record of the source behind it keeps its own identifier (`record_id`). Every
other food is identified by its source record, `<source>:<id in the source>`.
No identifier depends on a counter, a row number or the order of processing.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import duckdb

from . import schema
from .config import Source
from .kratheos import VALUE_COLUMNS
from .normalize import has_macros_sql, quality_flag_cases, quality_list_sql

GENERIC_NAME = "generic_foods"
PRODUCTS_NAME = "products"
BY_BARCODE_NAME = "products_by_barcode"
PRODUCT_ID_PREFIX = "gtin:"
AGREE_ENERGY_ABSOLUTE_KCAL = 10.0
AGREE_ENERGY_RELATIVE = 0.10
AGREE_MACRO_ABSOLUTE_G = 2.0
AGREE_MACRO_RELATIVE = 0.20
COMPARED_VALUES = ("energy_kcal", "protein_g", "carbohydrate_g", "fat_g")
PREFERENCE_ORDER = (
    "p.plausible DESC, p.has_macros DESC, p.updated_at DESC NULLS LAST, o.priority ASC, p.id ASC"
)
# Informational flag -> corrected column that makes it obsolete.
DERIVATION_FLAGS = {
    schema.FLAG_ENERGY_FROM_KJ: "energy_kcal",
    schema.FLAG_SALT_FROM_SODIUM: "salt_g",
    schema.FLAG_ALCOHOL_FROM_ABV: "alcohol_g",
    schema.FLAG_BASIS_ASSUMED: "basis",
    schema.FLAG_CARBOHYDRATE_FIBER_SUBTRACTED: "carbohydrate_g",
    schema.FLAG_CARBOHYDRATE_INCLUDES_FIBER: "carbohydrate_g",
}
NAME_ORIGIN_SOURCE = "source"
NAME_ORIGIN_KRATHEOS = "kratheos"
NAME_FROM_CORRECTION = "correction"


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


def select_by_barcode(con: duckdb.DuckDBPyConnection, table: str, relation: str) -> None:
    """Create the table of the preferred record of each barcode.

    The table has the output columns of the preferred record, `product_id`
    (the identifier of the product itself) and the columns describing the
    other sources.

    Args:
        con: DuckDB connection; the table `source_order` must exist.
        table: Name of the table to create.
        relation: Table or view holding the candidate records.
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
            row_number() OVER (PARTITION BY p.barcode ORDER BY {PREFERENCE_ORDER}) AS position,
            row_number() OVER (PARTITION BY p.barcode, p.source ORDER BY {PREFERENCE_ORDER})
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


def _corrected_select() -> str:
    """Build the SELECT giving the corrected version of the corrected foods.

    Each corrected value replaces the value of the food; the quality checks
    are run again on the result, and the flags that described how a replaced
    value had been obtained are dropped.

    Returns:
        A SELECT over `foods_base` joined to `kratheos_corrections`, with the
        columns of `foods_base`.
    """
    special = ("is_drink", "serving_unit", "basis")
    plain = [name for name, _ in VALUE_COLUMNS if name not in special]
    overlay = ",\n            ".join(f"coalesce(c.{name}, f.{name}) AS {name}" for name in plain)
    stale = " OR ".join(
        f"(flag = '{flag}' AND c.{column} IS NOT NULL)" for flag, column in DERIVATION_FLAGS.items()
    )
    quality = "[flag FOR flag IN [" + ", ".join(quality_flag_cases()) + "] IF flag IS NOT NULL]"
    passthrough = (
        "id",
        "record_id",
        "source",
        "source_dataset",
        "source_record_id",
        "kind",
        "barcode",
        "name_lang",
        "countries",
        "popularity",
        "licence",
        "source_url",
        "source_count",
        "sources",
        "other_records",
        "sources_agree",
    )
    return f"""
    WITH overlaid AS (
        SELECT
            {", ".join("f." + name for name in passthrough)},
            CASE WHEN c.names IS NULL THEN f.names ELSE map_concat(f.names, c.names) END AS names,
            {overlay},
            CASE WHEN c.carbohydrate_g IS NULL THEN f.carbohydrate_total_g END
                AS carbohydrate_total_g,
            coalesce(c.basis, f.basis) AS basis,
            coalesce(c.serving_unit, f.serving_unit) AS serving_unit,
            coalesce(CAST(c.is_drink AS BOOLEAN), f.is_drink) AS is_drink,
            coalesce(c.updated_at, f.updated_at) AS updated_at,
            CAST(NULL AS DOUBLE) AS polyols_g,
            [flag FOR flag IN f.flags
             IF NOT list_contains({quality_list_sql()}, flag) AND NOT ({stale})] AS kept_flags
        FROM foods_base AS f
        JOIN kratheos_corrections AS c ON c.record_id = f.id
    ),
    checked AS (
        SELECT *, {has_macros_sql()} AS has_macros FROM overlaid
    ),
    flagged AS (
        SELECT
            *,
            list_concat(kept_flags, {quality}, ['{schema.FLAG_KRATHEOS_CORRECTED}']) AS new_flags
        FROM checked
    )
    SELECT
        * EXCLUDE (polyols_g, kept_flags, new_flags),
        trim(map_extract(names, name_lang)[1]) AS name,
        new_flags AS flags,
        NOT list_has_any(new_flags, {quality_list_sql()}) AS plausible
    FROM flagged
    """


def build_foods(con: duckdb.DuckDBPyConnection, kratheos_records: Path | None) -> dict:
    """Create the tables of the main database, Kratheos layer applied.

    Creates `foods` (one row per food, with `food_pk`, a number used only to
    link the tables inside the SQLite file), `food_names` (every name of
    every food, with its origin) and `source_values` (the foods as their
    source gives them, for those a correction changed).

    Args:
        con: DuckDB connection on which `create_source_views` was called and
            the Kratheos tables were loaded (`kratheos.load_tables`).
        kratheos_records: Normalised records of the Kratheos products, or
            None when there is none.

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
            select_by_barcode(con, "by_barcode_all", relation)
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
    con.execute(f"CREATE OR REPLACE TEMP TABLE corrected AS {_corrected_select()}")
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE source_values AS
        SELECT * FROM foods_base WHERE id IN (SELECT id FROM corrected)
        """
    )
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE foods AS
        SELECT
            row_number() OVER (ORDER BY f.id) AS food_pk,
            f.*,
            k.confirmations
        FROM (
            SELECT * FROM foods_base WHERE id NOT IN (SELECT id FROM corrected)
            UNION ALL BY NAME
            SELECT * FROM corrected
        ) AS f
        LEFT JOIN kratheos_confirmations AS k ON k.record_id = f.id
        """
    )
    con.execute("DROP TABLE foods_base")
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE food_names AS
        WITH own AS (
            SELECT food_pk, id, unnest(map_keys(names)) AS lang, unnest(map_values(names)) AS name
            FROM foods
        ),
        corrected_names AS (
            SELECT record_id, unnest(map_keys(names)) AS lang
            FROM kratheos_corrections WHERE names IS NOT NULL
        )
        SELECT
            own.food_pk,
            own.lang,
            own.name,
            CASE WHEN c.lang IS NULL THEN '{NAME_ORIGIN_SOURCE}'
                 ELSE '{NAME_ORIGIN_KRATHEOS}' END AS origin,
            CASE WHEN c.lang IS NULL THEN NULL ELSE '{NAME_FROM_CORRECTION}' END AS kratheos_origin
        FROM own
        LEFT JOIN corrected_names AS c ON c.record_id = own.id AND c.lang = own.lang
        UNION ALL
        SELECT f.food_pk, t.lang, t.name, '{NAME_ORIGIN_KRATHEOS}', t.origin
        FROM kratheos_translations AS t
        JOIN foods AS f ON f.id = t.record_id
        WHERE map_extract(f.names, t.lang)[1] IS NULL
        """
    )
    corrections, corrections_applied = con.execute(
        "SELECT (SELECT count(*) FROM kratheos_corrections), (SELECT count(*) FROM corrected)"
    ).fetchone()
    confirmations, confirmations_applied = con.execute(
        """
        SELECT (SELECT count(*) FROM kratheos_confirmations),
               (SELECT count(*) FROM foods WHERE confirmations IS NOT NULL)
        """
    ).fetchone()
    translations, translations_applied, translations_known = con.execute(
        f"""
        SELECT
            (SELECT count(*) FROM kratheos_translations),
            (SELECT count(*) FROM food_names
             WHERE origin = '{NAME_ORIGIN_KRATHEOS}'
               AND kratheos_origin <> '{NAME_FROM_CORRECTION}'),
            (SELECT count(*) FROM kratheos_translations AS t JOIN foods AS f ON f.id = t.record_id
             WHERE map_extract(f.names, t.lang)[1] IS NOT NULL)
        """
    ).fetchone()
    con.execute("DROP TABLE corrected")
    return {
        "products": added,
        "products_with_valid_barcode": added_with_barcode,
        "corrections": corrections,
        "corrections_applied": corrections_applied,
        "corrections_without_target": corrections - corrections_applied,
        "confirmations": confirmations,
        "confirmations_applied": confirmations_applied,
        "confirmations_without_target": confirmations - confirmations_applied,
        "translations": translations,
        "translations_applied": translations_applied,
        "translations_ignored_name_already_given": translations_known,
        "translations_without_target": translations - translations_applied - translations_known,
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
        foods kept with the values of their source.
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
        "foods_kept_with_source_values": con.execute(
            "SELECT count(*) FROM source_values"
        ).fetchone()[0],
    }
