"""The main database as the service sees it: read-only, replaced without a pause.

`food.sqlite` is never modified in place. A new build is moved over the old
file in one step (a rename), so the file on disk is always a whole database.
Each request checks whether the file changed (one `stat`); when it did, the
new file is opened and checked, and takes over for the requests that follow.
Requests already running finish on the file they started with, which the
system keeps alive as long as a connection holds it: no request is lost.

A file that cannot be served (missing, damaged, of another layout) never
replaces the one being served: the service goes on with what it has and says
so in its log.

A file that starts being served is read once in the background, so that the
system holds it in memory before the first searches need it.

Each file being served is a `Generation`: a small pool of connections, what
was read once from it (sources, licence, counts, the build date of the
manifest next to it) and the range of internal numbers the barcoded products
occupy, which lets a short search look only at generic foods and dishes.
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import sqlite3
import threading
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .. import kratheos, sqlite_export
from .settings import MANIFEST_NAME

SUPPORTED_LAYOUT = sqlite_export.LAYOUT_VERSION
GTIN_FIRST = "gtin:"
GTIN_AFTER = "gtin;"  # the first text sorting after every identifier starting with gtin:
CHUNK = 500
WARM_CHUNK_BYTES = 8 * 1024 * 1024
Logger = Callable[[str], None]

FOOD_COLUMNS = (
    "pk",
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
    "carbohydrate_total_g",
    "sugars_g",
    "fat_g",
    "saturated_fat_g",
    "fiber_g",
    "salt_g",
    "alcohol_g",
    "serving_size",
    "serving_unit",
    "serving_text",
    "is_drink",
    "countries",
    "popularity",
    "votes",
    "has_macros",
    "plausible",
    "flags",
    "licence",
    "source_url",
    "updated_at",
    "versions_count",
)
_FOOD_SELECT = "SELECT " + ", ".join(FOOD_COLUMNS) + " FROM foods"


class DatabaseUnavailable(Exception):
    """No main database can be served."""


class DatabaseRejected(Exception):
    """A file is not a main database this service can serve."""


@dataclass
class Generation:
    """One `food.sqlite` being served.

    Attributes:
        path: Path the file was opened at.
        signature: Device, inode, size and modification time of the file.
        sources: The sources of the database, by identifier.
        priorities: Fixed order of the sources, lowest first.
        meta: The key and value pairs of its `meta` table.
        foods: Number of foods.
        built_at: When it was built: from the manifest next to it, else the
            modification time of the file.
        manifest: The manifest next to it, or None.
        first_gtin: Lowest internal number of a barcoded product, or None
            when the products do not occupy one unbroken range.
        last_gtin: Highest internal number of a barcoded product.
        loaded_at: When the service started serving it.
    """

    path: Path
    signature: tuple
    sources: dict[str, dict] = field(default_factory=dict)
    priorities: dict[str, int] = field(default_factory=dict)
    meta: dict[str, str] = field(default_factory=dict)
    foods: int = 0
    built_at: str = ""
    manifest: dict | None = None
    first_gtin: int | None = None
    last_gtin: int | None = None
    loaded_at: str = ""
    _idle: queue.LifoQueue = field(default_factory=queue.LifoQueue, repr=False)
    _slots: threading.Semaphore | None = field(default=None, repr=False)
    _retired: bool = field(default=False, repr=False)

    def connect(self) -> sqlite3.Connection:
        """Open a read-only connection to the file of this generation.

        Returns:
            A connection whose rows read by column name.
        """
        connection = sqlite3.connect(
            f"{self.path.resolve().as_uri()}?mode=ro", uri=True, check_same_thread=False
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = 1")
        return connection

    def retire(self) -> None:
        """Close the idle connections; those in use close when given back."""
        self._retired = True
        while True:
            try:
                self._idle.get_nowait().close()
            except queue.Empty:
                return


def file_signature(path: Path) -> tuple | None:
    """Describe a file so that a replacement is noticed.

    Args:
        path: Path of the file.

    Returns:
        Device, inode, size and modification time; None when it is missing.
    """
    try:
        status = os.stat(path)
    except OSError:
        return None
    return (status.st_dev, status.st_ino, status.st_size, status.st_mtime_ns)


def read_generation(path: Path, signature: tuple) -> Generation:
    """Open a main database and read what the service keeps about it.

    Args:
        path: Path of `food.sqlite`.
        signature: Its signature, taken before opening it.

    Returns:
        The generation, ready to serve.

    Raises:
        DatabaseRejected: When the file is not a database of the layout this
            service serves.
    """
    generation = Generation(path=path, signature=signature)
    try:
        connection = generation.connect()
    except sqlite3.Error as error:
        raise DatabaseRejected(f"{path}: {error}") from error
    try:
        generation.meta = dict(connection.execute("SELECT key, value FROM meta").fetchall())
        layout = generation.meta.get("layout_version")
        if layout != str(SUPPORTED_LAYOUT):
            raise DatabaseRejected(
                f"{path}: layout {layout}, this service serves layout {SUPPORTED_LAYOUT}"
            )
        for row in connection.execute("SELECT * FROM sources ORDER BY id"):
            generation.sources[row["id"]] = dict(row)
            generation.priorities[row["id"]] = row["priority"]
        generation.priorities.setdefault(kratheos.SOURCE_ID, kratheos.KRATHEOS_SOURCE.priority)
        generation.foods = connection.execute("SELECT count(*) FROM foods").fetchone()[0]
        lowest, highest, barcoded = connection.execute(
            "SELECT min(pk), max(pk), count(*) FROM foods WHERE id >= ? AND id < ?",
            (GTIN_FIRST, GTIN_AFTER),
        ).fetchone()
        if barcoded and highest - lowest + 1 == barcoded:
            generation.first_gtin, generation.last_gtin = lowest, highest
        connection.execute("SELECT rowid FROM foods_fts WHERE foods_fts MATCH 'a' LIMIT 1")
        connection.execute("SELECT food_pk FROM versions LIMIT 1").fetchall()
    except sqlite3.Error as error:
        connection.close()
        raise DatabaseRejected(f"{path}: {error}") from error
    except DatabaseRejected:
        connection.close()
        raise
    generation._idle.put(connection)
    manifest_path = path.with_name(MANIFEST_NAME)
    try:
        generation.manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        generation.manifest = None
    built_at = (generation.manifest or {}).get("built_at")
    if not built_at:
        modified = datetime.fromtimestamp(signature[3] / 1e9, UTC)
        built_at = modified.isoformat(timespec="seconds")
    generation.built_at = built_at
    generation.loaded_at = datetime.now(UTC).isoformat(timespec="seconds")
    return generation


def warm(generation: Generation) -> None:
    """Read a main database once from end to end, in the background.

    Nothing is kept: the point is that the system has the file in memory
    before the first searches need it, instead of making them wait for the
    disk. It stops as soon as the file is replaced.

    Args:
        generation: The generation that starts being served.
    """
    try:
        with generation.path.open("rb", buffering=0) as handle:
            while not generation._retired and handle.read(WARM_CHUNK_BYTES):
                pass
    except OSError:
        pass


class Catalogue:
    """Gives connections to the main database currently being served.

    Attributes:
        path: Path of `food.sqlite`.
    """

    def __init__(self, path: Path, readers: int = 8, log: Logger = print) -> None:
        """Remember where the main database is; nothing is opened yet.

        Args:
            path: Path of `food.sqlite`.
            readers: Largest number of connections kept per file.
            log: Function receiving a line when the file served changes.
        """
        self.path = path
        self._readers = max(1, readers)
        self._log = log
        self._lock = threading.Lock()
        self._current: Generation | None = None
        self._rejected: tuple | None = None

    def current(self) -> Generation:
        """Return the generation to serve, opening a replaced file first.

        Returns:
            The generation being served.

        Raises:
            DatabaseUnavailable: When there is no file that can be served.
        """
        signature = file_signature(self.path)
        current = self._current
        if current is not None and (signature is None or signature == current.signature):
            return current
        with self._lock:
            current = self._current
            if (
                signature is not None
                and signature != self._rejected
                and (current is None or signature != current.signature)
            ):
                try:
                    fresh = read_generation(self.path, signature)
                except DatabaseRejected as error:
                    self._rejected = signature
                    self._log(f"not serving the new file: {error}")
                else:
                    fresh._slots = threading.Semaphore(self._readers)
                    threading.Thread(target=warm, args=(fresh,), daemon=True).start()
                    self._current, self._rejected = fresh, None
                    self._log(f"serving {self.path}: {fresh.foods:,} foods, built {fresh.built_at}")
                    if current is not None:
                        current.retire()
                    current = fresh
        if current is None:
            raise DatabaseUnavailable(f"{self.path} is missing or cannot be served")
        return current

    @contextlib.contextmanager
    def reader(self) -> Iterator[tuple[Generation, sqlite3.Connection]]:
        """Lend a connection to the main database for the time of a request.

        Yields:
            The generation being served and one of its connections.

        Raises:
            DatabaseUnavailable: When there is no file that can be served.
        """
        generation = self.current()
        generation._slots.acquire()
        try:
            try:
                connection = generation._idle.get_nowait()
            except queue.Empty:
                connection = generation.connect()
            try:
                yield generation, connection
            finally:
                if generation._retired:
                    connection.close()
                else:
                    generation._idle.put(connection)
        finally:
            generation._slots.release()

    def close(self) -> None:
        """Close the connections of the file being served."""
        with self._lock:
            if self._current is not None:
                self._current.retire()
                self._current = None


def _chunks(values: Iterable) -> Iterable[list]:
    """Split values into lists small enough for one query.

    Args:
        values: The values of an IN list.

    Yields:
        Lists of at most `CHUNK` distinct values.
    """
    unique = list(dict.fromkeys(values))
    for start in range(0, len(unique), CHUNK):
        yield unique[start : start + CHUNK]


def _select_in(connection: sqlite3.Connection, sql: str, values: Iterable) -> list[sqlite3.Row]:
    """Run a query once per chunk of an IN list.

    Args:
        connection: A connection to the main database.
        sql: The query, with `{marks}` where the placeholders go.
        values: The values of the IN list.

    Returns:
        Every row found.
    """
    rows: list[sqlite3.Row] = []
    for chunk in _chunks(values):
        marks = ", ".join("?" for _ in chunk)
        rows += connection.execute(sql.format(marks=marks), chunk).fetchall()
    return rows


def foods_by_pk(connection: sqlite3.Connection, pks: Iterable[int]) -> list[sqlite3.Row]:
    """Read foods by their internal number.

    Args:
        connection: A connection to the main database.
        pks: Internal numbers, as the full-text index returns them.

    Returns:
        The rows found.
    """
    return _select_in(connection, _FOOD_SELECT + " WHERE pk IN ({marks})", pks)


def foods_by_id(connection: sqlite3.Connection, food_ids: Iterable[str]) -> list[sqlite3.Row]:
    """Read foods by their public identifier.

    Args:
        connection: A connection to the main database.
        food_ids: Public identifiers.

    Returns:
        The rows found.
    """
    return _select_in(connection, _FOOD_SELECT + " WHERE id IN ({marks})", food_ids)


def food_by_barcode(connection: sqlite3.Connection, barcode: str) -> sqlite3.Row | None:
    """Read the food carrying a barcode.

    Args:
        connection: A connection to the main database.
        barcode: Canonical barcode.

    Returns:
        The row, or None.
    """
    return connection.execute(_FOOD_SELECT + " WHERE barcode = ?", (barcode,)).fetchone()


def names_by_pk(connection: sqlite3.Connection, pks: Iterable[int]) -> dict[int, dict]:
    """Read the names of foods.

    Args:
        connection: A connection to the main database.
        pks: Internal numbers of foods.

    Returns:
        For each food, its names by language code, each with its origin.
    """
    found: dict[int, dict[str, tuple[str, str]]] = {}
    sql = "SELECT food_pk, lang, name, origin FROM names WHERE food_pk IN ({marks})"
    for row in _select_in(connection, sql, pks):
        found.setdefault(row["food_pk"], {})[row["lang"]] = (row["name"], row["origin"])
    return found


def versions_by_pk(connection: sqlite3.Connection, pks: Iterable[int]) -> dict[int, list]:
    """Read the versions of the foods that have several.

    Args:
        connection: A connection to the main database.
        pks: Internal numbers of foods.

    Returns:
        For each food that has several versions, their rows in build order.
    """
    found: dict[int, list[sqlite3.Row]] = {}
    sql = "SELECT * FROM versions WHERE food_pk IN ({marks}) ORDER BY food_pk, position"
    for row in _select_in(connection, sql, pks):
        found.setdefault(row["food_pk"], []).append(row)
    return found


def food_of_version(connection: sqlite3.Connection, version_id: str) -> str | None:
    """Return the food a version of the main database belongs to.

    Args:
        connection: A connection to the main database.
        version_id: Identifier of a version listed in `versions`.

    Returns:
        The identifier of the food, or None when the version is not listed.
    """
    row = connection.execute(
        "SELECT f.id FROM versions AS v JOIN foods AS f ON f.pk = v.food_pk"
        " WHERE v.version_id = ? LIMIT 1",
        (version_id,),
    ).fetchone()
    return row[0] if row else None
