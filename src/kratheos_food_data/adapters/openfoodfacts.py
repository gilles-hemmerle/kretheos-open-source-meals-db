"""Adapter for Open Food Facts (Parquet export published on Hugging Face).

Reads `food.parquet` (one row per product, about 4.8 million rows, 8 GB)
straight from disk with DuckDB: only the needed columns are decoded, the file
is never loaded whole.

What is taken: the barcode (`code`), the product names in every language, the
brands, the most specific category of the Open Food Facts taxonomy, the
nutrient values per 100 g or 100 ml, the serving size, the countries of sale,
the date of last modification and the number of distinct scans.

What is never read: any column naming a contributor (`creator`, `editors`,
`last_editor`, `last_modified_by`, `informers_tags`, `correctors_tags`,
`checkers_tags`, `photographers`, `owner`...) and the images.

Open Food Facts does not record whether a label is per 100 g or per 100 ml.
The basis is inferred from the unit of the package quantity, then from the
unit of the serving size; when neither is known, 100 g is assumed and flagged.

Nor does it record what a label means by carbohydrate, and labels differ:
those of the United States and Canada declare total carbohydrate, fibre
included, those of Europe available carbohydrate. The export only has a
separate `carbohydrates-total` value for about 4 000 products. What tells the
two apart is the country: on the fibre-rich products (5 g or more) of the
export of 2026-10-10, energy is best explained by a carbohydrate that
includes fibre for 83 % of the products sold only in the United States and
85 % of those sold only in Canada, and by available carbohydrate for 77 % of
those sold only in France and 82 % in Germany. So the carbohydrate of a
product is taken as including fibre when every country it is sold in is the
United States or Canada (or when only `carbohydrates-total` is given), and as
available carbohydrate otherwise.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from .. import schema
from ..config import Source
from ..download import read_record
from .base import AdapterContext, AdapterResult, sql_string, write_staging_query

PARQUET_NAME = "food.parquet"
COUNTRIES_NAME = "countries.json"
PRODUCT_URL = "https://world.openfoodfacts.org/product/"
UNKNOWN_LANGUAGE = "und"
UNDEFINED_CATEGORY = "en:undefined"
# The taxonomy writes the United Kingdom "UK"; ISO 3166-1 says "GB".
COUNTRY_CODE_FIXES = {"UK": "GB"}
# Countries whose labels declare total carbohydrate, dietary fibre included
# (United States: 21 CFR 101.9; Canada: Food and Drug Regulations B.01.401).
TOTAL_CARBOHYDRATE_COUNTRIES = ("US", "CA")
DRINK_CATEGORIES = ("en:beverages", "en:milks", "en:dairy-drinks")
DRINK_FOOD_GROUP = "en:beverages"
SERVING_UNIT_PATTERN = r"[0-9]\s*(ml|cl|dl|l|fl\.? ?oz|mg|kg|g|oz)\b"
VOLUME_UNITS = ("ml", "cl", "dl", "l", "fl oz", "fl. oz", "floz", "fl.oz")

# Staging column -> name of the nutrient in the `nutriments` list.
NUTRIENTS = {
    "energy_kcal": "energy-kcal",
    "energy_kj_explicit": "energy-kj",
    "energy_kj_generic": "energy",
    "protein_g": "proteins",
    "carbohydrate_g": "carbohydrates",
    "carbohydrate_total_g": "carbohydrates-total",
    "sugars_g": "sugars",
    "fat_g": "fat",
    "saturated_fat_g": "saturated-fat",
    "fiber_g": "fiber",
    "salt_g": "salt",
    "sodium_g": "sodium",
    "alcohol_abv": "alcohol",
    "polyols_g": "polyols",
}


def load_country_codes(context: AdapterContext) -> int:
    """Load the country taxonomy into a `off_countries(tag, iso)` table.

    The taxonomy maps a tag such as `en:france` to its ISO 3166-1 alpha-2
    code. Tags without a two-letter code (world, regions) are left out, and
    the one code that is not the ISO one is replaced.

    Args:
        context: The adapter context.

    Returns:
        The number of country tags loaded.
    """
    with context.file(COUNTRIES_NAME).open(encoding="utf-8") as handle:
        taxonomy = json.load(handle)
    pairs = []
    for tag, entry in taxonomy.items():
        code = (entry.get("country_code_2") or {}).get("en", "")
        if len(code) == 2 and code.isalpha():
            pairs.append((tag, COUNTRY_CODE_FIXES.get(code.upper(), code.upper())))
    context.con.execute("CREATE OR REPLACE TEMP TABLE off_countries (tag VARCHAR, iso VARCHAR)")
    context.con.executemany("INSERT INTO off_countries VALUES (?, ?)", pairs)
    return len(pairs)


def slim_sql(parquet: str) -> str:
    """Build the SELECT that reduces the export to the columns the build needs.

    Nutrients are pulled out of the `nutriments` list, which is by far the
    heaviest column, so that later steps work on a small table. When a barcode
    appears on several rows, only the most recently modified one is kept.

    Args:
        parquet: Path of the Parquet export.

    Returns:
        The SQL SELECT.
    """
    nutrient_columns = ",\n            ".join(
        f"""[x."100g" FOR x IN nutriments IF x.name = '{name}'][1] AS {column}"""
        for column, name in NUTRIENTS.items()
    )
    volume_units = ", ".join(f"'{unit}'" for unit in VOLUME_UNITS)
    drink_categories = ", ".join(f"'{tag}'" for tag in DRINK_CATEGORIES)
    return f"""
    WITH ranked AS (
        SELECT
            file_row_number,
            row_number() OVER (
                PARTITION BY code
                ORDER BY last_modified_t DESC NULLS LAST, file_row_number DESC
            ) AS position
        FROM read_parquet({sql_string(parquet)}, file_row_number = TRUE)
    ),
    superseded AS (
        SELECT file_row_number FROM ranked WHERE position > 1
    ),
    raw AS (
        SELECT
            code,
            lang,
            product_name,
            brands,
            categories_tags,
            food_groups_tags,
            countries_tags,
            serving_size,
            serving_quantity,
            product_quantity_unit,
            nutrition_data_per,
            last_modified_t,
            obsolete,
            unique_scans_n,
            nutriments,
            lower(regexp_extract(lower(serving_size), '{SERVING_UNIT_PATTERN}', 1))
                AS serving_unit_text
        FROM read_parquet({sql_string(parquet)}, file_row_number = TRUE)
        WHERE code IS NOT NULL
          AND file_row_number NOT IN (SELECT file_row_number FROM superseded)
    )
    SELECT
        code,
        nullif(trim(lang), '') AS lang,
        product_name,
        brands,
        nullif(
            [tag FOR tag IN categories_tags IF regexp_full_match(tag, 'en:[a-z0-9\\-]+')][-1],
            '{UNDEFINED_CATEGORY}'
        ) AS category,
        CASE
            WHEN coalesce(len(categories_tags), 0) = 0 AND coalesce(len(food_groups_tags), 0) = 0
                THEN NULL
            ELSE list_has_any(coalesce(categories_tags, []::VARCHAR[]), [{drink_categories}])
                OR list_contains(coalesce(food_groups_tags, []::VARCHAR[]), '{DRINK_FOOD_GROUP}')
        END AS is_drink,
        countries_tags,
        nullif(trim(serving_size), '') AS serving_text,
        try_cast(serving_quantity AS DOUBLE) AS serving_size,
        CASE
            WHEN serving_unit_text IS NULL OR serving_unit_text = '' THEN NULL
            WHEN serving_unit_text IN ({volume_units}) THEN 'ml'
            ELSE 'g'
        END AS serving_unit,
        CASE WHEN product_quantity_unit IN ('g', 'ml') THEN product_quantity_unit END
            AS quantity_unit,
        nutrition_data_per = '100ml' AS per_100ml,
        CAST(to_timestamp(last_modified_t) AS DATE) AS updated_at,
        coalesce(obsolete, FALSE) AS obsolete,
        unique_scans_n AS popularity,
        {nutrient_columns}
    FROM raw
    """


STAGING_SQL = f"""
WITH name_entries AS (
    SELECT
        slim.code,
        CASE WHEN entry.lang = 'main' THEN coalesce(slim.lang, '{UNKNOWN_LANGUAGE}')
             ELSE entry.lang END AS lang,
        entry.lang = 'main' AS is_main,
        trim(entry."text") AS name
    FROM off_slim AS slim, unnest(slim.product_name) AS names(entry)
    WHERE entry.lang IS NOT NULL AND trim(entry."text") <> ''
),
name_by_lang AS (
    SELECT
        code,
        lang,
        coalesce(max(name) FILTER (WHERE NOT is_main), max(name) FILTER (WHERE is_main)) AS name
    FROM name_entries
    GROUP BY code, lang
),
names AS (
    SELECT code, map_from_entries(list(struct_pack(k := lang, v := name) ORDER BY lang)) AS names
    FROM name_by_lang
    GROUP BY code
),
countries AS (
    SELECT slim.code, list(DISTINCT known.iso ORDER BY known.iso) AS countries
    FROM off_slim AS slim, unnest(slim.countries_tags) AS tags(tag)
    JOIN off_countries AS known ON known.tag = tags.tag
    GROUP BY slim.code
)
SELECT
    NULL AS source_dataset,
    slim.code AS source_record_id,
    '{schema.KIND_PRODUCT}' AS kind,
    slim.code AS barcode_raw,
    names.names AS names,
    coalesce(slim.lang, '{UNKNOWN_LANGUAGE}') AS main_lang,
    slim.brands AS brand,
    slim.category AS category,
    CASE
        WHEN slim.per_100ml THEN '{schema.BASIS_100ML}'
        WHEN slim.quantity_unit = 'ml' THEN '{schema.BASIS_100ML}'
        WHEN slim.quantity_unit = 'g' THEN '{schema.BASIS_100G}'
        WHEN slim.serving_unit = 'ml' THEN '{schema.BASIS_100ML}'
        ELSE '{schema.BASIS_100G}'
    END AS basis,
    NOT coalesce(slim.per_100ml, FALSE)
        AND slim.quantity_unit IS NULL
        AND slim.serving_unit IS NULL AS basis_assumed,
    slim.energy_kcal AS energy_kcal,
    coalesce(slim.energy_kj_explicit, slim.energy_kj_generic) AS energy_kj,
    slim.protein_g AS protein_g,
    coalesce(slim.carbohydrate_g, slim.carbohydrate_total_g) AS carbohydrate_g,
    (slim.carbohydrate_g IS NULL AND slim.carbohydrate_total_g IS NOT NULL)
        OR (len(countries.countries) > 0
            AND len([sold_in FOR sold_in IN countries.countries
                     IF sold_in NOT IN {TOTAL_CARBOHYDRATE_COUNTRIES}]) = 0)
        AS carbohydrate_includes_fiber,
    slim.sugars_g AS sugars_g,
    slim.fat_g AS fat_g,
    slim.saturated_fat_g AS saturated_fat_g,
    slim.fiber_g AS fiber_g,
    slim.salt_g AS salt_g,
    slim.sodium_g AS sodium_g,
    NULL AS alcohol_g,
    slim.alcohol_abv AS alcohol_abv,
    slim.polyols_g AS polyols_g,
    slim.serving_size AS serving_size,
    slim.serving_unit AS serving_unit,
    slim.serving_text AS serving_text,
    slim.is_drink AS is_drink,
    countries.countries AS countries,
    '{PRODUCT_URL}' || slim.code AS source_url,
    slim.updated_at AS updated_at,
    slim.popularity AS popularity,
    CASE WHEN slim.obsolete THEN ['{schema.FLAG_OBSOLETE}'] END AS source_flags
FROM off_slim AS slim
LEFT JOIN names ON names.code = slim.code
LEFT JOIN countries ON countries.code = slim.code
"""


def build(source: Source, context: AdapterContext) -> AdapterResult:
    """Write the staging file of Open Food Facts.

    Args:
        source: The Open Food Facts entry of the source list.
        context: The adapter context.

    Returns:
        The version of the export (date of the latest change and repository
        revision) and the row counts.
    """
    con = context.con
    parquet = context.file(PARQUET_NAME)
    country_tags = load_country_codes(context)
    total = con.execute(f"SELECT count(*) FROM read_parquet({sql_string(parquet)})").fetchone()[0]
    context.log(f"  {total:,} rows in the export; extracting the needed columns")
    con.execute(f"CREATE OR REPLACE TEMP TABLE off_slim AS {slim_sql(str(parquet))}")
    slim_rows, latest = con.execute("SELECT count(*), max(updated_at) FROM off_slim").fetchone()
    unmapped_uses, unmapped_tags, without_country = con.execute(
        """
        SELECT
            count(*) FILTER (WHERE known.iso IS NULL),
            count(DISTINCT tags.tag) FILTER (WHERE known.iso IS NULL),
            (SELECT count(*) FROM off_slim AS s
             WHERE len(s.countries_tags) > 0 AND NOT EXISTS (
                 SELECT 1 FROM unnest(s.countries_tags) AS t(tag)
                 JOIN off_countries AS k ON k.tag = t.tag))
        FROM off_slim AS slim, unnest(slim.countries_tags) AS tags(tag)
        LEFT JOIN off_countries AS known ON known.tag = tags.tag
        """
    ).fetchone()
    written = write_staging_query(context, STAGING_SQL)
    con.execute("DROP TABLE off_slim")
    con.execute("DROP TABLE off_countries")

    record = read_record(parquet)
    revision = record.revision if record else None
    released = latest.isoformat() if latest else ""
    if not released and record and record.downloaded_at:
        released = datetime.fromisoformat(record.downloaded_at).astimezone(UTC).date().isoformat()
    version = f"export with changes up to {released}"
    if revision:
        version += f" (Hugging Face revision {revision[:12]})"
    return AdapterResult(
        version=version,
        released=released,
        stats={
            "rows_in_source": total,
            "rows_without_code_or_superseded_duplicates": total - slim_rows,
            "rows_staged": written,
            "country_tags_known": country_tags,
            "country_tag_uses_not_mapped_to_a_code": unmapped_uses,
            "distinct_country_tags_not_mapped": unmapped_tags,
            "products_with_tags_but_no_mapped_country": without_country,
        },
    )
