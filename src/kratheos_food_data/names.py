"""The rule for the name to show to a person, in Python and in SQL.

A food has a name in each language its source gives, plus those Kratheos
added. The name to show is, in this order:

1. the name in the person's language;
2. if there is none, the English name;
3. if there is no English name either, whatever name the record has (its
   main name, `foods.name`).

`display_name` applies the rule to a mapping of names; `DISPLAY_NAME_SQL` is
the same rule as a query on `food.sqlite`, which also carries it ready-made
as the view `display_names`.
"""

from __future__ import annotations

from collections.abc import Mapping

FALLBACK_LANGUAGE = "en"

# Parameters: :id (identifier of the food) and :language (language code).
DISPLAY_NAME_SQL = """
SELECT COALESCE(
    (SELECT n.name FROM names AS n WHERE n.food_pk = f.pk AND n.lang = :language),
    (SELECT n.name FROM names AS n WHERE n.food_pk = f.pk AND n.lang = 'en'),
    f.name
) AS name
FROM foods AS f
WHERE f.id = :id
""".strip()

# One row per food and per language known to the database:
#   SELECT name FROM display_names WHERE id = ? AND lang = ?
DISPLAY_NAMES_VIEW_SQL = """
CREATE VIEW display_names AS
SELECT
    f.id AS id,
    l.lang AS lang,
    COALESCE(n.name, e.name, f.name) AS name
FROM foods AS f
CROSS JOIN languages AS l
LEFT JOIN names AS n ON n.food_pk = f.pk AND n.lang = l.lang
LEFT JOIN names AS e ON e.food_pk = f.pk AND e.lang = 'en'
""".strip()


def display_name(
    names: Mapping[str, str], language: str | None, default: str | None = None
) -> str | None:
    """Choose the name of a food to show to a person.

    Args:
        names: The names of the food, by language code.
        language: Language of the person, e.g. `fr` or `fr-CH`; a regional
            code falls back to its language (`fr-CH` to `fr`).
        default: The main name of the record, used when there is neither a
            name in the person's language nor an English one.

    Returns:
        The name in the person's language, else the English name, else the
        main name of the record, else any name it has; None without any name.
    """
    candidates = []
    if language:
        code = language.strip().lower().replace("_", "-")
        candidates += [code, code.split("-")[0]]
    candidates.append(FALLBACK_LANGUAGE)
    for candidate in candidates:
        name = names.get(candidate)
        if name:
            return name
    if default:
        return default
    for lang in sorted(names):
        if names[lang]:
            return names[lang]
    return None
