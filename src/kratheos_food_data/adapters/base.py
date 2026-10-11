"""Shared plumbing of the source adapters.

An adapter is a module exposing `build(source, context) -> AdapterResult`. It
reads the downloaded files of one source and writes one staging Parquet file
with the columns of `schema.STAGING_COLUMNS`. Adapters stay close to the
source: they map columns and units, and leave barcode checks, derivations and
quality judgements to `normalize.py`.

Two helpers cover the two ways adapters work: `write_staging_query` for
sources large enough to be transformed in SQL, and `write_staging_rows` for
small tables parsed in Python.

A source that publishes household portions for its foods ("1 large = 50 g")
also writes them, with `write_portions_query`, to a second file next to the
staging file, in the columns of `schema.PORTION_COLUMNS`. Nothing is guessed
there either: a portion without a weight or without a wording is left out.
"""

from __future__ import annotations

import json
import shutil
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import duckdb

from .. import schema

Logger = Callable[[str], None]


@dataclass
class AdapterContext:
    """What an adapter receives to do its work.

    Attributes:
        con: DuckDB connection, with the barcode macros registered.
        files: Path of each file of the source, by the name it is declared
            under in the source list. The files are the stored originals (or
            an override for this run): an adapter reads them, never writes.
        work_dir: Scratch directory of the source (extracted archives).
        staging_path: Parquet file the adapter must write.
        log: Function receiving progress lines.
    """

    con: duckdb.DuckDBPyConnection
    files: dict[str, Path]
    work_dir: Path
    staging_path: Path
    log: Logger = print

    @property
    def portions_path(self) -> Path:
        """Return the Parquet file an adapter writes the portions of its records to.

        Returns:
            The file next to the staging file: `<source>.portions.parquet`.
            Only the adapters of the sources that publish household portions
            write it.
        """
        return self.staging_path.with_name(self.staging_path.stem + ".portions.parquet")

    def file(self, name: str) -> Path:
        """Return the path of a file of the source.

        Args:
            name: File name as declared in the source list.

        Returns:
            Where the file is for this run.

        Raises:
            KeyError: When the source declares no such file.
        """
        if name not in self.files:
            raise KeyError(f"the source has no file named {name}")
        return self.files[name]


@dataclass
class AdapterResult:
    """What an adapter reports back.

    Attributes:
        version: Version of the data when the adapter can read it from the
            files (rolling sources); None to keep the declared one.
        released: Date of the data, when read from the files.
        stats: Counts worth keeping in the manifest (rows read, rows left out
            and why).
    """

    version: str | None = None
    released: str | None = None
    stats: dict = field(default_factory=dict)


def sql_string(value: str | Path) -> str:
    """Quote a value as a SQL string literal.

    Args:
        value: Text or path to quote.

    Returns:
        The SQL literal.
    """
    return "'" + str(value).replace("'", "''") + "'"


def extract_members(archive: Path, wanted: Iterable[str], dest: Path) -> dict[str, Path]:
    """Extract chosen files of a zip archive, by base name.

    Only the base name of a member is used to build the destination path, so
    an archive cannot write outside `dest`. A member is not extracted again
    when the copy on disk has the size and the CRC the archive announces; the
    CRC of each extracted member is remembered in a small stamp file.

    Args:
        archive: The zip file.
        wanted: Base names of the members to extract.
        dest: Directory to extract into.

    Returns:
        A mapping of base name to extracted path.

    Raises:
        FileNotFoundError: When a wanted member is not in the archive.
    """
    dest.mkdir(parents=True, exist_ok=True)
    stamp_path = dest / ".extracted.json"
    try:
        stamp = json.loads(stamp_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        stamp = {}
    wanted_set = set(wanted)
    found: dict[str, Path] = {}
    with zipfile.ZipFile(archive) as bundle:
        for info in bundle.infolist():
            base = PurePosixPath(info.filename).name
            if info.is_dir() or base not in wanted_set or base in found:
                continue
            target = dest / base
            current = (
                target.exists()
                and target.stat().st_size == info.file_size
                and stamp.get(base) == info.CRC
            )
            if not current:
                partial = dest / (base + ".part")
                with bundle.open(info) as source, partial.open("wb") as out:
                    shutil.copyfileobj(source, out, 4 * 1024 * 1024)
                partial.replace(target)
                stamp[base] = info.CRC
                stamp_path.write_text(json.dumps(stamp, sort_keys=True), encoding="utf-8")
            found[base] = target
    missing = wanted_set - set(found)
    if missing:
        raise FileNotFoundError(f"{archive.name} lacks: {', '.join(sorted(missing))}")
    return found


def write_staging_query(context: AdapterContext, select_sql: str) -> int:
    """Write the staging file from a SQL query.

    Args:
        context: The adapter context.
        select_sql: A SELECT exposing every staging column by name.

    Returns:
        The number of rows written.
    """
    context.staging_path.parent.mkdir(parents=True, exist_ok=True)
    cast = schema.staging_cast_select(f"({select_sql}) AS adapter_rows")
    context.con.execute(
        f"COPY ({cast}) TO {sql_string(context.staging_path)} (FORMAT PARQUET, COMPRESSION ZSTD)"
    )
    return context.con.execute(
        f"SELECT count(*) FROM read_parquet({sql_string(context.staging_path)})"
    ).fetchone()[0]


def write_staging_rows(context: AdapterContext, rows: list[dict]) -> int:
    """Write the staging file from rows built in Python.

    Each row is a dict keyed by staging column name; missing keys are NULL.
    `names` is a dict of language code to name, `countries` and `source_flags`
    are lists, `updated_at` is an ISO date string or None.

    Args:
        context: The adapter context.
        rows: The records of the source.

    Returns:
        The number of rows written.
    """
    con = context.con
    json_columns = {"names", "countries", "source_flags"}
    plain = [column for column in schema.STAGING_COLUMNS if column.name not in json_columns]
    definitions = [f'"{column.name}" {column.type}' for column in plain]
    definitions += [f'"{name}" VARCHAR' for name in sorted(json_columns)]
    con.execute(f"CREATE OR REPLACE TEMP TABLE adapter_rows_raw ({', '.join(definitions)})")
    names = [column.name for column in plain] + sorted(json_columns)
    placeholders = ", ".join("?" for _ in names)
    payload = []
    for row in rows:
        unknown = set(row) - set(schema.staging_names())
        if unknown:
            raise KeyError(f"unknown staging columns: {sorted(unknown)}")
        values = [row.get(column.name) for column in plain]
        for name in sorted(json_columns):
            value = row.get(name)
            values.append(None if value is None else json.dumps(value, ensure_ascii=False))
        payload.append(values)
    if payload:
        con.executemany(f"INSERT INTO adapter_rows_raw VALUES ({placeholders})", payload)
    select = ", ".join(f'"{column.name}"' for column in plain)
    written = write_staging_query(
        context,
        f"""
        SELECT
            {select},
            json_transform(names, '"MAP(VARCHAR, VARCHAR)"') AS names,
            json_transform(countries, '["VARCHAR"]') AS countries,
            json_transform(source_flags, '["VARCHAR"]') AS source_flags
        FROM adapter_rows_raw
        """,
    )
    con.execute("DROP TABLE adapter_rows_raw")
    return written


# A label that is only a weight ("100 g", "4 oz", "1 lb") says nothing about
# the food: it is the same for every food, and a person types a weight
# anyway. Such a measure is not a household portion and is left out. A
# volume ("250 ml", "1 fl oz") stays: its weight is the one of this food.
WEIGHT_ONLY_LABEL = r"^[0-9][0-9.,/ ]*(g|gram|grams|kg|oz|ounce|ounces|lb|lbs|pound|pounds)$"


def whole_number_sql(expression: str) -> str:
    """Build the SQL writing a number the way a person writes it.

    Args:
        expression: A DOUBLE SQL expression.

    Returns:
        A VARCHAR expression: `1` for 1.0, `0.5` for 0.5.
    """
    return (
        f"CASE WHEN {expression} = floor({expression})"
        f" THEN CAST(CAST({expression} AS BIGINT) AS VARCHAR)"
        f" ELSE CAST({expression} AS VARCHAR) END"
    )


def write_portions_query(context: AdapterContext, select_sql: str) -> int:
    """Write the staging file of the portions from a SQL query.

    Args:
        context: The adapter context.
        select_sql: A SELECT exposing every column of `schema.PORTION_COLUMNS`
            by name, one row per portion kept.

    Returns:
        The number of portions written.
    """
    context.portions_path.parent.mkdir(parents=True, exist_ok=True)
    cast = schema.portion_cast_select(f"({select_sql}) AS adapter_portions")
    context.con.execute(
        f"COPY ({cast}) TO {sql_string(context.portions_path)} (FORMAT PARQUET, COMPRESSION ZSTD)"
    )
    return context.con.execute(
        f"SELECT count(*) FROM read_parquet({sql_string(context.portions_path)})"
    ).fetchone()[0]


def clean_text(value: object) -> str | None:
    """Normalise a text cell: collapse whitespace, turn blanks into None.

    Args:
        value: Cell content of any type.

    Returns:
        The trimmed text, or None when empty.
    """
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text or None
