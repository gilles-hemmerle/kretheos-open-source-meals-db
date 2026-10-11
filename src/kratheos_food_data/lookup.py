"""Looks records up in a built database, for spot checks from the command line.

A query that is a valid barcode returns every product record carrying it, the
preferred one first. Any other query is taken as words of a food name: generic
foods whose name (in any language) contains every word come first, shortest
names first, then products by decreasing popularity.

This is a checking tool, not a search engine: the application that imports the
database is expected to build its own index.
"""

from __future__ import annotations

from pathlib import Path

import duckdb

from . import aggregate
from .barcode import normalize_gtin

SHOWN_COLUMNS = (
    "id",
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
    "has_macros",
    "plausible",
    "flags",
    "licence",
    "source_url",
    "updated_at",
)


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


def find(dist: Path, query: str, limit: int = 10) -> list[dict]:
    """Find the records matching a barcode or the words of a name.

    Args:
        dist: Output directory of a build.
        query: A barcode, or words of a food name.
        limit: Maximum number of records returned.

    Returns:
        The matching records as dictionaries of the shown columns. Barcode
        matches carry `preferred`, true for the record chosen for the barcode.
    """
    con = duckdb.connect()
    columns = ", ".join(SHOWN_COLUMNS)
    barcode = normalize_gtin(query)
    if barcode:
        preferred = con.execute(
            f"SELECT id FROM {_table(dist, aggregate.BY_BARCODE_NAME)} WHERE barcode = ?",
            [barcode],
        ).fetchone()
        cursor = con.execute(
            f"""
            SELECT {columns}, id = ? AS preferred
            FROM {_table(dist, aggregate.PRODUCTS_NAME)}
            WHERE barcode = ?
            ORDER BY preferred DESC, id
            LIMIT ?
            """,
            [preferred[0] if preferred else None, barcode, limit],
        )
    else:
        words = [word for word in query.lower().split() if word]
        if not words:
            return []
        searched = "lower(array_to_string(map_values(names), ' | '))"
        condition = " AND ".join(f"contains({searched}, ?)" for _ in words)
        cursor = con.execute(
            f"""
            SELECT {columns} FROM (
                SELECT *, 0 AS tier FROM {_table(dist, aggregate.GENERIC_NAME)}
                WHERE {condition}
                UNION ALL
                SELECT *, 1 AS tier FROM {_table(dist, aggregate.PRODUCTS_NAME)}
                WHERE {condition}
            )
            ORDER BY tier, CASE WHEN tier = 0 THEN length(name) END,
                popularity DESC NULLS LAST, id
            LIMIT ?
            """,
            [*words, *words, limit],
        )
    names = [description[0] for description in cursor.description]
    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


def format_records(records: list[dict]) -> str:
    """Render records as readable text.

    Args:
        records: Records returned by `find`.

    Returns:
        One block of lines per record, or a message when there is none.
    """
    if not records:
        return "no record found"
    blocks = []
    for record in records:
        title = record["name"] or "(no name)"
        if record.get("brand"):
            title += f" [{record['brand']}]"
        mark = " (preferred)" if record.get("preferred") else ""
        lines = [f"{record['id']}{mark}: {title}"]
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
        lines.append("  " + "; ".join(details))
        state = "plausible" if record["plausible"] else "NOT plausible"
        flags = ", ".join(record["flags"] or []) or "none"
        lines.append(f"  {state}; flags: {flags}; updated {record['updated_at']}")
        lines.append(f"  {record['licence']} - {record['source_url']}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)
