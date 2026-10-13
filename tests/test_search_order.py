"""Tests of the order of a search of everything, on foods written by hand.

The foods are those the real database has for "big mac", "frites", "coca
cola", "rivella" and "ketchup", with the scans they have there: a reference
food of a table, what a restaurant chain publishes for a country, and the
entries of Open Food Facts. No database is needed: the order only reads the
foods found and the version each is served by.
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
    shops = search._of_the_shops(foods, query, country)
    keys = {
        food.id: search._order(food, query, None, language, country, served, None, stand_in, *shops)
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
    a_food("gtin:3599741007593", "Frites", brand="Findus", scans=25),
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
    # The most scanned fries of the shops have 25 scans: "frites" stays a dish.
    assert search._of_the_shops(FRIES, search.parse("frites"), "FR") == (False, search.CHAIN_SCANS)
    # The chain and a word: its products of the country before the crowd entries.
    of_the_chain = [entry for entry in FRIES if entry[0].brand == "McDonald's"]
    assert found(of_the_chain, "mcdonald's frites", "fr", "FR") == [
        "chain:mcdonald-s:fr:3000",
        "off:2000000003781",
        "off:2000000016506",
    ]


COLA = [
    a_food("gtin:5449000214911", "Coca-Cola Original", brand="Coca-Cola", scans=763),
    a_food("gtin:54491472", "coca-cola", brand="Coca-Cola", scans=379),
    a_food("gtin:5000112558272", "COCA COLA 50CL", brand="Coca-Cola", scans=123),
    a_food("gtin:42105244", "Coca-Cola", brand="Coca-Cola", scans=76),
    a_food("gtin:5449000000453", "Coca-Cola", brand="Coca-Cola, coca", scans=29),
    a_food("off:2000000087511", "Coca-Cola", brand="McDonald's", scans=3),
    a_food("chain:o-tacos:fr:coca-cola", "COCA COLA", brand="O'Tacos"),
    a_food("chain:burger-king:fr:80101", "Coca-Cola (25cl)", brand="Burger King"),
    a_food(
        "chain:mcdonald-s:ch:200042", "Coca-Cola (0,4 L)", brand="McDonald's", countries=("CH",)
    ),
]


def test_a_chain_counts_for_a_hundred_scans_among_the_products() -> None:
    """More than the copies people made of a menu, fewer than what people buy in shops."""
    assert search.CHAIN_SCANS == 100
    # Under the name of a product of the shops the chains are products among others:
    # the bottles people scan, the cup of the chain, the bottles they scan less.
    assert search._of_the_shops(COLA, search.parse("coca cola"), "FR") == (True, 100)
    assert found(COLA, "coca cola", "fr", "FR") == [
        "gtin:54491472",  # the names equal to the text, by their scans
        "chain:o-tacos:fr:coca-cola",  # 100
        "gtin:42105244",
        "gtin:5449000000453",
        "off:2000000087511",  # the copy of a menu: after the chain
        "gtin:5449000214911",  # the names that begin with the text
        "gtin:5000112558272",
        "chain:burger-king:fr:80101",
        "chain:mcdonald-s:ch:200042",  # of another country
    ]
    # Typed with its chain, the cup is what was asked for.
    assert found(COLA, "o'tacos coca cola", "fr", "FR")[0] == "chain:o-tacos:fr:coca-cola"
    assert found(COLA, "burger king", "fr", "FR")[0] == "chain:burger-king:fr:80101"
    # The bottles are those of the country of the request: none of these is sold in
    # Switzerland, where the cup of the chain is all there is of that name.
    assert search._of_the_shops(COLA, search.parse("coca cola"), "CH") == (False, 100)
    assert found(COLA, "coca cola", "fr", "CH")[0] == "chain:mcdonald-s:ch:200042"
    # A dish of a chain meets its copies only: it passes the most scanned of them.
    assert search._of_the_shops(BIG_MAC, search.parse("big mac"), "FR") == (False, 100)
    scans = {food.id: food.popularity for food, _ in BIG_MAC}
    assert max(scans.values()) == 68 < search.CHAIN_SCANS


RIVELLA = [
    a_food("gtin:7610097111072", "Rivella", brand="Rivella", scans=22, countries=("CH",)),
    a_food("gtin:7610097111485", "Rivella rot", brand="rivella", scans=15, countries=("CH",)),
    a_food("gtin:7610097112000", "Rivella", brand="Rivella", scans=1, countries=("CH",)),
    a_food("chain:mcdonald-s:ch:200653", "Rivella", brand="McDonald's", countries=("CH",)),
    a_food("chain:starbucks:ch:98331", "Rivella Red", brand="Starbucks", countries=("CH",)),
]
PAIN_AU_CHOCOLAT = [
    a_food("ciqual:7730", "Pain au chocolat, artisanal"),
    a_food("gtin:3256540001305", "Pain au chocolat", brand="Pasquier", scans=3),
    a_food("gtin:3760049790214", "Pain au chocolat", brand="Pain au chocolat", scans=1),
    a_food("chain:mcdonald-s:fr:14939", "Pain au Chocolat", brand="McDonald's"),
]
KETCHUP = [
    a_food("swiss:1163", "Ketchup", countries=("CH",)),
    a_food("gtin:87157215", "Ketchup", scans=421, countries=("CH", "DE")),
    a_food("gtin:7610200012345", "Ketchup", brand="Heinz", scans=12, countries=("CH",)),
    a_food("gtin:2000000004327", "Ketchup", brand="Migros", scans=250, countries=("CH",)),
    a_food("chain:burger-king:ch:item_25445", "Ketchup", brand="Burger King", countries=("CH",)),
]


def test_the_name_of_a_brand_of_the_shops_begins_with_its_bottle_whatever_its_scans() -> None:
    """A Rivella has 22 scans, fewer than the copy of a Big Mac: its brand tells it."""
    # Two bottles named so bear the text typed as their brand: the chain counts for
    # one scan fewer than the most scanned of them, and is not the product typed.
    assert search._of_the_shops(RIVELLA, search.parse("rivella"), "CH") == (True, 21)
    assert found(RIVELLA, "rivella", "fr", "CH") == [
        "gtin:7610097111072",  # the names equal to the text: the bottle, 22 scans
        "chain:mcdonald-s:ch:200653",  # 21
        "gtin:7610097112000",
        "chain:starbucks:ch:98331",  # the names that begin with it
        "gtin:7610097111485",
    ]
    # With a hundred scans as for a dish, the cup would come before every bottle.
    assert all(food.popularity < search.CHAIN_SCANS for food, _ in RIVELLA)
    assert found(RIVELLA, "mcdonald's rivella", "fr", "CH")[0] == "chain:mcdonald-s:ch:200653"
    # One product alone that has its name for a brand is the mistake of its author:
    # "pain au chocolat" stays a dish, and the chain comes after the table.
    query = search.parse("pain au chocolat")
    assert search._of_the_shops(PAIN_AU_CHOCOLAT, query, "FR") == (False, 100)
    assert found(PAIN_AU_CHOCOLAT, "pain au chocolat", "fr", "FR") == [
        "ciqual:7730",
        "chain:mcdonald-s:fr:14939",
        "gtin:3256540001305",
        "gtin:3760049790214",
    ]


def test_what_people_buy_in_shops_is_told_by_its_scans_where_it_has_no_brand_of_that_name() -> None:
    """A ketchup: the table, the bottle with 421 scans, the sachet of the chain, the rest."""
    assert search._of_the_shops(KETCHUP, search.parse("ketchup"), "CH") == (True, 100)
    assert found(KETCHUP, "ketchup", "fr", "CH") == [
        "swiss:1163",  # the reference food typed comes first as ever
        "gtin:87157215",  # the bottle people scan
        "chain:burger-king:ch:item_25445",  # 100
        "gtin:7610200012345",
        "gtin:2000000004327",  # a label of the counter: last, and not what tells a shop
    ]
    # Without the bottle people scan, the label of a counter does not make a ketchup
    # a product of the shops, whatever its scans: the copies of a menu bear such numbers.
    others = [entry for entry in KETCHUP if entry[0].id != "gtin:87157215"]
    assert search._of_the_shops(others, search.parse("ketchup"), "CH") == (False, 100)
    assert found(others, "ketchup", "fr", "CH")[:2] == [
        "swiss:1163",
        "chain:burger-king:ch:item_25445",
    ]
    # Without a country every product is looked at, and no chain comes first.
    assert search._of_the_shops(KETCHUP, search.parse("ketchup"), None) == (True, 100)
