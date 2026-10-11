"""Loads and validates the declarative source list (`sources.toml`).

The source list is the single place that says which databases exist, where
their files are, under which licence they are published and whether the build
uses them. This module turns it into typed objects and refuses a list that is
incomplete, so that a source can never be enabled without its licence fields.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

REQUIRED_WHEN_ENABLED = (
    "adapter",
    "version",
    "licence",
    "licence_name",
    "licence_url",
    "terms_url",
    "terms_quote",
    "terms_checked",
    "attribution",
)
KINDS = {"generic", "product"}


class ConfigError(ValueError):
    """The source list is malformed."""


@dataclass(frozen=True)
class SourceFile:
    """A file to download for a source.

    Attributes:
        name: File name under `downloads/<source id>/`.
        url: Address to download it from.
        sha256: Pinned SHA-256, for files that never change.
        md5: Pinned MD5, for publishers that only give that.
    """

    name: str
    url: str
    sha256: str | None = None
    md5: str | None = None


@dataclass(frozen=True)
class Source:
    """A food database, as declared in the source list.

    Attributes:
        id: Short stable identifier, prefix of every record id.
        name: Name of the database.
        publisher: Who publishes it.
        homepage: Page of the database.
        adapter: Module of `kratheos_food_data.adapters` that reads it.
        version: Release of the listed files, or "rolling".
        released: Date of that release.
        kinds: What it holds: "generic" and/or "product".
        priority: Fixed source order, lowest first.
        licence: SPDX identifier or short name of the licence.
        licence_name: Full name of the licence or terms.
        licence_url: Text of the licence or terms.
        terms_url: Publisher page where the terms were read.
        terms_quote: Sentence(s) of the publisher allowing redistribution.
        terms_checked: Date the terms were last read.
        attribution: Text to reproduce next to the data.
        share_alike: Whether derived databases must keep the licence.
        enabled: Whether the build uses the source.
        reason: Why it is disabled.
        files: Files to download.
    """

    id: str
    name: str
    publisher: str
    homepage: str
    adapter: str
    version: str
    released: str
    kinds: tuple[str, ...]
    priority: int
    licence: str
    licence_name: str
    licence_url: str
    terms_url: str
    terms_quote: str
    terms_checked: str
    attribution: str
    share_alike: bool
    enabled: bool
    reason: str
    files: tuple[SourceFile, ...]

    def file(self, name: str) -> SourceFile:
        """Return the declared file with the given name.

        Args:
            name: File name as declared in the source list.

        Returns:
            The matching file entry.

        Raises:
            KeyError: When the source declares no such file.
        """
        for entry in self.files:
            if entry.name == name:
                return entry
        raise KeyError(f"source {self.id} declares no file named {name}")


def _parse_source(raw: dict) -> Source:
    """Build one source from its TOML table.

    Args:
        raw: The table of a `[[source]]` entry.

    Returns:
        The validated source.

    Raises:
        ConfigError: When a field is missing or inconsistent.
    """
    source_id = raw.get("id")
    if not source_id or not isinstance(source_id, str) or ":" in source_id:
        raise ConfigError(f"a source needs an id without a colon, got {source_id!r}")
    try:
        files = tuple(
            SourceFile(
                name=entry["name"],
                url=entry["url"],
                sha256=entry.get("sha256"),
                md5=entry.get("md5"),
            )
            for entry in raw.get("files", [])
        )
        source = Source(
            id=source_id,
            name=raw["name"],
            publisher=raw["publisher"],
            homepage=raw["homepage"],
            adapter=raw.get("adapter", ""),
            version=raw.get("version", ""),
            released=raw.get("released", ""),
            kinds=tuple(raw["kinds"]),
            priority=int(raw["priority"]),
            licence=raw.get("licence", ""),
            licence_name=raw.get("licence_name", ""),
            licence_url=raw.get("licence_url", ""),
            terms_url=raw.get("terms_url", ""),
            terms_quote=raw.get("terms_quote", ""),
            terms_checked=raw.get("terms_checked", ""),
            attribution=raw.get("attribution", ""),
            share_alike=bool(raw.get("share_alike", False)),
            enabled=bool(raw["enabled"]),
            reason=raw.get("reason", ""),
            files=files,
        )
    except KeyError as error:
        raise ConfigError(f"source {source_id}: missing field {error}") from error
    if not set(source.kinds) <= KINDS or not source.kinds:
        raise ConfigError(f"source {source_id}: kinds must be among {sorted(KINDS)}")
    if source.enabled:
        missing = [name for name in REQUIRED_WHEN_ENABLED if not getattr(source, name)]
        if missing:
            raise ConfigError(f"source {source_id} is enabled but lacks: {', '.join(missing)}")
        if not source.files:
            raise ConfigError(f"source {source_id} is enabled but lists no file")
    elif not source.reason:
        raise ConfigError(f"source {source_id} is disabled without a reason")
    return source


def load_sources(path: Path) -> list[Source]:
    """Read the source list.

    Args:
        path: Path of `sources.toml`.

    Returns:
        Every declared source, enabled or not, in fixed priority order.

    Raises:
        ConfigError: When the list is malformed, or ids or priorities repeat.
    """
    with path.open("rb") as handle:
        document = tomllib.load(handle)
    sources = [_parse_source(raw) for raw in document.get("source", [])]
    if not sources:
        raise ConfigError(f"{path} declares no source")
    for attribute in ("id", "priority"):
        values = [getattr(source, attribute) for source in sources]
        if len(values) != len(set(values)):
            raise ConfigError(f"{path}: two sources share the same {attribute}")
    return sorted(sources, key=lambda source: source.priority)


def enabled_sources(sources: list[Source], only: list[str] | None = None) -> list[Source]:
    """Select the sources a build must process.

    Args:
        sources: Every declared source.
        only: Optional list of source ids to restrict the build to.

    Returns:
        The enabled sources, restricted to `only` when given.

    Raises:
        ConfigError: When `only` names an unknown or disabled source.
    """
    by_id = {source.id: source for source in sources}
    if only:
        for source_id in only:
            if source_id not in by_id:
                raise ConfigError(f"unknown source: {source_id}")
            if not by_id[source_id].enabled:
                raise ConfigError(f"source {source_id} is disabled: {by_id[source_id].reason}")
        return [source for source in sources if source.id in only]
    return [source for source in sources if source.enabled]
