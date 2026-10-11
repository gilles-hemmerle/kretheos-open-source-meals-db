"""Barcode (GTIN) normalisation, in Python and as DuckDB SQL macros.

Sources write the same barcode in different ways: UPC-A on 12 digits, the
same number zero-padded to 13 or 14 digits, with spaces or dashes. To let
records of different sources meet, every barcode is brought to one canonical
form:

1. spaces and dashes are removed; anything else that is not a digit makes the
   barcode invalid;
2. leading zeros are removed, then the number is left-padded with zeros to
   8 digits when it fits in 8 (EAN-8), else to 13 digits (EAN-13, which is
   also how a 12-digit UPC-A is written as a GTIN-13), else kept on 14 digits
   (GTIN-14);
3. the GS1 check digit is verified; a barcode that fails is invalid.

The Python functions are the reference implementation (an application that
scans a barcode must apply the same rule before looking it up); the SQL macros
do exactly the same inside DuckDB and are tested against the Python ones.
"""

from __future__ import annotations

import re

import duckdb

_SEPARATORS = re.compile(r"[ \t\r\n\-]")
_DIGITS = re.compile(r"[0-9]+")


def check_digit_ok(digits: str) -> bool:
    """Verify the GS1 check digit of a string of digits.

    From the right, digits are weighted 1, 3, 1, 3, ...; the weighted sum of a
    valid GTIN is a multiple of 10. Left zero-padding does not change it.

    Args:
        digits: The whole number, check digit included.

    Returns:
        True when the check digit matches.
    """
    total = 0
    for position, char in enumerate(reversed(digits)):
        total += int(char) * (3 if position % 2 else 1)
    return total % 10 == 0


def normalize_gtin(raw: str | None) -> str | None:
    """Return the canonical form of a barcode, or None when it is invalid.

    Args:
        raw: The barcode as a source gives it.

    Returns:
        The barcode on 8, 13 or 14 digits, or None when it holds anything but
        digits, spaces and dashes, is zero, is longer than 14 digits or fails
        the check digit.
    """
    if raw is None:
        return None
    cleaned = _SEPARATORS.sub("", raw)
    if not _DIGITS.fullmatch(cleaned):
        return None
    significant = cleaned.lstrip("0")
    if not significant or len(significant) > 14:
        return None
    if len(significant) <= 8:
        candidate = significant.zfill(8)
    elif len(significant) <= 13:
        candidate = significant.zfill(13)
    else:
        candidate = significant
    return candidate if check_digit_ok(candidate) else None


def is_restricted(barcode: str | None) -> bool:
    """Tell whether a canonical barcode is a restricted-circulation number.

    GS1 reserves some prefixes for numbers that a shop or a company assigns
    itself (price-embedded labels of the fresh counter, store brands coded in
    house): 02, 04 and 20 to 29 for 13-digit numbers, 0 and 2 for 8-digit
    ones. Such a number is not unique across retailers, so two records that
    share it may well be different foods.

    Args:
        barcode: A canonical barcode, as returned by `normalize_gtin`.

    Returns:
        True for a restricted-circulation number.
    """
    if not barcode:
        return False
    if len(barcode) == 8:
        return barcode[0] in "02"
    if len(barcode) == 13:
        return barcode[0] == "2" or barcode[:2] in ("02", "04")
    return False


def is_in_store(barcode: str | None) -> bool:
    """Tell whether a canonical barcode is a number a shop puts on what it packs itself.

    These are the GTIN-13 beginning with 02, 04 and 20 to 29 (02 and 04 are
    the 12-digit UPC-A numbers beginning with 2 and 4) and the 8-digit
    numbers beginning the same way: the labels of the fresh counter, with a
    weight or a price inside, and what a shop codes in house.

    This is narrower than `is_restricted`, which also holds every 8-digit
    number beginning with 0: those are the own-brand products of a few
    retailers (Trader Joe's, Sainsbury's, Marks & Spencer, Tesco), packaged
    products that people look for by name.

    Args:
        barcode: A canonical barcode, as returned by `normalize_gtin`.

    Returns:
        True for an in-store number.
    """
    if not barcode or len(barcode) not in (8, 13):
        return False
    return barcode[0] == "2" or barcode[:2] in ("02", "04")


def _check_digit_sql(expression: str) -> str:
    """Build the SQL expression verifying the check digit of a digit string.

    The string is padded to 14 digits; from the left, odd positions weigh 3
    and even positions weigh 1. No lambda is used, so the expression does not
    depend on the lambda syntax of a given DuckDB version.

    Args:
        expression: SQL expression yielding the digits.

    Returns:
        A boolean SQL expression.
    """
    padded = f"lpad({expression}, 14, '0')"
    terms = []
    for position in range(1, 15):
        weight = 3 if position % 2 else 1
        terms.append(f"{weight} * CAST(substr({padded}, {position}, 1) AS INTEGER)")
    return f"(({' + '.join(terms)}) % 10 = 0)"


def register_macros(con: duckdb.DuckDBPyConnection) -> None:
    """Create the barcode SQL macros on a DuckDB connection.

    Macros created:
        gtin_normalize(raw): canonical barcode or NULL, like `normalize_gtin`.
        gtin_restricted(barcode): like `is_restricted`.

    Args:
        con: The connection to add the macros to.
    """
    con.execute(
        r"""
        CREATE OR REPLACE MACRO gtin_significant(raw) AS (
            CASE
                WHEN raw IS NULL THEN NULL
                WHEN regexp_full_match(regexp_replace(raw, '[ \t\r\n\-]', '', 'g'), '[0-9]+')
                    THEN ltrim(regexp_replace(raw, '[ \t\r\n\-]', '', 'g'), '0')
                ELSE NULL
            END
        )
        """
    )
    con.execute(
        """
        CREATE OR REPLACE MACRO gtin_padded(significant) AS (
            CASE
                WHEN significant IS NULL OR length(significant) = 0 THEN NULL
                WHEN length(significant) <= 8 THEN lpad(significant, 8, '0')
                WHEN length(significant) <= 13 THEN lpad(significant, 13, '0')
                WHEN length(significant) = 14 THEN significant
                ELSE NULL
            END
        )
        """
    )
    con.execute(
        f"""
        CREATE OR REPLACE MACRO gtin_check_ok(digits) AS (
            CASE WHEN digits IS NULL THEN FALSE ELSE {_check_digit_sql("digits")} END
        )
        """
    )
    con.execute(
        """
        CREATE OR REPLACE MACRO gtin_normalize(raw) AS (
            CASE
                WHEN gtin_check_ok(gtin_padded(gtin_significant(raw)))
                    THEN gtin_padded(gtin_significant(raw))
                ELSE NULL
            END
        )
        """
    )
    con.execute(
        """
        CREATE OR REPLACE MACRO gtin_restricted(barcode) AS (
            CASE
                WHEN barcode IS NULL THEN FALSE
                WHEN length(barcode) = 8 THEN barcode[1] IN ('0', '2')
                WHEN length(barcode) = 13
                    THEN barcode[1] = '2' OR barcode[1:2] IN ('02', '04')
                ELSE FALSE
            END
        )
        """
    )
