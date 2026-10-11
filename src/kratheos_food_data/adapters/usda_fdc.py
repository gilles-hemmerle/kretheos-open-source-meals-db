"""Adapter for USDA FoodData Central (CSV downloads).

Four datasets of FoodData Central are read, each from its own zip archive:

* `foundation`: Foundation Foods, analysed generic foods;
* `sr_legacy`: SR Legacy, the historical generic table (final release 2018),
  which holds fast-food and restaurant items;
* `fndds`: the Food and Nutrient Database for Dietary Studies, generic foods
  "as eaten", many of them described as coming from fast food or restaurants;
* `branded`: Branded Foods, label data with a GTIN / UPC barcode.

All four share the same tables (`food.csv`, `food_nutrient.csv`); values are
per 100 g, or per 100 ml for branded foods whose serving is given in ml.

Carbohydrate is "by difference" (what is left of 100 g after water, protein,
fat, ash and alcohol), which includes dietary fibre, and Branded Foods carries
the total carbohydrate of the label, fibre included as well. The adapter says
so, and the normalisation subtracts the fibre. The few branded foods of the
New Zealand market are labelled with available carbohydrate and left as is.

Identifiers. FoodData Central gives a food a new `fdc_id` every time it is
updated, so `fdc_id` cannot identify a record across releases. The identifier
that survives is used instead, prefixed with the dataset because the datasets
number their foods independently (211 NDB numbers exist both in Foundation
Foods and in SR Legacy):

* `foundation:<NDB number>` and `sr_legacy:<NDB number>`: the NDB number of
  `foundation_food.csv` and `sr_legacy_food.csv`. Checked on the releases of
  2023-04, 2025-04 and 2026-04: every NDB number of an older release is still
  there, including for the foods whose `fdc_id` changed in between;
* `fndds:<food code>`: the eight-digit FNDDS food code. Between the releases
  of 2022-10 and 2024-10 every `fdc_id` changed while 5 389 of 5 624 food
  codes are still there (the others were retired);
* `branded:<barcode>`: the canonical GTIN, or the barcode text as written
  when it is not a valid GTIN.

The `fdc_id` of the release that was read only appears in the record link.

Three things real data imposes:

* the nutrient identifiers differ: FNDDS writes the legacy nutrient numbers
  (208 for energy) where the other datasets write FoodData Central ids (1008).
  The two ranges do not overlap, so one mapping accepts both;
* Branded Foods keeps every past version of a product: two million rows for
  fewer than half a million barcodes. Only the latest version of each barcode
  is kept (latest publication date, then highest FDC id); the superseded
  versions are counted and left out;
* Foundation Foods does the same on a small scale: `food.csv` still lists the
  earlier versions of the foods that were updated. Only the foods listed in
  `foundation_food.csv`, the current ones, are kept.
"""

from __future__ import annotations

from pathlib import Path

from .. import schema
from ..config import Source
from .base import AdapterContext, AdapterResult, extract_members, sql_string, write_staging_query

FOOD_URL = "https://fdc.nal.usda.gov/food-details/"
BEVERAGE_CATEGORY = "Beverages"
BRANDED_DRINK_PATTERN = r"\b(juice|soda|water|milk|drinks?|beverages?|tea|coffee|alcohol)\b"
BRANDED_NOT_DRINK_PATTERN = r"additives|enhancer|concentrates|powdered|tea bags"

# Archive name -> (dataset label, data_type in food.csv, extra members).
DATASETS = {
    "foundation_food_csv.zip": (
        "foundation",
        "foundation_food",
        ["food_category.csv", "foundation_food.csv"],
    ),
    "sr_legacy_food_csv.zip": (
        "sr_legacy",
        "sr_legacy_food",
        ["food_category.csv", "sr_legacy_food.csv"],
    ),
    "survey_food_csv.zip": (
        "fndds",
        "survey_fndds_food",
        ["wweia_food_category.csv", "survey_fndds_food.csv"],
    ),
    "branded_food_csv.zip": ("branded", "branded_food", ["branded_food.csv"]),
}

# Dataset label -> (table giving the identifier that survives releases, column).
STABLE_KEYS = {
    "foundation": ("foundation_food.csv", "NDB_number"),
    "sr_legacy": ("sr_legacy_food.csv", "NDB_number"),
    "fndds": ("survey_fndds_food.csv", "food_code"),
}

# Staging helper column -> nutrient identifiers (FoodData Central id, then the
# legacy nutrient number used by FNDDS).
NUTRIENT_IDS = {
    "kcal": (1008, 208),
    "kcal_general": (2047,),
    "kcal_specific": (2048,),
    "kj": (1062, 268),
    "protein": (1003, 203),
    "fat": (1004, 204),
    "carbohydrate": (1005, 205),
    "carbohydrate_by_summation": (1050,),
    "sugars": (2000, 269),
    "sugars_other": (1063,),
    "saturated": (1258, 606),
    "fiber": (1079, 291),
    "fiber_other": (2033,),
    "sodium_mg": (1093, 307),
    "salt_mg": (1149,),
    "alcohol": (1018, 221),
    "polyols": (1086,),
}

COUNTRY_CODES = {
    "united states": "US",
    "us": "US",
    "new zealand": "NZ",
    "nz": "NZ",
    "canada": "CA",
    "ca": "CA",
}
# Market countries whose labels give available carbohydrate, fibre excluded
# (Australia New Zealand Food Standards Code, Standard 1.2.8). Everywhere else
# Branded Foods carries the total carbohydrate of North American labels.
AVAILABLE_CARBOHYDRATE = {"NZ"}


def csv_relation(path: Path) -> str:
    """Build a `read_csv` call reading every column as text.

    Types are cast explicitly by the queries, so that a malformed cell makes a
    NULL instead of failing the whole file.

    Args:
        path: Path of the CSV file.

    Returns:
        A SQL table expression.
    """
    return f"read_csv({sql_string(path)}, header = TRUE, all_varchar = TRUE)"


def nutrient_pivot_sql(food_nutrient: Path) -> str:
    """Build the SELECT giving one row per food with the nutrients needed.

    Args:
        food_nutrient: Path of `food_nutrient.csv`.

    Returns:
        A SELECT with `fdc_id` and one column per key of `NUTRIENT_IDS`.
    """
    wanted = sorted({nutrient for ids in NUTRIENT_IDS.values() for nutrient in ids})
    columns = ",\n            ".join(
        f"max(amount) FILTER (WHERE nutrient_id IN ({', '.join(map(str, ids))})) AS {name}"
        for name, ids in NUTRIENT_IDS.items()
    )
    return f"""
        SELECT
            fdc_id,
            {columns}
        FROM (
            SELECT
                fdc_id,
                try_cast(nutrient_id AS INTEGER) AS nutrient_id,
                try_cast(amount AS DOUBLE) AS amount
            FROM {csv_relation(food_nutrient)}
        )
        WHERE nutrient_id IN ({", ".join(map(str, wanted))})
        GROUP BY fdc_id
    """


NUTRIENT_COLUMNS_SQL = """
    coalesce(n.kcal, n.kcal_general, n.kcal_specific) AS energy_kcal,
    n.kj AS energy_kj,
    n.protein AS protein_g,
    coalesce(n.carbohydrate, n.carbohydrate_by_summation) AS carbohydrate_g,
    {includes_fiber} AS carbohydrate_includes_fiber,
    coalesce(n.sugars, n.sugars_other) AS sugars_g,
    n.fat AS fat_g,
    n.saturated AS saturated_fat_g,
    coalesce(n.fiber, n.fiber_other) AS fiber_g,
    n.salt_mg / 1000 AS salt_g,
    n.sodium_mg / 1000 AS sodium_g,
    n.alcohol AS alcohol_g,
    NULL AS alcohol_abv,
    n.polyols AS polyols_g
"""


def generic_sql(dataset: str, data_type: str, files: dict[str, Path], country: str) -> str:
    """Build the staging SELECT of a generic dataset.

    Args:
        dataset: Dataset label: foundation, sr_legacy or fndds.
        data_type: Value of `data_type` selecting the foods in `food.csv`.
        files: Extracted files of the dataset, by base name.
        country: Country whose foods the tables describe.

    Returns:
        The SQL SELECT.
    """
    if dataset == "fndds":
        category_join = f"""
            LEFT JOIN (
                SELECT wweia_food_category AS id, wweia_food_category_description AS description
                FROM {csv_relation(files["wweia_food_category.csv"])}
            ) AS c ON c.id = f.food_category_id
        """
        code = "try_cast(f.food_category_id AS INTEGER)"
        is_drink = f"""
            CASE
                WHEN {code} IS NULL THEN NULL
                ELSE ({code} BETWEEN 1000 AND 1499) OR ({code} BETWEEN 7000 AND 7999)
                    OR {code} IN (1902, 9202, 9204, 9402, 9404, 9602)
            END
        """
    else:
        category_join = f"""
            LEFT JOIN (
                SELECT id, description FROM {csv_relation(files["food_category.csv"])}
            ) AS c ON c.id = f.food_category_id
        """
        is_drink = (
            f"CASE WHEN c.description IS NULL THEN NULL"
            f" ELSE c.description = '{BEVERAGE_CATEGORY}' END"
        )
    key_table, key_column = STABLE_KEYS[dataset]
    return f"""
        SELECT
            '{dataset}' AS source_dataset,
            '{dataset}:' || trim(k.{key_column}) AS source_record_id,
            '{schema.KIND_GENERIC}' AS kind,
            NULL AS barcode_raw,
            MAP {{'en': trim(f.description)}} AS names,
            'en' AS main_lang,
            NULL AS brand,
            c.description AS category,
            '{schema.BASIS_100G}' AS basis,
            FALSE AS basis_assumed,
            {NUTRIENT_COLUMNS_SQL.format(includes_fiber="TRUE")},
            NULL AS serving_size,
            NULL AS serving_unit,
            NULL AS serving_text,
            {is_drink} AS is_drink,
            ['{country}'] AS countries,
            '{FOOD_URL}' || f.fdc_id || '/nutrients' AS source_url,
            try_cast(f.publication_date AS DATE) AS updated_at,
            NULL AS popularity,
            NULL AS source_flags
        FROM {csv_relation(files["food.csv"])} AS f
        JOIN (
            SELECT fdc_id, {key_column} FROM {csv_relation(files[key_table])}
            WHERE nullif(trim({key_column}), '') IS NOT NULL
            QUALIFY row_number() OVER (
                PARTITION BY trim({key_column}) ORDER BY try_cast(fdc_id AS BIGINT) DESC
            ) = 1
        ) AS k ON k.fdc_id = f.fdc_id
        LEFT JOIN ({nutrient_pivot_sql(files["food_nutrient.csv"])}) AS n ON n.fdc_id = f.fdc_id
        {category_join}
        WHERE f.data_type = '{data_type}' AND nullif(trim(f.description), '') IS NOT NULL
    """


def branded_sql(files: dict[str, Path]) -> str:
    """Build the staging SELECT of Branded Foods, latest version per barcode.

    Versions are grouped by canonical barcode; rows whose barcode is not a
    valid GTIN are grouped by the barcode text as written (spaces removed).
    That key is also the identifier of the record: FoodData Central gives a
    product a new `fdc_id` at each update, the barcode does not change. A row
    without any barcode text has nothing stable to be identified by and is
    left out.

    Args:
        files: Extracted files of the dataset, by base name.

    Returns:
        The SQL SELECT.
    """
    country_cases = " ".join(
        f"WHEN '{name}' THEN ['{code}']" for name, code in COUNTRY_CODES.items()
    )
    available = ", ".join(
        f"'{name}'" for name, code in COUNTRY_CODES.items() if code in AVAILABLE_CARBOHYDRATE
    )
    total_carbohydrate = f"lower(trim(b.market_country)) NOT IN ({available})"
    return f"""
        WITH latest AS (
            SELECT
                b.*,
                f.description,
                f.publication_date,
                lower(trim(b.serving_size_unit)) AS unit,
                coalesce(
                    gtin_normalize(b.gtin_upc),
                    regexp_replace(trim(b.gtin_upc), '\\s', '', 'g')
                ) AS product_key
            FROM {csv_relation(files["branded_food.csv"])} AS b
            JOIN {csv_relation(files["food.csv"])} AS f ON f.fdc_id = b.fdc_id
            WHERE nullif(trim(f.description), '') IS NOT NULL
              AND nullif(trim(b.gtin_upc), '') IS NOT NULL
            QUALIFY row_number() OVER (
                PARTITION BY coalesce(
                    gtin_normalize(b.gtin_upc),
                    regexp_replace(trim(b.gtin_upc), '\\s', '', 'g')
                )
                ORDER BY try_cast(f.publication_date AS DATE) DESC NULLS LAST,
                    try_cast(b.fdc_id AS BIGINT) DESC
            ) = 1
        )
        SELECT
            'branded' AS source_dataset,
            'branded:' || b.product_key AS source_record_id,
            '{schema.KIND_PRODUCT}' AS kind,
            b.gtin_upc AS barcode_raw,
            MAP {{'en': trim(b.description)}} AS names,
            'en' AS main_lang,
            coalesce(nullif(trim(b.brand_name), ''), nullif(trim(b.brand_owner), '')) AS brand,
            b.branded_food_category AS category,
            CASE WHEN b.unit IN ('ml', 'mlt') THEN '{schema.BASIS_100ML}'
                 ELSE '{schema.BASIS_100G}' END AS basis,
            b.unit IS NULL OR b.unit NOT IN ('ml', 'mlt', 'g', 'grm', 'gm') AS basis_assumed,
            {NUTRIENT_COLUMNS_SQL.format(includes_fiber=total_carbohydrate)},
            CASE WHEN b.unit IN ('ml', 'mlt', 'g', 'grm', 'gm')
                 THEN try_cast(b.serving_size AS DOUBLE) END AS serving_size,
            CASE WHEN b.unit IN ('ml', 'mlt') THEN 'ml'
                 WHEN b.unit IN ('g', 'grm', 'gm') THEN 'g' END AS serving_unit,
            b.household_serving_fulltext AS serving_text,
            CASE
                WHEN b.branded_food_category IS NULL THEN NULL
                ELSE b.unit IN ('ml', 'mlt')
                    AND regexp_matches(lower(b.branded_food_category), '{BRANDED_DRINK_PATTERN}')
                    AND NOT regexp_matches(
                        lower(b.branded_food_category), '{BRANDED_NOT_DRINK_PATTERN}')
            END AS is_drink,
            CASE lower(trim(b.market_country)) {country_cases} END AS countries,
            '{FOOD_URL}' || b.fdc_id || '/nutrients' AS source_url,
            coalesce(
                try_cast(b.modified_date AS DATE),
                try_cast(b.available_date AS DATE),
                try_cast(b.publication_date AS DATE)
            ) AS updated_at,
            NULL AS popularity,
            CASE WHEN try_cast(b.discontinued_date AS DATE) IS NOT NULL
                 THEN ['{schema.FLAG_OBSOLETE}'] END AS source_flags
        FROM latest AS b
        LEFT JOIN ({nutrient_pivot_sql(files["food_nutrient.csv"])}) AS n ON n.fdc_id = b.fdc_id
    """


def build(source: Source, context: AdapterContext) -> AdapterResult:
    """Write the staging file of USDA FoodData Central.

    Args:
        source: The USDA entry of the source list.
        context: The adapter context.

    Returns:
        The row counts per dataset and the number of superseded branded
        versions left out.
    """
    con = context.con
    selects = []
    stats: dict = {}
    for archive_name, (dataset, data_type, extra) in DATASETS.items():
        members = ["food.csv", "food_nutrient.csv", *extra]
        context.log(f"  extracting {archive_name}")
        files = extract_members(context.file(archive_name), members, context.work_dir / dataset)
        rows = con.execute(
            f"SELECT count(*) FROM {csv_relation(files['food.csv'])} WHERE data_type = ?",
            [data_type],
        ).fetchone()[0]
        stats[f"{dataset}_rows_in_source"] = rows
        if dataset == "branded":
            selects.append(branded_sql(files))
        else:
            selects.append(generic_sql(dataset, data_type, files, source.country))
    union = "\nUNION ALL BY NAME\n".join(f"({select})" for select in selects)
    written = write_staging_query(context, union)
    staged = dict(
        con.execute(
            f"SELECT source_dataset, count(*) FROM read_parquet({sql_string(context.staging_path)})"
            " GROUP BY 1"
        ).fetchall()
    )
    for dataset in ("foundation", "sr_legacy", "fndds", "branded"):
        left_out = stats[f"{dataset}_rows_in_source"] - staged.get(dataset, 0)
        stats[f"{dataset}_versions_or_unidentified_left_out"] = left_out
    stats["rows_staged"] = written
    return AdapterResult(stats=stats)
