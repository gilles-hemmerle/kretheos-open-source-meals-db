"""The versions of the values of a food, and the rule choosing the one served.

A food can have several versions of its values: one per record of a source
that knows it, the product added in Kratheos under its barcode, and those
proposed by users. Nothing is averaged or overwritten; people vote for the
version they find right.

Which version is served. A version is "usable" when its values are plausible
and it has the four macros (energy, protein, carbohydrate, fat).

1. Threshold. A version coming from a user (source `kratheos`: a product
   added, a version proposed) with fewer votes than the threshold
   (`DEFAULT_USER_VERSION_VOTES`, a setting) is left out when the food has at
   least one usable version of a source. When no version of a source is
   usable (there is none, or all are implausible or incomplete), every
   version of a user counts: one person's plausible entry replaces values
   the build itself flags.
2. Country. When a country is asked for, the candidates that list other
   countries are dropped, and those that list the country or no country at
   all stay (unknown is not elsewhere), provided at least one candidate
   listing the country is usable, or no candidate at all is usable.
   Otherwise nothing is dropped: a country never trades usable values for
   values that are not. Nothing is dropped either when that would leave no
   candidate.
3. Order among the candidates, the first wins:
   a. the most votes;
   b. plausible values;
   c. complete macros;
   d. it lists the country asked for (only when a country is asked for);
   e. a version of a source before a version of a user;
   f. among versions of sources the most recent, then the fixed source
      order, then the identifier; among versions of users the oldest, then
      the identifier.

So a version of a user needs strictly more votes than a version of a source
to pass it, and a later proposal strictly more votes than an earlier one.
Without votes and without a country this is the order of the build, as it
always was: plausible values, complete macros, most recent, fixed source
order, identifier.

"The order of the build" below is steps b, c, e and f: what orders the
versions of a food when neither votes nor a country are looked at. It is the
`position` of a version in `food.sqlite`.

The build applies the rule in SQL without a country, to fill `foods`
(`aggregate.build_foods`); a service applies this module per request, with
the country of the person. The tests check that both give the same answer.

Two versions "differ" by the tolerance the build uses between sources: energy
by more than 10 kcal and 10 %, or protein, carbohydrate or fat by more than
2 g and 20 %, or one is per 100 g and the other per 100 ml. Only versions
that have the four values are compared.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

ORIGIN_SOURCE = "source"
ORIGIN_USER = "user"
USER_SOURCE = "kratheos"
DEFAULT_USER_VERSION_VOTES = 3
AGREE_ENERGY_ABSOLUTE_KCAL = 10.0
AGREE_ENERGY_RELATIVE = 0.10
AGREE_MACRO_ABSOLUTE_G = 2.0
AGREE_MACRO_RELATIVE = 0.20
COMPARED_VALUES = ("energy_kcal", "protein_g", "carbohydrate_g", "fat_g")
FLAG_VERSIONS_DISAGREE = "versions_disagree"


def origin_of(source: str) -> str:
    """Tell whether a version comes from a source or from a user.

    Args:
        source: Identifier of the source of the version.

    Returns:
        `user` for what was entered in Kratheos, `source` otherwise.
    """
    return ORIGIN_USER if source == USER_SOURCE else ORIGIN_SOURCE


def usable(version: Mapping) -> bool:
    """Tell whether the values of a version can be relied on.

    Args:
        version: A version with `plausible` and `has_macros`.

    Returns:
        True when the values are plausible and the four macros are there.
    """
    return bool(version.get("plausible")) and bool(version.get("has_macros"))


def build_order(version: Mapping, priorities: Mapping[str, int]) -> tuple:
    """Return the sort key placing a version in the order of the build.

    Args:
        version: A version with `plausible`, `has_macros`, `updated_at` (ISO
            date or None), `source` and `version_id`.
        priorities: Fixed order of the sources, lowest first.

    Returns:
        A key sorting plausible values first, then complete macros, then the
        versions of sources before those of users; among sources the most
        recent, the fixed source order and the identifier; among users the
        oldest and the identifier.
    """
    updated = str(version.get("updated_at") or "")[:10]
    quality = (0 if version.get("plausible") else 1, 0 if version.get("has_macros") else 1)
    if origin_of(version["source"]) == ORIGIN_USER:
        # Dates sort as text; a missing date sorts after every date.
        return (*quality, 1, updated or "~", 0, version["version_id"])
    # Negating each character code sorts the dates descending with the missing
    # ones last, as `DESC NULLS LAST` does.
    recent = tuple(-ord(char) for char in updated) if updated else (1,)
    return (*quality, 0, recent, priorities.get(version["source"], 10**6), version["version_id"])


def ordered(versions: Iterable[Mapping], priorities: Mapping[str, int]) -> list:
    """Sort versions in the order of the build.

    Args:
        versions: The versions of one food.
        priorities: Fixed order of the sources, lowest first.

    Returns:
        The same objects, first in the order of the build first.
    """
    return sorted(versions, key=lambda version: build_order(version, priorities))


def choose_served(
    versions: Sequence[Mapping],
    country: str | None = None,
    user_version_votes: int = DEFAULT_USER_VERSION_VOTES,
) -> Mapping | None:
    """Choose the version served among the visible versions of a food.

    Args:
        versions: The visible versions, already in the order of the build
            (see `ordered`), each with `origin`, `votes`, `countries`,
            `plausible` and `has_macros`.
        country: Country of the person, ISO 3166-1 alpha-2, if known.
        user_version_votes: Votes a version coming from a user needs to be
            served when a version of a source is usable.

    Returns:
        The version served, or None when there is no version.
    """
    if not versions:
        return None
    candidates = list(enumerate(versions))
    if any(version["origin"] == ORIGIN_SOURCE and usable(version) for version in versions):
        candidates = [
            (position, version)
            for position, version in candidates
            if version["origin"] == ORIGIN_SOURCE or version["votes"] >= user_version_votes
        ]
    wanted = country.upper() if country else None

    def lists_the_country(version: Mapping) -> bool:
        """Tell whether a version lists the country asked for."""
        return wanted in (version.get("countries") or ())

    if wanted:
        usable_there = any(
            usable(version) for _, version in candidates if lists_the_country(version)
        )
        if usable_there or not any(usable(version) for _, version in candidates):
            kept = [
                (position, version)
                for position, version in candidates
                if lists_the_country(version) or not version.get("countries")
            ]
            candidates = kept or candidates

    def rank(item: tuple[int, Mapping]) -> tuple:
        """Order the candidates: votes, quality, country, then the order of the build."""
        position, version = item
        return (
            -version["votes"],
            0 if version.get("plausible") else 1,
            0 if version.get("has_macros") else 1,
            0 if wanted is None or lists_the_country(version) else 1,
            position,
        )

    return min(candidates, key=rank)[1]


def listing_order(versions: Sequence[Mapping], served: Mapping | None) -> list:
    """Order versions for display: the one served, then by votes.

    Args:
        versions: The visible versions, in the order of the build.
        served: The version served to the request.

    Returns:
        The versions, the served one first, then the most voted, the order
        of the build settling the ties.
    """
    indexed = list(enumerate(versions))
    indexed.sort(key=lambda item: (item[1] is not served, -item[1]["votes"], item[0]))
    return [version for _, version in indexed]


def _spread_agrees(values: list[float], absolute: float, relative: float) -> bool:
    """Tell whether figures of several versions agree.

    Args:
        values: The figure of each version.
        absolute: Largest accepted difference, in the unit of the figure.
        relative: Largest accepted difference, as a share of the highest.

    Returns:
        True when the highest and the lowest are close enough.
    """
    highest, lowest = max(values), min(values)
    return highest - lowest <= max(absolute, relative * highest)


def disagree(versions: Iterable[Mapping]) -> bool:
    """Tell whether the versions of a food give different values.

    Args:
        versions: The visible versions, each with `basis`, `has_macros` and
            the four compared values.

    Returns:
        True when at least two versions have the four values and either do
        not use the same basis or differ by more than the tolerance.
    """
    complete = [version for version in versions if version.get("has_macros")]
    if len(complete) < 2:
        return False
    if len({version["basis"] for version in complete}) > 1:
        return True
    for name in COMPARED_VALUES:
        values = [float(version[name]) for version in complete]
        if name == "energy_kcal":
            agrees = _spread_agrees(values, AGREE_ENERGY_ABSOLUTE_KCAL, AGREE_ENERGY_RELATIVE)
        else:
            agrees = _spread_agrees(values, AGREE_MACRO_ABSOLUTE_G, AGREE_MACRO_RELATIVE)
        if not agrees:
            return True
    return False
