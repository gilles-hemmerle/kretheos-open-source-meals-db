"""Search as a person types, on the stored full-text index, in bounded time.

What is searched. The words typed are looked for in every name of a food, in
every language, and in its brand; each word is taken as the beginning of a
word, and all of them must be found. A text made of digits is also a
barcode: the food carrying it comes first, then the barcodes beginning with
those digits.

Short texts. The index answers in a few milliseconds when a text is
selective and in about a second when it is not: two letters match hundreds
of thousands of the 3.4 million barcoded products, and ranking them is what
takes the time. So:

* with fewer than three letters or digits in all, only the generic foods and
  the dishes without a barcode are searched (160 000 foods, where two letters
  already find "Ei", "riz" or "thé"). Two letters are the beginning of a
  word; a single letter is a whole word;
* from three letters on everything is searched;
* inside a longer text, a word of one letter is a whole word ("vitamine c"),
  because every word beginning with one letter cannot be listed quickly;
* the whole search has a time budget (half a second by default). A text
  that still matches too much is cut off when the budget is spent, and the
  answer holds what was found until then, the generic foods and dishes and
  the foods of the country first: late results are dropped, never a late
  answer.

Where the results come from. The index ranks the matches of a text and gives
its best ones; several such lists are put together: the best generic foods
and dishes, the best of everything, and, when the request names a country,
the best of each among the foods of that country, so that the foods of a
small country are found even for a word that half the world uses. (When the
country alone has 120 matches or more, the barcoded products of the rest of
the world are not ranked as well: they would come after every page a person
reads, and ranking them doubles the time.) That is 440 foods at most, which
are then put in this order:

1. the food carrying the barcode typed;
2. the foods of the country of the request (the version served lists it);
3. a name equal to the text, then a name beginning with it, then the rest;
4. generic foods before products;
5. the most scanned products first (the popularity of Open Food Facts);
6. the shortest name, then the identifier, so that pages are stable.

What the live database holds (products added, names added) is searched in
its own small index and merged in, so it is found at once.
"""

from __future__ import annotations

import functools
import re
import sqlite3
import sys
import time
import unicodedata
from dataclasses import dataclass

from ..barcode import normalize_gtin
from ..schema import KIND_GENERIC
from . import catalogue, foods, live
from .foods import Context, Food

MAX_QUERY_CHARS = 100
MAX_WORDS = 8
SHORT_QUERY_CHARS = 3
POOL_GENERIC = 50
POOL_ALL = 120
POOL_LIVE = 60
POOL_BARCODES = 40
MIN_BARCODE_PREFIX = 4
PROGRESS_STEPS = 20_000
_SEPARATORS = re.compile(r"[\s\-]")
_NOT_A_WORD = re.compile(r"[\W_]+")


@dataclass(frozen=True)
class Query:
    """What a person typed, ready to be searched.

    Attributes:
        text: The words in lower case without accents, joined by spaces.
        words: The same, one by one.
        digits: The digits typed when the text is nothing else, else empty.
    """

    text: str
    words: tuple[str, ...]
    digits: str

    @property
    def short(self) -> bool:
        """Tell whether the text is too short to search every product."""
        return sum(len(word) for word in self.words) < SHORT_QUERY_CHARS


@functools.lru_cache(maxsize=1)
def _combining_marks() -> dict[int, None]:
    """List the combining marks of Unicode, for `str.translate` to drop them.

    Returns:
        A translation table deleting every combining mark.
    """
    return dict.fromkeys(
        code for code in range(sys.maxunicode + 1) if unicodedata.combining(chr(code))
    )


@functools.lru_cache(maxsize=65_536)
def fold(text: str) -> str:
    """Write a text the way the index compares it.

    The names of the foods found are folded for every search and the same
    ones come back often: the last ones are remembered.

    Args:
        text: Any text.

    Returns:
        The words of the text in lower case, without accents, separated by
        single spaces; punctuation is dropped.
    """
    plain = unicodedata.normalize("NFKD", text.casefold()).translate(_combining_marks())
    return _NOT_A_WORD.sub(" ", plain).strip()


def parse(text: str) -> Query:
    """Read what a person typed.

    Args:
        text: The text of the request.

    Returns:
        The query; it has no word when the text holds no letter or digit.
    """
    text = text[:MAX_QUERY_CHARS]
    digits = _SEPARATORS.sub("", text)
    words = tuple(fold(text).split()[:MAX_WORDS])
    return Query(" ".join(words), words, digits if digits.isdigit() else "")


def match_expression(query: Query) -> str:
    """Turn the words typed into an expression of the full-text index.

    Args:
        query: The query.

    Returns:
        Every word quoted; a word of several letters is a prefix, a word of
        one letter a whole word. Empty when there is no word.
    """
    return " ".join(f'"{word}"' + ("*" if len(word) > 1 else "") for word in query.words)


def _within(
    connection: sqlite3.Connection, sql: str, parameters: tuple, deadline: float
) -> list | None:
    """Run a query, giving up when a deadline passes.

    Args:
        connection: A connection to the main database.
        sql: The query.
        parameters: Its parameters.
        deadline: Time, on the monotonic clock, after which it is stopped.

    Returns:
        The rows, or None when the query was stopped or cannot be run.
    """
    connection.set_progress_handler(lambda: time.monotonic() > deadline, PROGRESS_STEPS)
    try:
        return connection.execute(sql, parameters).fetchall()
    except sqlite3.OperationalError:
        return None
    finally:
        connection.set_progress_handler(None, 0)


def _index_matches(
    context: Context, query: Query, country: str | None, generic_only: bool, budget: float
) -> list[int]:
    """Ask the full-text index of the main database for the best matches.

    Args:
        context: Both databases.
        query: The query.
        country: Country of the request, if any.
        generic_only: Leave the barcoded products out, whatever the length.
        budget: Seconds the whole search may take.

    Returns:
        Internal numbers of foods: the best generic foods and dishes and,
        when the text is long enough, the best of everything; each list
        first among the foods of the country. What the budget did not leave
        time for is missing.
    """
    match = match_expression(query)
    if not match:
        return []
    generation = context.generation
    deadline = time.monotonic() + budget
    everything = not query.short and not generic_only
    plain = "SELECT rowid FROM foods_fts WHERE foods_fts MATCH ? {where} ORDER BY rank LIMIT ?"
    local = (
        "SELECT m.rowid FROM foods_fts AS m"
        " JOIN food_countries AS c ON c.country = ? AND c.food_pk = m.rowid"
        " WHERE m.foods_fts MATCH ? {where} ORDER BY m.rank LIMIT ?"
    )
    ranges = []
    if generation.first_gtin is not None:
        ranges = [("AND rowid < ?", generation.first_gtin), ("AND rowid > ?", generation.last_gtin)]

    def run(sql: str, parameters: tuple) -> list[int]:
        """Run one search, unless the time is spent."""
        if time.monotonic() > deadline:
            return []
        return [row[0] for row in _within(context.base, sql, parameters, deadline) or ()]

    found: list[int] = []
    enough_of_the_country = False
    if country:
        for where, bound in ranges:
            sql = local.format(where=where.replace("rowid", "m.rowid"))
            found += run(sql, (country, match, bound, POOL_GENERIC))
        if everything:
            of_the_country = run(local.format(where=""), (country, match, POOL_ALL))
            found += of_the_country
            # A country with that many matches of its own fills every page a
            # person will read: ranking the products of the rest of the world
            # as well would double the time for results nobody reaches.
            enough_of_the_country = len(of_the_country) >= POOL_ALL
    for where, bound in ranges:
        found += run(plain.format(where=where), (match, bound, POOL_GENERIC))
    if everything and not enough_of_the_country:
        found += run(plain.format(where=""), (match, POOL_ALL))
    elif not everything and not ranges:
        # The products do not occupy one range in this file: take the first
        # matches as they come, which costs nothing, rather than rank them all.
        found += run(
            "SELECT rowid FROM foods_fts WHERE foods_fts MATCH ? LIMIT ?", (match, POOL_GENERIC)
        )
    return list(dict.fromkeys(found))


def _barcode_matches(context: Context, query: Query) -> tuple[str | None, list[int]]:
    """Look the digits typed up as a barcode.

    Args:
        context: Both databases.
        query: The query.

    Returns:
        The canonical barcode when the digits are a whole valid one, and the
        internal numbers of the foods whose barcode begins with the digits.
    """
    if not query.digits:
        return None, []
    exact = normalize_gtin(query.digits)
    found: list[int] = []
    if MIN_BARCODE_PREFIX <= len(query.digits) <= 13:
        # A UPC typed on 12 digits is stored with one more zero in front.
        for prefix in (query.digits, "0" + query.digits):
            rows = context.base.execute(
                "SELECT pk FROM foods WHERE barcode >= ? AND barcode < ? ORDER BY barcode LIMIT ?",
                (prefix, prefix + ":", POOL_BARCODES),
            ).fetchall()
            found += [row[0] for row in rows]
    return exact, found


def _rank(
    food: Food, query: Query, barcode: str | None, country: str | None, served: dict
) -> tuple:
    """Build the sort key of a food among the results.

    Args:
        food: The food.
        query: The query.
        barcode: The barcode typed, when the text is one.
        country: Country of the request, if any.
        served: The version of the food this request gets.

    Returns:
        A key sorting the results as the module describes.
    """
    best = 2
    shortest = 99
    for name, _origin in food.names.values():
        folded = fold(name)
        if folded == query.text:
            closeness = 0
        elif folded.startswith(query.text):
            closeness = 1
        else:
            closeness = 2
        if closeness < best:
            best, shortest = closeness, len(folded.split())
        elif closeness == best:
            shortest = min(shortest, len(folded.split()))
    return (
        0 if barcode and food.barcode == barcode else 1,
        0 if country and country in served["countries"] else 1,
        best,
        0 if food.kind == KIND_GENERIC else 1,
        -food.popularity,
        shortest,
        food.id,
    )


def search(
    context: Context,
    text: str,
    *,
    country: str | None = None,
    kind: str | None = None,
    drink: bool | None = None,
    budget: float = 0.5,
) -> list[Food]:
    """Find the foods matching what a person typed, best first.

    Args:
        context: Both databases.
        text: The text typed.
        country: Country of the request: its foods come first.
        kind: Keep only `generic` foods or only `product`s.
        drink: Keep only the drinks, or only what is not a drink.
        budget: Seconds the search of every food may take.

    Returns:
        The foods, in the order of the results; empty when the text holds no
        letter or digit.
    """
    query = parse(text)
    if not query.words:
        return []
    barcode, by_barcode = _barcode_matches(context, query)
    pks = list(by_barcode)
    live_ids = live.search(context.live, match_expression(query), POOL_LIVE)
    rows = []
    if barcode:
        row = catalogue.food_by_barcode(context.base, barcode)
        if row is not None:
            rows.append(row)
        else:
            product = live.product_by_barcode(context.live, barcode)
            if product is not None:
                live_ids.append(product["food_id"])
    pks += _index_matches(context, query, country, kind == KIND_GENERIC, budget)
    seen = {row["pk"] for row in rows}
    rows += catalogue.foods_by_pk(context.base, [pk for pk in dict.fromkeys(pks) if pk not in seen])
    in_base = {row["id"] for row in rows}
    # A food the live database names (a name added to it) may be in the main
    # database without having matched there.
    missing = [food_id for food_id in live_ids if food_id not in in_base]
    rows += catalogue.foods_by_id(context.base, missing)
    found = foods.assemble(context, rows, live_ids)
    ranked = []
    for food in found.values():
        if kind and food.kind != kind:
            continue
        if drink is not None and food.is_drink != drink:
            continue
        served = food.served(country, context.user_version_votes)
        ranked.append((_rank(food, query, barcode, country, served), food))
    ranked.sort(key=lambda item: item[0])
    return [food for _, food in ranked]
