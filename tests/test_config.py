"""Tests of the source list: the real file and the rules it must obey."""

from __future__ import annotations

from pathlib import Path

import pytest

from kratheos_food_data.adapters import ADAPTER_MODULES, get_adapter
from kratheos_food_data.config import ConfigError, Source, enabled_sources, load_sources

VALID = """
[[source]]
id = "demo"
name = "Demo"
publisher = "Somebody"
homepage = "https://example.org/"
adapter = "ciqual"
version = "1"
kinds = ["generic"]
priority = 1
licence = "CC0-1.0"
licence_name = "CC0"
licence_url = "https://example.org/licence"
terms_url = "https://example.org/terms"
terms_quote = "You may redistribute."
terms_checked = "2026-10-11"
attribution = "Demo data"
mirror = "git"
country = "fr"
enabled = true

  [[source.files]]
  name = "a.xml"
  url = "https://example.org/a.xml"
"""


def _load(tmp_path: Path, text: str) -> list[Source]:
    """Write a source list and load it.

    Args:
        tmp_path: Temporary directory of the test.
        text: Content of the source list.

    Returns:
        The loaded sources.
    """
    path = tmp_path / "sources.toml"
    path.write_text(text, encoding="utf-8")
    return load_sources(path)


def test_real_source_list_is_complete(declared_sources: dict[str, Source]) -> None:
    """Every enabled source of the repository states its licence and terms."""
    assert {"off", "usda", "ciqual", "swiss"} <= set(declared_sources)
    for source in declared_sources.values():
        if source.enabled:
            assert source.terms_quote and source.terms_url and source.attribution
            assert source.licence and source.licence_url
            assert source.adapter in ADAPTER_MODULES
            assert callable(get_adapter(source.adapter))
            assert source.files
        else:
            assert source.reason


def test_disabled_sources_stay_listed(declared_sources: dict[str, Source]) -> None:
    """Sources without clear terms are listed, disabled, with the reason."""
    assert not declared_sources["bls"].enabled
    assert not declared_sources["foodrepo"].enabled
    assert "licence" in declared_sources["bls"].reason


def test_only_open_food_facts_is_share_alike(declared_sources: dict[str, Source]) -> None:
    """The share-alike obligation comes from Open Food Facts alone."""
    share_alike = {source.id for source in declared_sources.values() if source.share_alike}
    assert share_alike == {"off"}


def test_valid_list_loads(tmp_path: Path) -> None:
    """A well-formed source list loads."""
    (source,) = _load(tmp_path, VALID)
    assert source.id == "demo"
    assert source.file("a.xml").url == "https://example.org/a.xml"
    with pytest.raises(KeyError):
        source.file("missing")


def test_storage_and_country_are_validated(tmp_path: Path) -> None:
    """A source says where its files are stored; its country is an ISO code."""
    (source,) = _load(tmp_path, VALID)
    assert source.mirror == "git"
    assert source.country == "FR"
    with pytest.raises(ConfigError, match="mirror"):
        _load(tmp_path, VALID.replace('mirror = "git"', 'mirror = "somewhere"'))
    with pytest.raises(ConfigError, match="country"):
        _load(tmp_path, VALID.replace('country = "fr"', 'country = "France"'))


def test_generic_tables_declare_their_country(declared_sources: dict[str, Source]) -> None:
    """Every enabled table of generic foods names the country it describes."""
    countries = {
        source.id: source.country
        for source in declared_sources.values()
        if source.enabled and "generic" in source.kinds
    }
    assert countries == {"usda": "US", "ciqual": "FR", "swiss": "CH", "cnf": "CA", "cofid": "GB"}
    assert declared_sources["off"].country == ""


def test_enabled_source_needs_its_licence(tmp_path: Path) -> None:
    """An enabled source without the sentence allowing redistribution is refused."""
    with pytest.raises(ConfigError, match="terms_quote"):
        _load(tmp_path, VALID.replace('terms_quote = "You may redistribute."\n', ""))


def test_disabled_source_needs_a_reason(tmp_path: Path) -> None:
    """A disabled source must say why."""
    with pytest.raises(ConfigError, match="without a reason"):
        _load(tmp_path, VALID.replace("enabled = true", "enabled = false"))


def test_duplicate_ids_are_refused(tmp_path: Path) -> None:
    """Two sources may not share an id."""
    with pytest.raises(ConfigError, match="same id"):
        _load(tmp_path, VALID + VALID.replace("priority = 1", "priority = 2"))


def test_only_rejects_disabled_and_unknown(declared_sources: dict[str, Source]) -> None:
    """A build cannot be restricted to a disabled or unknown source."""
    sources = list(declared_sources.values())
    assert [source.id for source in enabled_sources(sources, ["ciqual"])] == ["ciqual"]
    with pytest.raises(ConfigError, match="disabled"):
        enabled_sources(sources, ["bls"])
    with pytest.raises(ConfigError, match="unknown"):
        enabled_sources(sources, ["nope"])
