"""Adapter for the Canadian Nutrient File (Health Canada).

The release is one zip archive of CSV tables (UTF-8): `Food_Name.csv` (foods,
with English and French descriptions), `Nutrient_Amount.csv` (one value per
food and nutrient, per 100 g of edible portion) and `CNF_Food_Group.csv`.
Nutrients are identified by the legacy USDA nutrient numbers (208 = energy in
kcal, 203 = protein...), a good part of the file being derived from USDA data.

Carbohydrate is "total (by difference)" (nutrient 205, INFOODS tag CHOCDF): it
includes dietary fibre. The adapter says so and the normalisation subtracts
the fibre.
"""

from __future__ import annotations

from .. import schema
from ..config import Source
from .base import AdapterContext, AdapterResult, extract_members, sql_string, write_staging_query

FOOD_URL = "https://food-nutrition.canada.ca/cnf-fce/serving-portion?id="
BEVERAGE_GROUP = "Beverages"

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


def build(source: Source, context: AdapterContext) -> AdapterResult:
    """Write the staging file of the Canadian Nutrient File.

    Args:
        source: The Canadian entry of the source list.
        context: The adapter context.

    Returns:
        The row counts.
    """
    archive = context.file(source.files[0].name)
    files = extract_members(
        archive,
        ["Food_Name.csv", "Nutrient_Amount.csv", "CNF_Food_Group.csv"],
        context.work_dir,
    )
    written = write_staging_query(context, staging_sql(files, source.country))
    return AdapterResult(stats={"rows_in_source": written, "rows_staged": written})
