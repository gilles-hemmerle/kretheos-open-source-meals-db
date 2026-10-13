"""How well a name answers what was typed, and how plain the food it names is.

The full-text index finds the foods; this module puts the reference foods
(the generic foods of the food-composition tables) in an order a person can
predict. Nothing here knows a food: there is no list of foods, only rules on
the words of a name, and three short lists of words the tables use. The one
exception is the usual kind of two foods, the egg and the milk
(`USUAL_KINDS`), told below.

A reference food (and a product of a chain, see below) is ranked by, in
this order:

1. **language**: a name in the language of the request that matches what was
   typed beats a name that only matches in another language;
2. **the name is, or begins with, the words typed**, before the names that
   only hold them somewhere (`begins_with_the_text`);
3. the **country** of the request: the table of the person's country first,
   then the menus of the chains in that country, then the other tables;
4. **how the name matches** (`match_class`), from the best to the worst:
   the name is the text typed; its first part, before the first comma or
   parenthesis, is the text typed ("Pomme, chair et peau, crue" for
   "pomme"); the first part begins with the words typed ("Pomme de terre",
   "Riz blanc, cru"); the first part begins with the text, inside a longer
   word ("Apfelsaft" for "apfel"); every word typed is a word of the name,
   in any order ("Chicken, broilers or fryers, breast, meat only, raw" for
   "chicken breast"); every word typed begins a word of the name; the rest;
5. for a name that begins with the text, **the fewest words in its first
   part**: "Lait entier, UHT" is nearer to "lait" than "Lait fermenté à
   boire, nature", "Riso raffinato" nearer to "riso" than "Riso al latte";
6. **the usual kind**, where a table names it: the eggs of the hen, raw
   then cooked, before those of the duck and the goose
   (`says_the_usual_kind`);
7. **how plain the food is** (`form_level`): raw, fresh and unflavoured
   forms, then names that say nothing, then cooked forms, and last dried,
   powdered, canned and pre-packed foods and baby food;
8. the shortest name, then the identifier.

Why the country comes where it does. The tables do not name foods the same
way: the Canadian file writes "Riz, blanc et sausage, arômatisé", "Lait,
liquide, chocolat" where Ciqual writes "Riz blanc, cru", "Lait entier, UHT".
Ranked on the match alone, a French person typing "riz" or "lait" got the
flavoured mixes of Canada before the rice and the milk of the French table.
So among the names that are or begin with the text, the table of the country
goes first; a name that merely holds the words never passes one that begins
with them, whatever its country.

Details, each from what the tables really write:

* two words are "the same word" when one is the other with a plural or
  feminine ending (`ENDINGS`): "pommes" is "pomme", "Eier" is "Ei". This is
  not stemming, and it is deliberately small: it lets "Apples, raw" be the
  apple and "Apfelsaft" not be one;
* a form word does not count in the first part, unless it was typed: "Oeuf
  cru" is "Oeuf", like "Oeuf, cru", but "Yaourt nature" stays what it is for
  someone who types "yaourt nature";
* an expression counts as a whole: "rayon frais", which Ciqual writes for
  the dishes sold chilled, is pre-packed food, not fresh food;
* a name that says cooked is cooked, even when it also says what it was
  cooked from ("Chicken breast, fried, from raw", "Pâtes sèches, cuites");
  else a name that says raw is raw, whatever else it says ("Pâtes sèches
  standard, crues"); dried, canned and the like only count when the name
  says neither;
* French and Italian have one word for dried fruit and for dry rice, pasta
  and pulses ("sec", "secco"), where German and English have two
  ("getrocknet" and "trocken", "dried" and "dry"). The lists cannot tell them
  apart: "Riz blanc, sec" and "Riso raffinato, secco" rank with the dried
  foods, after cooked rice.

The usual kind (`USUAL_KINDS`). Asked for an egg, a person means that of
the hen. The tables say so in two ways. USDA and Ciqual do not name the
usual kind: "Egg, whole, raw, fresh", "Oeuf cru", beside "Egg, duck, whole,
fresh, raw" and "Oeuf de cane, cru"; there the shortest name is the usual
kind, and nothing was wrong. The Canadian file and CoFID name it like the
others: "Egg, chicken, whole, fresh or frozen, raw" beside "Egg, duck,
whole, fresh, raw", "Eggs, chicken, whole, raw" beside "Eggs, quail, whole,
raw"; all are raw eggs, the shortest name decided, and the duck, the goose,
the quail and the turkey came before the hen. No rule on the words alone
tells "chicken" from "duck": a kind is only usual for a food ("Soup,
chicken" is not the usual soup), and a count of how many names of a table
share a kind puts the white and the yolk of Ciqual before its "Oeuf cru".
So the two foods whose usual animal some tables name are listed, with that
animal, in five languages:

* a name that says the usual kind of its food as a part of its own ("Egg,
  chicken, whole", "Oeuf, poule, entier") comes before the names that do
  not, whatever their form: the fried and the boiled egg of the hen before
  the raw egg of the duck. A table names the usual kind for all its eggs or
  for none, and the country goes first, so the unnamed "Egg, whole, raw" of
  USDA only meets the named ones among the tables of other countries;
* said in the first part with the word that links it, the usual kind does
  not count there, unless it was typed: the "Oeuf de poule, entier, cru" and
  "Uovo di gallina, intero, crudo" of the Swiss table are "Oeuf" and "Uovo";
* it only reads names that begin with the food: "Egg roll, chicken" is not
  an egg, "Turkey, hen, breast" not a hen's;
* German writes one word, "Hühnerei", "Kuhmilch", which the index only
  finds from its beginning: the German words of the list are for names
  written in several words;
* nothing tells the usual milk where a table does not name the cow: the
  Canadian file has "Milk, fluid, whole, pasteurized, homogenized, 3.25%
  M.F." beside "Milk, fluid, chocolate, whole" and "Milk, fluid, sheep,
  whole", and the shortest still comes first.

The ligature of "œuf", "bœuf", "cœur" is written out, "oe", in every name
and in what is typed: the tables write "Oeuf", people write both. The index
keeps it, so a search asks it for both spellings (`spellings`).

The products of a restaurant chain (`reference_key` with `chain`). What a
chain publishes for a country are the figures of the publisher, as those of
a table are: its products are ranked by their name with the same rules,
not by how often a crowd scanned them. What is theirs:

* a product is named as the tables name such dishes, "McDONALD'S, BIG MAC"
  in USDA: by its name, and by its name after that of its chain. "big mac"
  is the name of the product; "mcdonald's" is the first part of
  "McDonald's, Big Mac", so the text names the chain and every product of
  it answers; "mcdonald's big mac" is the whole of it;
* only in the country of the request (`comes_first`): what McDonald's
  publishes for Switzerland is the Big Mac of a person in Switzerland, and
  one product of Spain among others for her, ordered as products are;
* there the language of its name does not count: the file of a country is
  in one of its languages (the Swiss files are in French), and a person who
  types "Big Mac" in German in Switzerland gets the Swiss figures;
* the reference food of the country goes first: "cheeseburger" is the
  cheeseburger of the Swiss table, then those of the chains in Switzerland,
  then the tables of the other countries. A text no table of the country
  has a food for ("big mac") begins with the chain.

The lists of form words (`FORM_WORDS`) are those the tables use (Ciqual, the
Swiss table, the Canadian file, USDA, CoFID) in French, English, German and
Italian; the Spanish ones are there for the day a table has Spanish names.
Words are written folded: lower case, without accents. A form word only
matches as it is listed, agreements included: "fraise" is not "frais".
"""

from __future__ import annotations

import functools
import re
import sys
import unicodedata
from collections.abc import Iterable, Mapping, Sequence

# Endings that make two words the same word: plurals and feminines of
# French, English, German and Spanish.
ENDINGS = ("s", "es", "x", "n", "en", "e", "er")

MATCH_NAME = 0
MATCH_FIRST_PART = 1
MATCH_FIRST_WORDS = 2
MATCH_FIRST_LETTERS = 3
MATCH_EVERY_WORD = 4
MATCH_EVERY_BEGINNING = 5
MATCH_NONE = 6

FORM_RAW = 0
FORM_UNSAID = 1
FORM_COOKED = 2
FORM_PROCESSED = 3

_FRENCH = ("", "e", "s", "es")
_GERMAN = ("", "e", "er", "es", "en", "em")
_ITALIAN = ("o", "a", "i", "e")
_SPANISH = ("o", "a", "os", "as")


def _agree(stems: tuple[str, ...], endings: tuple[str, ...]) -> tuple[str, ...]:
    """Write words with every ending their language gives them.

    Args:
        stems: Words, or stems of words, folded.
        endings: The endings of the language.

    Returns:
        Every stem with every ending.
    """
    return tuple(stem + ending for stem in stems for ending in endings)


# Language -> level -> words and expressions, folded, as they are matched: a
# word of a name must be one of them, an expression of several words must be
# found as it is.
FORM_WORDS: dict[str, dict[int, tuple[str, ...]]] = {
    "fr": {
        FORM_RAW: (*_agree(("cru",), _FRENCH), "frais", "fraiche", "fraiches", "nature"),
        FORM_COOKED: _agree(
            ("cuit", "bouilli", "roti", "grille", "frit", "poele", "braise", "saute"), _FRENCH
        ),
        FORM_PROCESSED: (
            *_agree(("sec", "seche", "deshydrate", "appertise", "preemballe"), _FRENCH),
            "poudre",
            "en conserve",
            # Ciqual says "rayon frais" of the dishes sold chilled, as it
            # says "préemballé" of the others: the aisle, not a fresh food.
            "rayon frais",
            "bebe",
            "bebes",
        ),
    },
    "en": {
        FORM_RAW: ("raw", "fresh", "plain"),
        FORM_COOKED: (
            "cooked",
            "boiled",
            "roasted",
            "grilled",
            "fried",
            "baked",
            "broiled",
            "braised",
            "steamed",
            "stewed",
        ),
        FORM_PROCESSED: (
            "dried",
            "dehydrated",
            "powder",
            "powdered",
            "canned",
            "prepacked",
            "baby",
            "babyfood",
            "infant",
        ),
    },
    "de": {
        FORM_RAW: (*_agree(("roh", "frisch"), _GERMAN), "natur", "nature"),
        FORM_COOKED: _agree(
            ("gekocht", "gebraten", "gegrillt", "gebacken", "gedampft", "gedunstet", "frittiert"),
            _GERMAN,
        ),
        FORM_PROCESSED: (*_agree(("getrocknet",), _GERMAN), "pulver", "konserve", "konserven"),
    },
    "it": {
        FORM_RAW: (*_agree(("crud",), _ITALIAN), "fresco", "fresca", "freschi", "fresche"),
        FORM_COOKED: (
            *_agree(("cott", "bollit", "fritt", "grigliat"), _ITALIAN),
            "arrosto",
        ),
        FORM_PROCESSED: (
            *_agree(("essiccat",), _ITALIAN),
            "secco",
            "secca",
            "secchi",
            "secche",
            "in polvere",
            "in scatola",
        ),
    },
    "es": {
        FORM_RAW: (*_agree(("crud", "fresc"), _SPANISH), "natural"),
        FORM_COOKED: _agree(("cocid", "hervid", "asad", "frit"), _SPANISH),
        FORM_PROCESSED: (
            *_agree(("sec", "deshidratad"), _SPANISH),
            "en polvo",
            "en conserva",
        ),
    },
}

# Language -> the foods whose usual kind some tables name, each with the
# words that say it, folded: the egg and the hen, the milk and the cow. A
# plural is the same word (`same_word`); the Italian plural of the egg is not
# made with an ending and is listed.
USUAL_KINDS: dict[str, tuple[tuple[tuple[str, ...], tuple[str, ...]], ...]] = {
    "fr": ((("oeuf",), ("poule",)), (("lait",), ("vache",))),
    "en": ((("egg",), ("chicken", "hen")), (("milk",), ("cow",))),
    "de": ((("ei",), ("huhn", "henne")), (("milch",), ("kuh",))),
    "it": ((("uovo", "uova"), ("gallina",)), (("latte",), ("vacca", "mucca"))),
    "es": ((("huevo",), ("gallina",)), (("leche",), ("vaca",))),
}
# Words that link a food to its kind in a first part: "Oeuf de poule", "Uovo
# di gallina", "Milch von der Kuh".
KIND_LINKS = ("de", "di", "d", "of", "von", "vom", "der")

# The ligature of "œuf", and how every name and every text typed writes it.
LIGATURE = "œ"
LIGATURE_WRITTEN_OUT = "oe"

_NOT_A_WORD = re.compile(r"[\W_]+")
_FIRST_PART_END = re.compile(r"[,(]")


@functools.lru_cache(maxsize=1)
def _combining_marks() -> dict[int, None]:
    """List the combining marks of Unicode, for `str.translate` to drop them.

    Returns:
        A translation table deleting every combining mark.
    """
    return dict.fromkeys(
        code for code in range(sys.maxunicode + 1) if unicodedata.combining(chr(code))
    )


@functools.lru_cache(maxsize=131_072)
def fold(text: str) -> str:
    """Write a text the way names and what is typed are compared.

    The names of the foods found are folded for every search and the same
    ones come back often: the last ones are remembered.

    Args:
        text: Any text.

    Returns:
        The words of the text in lower case, without accents, separated by
        single spaces; punctuation is dropped, and the ligature of "œuf" is
        written "oe". The index compares the same way but for the ligature,
        which it keeps: see `spellings`.
    """
    plain = unicodedata.normalize("NFKD", text.casefold()).translate(_combining_marks())
    return _NOT_A_WORD.sub(" ", plain.replace(LIGATURE, LIGATURE_WRITTEN_OUT)).strip()


def spellings(word: str) -> tuple[str, ...]:
    """Give the ways the index may hold a folded word.

    Args:
        word: A folded word.

    Returns:
        The word, and the word with the ligature when it has an "oe": the
        index holds "oeuf" for the "Oeuf" of the tables and "œuf" for the
        "Œufs" of Open Food Facts, and a person means both.
    """
    if LIGATURE_WRITTEN_OUT in word:
        return (word, word.replace(LIGATURE_WRITTEN_OUT, LIGATURE))
    return (word,)


def first_part(name: str) -> str:
    """Return the first part of a name: what comes before a comma or a parenthesis.

    Args:
        name: A name as a table writes it, e.g. "Pomme, chair et peau, crue".

    Returns:
        The first part, e.g. "Pomme"; the whole name when it has no comma
        and no parenthesis, or begins with one.
    """
    head = _FIRST_PART_END.split(name, maxsplit=1)[0]
    return head if head.strip() else name


def same_word(word: str, other: str) -> bool:
    """Tell whether two folded words are one word, give or take an ending.

    Args:
        word: A folded word.
        other: Another folded word.

    Returns:
        True when they are equal, or when the longer is the shorter with
        one of `ENDINGS`.
    """
    if word == other:
        return True
    short, long_ = (word, other) if len(word) < len(other) else (other, word)
    return long_.startswith(short) and long_[len(short) :] in ENDINGS


def _same_words(words: Sequence[str], typed: Sequence[str]) -> bool:
    """Tell whether two lists of words are the same words in the same order.

    Args:
        words: Words of a name.
        typed: Words typed.

    Returns:
        True when both have as many words and each pair is the same word.
    """
    return len(words) == len(typed) and all(map(same_word, words, typed))


# The names of the reference foods are 40 000: all of them are remembered.
@functools.lru_cache(maxsize=65_536)
def _shape(
    name: str, language: str | None
) -> tuple[tuple[str, ...], frozenset[str], tuple[str, ...], frozenset[str]]:
    """Cut a name into what `match_class` compares, once for many searches.

    Args:
        name: A name of a food, as its table writes it.
        language: Language code of the name, for its form words.

    Returns:
        The folded words of the name; the same as a set; the words of its
        first part; and those of the first part that do not count unless
        they were typed: the form words, and the usual kind of the food with
        the word that links it ("de poule" in "Oeuf de poule").
    """
    words = tuple(fold(name).split())
    head = tuple(fold(first_part(name)).split())
    single, _ = _form_words(language)
    silent = {word for word in head if word in single}
    silent.update(_usual_kind_in(head, language)[1])
    return words, frozenset(words), head, frozenset(silent)


def match(
    name: str, typed: Sequence[str], language: str | None = None, deepest: int = MATCH_NONE
) -> tuple[int, int]:
    """Say how well a name answers the words typed.

    Args:
        name: A name of a food, as its table writes it.
        typed: The folded words typed.
        language: Language code of the name, for its form words.
        deepest: The last `MATCH_...` value the caller tells apart: a name
            that matches worse is not looked at further and gets
            `MATCH_NONE`. A search of everything only asks whether a name is
            the food typed, for thousands of names.

    Returns:
        One of the `MATCH_...` values, the lowest being the best; and, for a
        name that is or begins with the text, the number of words of its
        first part (its form words left out), else 0.
    """
    if not typed:
        return MATCH_NONE, 0
    words, word_set, head, silent = _shape(name, language)
    count = len(typed)
    if len(words) == count and _same_words(words, typed):
        return MATCH_NAME, count
    if silent:
        # A form word does not count in the first part, unless it was typed;
        # nor does the usual kind of the food.
        kept = tuple(
            word
            for word in head
            if word not in silent or any(same_word(word, wanted) for wanted in typed)
        )
        head = kept or head
    if len(head) == count and _same_words(head, typed):
        return MATCH_FIRST_PART, count
    if deepest <= MATCH_FIRST_PART:
        return MATCH_NONE, 0
    if len(head) >= count and _same_words(head[: count - 1], typed[:-1]):
        if len(head) > count and same_word(head[count - 1], typed[-1]):
            return MATCH_FIRST_WORDS, len(head)
        if head[count - 1].startswith(typed[-1]):
            return MATCH_FIRST_LETTERS, len(head)
    for wanted in typed:
        if wanted not in word_set and not any(same_word(word, wanted) for word in words):
            break
    else:
        return MATCH_EVERY_WORD, 0
    for wanted in typed:
        if wanted not in word_set and not any(word.startswith(wanted) for word in words):
            return MATCH_NONE, 0
    return MATCH_EVERY_BEGINNING, 0


def match_class(name: str, typed: Sequence[str], language: str | None = None) -> int:
    """Say how well a name answers the words typed.

    Args:
        name: A name of a food, as its table writes it.
        typed: The folded words typed.
        language: Language code of the name, for its form words.

    Returns:
        One of the `MATCH_...` values, the lowest being the best.
    """
    return match(name, typed, language)[0]


def may_be_the_food_typed(names: Mapping[str, str], typed: Sequence[str]) -> bool:
    """Tell, at little cost, whether a food can be the food typed.

    Args:
        names: The names of the food, by language code.
        typed: The folded words typed.

    Returns:
        True when one of its names, or the first part of one, is the text
        typed. `reference_key` then says whether that name is the one the
        food is ranked by.
    """
    return any(
        match(name, typed, lang, MATCH_FIRST_PART)[0] != MATCH_NONE for lang, name in names.items()
    )


@functools.lru_cache(maxsize=16)
def _form_words(
    language: str | None,
) -> tuple[dict[str, int], tuple[tuple[int, tuple[str, ...]], ...]]:
    """Give the form words to look for in a name of a language.

    Args:
        language: Language code of the name; any other code takes the words
            of every language.

    Returns:
        The level of each single word, and the expressions of several words
        with their level.
    """
    base = (language or "").split("-")[0]
    tables = [FORM_WORDS[base]] if base in FORM_WORDS else list(FORM_WORDS.values())
    single: dict[str, int] = {}
    several = set()
    for table in tables:
        for level, entries in table.items():
            for entry in entries:
                if " " in entry:
                    several.add((level, tuple(entry.split())))
                else:
                    single.setdefault(entry, level)
    return single, tuple(sorted(several))


@functools.lru_cache(maxsize=65_536)
def form_level(name: str, language: str | None = None) -> int:
    """Say how plain the food a name describes is.

    Args:
        name: A name of a food, as its table writes it.
        language: Language code of the name.

    Returns:
        `FORM_COOKED` when the name says cooked; else `FORM_RAW` when it
        says raw or fresh; else `FORM_PROCESSED` when it says dried,
        powdered, canned, pre-packed or for babies; else `FORM_UNSAID`.
    """
    words = fold(name).split()
    single, several = _form_words(language)
    found = set()
    for level, entry in several:
        size = len(entry)
        for start in range(len(words) - size + 1):
            if tuple(words[start : start + size]) == entry:
                found.add(level)
                # The words of an expression do not count again on their own.
                words[start : start + size] = [""] * size
    found.update(single[word] for word in words if word in single)
    for level in (FORM_COOKED, FORM_RAW, FORM_PROCESSED):
        if level in found:
            return level
    return FORM_UNSAID


@functools.lru_cache(maxsize=16)
def _usual_kinds(language: str | None) -> tuple[tuple[tuple[str, ...], tuple[str, ...]], ...]:
    """Give the foods whose usual kind is listed, for a name of a language.

    Args:
        language: Language code of the name; any other code takes the foods
            of every language.

    Returns:
        Pairs of the words naming a food and the words saying its usual kind.
    """
    base = (language or "").split("-")[0]
    if base in USUAL_KINDS:
        return USUAL_KINDS[base]
    return tuple(pair for pairs in USUAL_KINDS.values() for pair in pairs)


def _usual_kind_in(
    head: Sequence[str], language: str | None
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Look for the usual kind of a food in the first part of its name.

    Args:
        head: The folded words of the first part of a name.
        language: Language code of the name.

    Returns:
        The words saying the usual kind of the food the first part begins
        with (none when it begins with no listed food); and the words of the
        first part that say it there, with those linking it: "de" and
        "poule" for "Oeuf de poule", nothing for "Oeuf", for "Oeuf de cane"
        or for "Oeufs au lait".
    """
    kinds: tuple[str, ...] = ()
    for foods, of_the_food in _usual_kinds(language) if head else ():
        if any(same_word(head[0], food) for food in foods):
            kinds = of_the_food
            break
    if not kinds:
        return (), ()
    single, _ = _form_words(language)
    said = [word for word in head[1:] if word not in single]
    if (
        said
        and any(same_word(said[-1], kind) for kind in kinds)
        and all(word in KIND_LINKS for word in said[:-1])
    ):
        return kinds, tuple(said)
    return kinds, ()


@functools.lru_cache(maxsize=65_536)
def says_the_usual_kind(name: str, language: str | None = None) -> bool:
    """Tell whether a name says the usual kind of the food it names.

    Args:
        name: A name of a food, as its table writes it.
        language: Language code of the name.

    Returns:
        True when the name begins with a food of `USUAL_KINDS` and says its
        usual kind as a part of its own ("Egg, chicken, whole, raw") or in
        its first part ("Oeuf de poule, entier, cru"); False for any other
        name, among them those of another food ("Egg roll, chicken") and
        those that name no kind ("Egg, whole, raw").
    """
    parts = [tuple(fold(part).split()) for part in _FIRST_PART_END.split(name)]
    head = tuple(fold(first_part(name)).split())
    kinds, said = _usual_kind_in(head, language)
    if said:
        return True
    single, _ = _form_words(language)
    if not kinds or any(word not in single for word in head[1:]):
        return False
    return any(
        len(part) == 1 and any(same_word(part[0], kind) for kind in kinds) for part in parts[1:]
    )


def begins_with_the_text(matched: int) -> bool:
    """Tell whether a name is, or begins with, the words typed.

    Args:
        matched: A `MATCH_...` value.

    Returns:
        True for a name that is the text, whose first part is the text, or
        whose first part begins with the words typed.
    """
    return matched <= MATCH_FIRST_WORDS


def is_the_food_typed(matched: int) -> bool:
    """Tell whether a name, or its first part, is the text typed.

    Args:
        matched: A `MATCH_...` value.

    Returns:
        True for the two best matches.
    """
    return matched <= MATCH_FIRST_PART


def reference_key(
    names: Mapping[str, str],
    typed: Sequence[str],
    language: str | None,
    countries: Iterable[str],
    country: str | None,
    food_id: str,
    chain: str | None = None,
) -> tuple:
    """Build the sort key of a food ranked by its name among the results of a search.

    Args:
        names: The names of the food, by language code.
        typed: The folded words typed.
        language: Language of the request, if any.
        countries: Countries the values of the food apply to.
        country: Country of the request, if any.
        food_id: Identifier of the food.
        chain: For a product a restaurant chain publishes, the name of the
            chain (empty when unknown); None for a reference food. Each name
            of the product is also read after it, "McDonald's, Big Mac", and
            in the country of the request its language does not count.

    Returns:
        A key sorting as the module describes: language, a name that is or
        begins with the text, country, reference food before product of a
        chain, match, words of the first part, usual kind, form, shortest
        name, identifier. `REFERENCE_KEY_MATCH` is the position of the `MATCH_...`
        value of the best name.
    """
    wanted = (language or "").split("-")[0]
    local = bool(country) and country in countries
    best: tuple | None = None
    for lang, name in names.items():
        for written in (name, f"{chain}, {name}") if chain else (name,):
            matched, head_words = match(written, typed, lang)
            if matched == MATCH_NONE:
                other_language = 2
            elif chain is not None and local:
                other_language = 0
            else:
                other_language = 0 if not wanted or lang == wanted else 1
            candidate = (other_language, matched, lang, name, head_words)
            if best is None or candidate < best:
                best = candidate
    other_language, matched, lang, name, head_words = best or (2, MATCH_NONE, "", "", 0)
    return (
        other_language,
        0 if begins_with_the_text(matched) else 1,
        0 if local else 1,
        0 if chain is None else 1,
        matched,
        head_words,
        0 if says_the_usual_kind(name, lang) else 1,
        form_level(name, lang),
        len(_shape(name, lang)[0]),
        food_id,
    )


REFERENCE_KEY_MATCH = 4
"""Position of the `MATCH_...` value in the key `reference_key` returns."""


def comes_first(key: tuple, stand_in: bool = False) -> bool:
    """Tell whether a food comes before the products in a search of everything.

    Args:
        key: The key `reference_key` gave the food.
        stand_in: Whether no reference food is the text typed. Nobody names
            a food "Frites": Ciqual has "Frites de pommes de terre,
            surgelées, cuites en friteuse", and that is the food a person
            who types "frites" is shown first.

    Returns:
        True for a reference food whose name, or first part, is the text
        typed, and for a product of a chain that is, in the country of the
        request only: what a chain publishes for another country is a
        product of another country. When `stand_in` is set, also for a
        reference food whose first part begins with the words typed, in the
        language of the request.
    """
    other_language, _, other_country, of_a_chain, matched = key[: REFERENCE_KEY_MATCH + 1]
    if of_a_chain:
        return is_the_food_typed(matched) and not other_country
    if is_the_food_typed(matched):
        return True
    return stand_in and begins_with_the_text(matched) and not other_language
