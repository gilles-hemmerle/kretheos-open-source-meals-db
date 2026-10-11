"""Adapter for the nutrition values restaurant chains publish for their products.

This source is not downloaded. It reads whatever CSV files are committed in
`sources/chains/`, one per chain and country (`burgerking_ch.csv`), and is
simply empty when the directory holds none. The files are compiled by
Kratheos from what each chain publishes, and the source is presented under
that name: a record carries no link to the page of a chain.

Columns read (a file may have more, they are ignored):

* `chain`, `country`, `language`, `product_name`, `category`, `product_key`;
* `portion_size`, `portion_unit` (`g` or `ml`), `is_drink` (`yes` or `no`);
* `energy_kcal_per_100`, `protein_g_per_100`, `carbohydrate_g_per_100`,
  `sugars_g_per_100`, `fat_g_per_100`, `saturated_fat_g_per_100`,
  `fibre_g_per_100`, `salt_g_per_100`: per 100 g, or per 100 ml when the
  portion is in ml;
* `energy_kcal_per_portion` and the seven other `..._per_portion` columns,
  when the file has them: only to recognise a row that gives its values per
  portion and nothing per 100;
* `date_collected`, when the file has it: the day the values were read.

`source_url`, when a file has it, is where the values of the row were read.
It stays in the file, which is the trace of the collection, and is not
carried into the database.

What becomes of a row:

* a product without a barcode, whose brand is the chain, sold in the country
  of the file, named in the language of the file;
* its identifier is `chain:<chain>:<country>:<key>`: the chain and the
  country in lower case, and the key the site gives the product. A site that
  gives no key leaves the name: the product is then identified by its name,
  written in lower case without accents, and keeps its identifier as long as
  the chain keeps the name;
* the same key in two files of the same chain and country (one per language)
  is one product with two names;
* carbohydrate is available carbohydrate, as on European menus, except in
  the United States and Canada where menus give total carbohydrate;
* a row whose values are given per portion only, the chain publishing no
  weight for that portion, cannot be expressed per 100 g: it is left out and
  counted, per file. No weight is ever guessed.

Nothing is corrected: a row whose values make no sense is kept and flagged by
the same quality checks as every other source.
"""

from __future__ import annotations

import csv
import hashlib
import re
import unicodedata
from datetime import date
from pathlib import Path

from .. import schema
from ..config import Source
from .base import AdapterContext, AdapterResult, clean_text, write_staging_rows

REQUIRED_COLUMNS = (
    "chain",
    "country",
    "language",
    "product_name",
    "category",
    "product_key",
    "portion_size",
    "portion_unit",
    "is_drink",
    "energy_kcal_per_100",
    "protein_g_per_100",
    "carbohydrate_g_per_100",
    "sugars_g_per_100",
    "fat_g_per_100",
    "saturated_fat_g_per_100",
    "fibre_g_per_100",
    "salt_g_per_100",
)
# Read when a file has them; a file without them is complete all the same.
DATE_COLUMN = "date_collected"
# Column of the file -> staging column.
VALUE_COLUMNS = {
    "energy_kcal_per_100": "energy_kcal",
    "protein_g_per_100": "protein_g",
    "carbohydrate_g_per_100": "carbohydrate_g",
    "sugars_g_per_100": "sugars_g",
    "fat_g_per_100": "fat_g",
    "saturated_fat_g_per_100": "saturated_fat_g",
    "fibre_g_per_100": "fiber_g",
    "salt_g_per_100": "salt_g",
}
# Columns of the values per portion, in a file that has them.
PORTION_COLUMNS = tuple(column.replace("_per_100", "_per_portion") for column in VALUE_COLUMNS)
UNITS = ("g", "ml")
TRUE_WORDS = {"yes", "true", "1", "oui", "ja"}
FALSE_WORDS = {"no", "false", "0", "non", "nein"}
# Menus of these countries give total carbohydrate, dietary fibre included.
TOTAL_CARBOHYDRATE_COUNTRIES = {"US", "CA"}
# The public identifier is `chain:<record id>` and may not exceed 80 characters.
MAX_RECORD_ID_LENGTH = 74
_KEY_PATTERN = re.compile(r"[A-Za-z0-9._-]+")
_NOT_SLUG = re.compile(r"[^a-z0-9]+")


def slug(text: str) -> str:
    """Write a text in lower case, without accents, words joined by dashes.

    Args:
        text: Any text.

    Returns:
        The text reduced to `a-z`, `0-9` and single dashes; empty when the
        text holds no letter or digit.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    plain = "".join(char for char in decomposed if not unicodedata.combining(char))
    return _NOT_SLUG.sub("-", plain.lower()).strip("-")


def record_id(chain: str, country: str, key: str | None, name: str) -> tuple[str, bool]:
    """Build the identifier of a product inside the source.

    Args:
        chain: Name of the chain.
        country: Country of the file, ISO 3166-1 alpha-2.
        key: Key the site gives the product, if any.
        name: Name of the product, used when the site gives no key.

    Returns:
        The identifier, and whether it is built from the name.
    """
    from_name = not key
    if key and not _KEY_PATTERN.fullmatch(key):
        key = slug(key)
    part = key or slug(name)
    prefix = f"{slug(chain)}:{country.lower()}:"
    room = MAX_RECORD_ID_LENGTH - len(prefix)
    if len(part) > room:
        digest = hashlib.sha1(part.encode("utf-8"), usedforsecurity=False).hexdigest()[:8]
        part = f"{part[: room - 9].rstrip('-')}-{digest}"
    return prefix + part, from_name


def parse_number(cell: str | None) -> float | None:
    """Read a number of the file.

    Args:
        cell: Cell content; a decimal comma is accepted.

    Returns:
        The number, or None when the cell is empty or not a number.
    """
    text = (cell or "").strip().replace(",", ".")
    if not text:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    return value if value == value and abs(value) != float("inf") else None


def parse_flag(cell: str | None) -> bool | None:
    """Read a yes or no cell.

    Args:
        cell: Cell content.

    Returns:
        True, False, or None when the cell says neither.
    """
    word = (cell or "").strip().lower()
    if word in TRUE_WORDS:
        return True
    if word in FALSE_WORDS:
        return False
    return None


def parse_date(cell: str | None) -> str | None:
    """Read an ISO date.

    Args:
        cell: Cell content, `YYYY-MM-DD`.

    Returns:
        The date as text, or None when the cell holds no valid date.
    """
    text = (cell or "").strip()[:10]
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return None


def per_portion_only(line: dict) -> bool:
    """Tell whether a line gives its values per portion and none per 100.

    Such a line is one whose portion has no published weight: a file is
    completed before it is committed, so a portion with a weight always has
    its values per 100 too.

    Args:
        line: A line of a file, by column name.

    Returns:
        True when a value per portion is a number and no value per 100 is.
    """
    if any(parse_number(line.get(column)) is not None for column in VALUE_COLUMNS):
        return False
    return any(parse_number(line.get(column)) is not None for column in PORTION_COLUMNS)


def read_file(path: Path) -> tuple[list[dict], int]:
    """Read one file of a chain as staging rows.

    Args:
        path: Path of the CSV file.

    Returns:
        One row per line that names a chain, a country and a product and can
        be expressed per 100 g or 100 ml, with an extra key `_from_name`
        telling how it is identified; and the number of lines left out
        because their values are per portion only.

    Raises:
        ValueError: When the file lacks one of the columns read.
    """
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = [name for name in REQUIRED_COLUMNS if name not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"{path.name}: missing columns {', '.join(missing)}")
        lines = list(reader)
    rows = []
    left_out = 0
    for line in lines:
        chain = clean_text(line["chain"])
        country = (clean_text(line["country"]) or "").upper()
        language = (clean_text(line["language"]) or "").lower()
        name = clean_text(line["product_name"])
        if not chain or not name or not language or not (len(country) == 2 and country.isalpha()):
            continue
        if per_portion_only(line):
            left_out += 1
            continue
        identifier, from_name = record_id(chain, country, clean_text(line["product_key"]), name)
        unit = (clean_text(line["portion_unit"]) or "").lower()
        size = parse_number(line["portion_size"])
        staged: dict = {
            "source_dataset": None,
            "source_record_id": identifier,
            "kind": schema.KIND_PRODUCT,
            "barcode_raw": None,
            "names": {language: name},
            "main_lang": language,
            "brand": chain,
            "category": clean_text(line["category"]),
            "basis": schema.BASIS_100ML if unit == "ml" else schema.BASIS_100G,
            "basis_assumed": unit not in UNITS,
            "carbohydrate_includes_fiber": country in TOTAL_CARBOHYDRATE_COUNTRIES,
            "serving_size": size if unit in UNITS and size and size > 0 else None,
            "serving_unit": unit if unit in UNITS and size and size > 0 else None,
            "is_drink": parse_flag(line["is_drink"]),
            "countries": [country],
            # Where a row was read stays in the file: see the module docstring.
            "source_url": None,
            "updated_at": parse_date(line.get(DATE_COLUMN)),
            "_from_name": from_name,
        }
        for column, target in VALUE_COLUMNS.items():
            staged[target] = parse_number(line[column])
        rows.append(staged)
    return rows, left_out


def merge_rows(rows: list[dict]) -> tuple[list[dict], int]:
    """Keep one row per product, joining the names of its languages.

    Args:
        rows: Rows of every file, in file order.

    Returns:
        The rows, one per identifier in the order they first appear, and the
        number of rows left out because they repeat a product in a language
        it already has.
    """
    merged: dict[str, dict] = {}
    repeated = 0
    for row in rows:
        known = merged.get(row["source_record_id"])
        if known is None:
            merged[row["source_record_id"]] = row
            continue
        ((language, name),) = row["names"].items()
        if language in known["names"]:
            repeated += 1
        else:
            known["names"][language] = name
    return list(merged.values()), repeated


def build(source: Source, context: AdapterContext) -> AdapterResult:
    """Write the staging file of the restaurant chains.

    Args:
        source: The entry of the source list.
        context: The adapter context, holding every file of `sources/chains/`.

    Returns:
        The number of files and rows, the rows left out because their values
        are per portion only, and the most recent collection date.
    """
    rows: list[dict] = []
    per_file: dict[str, int] = {}
    per_portion: dict[str, int] = {}
    for name in sorted(context.files):
        read, left_out = read_file(context.files[name])
        per_file[name] = len(read)
        if left_out:
            per_portion[name] = left_out
        rows += read
    merged, repeated = merge_rows(rows)
    from_name = sum(1 for row in merged if row.pop("_from_name"))
    for row in rows:
        row.pop("_from_name", None)
    written = write_staging_rows(context, merged)
    dates = sorted(row["updated_at"] for row in rows if row["updated_at"])
    return AdapterResult(
        version=f"{len(per_file)} files" + (f", collected up to {dates[-1]}" if dates else ""),
        released=dates[-1] if dates else None,
        stats={
            "files": per_file,
            "rows_in_files": len(rows),
            "rows_per_portion_only": per_portion,
            "repeated_products_skipped": repeated,
            "products_identified_by_name": from_name,
            "rows_staged": written,
        },
    )
