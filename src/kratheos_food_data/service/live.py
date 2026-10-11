"""The live database: what the service owns and writes to.

`live.sqlite` holds everything entered through the service since the first
day. It is the only place where it is written; `food.sqlite` is rebuilt from
it (through the public `kratheos.sqlite`) and never the other way round.

Tables that the weekly export publishes, once what moderation hid is left
out:

* `products`: the foods added, with their names and values;
* `versions`: the other values proposed for a food;
* `translations`: the names added to a food in a language it lacks.

Tables that never leave the server:

* `votes`: which contributor key voted for which version of which food, the
  key stored as a keyed hash (HMAC-SHA-256 with a secret of this
  installation). It only serves to count a person once and to let them move
  their vote. The export publishes counts, never this table;
* `anonymous_votes`: counts taken over from a `kratheos.sqlite` written
  elsewhere, for which no key is known: they count, and nobody can move them;
* `reports`: what people reported, with the note they wrote: one open report
  per person and per food, replaced when the person reports the food again;
* `hidden`: the identifiers moderation hid (a food or one version);
* `history`: one line per accepted write (what, on which record, by which
  hashed key). It is the only link between a product, a version or a name
  and the key that wrote it: the tables that are exported carry no author;
* `blocked`: the hashed keys moderation blocked. Their later writes are
  refused, their votes are no longer counted and their reports are left out
  of the moderation lists, for as long as the key is there;
* `withdrawn`: what a blocked key wrote (its products, its versions, the
  names it added that are still the ones shown), found through `history`
  when the key was blocked. A withdrawn record is hidden like one moderation
  hid, and shown again when the key is unblocked: `hidden` is never touched,
  so that undoing a block restores exactly what was there before it.

`names_fts` indexes the names of the products added and the translations, so
that a search finds them at once.

The file is in WAL mode: readers never wait for a writer. Writes are short
and go through one connection, one at a time.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import threading
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path

from .. import kratheos
from ..barcode import normalize_gtin
from ..schema import KIND_PRODUCT
from ..sqlite_export import FTS_PREFIXES, FTS_TOKENIZER

SCHEMA_VERSION = 2
CHUNK = 500
BUSY_TIMEOUT_MS = 10_000
REPORT_REASONS = ("wrong_values", "duplicate", "inappropriate", "other")
REPORT_OPEN = "open"
REPORT_CLOSED = "closed"
GTIN_PREFIX = "gtin:"
# Actions of the history that are the writes of a person.
ACTION_CREATE_PRODUCT = "create_product"
ACTION_ADD_VERSION = "add_version"
ACTION_VOTE = "vote"
ACTION_TRANSLATE = "translate"
WRITE_ACTIONS = (ACTION_CREATE_PRODUCT, ACTION_ADD_VERSION, ACTION_VOTE, ACTION_TRANSLATE)
# Largest number of rows of each kind listed for one contributor key.
CONTRIBUTOR_ROWS = 500
RECORD_PREFIX = f"{kratheos.SOURCE_ID}:"
# Condition leaving out the rows of `votes` or `reports` written by a blocked key.
_NOT_BLOCKED = "contributor NOT IN (SELECT contributor FROM blocked)"

NUTRIENTS = tuple(name for name, _ in kratheos.NUTRIENT_COLUMNS)
SERVING = tuple(name for name, _ in kratheos.SERVING_COLUMNS)
VERSION_VALUES = ("basis", *NUTRIENTS, *SERVING)
PRODUCT_VALUES = tuple(name for name, _ in kratheos.VALUE_COLUMNS)

_VALUE_DDL = ",\n    ".join(f"{name} {kind}" for name, kind in kratheos.VALUE_COLUMNS)
_VERSION_DDL = ",\n    ".join(f"{name} {kind}" for name, kind in kratheos.VERSION_VALUE_COLUMNS)

DDL = f"""
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
) WITHOUT ROWID;

CREATE TABLE products (
    uuid TEXT PRIMARY KEY,           -- its record is kratheos:<uuid>
    food_id TEXT NOT NULL UNIQUE,    -- gtin:<barcode>, or kratheos:<uuid> without barcode
    barcode TEXT UNIQUE,
    kind TEXT NOT NULL DEFAULT 'product',
    name TEXT NOT NULL,
    name_lang TEXT NOT NULL,
    names TEXT NOT NULL,             -- JSON object: every name by language code
    {_VALUE_DDL},
    countries TEXT NOT NULL,         -- JSON array
    updated_at TEXT NOT NULL
);

CREATE TABLE versions (
    version_id TEXT PRIMARY KEY,     -- kratheos:v:<uuid>
    food_id TEXT NOT NULL,
    {_VERSION_DDL},
    countries TEXT NOT NULL,         -- JSON array
    updated_at TEXT NOT NULL
);
CREATE INDEX versions_food ON versions (food_id);

CREATE TABLE translations (
    food_id TEXT NOT NULL,
    lang TEXT NOT NULL,
    name TEXT NOT NULL,
    origin TEXT NOT NULL DEFAULT 'human',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (food_id, lang)
) WITHOUT ROWID;

-- Never exported: one vote per contributor key and per food.
CREATE TABLE votes (
    food_id TEXT NOT NULL,
    contributor TEXT NOT NULL,       -- keyed hash of the contributor key
    version_id TEXT NOT NULL,
    voted_at TEXT NOT NULL,
    PRIMARY KEY (food_id, contributor)
) WITHOUT ROWID;
CREATE INDEX votes_version ON votes (version_id);

-- Counts taken over from a layer written elsewhere: no key is known.
CREATE TABLE anonymous_votes (
    version_id TEXT PRIMARY KEY,
    food_id TEXT NOT NULL,
    votes INTEGER NOT NULL,
    updated_at TEXT NOT NULL
) WITHOUT ROWID;
CREATE INDEX anonymous_votes_food ON anonymous_votes (food_id);

-- Never exported.
CREATE TABLE reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    food_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    note TEXT NOT NULL,
    contributor TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL,
    closed_at TEXT
);
CREATE INDEX reports_state ON reports (state, id);
CREATE INDEX reports_food ON reports (food_id, state);

-- Never exported: what is hidden is simply absent from the export.
CREATE TABLE hidden (
    record_id TEXT PRIMARY KEY,
    hidden_at TEXT NOT NULL
) WITHOUT ROWID;

-- Never exported.
CREATE TABLE history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    action TEXT NOT NULL,
    record_id TEXT NOT NULL,
    contributor TEXT,
    detail TEXT
);
CREATE INDEX history_record ON history (record_id);
CREATE INDEX history_contributor ON history (contributor);

-- Never exported: the contributor keys moderation blocked.
CREATE TABLE blocked (
    contributor TEXT PRIMARY KEY,    -- keyed hash of the contributor key
    blocked_at TEXT NOT NULL
) WITHOUT ROWID;

-- Never exported: what a blocked key wrote, hidden for as long as it is blocked.
CREATE TABLE withdrawn (
    record_id TEXT NOT NULL,         -- a product record or a version; the food, for a name
    lang TEXT NOT NULL DEFAULT '',   -- language of a name added, empty for a record
    contributor TEXT NOT NULL,       -- keyed hash of the key that wrote it
    withdrawn_at TEXT NOT NULL,
    PRIMARY KEY (record_id, lang)
) WITHOUT ROWID;
CREATE INDEX withdrawn_contributor ON withdrawn (contributor);

CREATE VIRTUAL TABLE names_fts USING fts5(
    food_id UNINDEXED, names, brand, tokenize = '{FTS_TOKENIZER}', prefix = '{FTS_PREFIXES}'
);
"""

# What each layout lacks to be the next one, applied in one transaction when
# a live database written by an earlier version is opened. Nothing that is
# stored is changed: layout 2 only adds tables and indexes.
UPGRADES = {
    1: """
CREATE INDEX IF NOT EXISTS reports_food ON reports (food_id, state);
CREATE INDEX IF NOT EXISTS history_record ON history (record_id);
CREATE INDEX IF NOT EXISTS history_contributor ON history (contributor);
CREATE TABLE IF NOT EXISTS blocked (
    contributor TEXT PRIMARY KEY,
    blocked_at TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS withdrawn (
    record_id TEXT NOT NULL,
    lang TEXT NOT NULL DEFAULT '',
    contributor TEXT NOT NULL,
    withdrawn_at TEXT NOT NULL,
    PRIMARY KEY (record_id, lang)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS withdrawn_contributor ON withdrawn (contributor);
""",
}


class Conflict(Exception):
    """A write met something that is already there.

    Attributes:
        what: What exists: `barcode` or `food`.
    """

    def __init__(self, what: str) -> None:
        """Remember what exists."""
        super().__init__(what)
        self.what = what


def utc_now() -> str:
    """Return the current UTC time, to the second, ISO 8601.

    Returns:
        A text like `2026-10-11T14:03:22Z`.
    """
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def food_id_of_product(uuid: str, kind: str, barcode: str | None) -> str:
    """Return the identifier of the food a product added in Kratheos is.

    Args:
        uuid: Identifier of the product.
        kind: `product` or `generic`.
        barcode: Canonical barcode, or None.

    Returns:
        `gtin:<barcode>` for a product with a barcode, else `kratheos:<uuid>`,
        as the build names it.
    """
    if kind == KIND_PRODUCT and barcode:
        return GTIN_PREFIX + barcode
    return RECORD_PREFIX + uuid


def _chunks(values: Iterable[str]) -> Iterable[list[str]]:
    """Split identifiers into lists small enough for one query.

    Args:
        values: The identifiers.

    Yields:
        Lists of at most `CHUNK` distinct identifiers.
    """
    unique = list(dict.fromkeys(values))
    for start in range(0, len(unique), CHUNK):
        yield unique[start : start + CHUNK]


def _select_in(
    connection: sqlite3.Connection, sql: str, values: Iterable[str]
) -> list[sqlite3.Row]:
    """Run a query once per chunk of an IN list.

    Args:
        connection: A connection to the live database.
        sql: The query, with `{marks}` where the placeholders go.
        values: The identifiers of the IN list.

    Returns:
        Every row found.
    """
    rows: list[sqlite3.Row] = []
    for chunk in _chunks(values):
        marks = ", ".join("?" for _ in chunk)
        rows += connection.execute(sql.format(marks=marks), chunk).fetchall()
    return rows


def products_by_food(connection: sqlite3.Connection, food_ids: Iterable[str]) -> dict:
    """Read the products added in Kratheos that are the given foods.

    Args:
        connection: A connection to the live database.
        food_ids: Identifiers of foods.

    Returns:
        The product rows, by food identifier.
    """
    rows = _select_in(connection, "SELECT * FROM products WHERE food_id IN ({marks})", food_ids)
    return {row["food_id"]: row for row in rows}


def product_by_barcode(connection: sqlite3.Connection, barcode: str) -> sqlite3.Row | None:
    """Read the product added in Kratheos under a barcode.

    Args:
        connection: A connection to the live database.
        barcode: Canonical barcode.

    Returns:
        The product row, or None.
    """
    return connection.execute("SELECT * FROM products WHERE barcode = ?", (barcode,)).fetchone()


def versions_by_food(connection: sqlite3.Connection, food_ids: Iterable[str]) -> dict:
    """Read the versions proposed for the given foods.

    Args:
        connection: A connection to the live database.
        food_ids: Identifiers of foods.

    Returns:
        The version rows of each food that has some, by food identifier.
    """
    found: dict[str, list[sqlite3.Row]] = {}
    sql = "SELECT * FROM versions WHERE food_id IN ({marks}) ORDER BY version_id"
    for row in _select_in(connection, sql, food_ids):
        found.setdefault(row["food_id"], []).append(row)
    return found


def version_owner(connection: sqlite3.Connection, version_id: str) -> str | None:
    """Return the food a proposed version or an added product belongs to.

    Args:
        connection: A connection to the live database.
        version_id: `kratheos:v:<uuid>` or `kratheos:<uuid>`.

    Returns:
        The identifier of the food, or None when the live database does not
        know the version.
    """
    row = connection.execute(
        "SELECT food_id FROM versions WHERE version_id = ?", (version_id,)
    ).fetchone()
    if row is None and version_id.startswith(RECORD_PREFIX):
        row = connection.execute(
            "SELECT food_id FROM products WHERE uuid = ?", (version_id[len(RECORD_PREFIX) :],)
        ).fetchone()
    return row["food_id"] if row else None


def vote_counts(connection: sqlite3.Connection, food_ids: Iterable[str]) -> dict:
    """Count the votes of the versions of the given foods.

    Args:
        connection: A connection to the live database.
        food_ids: Identifiers of foods.

    Returns:
        For each food the live database holds votes of, the number of votes
        by version identifier. A food it holds none of is absent: its votes
        are those the main database already carries. The votes of a blocked
        key are not counted, and their food stays one the live database
        holds the votes of: a vote an earlier build folded into the main
        database does not come back through it.
    """
    counts: dict[str, dict[str, int]] = {}
    cast = (
        f"SELECT food_id, version_id, sum({_NOT_BLOCKED}) AS votes FROM votes"
        " WHERE food_id IN ({marks}) GROUP BY food_id, version_id"
    )
    taken_over = "SELECT food_id, version_id, votes FROM anonymous_votes WHERE food_id IN ({marks})"
    food_ids = list(food_ids)
    for sql in (cast, taken_over):
        for row in _select_in(connection, sql, food_ids):
            food = counts.setdefault(row["food_id"], {})
            food[row["version_id"]] = food.get(row["version_id"], 0) + row["votes"]
    return counts


def translations_by_food(connection: sqlite3.Connection, food_ids: Iterable[str]) -> dict:
    """Read the names added to the given foods.

    Args:
        connection: A connection to the live database.
        food_ids: Identifiers of foods.

    Returns:
        For each food that has some, the names by language code. A name
        written by a blocked key is left out.
    """
    found: dict[str, dict[str, str]] = {}
    sql = (
        "SELECT food_id, lang, name FROM translations WHERE food_id IN ({marks})"
        " AND NOT EXISTS (SELECT 1 FROM withdrawn"
        " WHERE record_id = translations.food_id AND withdrawn.lang = translations.lang)"
    )
    for row in _select_in(connection, sql, food_ids):
        found.setdefault(row["food_id"], {})[row["lang"]] = row["name"]
    return found


def hidden_among(connection: sqlite3.Connection, record_ids: Iterable[str]) -> set[str]:
    """Tell which of the given identifiers moderation hid.

    Args:
        connection: A connection to the live database.
        record_ids: Identifiers of foods and versions.

    Returns:
        Those that are hidden: one by one, or with everything a blocked key
        wrote.
    """
    sql = (
        "SELECT record_id FROM hidden WHERE record_id IN ({marks})"
        " UNION SELECT record_id FROM withdrawn WHERE lang = '' AND record_id IN ({marks})"
    )
    found: set[str] = set()
    for chunk in _chunks(record_ids):
        marks = ", ".join("?" for _ in chunk)
        found |= {row[0] for row in connection.execute(sql.format(marks=marks), [*chunk, *chunk])}
    return found


def blocked_at(connection: sqlite3.Connection, contributor: str) -> str | None:
    """Tell when moderation blocked a contributor key.

    Args:
        connection: A connection to the live database.
        contributor: Keyed hash of the contributor key.

    Returns:
        The time of the block, or None when the key is not blocked.
    """
    row = connection.execute(
        "SELECT blocked_at FROM blocked WHERE contributor = ?", (contributor,)
    ).fetchone()
    return row["blocked_at"] if row else None


def search(connection: sqlite3.Connection, match: str, limit: int) -> list[str]:
    """Find the foods whose added names or brand match a full-text query.

    Args:
        connection: A connection to the live database.
        match: An FTS5 expression.
        limit: Largest number of foods returned.

    Returns:
        The identifiers of the foods, best match first.
    """
    try:
        rows = connection.execute(
            "SELECT food_id FROM names_fts WHERE names_fts MATCH ? ORDER BY rank LIMIT ?",
            (match, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [row["food_id"] for row in rows]


def reports(
    connection: sqlite3.Connection, state: str | None, limit: int, offset: int
) -> list[sqlite3.Row]:
    """List the reports, oldest first.

    Args:
        connection: A connection to the live database.
        state: `open`, `closed`, or None for both.
        limit: Largest number of reports returned.
        offset: Number of reports skipped.

    Returns:
        The report rows, without the key of who reported. The reports of a
        blocked key are left out.
    """
    where = "" if state is None else "AND state = ?"
    parameters: list = [] if state is None else [state]
    return connection.execute(
        "SELECT id, food_id, reason, note, state, created_at, closed_at FROM reports"
        f" WHERE {_NOT_BLOCKED} {where} ORDER BY id LIMIT ? OFFSET ?",
        [*parameters, limit, offset],
    ).fetchall()


def _reasons(rows: Iterable[sqlite3.Row]) -> dict[str, int]:
    """Count the different people behind each reason of a list of reports.

    Args:
        rows: Report rows of one food, with the hashed key of who reported.

    Returns:
        The number of different people by reason, the most given first.
    """
    people: dict[str, set[str]] = {}
    for row in rows:
        people.setdefault(row["reason"], set()).add(row["contributor"])
    ordered = sorted(people.items(), key=lambda item: (-len(item[1]), item[0]))
    return {reason: len(keys) for reason, keys in ordered}


def reported_foods(
    connection: sqlite3.Connection, state: str | None, limit: int, offset: int
) -> list[dict]:
    """List the foods people reported, the most reported first.

    Args:
        connection: A connection to the live database.
        state: `open`, `closed`, or None for both: which reports count.
        limit: Largest number of foods returned.
        offset: Number of foods skipped.

    Returns:
        One object per food: `food_id`, `reporters` (how many different
        people reported it), `reasons` (how many of them for each reason,
        the most given first), `first_reported_at`, `last_reported_at` and
        `reports` (number, reason, note, state and dates of each report,
        oldest first). Who reported is never part of it, and the reports of
        a blocked key are left out.
    """
    where = "" if state is None else "AND state = ?"
    parameters: list = [] if state is None else [state]
    foods = connection.execute(
        "SELECT food_id, count(DISTINCT contributor) AS reporters, min(created_at) AS first_at,"
        f" max(created_at) AS last_at FROM reports WHERE {_NOT_BLOCKED} {where}"
        " GROUP BY food_id ORDER BY reporters DESC, last_at DESC, food_id LIMIT ? OFFSET ?",
        [*parameters, limit, offset],
    ).fetchall()
    listed: dict[str, list[sqlite3.Row]] = {}
    for chunk in _chunks(row["food_id"] for row in foods):
        marks = ", ".join("?" for _ in chunk)
        rows = connection.execute(
            "SELECT id, food_id, reason, note, state, created_at, closed_at, contributor"
            f" FROM reports WHERE food_id IN ({marks}) AND {_NOT_BLOCKED} {where} ORDER BY id",
            [*chunk, *parameters],
        )
        for row in rows:
            listed.setdefault(row["food_id"], []).append(row)
    return [
        {
            "food_id": food["food_id"],
            "reporters": food["reporters"],
            "reasons": _reasons(listed.get(food["food_id"], ())),
            "first_reported_at": food["first_at"],
            "last_reported_at": food["last_at"],
            "reports": [
                {
                    "id": row["id"],
                    "reason": row["reason"],
                    "note": row["note"],
                    "state": row["state"],
                    "created_at": row["created_at"],
                    "closed_at": row["closed_at"],
                }
                for row in listed.get(food["food_id"], ())
            ],
        }
        for food in foods
    ]


def open_reports_of(connection: sqlite3.Connection, food_id: str) -> dict:
    """Count the people who have an open report on a food, and their reasons.

    Args:
        connection: A connection to the live database.
        food_id: Identifier of the food.

    Returns:
        `reporters`, the number of different people, and `reasons`, how many
        of them for each reason. The reports of a blocked key are left out.
    """
    rows = connection.execute(
        f"SELECT reason, contributor FROM reports WHERE food_id = ? AND state = ?"
        f" AND {_NOT_BLOCKED}",
        (food_id, REPORT_OPEN),
    ).fetchall()
    return {"reporters": len({row["contributor"] for row in rows}), "reasons": _reasons(rows)}


def record_contributors(connection: sqlite3.Connection, record_id: str) -> list[dict]:
    """List the hashed keys that wrote a record, from the history.

    Args:
        connection: A connection to the live database.
        record_id: Identifier of a food or of a version. For the record of a
            product added in Kratheos (`kratheos:<uuid>`), whoever created
            the product is listed too, even when the food is named after its
            barcode.

    Returns:
        One object per key, in the order of their first write: `contributor`
        (the keyed hash, never the key), `actions` (what it did on this
        record), `first_at` and `last_at`.
    """
    marks = ", ".join("?" for _ in WRITE_ACTIONS)
    rows = connection.execute(
        "SELECT id, at, action, contributor FROM history WHERE record_id = ?"
        f" AND contributor IS NOT NULL AND action IN ({marks})",
        (record_id, *WRITE_ACTIONS),
    ).fetchall()
    if record_id.startswith(RECORD_PREFIX):
        product = connection.execute(
            "SELECT food_id FROM products WHERE uuid = ?", (record_id[len(RECORD_PREFIX) :],)
        ).fetchone()
        if product is not None and product["food_id"] != record_id:
            rows += connection.execute(
                "SELECT id, at, action, contributor FROM history WHERE record_id = ?"
                " AND contributor IS NOT NULL AND action = ?",
                (product["food_id"], ACTION_CREATE_PRODUCT),
            ).fetchall()
    found: dict[str, dict] = {}
    for row in sorted(rows, key=lambda row: row["id"]):
        entry = found.setdefault(
            row["contributor"],
            {"contributor": row["contributor"], "actions": [], "first_at": row["at"]},
        )
        if row["action"] not in entry["actions"]:
            entry["actions"].append(row["action"])
        entry["last_at"] = row["at"]
    return list(found.values())


def _latest_names(
    connection: sqlite3.Connection, food_ids: Iterable[str]
) -> dict[tuple[str, str], sqlite3.Row]:
    """Find, for each name added to the given foods, the write that gave it last.

    Args:
        connection: A connection to the live database.
        food_ids: Identifiers of foods.

    Returns:
        The latest `translate` line of the history, by food and language.
    """
    latest: dict[tuple[str, str], sqlite3.Row] = {}
    sql = (
        "SELECT id, at, record_id, contributor, detail FROM history"
        " WHERE action = 'translate' AND record_id IN ({marks})"
    )
    for row in sorted(_select_in(connection, sql, food_ids), key=lambda row: row["id"]):
        latest[(row["record_id"], json.loads(row["detail"])["lang"])] = row
    return latest


def written_by(connection: sqlite3.Connection, contributor: str) -> dict:
    """Read everything a contributor key wrote, the history telling what is whose.

    Args:
        connection: A connection to the live database.
        contributor: Keyed hash of the contributor key.

    Returns:
        `products` (`food_id`, `record_id`, `name`, `at`), `versions`
        (`version_id`, `food_id`, `at`), `translations` (`food_id`,
        `language`, `name`, `at`, and `current`: whether it is still the
        name of the food in that language), `votes` (`food_id`,
        `version_id`, `at`) and `reports` (`id`, `food_id`, `reason`,
        `note`, `state`, `at`), each most recent first.
    """
    lines = connection.execute(
        "SELECT id, at, action, record_id, detail FROM history WHERE contributor = ?"
        " AND action IN (?, ?, ?) ORDER BY id DESC",
        (contributor, ACTION_CREATE_PRODUCT, ACTION_ADD_VERSION, ACTION_TRANSLATE),
    ).fetchall()
    products, versions, names = [], [], []
    for line in lines:
        detail = json.loads(line["detail"])
        if line["action"] == ACTION_CREATE_PRODUCT:
            products.append(
                {
                    "food_id": line["record_id"],
                    "record_id": RECORD_PREFIX + detail["uuid"],
                    "name": detail.get("name"),
                    "at": line["at"],
                }
            )
        elif line["action"] == ACTION_ADD_VERSION:
            versions.append(
                {"version_id": line["record_id"], "food_id": detail["food_id"], "at": line["at"]}
            )
        else:
            names.append(
                (line["id"], line["record_id"], detail["lang"], detail["name"], line["at"])
            )
    latest = _latest_names(connection, (name[1] for name in names))
    translations = [
        {
            "food_id": food_id,
            "language": lang,
            "name": name,
            "at": at,
            "current": latest[(food_id, lang)]["id"] == line_id,
        }
        for line_id, food_id, lang, name, at in names
    ]
    votes = [
        {"food_id": row["food_id"], "version_id": row["version_id"], "at": row["voted_at"]}
        for row in connection.execute(
            "SELECT food_id, version_id, voted_at FROM votes WHERE contributor = ?"
            " ORDER BY voted_at DESC, food_id",
            (contributor,),
        )
    ]
    reported = [
        {
            "id": row["id"],
            "food_id": row["food_id"],
            "reason": row["reason"],
            "note": row["note"],
            "state": row["state"],
            "at": row["created_at"],
        }
        for row in connection.execute(
            "SELECT id, food_id, reason, note, state, created_at FROM reports"
            " WHERE contributor = ? ORDER BY id DESC",
            (contributor,),
        )
    ]
    return {
        "products": products,
        "versions": versions,
        "translations": translations,
        "votes": votes,
        "reports": reported,
    }


def counts(connection: sqlite3.Connection) -> dict[str, int]:
    """Count what the live database holds.

    Args:
        connection: A connection to the live database.

    Returns:
        The number of products, versions, translations and votes.
    """
    row = connection.execute(
        """
        SELECT (SELECT count(*) FROM products),
               (SELECT count(*) FROM versions),
               (SELECT count(*) FROM translations),
               (SELECT count(*) FROM votes)
               + (SELECT coalesce(sum(votes), 0) FROM anonymous_votes)
        """
    ).fetchone()
    return dict(zip(("products", "versions", "translations", "votes"), row, strict=True))


class LiveStore:
    """The live database of the service.

    Attributes:
        path: Path of `live.sqlite`.
        created: Whether this object just created the file.
    """

    def __init__(self, path: Path, key_secret: bytes, clock: Callable[[], str] = utc_now) -> None:
        """Open the live database, creating it when it does not exist.

        Args:
            path: Path of `live.sqlite`.
            key_secret: Secret the contributor keys are hashed with.
            clock: Function giving the current UTC time as text.

        Raises:
            ValueError: When the file is not a live database this code reads.
        """
        self.path = path
        self._secret = key_secret
        self._clock = clock
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.created = not path.exists()
        if self.created:
            # The file holds hashed keys and reports: its owner only may read
            # it, and SQLite gives the same rights to its -wal and -shm files.
            os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        self._writer = self._connect()
        self._writer.execute("PRAGMA journal_mode = WAL")
        self._writer.execute("PRAGMA synchronous = NORMAL")
        if self.created:
            self._writer.executescript("BEGIN;" + DDL + "COMMIT;")
            with self._lock, self._writer:
                self._writer.executemany(
                    "INSERT INTO meta (key, value) VALUES (?, ?)",
                    [("schema_version", str(SCHEMA_VERSION)), ("created_at", self._clock())],
                )
        try:
            row = self._writer.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
        except sqlite3.DatabaseError as error:
            raise ValueError(f"{path} is not a live database") from error
        row = self._upgrade(row)
        if row is None or row[0] != str(SCHEMA_VERSION):
            raise ValueError(
                f"{path}: live database of layout {row and row[0]}, expected {SCHEMA_VERSION}"
            )

    def _upgrade(self, row: tuple | None) -> tuple | None:
        """Bring a live database written by an earlier version to this layout.

        Each step adds what the next layout has and changes nothing that is
        stored, in one transaction with the new layout number.

        Args:
            row: The row of `meta` holding the layout number, if any.

        Returns:
            The row as it is after the upgrade.
        """
        while row is not None and row[0].isdigit() and int(row[0]) in UPGRADES:
            layout = int(row[0])
            with self._lock:
                self._writer.executescript(
                    "BEGIN;"
                    + UPGRADES[layout]
                    + f"UPDATE meta SET value = '{layout + 1}' WHERE key = 'schema_version';"
                    + "COMMIT;"
                )
            row = (str(layout + 1),)
        return row

    def _connect(self) -> sqlite3.Connection:
        """Open a connection to the live database.

        Returns:
            A connection whose rows read by column name, usable from any
            thread as long as one thread uses it at a time.
        """
        connection = sqlite3.connect(
            self.path, timeout=BUSY_TIMEOUT_MS / 1000, check_same_thread=False
        )
        connection.row_factory = sqlite3.Row
        return connection

    def reader(self) -> sqlite3.Connection:
        """Open a connection that only reads.

        Returns:
            The connection; whoever asks for it closes it.
        """
        connection = self._connect()
        connection.execute("PRAGMA query_only = 1")
        return connection

    def close(self) -> None:
        """Close the writing connection."""
        with self._lock:
            self._writer.close()

    def contributor_hash(self, key: str) -> str:
        """Hash a contributor key with the secret of this installation.

        Args:
            key: The opaque key the backend sends.

        Returns:
            The hexadecimal HMAC-SHA-256 of the key; the key itself is never
            stored.
        """
        return hmac.new(self._secret, key.encode("utf-8"), hashlib.sha256).hexdigest()

    def _log(
        self, action: str, record_id: str, contributor: str | None, detail: dict | None = None
    ) -> None:
        """Add a line to the history, inside the current transaction.

        Args:
            action: What was done.
            record_id: On which food or version.
            contributor: Hashed key of who did it, if anybody.
            detail: What was written, as JSON.
        """
        self._writer.execute(
            "INSERT INTO history (at, action, record_id, contributor, detail)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                self._clock(),
                action,
                record_id,
                contributor,
                None if detail is None else json.dumps(detail, ensure_ascii=False, sort_keys=True),
            ),
        )

    def _refresh_search(self, food_id: str) -> None:
        """Index again the added names of a food, inside the current transaction.

        Args:
            food_id: Identifier of the food.
        """
        self._writer.execute("DELETE FROM names_fts WHERE food_id = ?", (food_id,))
        product = self._writer.execute(
            "SELECT names, brand FROM products WHERE food_id = ?", (food_id,)
        ).fetchone()
        names = set(json.loads(product["names"]).values()) if product else set()
        names |= {
            row["name"]
            for row in self._writer.execute(
                "SELECT name FROM translations WHERE food_id = ? AND lang NOT IN"
                " (SELECT lang FROM withdrawn WHERE record_id = ?)",
                (food_id, food_id),
            )
        }
        if names:
            self._writer.execute(
                "INSERT INTO names_fts (food_id, names, brand) VALUES (?, ?, ?)",
                (food_id, " | ".join(sorted(names)), product["brand"] if product else None),
            )

    def _vote(self, food_id: str, version_id: str, contributor: str) -> None:
        """Give or move a vote, inside the current transaction.

        Args:
            food_id: Identifier of the food.
            version_id: Version voted for.
            contributor: Hashed key of the voter.
        """
        self._writer.execute(
            "INSERT INTO votes (food_id, contributor, version_id, voted_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT (food_id, contributor) DO UPDATE"
            " SET version_id = excluded.version_id, voted_at = excluded.voted_at"
            " WHERE version_id <> excluded.version_id",
            (food_id, contributor, version_id, self._clock()),
        )

    def create_product(self, uuid: str, fields: dict, contributor: str) -> str:
        """Add a product, with the vote of whoever adds it.

        Args:
            uuid: Identifier given to the product.
            fields: `kind`, `barcode`, `name`, `name_lang`, `names` (every
                name by language), the value columns and `countries`.
            contributor: The contributor key.

        Returns:
            The identifier of the food.

        Raises:
            Conflict: When a product already has this barcode.
        """
        author = self.contributor_hash(contributor)
        food_id = food_id_of_product(uuid, fields["kind"], fields["barcode"])
        columns = ("kind", "barcode", "name", "name_lang", *PRODUCT_VALUES)
        with self._lock, self._writer:
            try:
                self._writer.execute(
                    f"INSERT INTO products (uuid, food_id, {', '.join(columns)}, names,"
                    f" countries, updated_at) VALUES ({', '.join('?' for _ in columns)}, ?, ?,"
                    " ?, ?, ?)",
                    (
                        uuid,
                        food_id,
                        *(fields[name] for name in columns),
                        json.dumps(fields["names"], ensure_ascii=False, sort_keys=True),
                        json.dumps(fields["countries"]),
                        self._clock(),
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise Conflict("barcode") from error
            self._vote(food_id, RECORD_PREFIX + uuid, author)
            self._refresh_search(food_id)
            self._log("create_product", food_id, author, {"uuid": uuid, **fields})
        return food_id

    def add_version(self, version_id: str, food_id: str, fields: dict, contributor: str) -> None:
        """Add a version of the values of a food, with the vote of its author.

        Args:
            version_id: Identifier given to the version.
            food_id: Identifier of the food.
            fields: `basis`, the nutrients, the portion and `countries`.
            contributor: The contributor key.
        """
        author = self.contributor_hash(contributor)
        with self._lock, self._writer:
            self._writer.execute(
                f"INSERT INTO versions (version_id, food_id, {', '.join(VERSION_VALUES)},"
                f" countries, updated_at) VALUES (?, ?, {', '.join('?' for _ in VERSION_VALUES)},"
                " ?, ?)",
                (
                    version_id,
                    food_id,
                    *(fields[name] for name in VERSION_VALUES),
                    json.dumps(fields["countries"]),
                    self._clock(),
                ),
            )
            self._vote(food_id, version_id, author)
            self._log("add_version", version_id, author, {"food_id": food_id, **fields})

    def vote(self, food_id: str, version_id: str, contributor: str) -> None:
        """Give the vote of a person to a version, moving it if they had voted.

        Args:
            food_id: Identifier of the food.
            version_id: Version voted for.
            contributor: The contributor key.
        """
        voter = self.contributor_hash(contributor)
        with self._lock, self._writer:
            self._vote(food_id, version_id, voter)
            self._log("vote", version_id, voter, {"food_id": food_id})

    def translate(self, food_id: str, lang: str, name: str, contributor: str) -> None:
        """Give a food a name in a language, replacing one added before.

        Args:
            food_id: Identifier of the food.
            lang: Language code.
            name: The name.
            contributor: The contributor key.
        """
        author = self.contributor_hash(contributor)
        with self._lock, self._writer:
            self._writer.execute(
                "INSERT INTO translations (food_id, lang, name, origin, updated_at)"
                " VALUES (?, ?, ?, 'human', ?) ON CONFLICT (food_id, lang) DO UPDATE"
                " SET name = excluded.name, origin = 'human', updated_at = excluded.updated_at",
                (food_id, lang, name, self._clock()),
            )
            # The name of a blocked key that this one replaces is gone for good.
            self._writer.execute(
                "DELETE FROM withdrawn WHERE record_id = ? AND lang = ?", (food_id, lang)
            )
            self._refresh_search(food_id)
            self._log(ACTION_TRANSLATE, food_id, author, {"lang": lang, "name": name})

    def report(self, food_id: str, reason: str, note: str, contributor: str) -> tuple[int, bool]:
        """Record what a person reports about a food.

        A person has one open report per food: reporting the food again
        replaces the reason, the note and the date of the earlier one, which
        keeps its number.

        Args:
            food_id: Identifier of the food.
            reason: One of `REPORT_REASONS`.
            note: What the person wrote.
            contributor: The contributor key.

        Returns:
            The number of the report, and whether it replaced an earlier one.
        """
        author = self.contributor_hash(contributor)
        with self._lock, self._writer:
            earlier = self._writer.execute(
                "SELECT id FROM reports WHERE food_id = ? AND contributor = ? AND state = ?"
                " ORDER BY id DESC LIMIT 1",
                (food_id, author, REPORT_OPEN),
            ).fetchone()
            if earlier is not None:
                self._writer.execute(
                    "UPDATE reports SET reason = ?, note = ?, created_at = ? WHERE id = ?",
                    (reason, note, self._clock(), earlier["id"]),
                )
                return earlier["id"], True
            cursor = self._writer.execute(
                "INSERT INTO reports (food_id, reason, note, contributor, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (food_id, reason, note, author, self._clock()),
            )
            return cursor.lastrowid, False

    def close_reports_of(self, food_id: str) -> int:
        """Close every open report about a food.

        Args:
            food_id: Identifier of the food.

        Returns:
            The number of reports closed.
        """
        with self._lock, self._writer:
            cursor = self._writer.execute(
                "UPDATE reports SET state = ?, closed_at = ? WHERE food_id = ? AND state = ?",
                (REPORT_CLOSED, self._clock(), food_id, REPORT_OPEN),
            )
            if cursor.rowcount:
                self._log("close_reports", food_id, None, {"reports": cursor.rowcount})
            return cursor.rowcount

    def _written_now(self, author: str) -> tuple[list[str], list[tuple[str, str]]]:
        """Find what a key wrote that is in the tables today, inside a transaction.

        Args:
            author: Keyed hash of the contributor key.

        Returns:
            The identifiers of its product records and of its versions, and
            the names it added (food and language) that nobody replaced
            since.
        """
        written = written_by(self._writer, author)
        records = [product["record_id"] for product in written["products"]]
        records += [version["version_id"] for version in written["versions"]]
        names = [
            (name["food_id"], name["language"])
            for name in written["translations"]
            if name["current"]
        ]
        return list(dict.fromkeys(records)), list(dict.fromkeys(names))

    def block(self, contributor: str) -> dict:
        """Hide everything a contributor key wrote, and refuse its later writes.

        Its products, its versions and the names it added (those nobody
        replaced since) are withdrawn: hidden like a record moderation hid,
        without touching what moderation hid one by one. Its votes stop
        counting and its reports are left out of the moderation lists, for
        as long as it is blocked. Blocking a key that already is changes
        nothing.

        Args:
            contributor: The contributor key.

        Returns:
            `contributor` (the keyed hash), `blocked_at`, and `withdrawn`:
            how many products, versions and names are hidden with the key.
        """
        author = self.contributor_hash(contributor)
        with self._lock, self._writer:
            now = self._clock()
            cursor = self._writer.execute(
                "INSERT OR IGNORE INTO blocked (contributor, blocked_at) VALUES (?, ?)",
                (author, now),
            )
            if cursor.rowcount:
                records, names = self._written_now(author)
                self._writer.executemany(
                    "INSERT OR IGNORE INTO withdrawn (record_id, lang, contributor, withdrawn_at)"
                    " VALUES (?, ?, ?, ?)",
                    [(record_id, "", author, now) for record_id in records]
                    + [(food_id, lang, author, now) for food_id, lang in names],
                )
                for food_id in sorted({food_id for food_id, _ in names}):
                    self._refresh_search(food_id)
                self._log("block_contributor", author, None, self._withdrawn_counts(author))
            return {
                "contributor": author,
                "blocked_at": blocked_at(self._writer, author),
                "withdrawn": self._withdrawn_counts(author),
            }

    def unblock(self, contributor: str) -> dict:
        """Undo a block: show again what the key wrote and accept its writes.

        What moderation hid one by one stays hidden, and a name somebody
        else gave since stays the name of the food. Unblocking a key that is
        not blocked changes nothing.

        Args:
            contributor: The contributor key.

        Returns:
            `contributor` (the keyed hash) and `restored`: how many
            products, versions and names are shown again.
        """
        author = self.contributor_hash(contributor)
        with self._lock, self._writer:
            restored = self._withdrawn_counts(author)
            foods = [
                row["record_id"]
                for row in self._writer.execute(
                    "SELECT DISTINCT record_id FROM withdrawn WHERE contributor = ? AND lang <> ''"
                    " ORDER BY record_id",
                    (author,),
                )
            ]
            self._writer.execute("DELETE FROM withdrawn WHERE contributor = ?", (author,))
            cursor = self._writer.execute("DELETE FROM blocked WHERE contributor = ?", (author,))
            for food_id in foods:
                self._refresh_search(food_id)
            if cursor.rowcount:
                self._log("unblock_contributor", author, None, restored)
            return {"contributor": author, "restored": restored}

    def _withdrawn_counts(self, author: str) -> dict[str, int]:
        """Count what is hidden with a blocked key, inside a transaction.

        Args:
            author: Keyed hash of the contributor key.

        Returns:
            The number of products, versions and names withdrawn.
        """
        row = self._writer.execute(
            "SELECT coalesce(sum(lang = '' AND record_id NOT LIKE ?), 0),"
            " coalesce(sum(lang = '' AND record_id LIKE ?), 0), coalesce(sum(lang <> ''), 0)"
            " FROM withdrawn WHERE contributor = ?",
            (kratheos.VERSION_PREFIX + "%", kratheos.VERSION_PREFIX + "%", author),
        ).fetchone()
        return dict(zip(("products", "versions", "translations"), row, strict=True))

    def close_report(self, report_id: int) -> bool:
        """Close a report.

        Args:
            report_id: Number of the report.

        Returns:
            False when there is no such report.
        """
        with self._lock, self._writer:
            cursor = self._writer.execute(
                "UPDATE reports SET state = 'closed', closed_at = coalesce(closed_at, ?)"
                " WHERE id = ?",
                (self._clock(), report_id),
            )
            if cursor.rowcount:
                self._log("close_report", str(report_id), None)
            return bool(cursor.rowcount)

    def set_hidden(self, record_id: str, hidden: bool) -> None:
        """Hide a food or a version, or show it again.

        Args:
            record_id: Identifier of the food or of the version.
            hidden: True to hide, False to restore.
        """
        with self._lock, self._writer:
            if hidden:
                self._writer.execute(
                    "INSERT OR IGNORE INTO hidden (record_id, hidden_at) VALUES (?, ?)",
                    (record_id, self._clock()),
                )
            else:
                self._writer.execute("DELETE FROM hidden WHERE record_id = ?", (record_id,))
            self._log("hide" if hidden else "restore", record_id, None)

    def import_layer(self, layer: kratheos.Layer) -> dict[str, int]:
        """Take over what a `kratheos.sqlite` holds and this database lacks.

        Nothing that is already here is changed. The votes of the layer are
        counts: they are taken over, as votes nobody can move, for the foods
        this database holds no vote of.

        Args:
            layer: The content of a `kratheos.sqlite`.

        Returns:
            How many products, versions, translations and votes were added.
        """
        added = {"products": 0, "versions": 0, "translations": 0, "votes": 0}
        touched: set[str] = set()
        with self._lock, self._writer:
            now = self._clock()
            for product in layer.products:
                barcode = normalize_gtin(product["barcode"])
                food_id = food_id_of_product(product["id"], product["kind"], barcode)
                names = kratheos.read_names(product["names"], f"product {product['id']}")
                names[product["name_lang"]] = " ".join(product["name"].split())
                countries = kratheos.read_countries(
                    product["countries"], f"product {product['id']}"
                )
                columns = ("kind", "name", "name_lang", *PRODUCT_VALUES)
                cursor = self._writer.execute(
                    f"INSERT OR IGNORE INTO products (uuid, food_id, barcode,"
                    f" {', '.join(columns)}, names, countries, updated_at)"
                    f" VALUES (?, ?, ?, {', '.join('?' for _ in columns)}, ?, ?, ?)",
                    (
                        product["id"],
                        food_id,
                        barcode,
                        *(product[name] for name in columns),
                        json.dumps(names, ensure_ascii=False, sort_keys=True),
                        json.dumps(sorted(set(countries or []))),
                        product["updated_at"],
                    ),
                )
                if cursor.rowcount:
                    added["products"] += 1
                    touched.add(food_id)
            for version in layer.versions:
                what = f"version {version['version_id']}"
                countries = kratheos.read_countries(version["countries"], what)
                cursor = self._writer.execute(
                    f"INSERT OR IGNORE INTO versions (version_id, food_id,"
                    f" {', '.join(VERSION_VALUES)}, countries, updated_at)"
                    f" VALUES (?, ?, {', '.join('?' for _ in VERSION_VALUES)}, ?, ?)",
                    (
                        version["version_id"],
                        version["food_id"],
                        *(version[name] for name in VERSION_VALUES),
                        json.dumps(sorted(set(countries or []))),
                        version["updated_at"],
                    ),
                )
                added["versions"] += cursor.rowcount
            for translation in layer.translations:
                cursor = self._writer.execute(
                    "INSERT OR IGNORE INTO translations (food_id, lang, name, origin, updated_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (
                        translation["record_id"],
                        translation["lang"],
                        " ".join(translation["name"].split()),
                        translation["origin"],
                        translation["updated_at"],
                    ),
                )
                if cursor.rowcount:
                    added["translations"] += 1
                    touched.add(translation["record_id"])
            known = {
                row[0]
                for row in self._writer.execute(
                    "SELECT food_id FROM votes UNION SELECT food_id FROM anonymous_votes"
                )
            }
            for vote in layer.votes:
                if vote["food_id"] in known or vote["votes"] <= 0:
                    continue
                self._writer.execute(
                    "INSERT INTO anonymous_votes (version_id, food_id, votes, updated_at)"
                    " VALUES (?, ?, ?, ?)",
                    (vote["version_id"], vote["food_id"], vote["votes"], vote["updated_at"]),
                )
                added["votes"] += vote["votes"]
            for food_id in sorted(touched):
                self._refresh_search(food_id)
            self._log("import_layer", "", None, {**added, "at": now})
        return added
