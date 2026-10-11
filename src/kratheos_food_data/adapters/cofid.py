"""Adapter for CoFID, the United Kingdom's Composition of Foods Integrated Dataset.

Reads the 2021 workbook of McCance and Widdowson's The Composition of Foods:
sheet `1.3 Proximates` (energy, protein, fat, carbohydrate, sugars, fibre,
saturated fat, alcohol) and sheet `1.4 Inorganics` (sodium). Values are per
100 g, except alcoholic beverages which the dataset gives per 100 ml.

Particularities of this source:

* carbohydrate is available carbohydrate (fibre is reported apart, as AOAC
  fibre or non-starch polysaccharides, and is not part of it);
* carbohydrate and sugars are expressed as monosaccharide equivalents, about
  5 to 10 % above the weight of the same starch or sucrose, and energy is
  computed with 3.75 kcal per gram of carbohydrate;
* `Tr` is a trace (kept as 0, the record is flagged `inexact_values`) and `N`
  means present in significant quantity but not measured (kept as missing);
* fibre is the AOAC value when given, otherwise non-starch polysaccharides;
* the workbook names food groups by code only; their names, copied below,
  come from Appendix B of the user guide published with the dataset;
* there is no page per food on the publisher's site, so every record links
  to the page of the dataset.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import openpyxl

from .. import schema
from ..config import Source
from .base import AdapterContext, AdapterResult, clean_text, write_staging_rows

PROXIMATES_SHEET = "1.3 Proximates"
INORGANICS_SHEET = "1.4 Inorganics"
HEADER_ROW = 0
FIRST_DATA_ROW = 3
ALCOHOLIC_GROUP_PREFIX = "Q"
NOT_DRINK_GROUPS = {"PAA", "BAR"}
DRINK_GROUP_PREFIXES = ("P", "Q", "BA")
DRINK_GROUPS = {"BC", "BH", "FC"}

# Header of the proximates sheet -> staging column.
VALUE_COLUMNS = {
    "Energy (kcal) (kcal)": "energy_kcal",
    "Energy (kJ) (kJ)": "energy_kj",
    "Protein (g)": "protein_g",
    "Carbohydrate (g)": "carbohydrate_g",
    "Total sugars (g)": "sugars_g",
    "Fat (g)": "fat_g",
    "Satd FA /100g fd (g)": "saturated_fat_g",
    "AOAC fibre (g)": "fiber_aoac",
    "NSP (g)": "fiber_nsp",
    "Alcohol (g)": "alcohol_g",
}
SODIUM_COLUMN = "Sodium (mg)"

# Food group codes, from Appendix B of the CoFID 2021 user guide.
GROUP_NAMES = {
    "A": "Cereals and cereal products",
    "AA": "Flours, grains and starches",
    "AB": "Sandwiches",
    "AC": "Rice",
    "AD": "Pasta",
    "AE": "Pizzas",
    "AF": "Breads",
    "AG": "Rolls",
    "AI": "Breakfast cereals",
    "AK": "Infant cereal foods",
    "AM": "Biscuits",
    "AN": "Cakes",
    "AO": "Pastry",
    "AP": "Buns and pastries",
    "AS": "Puddings",
    "AT": "Savouries",
    "B": "Milk and milk products",
    "BA": "Cows milk",
    "BAB": "Breakfast milk",
    "BAE": "Skimmed milk",
    "BAH": "Semi-skimmed milk",
    "BAK": "Whole milk",
    "BAN": "Channel Island milk",
    "BAR": "Processed milks",
    "BC": "Other milks",
    "BF": "Infant formulas",
    "BFD": "Whey-based modified milks",
    "BFG": "Non-whey-based modified milks",
    "BFJ": "Soya-based modified milks",
    "BFP": "Follow-on formulas",
    "BH": "Milk-based drinks",
    "BJ": "Creams",
    "BJC": "Fresh creams (pasteurised)",
    "BJF": "Frozen creams (pasteurised)",
    "BJL": "Sterilised creams",
    "BJP": "UHT creams",
    "BJS": "Imitation creams",
    "BL": "Cheeses",
    "BN": "Yogurts",
    "BNE": "Whole milk yogurts",
    "BNH": "Low fat yogurts",
    "BNS": "Other yogurts",
    "BP": "Ice creams",
    "BR": "Puddings and chilled desserts",
    "BV": "Savoury dishes and sauces",
    "C": "Eggs",
    "CA": "Eggs",
    "CD": "Egg dishes",
    "CDE": "Savoury egg dishes",
    "CDH": "Sweet egg dishes",
    "D": "Vegetables",
    "DA": "Potatoes",
    "DAE": "Early potatoes",
    "DAM": "Main crop potatoes",
    "DAP": "Chipped old potatoes",
    "DAR": "Potato products",
    "DB": "Beans and lentils",
    "DF": "Peas",
    "DG": "Vegetables, general",
    "DI": "Vegetables, dried",
    "DR": "Vegetable dishes",
    "F": "Fruit",
    "FA": "Fruit, general",
    "FC": "Fruit juices",
    "G": "Nuts and seeds",
    "GA": "Nuts and seeds, general",
    "H": "Herbs and spices",
    "J": "Fish and fish products",
    "JA": "White fish",
    "JC": "Fatty fish",
    "JK": "Crustacea",
    "JM": "Molluscs",
    "JR": "Fish products and dishes",
    "M": "Meat and meat products",
    "MA": "Meat",
    "MAA": "Bacon",
    "MAC": "Beef",
    "MAE": "Lamb",
    "MAG": "Pork",
    "MAI": "Veal",
    "MC": "Poultry",
    "MCA": "Chicken",
    "MCC": "Duck",
    "MCE": "Goose",
    "MCG": "Grouse",
    "MCI": "Partridge",
    "MCK": "Pheasant",
    "MCM": "Pigeon",
    "MCO": "Turkey",
    "ME": "Game",
    "MEA": "Hare",
    "MEC": "Rabbit",
    "MEE": "Venison",
    "MG": "Offal",
    "MBG": "Burgers and grillsteaks",
    "MI": "Meat products",
    "MIG": "Other meat products",
    "MR": "Meat dishes",
    "O": "Fats and oils",
    "OA": "Spreading fats",
    "OB": "Animal fats",
    "OC": "Oils",
    "OE": "Non-animal fats",
    "OF": "Cooking fats",
    "P": "Beverages",
    "PA": "Powdered drinks, essences and infusions",
    "PAA": "Powdered drinks and essences",
    "PAC": "Infusions",
    "PC": "Soft drinks",
    "PCA": "Carbonated drinks",
    "PCC": "Squash and cordials",
    "PE": "Juices",
    "Q": "Alcoholic beverages",
    "QA": "Beers",
    "QC": "Ciders",
    "QE": "Wines",
    "QF": "Fortified wines",
    "QG": "Vermouths",
    "QI": "Liqueurs",
    "QK": "Spirits",
    "S": "Sugars, preserves and snacks",
    "SC": "Sugars, syrups and preserves",
    "SE": "Confectionery",
    "SEA": "Chocolate confectionery",
    "SEC": "Non-chocolate confectionery",
    "SN": "Savoury snacks",
    "SNA": "Potato-based snacks",
    "SNB": "Potato and mixed cereal snacks",
    "SNC": "Non-potato snacks",
    "W": "Soups, sauces and miscellaneous foods",
    "WA": "Soups",
    "WAA": "Homemade soups",
    "WAC": "Canned soups",
    "WAE": "Packet soups",
    "WC": "Sauces",
    "WCD": "Dairy sauces",
    "WCG": "Salad sauces, dressings and pickles",
    "WCN": "Non-salad sauces",
    "WE": "Pickles and chutneys",
    "WY": "Miscellaneous foods",
}


def parse_value(cell: object) -> tuple[float | None, bool]:
    """Read a value cell of the workbook.

    Args:
        cell: Cell content: a number, a numeric text, `Tr` or `N`.

    Returns:
        The number (None when missing or `N`) and whether it is a trace.
    """
    if cell is None or isinstance(cell, bool):
        return None, False
    if isinstance(cell, (int, float)):
        return float(cell), False
    text = str(cell).strip()
    if text.lower() == "tr":
        return 0.0, True
    try:
        return float(text.replace(",", ".")), False
    except ValueError:
        return None, False


def group_category(code: str | None) -> str | None:
    """Spell out a food group code with the names of the user guide.

    Args:
        code: Group code of the workbook, e.g. `PCA`.

    Returns:
        The names from the main group down, e.g. `Beverages / Soft drinks /
        Carbonated drinks`, followed by the code itself when the guide does
        not list it; None without a code.
    """
    if not code:
        return None
    names = []
    for length in range(1, len(code) + 1):
        name = GROUP_NAMES.get(code[:length])
        if name and name not in names:
            names.append(name)
    if code not in GROUP_NAMES:
        names.append(code)
    return " / ".join(names)


def is_drink(code: str | None) -> bool | None:
    """Tell whether a food group code designates a beverage.

    Args:
        code: Group code of the workbook.

    Returns:
        None without a code; True for beverages, alcoholic beverages, liquid
        milks, milk-based drinks and fruit juices.
    """
    if not code:
        return None
    if code in NOT_DRINK_GROUPS:
        return False
    return code.startswith(DRINK_GROUP_PREFIXES) or code in DRINK_GROUPS


def read_sheet(workbook: openpyxl.Workbook, name: str) -> tuple[list[str | None], list[tuple]]:
    """Read the header and the data rows of a sheet.

    Args:
        workbook: The opened workbook.
        name: Sheet name.

    Returns:
        The cleaned header cells and the rows that have a food code.
    """
    rows = list(workbook[name].iter_rows(values_only=True))
    header = [clean_text(cell) for cell in rows[HEADER_ROW]]
    data = [row for row in rows[FIRST_DATA_ROW:] if clean_text(row[0])]
    return header, data


def read_workbook(path: Path) -> tuple[list[dict], int]:
    """Read the foods of the workbook as staging rows without source fields.

    Args:
        path: Path of the workbook.

    Returns:
        The rows, and the number of repeated food codes that were skipped.

    Raises:
        ValueError: When an expected column is missing.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        header, data = read_sheet(workbook, PROXIMATES_SHEET)
        sodium_header, sodium_data = read_sheet(workbook, INORGANICS_SHEET)
    finally:
        workbook.close()
    try:
        index = {column: header.index(label) for label, column in VALUE_COLUMNS.items()}
        name_index = header.index("Food Name")
        group_index = header.index("Group")
        sodium_index = sodium_header.index(SODIUM_COLUMN)
    except ValueError as error:
        raise ValueError(f"{path.name}: missing column ({error})") from error
    sodium = {clean_text(row[0]): parse_value(row[sodium_index]) for row in sodium_data}

    rows = []
    seen: set[str] = set()
    repeated = 0
    for row in data:
        code = clean_text(row[0])
        if code in seen:
            repeated += 1
            continue
        seen.add(code)
        group = clean_text(row[group_index])
        staged: dict = {
            "source_dataset": None,
            "source_record_id": code,
            "kind": schema.KIND_GENERIC,
            "names": {"en": clean_text(row[name_index])} if clean_text(row[name_index]) else {},
            "main_lang": "en",
            "category": group_category(group),
            "basis": schema.BASIS_100ML
            if (group or "").startswith(ALCOHOLIC_GROUP_PREFIX)
            else schema.BASIS_100G,
            "basis_assumed": False,
            "carbohydrate_includes_fiber": False,
            "is_drink": is_drink(group),
        }
        inexact = False
        for column, column_index in index.items():
            value, trace = parse_value(row[column_index])
            staged[column] = value
            inexact = inexact or trace
        aoac = staged.pop("fiber_aoac")
        nsp = staged.pop("fiber_nsp")
        staged["fiber_g"] = aoac if aoac is not None else nsp
        sodium_mg, sodium_trace = sodium.get(code, (None, False))
        staged["sodium_g"] = None if sodium_mg is None else sodium_mg / 1000
        if inexact or sodium_trace:
            staged["source_flags"] = [schema.FLAG_INEXACT_VALUES]
        rows.append(staged)
    return rows, repeated


def build(source: Source, context: AdapterContext) -> AdapterResult:
    """Write the staging file of CoFID.

    Args:
        source: The CoFID entry of the source list.
        context: The adapter context.

    Returns:
        The row counts.
    """
    rows, repeated = read_workbook(context.file(source.files[0].name))
    for row in rows:
        row["source_url"] = source.homepage
        row["countries"] = [source.country] if source.country else None
        row["updated_at"] = source.released or None
    written = write_staging_rows(context, rows)
    return AdapterResult(
        stats={
            "rows_in_source": len(rows) + repeated,
            "repeated_food_codes_skipped": repeated,
            "rows_staged": written,
        }
    )
