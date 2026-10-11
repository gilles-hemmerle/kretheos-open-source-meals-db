"""Looks foods up in a built `food.sqlite`, the way a consumer would.

Three kinds of query, tried in this order:

* a valid barcode returns the food carrying it (`foods.barcode`);
* a public identifier (`gtin:...`, `ciqual:13000`...) returns that food;
* anything else is searched in the full-text index, every word taken as a
  prefix, so that the first letters of a name are enough.

It is both a checking tool for the command line and a worked example of the
three queries an application needs; only the Python standard library is used.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .barcode import normalize_gtin
from .names import display_name

SHOWN_COLUMNS = (
    "id",
    "record_id",
    "source",
    "kind",
    "barcode",
    "name",
    "name_lang",
    "brand",
    "category",
    "basis",
    "energy_kcal",
    "protein_g",
    "carbohydrate_g",
    "sugars_g",
    "fat_g",
    "saturated_fat_g",
    "fiber_g",
    "salt_g",
    "alcohol_g",
    "serving_size",
    "serving_unit",
    "is_drink",
    "countries",
    "confirmations",
    "has_macros",
    "plausible",
    "flags",
    "licence",
    "source_url",
    "updated_at",
    "sources",
    "other_records",
    "sources_agree",
)
JSON_COLUMNS = ("countries", "flags", "sources", "other_records")


def fts_query(text: str) -> str:
    """Turn what a person typed into an FTS5 query.

    Every word becomes a quoted prefix term, and all of them must match, so
    `pain compl` finds "Pain complet". Quoting keeps the punctuation a person
    may type from being read as FTS5 syntax.

    Args:
        text: The text typed.

    Returns:
        The FTS5 MATCH expression, empty when the text holds no word.
    """
    terms = []
    for word in text.split():
        cleaned = word.replace('"', "").strip()
        if cleaned:
            terms.append(f'"{cleaned}"*')
    return " ".join(terms)


def open_database(path: Path) -> sqlite3.Connection:
    """Open a built database read-only.

    Args:
        path: Path of `food.sqlite`.

    Returns:
        The connection, with rows readable by column name.

    Raises:
        FileNotFoundError: When the database has not been built.
    """
    if not path.exists():
        raise FileNotFoundError(f"{path} does not exist; run the build first")
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def find(path: Path, query: str, language: str | None = None, limit: int = 10) -> list[dict]:
    """Find the foods matching a barcode, an identifier or words of a name.

    Args:
        path: Path of `food.sqlite`.
        query: A barcode, a public identifier, or the beginning of words.
        language: Language of the person, for the name to show.
        limit: Maximum number of foods returned.

    Returns:
        The foods, each with its columns, `names` (every name with its
        origin) and `display_name` (the name to show in that language).
    """
    connection = open_database(path)
    try:
        columns = ", ".join(f"f.{name}" for name in SHOWN_COLUMNS)
        barcode = normalize_gtin(query)
        if barcode:
            rows = connection.execute(
                f"SELECT f.pk, {columns} FROM foods AS f WHERE f.barcode = ?", [barcode]
            ).fetchall()
        elif ":" in query and " " not in query.strip():
            rows = connection.execute(
                f"SELECT f.pk, {columns} FROM foods AS f WHERE f.id = ?", [query.strip()]
            ).fetchall()
        else:
            match = fts_query(query)
            if not match:
                return []
            rows = connection.execute(
                f"""
                SELECT f.pk, {columns}
                FROM foods_fts
                JOIN foods AS f ON f.pk = foods_fts.rowid
                WHERE foods_fts MATCH ?
                ORDER BY f.kind = 'generic' DESC, bm25(foods_fts), f.popularity DESC, f.id
                LIMIT ?
                """,
                [match, limit],
            ).fetchall()
        foods = []
        for row in rows[:limit]:
            food = {name: row[name] for name in SHOWN_COLUMNS}
            for name in JSON_COLUMNS:
                food[name] = json.loads(food[name]) if food[name] else None
            names = connection.execute(
                "SELECT lang, name, origin, kratheos_origin FROM names WHERE food_pk = ?"
                " ORDER BY lang",
                [row["pk"]],
            ).fetchall()
            food["names"] = [dict(name) for name in names]
            by_language = {name["lang"]: name["name"] for name in names}
            food["display_name"] = display_name(by_language, language, food["name"])
            foods.append(food)
        return foods
    finally:
        connection.close()


def format_records(records: list[dict]) -> str:
    """Render foods as readable text.

    Args:
        records: Foods returned by `find`.

    Returns:
        One block of lines per food, or a message when there is none.
    """
    if not records:
        return "no food found"
    blocks = []
    for record in records:
        title = record["display_name"] or "(no name)"
        if record.get("brand"):
            title += f" [{record['brand']}]"
        lines = [f"{record['id']}: {title}"]
        if record["record_id"] != record["id"]:
            others = ", ".join(record["other_records"] or []) or "none"
            lines.append(f"  record {record['record_id']}; other records: {others}")
        values = ", ".join(
            f"{label} {record[column]:g}"
            for label, column in (
                ("kcal", "energy_kcal"),
                ("protein", "protein_g"),
                ("carbohydrate", "carbohydrate_g"),
                ("sugars", "sugars_g"),
                ("fat", "fat_g"),
                ("saturated", "saturated_fat_g"),
                ("fibre", "fiber_g"),
                ("salt", "salt_g"),
                ("alcohol", "alcohol_g"),
            )
            if record.get(column) is not None
        )
        lines.append(f"  per {record['basis']}: {values}")
        details = [f"kind {record['kind']}"]
        if record.get("barcode"):
            details.append(f"barcode {record['barcode']}")
        if record.get("category"):
            details.append(f"category {record['category']}")
        if record.get("serving_size") is not None:
            details.append(f"serving {record['serving_size']:g} {record['serving_unit'] or ''}")
        if record.get("countries"):
            details.append("countries " + ",".join(record["countries"]))
        if record.get("confirmations") is not None:
            details.append(f"confirmed by {record['confirmations']}")
        lines.append("  " + "; ".join(details))
        names = ", ".join(
            f"{name['lang']}={name['name']}"
            + ("" if name["origin"] == "source" else f" ({name['kratheos_origin']})")
            for name in record["names"][:8]
        )
        more = len(record["names"]) - 8
        lines.append(f"  names: {names}" + (f" and {more} more" if more > 0 else ""))
        state = "plausible" if record["plausible"] else "NOT plausible"
        flags = ", ".join(record["flags"] or []) or "none"
        lines.append(f"  {state}; flags: {flags}; updated {record['updated_at']}")
        lines.append(f"  {record['licence']} - {record['source_url']}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)
