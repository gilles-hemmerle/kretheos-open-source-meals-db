"""Tests of the barcode normalisation, in Python and in SQL."""

from __future__ import annotations

import random

import duckdb
import pytest

from kratheos_food_data.barcode import check_digit_ok, is_restricted, normalize_gtin

CASES = [
    # (raw barcode, canonical form)
    ("3017620422003", "3017620422003"),  # EAN-13
    ("5449000000996", "5449000000996"),
    ("049000028911", "0049000028911"),  # UPC-A padded to GTIN-13
    ("00049000028911", "0049000028911"),  # GTIN-14 with leading zeros
    ("0 49000 02891 1", "0049000028911"),  # spaces
    ("0-49000-02891-1", "0049000028911"),  # dashes
    ("20095314", "20095314"),  # EAN-8
    ("0000020095314", "20095314"),  # EAN-8 zero-padded to 13 digits
    ("10012345678902", "10012345678902"),  # GTIN-14 with a packaging indicator
    ("3017620422004", None),  # wrong check digit
    ("84279810254X", None),  # letters
    ("NIELSENUK0002", None),
    ("6005207001298>", None),  # stray character
    ("853784259806586", None),  # longer than 14 digits
    ("0000000000000", None),  # zero
    ("", None),
    ("   ", None),
    (None, None),
]


@pytest.mark.parametrize(("raw", "expected"), CASES)
def test_normalize_gtin(raw: str | None, expected: str | None) -> None:
    """The Python reference gives the documented canonical form."""
    assert normalize_gtin(raw) == expected


@pytest.mark.parametrize(("raw", "expected"), CASES)
def test_sql_macro_matches_reference(
    con: duckdb.DuckDBPyConnection, raw: str | None, expected: str | None
) -> None:
    """The SQL macro gives the same result as the Python reference."""
    assert con.execute("SELECT gtin_normalize(?)", [raw]).fetchone()[0] == expected


def test_sql_macro_matches_reference_on_random_digits(con: duckdb.DuckDBPyConnection) -> None:
    """Python and SQL agree on thousands of random digit strings."""
    generator = random.Random(20261011)
    samples = [
        "".join(generator.choice("0123456789") for _ in range(generator.randint(1, 16)))
        for _ in range(5000)
    ]
    con.execute("CREATE TEMP TABLE samples (raw VARCHAR)")
    con.executemany("INSERT INTO samples VALUES (?)", [(sample,) for sample in samples])
    rows = con.execute(
        "SELECT raw, gtin_normalize(raw), gtin_restricted(gtin_normalize(raw)) FROM samples"
    ).fetchall()
    valid = 0
    for raw, canonical, restricted in rows:
        assert canonical == normalize_gtin(raw), raw
        assert restricted == is_restricted(normalize_gtin(raw)), raw
        valid += canonical is not None
    # About one random string in ten has a valid check digit.
    assert 300 < valid < 700


def test_check_digit_is_unchanged_by_zero_padding() -> None:
    """Padding a GTIN with zeros on the left keeps its check digit valid."""
    assert check_digit_ok("049000028911")
    assert check_digit_ok("0049000028911")
    assert check_digit_ok("00049000028911")
    assert not check_digit_ok("049000028912")


@pytest.mark.parametrize(
    ("barcode", "expected"),
    [
        ("20095314", True),  # EAN-8 starting with 2: in-store number
        ("00123457", True),  # EAN-8 starting with 0: velocity code or UPC-E
        ("2012345678903", True),  # EAN-13 starting with 2
        ("0212345678908", True),  # UPC-A starting with 2: variable weight
        ("0412345678906", True),  # UPC-A starting with 4: in-store
        ("3017620422003", False),
        ("0049000028911", False),
        ("40123455", False),
        (None, False),
    ],
)
def test_is_restricted(barcode: str | None, expected: bool) -> None:
    """Restricted-circulation prefixes are recognised."""
    assert is_restricted(barcode) is expected
