"""Settings of the food service, read from the environment and the command line.

Every setting has an environment variable (`FOOD_DATA_...`), so that the same
values serve the service and the commands run by timers; a command-line
option, when given, wins.

Files, all under one data directory (`FOOD_DATA_DIR`, by default `data/` in
the repository):

* `food.sqlite`: the main database being served (`FOOD_DATA_DATABASE`);
* `manifest.json`: the manifest of the build it comes from, next to it;
* `live.sqlite`: the live database (`FOOD_DATA_LIVE`), with its `-wal` and
  `-shm` companions while the service runs;
* `contributor.key`: the secret the contributor keys are hashed with, when
  `FOOD_DATA_KEY_SECRET` does not give one.

The secret token of the writes is `FOOD_DATA_TOKEN`; without it the service
only answers reads.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ..versions import DEFAULT_USER_VERSION_VOTES

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8750
DATABASE_NAME = "food.sqlite"
LIVE_NAME = "live.sqlite"
KEY_NAME = "contributor.key"
MANIFEST_NAME = "manifest.json"
MIN_SECRET_LENGTH = 16


class SettingsError(ValueError):
    """A setting is missing or makes no sense."""


@dataclass(frozen=True)
class Settings:
    """What the service needs to know to run.

    Attributes:
        database: Path of `food.sqlite`.
        live: Path of the live database.
        host: Address to listen on.
        port: Port to listen on.
        token: Shared secret of the writes and of moderation; empty when
            writes are disabled.
        key_secret: Secret the contributor keys are hashed with.
        user_version_votes: Votes a version coming from a user needs to be
            served instead of a record of a source.
        layer: `kratheos.sqlite` a new live database is filled from, if any.
        readers: Number of connections kept on the main database.
        max_body_bytes: Largest request body accepted.
        search_budget_seconds: Time a search may spend in the full-text
            index before it answers with what it has found.
    """

    database: Path
    live: Path
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    token: str = ""
    key_secret: bytes = b""
    user_version_votes: int = DEFAULT_USER_VERSION_VOTES
    layer: Path | None = None
    readers: int = 8
    max_body_bytes: int = 64 * 1024
    search_budget_seconds: float = 0.5

    @property
    def manifest(self) -> Path:
        """Return the path of the manifest kept next to the main database."""
        return self.database.with_name(MANIFEST_NAME)


def data_directory(root: Path, environment: Mapping[str, str] | None = None) -> Path:
    """Return the directory holding the data of the service.

    Args:
        root: Directory of the repository.
        environment: Environment variables, by default those of the process.

    Returns:
        `FOOD_DATA_DIR` when set, else `data/` under the repository.
    """
    environment = os.environ if environment is None else environment
    configured = environment.get("FOOD_DATA_DIR", "").strip()
    return Path(configured).expanduser().resolve() if configured else root / "data"


def _integer(name: str, text: str, minimum: int, maximum: int) -> int:
    """Read an integer setting.

    Args:
        name: Name of the setting, for the error message.
        text: Its value as text.
        minimum: Smallest accepted value.
        maximum: Largest accepted value.

    Returns:
        The value.

    Raises:
        SettingsError: When the text is not an integer within the bounds.
    """
    try:
        value = int(text)
    except ValueError as error:
        raise SettingsError(f"{name} must be a whole number, got {text!r}") from error
    if not minimum <= value <= maximum:
        raise SettingsError(f"{name} must be between {minimum} and {maximum}, got {value}")
    return value


def load_key_secret(path: Path, environment: Mapping[str, str]) -> bytes:
    """Return the secret the contributor keys are hashed with.

    The secret comes from `FOOD_DATA_KEY_SECRET` when it is set. Otherwise it
    is read from a file next to the live database, created with a random
    value the first time and readable by its owner only.

    Args:
        path: Path of the key file.
        environment: Environment variables.

    Returns:
        The secret.

    Raises:
        SettingsError: When the configured secret is too short to be one.
    """
    configured = environment.get("FOOD_DATA_KEY_SECRET", "").strip()
    if configured:
        if len(configured) < MIN_SECRET_LENGTH:
            raise SettingsError(
                f"FOOD_DATA_KEY_SECRET must have at least {MIN_SECRET_LENGTH} characters"
            )
        return configured.encode("utf-8")
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            handle.write(secrets.token_hex(32) + "\n")
    secret = path.read_text(encoding="ascii").strip()
    if len(secret) < MIN_SECRET_LENGTH:
        raise SettingsError(f"{path} does not hold a secret")
    return secret.encode("utf-8")


def load_settings(
    root: Path,
    *,
    database: Path | None = None,
    live: Path | None = None,
    host: str | None = None,
    port: int | None = None,
    layer: Path | None = None,
    user_version_votes: int | None = None,
    environment: Mapping[str, str] | None = None,
    with_secret: bool = True,
) -> Settings:
    """Assemble the settings from the options given and the environment.

    Args:
        root: Directory of the repository.
        database: Path of `food.sqlite`, when given on the command line.
        live: Path of the live database, when given on the command line.
        host: Address to listen on, when given on the command line.
        port: Port to listen on, when given on the command line.
        layer: `kratheos.sqlite` a new live database is filled from.
        user_version_votes: Vote threshold, when given on the command line.
        environment: Environment variables, by default those of the process.
        with_secret: Read (or create) the secret of the contributor keys;
            commands that never touch a key do without.

    Returns:
        The settings.

    Raises:
        SettingsError: When a setting makes no sense.
    """
    environment = os.environ if environment is None else environment
    directory = data_directory(root, environment)

    def path_setting(given: Path | None, variable: str, default: Path) -> Path:
        """Choose a path: the option, else the variable, else the default."""
        if given is not None:
            return given.expanduser().resolve()
        configured = environment.get(variable, "").strip()
        return Path(configured).expanduser().resolve() if configured else default

    live_path = path_setting(live, "FOOD_DATA_LIVE", directory / LIVE_NAME)
    if port is None:
        port = _integer(
            "FOOD_DATA_PORT", environment.get("FOOD_DATA_PORT", str(DEFAULT_PORT)), 1, 65535
        )
    if user_version_votes is None:
        user_version_votes = _integer(
            "FOOD_DATA_USER_VERSION_VOTES",
            environment.get("FOOD_DATA_USER_VERSION_VOTES", str(DEFAULT_USER_VERSION_VOTES)),
            1,
            1_000_000,
        )
    token = environment.get("FOOD_DATA_TOKEN", "").strip()
    if token and len(token) < MIN_SECRET_LENGTH:
        raise SettingsError(f"FOOD_DATA_TOKEN must have at least {MIN_SECRET_LENGTH} characters")
    secret = load_key_secret(live_path.with_name(KEY_NAME), environment) if with_secret else b""
    budget = _integer(
        "FOOD_DATA_SEARCH_BUDGET_MS", environment.get("FOOD_DATA_SEARCH_BUDGET_MS", "500"), 50, 5000
    )
    return Settings(
        database=path_setting(database, "FOOD_DATA_DATABASE", directory / DATABASE_NAME),
        live=live_path,
        host=host or environment.get("FOOD_DATA_HOST", "").strip() or DEFAULT_HOST,
        port=port,
        token=token,
        key_secret=secret,
        user_version_votes=user_version_votes,
        layer=layer,
        search_budget_seconds=budget / 1000,
    )
