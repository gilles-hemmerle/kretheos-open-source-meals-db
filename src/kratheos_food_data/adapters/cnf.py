"""Adapter for the Canadian Nutrient File (Health Canada).

The release is one zip archive of CSV tables (UTF-8): `Food_Name.csv` (foods,
with English and French descriptions), `Nutrient_Amount.csv` (one value per
food and nutrient, per 100 g of edible portion) and `CNF_Food_Group.csv`.
Nutrients are identified by the legacy USDA nutrient numbers (208 = energy in
kcal, 203 = protein...), a good part of the file being derived from USDA data.

Carbohydrate is "total (by difference)" (nutrient 205, INFOODS tag CHOCDF): it
includes dietary fibre. The adapter says so and the normalisation subtracts
the fibre.

Household portions. `Measure_Weight_Conversion.csv` gives, for each food, what
its measures weigh in grams, and `Measure_Name.csv` words each measure in
English and in French ("1 large egg", "1 oeufs large (gros)"; "250 ml"). Only
the measures a person uses are read (type 6; type 3 is the part that is not
eaten and type 9 a cooking yield), in the order of the file. A measure is
left out, and counted, when it has no weight, no wording, or when its label
is only a weight ("100 g", "50 g"): that is the same for every food. A
volume stays ("250 ml" of a food weighs what the file says).
"""

from __future__ import annotations

import csv
from pathlib import Path

from .. import schema
from ..config import Source
from .base import (
    WEIGHT_ONLY_LABEL,
    AdapterContext,
    AdapterResult,
    extract_members,
    sql_string,
    write_portions_query,
    write_staging_query,
)

FOOD_URL = "https://food-nutrition.canada.ca/cnf-fce/serving-portion?id="
BEVERAGE_GROUP = "Beverages"
MEASURE_NAMES = "Measure_Name.csv"
MEASURE_WEIGHTS = "Measure_Weight_Conversion.csv"
# Measure_Type_Code of a measure a person uses (3: refuse, 9: yield).
HOUSEHOLD_MEASURE_TYPE = "6"
# Why a measure is left out, in the order the reasons are tested.
PORTION_REASONS = ("no_weight", "no_wording", "only_a_weight", "repeated")

# Staging helper column -> nutrient code.
NUTRIENT_CODES = {
    "kcal": 208,
    "kj": 268,
    "protein": 203,
    "fat": 204,
    "carbohydrate": 205,
    "sugars": 269,
    "fiber": 291,
    "saturated": 606,
    "sodium_mg": 307,
    "alcohol": 221,
}


def staging_sql(files: dict, country: str) -> str:
    """Build the staging SELECT of the Canadian Nutrient File.

    Args:
        files: Extracted CSV files, by base name.
        country: Country whose foods the file describes.

    Returns:
        The SQL SELECT.
    """
    pivot = ",\n            ".join(
        f"max(amount) FILTER (WHERE code = {code}) AS {name}"
        for name, code in NUTRIENT_CODES.items()
    )
    codes = ", ".join(str(code) for code in NUTRIENT_CODES.values())

    def csv(name: str) -> str:
        """Build the `read_csv` call of one table, every column as text."""
        return f"read_csv({sql_string(files[name])}, header = TRUE, all_varchar = TRUE)"

    return f"""
        WITH nutrients AS (
            SELECT
                Food_Code AS food_code,
                {pivot}
            FROM (
                SELECT
                    Food_Code,
                    try_cast(Nutrient_Code AS INTEGER) AS code,
                    try_cast(Nutrient_Amount AS DOUBLE) AS amount
                FROM {csv("Nutrient_Amount.csv")}
            )
            WHERE code IN ({codes})
            GROUP BY Food_Code
        ),
        names AS (
            SELECT
                Food_Code,
                nullif(trim(Food_Description_EN), '') AS name_en,
                nullif(trim(Food_Description_FR), '') AS name_fr,
                CNF_Food_Group_Code,
                Food_Last_Updated_Date
            FROM {csv("Food_Name.csv")}
        )
        SELECT
            NULL AS source_dataset,
            f.Food_Code AS source_record_id,
            '{schema.KIND_GENERIC}' AS kind,
            NULL AS barcode_raw,
            CASE
                WHEN f.name_en IS NOT NULL AND f.name_fr IS NOT NULL
                    THEN MAP {{'en': f.name_en, 'fr': f.name_fr}}
                WHEN f.name_en IS NOT NULL THEN MAP {{'en': f.name_en}}
                WHEN f.name_fr IS NOT NULL THEN MAP {{'fr': f.name_fr}}
            END AS names,
            'en' AS main_lang,
            NULL AS brand,
            g.CNF_Food_Group_Description_EN AS category,
            '{schema.BASIS_100G}' AS basis,
            FALSE AS basis_assumed,
            n.kcal AS energy_kcal,
            n.kj AS energy_kj,
            n.protein AS protein_g,
            n.carbohydrate AS carbohydrate_g,
            TRUE AS carbohydrate_includes_fiber,
            n.sugars AS sugars_g,
            n.fat AS fat_g,
            n.saturated AS saturated_fat_g,
            n.fiber AS fiber_g,
            NULL AS salt_g,
            n.sodium_mg / 1000 AS sodium_g,
            n.alcohol AS alcohol_g,
            NULL AS alcohol_abv,
            NULL AS polyols_g,
            NULL AS serving_size,
            NULL AS serving_unit,
            NULL AS serving_text,
            CASE WHEN g.CNF_Food_Group_Description_EN IS NULL THEN NULL
                 ELSE g.CNF_Food_Group_Description_EN = '{BEVERAGE_GROUP}' END AS is_drink,
            ['{country}'] AS countries,
            '{FOOD_URL}' || f.Food_Code AS source_url,
            try_cast(f.Food_Last_Updated_Date AS DATE) AS updated_at,
            NULL AS popularity,
            NULL AS source_flags
        FROM names AS f
        LEFT JOIN nutrients AS n ON n.food_code = f.Food_Code
        LEFT JOIN {csv("CNF_Food_Group.csv")} AS g
            ON g.CNF_Food_Group_Code = f.CNF_Food_Group_Code
        WHERE f.Food_Code IS NOT NULL
    """


def numbered_copy(path: Path, dest: Path) -> Path:
    """Copy a CSV file with the number of each line in a first column.

    The order of the measures of a food is the order of the file, which a
    parallel CSV reader does not keep: the number makes it explicit.

    Args:
        path: The CSV file of the source (UTF-8, with or without a mark).
        dest: Where to write the copy, in the scratch directory.

    Returns:
        The copy, whose first column `line` counts the rows from 1.
    """
    with (
        path.open(encoding="utf-8-sig", newline="") as source,
        dest.open("w", encoding="utf-8", newline="") as out,
    ):
        reader = csv.reader(source)
        writer = csv.writer(out)
        writer.writerow(["line", *next(reader, [])])
        for number, row in enumerate(reader, start=1):
            writer.writerow([number, *row])
    return dest


def portions_sql(names: Path, numbered_weights: Path) -> str:
    """Build the SELECT reading and judging the measures of the foods.

    Args:
        names: `Measure_Name.csv`.
        numbered_weights: `Measure_Weight_Conversion.csv` with its line
            numbers (`numbered_copy`).

    Returns:
        A SELECT with `source_record_id`, `line` (the order of the file),
        `en`, `fr` (the label in each language), `amount` (grams) and
        `reason` (NULL for a measure that is kept).
    """

    def csv_table(path: Path) -> str:
        """Build the `read_csv` call of one table, every column as text."""
        return f"read_csv({sql_string(path)}, header = TRUE, all_varchar = TRUE)"

    def label(column: str) -> str:
        """Build the SQL of a label with its white space collapsed, NULL when empty."""
        return f"nullif(regexp_replace(trim({column}), '\\s+', ' ', 'g'), '')"

    return f"""
        WITH measures AS (
            SELECT
                Measure_Code AS code,
                {label("Measure_Description_and_Unit_EN")} AS en,
                {label("Measure_Description_and_Unit_FR")} AS fr
            FROM {csv_table(names)}
        ),
        read AS (
            SELECT
                w.Food_Code AS source_record_id,
                try_cast(w.line AS BIGINT) AS line,
                m.en,
                m.fr,
                round(try_cast(w.Measure_Weight_Conversion AS DOUBLE), 4) AS amount
            FROM {csv_table(numbered_weights)} AS w
            LEFT JOIN measures AS m ON m.code = w.Measure_Code
            WHERE trim(w.Measure_Type_Code) = '{HOUSEHOLD_MEASURE_TYPE}'
              AND w.Food_Code IS NOT NULL
        ),
        judged AS (
            SELECT
                *,
                CASE
                    WHEN amount IS NULL OR amount <= 0 THEN 'no_weight'
                    WHEN en IS NULL AND fr IS NULL THEN 'no_wording'
                    WHEN regexp_matches(lower(coalesce(en, fr)), '{WEIGHT_ONLY_LABEL}')
                        THEN 'only_a_weight'
                END AS reason
            FROM read
        )
        SELECT
            source_record_id, line, en, fr, amount,
            CASE
                WHEN reason IS NULL AND row_number() OVER (
                    PARTITION BY source_record_id, reason IS NULL, lower(coalesce(en, fr)), amount
                    ORDER BY line
                ) > 1 THEN 'repeated'
                ELSE reason
            END AS reason
        FROM judged
    """


def build(source: Source, context: AdapterContext) -> AdapterResult:
    """Write the staging file of the Canadian Nutrient File, and its portions.

    Args:
        source: The Canadian entry of the source list.
        context: The adapter context.

    Returns:
        The row counts, and the measures read, kept and left out.
    """
    con = context.con
    archive = context.file(source.files[0].name)
    members = ["Food_Name.csv", "Nutrient_Amount.csv", "CNF_Food_Group.csv"]
    files = extract_members(archive, [*members, MEASURE_NAMES, MEASURE_WEIGHTS], context.work_dir)
    written = write_staging_query(context, staging_sql(files, source.country))
    numbered = numbered_copy(files[MEASURE_WEIGHTS], context.work_dir / "measure_weights.csv")
    con.execute(
        "CREATE OR REPLACE TEMP TABLE cnf_portions AS "
        + portions_sql(files[MEASURE_NAMES], numbered)
    )
    write_portions_query(
        context,
        """
        SELECT
            source_record_id,
            row_number() OVER (PARTITION BY source_record_id ORDER BY line) AS position,
            CASE
                WHEN en IS NOT NULL AND fr IS NOT NULL THEN MAP {'en': en, 'fr': fr}
                WHEN en IS NOT NULL THEN MAP {'en': en}
                ELSE MAP {'fr': fr}
            END AS labels,
            amount,
            'g' AS unit
        FROM cnf_portions
        WHERE reason IS NULL
        """,
    )
    in_source, kept, foods = con.execute(
        """
        SELECT count(*), count(*) FILTER (WHERE reason IS NULL),
               count(DISTINCT source_record_id) FILTER (WHERE reason IS NULL)
        FROM cnf_portions
        """
    ).fetchone()
    reasons = dict(
        con.execute(
            "SELECT reason, count(*) FROM cnf_portions WHERE reason IS NOT NULL GROUP BY 1"
        ).fetchall()
    )
    con.execute("DROP TABLE cnf_portions")
    numbered.unlink()
    return AdapterResult(
        stats={
            "rows_in_source": written,
            "rows_staged": written,
            "portions": {
                "in_source": in_source,
                "kept": kept,
                "foods": foods,
                "left_out": {
                    reason: reasons[reason] for reason in PORTION_REASONS if reason in reasons
                },
            },
        }
    )
