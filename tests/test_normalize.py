"""Tests of the normalisation: derivations, conventions and quality flags."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from kratheos_food_data import schema
from kratheos_food_data.adapters import AdapterContext
from kratheos_food_data.adapters.base import write_staging_rows
from kratheos_food_data.config import Source
from kratheos_food_data.names import display_name
from kratheos_food_data.normalize import normalize_source

BASE = {
    "kind": "generic",
    "names": {"en": "Food"},
    "main_lang": "en",
    "basis": "100g",
    "energy_kcal": 200.0,
    "protein_g": 10.0,
    "carbohydrate_g": 20.0,
    "fat_g": 8.0,
}


def _normalize(
    con: duckdb.DuckDBPyConnection, tmp_path: Path, source: Source, rows: list[dict]
) -> tuple[dict[str, dict], dict]:
    """Stage rows and normalise them.

    Args:
        con: DuckDB connection with the barcode macros.
        tmp_path: Temporary directory of the test.
        source: Source the rows are attributed to.
        rows: Staging rows; `source_record_id` is added when missing.

    Returns:
        The normalised records by source record id, and the counts.
    """
    staged = [{"source_record_id": str(number), **row} for number, row in enumerate(rows)]
    context = AdapterContext(
        con=con, files={}, work_dir=tmp_path, staging_path=tmp_path / "staging.parquet"
    )
    write_staging_rows(context, staged)
    out = tmp_path / "normalized.parquet"
    counts = normalize_source(con, source, context.staging_path, out)
    cursor = con.execute(f"SELECT * FROM read_parquet('{out}')")
    names = [description[0] for description in cursor.description]
    records = [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]
    return {record["source_record_id"]: record for record in records}, counts


@pytest.fixture
def source(declared_sources: dict[str, Source]) -> Source:
    """Give a real source to attribute the test rows to."""
    return declared_sources["ciqual"]


def test_output_schema_and_identity(
    con: duckdb.DuckDBPyConnection, tmp_path: Path, source: Source
) -> None:
    """A normalised file has exactly the output columns, and stable ids."""
    records, counts = _normalize(con, tmp_path, source, [BASE])
    record = records["0"]
    assert list(record) == schema.output_names()
    assert record["id"] == "ciqual:0" and record["source"] == "ciqual"
    assert record["licence"] == "etalab-2.0"
    assert record["has_macros"] is True and record["plausible"] is True
    assert record["flags"] == []
    assert counts == {"read": 1, "kept": 1, "dropped": {}}


def test_records_without_name_or_energy_are_dropped_and_counted(
    con: duckdb.DuckDBPyConnection, tmp_path: Path, source: Source
) -> None:
    """Only records with a name and an energy value are kept."""
    rows = [BASE, {**BASE, "names": {}}, {**BASE, "energy_kcal": None}, {**BASE, "names": None}]
    records, counts = _normalize(con, tmp_path, source, rows)
    assert set(records) == {"0"}
    assert counts["dropped"] == {"no_energy": 1, "no_name": 2}


def test_derived_values_are_flagged(
    con: duckdb.DuckDBPyConnection, tmp_path: Path, source: Source
) -> None:
    """Energy from kJ, salt from sodium and alcohol from % vol say so."""
    rows = [
        {**BASE, "energy_kcal": None, "energy_kj": 836.8, "sodium_g": 0.4},
        {**BASE, "salt_g": 1.0, "sodium_g": 0.4, "alcohol_abv": 0.0},
        {
            **BASE,
            "energy_kcal": 82.0,
            "protein_g": 0.1,
            "carbohydrate_g": 2.6,
            "fat_g": 0.0,
            "alcohol_abv": 12.5,
        },
    ]
    records, _ = _normalize(con, tmp_path, source, rows)
    assert records["0"]["energy_kcal"] == 200.0
    assert records["0"]["salt_g"] == 1.0
    assert set(records["0"]["flags"]) == {
        schema.FLAG_ENERGY_FROM_KJ,
        schema.FLAG_SALT_FROM_SODIUM,
    }
    assert records["1"]["salt_g"] == 1.0 and records["1"]["flags"] == []
    assert records["1"]["alcohol_g"] == 0.0  # zero is a value, not a derivation worth a flag
    assert records["2"]["alcohol_g"] == pytest.approx(9.86)
    assert records["2"]["flags"] == [schema.FLAG_ALCOHOL_FROM_ABV]
    assert records["2"]["plausible"] is True  # alcohol explains the energy


def test_carbohydrate_convention(
    con: duckdb.DuckDBPyConnection, tmp_path: Path, source: Source
) -> None:
    """Carbohydrate is available carbohydrate; a total is converted when it can be."""
    rows = [
        {**BASE, "carbohydrate_includes_fiber": False, "fiber_g": 5.0},
        {**BASE, "carbohydrate_includes_fiber": True, "fiber_g": 5.0},
        {**BASE, "carbohydrate_includes_fiber": True},
        {**BASE, "carbohydrate_includes_fiber": True, "carbohydrate_g": 3.0, "fiber_g": 4.0},
    ]
    records, _ = _normalize(con, tmp_path, source, rows)
    assert records["0"]["carbohydrate_g"] == 20.0
    assert records["0"]["carbohydrate_total_g"] is None
    assert records["0"]["flags"] == []
    assert records["1"]["carbohydrate_g"] == 15.0
    assert records["1"]["carbohydrate_total_g"] == 20.0
    assert records["1"]["flags"] == [schema.FLAG_CARBOHYDRATE_FIBER_SUBTRACTED]
    assert records["2"]["carbohydrate_g"] == 20.0
    assert records["2"]["carbohydrate_total_g"] == 20.0
    assert records["2"]["flags"] == [schema.FLAG_CARBOHYDRATE_INCLUDES_FIBER]
    assert records["3"]["carbohydrate_g"] == 0.0  # never negative


def test_energy_check_follows_the_convention(
    con: duckdb.DuckDBPyConnection, tmp_path: Path, source: Source
) -> None:
    """Fibre is counted apart at 2 kcal/g, with a margin for how labels count it."""
    fibre_rich = {"protein_g": 10.0, "carbohydrate_g": 10.0, "fat_g": 5.0, "fiber_g": 40.0}
    rows = [
        # European label: 4*10 + 4*10 + 9*5 + 2*40 = 205 kcal.
        {**BASE, **fibre_rich, "energy_kcal": 205.0},
        # United States label counting fibre at 4 kcal/g: 285 kcal for the same food.
        {**BASE, **fibre_rich, "energy_kcal": 285.0},
        # United States label counting insoluble fibre at 0: 125 kcal.
        {**BASE, **fibre_rich, "energy_kcal": 125.0},
        # Twice the energy the macros explain: kJ typed as kcal, or a wrong serving.
        {**BASE, **fibre_rich, "energy_kcal": 520.0},
        # No energy to speak of: the 10 kcal absorb the rounding.
        {**BASE, "energy_kcal": 3.0, "protein_g": 0.0, "carbohydrate_g": 2.5, "fat_g": 0.0},
        # Sugar-free sweets: polyols bring 2.4 kcal/g, or nothing.
        {
            **BASE,
            "energy_kcal": 230.0,
            "protein_g": 0.0,
            "carbohydrate_g": 96.0,
            "fat_g": 0.0,
            "polyols_g": 95.0,
        },
    ]
    records, _ = _normalize(con, tmp_path, source, rows)
    mismatch = schema.FLAG_ENERGY_MACRO_MISMATCH
    assert [mismatch in records[str(number)]["flags"] for number in range(6)] == [
        False,
        False,
        False,
        True,
        False,
        False,
    ]
    assert records["3"]["plausible"] is False


def test_range_and_part_checks(
    con: duckdb.DuckDBPyConnection, tmp_path: Path, source: Source
) -> None:
    """Out-of-range values and parts larger than their whole are flagged, not fixed."""
    rows = [
        {**BASE, "protein_g": 120.0},
        {**BASE, "energy_kcal": 950.0, "fat_g": 100.0, "protein_g": 0.0, "carbohydrate_g": 0.0},
        {**BASE, "sugars_g": 25.0},
        {**BASE, "sugars_g": 20.9},
        {**BASE, "saturated_fat_g": 9.5},
        {**BASE, "energy_kcal": 430.0, "protein_g": 30.0, "carbohydrate_g": 50.0, "fat_g": 30.0},
        # A converted total: sugars are compared with the figure of the source.
        {
            **BASE,
            "carbohydrate_includes_fiber": True,
            "carbohydrate_g": 20.0,
            "fiber_g": 15.0,
            "sugars_g": 8.0,
            "energy_kcal": 170.0,
        },
    ]
    records, _ = _normalize(con, tmp_path, source, rows)
    assert schema.FLAG_VALUE_OUT_OF_RANGE in records["0"]["flags"]
    assert records["0"]["protein_g"] == 120.0  # kept as given
    assert schema.FLAG_ENERGY_OUT_OF_RANGE in records["1"]["flags"]
    assert schema.FLAG_SUGARS_EXCEED_CARBOHYDRATE in records["2"]["flags"]
    assert records["3"]["flags"] == []  # within the 1 g allowed for rounding
    assert schema.FLAG_SATURATED_EXCEED_FAT in records["4"]["flags"]
    assert schema.FLAG_MACROS_SUM_OVER_100 in records["5"]["flags"]
    assert schema.FLAG_SUGARS_EXCEED_CARBOHYDRATE not in records["6"]["flags"]
    assert all(not records[str(number)]["plausible"] for number in (0, 1, 2, 4, 5))


def test_barcodes_names_and_countries(
    con: duckdb.DuckDBPyConnection, tmp_path: Path, source: Source
) -> None:
    """Barcodes are normalised or dropped; names and countries are tidied."""
    product = {**BASE, "kind": "product"}
    rows = [
        {**product, "barcode_raw": "049000028911", "countries": ["us", "FR", "us"]},
        {**product, "barcode_raw": "2000000002603"},
        {**product, "barcode_raw": "20095314"},
        {**product, "names": {"de": "Müesli", "it": "Muesli"}, "main_lang": "fr"},
        {**product, "names": {"ja": "ミューズリー"}, "main_lang": None, "countries": []},
    ]
    records, _ = _normalize(con, tmp_path, source, rows)
    assert records["0"]["barcode"] == "0049000028911"
    assert records["0"]["countries"] == ["FR", "US"]
    assert records["1"]["barcode"] is None
    assert records["1"]["flags"] == [schema.FLAG_BARCODE_INVALID]
    assert records["2"]["flags"] == [schema.FLAG_BARCODE_RESTRICTED]
    assert (records["3"]["name"], records["3"]["name_lang"]) == ("Müesli", "de")
    assert (records["4"]["name"], records["4"]["name_lang"]) == ("ミューズリー", "ja")
    assert records["4"]["countries"] is None


def test_duplicate_identifiers_are_refused(
    con: duckdb.DuckDBPyConnection, tmp_path: Path, source: Source
) -> None:
    """Two records of a source cannot share an identifier."""
    rows = [{**BASE, "source_record_id": "same"}, {**BASE, "source_record_id": "same"}]
    with pytest.raises(ValueError, match="duplicate"):
        _normalize(con, tmp_path, source, rows)


@pytest.mark.parametrize(
    ("names", "language", "default", "expected"),
    [
        ({"fr": "Abricot", "en": "Apricot"}, "fr", "Abricot", "Abricot"),
        ({"fr": "Abricot", "en": "Apricot"}, "de", "Abricot", "Apricot"),  # English next
        ({"fr": "Abricot", "it": "Albicocca"}, "de", "Abricot", "Abricot"),  # then the main name
        ({"fr": "Abricot"}, "fr-CH", None, "Abricot"),  # a regional code falls back
        ({"fr": "Abricot"}, "FR", None, "Abricot"),
        ({"it": "Albicocca", "fr": "Abricot"}, None, None, "Abricot"),  # any name at all
        ({}, "fr", None, None),
    ],
)
def test_display_name_rule(
    names: dict[str, str], language: str | None, default: str | None, expected: str | None
) -> None:
    """The person's language, else English, else whatever name the record has."""
    assert display_name(names, language, default) == expected
