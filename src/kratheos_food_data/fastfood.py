"""Measures how prepared dishes and restaurant chains are covered.

Three questions are answered from the files in `dist/` (the sources as they
are, before the Kratheos layer):

* generic prepared dishes: which generic foods are an average pizza, burger,
  kebab, portion of fries, sandwich, sushi, lasagne... These answer "a
  generic pizza" when no exact product exists;
* which records a source itself labels as fast food or restaurant food (a
  "Fast Foods" or "Restaurant Foods" category, a name saying so);
* which records belong to a known chain. For generic foods the chain is in
  the name ("McDONALD'S, BIG MAC"); for products the brand is looked at
  first, and records that only mention the chain in their name (crisps "Big
  Mac flavour") are counted apart.

This is a measurement, not a classification kept in the database: the
patterns are simple and every figure comes with examples to judge by eye.
"""

from __future__ import annotations

from pathlib import Path

import duckdb

from . import aggregate

# Chain -> regular expression matched against lower-cased text.
CHAINS: dict[str, str] = {
    "McDonald's": r"\bmc ?donald'?s?\b|\bmcdo\b",
    "Burger King": r"\bburger king\b",
    "KFC": r"\bkfc\b|kentucky fried chicken",
    "Subway": r"\bsubway\b",
    "Starbucks": r"\bstarbucks\b",
    "Domino's": r"\bdomino'?s pizza\b|^domino'?s$|\bdomino'?s,",
    "Pizza Hut": r"\bpizza hut\b",
    "Five Guys": r"\bfive guys\b",
    "Quick": r"^quick$|\bquick (burger|restaurant)s?\b",
    "O'Tacos": r"\bo'?tacos\b",
    "Paul (bakery)": r"^paul$|\bboulangeries? paul\b",
    "Brioche Dorée": r"\bbrioche dor[ée]e\b",
    "Nordsee": r"\bnordsee\b",
    "Vapiano": r"\bvapiano\b",
    "Pret A Manger": r"\bpret a manger\b",
    "Greggs": r"\bgreggs\b",
    "Nando's": r"\bnando'?s\b",
    "Costa Coffee": r"\bcosta coffee\b",
    "Tim Hortons": r"\btim hortons?\b",
    "Wendy's": r"^wendy'?s\b|\bwendy'?s,",
    "Taco Bell": r"\btaco bell\b",
    "Papa John's": r"^papa john'?s$|\bpapa john'?s (pizza|14)",
    "Little Caesars": r"\blittle caesars?\b",
    "Dunkin'": r"\bdunkin\b",
    "Chick-fil-A": r"\bchick-?fil-?a\b",
    "Popeyes": r"\bpopeyes\b",
    "Arby's": r"\barby'?s\b",
    "Dairy Queen": r"\bdairy queen\b",
    "Jack in the Box": r"\bjack in the box\b",
    "Panda Express": r"\bpanda express\b",
    "Panera": r"\bpanera\b",
    "Applebee's": r"\bapplebee'?s\b",
    "Denny's": r"\bdenny'?s\b",
    "Olive Garden": r"\bolive garden\b",
}
# Dish -> regular expression matched against the lower-cased names of a record.
DISHES: dict[str, str] = {
    "pizza": r"\bpizzas?\b",
    "burger": r"\b(ham|cheese|chicken|veggie|fish|beef)?burgers?\b",
    "kebab, döner, gyros, shawarma": (
        r"\bkebabs?\b|\bkebap\b|\bd[öo]ner\b|\bgyros?\b|\bs?[hc]h?awarma\b"
    ),
    "fries": r"\bfries\b|\bfrites\b|french[ -]fried|\bpommes[ -]frites\b",
    "sandwich": r"\bsandwich(es|s)?\b|\bpaninis?\b|\bcroque[ -]monsieur\b|\bsubs?\b",
    "wrap, burrito, taco": r"\bwraps?\b|\bburritos?\b|\btacos?\b|\bfajitas?\b|\bquesadillas?\b",
    "sushi": r"\bsushis?\b|\bmakis?\b|\bsashimis?\b",
    "lasagne": r"\blasagn[ea]s?\b",
    "hot dog": r"\bhot[ -]?dogs?\b",
    "nuggets, fried chicken": r"\bnuggets?\b|\bfried chicken\b|\bpoulet frit\b",
    "quiche": r"\bquiches?\b",
    "noodle dishes": r"\bramen\b|\bpad thai\b|\bchow mein\b|\blo mein\b|\bnouilles saut[ée]es\b",
    "falafel": r"\bfalafels?\b",
    "curry": r"\bcurr(y|ies)\b",
    "paella, risotto, couscous": r"\bpaella\b|\brisotto\b|\bcouscous\b",
    "spring roll, dumpling": r"\bspring rolls?\b|\begg rolls?\b|\bnems?\b|\bdumplings?\b|\bravioli",
}
# Names holding one of these are an ingredient or a flavour, not the dish.
NOT_A_DISH = (
    r"\bsauce\b|\bbread\b|\bbuns?\b|\bdough\b|\bcrust\b|\bbase\b|seasoning|\bspread\b|\bpain "
    r"|\bp[âa]te [àa]|\bpatt(y|ies)\b|\bflavou?r|\bpowder\b|\bpaste\b|\bmix\b|\bkit\b"
    r"|\brolls?, (hamburger|hot)|\bcheese, |\bfrom frozen\b|\bfrozen, unprepared\b"
)
FAST_FOOD_LABEL = r"fast[ -]?foods?|restaurant|take[ -]?away|takeaway|restauration rapide"
EUROPE = ("FR", "CH", "DE", "BE", "IT", "ES", "GB", "NL", "AT", "PT", "LU", "IE")


def _table(dist: Path, name: str) -> str:
    """Return the SQL table expression of an output file.

    Args:
        dist: Output directory of a build.
        name: Logical table name.

    Returns:
        A `read_parquet` call.

    Raises:
        FileNotFoundError: When the database has not been built.
    """
    path = dist / f"{name}.parquet"
    if not path.exists():
        raise FileNotFoundError(f"{path} does not exist; run the build first")
    return "read_parquet('" + str(path).replace("'", "''") + "')"


EXAMPLE_SQL = """
    name || coalesce(' [' || brand || ']', '')
        || CASE WHEN has_macros THEN ' - ' || CAST(round(energy_kcal) AS INTEGER) || ' kcal, P '
            || round(protein_g, 1) || ' C ' || round(carbohydrate_g, 1) || ' F ' || round(fat_g, 1)
            ELSE '' END
"""


def measure(dist: Path, examples: int = 4) -> dict:
    """Measure dishes, fast-food labels and chains, per source.

    Args:
        dist: Output directory of a build.
        examples: Number of example records kept per group.

    Returns:
        A report with three parts: `dishes` (generic prepared dishes per dish
        and source), `labelled` (records a source labels as fast food) and
        `chains` (records per chain and source).
    """
    con = duckdb.connect()
    take = int(examples)
    europe = ", ".join(f"'{code}'" for code in EUROPE)
    con.execute(
        f"""
        CREATE TEMP TABLE searched AS
        SELECT
            id, source, coalesce(source_dataset, 'main') AS dataset, kind, name, brand, category,
            countries, has_macros, barcode, energy_kcal, protein_g, carbohydrate_g, fat_g,
            popularity,
            lower(array_to_string(map_values(names), ' | ')) AS names_text,
            lower(coalesce(brand, '')) AS brand_text,
            lower(coalesce(category, '') || ' | ' || coalesce(name, '')) AS labelled_text
        FROM (
            SELECT * FROM {_table(dist, aggregate.GENERIC_NAME)}
            UNION ALL
            SELECT * FROM {_table(dist, aggregate.PRODUCTS_NAME)}
        )
        """
    )
    dishes = []
    for dish, pattern in DISHES.items():
        rows = con.execute(
            f"""
            SELECT source, dataset, count(*), count(*) FILTER (WHERE has_macros),
                   (list({EXAMPLE_SQL} ORDER BY length(name), id))[1:{take}]
            FROM searched
            WHERE kind = 'generic' AND regexp_matches(names_text, ?)
              AND NOT regexp_matches(names_text, ?)
            GROUP BY 1, 2 ORDER BY 1, 2
            """,
            [pattern, NOT_A_DISH],
        ).fetchall()
        dishes.append(
            {
                "dish": dish,
                "records": sum(row[2] for row in rows),
                "by_source": [
                    {
                        "source": source,
                        "dataset": dataset,
                        "records": records,
                        "with_four_macros": with_macros,
                        "examples": sample,
                    }
                    for source, dataset, records, with_macros, sample in rows
                ],
            }
        )
    labelled = [
        {
            "source": source,
            "dataset": dataset,
            "kind": kind,
            "records": records,
            "with_four_macros": with_macros,
            "examples": sample,
        }
        for source, dataset, kind, records, with_macros, sample in con.execute(
            f"""
            SELECT source, dataset, kind, count(*), count(*) FILTER (WHERE has_macros),
                   (list({EXAMPLE_SQL} ORDER BY popularity DESC NULLS LAST, id))[1:{take}]
            FROM searched
            WHERE regexp_matches(labelled_text, ?)
            GROUP BY 1, 2, 3 ORDER BY 1, 2, 3
            """,
            [FAST_FOOD_LABEL],
        ).fetchall()
    ]
    chains = []
    for chain, pattern in CHAINS.items():
        rows = con.execute(
            f"""
            WITH matched AS (
                SELECT *,
                    CASE WHEN kind = 'generic' THEN regexp_matches(names_text, ?)
                         ELSE regexp_matches(brand_text, ?) END AS is_item,
                    kind = 'product' AND NOT regexp_matches(brand_text, ?)
                        AND regexp_matches(names_text, ?) AS only_named
                FROM searched
            )
            SELECT source, dataset, kind,
                   count(*) FILTER (WHERE is_item),
                   count(*) FILTER (WHERE is_item AND has_macros),
                   count(*) FILTER (WHERE is_item AND barcode IS NOT NULL),
                   count(*) FILTER (WHERE is_item AND list_has_any(countries, [{europe}])),
                   count(*) FILTER (WHERE is_item AND list_contains(countries, 'FR')),
                   count(*) FILTER (WHERE is_item AND list_contains(countries, 'CH')),
                   count(*) FILTER (WHERE is_item AND list_contains(countries, 'US')),
                   count(*) FILTER (WHERE only_named),
                   (list({EXAMPLE_SQL} ORDER BY popularity DESC NULLS LAST, id)
                        FILTER (WHERE is_item))[1:{take}]
            FROM matched
            WHERE is_item OR only_named
            GROUP BY 1, 2, 3 ORDER BY 1, 2, 3
            """,
            [pattern, pattern, pattern, pattern],
        ).fetchall()
        chains.append(
            {
                "chain": chain,
                "records": sum(row[3] for row in rows),
                "by_source": [
                    {
                        "source": row[0],
                        "dataset": row[1],
                        "kind": row[2],
                        "records": row[3],
                        "with_four_macros": row[4],
                        "with_valid_barcode": row[5],
                        "sold_in_europe": row[6],
                        "sold_in_france": row[7],
                        "sold_in_switzerland": row[8],
                        "sold_in_united_states": row[9],
                        "only_named_in_another_brand": row[10],
                        "examples": row[11] or [],
                    }
                    for row in rows
                ],
            }
        )
    return {"dishes": dishes, "labelled": labelled, "chains": chains}


def format_report(report: dict) -> str:
    """Render the report as readable text.

    Args:
        report: The report built by `measure`.

    Returns:
        The text of the report.
    """
    lines = ["Generic prepared dishes (generic foods only):"]
    for dish in report["dishes"]:
        lines.append(f"  {dish['dish']}: {dish['records']:,} records")
        for entry in dish["by_source"]:
            lines.append(
                f"    {entry['source']}/{entry['dataset']}: {entry['records']:,},"
                f" {entry['with_four_macros']:,} with the four macros"
            )
            for example in entry["examples"]:
                lines.append(f"      e.g. {example}")
    lines.append("")
    lines.append("Records a source itself labels as fast food or restaurant food:")
    for entry in report["labelled"]:
        lines.append(
            f"  {entry['source']}/{entry['dataset']} ({entry['kind']}): {entry['records']:,}"
            f" records, {entry['with_four_macros']:,} with the four macros"
        )
        for example in entry["examples"]:
            lines.append(f"      e.g. {example}")
    lines.append("")
    lines.append("Items of a chain (name for generic foods, brand for products):")
    for chain in report["chains"]:
        lines.append(f"  {chain['chain']}: {chain['records']:,} records")
        for entry in chain["by_source"]:
            lines.append(
                f"    {entry['source']}/{entry['dataset']} ({entry['kind']}): {entry['records']:,},"
                f" {entry['with_four_macros']:,} with the four macros,"
                f" {entry['with_valid_barcode']:,} with a valid barcode;"
                f" sold in France {entry['sold_in_france']:,},"
                f" Switzerland {entry['sold_in_switzerland']:,},"
                f" Europe {entry['sold_in_europe']:,},"
                f" United States {entry['sold_in_united_states']:,};"
                f" {entry['only_named_in_another_brand']:,} other products only name the chain"
            )
            for example in entry["examples"]:
                lines.append(f"      e.g. {example}")
    return "\n".join(lines)
