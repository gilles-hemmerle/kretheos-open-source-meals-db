"""Adapter for the nutrition values restaurant chains publish on their sites.

This source is not downloaded. It reads whatever CSV files are committed in
`sources/chains/`, one per chain and country (`burgerking_ch.csv`), and is
simply empty when the directory holds none. Whether the values of a chain
may be published is decided chain by chain by whoever commits its file.

Columns read (a file may have more, they are ignored):

* `chain`, `country`, `language`, `product_name`, `category`, `product_key`;
* `portion_size`, `portion_unit` (`g` or `ml`), `is_drink` (`yes` or `no`);
* `energy_kcal_per_100`, `protein_g_per_100`, `carbohydrate_g_per_100`,
  `sugars_g_per_100`, `fat_g_per_100`, `saturated_fat_g_per_100`,
  `fibre_g_per_100`, `salt_g_per_100`: per 100 g, or per 100 ml when the
  portion is in ml;
* `source_url` (the page the values were read on) and `date_collected`.

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
  the United States and Canada where menus give total carbohydrate.

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
    "source_url",
    "date_collected",
)
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


def read_file(path: Path) -> list[dict]:
    """Read one file of a chain as staging rows.

    Args:
        path: Path of the CSV file.

    Returns:
        One row per line that names a chain, a country and a product, with an
        extra key `_from_name` telling how it is identified.

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
    for line in lines:
        chain = clean_text(line["chain"])
        country = (clean_text(line["country"]) or "").upper()
        language = (clean_text(line["language"]) or "").lower()
        name = clean_text(line["product_name"])
        if not chain or not name or not language or not (len(country) == 2 and country.isalpha()):
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
            "source_url": clean_text(line["source_url"]),
            "updated_at": parse_date(line["date_collected"]),
            "_from_name": from_name,
        }
        for column, target in VALUE_COLUMNS.items():
            staged[target] = parse_number(line[column])
        rows.append(staged)
    return rows


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
        The number of files and rows, and the most recent collection date.
    """
    rows: list[dict] = []
    per_file: dict[str, int] = {}
    for name in sorted(context.files):
        read = read_file(context.files[name])
        per_file[name] = len(read)
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
            "repeated_products_skipped": repeated,
            "products_identified_by_name": from_name,
            "rows_staged": written,
        },
    )
