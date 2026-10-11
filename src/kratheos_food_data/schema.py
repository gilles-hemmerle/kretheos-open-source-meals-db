"""The common schema: what an adapter must produce and what the build outputs.

Two shapes are defined here.

* The *staging* shape is the contract of the adapters. Every adapter writes one
  Parquet file with exactly these columns, as close to the source as possible:
  no barcode check, no unit derivation, no quality judgement.
* The *output* shape is what the build publishes. It is computed from the
  staging files by `normalize.py`, the same way for every source.

A third, small shape sits beside them: the household portions a source
publishes for its records (`PORTION_COLUMNS`), which the adapters that have
some write to a second staging file.

Keeping them in one module makes the schema reviewable in one place, and lets
the tests check that adapters and outputs never drift from it.
"""

from __future__ import annotations

from dataclasses import dataclass

KIND_GENERIC = "generic"
KIND_PRODUCT = "product"
BASIS_100G = "100g"
BASIS_100ML = "100ml"


@dataclass(frozen=True)
class Column:
    """A column of the staging or output schema.

    Attributes:
        name: Column name.
        type: DuckDB type.
        description: What the column holds.
    """

    name: str
    type: str
    description: str


STAGING_COLUMNS: tuple[Column, ...] = (
    Column("source_dataset", "VARCHAR", "Sub-dataset inside the source, if it has several."),
    Column("source_record_id", "VARCHAR", "Identifier of the record in the source."),
    Column("kind", "VARCHAR", "generic or product."),
    Column("barcode_raw", "VARCHAR", "Barcode exactly as the source writes it."),
    Column("names", "MAP(VARCHAR, VARCHAR)", "Language code to name, as the source gives them."),
    Column("main_lang", "VARCHAR", "Language the source treats as the main one of the record."),
    Column("brand", "VARCHAR", "Brand, for products."),
    Column("category", "VARCHAR", "Category in the source's own classification."),
    Column("basis", "VARCHAR", "100g or 100ml: what the nutrient values refer to."),
    Column("basis_assumed", "BOOLEAN", "True when the source does not tell g from ml."),
    Column("energy_kcal", "DOUBLE", "Energy in kcal, when the source gives it."),
    Column("energy_kj", "DOUBLE", "Energy in kJ, when the source gives it."),
    Column("protein_g", "DOUBLE", "Protein, g."),
    Column("carbohydrate_g", "DOUBLE", "Carbohydrate as the source gives it, g."),
    Column(
        "carbohydrate_includes_fiber",
        "BOOLEAN",
        "True when the carbohydrate of the source includes dietary fibre.",
    ),
    Column("sugars_g", "DOUBLE", "Sugars, g."),
    Column("fat_g", "DOUBLE", "Fat, g."),
    Column("saturated_fat_g", "DOUBLE", "Saturated fat, g."),
    Column("fiber_g", "DOUBLE", "Dietary fibre, g."),
    Column("salt_g", "DOUBLE", "Salt, g, when the source gives it."),
    Column("sodium_g", "DOUBLE", "Sodium, g, when the source gives it."),
    Column("alcohol_g", "DOUBLE", "Alcohol, g, when the source gives it."),
    Column("alcohol_abv", "DOUBLE", "Alcohol, % vol, when the source gives that instead."),
    Column("polyols_g", "DOUBLE", "Polyols, g; only used by the energy consistency check."),
    Column("serving_size", "DOUBLE", "Size of one serving, in serving_unit."),
    Column("serving_unit", "VARCHAR", "g or ml."),
    Column("serving_text", "VARCHAR", "Serving as the source words it."),
    Column("is_drink", "BOOLEAN", "Whether the source classifies the food as a beverage."),
    Column("countries", "VARCHAR[]", "ISO 3166-1 alpha-2 codes."),
    Column("source_url", "VARCHAR", "Link to the record on the publisher's site."),
    Column("updated_at", "DATE", "Last update of the record in the source."),
    Column("popularity", "BIGINT", "Popularity figure of the source, if any."),
    Column("source_flags", "VARCHAR[]", "Informational flags set by the adapter."),
)

OUTPUT_COLUMNS: tuple[Column, ...] = (
    Column("id", "VARCHAR", "Identifier of the source record: `<source>:<id in the source>`."),
    Column("source", "VARCHAR", "Identifier of the source in `sources.toml`."),
    Column("source_dataset", "VARCHAR", "Sub-dataset inside the source, if it has several."),
    Column("source_record_id", "VARCHAR", "Identifier of the record in the source."),
    Column("kind", "VARCHAR", "`generic` (food-composition table) or `product` (branded item)."),
    Column("barcode", "VARCHAR", "Canonical GTIN (8, 13 or 14 digits); NULL when none is valid."),
    Column("name", "VARCHAR", "Name in the main language of the record."),
    Column("name_lang", "VARCHAR", "Language code of `name`."),
    Column("names", "MAP(VARCHAR, VARCHAR)", "Every name the source gives, by language code."),
    Column("brand", "VARCHAR", "Brand, for products."),
    Column("category", "VARCHAR", "Category in the source's own classification."),
    Column("basis", "VARCHAR", "`100g` or `100ml`: what the nutrient values refer to."),
    Column("energy_kcal", "DOUBLE", "Energy, kcal."),
    Column("protein_g", "DOUBLE", "Protein, g."),
    Column("carbohydrate_g", "DOUBLE", "Available carbohydrate, dietary fibre excluded, g."),
    Column(
        "carbohydrate_total_g",
        "DOUBLE",
        "Carbohydrate of the source when it includes fibre, g; NULL otherwise.",
    ),
    Column("sugars_g", "DOUBLE", "Sugars, g."),
    Column("fat_g", "DOUBLE", "Fat, g."),
    Column("saturated_fat_g", "DOUBLE", "Saturated fat, g."),
    Column("fiber_g", "DOUBLE", "Dietary fibre, g."),
    Column("salt_g", "DOUBLE", "Salt, g."),
    Column("alcohol_g", "DOUBLE", "Alcohol, g."),
    Column("serving_size", "DOUBLE", "Size of one serving, in `serving_unit`."),
    Column("serving_unit", "VARCHAR", "`g` or `ml`."),
    Column("serving_text", "VARCHAR", "Serving as the source words it."),
    Column("is_drink", "BOOLEAN", "Whether the source classifies the food as a beverage."),
    Column(
        "countries",
        "VARCHAR[]",
        "Countries the values apply to, ISO 3166-1 alpha-2, upper case: where a product is"
        " sold, or the country of the authority publishing a generic food.",
    ),
    Column("popularity", "BIGINT", "Popularity figure of the source (Open Food Facts scans)."),
    Column("has_macros", "BOOLEAN", "Energy, protein, carbohydrate and fat are all present."),
    Column("plausible", "BOOLEAN", "No quality flag was raised."),
    Column("flags", "VARCHAR[]", "Derivation and quality flags (see the README)."),
    Column("licence", "VARCHAR", "Licence of the record (SPDX identifier or short name)."),
    Column("source_url", "VARCHAR", "Link to the record on the publisher's site."),
    Column("updated_at", "DATE", "Last update of the record in the source."),
)

# The household portions a source publishes for its records ("1 large = 50 g",
# "1 cup"), several per record. An adapter that has some writes them to a
# second staging file with exactly these columns; nothing is ever guessed.
PORTION_COLUMNS: tuple[Column, ...] = (
    Column("source_record_id", "VARCHAR", "Identifier, in the source, of the record it is of."),
    Column("position", "INTEGER", "Order among the portions of the record, from 1, as published."),
    Column("labels", "MAP(VARCHAR, VARCHAR)", "Language code to label, as the source words it."),
    Column("amount", "DOUBLE", "What one portion weighs or holds, in `unit`."),
    Column("unit", "VARCHAR", "g or ml."),
)
PORTION_UNITS = ("g", "ml")

BARCODE_EXTRA_COLUMNS: tuple[Column, ...] = (
    Column("product_id", "VARCHAR", "Identifier of the product itself: `gtin:<barcode>`."),
    Column("source_count", "INTEGER", "Number of sources that know the barcode."),
    Column("sources", "VARCHAR[]", "Identifiers of the sources that know the barcode."),
    Column("other_records", "VARCHAR[]", "Ids of the other records with the same barcode."),
    Column("sources_agree", "BOOLEAN", "Whether sources agree on energy and macros."),
)

# Flags that only say how a value was obtained. They never make a record
# implausible.
FLAG_ENERGY_FROM_KJ = "energy_from_kj"
FLAG_SALT_FROM_SODIUM = "salt_from_sodium"
FLAG_ALCOHOL_FROM_ABV = "alcohol_from_abv"
FLAG_BASIS_ASSUMED = "basis_assumed"
FLAG_CARBOHYDRATE_FIBER_SUBTRACTED = "carbohydrate_fiber_subtracted"
FLAG_CARBOHYDRATE_INCLUDES_FIBER = "carbohydrate_includes_fiber"
FLAG_INEXACT_VALUES = "inexact_values"
FLAG_OBSOLETE = "obsolete"
FLAG_BARCODE_INVALID = "barcode_invalid"
FLAG_BARCODE_RESTRICTED = "barcode_restricted"

# Flags raised by the quality checks. Any of them makes a record implausible.
FLAG_VALUE_OUT_OF_RANGE = "value_out_of_range"
FLAG_ENERGY_OUT_OF_RANGE = "energy_out_of_range"
FLAG_SUGARS_EXCEED_CARBOHYDRATE = "sugars_exceed_carbohydrate"
FLAG_SATURATED_EXCEED_FAT = "saturated_exceed_fat"
FLAG_MACROS_SUM_OVER_100 = "macros_sum_over_100"
FLAG_ENERGY_MACRO_MISMATCH = "energy_macro_mismatch"

INFO_FLAGS: dict[str, str] = {
    FLAG_ENERGY_FROM_KJ: "Energy was only given in kJ; kcal = kJ / 4.184.",
    FLAG_SALT_FROM_SODIUM: "Salt was not given; salt = sodium x 2.5.",
    FLAG_ALCOHOL_FROM_ABV: "Alcohol was given in % vol; g = % vol x 0.789.",
    FLAG_BASIS_ASSUMED: "The source does not tell 100 g from 100 ml; 100 g was assumed.",
    FLAG_CARBOHYDRATE_FIBER_SUBTRACTED: "The source gives carbohydrate with fibre included;"
    " fibre was subtracted (the source figure is in carbohydrate_total_g).",
    FLAG_CARBOHYDRATE_INCLUDES_FIBER: "The source gives carbohydrate with fibre included and no"
    " fibre value: the figure could not be converted and still includes fibre.",
    FLAG_INEXACT_VALUES: "A value was given as 'traces' (kept as 0) or '< x' (kept as x).",
    FLAG_OBSOLETE: "The source marks the product as no longer sold.",
    FLAG_BARCODE_INVALID: "The source has a barcode that is not a valid GTIN; it was dropped.",
    FLAG_BARCODE_RESTRICTED: "The barcode is an in-store number, not unique across retailers.",
}

QUALITY_FLAGS: dict[str, str] = {
    FLAG_VALUE_OUT_OF_RANGE: "A nutrient is below 0 or above 100 per 100 g or 100 ml.",
    FLAG_ENERGY_OUT_OF_RANGE: "Energy is below 0 or above 905 kcal per 100 g or 100 ml.",
    FLAG_SUGARS_EXCEED_CARBOHYDRATE: "Sugars exceed carbohydrate by more than 1 g.",
    FLAG_SATURATED_EXCEED_FAT: "Saturated fat exceeds fat by more than 1 g.",
    FLAG_MACROS_SUM_OVER_100: "Protein + carbohydrate + fat exceed 105 g.",
    FLAG_ENERGY_MACRO_MISMATCH: "Energy does not match the macros (Atwater factors).",
}


def staging_names() -> list[str]:
    """Return the staging column names, in order.

    Returns:
        The names of `STAGING_COLUMNS`.
    """
    return [column.name for column in STAGING_COLUMNS]


def output_names() -> list[str]:
    """Return the output column names, in order.

    Returns:
        The names of `OUTPUT_COLUMNS`.
    """
    return [column.name for column in OUTPUT_COLUMNS]


def staging_ddl(table: str) -> str:
    """Build the CREATE TABLE statement of a staging table.

    Args:
        table: Name of the table to create.

    Returns:
        The SQL statement.
    """
    columns = ", ".join(f'"{column.name}" {column.type}' for column in STAGING_COLUMNS)
    return f"CREATE OR REPLACE TABLE {table} ({columns})"


def staging_cast_select(relation: str) -> str:
    """Build a SELECT that casts a relation to the exact staging schema.

    The relation must expose every staging column by name; the SELECT fixes
    their order and types, so that every adapter writes identical files.

    Args:
        relation: Table or sub-query (already parenthesised and aliased).

    Returns:
        The SQL SELECT.
    """
    columns = ", ".join(
        f'CAST("{column.name}" AS {column.type}) AS "{column.name}"' for column in STAGING_COLUMNS
    )
    return f"SELECT {columns} FROM {relation}"


def portion_names() -> list[str]:
    """Return the column names of a staging file of portions, in order.

    Returns:
        The names of `PORTION_COLUMNS`.
    """
    return [column.name for column in PORTION_COLUMNS]


def portion_cast_select(relation: str) -> str:
    """Build a SELECT that casts a relation to the exact schema of the portions.

    Args:
        relation: Table or sub-query (already parenthesised and aliased)
            exposing every column of `PORTION_COLUMNS` by name.

    Returns:
        The SQL SELECT, in the order of the records and of their portions.
    """
    columns = ", ".join(
        f'CAST("{column.name}" AS {column.type}) AS "{column.name}"' for column in PORTION_COLUMNS
    )
    return f"SELECT {columns} FROM {relation} ORDER BY source_record_id, position"
