"""Turns the staging file of a source into records of the output schema.

Everything that must be done the same way for every source lives here, in one
SQL statement:

* a record is kept only if it has a name and an energy value (kcal or kJ);
* the barcode is normalised and its check digit verified (`barcode.py`);
* kcal is derived from kJ, salt from sodium and alcohol from % vol when the
  source only has the latter, and a flag says so;
* the quality checks are run and their flags attached. A record that fails is
  kept: nothing is corrected, nothing is removed.

Quality thresholds, and why:

* nutrients must lie within 0 to 100 g per 100 g (or 100 ml);
* energy must lie within 0 to 905 kcal: nothing edible exceeds pure fat, which
  is 900 kcal per 100 g with the general Atwater factor and 902 kcal with the
  specific factor USDA uses for animal fats;
* sugars may not exceed carbohydrate, nor saturated fat exceed fat, by more
  than 1 g (labels round each value separately);
* protein + carbohydrate + fat may not exceed 105 g (same rounding margin).
  These two bounds use the carbohydrate figure of the source, whichever
  convention it follows;
* energy must be consistent with the macros. The schema holds one convention
  for carbohydrate: available carbohydrate, fibre excluded, as on European
  labels (a source that gives total carbohydrate is converted, see below). The
  expected energy follows the same convention, with the general Atwater
  factors: 4 kcal/g for protein and carbohydrate, 9 for fat, 7 for alcohol,
  2 for fibre, counted apart, and 2.4 for the polyols that carbohydrate
  includes. The energy passes when it is within 20 % and 10 kcal of that
  expectation. 20 % is the tolerance regulators accept between a label and an
  analysis (European Commission guidance on tolerances, 2012; United States
  21 CFR 101.9(g)), and it also covers the gap between general and
  food-specific Atwater factors; the 10 kcal absorb the rounding of foods that
  hold almost no energy, where a percentage means nothing. Two more margins
  cover what sources really do with the factors that vary: 2 kcal per gram of
  fibre, because a United States label counts fibre at 4 kcal/g or at 0 (when
  insoluble) where a European one counts 2, and 2.4 kcal per gram of polyols,
  because some polyols bring no energy at all.

Carbohydrate convention. Sources do not mean the same thing by carbohydrate:
"by difference" (USDA, the Canadian file) and the total carbohydrate of North
American labels include dietary fibre; European tables and labels give
available carbohydrate. An adapter says which one its source gives
(`carbohydrate_includes_fiber`). When fibre is included and known, it is
subtracted: `carbohydrate_g` becomes the available carbohydrate, the figure of
the source is kept in `carbohydrate_total_g`, and the record is flagged
`carbohydrate_fiber_subtracted`. When fibre is included but not given, the
figure cannot be converted: it is kept as it is, also copied in
`carbohydrate_total_g`, and the record is flagged `carbohydrate_includes_fiber`.
"""

from __future__ import annotations

from pathlib import Path

import duckdb

from . import schema
from .config import Source

KJ_PER_KCAL = 4.184
SALT_PER_SODIUM = 2.5
ETHANOL_G_PER_ML = 0.789
NUTRIENT_MAX_G = 100.0
ENERGY_MAX_KCAL = 905.0
PART_EXCESS_TOLERANCE_G = 1.0
MACRO_SUM_MAX_G = 105.0
ATWATER_RELATIVE_TOLERANCE = 0.20
ATWATER_ABSOLUTE_TOLERANCE_KCAL = 10.0
FIBER_KCAL_PER_G = 2.0
POLYOLS_KCAL_PER_G = 2.4
PREFERRED_NAME_LANGUAGES = ("en", "fr", "de", "it", "es")
VALUE_DECIMALS = 4

NUTRIENTS = (
    "protein_g",
    "carbohydrate_g",
    "sugars_g",
    "fat_g",
    "saturated_fat_g",
    "fiber_g",
    "salt_g",
    "alcohol_g",
)


def _sql_string(value: str) -> str:
    """Quote a Python string as a SQL literal.

    Args:
        value: The text to quote.

    Returns:
        The SQL string literal.
    """
    return "'" + value.replace("'", "''") + "'"


def _name_lang_sql() -> str:
    """Build the SQL expression choosing the language of the display name.

    The main language of the record wins when the source has a name in it;
    otherwise the first available of a fixed list, then any language.

    Returns:
        A SQL expression over the columns `names` and `main_lang`.
    """
    cases = ["CASE WHEN map_extract(names, main_lang)[1] IS NOT NULL THEN main_lang END"]
    for lang in PREFERRED_NAME_LANGUAGES:
        cases.append(f"CASE WHEN map_extract(names, '{lang}')[1] IS NOT NULL THEN '{lang}' END")
    cases.append("map_keys(names)[1]")
    return "coalesce(" + ", ".join(cases) + ")"


def quality_flag_cases() -> list[str]:
    """Build the SQL expressions that each raise one quality flag.

    The expressions work on any relation exposing the nutrient columns of the
    output schema (`carbohydrate_total_g` included) plus `polyols_g` and
    `has_macros`, so the same checks serve the records of the sources and the
    records corrected by Kratheos.

    Returns:
        One `CASE WHEN ... THEN '<flag>' END` expression per quality flag.
    """
    range_checks = " OR ".join(f"({name} < 0 OR {name} > {NUTRIENT_MAX_G})" for name in NUTRIENTS)
    expected = (
        "(4 * protein_g + 4 * greatest(carbohydrate_g - coalesce(polyols_g, 0), 0)"
        f" + {POLYOLS_KCAL_PER_G} * coalesce(polyols_g, 0)"
        f" + {FIBER_KCAL_PER_G} * coalesce(fiber_g, 0)"
        " + 9 * fat_g + 7 * coalesce(alcohol_g, 0))"
    )
    margin = (
        f"({ATWATER_RELATIVE_TOLERANCE} * {expected} + {ATWATER_ABSOLUTE_TOLERANCE_KCAL}"
        f" + {FIBER_KCAL_PER_G} * greatest(coalesce(fiber_g, 0), 0)"
        f" + {POLYOLS_KCAL_PER_G} * greatest(coalesce(polyols_g, 0), 0))"
    )
    # The two bounds below compare carbohydrate with other figures of the same
    # source, so they use the figure the source gives: subtracting a rounded
    # fibre from a rounded total would add noise to them.
    given = "coalesce(carbohydrate_total_g, carbohydrate_g)"
    mismatch = (
        "has_macros AND protein_g >= 0 AND carbohydrate_g >= 0 AND fat_g >= 0"
        f" AND abs(energy_kcal - {expected}) > {margin}"
    )
    return [
        f"CASE WHEN {range_checks} THEN '{schema.FLAG_VALUE_OUT_OF_RANGE}' END",
        f"CASE WHEN energy_kcal < 0 OR energy_kcal > {ENERGY_MAX_KCAL}"
        f" THEN '{schema.FLAG_ENERGY_OUT_OF_RANGE}' END",
        f"CASE WHEN sugars_g > {given} + {PART_EXCESS_TOLERANCE_G}"
        f" THEN '{schema.FLAG_SUGARS_EXCEED_CARBOHYDRATE}' END",
        f"CASE WHEN saturated_fat_g > fat_g + {PART_EXCESS_TOLERANCE_G}"
        f" THEN '{schema.FLAG_SATURATED_EXCEED_FAT}' END",
        f"CASE WHEN protein_g + {given} + fat_g > {MACRO_SUM_MAX_G}"
        f" THEN '{schema.FLAG_MACROS_SUM_OVER_100}' END",
        f"CASE WHEN {mismatch} THEN '{schema.FLAG_ENERGY_MACRO_MISMATCH}' END",
    ]


def has_macros_sql() -> str:
    """Build the SQL test that a record has the four macros.

    Returns:
        A boolean SQL expression over the nutrient columns.
    """
    return (
        "(energy_kcal IS NOT NULL AND protein_g IS NOT NULL"
        " AND carbohydrate_g IS NOT NULL AND fat_g IS NOT NULL)"
    )


def _flags_sql() -> str:
    """Build the SQL expression computing the list of flags of a record.

    Returns:
        A SQL expression over the derived columns of the `derived` CTE.
    """
    candidates = [
        f"CASE WHEN energy_derived THEN '{schema.FLAG_ENERGY_FROM_KJ}' END",
        f"CASE WHEN salt_derived THEN '{schema.FLAG_SALT_FROM_SODIUM}' END",
        f"CASE WHEN alcohol_derived THEN '{schema.FLAG_ALCOHOL_FROM_ABV}' END",
        f"CASE WHEN basis_assumed THEN '{schema.FLAG_BASIS_ASSUMED}' END",
        f"CASE WHEN fiber_subtracted THEN '{schema.FLAG_CARBOHYDRATE_FIBER_SUBTRACTED}' END",
        f"CASE WHEN fiber_included THEN '{schema.FLAG_CARBOHYDRATE_INCLUDES_FIBER}' END",
        f"CASE WHEN barcode_given AND barcode IS NULL THEN '{schema.FLAG_BARCODE_INVALID}' END",
        f"CASE WHEN gtin_restricted(barcode) THEN '{schema.FLAG_BARCODE_RESTRICTED}' END",
        *quality_flag_cases(),
    ]
    computed = "[flag FOR flag IN [" + ", ".join(candidates) + "] IF flag IS NOT NULL]"
    return f"list_concat(coalesce(source_flags, []::VARCHAR[]), {computed})"


def quality_list_sql() -> str:
    """Build the SQL list literal of the flags that make a record implausible.

    Returns:
        A SQL list of string literals.
    """
    return "[" + ", ".join(_sql_string(flag) for flag in schema.QUALITY_FLAGS) + "]"


def normalize_sql(source: Source, staging_paths: list[Path]) -> str:
    """Build the SELECT that turns a staging file into output records.

    Besides the output columns, the SELECT yields two helper columns used for
    the statistics and removed before writing: `drop_reason` (NULL for a kept
    record) and `barcode_given`.

    Args:
        source: The source the staging files belong to.
        staging_paths: Paths of the staging Parquet files.

    Returns:
        The SQL SELECT.
    """
    files = ", ".join(_sql_string(str(path)) for path in staging_paths)
    decimals = VALUE_DECIMALS
    rounded = ",\n            ".join(
        f"round({name}, {decimals}) AS {name}"
        for name in NUTRIENTS
        if name not in ("salt_g", "alcohol_g", "carbohydrate_g")
    )
    total_given = "coalesce(carbohydrate_includes_fiber, FALSE) AND carbohydrate_g IS NOT NULL"
    return f"""
    WITH staged AS (
        SELECT * FROM read_parquet([{files}])
    ),
    named AS (
        SELECT *, {_name_lang_sql()} AS name_lang FROM staged
    ),
    derived AS (
        SELECT
            source_dataset,
            source_record_id,
            kind,
            nullif(trim(barcode_raw), '') IS NOT NULL AS barcode_given,
            gtin_normalize(barcode_raw) AS barcode,
            trim(map_extract(names, name_lang)[1]) AS name,
            name_lang,
            names,
            nullif(trim(brand), '') AS brand,
            nullif(trim(category), '') AS category,
            basis,
            coalesce(basis_assumed, FALSE) AS basis_assumed,
            CASE
                WHEN energy_kcal IS NOT NULL THEN round(energy_kcal, {decimals})
                ELSE round(energy_kj / {KJ_PER_KCAL}, 1)
            END AS energy_kcal,
            energy_kcal IS NULL AND energy_kj IS NOT NULL AS energy_derived,
            {rounded},
            CASE
                WHEN {total_given} AND fiber_g IS NOT NULL
                    THEN round(greatest(carbohydrate_g - greatest(fiber_g, 0), 0), {decimals})
                ELSE round(carbohydrate_g, {decimals})
            END AS carbohydrate_g,
            CASE WHEN {total_given} THEN round(carbohydrate_g, {decimals}) END
                AS carbohydrate_total_g,
            {total_given} AND fiber_g IS NOT NULL AS fiber_subtracted,
            {total_given} AND fiber_g IS NULL AS fiber_included,
            CASE
                WHEN salt_g IS NOT NULL THEN round(salt_g, {decimals})
                ELSE round(sodium_g * {SALT_PER_SODIUM}, {decimals})
            END AS salt_g,
            salt_g IS NULL AND sodium_g IS NOT NULL AS salt_derived,
            CASE
                WHEN alcohol_g IS NOT NULL THEN round(alcohol_g, {decimals})
                ELSE round(alcohol_abv * {ETHANOL_G_PER_ML}, 2)
            END AS alcohol_g,
            alcohol_g IS NULL AND alcohol_abv > 0 AS alcohol_derived,
            polyols_g,
            round(serving_size, {decimals}) AS serving_size,
            serving_unit,
            nullif(trim(serving_text), '') AS serving_text,
            is_drink,
            CASE WHEN len(countries) > 0
                 THEN list_sort(list_distinct([upper(code) FOR code IN countries]))
            END AS countries,
            popularity,
            source_flags,
            source_url,
            updated_at
        FROM named
    ),
    checked AS (
        SELECT
            *,
            {has_macros_sql()} AS has_macros
        FROM derived
    ),
    flagged AS (
        SELECT *, {_flags_sql()} AS flags FROM checked
    )
    SELECT
        {_sql_string(source.id)} || ':' || source_record_id AS id,
        {_sql_string(source.id)} AS source,
        source_dataset,
        source_record_id,
        kind,
        barcode,
        name,
        name_lang,
        names,
        brand,
        category,
        basis,
        energy_kcal,
        protein_g,
        carbohydrate_g,
        carbohydrate_total_g,
        sugars_g,
        fat_g,
        saturated_fat_g,
        fiber_g,
        salt_g,
        alcohol_g,
        serving_size,
        serving_unit,
        serving_text,
        is_drink,
        countries,
        popularity,
        has_macros,
        NOT list_has_any(flags, {quality_list_sql()}) AS plausible,
        flags,
        {_sql_string(source.licence)} AS licence,
        source_url,
        updated_at,
        barcode_given,
        CASE
            WHEN source_record_id IS NULL THEN 'no_id'
            WHEN name IS NULL OR name = '' THEN 'no_name'
            WHEN energy_kcal IS NULL THEN 'no_energy'
        END AS drop_reason
    FROM flagged
    """


def normalize_source(
    con: duckdb.DuckDBPyConnection, source: Source, staging_path: Path, out_path: Path
) -> dict:
    """Write the normalised records of a source and count what was dropped.

    Args:
        con: DuckDB connection with the barcode macros registered.
        source: The source being processed.
        staging_path: Staging Parquet file written by the adapter.
        out_path: Where to write the normalised Parquet file.

    Returns:
        Counts: records read from the adapter, kept, and dropped by reason.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    select = normalize_sql(source, [staging_path])
    con.execute(f"CREATE OR REPLACE TEMP TABLE normalized AS {select}")
    rows = con.execute(
        "SELECT coalesce(drop_reason, 'kept'), count(*) FROM normalized GROUP BY 1"
    ).fetchall()
    counts = dict(rows)
    duplicates = con.execute(
        "SELECT count(*) - count(DISTINCT id) FROM normalized WHERE drop_reason IS NULL"
    ).fetchone()[0]
    if duplicates:
        raise ValueError(f"source {source.id}: {duplicates} duplicate record ids")
    columns = ", ".join(f'"{name}"' for name in schema.output_names())
    con.execute(
        f"""
        COPY (
            SELECT {columns} FROM normalized WHERE drop_reason IS NULL ORDER BY id
        ) TO {_sql_string(str(out_path))} (FORMAT PARQUET, COMPRESSION ZSTD)
        """
    )
    con.execute("DROP TABLE normalized")
    kept = counts.pop("kept", 0)
    return {
        "read": kept + sum(counts.values()),
        "kept": kept,
        "dropped": {reason: counts[reason] for reason in sorted(counts)},
    }
