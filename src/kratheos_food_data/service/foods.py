"""Foods as the service answers them: the main database with the live layer on top.

A food is put together from two places, for every read:

* who it is (names, brand, category, kind, barcode) and its versions come
  from `food.sqlite` when the food is there, else from the product added in
  the live database;
* the live database then adds what the main database does not have yet: the
  versions proposed since the last build, the names added, and the votes.
  For a food the live database holds votes of, its counts are the truth and
  those folded into the main database are ignored: a vote is never counted
  twice. A version or a name present in both is kept once;
* what moderation hid is left out: a hidden food does not exist for a read,
  a hidden version is not listed and cannot be served.

The version served is then chosen for the country of the request
(`versions.choose_served`), and the food is written as the JSON record of
the API: the column names of the database are mapped to the field names of
the contract here, and nowhere else.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from .. import kratheos, plausibility, schema, versions
from ..barcode import is_restricted
from ..names import FALLBACK_LANGUAGE
from . import catalogue, live
from .catalogue import Generation

# Field of the API -> column of the database.
NUTRIENT_FIELDS = (
    ("kcal", "energy_kcal"),
    ("protein_g", "protein_g"),
    ("carbs_g", "carbohydrate_g"),
    ("sugars_g", "sugars_g"),
    ("fat_g", "fat_g"),
    ("saturated_fat_g", "saturated_fat_g"),
    ("fibre_g", "fiber_g"),
    ("salt_g", "salt_g"),
    ("alcohol_g", "alcohol_g"),
)
NAME_ORIGIN_SOURCE = "source"
NAME_ORIGIN_KRATHEOS = "kratheos"
KRATHEOS_TITLE = kratheos.KRATHEOS_SOURCE.name
UNITS = {schema.BASIS_100G: "g", schema.BASIS_100ML: "ml"}
VALUE_KEYS = ("basis", *live.NUTRIENTS, "carbohydrate_total_g", *live.SERVING)


@dataclass
class Context:
    """What a read needs: both databases and the settings that matter.

    Attributes:
        generation: The main database being served.
        base: A connection to it.
        live: A read connection to the live database.
        user_version_votes: Votes a version coming from a user needs to be
            served instead of a record of a source.
    """

    generation: Generation
    base: sqlite3.Connection
    live: sqlite3.Connection
    user_version_votes: int


@dataclass
class Food:
    """A food, with every visible version of its values.

    Attributes:
        id: Public identifier.
        kind: `generic` or `product`.
        barcode: Canonical barcode, or None.
        names: Every name, by language code, with its origin.
        name: Main name of the food.
        name_lang: Language of the main name.
        brand: Brand, for products.
        category: Category in the classification of its source.
        is_drink: Whether its source classifies it as a beverage.
        popularity: Popularity figure of its source, 0 when it has none.
        versions: Its visible versions, in the order of the build.
        hidden: Whether moderation hid the food (only seen by moderation).
    """

    id: str
    kind: str
    barcode: str | None
    names: dict[str, tuple[str, str]]
    name: str
    name_lang: str
    brand: str | None
    category: str | None
    is_drink: bool
    popularity: int
    versions: list[dict] = field(default_factory=list)
    hidden: bool = False

    def served(self, country: str | None, user_version_votes: int) -> dict:
        """Choose the version served to a request.

        Args:
            country: Country of the request, if any.
            user_version_votes: The vote threshold of the service.

        Returns:
            The version served.
        """
        return versions.choose_served(self.versions, country, user_version_votes)


def _json_list(text: str | None) -> list:
    """Read a JSON array stored as text.

    Args:
        text: The text, or None.

    Returns:
        The list, empty when there is none.
    """
    return json.loads(text) if text else []


def _base_version(row: Mapping, version_id: str, votes: int) -> dict:
    """Build a version from a row of `foods` or of `versions`.

    Args:
        row: The row, holding the columns of a version.
        version_id: Identifier of the version.
        votes: Votes folded into the main database.

    Returns:
        The version.
    """
    version = {name: row[name] for name in VALUE_KEYS}
    version.update(
        version_id=version_id,
        source=row["source"],
        origin=versions.origin_of(row["source"]),
        countries=_json_list(row["countries"]),
        has_macros=bool(row["has_macros"]),
        plausible=bool(row["plausible"]),
        flags=_json_list(row["flags"]),
        licence=row["licence"],
        source_url=row["source_url"],
        updated_at=row["updated_at"],
        votes=votes,
    )
    return version


def _live_version(row: Mapping, version_id: str, extra_flags: Iterable[str] = ()) -> dict:
    """Build a version from a row of the live database.

    The values are judged by the same checks as the build, so the version
    carries the flags it will have once a build has taken it in.

    Args:
        row: A row of `versions` or of `products` of the live database.
        version_id: Identifier of the version.
        extra_flags: Flags that do not come from the values.

    Returns:
        The version.
    """
    version = {name: row[name] for name in ("basis", *live.NUTRIENTS, *live.SERVING)}
    version["carbohydrate_total_g"] = None
    flags = [*extra_flags, *plausibility.quality_flags(version)]
    version.update(
        version_id=version_id,
        source=kratheos.SOURCE_ID,
        origin=versions.ORIGIN_USER,
        countries=_json_list(row["countries"]),
        has_macros=plausibility.has_macros(version),
        plausible=not any(flag in schema.QUALITY_FLAGS for flag in flags),
        flags=flags,
        licence=kratheos.LICENCE,
        source_url=None,
        updated_at=str(row["updated_at"])[:10],
        votes=0,
    )
    return version


def _product_version(product: Mapping) -> dict:
    """Build the version a product added in Kratheos is.

    Args:
        product: A row of `products` of the live database.

    Returns:
        The version, identified by the record of the product.
    """
    restricted = [schema.FLAG_BARCODE_RESTRICTED] if is_restricted(product["barcode"]) else []
    return _live_version(product, live.RECORD_PREFIX + product["uuid"], restricted)


def assemble(
    context: Context,
    base_rows: Iterable[sqlite3.Row],
    other_ids: Iterable[str] = (),
    include_hidden: bool = False,
) -> dict[str, Food]:
    """Put foods together from the main database and the live database.

    Args:
        context: Both databases.
        base_rows: Rows of `foods` of the main database.
        other_ids: Identifiers of foods the main database does not have:
            those that are products added in the live database are served
            from it.
        include_hidden: Also return what moderation hid, marked as such.

    Returns:
        The foods found, by identifier. A hidden food, or one whose versions
        are all hidden, is left out unless `include_hidden` is set.
    """
    base_rows = list(base_rows)
    base_ids = [row["id"] for row in base_rows]
    in_base = set(base_ids)
    live_only = {food_id: None for food_id in other_ids if food_id not in in_base}
    food_ids = [*base_ids, *live_only]
    products = live.products_by_food(context.live, food_ids)
    proposed = live.versions_by_food(context.live, food_ids)
    counts = live.vote_counts(context.live, food_ids)
    added_names = live.translations_by_food(context.live, food_ids)
    pks = [row["pk"] for row in base_rows]
    base_names = catalogue.names_by_pk(context.base, pks)
    several = catalogue.versions_by_pk(
        context.base, [row["pk"] for row in base_rows if row["versions_count"] > 1]
    )

    foods: dict[str, Food] = {}
    for row in base_rows:
        listed = [
            _base_version(version, version["version_id"], version["votes"])
            for version in several.get(row["pk"], ())
        ] or [_base_version(row, row["record_id"], row["votes"])]
        names = dict(base_names.get(row["pk"], {}))
        names.setdefault(row["name_lang"], (row["name"], NAME_ORIGIN_SOURCE))
        foods[row["id"]] = Food(
            id=row["id"],
            kind=row["kind"],
            barcode=row["barcode"],
            names=names,
            name=row["name"],
            name_lang=row["name_lang"],
            brand=row["brand"],
            category=row["category"],
            is_drink=bool(row["is_drink"]),
            popularity=row["popularity"] or 0,
            versions=listed,
        )
    for food_id in live_only:
        product = products.get(food_id)
        if product is None:
            continue
        names = {
            lang: (name, NAME_ORIGIN_SOURCE) for lang, name in json.loads(product["names"]).items()
        }
        foods[food_id] = Food(
            id=food_id,
            kind=product["kind"],
            barcode=product["barcode"],
            names=names,
            name=product["name"],
            name_lang=product["name_lang"],
            brand=product["brand"],
            category=product["category"],
            is_drink=bool(product["is_drink"]),
            popularity=0,
        )

    for food_id, food in foods.items():
        known = {version["version_id"] for version in food.versions}
        product = products.get(food_id)
        if product is not None and live.RECORD_PREFIX + product["uuid"] not in known:
            food.versions.append(_product_version(product))
        for row in proposed.get(food_id, ()):
            if row["version_id"] not in known:
                food.versions.append(_live_version(row, row["version_id"]))
        if food_id in counts:
            for version in food.versions:
                version["votes"] = counts[food_id].get(version["version_id"], 0)
        for lang, name in added_names.get(food_id, {}).items():
            if lang not in food.names or food.names[lang][1] == NAME_ORIGIN_KRATHEOS:
                food.names[lang] = (name, NAME_ORIGIN_KRATHEOS)

    record_ids = list(foods)
    for food in foods.values():
        record_ids += [version["version_id"] for version in food.versions]
    hidden = live.hidden_among(context.live, record_ids)
    visible: dict[str, Food] = {}
    for food_id, food in foods.items():
        product = products.get(food_id)
        food.hidden = food_id in hidden or (
            food_id in live_only and live.RECORD_PREFIX + product["uuid"] in hidden
        )
        if not include_hidden:
            food.versions = [
                version for version in food.versions if version["version_id"] not in hidden
            ]
            if food.hidden or not food.versions:
                continue
        food.versions = versions.ordered(food.versions, context.generation.priorities)
        visible[food_id] = food
    return visible


def find(context: Context, food_id: str, include_hidden: bool = False) -> Food | None:
    """Read one food by its identifier.

    Args:
        context: Both databases.
        food_id: Public identifier.
        include_hidden: Also return a food moderation hid.

    Returns:
        The food, or None when it does not exist or is hidden.
    """
    rows = catalogue.foods_by_id(context.base, [food_id])
    return assemble(context, rows, [food_id], include_hidden).get(food_id)


def find_by_barcode(context: Context, barcode: str) -> Food | None:
    """Read the food carrying a barcode.

    Args:
        context: Both databases.
        barcode: Canonical barcode.

    Returns:
        The food, or None when no visible food carries the barcode.
    """
    row = catalogue.food_by_barcode(context.base, barcode)
    if row is not None:
        return assemble(context, [row]).get(row["id"])
    product = live.product_by_barcode(context.live, barcode)
    if product is None:
        return None
    return assemble(context, [], [product["food_id"]]).get(product["food_id"])


def barcode_owner(context: Context, barcode: str) -> str | None:
    """Tell which food already carries a barcode, hidden or not.

    Args:
        context: Both databases.
        barcode: Canonical barcode.

    Returns:
        The identifier of the food, or None when the barcode is unknown.
    """
    row = catalogue.food_by_barcode(context.base, barcode)
    if row is not None:
        return row["id"]
    product = live.product_by_barcode(context.live, barcode)
    return product["food_id"] if product else None


def resolve_name(food: Food, language: str | None) -> tuple[str, str, str]:
    """Choose the name of a food to show to a person.

    Args:
        food: The food.
        language: Language of the person, e.g. `fr` or `fr-CH`.

    Returns:
        The name, its language and its origin: the name in the language of
        the person, else the English one, else the main name of the food.
    """
    candidates = []
    if language:
        code = language.strip().lower().replace("_", "-")
        candidates += [code, code.split("-")[0]]
    candidates += [FALLBACK_LANGUAGE, food.name_lang]
    for candidate in candidates:
        if candidate in food.names:
            name, origin = food.names[candidate]
            return name, candidate, origin
    return food.name, food.name_lang, NAME_ORIGIN_SOURCE


def _timestamp(day: str | None) -> str | None:
    """Write a date of the database as the API gives dates.

    Args:
        day: An ISO date, or None.

    Returns:
        The date at midnight UTC, or None.
    """
    return f"{str(day)[:10]}T00:00:00Z" if day else None


def _source(generation: Generation, version: Mapping) -> dict:
    """Describe the source of a version.

    Args:
        generation: The main database, which knows the sources.
        version: The version.

    Returns:
        Identifier, title, licence and link, as the API gives a source.
    """
    known = generation.sources.get(version["source"], {})
    title = known.get("name") or (
        KRATHEOS_TITLE if version["source"] == kratheos.SOURCE_ID else version["source"]
    )
    return {
        "id": version["source"],
        "title": title,
        "licence": version["licence"],
        "url": version["source_url"] or known.get("homepage") or None,
    }


def _servings(version: Mapping) -> list[dict]:
    """List the portions of a version.

    Args:
        version: The version.

    Returns:
        At most one portion: the database keeps one per version. Empty when
        the size of the portion is unknown.
    """
    size = version["serving_size"]
    if size is None or size <= 0:
        return []
    unit = version["serving_unit"] or UNITS[version["basis"]]
    return [{"label": version["serving_text"] or f"{size:g} {unit}", "amount": size, "unit": unit}]


def _nutrients(version: Mapping) -> dict:
    """Give the nutrients of a version under the names of the API.

    Args:
        version: The version.

    Returns:
        The nine values of the contract, None when unknown.
    """
    return {name: version[column] for name, column in NUTRIENT_FIELDS}


def render_version(generation: Generation, version: Mapping, served: bool) -> dict:
    """Write a version as the API gives it.

    Args:
        generation: The main database, which knows the sources.
        version: The version.
        served: Whether it is the one the request gets.

    Returns:
        The version object of the contract.
    """
    return {
        "version_id": version["version_id"],
        "origin": version["origin"],
        "source": _source(generation, version),
        "countries": list(version["countries"]),
        "basis": version["basis"],
        "nutrients": _nutrients(version),
        "servings": _servings(version),
        "votes": version["votes"],
        "served": served,
        "flags": list(version["flags"]),
        "updated_at": _timestamp(version["updated_at"]),
    }


def render_versions(context: Context, food: Food, country: str | None) -> list[dict]:
    """Write the versions of a food, the one served first, then by votes.

    Args:
        context: Both databases.
        food: The food.
        country: Country of the request, if any.

    Returns:
        The version objects.
    """
    served = food.served(country, context.user_version_votes)
    return [
        render_version(context.generation, version, version is served)
        for version in versions.listing_order(food.versions, served)
    ]


def render_food(context: Context, food: Food, language: str | None, country: str | None) -> dict:
    """Write a food as the API gives it.

    Args:
        context: Both databases.
        food: The food.
        language: Language of the request, for the name.
        country: Country of the request, for the version served.

    Returns:
        The food record of the contract.
    """
    served = food.served(country, context.user_version_votes)
    name, name_language, name_origin = resolve_name(food, language)
    flags = list(served["flags"])
    if versions.disagree(food.versions):
        flags.append(versions.FLAG_VERSIONS_DISAGREE)
    return {
        "id": food.id,
        "kind": food.kind,
        "barcode": food.barcode,
        "names": {lang: entry[0] for lang, entry in sorted(food.names.items())},
        "name": name,
        "name_language": name_language,
        "name_origin": name_origin,
        "brand": food.brand,
        "category": food.category,
        "countries": list(served["countries"]),
        "basis": served["basis"],
        "is_drink": food.is_drink,
        "nutrients": _nutrients(served),
        "servings": _servings(served),
        "source": _source(context.generation, served),
        "version_id": served["version_id"],
        "version_origin": served["origin"],
        "votes": served["votes"],
        "versions_count": len(food.versions),
        "flags": flags,
        "updated_at": _timestamp(served["updated_at"]),
    }
