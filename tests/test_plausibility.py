"""Tests that the quality checks in Python are those of the build in SQL.

A value entered through the service must be judged exactly as the build will
judge it once it is in the layer. Both implementations are run on the same
cases, missing values included.
"""

from __future__ import annotations

import itertools

import duckdb

from kratheos_food_data import plausibility, schema
from kratheos_food_data.normalize import has_macros_sql, quality_flag_cases

COLUMNS = (
    "energy_kcal",
    "protein_g",
    "carbohydrate_g",
    "carbohydrate_total_g",
    "sugars_g",
    "fat_g",
    "saturated_fat_g",
    "fiber_g",
    "salt_g",
    "alcohol_g",
    "polyols_g",
)
CASES = [
    # energy, protein, carbohydrate, total, sugars, fat, saturated, fibre, salt, alcohol, polyols
    (539, 6.3, 57.5, None, 56.3, 30.9, 10.6, 0, 0.107, 0, None),
    (250, 13, 25, None, 4, 11, 4, 2, 1.2, None, None),
    (25, 13, 25, None, 4, 11, 4, 2, 1.2, None, None),
    (2510, 13, 25, None, 3, 11, 4, None, 1, None, None),
    (82, 0.1, 2.6, None, None, 0, None, None, None, 10, None),
    (82, 0.1, 2.6, None, None, 0, None, None, None, None, None),
    (400, 0, 100, None, 105, 0, 0, 0, 0, 0, None),
    (394, 0, 105, None, 105, 0, 0, 0, 0, 0, None),
    (900, 0, 0, None, 0, 100, 102, 0, 0, 0, None),
    (100, None, 20, None, 22, None, 5, None, None, None, None),
    (None, 10, 10, None, 1, 10, 1, 1, 1, None, None),
    (350, 10, 60, 70, 65, 5, 1, 10, 1, 0, None),
    (240, 5, 80, None, 2, 1, 0, 0, 0, 0, 75),
    (0, 0, 0, None, 0, 0, 0, 0, 0, 0, None),
    (-5, 1, 1, None, 1, 1, 1, 1, 1, 1, None),
    (50, -1, 5, None, 1, 1, 0, 0, 0, 0, None),
    (60, 40, 40, None, 0, 30, 0, 0, 0, 0, None),
    (120, 3, 20, None, 1, 2, 0.5, 30, 0.2, 0, None),
    (37, 0.5, 1, None, 0.5, 0.2, 0, 0, 101, 0, None),
]


def sql_flags(con: duckdb.DuckDBPyConnection, case: tuple) -> list[str]:
    """Run the checks of the build on one case.

    Args:
        con: A DuckDB connection.
        case: The values, in the order of `COLUMNS`.

    Returns:
        The quality flags the SQL raises, in its order.
    """
    typed = ", ".join(f"CAST(? AS DOUBLE) AS {name}" for name in COLUMNS)
    flags = "[flag FOR flag IN [" + ", ".join(quality_flag_cases()) + "] IF flag IS NOT NULL]"
    return con.execute(
        f"SELECT {flags} FROM (SELECT *, {has_macros_sql()} AS has_macros FROM (SELECT {typed}))",
        list(case),
    ).fetchone()[0]


def test_python_checks_are_the_sql_checks(con: duckdb.DuckDBPyConnection) -> None:
    """Every case gets the same flags, in the same order, from both."""
    raised = set()
    for case in CASES:
        values = dict(zip(COLUMNS, case, strict=True))
        expected = sql_flags(con, case)
        assert plausibility.quality_flags(values) == expected, case
        assert plausibility.has_macros(values) == all(
            values[name] is not None
            for name in ("energy_kcal", "protein_g", "carbohydrate_g", "fat_g")
        )
        raised |= set(expected)
    assert raised == set(schema.QUALITY_FLAGS)  # the cases reach every check


def test_checks_agree_around_every_threshold(con: duckdb.DuckDBPyConnection) -> None:
    """Values just below, on and just above the limits get the same verdict."""
    energies = (0, 9.9, 10, 126, 157.5, 189, 189.1, 905, 905.1)
    fats = (0, 4, 5, 5.1)
    sugars = (None, 20, 21, 21.1)
    for energy, fat, sugar, fibre in itertools.product(energies, fats, sugars, (None, 0, 6)):
        case = (energy, 10, 20, None, sugar, fat, fat + 1, fibre, 0.5, None, None)
        values = dict(zip(COLUMNS, case, strict=True))
        assert plausibility.quality_flags(values) == sql_flags(con, case), case


def test_rounding_is_the_rounding_of_the_build(con: duckdb.DuckDBPyConnection) -> None:
    """A value rounded by the service is left as it is by the build."""
    for value in (0.107, 56.76, 10.81, 2.00005, 12.34565, 1 / 3, 539, 0.00004):
        rounded = plausibility.rounded(value)
        assert con.execute("SELECT round(?, 4)", [rounded]).fetchone()[0] == rounded
    assert plausibility.rounded(None) is None
