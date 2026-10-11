"""Adapter for the Swiss Food Composition Database (FSVO).

The database is published as four Excel workbooks, one per language (English,
German, French, Italian). They hold the same foods under the same identifiers;
only the names and the category labels are translated. Values are read from
the English workbook, names from all four.

Each workbook has a sheet of generic foods and a small sheet of branded foods
(mineral waters, a few spirits). Branded foods have no barcode; they are
staged as products without one.

Carbohydrate is "Carbohydrates, available": dietary fibre is a column of its
own and is not included.

Cells hold numbers, `n.d.` (no data), `tr.` (traces, kept as 0) or `<x`
(below a limit, kept as x); the last two make the record `inexact_values`.

The public site has no stable page per database identifier (its pages use an
internal number that the workbook does not contain), so the record link is a
search of the site for the exact English name.
"""

from __future__ import annotations

import re
import warnings
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import openpyxl

from .. import schema
from ..config import Source
from .base import AdapterContext, AdapterResult, clean_text, write_staging_rows

LANGUAGE_FILES = {
    "en": "swiss_en.xlsx",
    "de": "swiss_de.xlsx",
    "fr": "swiss_fr.xlsx",
    "it": "swiss_it.xlsx",
}
SEARCH_URL = "https://naehrwertdaten.ch/en/search/#/search-results/"
TITLE_ROW = 0
HEADER_ROW = 2
ID_COLUMN = 0
NAME_COLUMN = 3
# Sheet position -> (dataset label, kind).
SHEETS = {0: ("generic", schema.KIND_GENERIC), 1: ("branded", schema.KIND_PRODUCT)}
VERSION_PATTERN = re.compile(r"V\s*([0-9.]+)\s*\((\d{2})\.(\d{2})\.(\d{4})\)")
DRINK_PREFIXES = ("Non-alcoholic beverages/", "Alcoholic beverages/")
DRINK_CATEGORIES = {
    "Milk and dairy products/Milk",
    "Milk and dairy products/Milk and yoghurt beverages",
}

# Header of the English workbook -> staging column.
VALUE_COLUMNS = {
    "Energy, kilocalories (kcal)": "energy_kcal",
    "Energy, kilojoules (kJ)": "energy_kj",
    "Protein (g)": "protein_g",
    "Carbohydrates, available (g)": "carbohydrate_g",
    "Sugars (g)": "sugars_g",
    "Fat, total (g)": "fat_g",
    "Fatty acids, saturated (g)": "saturated_fat_g",
    "Dietary fibres (g)": "fiber_g",
    "Salt (NaCl) (g)": "salt_g",
    "Sodium (Na) (mg)": "sodium_mg",
    "Alcohol (g)": "alcohol_g",
}


def parse_value(cell: object) -> tuple[float | None, bool]:
    """Read a value cell of the workbook.

    Args:
        cell: Cell content: a number or a text.

    Returns:
        The number (None when missing) and whether it is inexact (`tr.` or an
        upper bound `<x`).
    """
    if cell is None:
        return None, False
    if isinstance(cell, bool):
        return None, False
    if isinstance(cell, (int, float)):
        return float(cell), False
    text = str(cell).strip().lower()
    if text in ("", "n.d.", "nd", "-"):
        return None, False
    if text.startswith("tr"):
        return 0.0, True
    inexact = text.startswith("<")
    try:
        return float(text.lstrip("<").strip().replace(",", ".")), inexact
    except ValueError:
        return None, False


def read_sheets(path: Path) -> list[list[tuple]]:
    """Read the two food sheets of a workbook.

    Args:
        path: Path of the workbook.

    Returns:
        The rows of the generic sheet, then of the branded sheet.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        return [
            list(workbook.worksheets[position].iter_rows(values_only=True)) for position in SHEETS
        ]
    finally:
        workbook.close()


def is_drink(category: str | None) -> bool | None:
    """Tell whether a category string designates a beverage.

    Args:
        category: Category of the English workbook; several categories are
            separated by semicolons.

    Returns:
        None without a category, else whether one of them is a beverage.
    """
    if not category:
        return None
    parts = [part.strip() for part in category.split(";")]
    return any(part.startswith(DRINK_PREFIXES) or part in DRINK_CATEGORIES for part in parts)


def build(source: Source, context: AdapterContext) -> AdapterResult:
    """Write the staging file of the Swiss Food Composition Database.

    Args:
        source: The Swiss entry of the source list.
        context: The adapter context.

    Returns:
        The version and date printed in the workbook, and the row counts.

    Raises:
        ValueError: When the layout of a workbook is not the expected one.
    """
    sheets = {lang: read_sheets(context.file(name)) for lang, name in LANGUAGE_FILES.items()}
    version = released = None
    title = clean_text(sheets["en"][0][TITLE_ROW][0]) or ""
    match = VERSION_PATTERN.search(title)
    if match:
        version = match.group(1)
        released = (
            datetime(int(match.group(4)), int(match.group(3)), int(match.group(2)))
            .date()
            .isoformat()
        )
    updated_at = released or source.released or None

    rows = []
    for position, (dataset, kind) in SHEETS.items():
        localized: dict[str, dict[object, str]] = {}
        for lang, lang_sheets in sheets.items():
            sheet = lang_sheets[position]
            if clean_text(sheet[HEADER_ROW][ID_COLUMN]) != "ID":
                raise ValueError(f"swiss_{lang}.xlsx: unexpected layout of sheet {position}")
            localized[lang] = {
                row[ID_COLUMN]: clean_text(row[NAME_COLUMN])
                for row in sheet[HEADER_ROW + 1 :]
                if row[ID_COLUMN] is not None and clean_text(row[NAME_COLUMN])
            }
        english = sheets["en"][position]
        header = [clean_text(cell) for cell in english[HEADER_ROW]]
        try:
            index = {column: header.index(label) for label, column in VALUE_COLUMNS.items()}
            category_index = header.index("Category")
            unit_index = header.index("Matrix unit")
        except ValueError as error:
            raise ValueError(f"swiss_en.xlsx: missing column ({error})") from error
        for row in english[HEADER_ROW + 1 :]:
            record_id = row[ID_COLUMN]
            if record_id is None:
                continue
            names = {
                lang: localized[lang][record_id]
                for lang in LANGUAGE_FILES
                if record_id in localized[lang]
            }
            category = clean_text(row[category_index])
            unit = (clean_text(row[unit_index]) or "").lower()
            staged: dict = {
                "source_dataset": dataset,
                "source_record_id": str(record_id),
                "kind": kind,
                "names": names,
                "main_lang": "en",
                "category": category,
                "basis": schema.BASIS_100ML if "ml" in unit else schema.BASIS_100G,
                "basis_assumed": not unit,
                "carbohydrate_includes_fiber": False,
                "countries": [source.country] if source.country else None,
                "is_drink": is_drink(category),
                "source_url": SEARCH_URL + quote(names.get("en", ""), safe="") if names else None,
                "updated_at": updated_at,
            }
            inexact = False
            for column, column_index in index.items():
                value, flagged = parse_value(row[column_index])
                staged[column] = value
                inexact = inexact or (flagged and value is not None)
            sodium_mg = staged.pop("sodium_mg")
            staged["sodium_g"] = None if sodium_mg is None else sodium_mg / 1000
            if inexact:
                staged["source_flags"] = [schema.FLAG_INEXACT_VALUES]
            rows.append(staged)
    written = write_staging_rows(context, rows)
    return AdapterResult(
        version=version,
        released=released,
        stats={"rows_in_source": len(rows), "rows_staged": written},
    )
