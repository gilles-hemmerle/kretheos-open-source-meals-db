"""Measures how fast-food and restaurant chains are covered by a built database.

Two questions are answered from the files in `dist/`:

* which records are fast-food or restaurant items at all, according to what
  each source says (a "Fast Foods" or "Restaurant Foods" category, or a name
  saying "fast food" or "restaurant");
* which records belong to a known chain, found by the chain's name in the
  brand or in the name of the record.

Matching a chain by name is a measurement, not a classification kept in the
database: a supermarket product sold under a chain's brand (a bottled sauce, a
frozen pizza) matches too. The report therefore separates, for each chain, the
generic foods (always restaurant items) from the barcoded products, and gives
examples to judge by eye.
"""

from __future__ import annotations

from pathlib import Path

import duckdb

from . import aggregate

# Chain -> regular expression matched against the lower-cased brand and name.
CHAINS: dict[str, str] = {
    "McDonald's": r"\bmc ?donald'?s?\b|\bmcdo\b|\bbig mac\b|\bmc ?nuggets\b|\bmcflurry\b",
    "Burger King": r"\bburger king\b|\bwhopper\b",
    "KFC": r"\bkfc\b|kentucky fried chicken",
    "Subway": r"\bsubway\b",
    "Wendy's": r"\bwendy'?s\b",
    "Taco Bell": r"\btaco bell\b",
    "Pizza Hut": r"\bpizza hut\b",
    "Domino's": r"\bdomino'?s\b",
    "Papa John's": r"\bpapa john'?s\b",
    "Little Caesars": r"\blittle caesars?\b",
    "Starbucks": r"\bstarbucks\b",
    "Dunkin'": r"\bdunkin\b",
    "Chick-fil-A": r"\bchick-?fil-?a\b",
    "Popeyes": r"\bpopeyes\b",
    "Arby's": r"\barby'?s\b",
    "Five Guys": r"\bfive guys\b",
    "Chipotle": r"\bchipotle mexican grill\b",
    "Dairy Queen": r"\bdairy queen\b",
    "Jack in the Box": r"\bjack in the box\b",
    "Carl's Jr. / Hardee's": r"\bcarl'?s jr\b|\bhardee'?s\b",
    "Tim Hortons": r"\btim hortons?\b",
    "Quick": r"\bquick (burger|restaurant)\b|\bgiant quick\b",
    "O'Tacos": r"\bo'?tacos\b",
    "Paul": r"\bboulangeries? paul\b",
    "Brioche Dorée": r"\bbrioche dor[ée]e\b",
    "Nordsee": r"\bnordsee\b",
    "Vapiano": r"\bvapiano\b",
    "Pret A Manger": r"\bpret a manger\b",
    "Greggs": r"\bgreggs\b",
    "Nando's": r"\bnando'?s\b",
    "Costa Coffee": r"\bcosta coffee\b",
    "Applebee's": r"\bapplebee'?s\b",
    "Denny's": r"\bdenny'?s\b",
    "Olive Garden": r"\bolive garden\b",
    "T.G.I. Friday's": r"\bt\.?g\.?i\.? ?friday'?s\b",
    "Cracker Barrel": r"\bcracker barrel\b",
    "Panda Express": r"\bpanda express\b",
    "Panera": r"\bpanera\b",
}
GENERIC_FAST_FOOD = r"fast[ -]?food|restaurant|take[ -]?away|takeaway|restauration rapide"


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


def measure(dist: Path, examples: int = 4) -> dict:
    """Count fast-food and chain records per source.

    Args:
        dist: Output directory of a build.
        examples: Number of example records kept per chain and source.

    Returns:
        A report with, per source and kind, the records a source itself labels
        as fast food or restaurant food, and per chain the number of records,
        how many have the four macros and a few examples.
    """
    con = duckdb.connect()
    con.execute(
        f"""
        CREATE TEMP TABLE searched AS
        SELECT
            id, source, source_dataset, kind, name, brand, category, countries, has_macros,
            energy_kcal, protein_g, carbohydrate_g, fat_g, popularity,
            lower(coalesce(brand, '') || ' | ' || array_to_string(map_values(names), ' | '))
                AS haystack,
            lower(coalesce(category, '') || ' | ' || coalesce(name, '')) AS labelled
        FROM (
            SELECT * FROM {_table(dist, aggregate.GENERIC_NAME)}
            UNION ALL
            SELECT * FROM {_table(dist, aggregate.PRODUCTS_NAME)}
        )
        """
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
            SELECT source, coalesce(source_dataset, 'main'), kind, count(*),
                   count(*) FILTER (WHERE has_macros),
                   (list(name ORDER BY popularity DESC NULLS LAST, id))[1:{int(examples)}]
            FROM searched
            WHERE regexp_matches(labelled, ?)
            GROUP BY 1, 2, 3 ORDER BY 1, 2, 3
            """,
            [GENERIC_FAST_FOOD],
        ).fetchall()
    ]
    chains = []
    for chain, pattern in CHAINS.items():
        rows = con.execute(
            f"""
            SELECT source, coalesce(source_dataset, 'main'), kind, count(*),
                   count(*) FILTER (WHERE has_macros),
                   (list(
                       name || coalesce(' [' || brand || ']', '')
                           || CASE WHEN has_macros THEN ' - ' || CAST(energy_kcal AS VARCHAR)
                               || ' kcal, P ' || CAST(protein_g AS VARCHAR)
                               || ' C ' || CAST(carbohydrate_g AS VARCHAR)
                               || ' F ' || CAST(fat_g AS VARCHAR) ELSE '' END
                       ORDER BY popularity DESC NULLS LAST, id
                   ))[1:{int(examples)}],
                   count(*) FILTER (WHERE list_has_any(countries,
                       ['fr', 'ch', 'de', 'be', 'it', 'es', 'gb', 'nl', 'at', 'pt', 'lu']))
            FROM searched
            WHERE regexp_matches(haystack, ?)
            GROUP BY 1, 2, 3 ORDER BY 1, 2, 3
            """,
            [pattern],
        ).fetchall()
        if not rows:
            chains.append({"chain": chain, "records": 0, "by_source": []})
            continue
        chains.append(
            {
                "chain": chain,
                "records": sum(row[3] for row in rows),
                "by_source": [
                    {
                        "source": source,
                        "dataset": dataset,
                        "kind": kind,
                        "records": records,
                        "with_four_macros": with_macros,
                        "sold_in_western_europe": europe,
                        "examples": sample,
                    }
                    for source, dataset, kind, records, with_macros, sample, europe in rows
                ],
            }
        )
    return {"labelled_by_source": labelled, "chains": chains}


def format_report(report: dict) -> str:
    """Render the report as readable text.

    Args:
        report: The report built by `measure`.

    Returns:
        The text of the report.
    """
    lines = ["Records a source itself labels as fast food or restaurant food:"]
    for entry in report["labelled_by_source"]:
        lines.append(
            f"  {entry['source']}/{entry['dataset']} ({entry['kind']}): {entry['records']:,}"
            f" records, {entry['with_four_macros']:,} with the four macros"
        )
        for example in entry["examples"]:
            lines.append(f"      e.g. {example}")
    lines.append("")
    lines.append("Records naming a chain (brand or name):")
    for chain in report["chains"]:
        lines.append(f"  {chain['chain']}: {chain['records']:,} records")
        for entry in chain["by_source"]:
            lines.append(
                f"    {entry['source']}/{entry['dataset']} ({entry['kind']}):"
                f" {entry['records']:,}, {entry['with_four_macros']:,} with the four macros,"
                f" {entry['sold_in_western_europe']:,} sold in western Europe"
            )
            for example in entry["examples"]:
                lines.append(f"      e.g. {example}")
    return "\n".join(lines)
