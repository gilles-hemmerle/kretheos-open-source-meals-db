"""What a request may contain: every parameter and every body is checked here.

The service trusts nothing it receives, even from its only client. A
parameter or a field that is missing, of the wrong type, too long or unknown
is refused with `invalid_request` (422) and a sentence naming it; a body
that is not JSON with `invalid_json` (400). Values are then judged by the
quality checks of the build (`plausibility.py`): what fails them is refused
with `implausible_values` and the list of the checks that failed.

Limits, chosen wide enough for any real food and narrow enough to keep what
is stored small: names of 200 characters, brand and category of 120, a note
of 1000, 30 languages, 60 countries, 10 portions (the first one is stored).

A household portion people add to a food ("1 oeuf moyen", 50 g) has a label
of 120 characters at most and weighs or holds from 1 to 2000 g or ml: below
or above, it is refused with `implausible_portion`.
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from collections.abc import Mapping

from .. import plausibility, schema
from ..barcode import normalize_gtin
from .foods import NUTRIENT_FIELDS
from .live import REPORT_REASONS

MAX_NAME = 200
MAX_LABEL = 120
MAX_NOTE = 1000
MAX_LANGUAGES = 30
MAX_COUNTRIES = 60
MAX_SERVINGS = 10
MAX_SERVING_AMOUNT = 10_000
# What a household portion added by a person may weigh or hold, in g or ml.
MIN_PORTION_AMOUNT = 1
MAX_PORTION_AMOUNT = 2000
MAX_ID = 80
MAX_LIMIT = 50
DEFAULT_LIMIT = 20
MAX_OFFSET = 1000
MAX_REPORTS_LIMIT = 100
BASES = (schema.BASIS_100G, schema.BASIS_100ML)
KINDS = (schema.KIND_GENERIC, schema.KIND_PRODUCT)
UNITS = ("g", "ml")
# Alcohol may be left out of a write, unlike the other nutrients, which are
# then unknown: a version proposed without it keeps the alcohol the food is
# known with, so that correcting the sugar of a wine does not lose it.
OPTIONAL_NUTRIENT = "alcohol_g"
_LANGUAGE = re.compile(r"[a-z]{2,3}(-[a-z0-9]{2,8})?")
_COUNTRY = re.compile(r"[A-Za-z]{2}")
_CONTRIBUTOR = re.compile(r"[\x21-\x7e]{8,200}")
_ID = re.compile(r"[\x21-\x7e]{1,80}")


class ApiError(Exception):
    """A request that cannot be answered as asked.

    Attributes:
        status: HTTP status of the answer.
        code: Stable code of the error.
        message: English sentence saying what is wrong.
        extra: Other fields of the answer (the checks that failed, the
            identifier that already exists).
    """

    def __init__(self, status: int, code: str, message: str, **extra: object) -> None:
        """Describe the error."""
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra


def invalid(message: str) -> ApiError:
    """Build the error of a request that is not well formed.

    Args:
        message: What is wrong, naming the parameter or the field.

    Returns:
        The error, to be raised.
    """
    return ApiError(422, "invalid_request", message)


def parse_body(raw: bytes, allowed: tuple[str, ...]) -> dict:
    """Read the JSON object of a request.

    Args:
        raw: The body received.
        allowed: The fields the endpoint knows.

    Returns:
        The object.

    Raises:
        ApiError: When the body is not a JSON object, or has an unknown field.
    """

    def refuse_constant(name: str) -> None:
        """Refuse NaN and the infinities, which are not JSON."""
        raise ValueError(name)

    try:
        body = json.loads(raw.decode("utf-8"), parse_constant=refuse_constant)
    except (ValueError, RecursionError) as error:
        raise ApiError(400, "invalid_json", "the body is not valid JSON") from error
    if not isinstance(body, dict):
        raise ApiError(400, "invalid_json", "the body must be a JSON object")
    unknown = sorted(set(body) - set(allowed))
    if unknown:
        raise invalid(f"unknown field: {unknown[0]}")
    return body


def text(value: object, what: str, maximum: int, required: bool = False) -> str | None:
    """Check a text field.

    Args:
        value: The value received.
        what: Name of the field, for the error message.
        maximum: Largest number of characters.
        required: Refuse a missing or empty text.

    Returns:
        The text with its white space collapsed, or None when there is none.

    Raises:
        ApiError: When the value is not a text, is too long or is missing.
    """
    if value is None:
        cleaned = ""
    elif isinstance(value, str):
        cleaned = " ".join(value.split())
    else:
        raise invalid(f"{what} must be a text")
    if len(cleaned) > maximum:
        raise invalid(f"{what} must have at most {maximum} characters")
    if any(is_control(char) for char in cleaned):
        raise invalid(f"{what} holds control characters")
    if required and not cleaned:
        raise invalid(f"{what} is required")
    return cleaned or None


def is_control(char: str) -> bool:
    """Tell whether a character is a control character.

    Args:
        char: One character.

    Returns:
        True for the C0 and C1 controls and the delete character.
    """
    code = ord(char)
    return code < 32 or 127 <= code < 160


def contributor(body: Mapping) -> str:
    """Check the contributor key every write carries.

    Args:
        body: The body of the request.

    Returns:
        The key.

    Raises:
        ApiError: When the key is missing or is not 8 to 200 visible ASCII
            characters.
    """
    value = body.get("contributor")
    if not isinstance(value, str) or not _CONTRIBUTOR.fullmatch(value):
        raise invalid("contributor must be a key of 8 to 200 visible ASCII characters")
    return value


def language(value: object, what: str = "lang", required: bool = False) -> str | None:
    """Check a language code.

    Args:
        value: The value received.
        what: Name of the parameter or field.
        required: Refuse a missing code.

    Returns:
        The code in lower case, or None when there is none.

    Raises:
        ApiError: When the value is not a language code.
    """
    if value in (None, "") and not required:
        return None
    code = value.strip().lower().replace("_", "-") if isinstance(value, str) else ""
    if not _LANGUAGE.fullmatch(code):
        raise invalid(f"{what} must be a language code such as fr or pt-br")
    return code


def country(value: object, what: str = "country") -> str | None:
    """Check a country code.

    Args:
        value: The value received.
        what: Name of the parameter or field.

    Returns:
        The code in upper case, or None when there is none.

    Raises:
        ApiError: When the value is not an ISO 3166-1 alpha-2 code.
    """
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not _COUNTRY.fullmatch(value.strip()):
        raise invalid(f"{what} must be an ISO 3166-1 alpha-2 country code such as FR")
    return value.strip().upper()


def countries(value: object) -> list[str]:
    """Check a list of country codes.

    Args:
        value: The value received.

    Returns:
        The codes in upper case, sorted, each once; empty when none is given.

    Raises:
        ApiError: When the value is not a list of country codes.
    """
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_COUNTRIES:
        raise invalid(f"countries must be a list of at most {MAX_COUNTRIES} country codes")
    codes = {country(item, "countries") for item in value}
    if None in codes:
        raise invalid("countries must hold ISO 3166-1 alpha-2 country codes")
    return sorted(codes)


def food_id(value: str, what: str = "id") -> str:
    """Check an identifier taken from the address of a request.

    Args:
        value: The identifier.
        what: What it identifies, for the error message.

    Returns:
        The identifier.

    Raises:
        ApiError: When it is not 1 to 80 visible ASCII characters.
    """
    if not _ID.fullmatch(value):
        raise invalid(f"{what} must be 1 to {MAX_ID} visible ASCII characters")
    return value


def barcode(value: object, required: bool = True) -> str | None:
    """Check and normalise a barcode.

    Args:
        value: The barcode as received.
        required: Refuse a missing barcode.

    Returns:
        The canonical barcode, or None when there is none and it is optional.

    Raises:
        ApiError: `invalid_barcode` when the value is not a valid GTIN.
    """
    if value in (None, "") and not required:
        return None
    canonical = normalize_gtin(value) if isinstance(value, str) and len(value) <= 40 else None
    if canonical is None:
        raise ApiError(
            422,
            "invalid_barcode",
            "the barcode must be a GTIN of 8, 12, 13 or 14 digits with a valid check digit",
        )
    return canonical


def names(value: object) -> dict[str, str]:
    """Check the names of a product.

    Args:
        value: The value received: names by language code.

    Returns:
        The names, by language code in lower case, in the order received.

    Raises:
        ApiError: When there is no name or a name or a code is not valid.
    """
    if not isinstance(value, dict) or not value or len(value) > MAX_LANGUAGES:
        raise invalid(f"names must give 1 to {MAX_LANGUAGES} names by language code")
    cleaned: dict[str, str] = {}
    for code, name in value.items():
        lang = language(code, "a language of names", required=True)
        cleaned[lang] = text(name, f"names.{lang}", MAX_NAME, required=True)
    return cleaned


def nutrients(value: object) -> dict[str, float | None]:
    """Check the nutrients of a product or of a version.

    Args:
        value: The value received: numbers by nutrient name of the API.

    Returns:
        The values by column name of the database, rounded as the build
        stores them. `alcohol_g` is only present when the request gives it.

    Raises:
        ApiError: When a name is unknown, a value is not a number, or the
            energy is missing.
    """
    known = dict(NUTRIENT_FIELDS)
    if not isinstance(value, dict):
        raise invalid("nutrients must be an object of numbers")
    unknown = sorted(set(value) - set(known))
    if unknown:
        raise invalid(f"unknown nutrient: {unknown[0]}")
    cleaned: dict[str, float | None] = {}
    for name, column in known.items():
        if name == OPTIONAL_NUTRIENT and name not in value:
            continue
        number = value.get(name)
        if number is not None and (
            isinstance(number, bool)
            or not isinstance(number, (int, float))
            or not math.isfinite(number)
        ):
            raise invalid(f"nutrients.{name} must be a number or null")
        cleaned[column] = plausibility.rounded(number)
    if cleaned["energy_kcal"] is None:
        raise invalid("nutrients.kcal is required")
    return cleaned


def serving(value: object, basis: str) -> dict:
    """Check the portions of a product or of a version.

    The database keeps one portion per version: the first of the list.

    Args:
        value: The value received: a list of portions.
        basis: Basis of the values, which gives the default unit.

    Returns:
        `serving_size`, `serving_unit` and `serving_text`, None without a
        portion.

    Raises:
        ApiError: When the value is not a list of portions.
    """
    empty = {"serving_size": None, "serving_unit": None, "serving_text": None}
    if value is None:
        return empty
    if not isinstance(value, list) or len(value) > MAX_SERVINGS:
        raise invalid(f"servings must be a list of at most {MAX_SERVINGS} portions")
    kept = None
    for item in value:
        if not isinstance(item, dict) or set(item) - {"label", "amount", "unit"}:
            raise invalid("a serving has a label, an amount and a unit")
        amount = item.get("amount")
        if (
            isinstance(amount, bool)
            or not isinstance(amount, (int, float))
            or not math.isfinite(amount)
            or not 0 < amount <= MAX_SERVING_AMOUNT
        ):
            raise invalid(f"servings.amount must be above 0 and at most {MAX_SERVING_AMOUNT}")
        unit = item.get("unit", "ml" if basis == schema.BASIS_100ML else "g")
        if unit not in UNITS:
            raise invalid("servings.unit must be g or ml")
        label = text(item.get("label"), "servings.label", MAX_LABEL)
        if kept is None:
            kept = {
                "serving_size": plausibility.rounded(amount),
                "serving_unit": unit,
                "serving_text": label,
            }
    return kept or empty


def fold(text: str) -> str:
    """Fold a label so that two wordings of the same portion compare equal.

    Args:
        text: A label.

    Returns:
        The label in lower case, without accents nor repeated spaces
        ("1 Œuf  moyen" and "1 oeuf moyen" fold to the same text).
    """
    decomposed = unicodedata.normalize("NFKD", text.replace("œ", "oe").replace("Œ", "oe"))
    stripped = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(stripped.casefold().split())


def portion(body: Mapping) -> dict:
    """Check the household portion a person adds to a food.

    Args:
        body: The body of the request: `language`, `label`, `amount`, `unit`.

    Returns:
        `lang`, `label`, `amount` (rounded as the build stores numbers) and
        `unit` of the portion.

    Raises:
        ApiError: `invalid_request` when a field is missing or malformed,
            `implausible_portion` when the amount is below 1 or above 2000.
    """
    lang = language(body.get("language"), "language", required=True)
    label = text(body.get("label"), "label", MAX_LABEL, required=True)
    amount = body.get("amount")
    if (
        isinstance(amount, bool)
        or not isinstance(amount, (int, float))
        or not math.isfinite(amount)
    ):
        raise invalid("amount must be a number")
    unit = body.get("unit")
    if unit not in UNITS:
        raise invalid("unit must be g or ml")
    if not MIN_PORTION_AMOUNT <= amount <= MAX_PORTION_AMOUNT:
        raise ApiError(
            422,
            "implausible_portion",
            f"a portion weighs or holds from {MIN_PORTION_AMOUNT} to {MAX_PORTION_AMOUNT} g or ml",
        )
    return {"lang": lang, "label": label, "amount": plausibility.rounded(amount), "unit": unit}


def basis(value: object) -> str:
    """Check the basis of the values.

    Args:
        value: The value received.

    Returns:
        `100g` or `100ml`.

    Raises:
        ApiError: When the value is neither.
    """
    if value not in BASES:
        raise invalid("basis must be 100g or 100ml")
    return value


def plausible(values: Mapping[str, float | None]) -> None:
    """Refuse values that fail the quality checks of the build.

    Args:
        values: Values by column name of the database.

    Raises:
        ApiError: `implausible_values`, with the checks that failed.
    """
    failed = plausibility.quality_flags(values)
    if failed:
        raise ApiError(
            422,
            "implausible_values",
            "the values fail the plausibility checks: "
            + "; ".join(schema.QUALITY_FLAGS[flag] for flag in failed),
            checks=failed,
        )


def flag(value: str | None, what: str) -> bool | None:
    """Read a `true` or `false` parameter.

    Args:
        value: The text of the parameter.
        what: Its name.

    Returns:
        The boolean, or None when the parameter is absent or empty.

    Raises:
        ApiError: When the text is neither `true` nor `false`.
    """
    if value in (None, ""):
        return None
    if value not in ("true", "false"):
        raise invalid(f"{what} must be true or false")
    return value == "true"


def integer(value: str | None, what: str, default: int, minimum: int, maximum: int) -> int:
    """Read a whole-number parameter.

    Args:
        value: The text of the parameter.
        what: Its name.
        default: Value when the parameter is absent.
        minimum: Smallest accepted value.
        maximum: Largest accepted value.

    Returns:
        The number.

    Raises:
        ApiError: When the text is not a whole number within the bounds.
    """
    if value in (None, ""):
        return default
    if not re.fullmatch(r"[0-9]{1,9}", value) or not minimum <= int(value) <= maximum:
        raise invalid(f"{what} must be a whole number from {minimum} to {maximum}")
    return int(value)


def choice(value: str | None, what: str, allowed: tuple[str, ...]) -> str | None:
    """Read a parameter that takes one of a few words.

    Args:
        value: The text of the parameter.
        what: Its name.
        allowed: The words accepted.

    Returns:
        The word, or None when the parameter is absent or empty.

    Raises:
        ApiError: When the text is not one of the words.
    """
    if value in (None, ""):
        return None
    if value not in allowed:
        raise invalid(f"{what} must be one of: {', '.join(allowed)}")
    return value


def report_reason(value: object) -> str:
    """Check the reason of a report.

    Args:
        value: The value received.

    Returns:
        The reason.

    Raises:
        ApiError: When the value is not one of the reasons of the contract.
    """
    if value not in REPORT_REASONS:
        raise invalid(f"reason must be one of: {', '.join(REPORT_REASONS)}")
    return value
