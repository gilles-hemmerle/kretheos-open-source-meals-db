"""Writes the public `kratheos.sqlite` from the live database.

The file written has the closed layout the build enforces (`kratheos.py`),
and holds what users see and nothing else:

* the products added, the versions proposed and the names added, except what
  moderation hid: a hidden food is left out with everything attached to it,
  a hidden version with its votes;
* the votes as one count per version. The table saying which contributor key
  voted for what is never read by this module: counting is done in SQL, and
  no key, hashed or not, reaches the file;
* no report, no history, nothing about who.

The same live data always gives the same file, byte for byte: the file is
created anew, rows are written in the order of their identifiers in a single
transaction, and its date (`exported_at`) is the time of the latest entry of
the live database rather than the time of the export. An export of a live
database that holds nothing is the empty file `kratheos-init` creates.

An export never quietly shrinks the layer: it is refused when the live
database holds nothing, or a fifth fewer products, versions or names than
the file it would replace. That is what a live database that lost its data,
or never took a published layer over, would do.
(Byte for byte holds for one version of the SQLite library, which writes its
own version number in the header of the files it creates.)

`commit_layer` commits the file in the repository it lies in, alone and with
a dated message, and pushes when asked and a remote exists.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from .. import kratheos
from ..download import hash_file
from . import live

Logger = Callable[[str], None]
FALLBACK_AUTHOR = ("kratheos-food-data", "kratheos-food-data@localhost")
# An export that drops more than this share of the products, of the versions
# or of the names of the layer it replaces is refused: moderation hides a few
# records, a live database that lost its data hides most of them.
MAX_SHRINK = 0.20
MIN_ROWS_TO_JUDGE = 10
GIT_TIMEOUT_SECONDS = 300


class ExportRefused(RuntimeError):
    """The export would replace a layer that holds data by an empty one."""


def _day(stamp: str | None) -> str:
    """Keep the date of a time of the live database.

    Args:
        stamp: A UTC time or a date, ISO 8601.

    Returns:
        The date, as the layer stores dates.
    """
    return str(stamp or "")[:10]


def read_live(path: Path) -> kratheos.Layer:
    """Read from the live database what the public layer must hold.

    Args:
        path: Path of `live.sqlite`.

    Returns:
        The layer: rows as the build reads them, in the order of their
        identifiers, what moderation hid left out.

    Raises:
        FileNotFoundError: When there is no live database.
    """
    if not path.exists():
        raise FileNotFoundError(f"{path} does not exist")
    connection = sqlite3.connect(path, timeout=live.BUSY_TIMEOUT_MS / 1000)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only = 1")
        connection.execute("BEGIN")
        hidden = {row[0] for row in connection.execute("SELECT record_id FROM hidden")}
        layer = kratheos.Layer(meta={"schema_version": str(kratheos.SCHEMA_VERSION)})
        latest = [
            connection.execute(f"SELECT max({column}) FROM {table}").fetchone()[0]
            for table, column in (
                ("products", "updated_at"),
                ("versions", "updated_at"),
                ("translations", "updated_at"),
                ("votes", "voted_at"),
                ("anonymous_votes", "updated_at"),
                ("hidden", "hidden_at"),
            )
        ]
        layer.meta["exported_at"] = max((stamp for stamp in latest if stamp), default="")

        for row in connection.execute("SELECT * FROM products ORDER BY uuid"):
            if row["food_id"] in hidden or live.RECORD_PREFIX + row["uuid"] in hidden:
                continue
            names = json.loads(row["names"])
            names.pop(row["name_lang"], None)
            countries = json.loads(row["countries"])
            product = {
                "id": row["uuid"],
                "kind": row["kind"],
                "barcode": row["barcode"],
                "name": row["name"],
                "name_lang": row["name_lang"],
                "names": json.dumps(names, ensure_ascii=False, sort_keys=True) if names else None,
                **{name: row[name] for name in live.PRODUCT_VALUES},
                "countries": json.dumps(countries) if countries else None,
                "updated_at": _day(row["updated_at"]),
            }
            layer.products.append(product)

        left_out = set(hidden)
        for row in connection.execute("SELECT * FROM versions ORDER BY version_id"):
            if row["food_id"] in hidden or row["version_id"] in hidden:
                left_out.add(row["version_id"])
                continue
            countries = json.loads(row["countries"])
            layer.versions.append(
                {
                    "version_id": row["version_id"],
                    "food_id": row["food_id"],
                    **{name: row[name] for name in live.VERSION_VALUES},
                    "countries": json.dumps(countries) if countries else None,
                    "updated_at": _day(row["updated_at"]),
                }
            )

        for row in connection.execute("SELECT * FROM translations ORDER BY food_id, lang"):
            if row["food_id"] in hidden:
                continue
            layer.translations.append(
                {
                    "record_id": row["food_id"],
                    "lang": row["lang"],
                    "name": row["name"],
                    "origin": row["origin"],
                    "updated_at": _day(row["updated_at"]),
                }
            )

        counted = connection.execute(
            """
            SELECT version_id, min(food_id) AS food_id, sum(votes) AS votes,
                   max(updated_at) AS updated_at
            FROM (
                SELECT version_id, food_id, count(*) AS votes, max(voted_at) AS updated_at
                FROM votes GROUP BY version_id, food_id
                UNION ALL
                SELECT version_id, food_id, votes, updated_at FROM anonymous_votes
            )
            GROUP BY version_id
            ORDER BY version_id
            """
        )
        for row in counted:
            if row["food_id"] in hidden or row["version_id"] in left_out or row["votes"] <= 0:
                continue
            layer.votes.append(
                {
                    "version_id": row["version_id"],
                    "food_id": row["food_id"],
                    "votes": row["votes"],
                    "updated_at": _day(row["updated_at"]),
                }
            )
        return layer
    finally:
        connection.close()


def write_layer(layer: kratheos.Layer, path: Path) -> None:
    """Write a layer as a new `kratheos.sqlite`, replacing the file in one step.

    Args:
        layer: The content to write.
        path: Where the file goes.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".part")
    partial.unlink(missing_ok=True)
    kratheos.create_database(partial)
    if layer.rows or layer.meta.get("exported_at"):
        connection = sqlite3.connect(partial)
        try:
            with connection:
                for table in ("products", "versions", "votes", "translations"):
                    columns = [name for name, _ in kratheos.TABLES[table]]
                    connection.executemany(
                        f"INSERT INTO {table} ({', '.join(columns)})"
                        f" VALUES ({', '.join('?' for _ in columns)})",
                        [[row[name] for name in columns] for row in getattr(layer, table)],
                    )
                connection.execute(
                    "UPDATE meta SET value = ? WHERE key = 'exported_at'",
                    (layer.meta.get("exported_at", ""),),
                )
        finally:
            connection.close()
    os.replace(partial, path)


def export_layer(live_path: Path, out: Path, force: bool = False) -> dict:
    """Write the public layer from the live database.

    Args:
        live_path: Path of `live.sqlite`.
        out: Path of the `kratheos.sqlite` to write.
        force: Write even when it replaces a layer holding data by an empty
            one.

    Returns:
        The number of rows written per table, the checksum of the file and
        whether the file changed.

    Raises:
        ExportRefused: When the live database holds nothing, or far less
            than the file to replace, which looks like a live database that
            lost its data or never took the layer over.
        FileNotFoundError: When there is no live database.
    """
    layer = read_live(live_path)
    before = hash_file(out)[0] if out.exists() else None
    if out.exists() and not force:
        previous = kratheos.read_layer(out)
        advice = (
            " Import the layer into the live database (food-data import-layer) if it was"
            " never taken over, or export with --force if this is what you want"
        )
        if previous.rows and not layer.rows:
            raise ExportRefused(
                f"{live_path} holds nothing while {out} holds {previous.rows} rows: not"
                f" replaced.{advice}"
            )
        for table in ("products", "versions", "translations"):
            had, has = len(getattr(previous, table)), len(getattr(layer, table))
            if had >= MIN_ROWS_TO_JUDGE and has < had * (1 - MAX_SHRINK):
                raise ExportRefused(
                    f"{out} holds {had} {table} and the export would leave {has}: more than"
                    f" {MAX_SHRINK:.0%} fewer, not replaced.{advice}"
                )
    write_layer(layer, out)
    kratheos.read_layer(out)  # the file written must be one the build accepts
    after = hash_file(out)[0]
    return {
        "file": str(out),
        "products": len(layer.products),
        "versions": len(layer.versions),
        "votes": sum(row["votes"] for row in layer.votes),
        "voted_versions": len(layer.votes),
        "translations": len(layer.translations),
        "exported_at": layer.meta["exported_at"],
        "sha256": after,
        "changed": after != before,
    }


def _git(repository: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run git in a repository.

    Args:
        repository: Directory of the repository.
        *arguments: The git command and its arguments.
        check: Fail when git fails.

    Returns:
        The finished process, with its output as text.

    Raises:
        RuntimeError: When git fails and `check` is set.
    """
    process = subprocess.run(  # noqa: S603 - fixed program, arguments passed as a list
        ["git", "-C", str(repository), *arguments],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_SECONDS,
        check=False,
    )
    if check and process.returncode != 0:
        detail = (process.stderr or process.stdout).strip()
        raise RuntimeError(f"git {' '.join(arguments)} failed: {detail}")
    return process


def commit_layer(path: Path, summary: dict, push: bool = False, log: Logger = print) -> str:
    """Commit the exported layer in its repository, alone, with a dated message.

    Nothing else is committed, whatever else changed or is staged. Without a
    change there is no commit. A push is only done when asked for and when
    the repository has a remote.

    Args:
        path: Path of the `kratheos.sqlite` that was written.
        summary: The counts returned by `export_layer`, for the message.
        push: Push the branch after the commit.
        log: Function receiving what happens.

    Returns:
        `committed`, `unchanged`, `committed and pushed` or `committed, no
        remote to push to`.

    Raises:
        RuntimeError: When the file is not in a git repository or git fails.
    """
    top = _git(path.parent, "rev-parse", "--show-toplevel").stdout.strip()
    repository = Path(top)
    relative = str(path.resolve().relative_to(repository.resolve()))
    if not _git(repository, "status", "--porcelain", "--", relative).stdout.strip():
        log(f"{relative} did not change: nothing to commit")
        return "unchanged"
    identity = []
    if not _git(repository, "config", "user.email", check=False).stdout.strip():
        identity = [
            "-c",
            f"user.name={FALLBACK_AUTHOR[0]}",
            "-c",
            f"user.email={FALLBACK_AUTHOR[1]}",
        ]
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    message = (
        f"data: the Kratheos layer exported on {today}\n\n"
        f"{summary['products']} products, {summary['versions']} versions,"
        f" {summary['votes']} votes on {summary['voted_versions']} versions,"
        f" {summary['translations']} names. Latest entry: {summary['exported_at'] or 'none'}."
    )
    _git(repository, "add", "--", relative)
    _git(repository, *identity, "commit", "--quiet", "-m", message, "--", relative)
    log(f"committed {relative}")
    if not push:
        return "committed"
    if not _git(repository, "remote").stdout.split():
        log("the repository has no remote: nothing pushed")
        return "committed, no remote to push to"
    _git(repository, "push", "--quiet")
    log("pushed")
    return "committed and pushed"
