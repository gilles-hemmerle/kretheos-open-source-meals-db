"""Tests of the order of the reference foods: the rules on the words of a name.

The names are those the food-composition tables really write (Ciqual, the
Swiss table, the Canadian file, USDA, CoFID), so that each rule is tested on
the case that asked for it. No database is needed: the rules only read names.
"""

from __future__ import annotations

import pytest

from kratheos_food_data.service import ranking
from kratheos_food_data.service.ranking import (
    FORM_COOKED,
    FORM_PROCESSED,
    FORM_RAW,
    FORM_UNSAID,
    MATCH_EVERY_BEGINNING,
    MATCH_EVERY_WORD,
    MATCH_FIRST_LETTERS,
    MATCH_FIRST_PART,
    MATCH_FIRST_WORDS,
    MATCH_NAME,
    MATCH_NONE,
)


def typed(text: str) -> tuple[str, ...]:
    """Give the words of a text as a search folds them."""
    return tuple(ranking.fold(text).split())


def ordered(
    foods: dict[str, tuple[str, dict[str, str]]],
    text: str,
    language: str | None,
    country: str | None,
) -> list[str]:
    """Put foods in the order of a search of reference foods.

    Args:
        foods: For each identifier, the country of the table and the names.
        text: The text typed.
        language: Language of the request.
        country: Country of the request.

    Returns:
        The identifiers, best first.
    """
    keys = {
        food_id: ranking.reference_key(names, typed(text), language, [table], country, food_id)
        for food_id, (table, names) in foods.items()
    }
    return sorted(keys, key=keys.get)


def test_names_are_folded_and_cut_at_the_first_comma_or_parenthesis() -> None:
    """Lower case, no accent, no punctuation; the first part names the food."""
    # The ligature is written out: the tables write "Oeuf", people write both.
    assert ranking.fold("  Pâtes SÈCHES, aux œufs (crues) ") == "pates seches aux oeufs crues"
    assert ranking.fold("ŒUF, Bœuf") == ranking.fold("oeuf boeuf") == "oeuf boeuf"
    assert ranking.spellings("oeuf") == ("oeuf", "œuf") and ranking.spellings("lait") == ("lait",)
    assert ranking.fold("Jus d'orange 100%") == "jus d orange 100"
    assert ranking.first_part("Pomme, chair et peau, crue") == "Pomme"
    assert ranking.first_part("Milch (Durchschnitt)") == "Milch "
    assert ranking.first_part("Oeuf cru") == "Oeuf cru"
    assert ranking.first_part(", odd") == ", odd"


def test_a_plural_or_a_feminine_is_the_same_word_and_a_compound_is_not() -> None:
    """The tolerance is an ending, not a stem."""
    assert ranking.same_word("pomme", "pommes") and ranking.same_word("apples", "apple")
    assert ranking.same_word("ei", "eier") and ranking.same_word("crue", "cru")
    assert ranking.same_word("gateau", "gateaux")
    assert not ranking.same_word("apfel", "apfelsaft")
    assert not ranking.same_word("milch", "milchkaffee")
    assert not ranking.same_word("riz", "rizotto")


@pytest.mark.parametrize(
    ("name", "text", "language", "expected"),
    [
        ("Pomme", "pomme", "fr", MATCH_NAME),
        ("Yaourt nature", "yaourt nature", "fr", MATCH_NAME),
        ("Pomme, chair et peau, crue", "pomme", "fr", MATCH_FIRST_PART),
        ("Apples, raw, without skin", "apple", "en", MATCH_FIRST_PART),
        ("Milch (Durchschnitt)", "milch", "de", MATCH_FIRST_PART),
        # A form word does not count in the first part, unless it was typed.
        ("Oeuf cru", "oeuf", "fr", MATCH_FIRST_PART),
        ("Poulet, filet sans peau cru", "poulet", "fr", MATCH_FIRST_PART),
        ("Yaourt à la grecque nature", "yaourt nature", "fr", MATCH_EVERY_WORD),
        ("Pomme de terre, bouillie/cuite à l'eau", "pomme", "fr", MATCH_FIRST_WORDS),
        ("Riz blanc, cru", "riz", "fr", MATCH_FIRST_WORDS),
        ("Pain complet ou intégral (à la farine T150)", "pain complet", "fr", MATCH_FIRST_WORDS),
        ("Apfelsaft", "apfel", "de", MATCH_FIRST_LETTERS),
        ("Pain complet au seigle", "pain comp", "fr", MATCH_FIRST_LETTERS),
        ("Compote de pomme", "pomme", "fr", MATCH_EVERY_WORD),
        ("Chicken, broilers or fryers, breast, meat only, raw", "chicken breast", "en", 4),
        ("Pain grillé au blé complet", "pain complet", "fr", MATCH_EVERY_WORD),
        ("Haselnuss-Schokolade-Brotaufstrich", "brot", "de", MATCH_EVERY_BEGINNING),
        ("Jus de pomme, pur jus", "pom", "fr", MATCH_EVERY_BEGINNING),
        ("Abricot, dénoyauté, cru", "pomme", "fr", MATCH_NONE),
        ("Pomme", "", "fr", MATCH_NONE),
        # The ligature and its two letters are one spelling.
        ("Oeuf, poule, entier, frais ou congelé, cru", "œuf", "fr", MATCH_FIRST_PART),
        ("Œufs brouillés", "oeuf", "fr", MATCH_FIRST_WORDS),
        # The usual kind of a food does not count in the first part, unless it was typed.
        ("Oeuf de poule, entier, cru", "oeuf", "fr", MATCH_FIRST_PART),
        ("Uovo di gallina, intero, crudo", "uovo", "it", MATCH_FIRST_PART),
        ("Oeuf de poule, entier, cru", "oeuf de poule", "fr", MATCH_FIRST_PART),
        ("Oeuf de cane, cru", "oeuf", "fr", MATCH_FIRST_WORDS),
        ("Oeuf de poule, entier, cru", "oeuf de cane", "fr", MATCH_NONE),
        ("Bouillon de poule", "bouillon", "fr", MATCH_FIRST_WORDS),
    ],
)
def test_how_a_name_matches(name: str, text: str, language: str, expected: int) -> None:
    """Each way a name can answer what was typed, from the best to the worst."""
    assert ranking.match_class(name, typed(text), language) == expected


def test_the_cheap_question_agrees_with_the_full_one() -> None:
    """Asking only whether a name is the food typed gives the answer of the full match."""
    names = [
        "Pomme",
        "Pomme, chair et peau, crue",
        "Oeuf cru",
        "Pomme de terre, bouillie/cuite à l'eau",
        "Compote de pomme",
        "Pommes dauphine, surgelées, crues",
        "Jus de pomme, pur jus",
        "Abricot, dénoyauté, cru",
    ]
    for text in ("pomme", "oeuf", "pomme de terre", "jus"):
        for name in names:
            full = ranking.match_class(name, typed(text), "fr")
            cheap = ranking.match(name, typed(text), "fr", MATCH_FIRST_PART)[0]
            assert cheap == (full if full <= MATCH_FIRST_PART else MATCH_NONE), (text, name)
            assert ranking.may_be_the_food_typed({"fr": name}, typed(text)) == (
                full <= MATCH_FIRST_PART
            )


@pytest.mark.parametrize(
    ("name", "language", "expected"),
    [
        ("Pomme, chair et peau, crue", "fr", FORM_RAW),
        ("Apfel, roh", "de", FORM_RAW),
        ("Mela, cruda", "it", FORM_RAW),
        ("Egg, whole, raw, fresh", "en", FORM_RAW),
        ("Yaourt ou lait fermenté, nature", "fr", FORM_RAW),
        ("Lait entier, UHT", "fr", FORM_UNSAID),
        ("Milk, whole", "en", FORM_UNSAID),
        ("Pomme, chair sans peau, bouillie/cuite à l'eau", "fr", FORM_COOKED),
        ("Reis poliert, gekocht in Salzwasser (unjodiert)", "de", FORM_COOKED),
        ("Riso raffinato, bollito in acqua salata", "it", FORM_COOKED),
        ("Pomme, sèche", "fr", FORM_PROCESSED),
        ("Apfel, geschält, getrocknet", "de", FORM_PROCESSED),
        ("Oeuf, en poudre", "fr", FORM_PROCESSED),
        ("Pain complet au seigle, préemballé", "fr", FORM_PROCESSED),
        ("Babyfood, apples, dices, toddler", "en", FORM_PROCESSED),
        # A name that says cooked is cooked, even when it says what it was cooked from.
        ("Chicken breast, fried, coated, skin eaten, from raw", "en", FORM_COOKED),
        ("Pâtes sèches, standard, cuites, sans sel ajouté", "fr", FORM_COOKED),
        # A name that says raw is raw, whatever else it says.
        ("Pâtes sèches, standard, crues", "fr", FORM_RAW),
        # A form word only matches as it is listed: a strawberry is not fresh.
        ("Lait, liquide, fraise", "fr", FORM_UNSAID),
        ("Fraise, crue", "fr", FORM_RAW),
        # "rayon frais" is the chilled aisle, not a fresh food.
        ("Taboulé ou Salade de couscous, préemballé, rayon frais", "fr", FORM_PROCESSED),
        ("Pizza, rayon frais", "fr", FORM_PROCESSED),
        ("Fromage frais", "fr", FORM_RAW),
        # A name in a language without a list takes the words of every language.
        ("Manzana, cruda", "pt", FORM_RAW),
    ],
)
def test_how_plain_a_name_says_the_food_is(name: str, language: str, expected: int) -> None:
    """Raw and fresh, nothing said, cooked, and dried or packed, from short lists of words."""
    assert ranking.form_level(name, language) == expected


def test_the_lists_of_form_words_are_small_and_folded() -> None:
    """Five languages, three levels each, words written as they are compared."""
    assert set(ranking.FORM_WORDS) == {"fr", "en", "de", "it", "es"}
    for table in ranking.FORM_WORDS.values():
        assert set(table) == {FORM_RAW, FORM_COOKED, FORM_PROCESSED}
        for entries in table.values():
            # A dozen words at most, written with the agreements of their language.
            assert len(entries) <= 48 and len(set(entries)) == len(entries)
            for entry in entries:
                assert entry == ranking.fold(entry), entry


APPLES = {
    "ciqual:13039": ("FR", {"fr": "Pomme, chair et peau, crue", "en": "Apple, pulp and skin, raw"}),
    "ciqual:13176": ("FR", {"fr": "Pomme, chair sans peau, bouillie/cuite à l'eau"}),
    "ciqual:13111": ("FR", {"fr": "Pomme, sèche"}),
    "ciqual:13621": ("FR", {"fr": "Pomme Golden, chair sans peau, crue"}),
    "ciqual:4003": ("FR", {"fr": "Pomme de terre, bouillie/cuite à l'eau"}),
    "ciqual:4048": ("FR", {"fr": "Pomme de terre, sans peau, crue"}),
    "ciqual:13038": ("FR", {"fr": "Compote de pomme"}),
    "ciqual:2069": ("FR", {"fr": "Jus de pomme, pur jus"}),
    "cnf:1485": ("CA", {"fr": "Pomme, crue, avec pelure", "en": "Apple, raw, with skin"}),
    "swiss:378": ("CH", {"fr": "Pomme, crue", "de": "Apfel, roh", "en": "Apple, raw"}),
    "usda:fndds:63101000": ("US", {"en": "Apple, raw"}),
}


def test_the_raw_apple_comes_first_and_no_potato_or_sauce_before_an_apple() -> None:
    """The case that asked for the rules: "pomme" in French, in France."""
    found = ordered(APPLES, "pomme", "fr", "FR")
    assert found == [
        "ciqual:13039",  # the first part is the text; raw
        "ciqual:13176",  # the same, cooked
        "ciqual:13111",  # the same, dried
        "ciqual:13621",  # a variety: the first part begins with the text, two words
        "ciqual:4048",  # the potato: three words, raw
        "ciqual:4003",  # the potato, cooked
        "swiss:378",  # the apple of another table
        "cnf:1485",
        "ciqual:13038",  # the names that merely hold the word, whatever their country
        "ciqual:2069",
        "usda:fndds:63101000",  # no French name: it does not match "pomme" at all
    ]
    # The same search from Switzerland puts the Swiss table first; the raw apples of
    # the other tables follow, the shortest name first.
    assert ordered(APPLES, "pomme", "fr", "CH")[:3] == ["swiss:378", "cnf:1485", "ciqual:13039"]
    # Without a country the match decides alone: every raw apple before anything else.
    assert ordered(APPLES, "pomme", "fr", None)[:3] == ["swiss:378", "cnf:1485", "ciqual:13039"]


def test_the_language_of_the_request_comes_before_everything_else() -> None:
    """A name in the language asked for beats a better match in another language."""
    found = ordered(APPLES, "apple", "en", "FR")
    assert found[0] == "ciqual:13039"  # the French table has an English name: country first
    assert set(found[:4]) == {"ciqual:13039", "cnf:1485", "swiss:378", "usda:fndds:63101000"}
    # In German only the Swiss table has a name; the others answer through another
    # language, after it, whatever their country.
    foods = {
        "swiss:568": ("CH", {"de": "Apfelsaft", "fr": "Jus de pomme"}),
        "ciqual:2069": ("FR", {"fr": "Jus de pomme, pur jus"}),
    }
    assert ordered(foods, "jus de pomme", "de", "FR") == ["ciqual:2069", "swiss:568"]
    assert ordered(foods, "apfelsaft", "de", "FR") == ["swiss:568", "ciqual:2069"]
    foods["cnf:1"] = ("CA", {"de": "Jus de pomme (Apfelsaft)", "fr": "Autre chose"})
    assert ordered(foods, "jus de pomme", "de", "FR")[0] == "cnf:1"
    # A language given with its region is the same language.
    assert ordered(foods, "jus de pomme", "de-CH", "FR")[0] == "cnf:1"


def test_the_first_part_with_the_fewest_words_is_the_nearest() -> None:
    """A first part of two words, "Lait entier", is nearer to "lait" than a fermented milk."""
    milks = {
        "ciqual:19039": ("FR", {"fr": "Lait, sans précision sur la teneur en matière grasse, UHT"}),
        "ciqual:19023": ("FR", {"fr": "Lait entier, UHT"}),
        "ciqual:19202": ("FR", {"fr": "Lait de chèvre, entier, cru"}),
        "ciqual:19546": ("FR", {"fr": "Lait fermenté type yaourt au bifidus, nature"}),
        "cnf:61": ("CA", {"fr": "Lait, liquide, chocolat"}),
    }
    assert ordered(milks, "lait", "fr", "FR") == [
        "ciqual:19039",
        "ciqual:19023",
        "ciqual:19202",
        "ciqual:19546",
        "cnf:61",  # the first part is the text, but in the table of another country
    ]
    # Among the names that begin with the text the country goes first; a name that
    # merely holds the words never passes one that begins with them.
    foods = {
        "ciqual:1": ("FR", {"fr": "Riz blanc, cru"}),
        "cnf:1": ("CA", {"fr": "Riz, blanc et sausage, arômatisé"}),
        "ciqual:2": ("FR", {"fr": "Galette de riz soufflé"}),
    }
    assert ordered(foods, "riz", "fr", "FR") == ["ciqual:1", "cnf:1", "ciqual:2"]
    assert ordered(foods, "riz", "fr", "CA") == ["cnf:1", "ciqual:1", "ciqual:2"]


def test_equal_names_are_told_apart_by_the_identifier() -> None:
    """The order never depends on the order the foods were found in."""
    foods = {
        "swiss:381": ("CH", {"fr": "Banane, crue"}),
        "cnf:1704": ("CA", {"fr": "Banane, crue"}),
    }
    assert ordered(foods, "banane", "fr", "FR") == ["cnf:1704", "swiss:381"]
    key = ranking.reference_key({"fr": "Banane, crue"}, typed("banane"), "fr", ["CA"], "FR", "x")
    assert key[ranking.REFERENCE_KEY_MATCH] == MATCH_FIRST_PART
    assert ranking.is_the_food_typed(MATCH_FIRST_PART) and not ranking.is_the_food_typed(2)
    assert ranking.begins_with_the_text(MATCH_FIRST_WORDS)
    assert not ranking.begins_with_the_text(MATCH_FIRST_LETTERS)
    # A food without any name is last, not an error.
    assert ranking.reference_key({}, typed("banane"), "fr", [], "FR", "y")[0] == 2


def ranked(
    foods: dict[str, tuple[str, dict[str, str], str | None]],
    text: str,
    language: str | None,
    country: str | None,
) -> list[str]:
    """Put reference foods and products of chains in the order of a search.

    Args:
        foods: For each identifier, the country, the names and, for a
            product of a chain, the name of the chain (None for a table).
        text: The text typed.
        language: Language of the request.
        country: Country of the request.

    Returns:
        The identifiers, best first.
    """
    keys = {
        food_id: ranking.reference_key(
            names, typed(text), language, [place], country, food_id, chain
        )
        for food_id, (place, names, chain) in foods.items()
    }
    return sorted(keys, key=keys.get)


def matched(name: str, text: str, chain: str | None, language: str = "fr") -> int:
    """Say how the name of a food answers a text, as its sort key records it."""
    key = ranking.reference_key({"fr": name}, typed(text), language, ["FR"], "FR", "x", chain)
    return key[ranking.REFERENCE_KEY_MATCH]


def test_a_product_of_a_chain_is_named_by_its_name_and_after_its_chain() -> None:
    """It reads "McDonald's, Big Mac", as USDA writes "McDONALD'S, BIG MAC"."""
    assert matched("Big Mac", "big mac", "McDonald's") == MATCH_NAME
    # The text names the chain: every product of it is what was typed.
    assert matched("Big Mac", "mcdonald's", "McDonald's") == MATCH_FIRST_PART
    assert matched("WHOPPER®", "Burger King", "Burger King") == MATCH_FIRST_PART
    assert matched("Big Mac", "McDonald's Big Mac", "McDonald's") == MATCH_NAME
    assert matched("Frites (Grande)", "frites", "Five Guys") == MATCH_FIRST_PART
    # The chain and a word of the product: found, but not the product typed.
    for name in ("La petite Portion de Frites", "La Sauce Pommes-Frites"):
        found = matched(name, "mcdonald's frites", "McDonald's")
        assert found == MATCH_EVERY_WORD and not ranking.is_the_food_typed(found)
    # Half a name of chain is not the chain; another chain is nothing.
    assert matched("Big Mac", "mcdo", "McDonald's") == MATCH_FIRST_LETTERS
    assert matched("Big Mac", "burger king", "McDonald's") == MATCH_NONE
    # Without a chain the brand is not read: a crowd entry is ranked as a product.
    assert matched("Big Mac", "mcdonald's", None) == MATCH_NONE
    # A product whose chain is unknown is still one of a chain.
    key = ranking.reference_key({"fr": "Big Mac"}, typed("big mac"), "fr", ["FR"], "FR", "x", "")
    assert key[ranking.REFERENCE_KEY_MATCH] == MATCH_NAME and key[3] == 1


BURGERS = {
    "swiss:14024": ("CH", {"fr": "Cheeseburger", "de": "Cheeseburger"}, None),
    "ciqual:25414": ("FR", {"fr": "Cheeseburger, de restauration rapide"}, None),
    "usda:fndds:27510172": ("US", {"en": "Cheeseburger (McDonalds)"}, None),
    "usda:fndds:27510389": ("US", {"en": "Big Mac (McDonalds)"}, None),
    "chain:mcdonald-s:ch:200024": ("CH", {"fr": "Cheeseburger"}, "McDonald's"),
    "chain:mcdonald-s:fr:13160": ("FR", {"fr": "Cheeseburger"}, "McDonald's"),
    "chain:mcdonald-s:ch:200008": ("CH", {"fr": "Big Mac"}, "McDonald's"),
    "chain:mcdonald-s:fr:13111": ("FR", {"fr": "Big Mac"}, "McDonald's"),
    "chain:mcdonald-s:de:200020": ("DE", {"de": "Big Mac"}, "McDonald's"),
}


def first(
    foods: dict[str, tuple[str, dict[str, str], str | None]],
    text: str,
    language: str | None,
    country: str | None,
) -> list[str]:
    """Give the foods that come before the products in a search of everything, in order."""
    keys = {
        food_id: ranking.reference_key(
            names, typed(text), language, [place], country, food_id, chain
        )
        for food_id, (place, names, chain) in foods.items()
    }
    return [food_id for food_id in sorted(keys, key=keys.get) if ranking.comes_first(keys[food_id])]


def test_the_table_of_the_country_then_its_chains_then_the_other_tables() -> None:
    """A generic word keeps its reference food first; a product of a chain has none before it."""
    assert first(BURGERS, "cheeseburger", "fr", "CH") == [
        "swiss:14024",  # the table of the country
        "chain:mcdonald-s:ch:200024",  # then what the chains publish for that country
        "ciqual:25414",  # the table of another country
        "usda:fndds:27510172",  # no French name
    ]
    assert first(BURGERS, "cheeseburger", "fr", "FR") == [
        "ciqual:25414",
        "chain:mcdonald-s:fr:13160",
        "swiss:14024",
        "usda:fndds:27510172",
    ]
    # No table has a "Big Mac" in French: the chain of the country comes first, before
    # the record USDA has of it in English. What the chain publishes for other
    # countries is not the product typed: it is ordered with the products.
    assert first(BURGERS, "big mac", "fr", "FR") == [
        "chain:mcdonald-s:fr:13111",
        "usda:fndds:27510389",
    ]
    # In the country of the request the language of a menu does not count: in German
    # in Switzerland, the Swiss figures, whose file is in French.
    assert first(BURGERS, "big mac", "de", "CH") == [
        "chain:mcdonald-s:ch:200008",
        "usda:fndds:27510389",
    ]
    # Where no file of a chain is, and without a country, the table alone.
    assert first(BURGERS, "big mac", "en", "US") == ["usda:fndds:27510389"]
    assert first(BURGERS, "big mac", "fr", None) == ["usda:fndds:27510389"]
    # The chain typed: its products of the country, whatever their name.
    assert set(first(BURGERS, "mcdonald's", "fr", "FR")) == {
        "chain:mcdonald-s:fr:13111",
        "chain:mcdonald-s:fr:13160",
    }
    # Among the products, the menu of another country answers in its language, like
    # any name: its key is that of a name in another language.
    tea = ranking.reference_key({"fr": "Thé"}, typed("the"), "en", ["CH"], "US", "x", "McDonald's")
    assert tea[0] == 1 and not ranking.comes_first(tea)
    assert ranking.reference_key({"fr": "Thé"}, typed("the"), "en", ["CH"], "CH", "x", "M")[0] == 0


def test_a_reference_food_that_begins_with_the_text_only_stands_in_when_asked() -> None:
    """For "frites", "Frites de pommes de terre", in the language of the request."""
    fries = {"fr": "Frites de pommes de terre, surgelées, cuites en friteuse"}
    key = ranking.reference_key(fries, typed("frites"), "fr", ["FR"], "FR", "ciqual:4032")
    assert key[ranking.REFERENCE_KEY_MATCH] == MATCH_FIRST_WORDS
    assert not ranking.comes_first(key) and ranking.comes_first(key, stand_in=True)
    other = ranking.reference_key(fries, typed("frites"), "de", ["FR"], "FR", "ciqual:4032")
    assert not ranking.comes_first(other, stand_in=True)
    # A product of a chain never stands in: it is the product typed or it is not.
    chain = ranking.reference_key(
        {"fr": "Frites de patate douce"}, typed("frites"), "fr", ["FR"], "FR", "x", "Quick"
    )
    assert not ranking.comes_first(chain, stand_in=True)


@pytest.mark.parametrize(
    ("name", "language", "expected"),
    [
        # The Canadian file and CoFID name the hen like the duck: a part of its own.
        ("Egg, chicken, whole, fresh or frozen, raw", "en", True),
        ("Eggs, chicken, whole, raw", "en", True),
        ("Oeuf, poule, entier, frais ou congelé, cru", "fr", True),
        ("Egg, duck, whole, fresh, raw", "en", False),
        ("Oeuf, canard, entier, frais, cru", "fr", False),
        # USDA and Ciqual do not name the usual kind.
        ("Egg, whole, raw, fresh", "en", False),
        ("Oeuf cru", "fr", False),
        # The Swiss table names it in the first part.
        ("Oeuf de poule, entier, cru", "fr", True),
        ("Œuf de poule, entier, cuit dur", "fr", True),
        ("Uovo di gallina, intero, crudo", "it", True),
        ("Oeuf de cane, cru", "fr", False),
        ("Oeufs au lait, rayon frais", "fr", False),
        # The cow, the day a table names it; and the other animals.
        ("Milk, cow, whole", "en", True),
        ("Lait de vache, entier, cru", "fr", True),
        ("Milch von der Kuh", "de", True),
        ("Ei, Huhn, roh", "de", True),
        ("Latte di vacca, intero", "it", True),
        ("Uova, gallina, intere", "it", True),
        ("Huevo de gallina, crudo", "es", True),
        ("Leche de vaca, entera", "es", True),
        ("Milk, fluid, sheep, whole", "en", False),
        ("Lait de chèvre, entier, cru", "fr", False),
        ("Latte di capra", "it", False),
        ("Leche de cabra", "es", False),
        # A kind is only usual for its food, and only read in a name that begins with it.
        ("Egg roll, chicken, refrigerated, heated", "en", False),
        ("Soup, chicken, canned", "en", False),
        ("Turkey, hen, breast, meat only, roasted", "en", False),
        ("Camembert cheese, from cow's milk", "en", False),
        ("Fromage type feta, au lait de vache 100%", "fr", False),
        # German writes one word, which is not read.
        ("Hühnerei, ganz, roh", "de", False),
        # A name in a language without a list takes the foods of every language.
        ("Huevo de gallina, crudo", "pt", True),
    ],
)
def test_whether_a_name_says_the_usual_kind(name: str, language: str, expected: bool) -> None:
    """The hen for the egg and the cow for the milk, as the tables write them."""
    assert ranking.says_the_usual_kind(name, language) is expected


def test_the_list_of_usual_kinds_is_small_and_folded() -> None:
    """Two foods in five languages, written as they are compared."""
    assert set(ranking.USUAL_KINDS) == set(ranking.FORM_WORDS)
    for pairs in ranking.USUAL_KINDS.values():
        assert len(pairs) == 2
        for foods, kinds in pairs:
            assert 1 <= len(foods) <= 2 and 1 <= len(kinds) <= 2
            for word in (*foods, *kinds):
                assert word == ranking.fold(word) and " " not in word, word


EGGS = {
    "cnf:88": (
        "CA",
        {"en": "Egg, duck, whole, fresh, raw", "fr": "Oeuf, canard, entier, frais, cru"},
    ),
    "cnf:89": (
        "CA",
        {"en": "Egg, goose, whole, fresh, raw", "fr": "Oeuf, oie, entier, frais, cru"},
    ),
    "cnf:125": (
        "CA",
        {
            "en": "Egg, chicken, whole, fresh or frozen, raw",
            "fr": "Oeuf, poule, entier, frais ou congelé, cru",
        },
    ),
    "cnf:129": (
        "CA",
        {"en": "Egg, chicken, whole, cooked, fried", "fr": "Oeuf, poule, entier, cuit, frit"},
    ),
    "usda:sr_legacy:1123": ("US", {"en": "Egg, whole, raw, fresh"}),
    "usda:sr_legacy:1138": ("US", {"en": "Egg, duck, whole, fresh, raw"}),
    "ciqual:22000": ("FR", {"fr": "Oeuf cru", "en": "Egg, raw"}),
    "ciqual:22060": ("FR", {"fr": "Oeuf de cane, cru"}),
    "swiss:290": (
        "CH",
        {
            "fr": "Oeuf de poule, entier, cru",
            "it": "Uovo di gallina, intero, crudo",
            "de": "Hühnerei, ganz, roh",
            "en": "Egg, raw",
        },
    ),
}


def test_the_egg_of_the_hen_comes_before_those_of_the_duck_and_the_goose() -> None:
    """The case that asked for the list: "egg" and "œuf" in Canada."""
    # The names of the duck and the goose are the shorter ones: they were first.
    hen_first = ["cnf:125", "cnf:129", "cnf:88", "cnf:89"]
    assert ordered(EGGS, "egg", "en", "CA")[:4] == hen_first
    assert ordered(EGGS, "oeuf", "fr", "CA")[:4] == hen_first
    assert ordered(EGGS, "œuf", "fr", "CA") == ordered(EGGS, "oeuf", "fr", "CA")
    assert ordered(EGGS, "Œufs", "fr", "CA")[:4] == hen_first
    # Where the table does not name the usual kind, its name is the shortest: as before.
    assert ordered(EGGS, "egg", "en", "US")[:2] == ["usda:sr_legacy:1123", "usda:sr_legacy:1138"]
    assert ordered(EGGS, "oeuf", "fr", "FR")[:2] == ["ciqual:22000", "ciqual:22060"]
    # The Swiss table writes "Oeuf de poule": it is the egg typed, in Switzerland first.
    assert ordered(EGGS, "oeuf", "fr", "CH")[0] == "swiss:290"
    assert ordered(EGGS, "uovo", "it", "CH")[0] == "swiss:290"
    assert ordered(EGGS, "oeuf", "fr", "FR").index("swiss:290") == 2  # after the French table
    # Typed, the other kind is the food asked for.
    assert ordered(EGGS, "oeuf de cane", "fr", "CA")[0] == "ciqual:22060"
    assert ordered(EGGS, "egg duck", "en", "CA")[0] == "cnf:88"
