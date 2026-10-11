"""Tests of the source adapters on the miniature source files.

Each test runs one adapter and reads back the staging file it wrote: an
adapter must map the columns and units of its source, and nothing else.
"""

from __future__ import annotations

from datetime import date

import pytest
from conftest import staged_rows

from kratheos_food_data import schema
from kratheos_food_data.adapters import get_adapter
from kratheos_food_data.config import Source


def _run(source_id: str, declared_sources: dict[str, Source], adapter_context: callable) -> tuple:
    """Run the adapter of a source on its miniature files.

    Args:
        source_id: Identifier of the source.
        declared_sources: The real source list.
        adapter_context: Factory of adapter contexts.

    Returns:
        The staged rows by record id, and the result of the adapter.
    """
    source = declared_sources[source_id]
    context = adapter_context(source_id)
    result = get_adapter(source.adapter)(source, context)
    return staged_rows(context.con, context.staging_path), result


def test_every_adapter_writes_the_staging_schema(
    declared_sources: dict[str, Source], adapter_context: callable
) -> None:
    """Every adapter writes exactly the staging columns, in order and type."""
    expected = [(column.name, column.type) for column in schema.STAGING_COLUMNS]
    for source in declared_sources.values():
        if not source.enabled:
            continue
        context = adapter_context(source.id)
        get_adapter(source.adapter)(source, context)
        described = context.con.execute(
            f"DESCRIBE SELECT * FROM read_parquet('{context.staging_path}')"
        ).fetchall()
        assert [(row[0], row[1]) for row in described] == expected, source.id


def test_open_food_facts(declared_sources: dict[str, Source], adapter_context: callable) -> None:
    """Names, countries, basis, serving and nutrients are read from the export."""
    rows, result = _run("off", declared_sources, adapter_context)
    assert result.stats["rows_in_source"] == 10
    assert result.stats["rows_staged"] == 9  # one barcode appears on two rows
    assert result.stats["distinct_country_tags_not_mapped"] == 2  # world and an unknown tag
    assert result.released == "2026-10-10"

    nutella = rows["3017620422003"]
    assert nutella["names"] == {"fr": "Nutella", "en": "Nutella hazelnut spread"}
    assert nutella["main_lang"] == "fr"
    assert nutella["brand"] == "Nutella, Ferrero"
    assert nutella["category"] == "en:hazelnut-spreads"
    assert nutella["countries"] == ["FR", "GB"]  # GB, not the UK of the taxonomy
    assert nutella["basis"] == "100g" and nutella["basis_assumed"] is False
    assert nutella["energy_kcal"] == 539
    assert nutella["carbohydrate_g"] == pytest.approx(57.5)
    assert nutella["carbohydrate_includes_fiber"] is False
    assert nutella["salt_g"] == pytest.approx(0.107)
    assert nutella["is_drink"] is False
    assert nutella["popularity"] == 1064
    assert nutella["updated_at"] == date(2026, 10, 10)
    assert nutella["source_url"] == "https://world.openfoodfacts.org/product/3017620422003"

    cola = rows["5449000000996"]
    assert cola["names"] == {"es": "Coca-Cola Original", "fr": "Coca-Cola"}
    assert cola["energy_kcal"] is None and cola["energy_kj"] == 180
    assert cola["basis"] == "100ml"
    assert (cola["serving_size"], cola["serving_unit"]) == (330, "ml")
    assert cola["is_drink"] is True

    american = rows["0049000028911"]
    assert american["countries"] == ["US"]
    assert american["carbohydrate_includes_fiber"] is True  # sold in the United States only
    assert american["serving_unit"] == "ml" and american["basis"] == "100ml"

    muesli = rows["7610000123451"]
    assert muesli["names"] == {"de": "Bircher Müesli"}  # the most recent of the two rows
    assert muesli["category"] is None and muesli["is_drink"] is None
    assert muesli["basis_assumed"] is True

    wine = rows["3560070000012"]
    assert wine["alcohol_abv"] == 12.5 and wine["alcohol_g"] is None
    assert wine["source_flags"] == [schema.FLAG_OBSOLETE]
    assert rows["20095314"]["countries"] is None  # its only tag maps to no country
    assert rows["4006381333931"]["names"] is None


def test_open_food_facts_never_copies_contributors(
    declared_sources: dict[str, Source], adapter_context: callable
) -> None:
    """No handle of a contributor reaches the staging file."""
    rows, _ = _run("off", declared_sources, adapter_context)
    text = repr(rows)
    assert "a-contributor-handle" not in text
    assert "another-contributor" not in text


def test_usda(declared_sources: dict[str, Source], adapter_context: callable) -> None:
    """Identifiers survive releases; versions and both nutrient numberings are handled."""
    rows, result = _run("usda", declared_sources, adapter_context)
    assert set(rows) == {
        "foundation:16158",
        "foundation:100147",
        "sr_legacy:21237",
        "sr_legacy:14400",
        "sr_legacy:99999",
        "fndds:27510389",
        "fndds:11111000",
        "branded:0049000028911",
        "branded:3017620422003",
        "branded:NIELSENUK0002",
        "branded:9400547001910",
    }
    assert result.stats["foundation_versions_or_unidentified_left_out"] == 1
    assert result.stats["branded_versions_or_unidentified_left_out"] == 2

    hummus = rows["foundation:16158"]
    assert hummus["kind"] == "generic" and hummus["source_dataset"] == "foundation"
    assert hummus["energy_kcal"] == 229  # the current version, not the superseded one
    assert hummus["carbohydrate_g"] == 14.9 and hummus["carbohydrate_includes_fiber"] is True
    assert hummus["fiber_g"] == 5.4
    assert hummus["sodium_g"] == pytest.approx(0.438)
    assert hummus["sugars_g"] == 0.34  # from the second sugar identifier
    assert hummus["countries"] == ["US"]
    assert hummus["source_url"] == "https://fdc.nal.usda.gov/food-details/1001/nutrients"
    assert rows["foundation:100147"]["energy_kcal"] == 884  # Atwater general factors

    big_mac = rows["sr_legacy:21237"]
    assert big_mac["names"] == {"en": "McDONALD'S, BIG MAC"}
    assert big_mac["category"] == "Fast Foods" and big_mac["is_drink"] is False
    assert rows["sr_legacy:14400"]["is_drink"] is True

    cheeseburger = rows["fndds:27510389"]  # legacy nutrient numbers
    assert cheeseburger["energy_kcal"] == 263 and cheeseburger["protein_g"] == 13.0
    assert cheeseburger["category"] == "Burgers"
    assert rows["fndds:11111000"]["is_drink"] is True

    cola = rows["branded:0049000028911"]
    assert cola["names"] == {"en": "Cola"}  # the latest of two versions
    assert cola["barcode_raw"] == "049000028911"
    assert cola["brand"] == "Cola Classic"
    assert cola["basis"] == "100ml" and cola["is_drink"] is True
    assert (cola["serving_size"], cola["serving_unit"]) == (355, "ml")
    assert cola["countries"] == ["US"] and cola["updated_at"] == date(2025, 8, 28)
    assert rows["branded:3017620422003"]["brand"] == "Spread Makers Inc."

    snack = rows["branded:NIELSENUK0002"]
    assert snack["names"] == {"en": "BAD BARCODE SNACK"}
    assert snack["basis_assumed"] is True and snack["source_flags"] == [schema.FLAG_OBSOLETE]

    juice = rows["branded:9400547001910"]
    assert juice["countries"] == ["NZ"] and juice["energy_kj"] == 200
    assert juice["carbohydrate_includes_fiber"] is False  # New Zealand labels


def test_ciqual(declared_sources: dict[str, Source], adapter_context: callable) -> None:
    """Both names, the food group and the textual values are read."""
    rows, _ = _run("ciqual", declared_sources, adapter_context)
    apricot = rows["13000"]
    assert apricot["names"] == {"fr": "Abricot, dénoyauté, cru", "en": "Apricot, pitted, raw"}
    assert apricot["main_lang"] == "fr"
    assert apricot["category"] == "fruits, vegetables, legumes and nuts / fruits / fruits, raw"
    assert apricot["energy_kcal"] == 45.9 and apricot["energy_kj"] == 194
    assert apricot["fat_g"] == 0.5  # given as "< 0,5"
    assert apricot["saturated_fat_g"] == 0.0  # given as "traces"
    assert apricot["sodium_g"] == pytest.approx(0.001)
    assert apricot["source_flags"] == [schema.FLAG_INEXACT_VALUES]
    assert apricot["carbohydrate_includes_fiber"] is False
    assert apricot["countries"] == ["FR"]
    assert apricot["source_url"] == "https://ciqual.anses.fr/#/aliments/13000"

    juice = rows["2069"]
    assert juice["energy_kcal"] is None and juice["energy_kj"] == 186
    assert juice["protein_g"] == 0.1  # fallback on nitrogen x 6.25
    assert juice["salt_g"] is None and juice["sodium_g"] == pytest.approx(0.002)
    assert juice["is_drink"] is True
    assert rows["25999"]["category"] == "starters and dishes / sandwiches"
    assert rows["99999"]["energy_kcal"] is None


def test_swiss(declared_sources: dict[str, Source], adapter_context: callable) -> None:
    """Names come from the four workbooks, values from the English one."""
    rows, result = _run("swiss", declared_sources, adapter_context)
    assert (result.version, result.released) == ("7.1", "2026-07-01")
    almond = rows["273"]
    assert almond["names"] == {"en": "Almond", "de": "Mandel", "fr": "Amande", "it": "Mandorla"}
    assert almond["kind"] == "generic" and almond["energy_kcal"] == 624
    assert almond["sodium_g"] == 0.0 and almond["source_flags"] == [schema.FLAG_INEXACT_VALUES]
    assert almond["countries"] == ["CH"] and almond["updated_at"] == date(2026, 7, 1)

    juice = rows["605"]
    assert juice["basis"] == "100ml" and juice["is_drink"] is True
    assert juice["fat_g"] == 0.1 and juice["saturated_fat_g"] is None
    assert juice["source_url"].endswith("#/search-results/Apple%20juice")

    water = rows["1045"]
    assert water["kind"] == "product" and water["source_dataset"] == "branded"
    assert water["barcode_raw"] is None


def test_canadian_nutrient_file(
    declared_sources: dict[str, Source], adapter_context: callable
) -> None:
    """Both languages and the legacy nutrient codes are read."""
    rows, _ = _run("cnf", declared_sources, adapter_context)
    souffle = rows["2"]
    assert souffle["names"] == {"en": "Cheese souffle", "fr": "Soufflé au fromage"}
    assert souffle["category"] == "Mixed Dishes"
    assert souffle["energy_kcal"] == 204 and souffle["energy_kj"] == 855
    assert souffle["carbohydrate_g"] == pytest.approx(5.911)
    assert souffle["carbohydrate_includes_fiber"] is True
    assert souffle["sodium_g"] == pytest.approx(0.374)
    assert souffle["countries"] == ["CA"] and souffle["updated_at"] == date(2012, 6, 28)
    assert rows["502"]["is_drink"] is True


def test_cofid(declared_sources: dict[str, Source], adapter_context: callable) -> None:
    """Group names, the 100 ml basis of alcoholic drinks and Tr / N are handled."""
    rows, result = _run("cofid", declared_sources, adapter_context)
    assert result.stats["repeated_food_codes_skipped"] == 1
    bread = rows["11-001"]
    assert bread["names"] == {"en": "Bread, white, sliced"}
    assert bread["category"] == "Cereals and cereal products / Breads"
    assert bread["fiber_g"] == 2.5  # AOAC fibre preferred to NSP
    assert bread["sodium_g"] == pytest.approx(0.461)
    assert bread["countries"] == ["GB"] and bread["basis"] == "100g"

    beer = rows["17-225"]
    assert beer["basis"] == "100ml" and beer["is_drink"] is True
    assert beer["fat_g"] == 0.0 and beer["saturated_fat_g"] is None  # Tr and N
    assert beer["source_flags"] == [schema.FLAG_INEXACT_VALUES]
    assert rows["17-063"]["fiber_g"] == 0.0  # NSP when there is no AOAC value
    assert rows["19-999"]["energy_kcal"] is None


def test_chain_identifiers_and_cells(tmp_path) -> None:
    """A chain product is identified by chain, country and key, in at most 74 characters."""
    from kratheos_food_data.adapters import chain

    assert chain.slug("Chaîne  Exemple & Fils") == "chaine-exemple-fils"
    assert chain.record_id("Burger King", "CH", "b3c0ce68-1261", "Whopper") == (
        "burger-king:ch:b3c0ce68-1261",
        False,
    )
    # No key on the site: the name, as long as the chain keeps it.
    assert chain.record_id("KFC", "ch", None, "Hot Wings® Aile supérieure") == (
        "kfc:ch:hot-wings-aile-superieure",
        True,
    )
    # A key that is not plain is written like a name, and stays a key.
    assert chain.record_id("KFC", "CH", "menu/42 b", "x") == ("kfc:ch:menu-42-b", False)
    long_name = "Menu " + "très " * 30 + "long"
    identifier, from_name = chain.record_id("KFC", "CH", "", long_name)
    assert from_name and len(identifier) == chain.MAX_RECORD_ID_LENGTH
    assert identifier == chain.record_id("KFC", "CH", "", long_name)[0]
    assert identifier != chain.record_id("KFC", "CH", "", long_name + " bis")[0]

    assert chain.parse_number(" 12,5 ") == 12.5
    assert chain.parse_number("") is None and chain.parse_number("n/a") is None
    assert chain.parse_number("nan") is None and chain.parse_number("inf") is None
    assert chain.parse_flag("Yes") is True and chain.parse_flag("no") is False
    assert chain.parse_flag("") is None
    assert chain.parse_date("2026-10-11") == "2026-10-11"
    assert chain.parse_date("11.10.2026") is None

    incomplete = tmp_path / "incomplete.csv"
    incomplete.write_text("chain,country\nX,CH\n", encoding="utf-8")
    with pytest.raises(ValueError, match="missing columns language"):
        chain.read_file(incomplete)


def test_chain_file_without_origin_columns_and_rows_per_portion_only(tmp_path) -> None:
    """A file needs no column saying where it was read; a row per portion only is left out."""
    from kratheos_food_data.adapters import chain

    per_100 = list(chain.VALUE_COLUMNS)
    header = [
        "chain", "country", "language", "product_name", "category", "product_key",
        "portion_size", "portion_unit", "is_drink", *chain.PORTION_COLUMNS, *per_100,
    ]  # fmt: skip
    burger = ["X", "CH", "fr", "Burger", "", "b-1", "200", "g", "no"] + ["500"] + [""] * 7
    burger += ["250", "13", "20", "4", "13", "5", "2", "1.2"]
    # The chain gives 180 kcal per portion and no weight for that portion.
    ice = ["X", "CH", "fr", "Glace", "", "g-1", "", "", "no", "180"] + [""] * 7 + [""] * 8
    empty = ["X", "CH", "fr", "Sauce", "", "s-1", "", "", "no"] + [""] * 16
    path = tmp_path / "x_ch.csv"
    lines = [header, burger, ice, empty]
    path.write_text("\n".join(",".join(cells) for cells in lines) + "\n", encoding="utf-8")
    rows, left_out = chain.read_file(path)
    assert left_out == 1
    assert [row["source_record_id"] for row in rows] == ["x:ch:b-1", "x:ch:s-1"]
    assert rows[0]["energy_kcal"] == 250.0 and rows[0]["serving_size"] == 200.0
    assert rows[0]["source_url"] is None and rows[0]["updated_at"] is None
    assert rows[1]["energy_kcal"] is None  # no value at all: dropped later, as without energy

    with_origin = tmp_path / "y_ch.csv"
    header += ["source_url", "date_collected"]
    lines = [header, [*burger, "https://example.org/b", "2026-10-11"]]
    with_origin.write_text("\n".join(",".join(c) for c in lines) + "\n", encoding="utf-8")
    rows, left_out = chain.read_file(with_origin)
    # The date is read; where the row was read stays in the file.
    assert left_out == 0 and rows[0]["updated_at"] == "2026-10-11"
    assert rows[0]["source_url"] is None
