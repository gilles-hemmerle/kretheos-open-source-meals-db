"""Adapter for Ciqual, the French food composition table (Anses).

Reads the XML files of the release: `alim.xml` (foods, with French and English
names), `alim_grp.xml` (food groups), `compo.xml` (one value per food and
constituent). Values are per 100 g of edible portion.

Ciqual writes values as text: a decimal comma, `-` for a missing value,
`traces`, and `< 0,5` for a content below the limit of the analysis. `traces`
is kept as 0 and `< x` as x, and the record is flagged `inexact_values`.

Energy is the value computed by Ciqual according to Regulation (EU)
No 1169/2011, protein is nitrogen multiplied by the Jones factor of the food
(Ciqual's own headline value), carbohydrate is available carbohydrate.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from .. import schema
from ..config import Source
from .base import AdapterContext, AdapterResult, clean_text, write_staging_rows

FOOD_URL = "https://ciqual.anses.fr/#/aliments/"
DRINK_GROUP = "06"
NOT_DRINK_SUBGROUPS = {"060207"}
LIQUID_MILK_SUBGROUPS = {"050101", "050102"}
NO_SUBGROUP = {"", "000000", "0000", "00"}

# Constituent code -> staging column. Protein has two candidates: the Jones
# factor value, then nitrogen x 6.25.
CONSTITUENTS = {
    "328": "energy_kcal",
    "327": "energy_kj",
    "25000": "protein_g",
    "25003": "protein_alt",
    "31000": "carbohydrate_g",
    "32000": "sugars_g",
    "40000": "fat_g",
    "40302": "saturated_fat_g",
    "34100": "fiber_g",
    "10004": "salt_g",
    "10110": "sodium_mg",
    "60000": "alcohol_g",
    "34000": "polyols_g",
}


def parse_value(text: str | None) -> tuple[float | None, bool]:
    """Read a Ciqual value.

    Args:
        text: The content of a `teneur` element.

    Returns:
        The number (None when missing) and whether it is inexact, that is
        given as `traces` or as an upper bound `< x`.
    """
    if text is None:
        return None, False
    cleaned = text.strip().lower()
    if cleaned in ("", "-"):
        return None, False
    if cleaned.startswith("trace"):
        return 0.0, True
    inexact = cleaned.startswith("<")
    number = cleaned.lstrip("<").strip().replace(",", ".")
    try:
        return float(number), inexact
    except ValueError:
        return None, False


def _children(element: ET.Element) -> dict[str, str]:
    """Return the trimmed text of every child of an XML element.

    Args:
        element: An XML element whose children are leaves.

    Returns:
        A mapping of child tag to trimmed text (empty when absent).
    """
    return {child.tag: (child.text or "").strip() for child in element}


def read_groups(path: Path) -> dict[str, str]:
    """Read the food groups.

    Args:
        path: Path of `alim_grp.xml`.

    Returns:
        A mapping of group, sub-group and sub-sub-group code to English name.
    """
    names: dict[str, str] = {}
    for group in ET.parse(path).getroot():
        fields = _children(group)
        for level in ("alim_grp", "alim_ssgrp", "alim_ssssgrp"):
            code = fields.get(f"{level}_code", "")
            name = fields.get(f"{level}_nom_eng", "")
            if code not in NO_SUBGROUP and name not in ("", "-"):
                names[code] = name
    return names


def read_values(path: Path) -> dict[str, dict[str, tuple[float | None, bool]]]:
    """Read the composition values of the constituents the schema needs.

    Args:
        path: Path of `compo.xml`.

    Returns:
        For each food code, a mapping of staging column to (value, inexact).
    """
    values: dict[str, dict[str, tuple[float | None, bool]]] = {}
    for _, element in ET.iterparse(path, events=("end",)):
        if element.tag != "COMPO":
            continue
        column = CONSTITUENTS.get((element.findtext("const_code") or "").strip())
        if column is not None:
            food = (element.findtext("alim_code") or "").strip()
            values.setdefault(food, {})[column] = parse_value(element.findtext("teneur"))
        element.clear()
    return values


def build(source: Source, context: AdapterContext) -> AdapterResult:
    """Write the staging file of Ciqual.

    Args:
        source: The Ciqual entry of the source list.
        context: The adapter context.

    Returns:
        The row counts.
    """
    groups = read_groups(context.download_dir / "alim_grp.xml")
    values = read_values(context.download_dir / "compo.xml")
    rows = []
    foods = ET.parse(context.download_dir / "alim.xml").getroot()
    for food in foods:
        fields = _children(food)
        code = fields.get("alim_code", "")
        if not code:
            continue
        names = {}
        if clean_text(fields.get("alim_nom_fr")):
            names["fr"] = clean_text(fields["alim_nom_fr"])
        if clean_text(fields.get("alim_nom_eng")):
            names["en"] = clean_text(fields["alim_nom_eng"])
        group = fields.get("alim_grp_code", "")
        subgroup = fields.get("alim_ssgrp_code", "")
        subsubgroup = fields.get("alim_ssssgrp_code", "")
        category = " / ".join(
            groups[code_] for code_ in (group, subgroup, subsubgroup) if code_ in groups
        )
        food_values = values.get(code, {})
        row: dict = {
            "source_dataset": None,
            "source_record_id": code,
            "kind": schema.KIND_GENERIC,
            "names": names,
            "main_lang": "fr",
            "category": category or None,
            "basis": schema.BASIS_100G,
            "basis_assumed": False,
            "is_drink": (group == DRINK_GROUP and subsubgroup not in NOT_DRINK_SUBGROUPS)
            or subsubgroup in LIQUID_MILK_SUBGROUPS,
            "source_url": FOOD_URL + code,
            "updated_at": source.released or None,
        }
        inexact = False
        for column in CONSTITUENTS.values():
            value, flagged = food_values.get(column, (None, False))
            row[column] = value
            inexact = inexact or (flagged and value is not None)
        if row["protein_g"] is None:
            row["protein_g"] = row["protein_alt"]
        del row["protein_alt"]
        sodium_mg = row.pop("sodium_mg")
        row["sodium_g"] = None if sodium_mg is None else sodium_mg / 1000
        if inexact:
            row["source_flags"] = [schema.FLAG_INEXACT_VALUES]
        rows.append(row)
    written = write_staging_rows(context, rows)
    return AdapterResult(stats={"rows_in_source": len(foods), "rows_staged": written})
