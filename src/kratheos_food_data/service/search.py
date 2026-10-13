"""Search as a person types, on the stored full-text index, in bounded time.

What is searched. The words typed are looked for in every name of a food, in
every language, and in its brand; each word is taken as the beginning of a
word, and all of them must be found. "œuf" and "oeuf" are one word: the
index keeps the ligature, so both spellings are asked of it. A text made of
digits is also a barcode: the food carrying it comes first, then the
barcodes beginning with those digits.

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
  answer holds what was found until then, the reference foods first: late
  results are dropped, never a late answer.

Reference foods (`kind=generic`). The 27 000 generic foods of the
food-composition tables sit in a few blocks of the index, so all those that
match are read, not the best few of the index, whose own order prefers short
names and knows nothing of foods. They are then put in the order of
`ranking.py`: language of the request, a name that is or begins with the
text, the table of the country, how the name matches, plain before
processed, shortest name. A text matching more than 2 000 of them (two
letters, "de", "with") falls back to the 300 the index ranks best in each
block.

Products (`kind=product`). The index ranks the matches and gives its best
ones; several such lists are put together: the best foods without barcode,
the best of everything, and, when the request names a country, the best of
each among the foods of that country, so that the products of a small
country are found even for a word that half the world uses. (When the
country alone has 120 matches or more, the barcoded products of the rest of
the world are not ranked as well: they would come after every page a person
reads, and ranking them doubles the time.) They are ordered by:

1. the food carrying the barcode typed;
2. a barcode that is not a 13-digit in-store number before one that is;
3. the foods of the country of the request (the version served lists it);
4. a name equal to the text, then a name beginning with it, then the rest;
5. what a restaurant chain publishes for its products before the entries of
   Open Food Facts: these are the figures of the publisher for that country;
6. the most scanned products first (the popularity of Open Food Facts);
7. the shortest name, then the identifier, so that pages are stable.

Products of restaurant chains. A chain has no barcode and nobody scans its
menu, so by popularity alone its products came after every crowd entry of
the same name that was scanned once. They are ranked as the reference foods
are, by their name (`ranking.py`): in the country of the request, a product
whose name, or first part, is the text typed, or whose chain is ("big mac",
"mcdonald's", "mcdonald's big mac"), comes first with the reference foods
that are the text typed, the table of the country before its chains; any
other product of a chain, and those of other countries, go by the order
above.

In-store barcodes. A shop numbers what it weighs and packs itself with
numbers of its own (GTIN-13 beginning with 02, 04 or 20 to 29, and the 8
digit numbers beginning the same way: `barcode.is_in_store`). The 13-digit
ones are the labels of the counter: 320 000 records hardly ever scanned
twice, which by their short names ("Poulet", "Pain complet") were most of
what the index ranked best. They come after every other product: the best
matches of the index are read past them; when 120 other products match,
those fill the answer and none of them is in it; when fewer do, the best 20
of them are added at the very end. The 8-digit ones are the own-brand
products of Lidl, Aldi, Penny or Marks & Spencer: they rank like any
product. A lookup by barcode is not concerned.

Without `kind`, both are searched: the reference foods whose name, or first
part, is the text typed come first, in their own order (40 at most), with
the products of the chains that are the text typed. When no table has a food
that is the text typed, the reference foods whose first part begins with it,
in the language of the request, stand in: nobody names a food "Frites",
Ciqual has "Frites de pommes de terre, surgelées, cuites en friteuse", and a
person who types "frites" gets it before the fries of the chains and the
bags of Open Food Facts.
Everything else follows
in the order of the products, generic foods, then products of chains, before
the other products where that order does not tell them apart.

What each question costs. Asking the index anything about a word costs the
time of reading that word in it, however few rows are wanted, so the
questions are counted: the reference foods are read once per block; where
the foods placed before (or after) the barcoded products are all reference
foods, their best matches are taken from what was just read, with the rank
the index gave, instead of being asked again, and a search of products does
not look there at all.

What the live database holds (products added, names added) is searched in
its own small index and merged in, so it is found at once.
"""

from __future__ import annotations

import heapq
import re
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from ..barcode import is_in_store_label, normalize_gtin
from ..schema import KIND_GENERIC, KIND_PRODUCT
from . import catalogue, foods, live, ranking
from .foods import Context, Food
from .ranking import fold

MAX_QUERY_CHARS = 100
MAX_WORDS = 8
SHORT_QUERY_CHARS = 3
POOL_GENERIC = 50
POOL_ALL = 120
POOL_IN_STORE = 20
# Rows of a ranked list read at most to find its best ones past the labels
# of the counter, and rows asked of the database at a time.
POOL_SCAN = 20_000
POOL_FETCH = 128
POOL_LIVE = 60
POOL_BARCODES = 40
# Reference foods: all those matching are ordered, up to this many, beyond
# which the best of the index in each block are taken instead.
REFERENCE_MATCHES = 2000
REFERENCE_RANKED = 300
# Reference foods kept for a search of reference foods, and, in a search of
# everything, those whose name or first part is the text typed.
REFERENCE_KEPT = 200
REFERENCE_FIRST = 40
MIN_BARCODE_PREFIX = 4
# Whose figures a food carries, where the order of the products does not tell
# two foods apart: a food-composition table, a restaurant chain for its own
# products, anyone else (the crowd entries of Open Food Facts, what people add).
FROM_A_TABLE = 0
FROM_ITS_CHAIN = 1
FROM_ANYONE = 2
PROGRESS_STEPS = 20_000
_SEPARATORS = re.compile(r"[\s\-]")


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
        one letter a whole word. A word the index may hold with a ligature
        is asked in both spellings: `("oeuf"* OR "œuf"*)`. Empty when there
        is no word.
    """
    terms = []
    for word in query.words:
        prefix = "*" if len(word) > 1 else ""
        asked = [f'"{spelling}"{prefix}' for spelling in ranking.spellings(word)]
        terms.append(asked[0] if len(asked) == 1 else f"({' OR '.join(asked)})")
    # Words in a row are "all of them" for the index, but not next to a
    # parenthesis: the word between them is then written.
    return (" AND " if any(term.startswith("(") for term in terms) else " ").join(terms)


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
    if time.monotonic() > deadline:
        return None
    connection.set_progress_handler(lambda: time.monotonic() > deadline, PROGRESS_STEPS)
    try:
        return connection.execute(sql, parameters).fetchall()
    except sqlite3.OperationalError:
        return None
    finally:
        connection.set_progress_handler(None, 0)


def _best_within(
    connection: sqlite3.Connection,
    sql: str,
    parameters: tuple,
    deadline: float,
    count: int,
    is_label: Callable[[int], bool],
) -> tuple[list[int], list[int]]:
    """Read the best rows of a ranked query, past the labels of the counter.

    The rows are read as the index gives them, best first, and the reading
    stops as soon as enough are kept: the labels met on the way cost a row
    each, not a query.

    Args:
        connection: A connection to the main database.
        sql: The query, ordered by rank.
        parameters: Its parameters.
        deadline: Time, on the monotonic clock, after which it is stopped.
        count: How many rows to keep.
        is_label: Tells whether an internal number is that of a product with
            a 13-digit in-store barcode.

    Returns:
        The rows kept, best first, and the labels met before the reading
        stopped, best first too. Both are empty when the time was spent.
    """
    kept: list[int] = []
    labels: list[int] = []
    if time.monotonic() > deadline:
        return kept, labels
    connection.set_progress_handler(lambda: time.monotonic() > deadline, PROGRESS_STEPS)
    cursor = None
    try:
        cursor = connection.execute(sql, parameters)
        while len(kept) < count:
            rows = cursor.fetchmany(POOL_FETCH)
            if not rows:
                break
            for row in rows:
                if is_label(row[0]):
                    labels.append(row[0])
                elif len(kept) < count:
                    kept.append(row[0])
    except sqlite3.OperationalError:
        pass
    finally:
        if cursor is not None:
            cursor.close()
        connection.set_progress_handler(None, 0)
    return kept, labels


@dataclass
class Block:
    """What a text matches in one block of reference foods.

    Attributes:
        low: Lowest internal number of the block.
        high: Highest internal number of the block.
        ranked: Whether the ranks of the index were asked for.
        rows: Internal number and rank of each reference food matching; the
            rank is 0 when it was not asked for.
    """

    low: int
    high: int
    ranked: bool
    rows: list[tuple[int, float]] = field(default_factory=list)


def best_of(rows: list[tuple[int, float]], count: int) -> list[int]:
    """Pick the rows the index ranks best, as its own `ORDER BY rank LIMIT` does.

    Args:
        rows: Internal numbers with their rank; the lowest rank is the best.
        count: How many to keep.

    Returns:
        The internal numbers kept, best first, equal ranks by number.
    """
    return [pk for pk, _ in heapq.nsmallest(count, rows, key=lambda row: (row[1], row[0]))]


def _read_blocks(context: Context, query: Query, deadline: float, with_ranks: bool) -> list[Block]:
    """Read every reference food matching a text, block by block.

    Args:
        context: Both databases.
        query: The query.
        deadline: Time after which the index is no longer asked.
        with_ranks: Ask for the rank of the index where the block lies among
            reference foods only: its best matches then need not be asked
            again. A rank costs the reading of the whole word in the index,
            so it is not asked where it would not be used.

    Returns:
        One entry per block of the generation, in their order.
    """
    generation = context.generation
    match = match_expression(query)
    blocks = []
    for low, high in generation.reference_blocks:
        block = Block(low, high, with_ranks and generation.only_reference(low, high))
        rows = _within(
            context.base,
            f"SELECT rowid, {'rank' if block.ranked else '0'} FROM foods_fts"
            " WHERE foods_fts MATCH ? AND rowid BETWEEN ? AND ?",
            (match, low, high),
            deadline,
        )
        block.rows = [(pk, rank) for pk, rank in rows or () if pk in generation.reference_pks]
        blocks.append(block)
    return blocks


def _reference_order(
    context: Context,
    query: Query,
    blocks: list[Block],
    language: str | None,
    country: str | None,
    deadline: float,
    only_the_nearest: bool = False,
) -> tuple[list[tuple[tuple, int]], int]:
    """Put the reference foods matching a text in the order of `ranking.py`.

    Args:
        context: Both databases.
        query: The query.
        blocks: What the text matches in each block.
        language: Language of the request, if any.
        country: Country of the request, if any.
        deadline: Time after which the index is no longer asked.
        only_the_nearest: Keep only the foods ranked by a name that is, or
            whose first part is, the text typed; when there is none, those
            whose first part begins with the words typed stand in.

    Returns:
        The sort key and the internal number of each reference food kept,
        best first; and whether those kept stand in for the text typed,
        none being it. The keys are computed on the names of the main
        database; what the live database adds is counted when the foods are
        put together.
    """
    generation = context.generation
    if sum(len(block.rows) for block in blocks) <= REFERENCE_MATCHES:
        pks = [pk for block in blocks for pk, _ in block.rows]
    else:
        pks = []
        for block in blocks:
            if block.ranked:
                pks += best_of(block.rows, REFERENCE_RANKED)
                continue
            rows = _within(
                context.base,
                "SELECT rowid FROM foods_fts WHERE foods_fts MATCH ? AND rowid BETWEEN ? AND ?"
                " ORDER BY rank LIMIT ?",
                (match_expression(query), block.low, block.high, REFERENCE_RANKED),
                deadline,
            )
            pks += [row[0] for row in rows or () if row[0] in generation.reference_pks]
    read = catalogue.reference_names(context.base, pks)

    def keyed(everything: bool, stand_in: bool = False) -> list[tuple[tuple, int]]:
        """Order the foods: all of them, those typed, or those standing in."""
        found = []
        for pk, (food_id, countries, names) in read.items():
            if not everything and not stand_in:
                # Thousands of names may match: most are told apart cheaply.
                if not ranking.may_be_the_food_typed(names, query.words):
                    continue
            key = ranking.reference_key(names, query.words, language, countries, country, food_id)
            if everything or ranking.comes_first(key, stand_in):
                found.append((key, pk))
        found.sort()
        return found

    if not only_the_nearest:
        return keyed(everything=True), False
    found = keyed(everything=False)
    if found:
        return found, False
    return keyed(everything=False, stand_in=True), True


def _index_matches(
    context: Context,
    query: Query,
    country: str | None,
    deadline: float,
    *,
    generic_only: bool = False,
    products_only: bool = False,
    blocks: list[Block] | None = None,
) -> list[int]:
    """Ask the full-text index of the main database for the best matches.

    Args:
        context: Both databases.
        query: The query.
        country: Country of the request, if any.
        deadline: Time after which the index is no longer asked.
        generic_only: Leave the barcoded products out, whatever the length.
        products_only: Only products are wanted: where nothing but reference
            foods is, the index is not asked.
        blocks: What was already read of the reference foods, if it was.

    Returns:
        Internal numbers of foods: the best foods without barcode and, when
        the text is long enough, the best of everything that is not a label
        of the counter (a 13-digit in-store barcode); each list first among
        the foods of the country; then, when those did not fill their list,
        the best of the labels. What the time did not allow is missing.
    """
    match = match_expression(query)
    if not match:
        return []
    generation = context.generation
    everything = not query.short and not generic_only
    in_store = generation.in_store_blocks
    ranked_anyway = generation.in_store_ranked
    plain = "SELECT rowid FROM foods_fts WHERE foods_fts MATCH ? {where} ORDER BY rank LIMIT ?"
    local = (
        "SELECT m.rowid FROM foods_fts AS m"
        " JOIN food_countries AS c ON c.country = ? AND c.food_pk = m.rowid"
        " WHERE m.foods_fts MATCH ? {where} ORDER BY m.rank LIMIT ?"
    )
    # The foods without a barcode: those placed before the barcoded products,
    # those placed after. For each, the condition, its bound, whether nothing
    # but reference foods is there, and the blocks lying there.
    ranges = []
    if generation.first_gtin is not None:
        first, last = generation.first_gtin, generation.last_gtin
        read = blocks or []
        ranges = [
            (
                "AND rowid < ?",
                first,
                generation.reference_only_below,
                [block for block in read if block.high < first],
            ),
            (
                "AND rowid > ?",
                last,
                generation.reference_only_above,
                [block for block in read if block.low > last],
            ),
        ]

    def run(sql: str, parameters: tuple) -> list[int]:
        """Run one search, unless the time is spent."""
        return [row[0] for row in _within(context.base, sql, parameters, deadline) or ()]

    def is_label(pk: int) -> bool:
        """Tell whether a product has a 13-digit in-store barcode."""
        return any(low <= pk <= high for low, high in in_store) and pk not in ranked_anyway

    def best(sql: str, parameters: tuple) -> tuple[list[int], list[int]]:
        """Run one search of everything: its best rows, and the labels met."""
        return _best_within(context.base, sql, parameters, deadline, POOL_ALL, is_label)

    found: list[int] = []
    of_the_country = generation.reference_by_country.get(country or "", frozenset())
    for where, bound, only_reference, inside in ranges:
        if only_reference and products_only:
            continue
        if only_reference and blocks is not None and all(block.ranked for block in inside):
            # Every match of this range was just read with its rank: its best
            # ones are known without asking the index again.
            rows = [row for block in inside for row in block.rows]
            if country:
                found += best_of([row for row in rows if row[0] in of_the_country], POOL_GENERIC)
            found += best_of(rows, POOL_GENERIC)
            continue
        if country:
            sql = local.format(where=where.replace("rowid", "m.rowid"))
            found += run(sql, (country, match, bound, POOL_GENERIC))
        found += run(plain.format(where=where), (match, bound, POOL_GENERIC))
    if everything:
        others: list[int] | None = None
        labels: list[int] = []
        if country:
            others, labels = best(local.format(where=""), (country, match, POOL_SCAN))
            found += others
        # A country with that many matches of its own fills every page a
        # person will read: ranking the products of the rest of the world as
        # well would double the time for results nobody reaches.
        if others is None or len(others) < POOL_ALL:
            others, labels = best(plain.format(where=""), (match, POOL_SCAN))
            found += others
        if len(others) < POOL_ALL:
            # Fewer other products match than a list holds, so the labels of
            # the counter have their place after them.
            found += labels[:POOL_IN_STORE]
    elif not ranges:
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


def _order(
    food: Food,
    query: Query,
    barcode: str | None,
    language: str | None,
    country: str | None,
    served: dict,
    kind: str | None,
    stand_in: bool = False,
) -> tuple:
    """Build the sort key of a food among the results.

    Args:
        food: The food.
        query: The query.
        barcode: The barcode typed, when the text is one.
        language: Language of the request, if any.
        country: Country of the request, if any.
        served: The version of the food this request gets.
        kind: The kind asked for, if any.
        stand_in: Whether no reference food is the text typed, so that
            those that begin with it come first (see `_reference_order`).

    Returns:
        A key sorting the results as the module describes: the barcode
        typed; then the reference foods in the order of `ranking.py` (all of
        them in a search of reference foods, those that are the food typed
        otherwise, or that begin with it when none is) and, in the same
        order, the products of the chains that are the food typed; then the
        rest in the order of the products.
    """
    typed_barcode = 0 if barcode and food.barcode == barcode else 1
    chain = foods.chain_of(food)
    if food.kind == KIND_GENERIC or chain is not None:
        names = {lang: entry[0] for lang, entry in food.names.items()}
        reference = ranking.reference_key(
            names, query.words, language, served["countries"], country, food.id, chain
        )
        if kind == KIND_GENERIC or ranking.comes_first(reference, stand_in):
            return (typed_barcode, 0, *reference)
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
    if food.kind == KIND_GENERIC:
        whose = FROM_A_TABLE
    else:
        whose = FROM_ANYONE if chain is None else FROM_ITS_CHAIN
    return (
        typed_barcode,
        1,
        1 if is_in_store_label(food.barcode) else 0,
        0 if country and country in served["countries"] else 1,
        best,
        whose,
        -food.popularity,
        shortest,
        food.id,
    )


def search(
    context: Context,
    text: str,
    *,
    language: str | None = None,
    country: str | None = None,
    kind: str | None = None,
    drink: bool | None = None,
    budget: float = 0.5,
) -> list[Food]:
    """Find the foods matching what a person typed, best first.

    Args:
        context: Both databases.
        text: The text typed.
        language: Language of the request: a reference food named in it
            comes before one that only matches in another language.
        country: Country of the request: its foods come first.
        kind: Keep only `generic` foods or only `product`s.
        drink: Keep only the drinks, or only what is not a drink.
        budget: Seconds the full-text index may take.

    Returns:
        The foods, in the order of the results; empty when the text holds no
        letter or digit.
    """
    query = parse(text)
    if not query.words:
        return []
    deadline = time.monotonic() + budget
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
    # Where the reference foods sit in blocks, all those matching are read
    # first; the lists of the index are then only needed for the rest.
    blocks = None
    stand_in = False
    if kind != KIND_PRODUCT and context.generation.reference_blocks:
        everything = kind != KIND_GENERIC
        blocks = _read_blocks(context, query, deadline, with_ranks=everything)
        references, stand_in = _reference_order(
            context, query, blocks, language, country, deadline, only_the_nearest=everything
        )
        pks += [pk for _, pk in references[: REFERENCE_FIRST if everything else REFERENCE_KEPT]]
    if kind != KIND_GENERIC or blocks is None:
        pks += _index_matches(
            context,
            query,
            country,
            deadline,
            generic_only=kind == KIND_GENERIC,
            products_only=kind == KIND_PRODUCT,
            blocks=blocks,
        )
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
        key = _order(food, query, barcode, language, country, served, kind, stand_in)
        ranked.append((key, food))
    ranked.sort(key=lambda item: item[0])
    return [food for _, food in ranked]
