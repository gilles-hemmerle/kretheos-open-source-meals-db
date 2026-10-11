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
* protein + carbohydrate + fat may not exceed 105 g (same rounding margin);
* energy must be consistent with the macros. The expected energy is computed
  with the general Atwater factors (4 kcal/g protein and carbohydrate, 9 fat,
  7 alcohol, 2 fibre). Sources do not agree on what "carbohydrate" means, so
  the expectation is a range rather than a number: its low end assumes that
  carbohydrate includes fibre and polyols and that both bring no energy (the
  United States convention, "by difference"), its high end assumes that
  carbohydrate excludes fibre, counted apart at 2 kcal/g (the European Union
  convention). The energy passes when it lies within that range widened by
  20 % and 10 kcal. 20 % is the tolerance regulators accept between a label
  and an analysis (European Commission guidance on tolerances, 2012; United
  States 21 CFR 101.9(g)), and it also covers the gap between general and
  food-specific Atwater factors; the 10 kcal absorb the rounding of foods
  that hold almost no energy, where a percentage means nothing.
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


def _flags_sql() -> str:
    """Build the SQL expression computing the list of flags of a record.

    Returns:
        A SQL expression over the derived columns of the `derived` CTE.
    """
    range_checks = " OR ".join(f"({name} < 0 OR {name} > {NUTRIENT_MAX_G})" for name in NUTRIENTS)
    low = (
        "4 * protein_g + 4 * greatest(carbohydrate_g - coalesce(fiber_g, 0)"
        " - coalesce(polyols_g, 0), 0) + 9 * fat_g + 7 * coalesce(alcohol_g, 0)"
    )
    high = (
        "4 * protein_g + 4 * carbohydrate_g + 2 * coalesce(fiber_g, 0)"
        " + 9 * fat_g + 7 * coalesce(alcohol_g, 0)"
    )
    rel = ATWATER_RELATIVE_TOLERANCE
    absolute = ATWATER_ABSOLUTE_TOLERANCE_KCAL
    mismatch = (
        "has_macros AND protein_g >= 0 AND carbohydrate_g >= 0 AND fat_g >= 0 AND ("
        f"energy_kcal < ({low}) * (1 - {rel}) - {absolute}"
        f" OR energy_kcal > ({high}) * (1 + {rel}) + {absolute})"
    )
    candidates = [
        f"CASE WHEN energy_derived THEN '{schema.FLAG_ENERGY_FROM_KJ}' END",
        f"CASE WHEN salt_derived THEN '{schema.FLAG_SALT_FROM_SODIUM}' END",
        f"CASE WHEN alcohol_derived THEN '{schema.FLAG_ALCOHOL_FROM_ABV}' END",
        f"CASE WHEN basis_assumed THEN '{schema.FLAG_BASIS_ASSUMED}' END",
        f"CASE WHEN barcode_given AND barcode IS NULL THEN '{schema.FLAG_BARCODE_INVALID}' END",
        f"CASE WHEN gtin_restricted(barcode) THEN '{schema.FLAG_BARCODE_RESTRICTED}' END",
        f"CASE WHEN {range_checks} THEN '{schema.FLAG_VALUE_OUT_OF_RANGE}' END",
        f"CASE WHEN energy_kcal < 0 OR energy_kcal > {ENERGY_MAX_KCAL}"
        f" THEN '{schema.FLAG_ENERGY_OUT_OF_RANGE}' END",
        f"CASE WHEN sugars_g > carbohydrate_g + {PART_EXCESS_TOLERANCE_G}"
        f" THEN '{schema.FLAG_SUGARS_EXCEED_CARBOHYDRATE}' END",
        f"CASE WHEN saturated_fat_g > fat_g + {PART_EXCESS_TOLERANCE_G}"
        f" THEN '{schema.FLAG_SATURATED_EXCEED_FAT}' END",
        f"CASE WHEN protein_g + carbohydrate_g + fat_g > {MACRO_SUM_MAX_G}"
        f" THEN '{schema.FLAG_MACROS_SUM_OVER_100}' END",
        f"CASE WHEN {mismatch} THEN '{schema.FLAG_ENERGY_MACRO_MISMATCH}' END",
    ]
    computed = "[flag FOR flag IN [" + ", ".join(candidates) + "] IF flag IS NOT NULL]"
    return f"list_concat(coalesce(source_flags, []::VARCHAR[]), {computed})"


def _quality_list_sql() -> str:
    """Build the SQL list literal of the flags that make a record implausible.

    Returns:
        A SQL list of string literals.
    """
    return "[" + ", ".join(_sql_string(flag) for flag in schema.QUALITY_FLAGS) + "]"


def normalize_sql(source: Source, staging_path: Path) -> str:
    """Build the SELECT that turns a staging file into output records.

    Besides the output columns, the SELECT yields two helper columns used for
    the statistics and removed before writing: `drop_reason` (NULL for a kept
    record) and `barcode_given`.

    Args:
        source: The source the staging file belongs to.
        staging_path: Path of the staging Parquet file.

    Returns:
        The SQL SELECT.
    """
    decimals = VALUE_DECIMALS
    rounded = ",\n            ".join(
        f"round({name}, {decimals}) AS {name}"
        for name in NUTRIENTS
        if name not in ("salt_g", "alcohol_g")
    )
    return f"""
    WITH staged AS (
        SELECT * FROM read_parquet({_sql_string(str(staging_path))})
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
            CASE WHEN len(countries) > 0 THEN countries END AS countries,
            popularity,
            source_flags,
            source_url,
            updated_at
        FROM named
    ),
    checked AS (
        SELECT
            *,
            energy_kcal IS NOT NULL AND protein_g IS NOT NULL
                AND carbohydrate_g IS NOT NULL AND fat_g IS NOT NULL AS has_macros
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
        NOT list_has_any(flags, {_quality_list_sql()}) AS plausible,
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
    con.execute(f"CREATE OR REPLACE TEMP TABLE normalized AS {normalize_sql(source, staging_path)}")
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
