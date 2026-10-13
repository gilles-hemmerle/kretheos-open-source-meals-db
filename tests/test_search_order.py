"""Tests of the order of a search of everything, on foods written by hand.

The foods are those the real database has for "big mac", "cheeseburger" and
"frites": a reference food of a table, what a restaurant chain publishes for
a country, and the entries of Open Food Facts with their scans. No database
is needed: `search._order` only reads a food and the version it is served by.
"""

from __future__ import annotations

from kratheos_food_data.service import search
from kratheos_food_data.service.foods import Food, chain_of


def a_food(
    food_id: str,
    name: str,
    *,
    language: str = "fr",
    countries: tuple[str, ...] = ("FR",),
    brand: str | None = None,
    scans: int = 0,
) -> tuple[Food, dict]:
    """Write a food as a search finds it, with the version it is served by.

    Args:
        food_id: Identifier: `gtin:` or `off:` for an entry of Open Food
            Facts, `chain:` for a product of a chain, else a reference food.
        name: Its name.
        language: Language of the name.
        countries: Countries of its values.
        brand: Its brand.
        scans: Popularity of Open Food Facts.

    Returns:
        The food and its version.
    """
    product = food_id.startswith(("gtin:", "off:", "chain:", "kratheos:"))
    food = Food(
        id=food_id,
        kind="product" if product else "generic",
        barcode=food_id[5:] if food_id.startswith("gtin:") else None,
        names={language: (name, "source")},
        name=name,
        name_lang=language,
        brand=brand,
        category=None,
        is_drink=False,
        popularity=scans,
    )
    return food, {"countries": list(countries)}


def found(
    foods: list[tuple[Food, dict]],
    text: str,
    language: str,
    country: str,
    stand_in: bool = False,
) -> list[str]:
    """Put foods in the order of a search of everything.

    Args:
        foods: The foods found, each with its version.
        text: The text typed.
        language: Language of the request.
        country: Country of the request.
        stand_in: Whether no reference food is the text typed.

    Returns:
        The identifiers, best first.
    """
    query = search.parse(text)
    keys = {
        food.id: search._order(food, query, None, language, country, served, None, stand_in)
        for food, served in foods
    }
    return sorted(keys, key=keys.get)


BIG_MAC = [
    a_food("usda:fndds:27510389", "Big Mac (McDonalds)", language="en", countries=("US",)),
    a_food("off:2000000002603", "Big Mac", brand="McDonald's", countries=("BE", "FR"), scans=68),
    a_food("gtin:4210101939130", "Big Mac", brand="McDonald's", scans=1),
    a_food("chain:mcdonald-s:fr:13111", "Big Mac", brand="McDonald's"),
    a_food("chain:mcdonald-s:ch:200008", "Big Mac", brand="McDonald's", countries=("CH",)),
    a_food("off:2000000056723", "Big Mac™ Bacon", brand="McDonald's", scans=8),
    a_food("chain:mcdonald-s:fr:13112", "Big Mac Bacon", brand="McDonald's"),
]


def test_what_a_chain_publishes_comes_before_the_crowd_entries_of_its_products() -> None:
    """A chain has no scans: it is ranked by the name, like the food of a table."""
    assert chain_of(BIG_MAC[3][0]) == "McDonald's"
    # A crowd entry of the brand is not the chain's, nor is a reference food.
    assert chain_of(BIG_MAC[1][0]) is None and chain_of(BIG_MAC[0][0]) is None
    assert found(BIG_MAC, "big mac", "fr", "FR") == [
        "chain:mcdonald-s:fr:13111",  # the product typed, of the country
        "usda:fndds:27510389",  # the reference food, in another language
        "off:2000000002603",  # then the entries of Open Food Facts, by their scans
        "gtin:4210101939130",
        "chain:mcdonald-s:fr:13112",  # begins with the text: the chain before the crowd
        "off:2000000056723",
        "chain:mcdonald-s:ch:200008",  # of another country: after those of France
    ]
    # In Switzerland the Swiss figures; those of France are products of another
    # country, where the chain still comes before the crowd.
    assert found(BIG_MAC, "big mac", "fr", "CH")[:4] == [
        "chain:mcdonald-s:ch:200008",
        "usda:fndds:27510389",
        "chain:mcdonald-s:fr:13111",
        "off:2000000002603",
    ]


FRIES = [
    a_food("ciqual:4032", "Frites de pommes de terre, surgelées, cuites en friteuse"),
    a_food("gtin:3599741007593", "Frites", brand="Findus", scans=140),
    a_food("chain:o-tacos:fr:frites", "FRITES", brand="O'Tacos"),
    a_food("chain:mcdonald-s:fr:3000", "La petite Portion de Frites", brand="McDonald's"),
    a_food("off:2000000003781", "Frites Moyenne", brand="McDonald's", scans=9),
    a_food("off:2000000016506", "Frites de légumes", brand="McDonald's", scans=1),
]


def test_a_generic_word_has_its_reference_food_first_then_the_chains() -> None:
    """For "frites": the fries of the table, those of the chains, the bags of the shops."""
    # No table names a food "Frites": the one that begins with it stands in.
    assert found(FRIES, "frites", "fr", "FR", stand_in=True) == [
        "ciqual:4032",
        "chain:o-tacos:fr:frites",
        "gtin:3599741007593",
        "off:2000000003781",
        "off:2000000016506",
        "chain:mcdonald-s:fr:3000",  # holds the word: after the names that begin with it
    ]
    # In another language than that of the request it does not stand in, no more than
    # where a table has the food typed: it keeps its place among the products, after
    # the names equal to the text.
    assert found(FRIES, "frites", "de", "FR", stand_in=True)[:2] == [
        "chain:o-tacos:fr:frites",
        "gtin:3599741007593",
    ]
    assert found(FRIES, "frites", "fr", "FR")[:3] == [
        "chain:o-tacos:fr:frites",
        "gtin:3599741007593",
        "ciqual:4032",
    ]
    # The chain and a word: its products of the country before the crowd entries.
    of_the_chain = [entry for entry in FRIES if entry[0].brand == "McDonald's"]
    assert found(of_the_chain, "mcdonald's frites", "fr", "FR") == [
        "chain:mcdonald-s:fr:3000",
        "off:2000000003781",
        "off:2000000016506",
    ]
