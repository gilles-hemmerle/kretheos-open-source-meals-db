"""The quality checks of the build, in Python, for values entered one at a time.

`normalize.py` runs the checks in SQL on millions of records. A service that
receives the values of one food must judge them the same way before
accepting them, so the same rules are written here with the same constants,
and the tests run both on the same cases.

A missing value never fails a check: a rule only speaks when every figure it
needs is there, exactly as a SQL comparison with NULL does.
"""

from __future__ import annotations

from collections.abc import Mapping

from . import normalize, schema

VALUE_DECIMALS = normalize.VALUE_DECIMALS


def has_macros(values: Mapping[str, float | None]) -> bool:
    """Tell whether energy, protein, carbohydrate and fat are all present.

    Args:
        values: Values by column name of the common schema.

    Returns:
        True when the four are there.
    """
    return all(
        values.get(name) is not None
        for name in ("energy_kcal", "protein_g", "carbohydrate_g", "fat_g")
    )


def quality_flags(values: Mapping[str, float | None]) -> list[str]:
    """Run the quality checks of the build on the values of one food.

    Args:
        values: Values per 100 g or 100 ml, by column name of the common
            schema: `energy_kcal` and the nutrients, optionally
            `carbohydrate_total_g` (carbohydrate with fibre, when a source
            gives that) and `polyols_g`. Missing keys are unknown values.

    Returns:
        The quality flags raised, in the order the build lists them; empty
        when the values are plausible.
    """
    energy = values.get("energy_kcal")
    protein = values.get("protein_g")
    carbohydrate = values.get("carbohydrate_g")
    sugars = values.get("sugars_g")
    fat = values.get("fat_g")
    saturated = values.get("saturated_fat_g")
    fiber = values.get("fiber_g")
    alcohol = values.get("alcohol_g")
    polyols = values.get("polyols_g")
    given = values.get("carbohydrate_total_g")
    if given is None:
        given = carbohydrate

    flags = []
    if any(
        values.get(name) is not None
        and (values[name] < 0 or values[name] > normalize.NUTRIENT_MAX_G)
        for name in normalize.NUTRIENTS
    ):
        flags.append(schema.FLAG_VALUE_OUT_OF_RANGE)
    if energy is not None and (energy < 0 or energy > normalize.ENERGY_MAX_KCAL):
        flags.append(schema.FLAG_ENERGY_OUT_OF_RANGE)
    if (
        sugars is not None
        and given is not None
        and sugars > given + normalize.PART_EXCESS_TOLERANCE_G
    ):
        flags.append(schema.FLAG_SUGARS_EXCEED_CARBOHYDRATE)
    if (
        saturated is not None
        and fat is not None
        and saturated > fat + normalize.PART_EXCESS_TOLERANCE_G
    ):
        flags.append(schema.FLAG_SATURATED_EXCEED_FAT)
    if (
        protein is not None
        and given is not None
        and fat is not None
        and protein + given + fat > normalize.MACRO_SUM_MAX_G
    ):
        flags.append(schema.FLAG_MACROS_SUM_OVER_100)
    if has_macros(values) and protein >= 0 and carbohydrate >= 0 and fat >= 0:
        expected = (
            4 * protein
            + 4 * max(carbohydrate - (polyols or 0), 0)
            + normalize.POLYOLS_KCAL_PER_G * (polyols or 0)
            + normalize.FIBER_KCAL_PER_G * (fiber or 0)
            + 9 * fat
            + 7 * (alcohol or 0)
        )
        margin = (
            normalize.ATWATER_RELATIVE_TOLERANCE * expected
            + normalize.ATWATER_ABSOLUTE_TOLERANCE_KCAL
            + normalize.FIBER_KCAL_PER_G * max(fiber or 0, 0)
            + normalize.POLYOLS_KCAL_PER_G * max(polyols or 0, 0)
        )
        if abs(energy - expected) > margin:
            flags.append(schema.FLAG_ENERGY_MACRO_MISMATCH)
    return flags


def rounded(value: float | None) -> float | None:
    """Round a value the way the build stores it.

    Args:
        value: A value, or None when unknown.

    Returns:
        The value with the decimals the build keeps; None stays None.
    """
    return None if value is None else round(float(value), VALUE_DECIMALS)
