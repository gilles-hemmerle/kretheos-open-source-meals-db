"""End-to-end tests: the whole build on the miniature sources.

The publishers are played by a local web server. The tests cover what the
database promises: every record of every source, one food per barcode,
identifiers that do not change from one build to the next, a rebuild from the
stored copies when a publisher is gone, and the Kratheos layer applied on top
without ever touching a source.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

import duckdb
import openpyxl
import pytest
from builders import SWISS_VALUE_HEADERS
from conftest import FileServer, make_repository, quiet

from kratheos_food_data import cli, lookup, schema, sqlite_export, versions
from kratheos_food_data.build import BuildOptions, Workspace, run_build
from kratheos_food_data.kratheos import LayerError
from kratheos_food_data.mirror import parse_overrides

EXPECTED_RECORDS = {
    "usda": 10,
    "off": 7,
    "ciqual": 3,
    "swiss": 3,
    "cnf": 2,
    "cofid": 3,
    "chain": 0,  # no file in sources/chains/: the source is there and empty
}
CHAIN_FIXTURES = Path(__file__).parent / "fixtures" / "chains"
EXPECTED_FOODS = 26  # 16 generic foods, 7 barcodes, 3 products without a valid barcode


@pytest.fixture
def repository(tmp_path: Path, miniature_sources: Path, file_server: FileServer) -> Workspace:
    """Give a miniature repository, as after a fresh checkout."""
    return make_repository(tmp_path / "repository", miniature_sources, file_server)


def build(workspace: Workspace, **options: object) -> dict:
    """Run a build and return its manifest."""
    return run_build(workspace, BuildOptions(**options), log=quiet)


def from_nothing(workspace: Workspace) -> None:
    """Remove everything a build produces, keeping only the stored originals."""
    shutil.rmtree(workspace.work, ignore_errors=True)
    shutil.rmtree(workspace.dist, ignore_errors=True)


def checksum(path: Path) -> str:
    """Return the SHA-256 of a file."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def foods_by_id(workspace: Workspace) -> dict[str, tuple]:
    """Read every food of the built database, without its internal number."""
    database = sqlite3.connect(workspace.dist / "food.sqlite")
    try:
        columns = [row[1] for row in database.execute("PRAGMA table_info(foods)") if row[1] != "pk"]
        rows = database.execute(f"SELECT {', '.join(columns)} FROM foods").fetchall()
        return {row[0]: row for row in rows}
    finally:
        database.close()


def query(workspace: Workspace, sql: str, parameters: tuple | dict = ()) -> list[tuple]:
    """Run a query on the built database."""
    database = sqlite3.connect(workspace.dist / "food.sqlite")
    try:
        return database.execute(sql, parameters).fetchall()
    finally:
        database.close()


def read_identifiers(workspace: Workspace) -> list[str]:
    """Read the identifier list written by the build."""
    with gzip.open(workspace.dist / "identifiers.txt.gz", "rt", encoding="utf-8") as handle:
        return handle.read().split()


def test_build_holds_every_record_of_every_source(repository: Workspace) -> None:
    """Counts, files, storage and manifest of a first build from upstream."""
    manifest = build(repository)
    by_source = {entry["id"]: entry for entry in manifest["sources"]}
    assert {key: entry["records"]["records"] for key, entry in by_source.items()} == (
        EXPECTED_RECORDS
    )
    assert by_source["off"]["normalization"]["dropped"] == {"no_energy": 1, "no_name": 1}
    assert by_source["usda"]["records"]["by_dataset"] == {
        "branded": 4,
        "fndds": 2,
        "foundation": 2,
        "sr_legacy": 2,
    }
    statistics = manifest["statistics"]
    assert statistics["generic_foods"] == 16
    assert statistics["products"]["records"] == 12
    assert statistics["barcodes"]["distinct"] == 7
    assert statistics["barcodes"]["known_to_several_sources"] == 2
    assert statistics["barcodes"]["sources_agree"] == 1
    assert statistics["barcodes"]["sources_disagree"] == 1
    assert statistics["foods"]["foods"] == EXPECTED_FOODS
    assert statistics["countries"]["by_source"]["ciqual"]["share"] == 1.0

    # The originals are stored where the source list says, untouched.
    assert manifest["inputs"]["mode"] == "upstream"
    downloaded = [entry for entry in manifest["sources"] if entry["id"] != "chain"]
    assert all(entry["origin"] == "upstream" for entry in downloaded)
    assert by_source["chain"]["origin"] == "mirror" and by_source["chain"]["files"] == []
    assert (repository.sources_dir / "ciqual" / "alim.xml").exists()
    assert (repository.mirror / "usda.branded_food_csv.zip").exists()
    assert (repository.mirror / "off.food.parquet").exists()
    assert (repository.mirror / "MIRROR.json").exists()
    stored = by_source["ciqual"]["files"][0]
    assert stored["stored_as"] == "sources/ciqual/alim.xml"
    assert stored["sha256"] == checksum(repository.sources_dir / "ciqual" / "alim.xml")
    assert {entry["id"] for entry in manifest["sources_not_built"]} == {"bls", "foodrepo"}

    # Every output is listed with its checksum.
    outputs = {entry["file"]: entry for entry in manifest["outputs"]}
    assert set(outputs) == {
        "generic_foods.parquet",
        "generic_foods.csv.gz",
        "generic_foods.jsonl.gz",
        "products.parquet",
        "products_by_barcode.parquet",
        "food.sqlite",
        "identifiers.txt.gz",
    }
    for name, entry in outputs.items():
        assert entry["sha256"] == checksum(repository.dist / name)
    assert outputs["generic_foods.parquet"]["records"] == 16
    assert outputs["products.parquet"]["records"] == 12
    assert outputs["food.sqlite"]["records"] == EXPECTED_FOODS


def test_parquet_outputs_follow_the_schema_and_carry_no_contributor(
    repository: Workspace,
) -> None:
    """The files of the sources have the documented columns and no handle."""
    build(repository)
    con = duckdb.connect()
    for name, extra in (("generic_foods", []), ("products", []), ("products_by_barcode", True)):
        columns = [
            row[0]
            for row in con.execute(
                f"DESCRIBE SELECT * FROM read_parquet('{repository.dist / name}.parquet')"
            ).fetchall()
        ]
        expected = schema.output_names()
        if extra:
            expected = expected + [column.name for column in schema.BARCODE_EXTRA_COLUMNS]
        assert columns == expected, name
    with gzip.open(repository.dist / "generic_foods.jsonl.gz", "rt", encoding="utf-8") as handle:
        first = json.loads(handle.readline())
    assert first["id"] == "ciqual:13000" and first["names"]["en"] == "Apricot, pitted, raw"
    assert first["countries"] == ["FR"]
    for path in repository.dist.iterdir():
        data = gzip.decompress(path.read_bytes()) if path.suffix == ".gz" else path.read_bytes()
        assert b"a-contributor-handle" not in data, path.name
        assert b"another-contributor" not in data, path.name


def test_one_food_per_barcode_with_the_other_sources(repository: Workspace) -> None:
    """A barcode gives one food, identified by the barcode, whoever is preferred."""
    build(repository)
    foods = {row[0]: row for row in query(repository, "SELECT id, record_id, source FROM foods")}
    assert len(foods) == EXPECTED_FOODS
    # Open Food Facts is the more recent of the two sources for this one...
    assert foods["gtin:3017620422003"][1:] == ("off:3017620422003", "off")
    # ...and USDA for that one, written three different ways by the sources.
    assert foods["gtin:0049000028911"][1:] == ("usda:branded:0049000028911", "usda")
    cola = query(
        repository,
        "SELECT source_count, sources, other_records, sources_agree FROM foods WHERE id = ?",
        ("gtin:0049000028911",),
    )[0]
    assert cola == (2, '["off","usda"]', '["off:0049000028911"]', 0)
    agree = query(
        repository, "SELECT sources_agree FROM foods WHERE id = ?", ("gtin:3017620422003",)
    )
    assert agree == [(1,)]
    # Foods without a valid barcode are identified by their source record.
    for identifier in (
        "off:853784259806586",
        "usda:branded:NIELSENUK0002",
        "swiss:1045",
        "ciqual:13000",
        "usda:fndds:27510389",
        "usda:foundation:16158",
        "usda:sr_legacy:21237",
    ):
        assert foods[identifier][1] == identifier
    assert query(repository, "SELECT count(*) FROM foods WHERE barcode IS NOT NULL") == [(7,)]
    assert read_identifiers(repository) == sorted(foods)


def test_search_names_countries_and_display_name(repository: Workspace) -> None:
    """Search by any name of a food, filter by country, show the right name."""
    build(repository)
    database = repository.dist / "food.sqlite"

    def found(text: str) -> list[str]:
        """Return the identifiers found for a typed text."""
        return [food["id"] for food in lookup.find(database, text)]

    assert found("mandel") == ["swiss:273"]  # German name
    assert found("mandorla") == ["swiss:273"]  # Italian name
    assert found("amande") == ["swiss:273"]  # French name
    assert "ciqual:13000" in found("abri")  # first letters are enough
    assert found("muesli") == ["gtin:7610000123451"]  # diacritics are ignored
    assert found("ferrero") == ["gtin:3017620422003"]  # the brand is indexed too
    assert found("souffle fromage") == ["cnf:2"]  # every word must match
    assert found('"quoted" (odd) input*') == []  # punctuation is not FTS syntax

    (nutella,) = lookup.find(database, "3017620422003", language="en")
    assert nutella["id"] == "gtin:3017620422003"
    assert nutella["display_name"] == "Nutella hazelnut spread"
    (same,) = lookup.find(database, "gtin:3017620422003", language="de")
    assert same["display_name"] == "Nutella hazelnut spread"  # no German name: English
    (apricot,) = lookup.find(database, "ciqual:13000", language="fr")
    assert apricot["display_name"] == "Abricot, dénoyauté, cru"
    (bircher,) = lookup.find(database, "7610000123451", language="fr")
    assert bircher["display_name"] == "Bircher Müesli"  # neither French nor English

    view = query(
        repository,
        "SELECT lang, name FROM display_names WHERE id = ? AND lang IN ('de', 'fr') ORDER BY lang",
        ("ciqual:13000",),
    )
    assert view == [("de", "Apricot, pitted, raw"), ("fr", "Abricot, dénoyauté, cru")]
    helper = query(repository, "SELECT value FROM meta WHERE key = 'display_name_query'")[0][0]
    assert query(repository, helper, {"language": "it", "id": "swiss:273"}) == [("Mandorla",)]

    swiss = query(
        repository,
        "SELECT f.id FROM food_countries AS c JOIN foods AS f ON f.pk = c.food_pk"
        " WHERE c.country = 'CH' ORDER BY f.id",
    )
    assert [row[0] for row in swiss] == [
        "gtin:5449000000996",
        "gtin:7610000123451",
        "swiss:1045",
        "swiss:273",
        "swiss:605",
    ]
    origins = query(repository, "SELECT DISTINCT origin FROM names")
    assert origins == [("source",)]
    licences = dict(query(repository, "SELECT id, licence FROM sources"))
    assert licences["off"] == "ODbL-1.0" and licences["ciqual"] == "etalab-2.0"


def test_two_builds_from_nothing_give_the_same_identifiers(repository: Workspace) -> None:
    """Identifiers, and the whole database, are the same from build to build."""
    build(repository)
    first_foods = foods_by_id(repository)
    first_list = checksum(repository.dist / "identifiers.txt.gz")
    first_database = checksum(repository.dist / "food.sqlite")
    saved = repository.dist.parent / "identifiers-first.txt.gz"
    shutil.copy(repository.dist / "identifiers.txt.gz", saved)

    from_nothing(repository)
    build(repository)
    assert foods_by_id(repository) == first_foods  # each identifier on the same record
    assert checksum(repository.dist / "identifiers.txt.gz") == first_list
    assert checksum(repository.dist / "food.sqlite") == first_database
    result = sqlite_export.compare_identifiers(saved, repository.dist / "identifiers.txt.gz")
    assert result == {"old": EXPECTED_FOODS, "new": EXPECTED_FOODS, "added": [], "removed": []}


def test_rebuild_when_the_publishers_are_gone(
    repository: Workspace, file_server: FileServer
) -> None:
    """With every publisher gone, the stored copies rebuild the same database."""
    build(repository)
    database = checksum(repository.dist / "food.sqlite")
    for path in file_server.root.rglob("*"):
        if path.is_file():
            path.unlink()

    # Default mode: upstream fails, the stored copies are used, and it is said.
    from_nothing(repository)
    manifest = build(repository)
    assert checksum(repository.dist / "food.sqlite") == database
    assert all(entry["origin"] == "mirror" for entry in manifest["sources"])
    assert "upstream failed" in manifest["sources"][0]["files"][0]["origin_detail"]

    # Forced mirror: reproducible rebuild without a single request.
    from_nothing(repository)
    file_server.requests.clear()
    manifest = build(repository, from_mirror=True)
    assert file_server.requests == []
    assert manifest["inputs"]["mode"] == "mirror"
    assert checksum(repository.dist / "food.sqlite") == database


def _swiss_copy_with_one_more_food(original: Path, copy: Path) -> None:
    """Write a copy of the English Swiss workbook with one more generic food.

    Args:
        original: The stored workbook, which is only read.
        copy: Where to write the changed copy.
    """
    workbook = openpyxl.load_workbook(original)
    row = [999999, None, None, "Temporary test food", None, "Various", None]
    row.append("per 100g edible portion")
    for value in [837, 200, 8, 3, 20, 5, 2, 10, 0.5, 0, 200][: len(SWISS_VALUE_HEADERS)]:
        row += [value, "Estimation", 1]
    workbook.worksheets[0].append(row)
    workbook.save(copy)


def test_kratheos_layer_and_temporary_source_line_then_back(repository: Workspace) -> None:
    """The proof asked for: add to Kratheos and to a source, rebuild, undo, rebuild."""
    before = build(repository)
    baseline_foods = foods_by_id(repository)
    baseline_database = checksum(repository.dist / "food.sqlite")
    baseline_list = checksum(repository.dist / "identifiers.txt.gz")
    counts_before = {entry["id"]: entry["records"]["records"] for entry in before["sources"]}
    empty_layer = repository.kratheos.read_bytes()

    # One test translation and one test product in kratheos.sqlite.
    layer = sqlite3.connect(repository.kratheos)
    layer.execute(
        "INSERT INTO translations VALUES ('ciqual:13000', 'de', 'Aprikose, entsteint, roh',"
        " 'human', '2026-10-11')"
    )
    layer.execute(
        "INSERT INTO products (id, kind, name, name_lang, basis, energy_kcal, protein_g,"
        " carbohydrate_g, fat_g, countries, updated_at) VALUES ('test-1', 'product',"
        " 'Burger maison du test', 'fr', '100g', 250, 13, 25, 11, '[\"ch\"]', '2026-10-11')"
    )
    layer.commit()
    layer.close()

    # One temporary line in one source, on a copy: the stored original must not move.
    original = repository.sources_dir / "swiss" / "swiss_en.xlsx"
    original_checksum = checksum(original)
    copy = repository.work.parent / "swiss_en_with_one_more_line.xlsx"
    _swiss_copy_with_one_more_food(original, copy)
    overrides = parse_overrides([f"swiss/swiss_en.xlsx={copy}"])

    from_nothing(repository)
    after = build(repository, overrides=overrides)
    assert checksum(original) == original_checksum  # byte for byte the same
    counts_after = {entry["id"]: entry["records"]["records"] for entry in after["sources"]}
    assert counts_after == {**counts_before, "swiss": counts_before["swiss"] + 1}
    swiss = next(entry for entry in after["sources"] if entry["id"] == "swiss")
    assert swiss["origin"] == "override"
    assert after["inputs"]["overrides"] == {"swiss/swiss_en.xlsx": str(copy)}
    assert after["kratheos"]["statistics"]["products"] == 1
    assert after["kratheos"]["statistics"]["translations_applied"] == 1

    foods = foods_by_id(repository)
    assert set(foods) - set(baseline_foods) == {"swiss:999999", "kratheos:test-1"}
    assert set(baseline_foods) <= set(foods)  # every record of every source is still there
    assert {key: foods[key] for key in baseline_foods} == baseline_foods  # and unchanged
    product = query(
        repository,
        "SELECT source, kind, name, energy_kcal, countries, licence FROM foods WHERE id = ?",
        ("kratheos:test-1",),
    )
    assert product == [
        ("kratheos", "product", "Burger maison du test", 250.0, '["CH"]', "ODbL-1.0")
    ]
    names = query(
        repository,
        "SELECT n.lang, n.name, n.origin, n.kratheos_origin FROM names AS n"
        " JOIN foods AS f ON f.pk = n.food_pk WHERE f.id = 'ciqual:13000' ORDER BY n.lang",
    )
    assert names == [
        ("de", "Aprikose, entsteint, roh", "kratheos", "human"),
        ("en", "Apricot, pitted, raw", "source", None),
        ("fr", "Abricot, dénoyauté, cru", "source", None),
    ]
    database = repository.dist / "food.sqlite"
    assert [food["id"] for food in lookup.find(database, "aprikose")] == ["ciqual:13000"]
    assert lookup.find(database, "ciqual:13000", language="de")[0]["display_name"] == (
        "Aprikose, entsteint, roh"
    )
    assert [food["id"] for food in lookup.find(database, "burger maison")] == ["kratheos:test-1"]
    # The files of the sources hold nothing from Kratheos.
    con = duckdb.connect()
    in_sources = con.execute(
        f"SELECT count(*) FROM read_parquet('{repository.dist / 'products.parquet'}')"
        " WHERE source = 'kratheos'"
    ).fetchone()[0]
    assert in_sources == 0

    # Undo everything, rebuild from nothing: the build before the test, exactly.
    layer = sqlite3.connect(repository.kratheos)
    layer.execute("DELETE FROM translations")
    layer.execute("DELETE FROM products")
    layer.commit()
    layer.close()
    copy.unlink()
    from_nothing(repository)
    build(repository)
    assert foods_by_id(repository) == baseline_foods
    assert checksum(repository.dist / "identifiers.txt.gz") == baseline_list
    assert checksum(repository.dist / "food.sqlite") == baseline_database
    assert checksum(original) == original_checksum
    assert len(empty_layer) == repository.kratheos.stat().st_size


VERSION_INSERT = (
    "INSERT INTO versions (version_id, food_id, basis, energy_kcal, protein_g, carbohydrate_g,"
    " sugars_g, fat_g, saturated_fat_g, salt_g, serving_size, serving_unit, serving_text,"
    " countries, updated_at) VALUES (?, ?, '100g', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


def fill_layer_with_versions_and_votes(workspace: Workspace) -> None:
    """Write users' versions, votes, translations and a product into the layer."""
    layer = sqlite3.connect(workspace.kratheos)
    layer.executemany(
        VERSION_INSERT,
        [
            # Other values for a barcode two sources know, proposed for France.
            ("kratheos:v:aaa", "gtin:3017620422003", 530, 6, 56, 55, 31, 11, 0.1)
            + (15, "g", "15 g", '["fr"]', "2026-11-01"),
            # Other values for a generic food, with too few votes to be served.
            ("kratheos:v:bbb", "ciqual:13000", 48, 0.9, 9.5, 7, 0.4, 0.05, 0.01)
            + (None, None, None, None, "2026-11-02"),
            # Values that make no sense: kept, and flagged by the same checks.
            ("kratheos:v:ddd", "ciqual:25999", 2510, 13, 25, 3, 11, 4, 1)
            + (None, None, None, None, "2026-11-03"),
            ("kratheos:v:ccc", "nothing:here", 100, 1, 1, 1, 1, 1, 1)
            + (None, None, None, None, "2026-11-04"),
        ],
    )
    layer.executescript(
        """
        INSERT INTO votes VALUES ('kratheos:v:aaa', 'gtin:3017620422003', 4, '2026-11-05');
        INSERT INTO votes VALUES ('off:3017620422003', 'gtin:3017620422003', 2, '2026-11-05');
        INSERT INTO votes VALUES ('usda:branded:3017620422003', 'gtin:3017620422003', 1,
                                  '2026-11-05');
        INSERT INTO votes VALUES ('kratheos:v:bbb', 'ciqual:13000', 2, '2026-11-05');
        INSERT INTO votes VALUES ('kratheos:v:ddd', 'ciqual:25999', 9, '2026-11-05');
        INSERT INTO votes VALUES ('swiss:273', 'swiss:273', 7, '2026-11-05');
        INSERT INTO votes VALUES ('nothing:voted', 'nothing:here', 3, '2026-11-05');
        INSERT INTO translations VALUES ('ciqual:13000', 'fr', 'Autre nom', 'machine',
                                         '2026-11-01');
        INSERT INTO translations VALUES ('ciqual:13000', 'it', 'Albicocca', 'machine',
                                         '2026-11-01');
        INSERT INTO translations VALUES ('gtin:3017620422003', 'de', 'Nutella', 'human',
                                         '2026-11-01');
        INSERT INTO translations VALUES ('nothing:here', 'de', 'Nichts', 'human', '2026-11-01');
        INSERT INTO products (id, barcode, name, name_lang, basis, energy_kcal, protein_g,
                              carbohydrate_g, sugars_g, fat_g, countries, updated_at)
            VALUES ('cola-ch', '5449000000996', 'Coca-Cola (Suisse)', 'fr', '100ml', 42, 0,
                    10.6, 10.6, 0, '["CH"]', '2026-12-01');
        """
    )
    layer.commit()
    layer.close()


def versions_of(workspace: Workspace, food_id: str) -> list[dict]:
    """Read the versions of a food from the built database, in build order."""
    database = sqlite3.connect(workspace.dist / "food.sqlite")
    database.row_factory = sqlite3.Row
    try:
        rows = database.execute(
            "SELECT v.* FROM versions AS v JOIN foods AS f ON f.pk = v.food_pk"
            " WHERE f.id = ? ORDER BY v.position",
            (food_id,),
        ).fetchall()
        return [
            dict(row, countries=json.loads(row["countries"]) if row["countries"] else [])
            for row in rows
        ]
    finally:
        database.close()


def test_versions_votes_and_translations_rules(repository: Workspace) -> None:
    """Every version is kept, the most voted is served, names are never replaced."""
    fill_layer_with_versions_and_votes(repository)
    manifest = build(repository)
    assert manifest["kratheos"]["statistics"] == {
        "products": 1,
        "products_with_valid_barcode": 1,
        "versions": 4,
        "versions_applied": 3,
        "versions_without_target": 1,
        "votes": 7,
        "votes_applied": 6,
        "votes_without_target": 1,
        "translations": 4,
        "translations_applied": 2,
        "translations_ignored_name_already_given": 1,
        "translations_without_target": 1,
        "user_version_votes": 3,
        "foods_with_several_versions": 5,
        "foods_served_from_a_user_version": 2,
        "products_dropped": {},
    }

    # Two sources and a user's version: nothing is overwritten, the most voted is served.
    listed = versions_of(repository, "gtin:3017620422003")
    # In the order of the build: the sources, the most recent first, then the users.
    assert [(v["version_id"], v["origin"], v["votes"], v["served"]) for v in listed] == [
        ("off:3017620422003", "source", 2, 0),
        ("usda:branded:3017620422003", "source", 1, 0),
        ("kratheos:v:aaa", "user", 4, 1),
    ]
    assert [(v["energy_kcal"], v["countries"]) for v in listed] == [
        (539.0, ["FR", "GB"]),
        (541.0, ["US"]),
        (530.0, ["FR"]),
    ]
    nutella = query(
        repository,
        "SELECT record_id, source, name, brand, energy_kcal, carbohydrate_g, serving_text,"
        " countries, licence, updated_at, votes, versions_count, plausible, flags,"
        " other_records FROM foods WHERE id = 'gtin:3017620422003'",
    )[0]
    assert nutella == (
        "kratheos:v:aaa",
        "kratheos",
        "Nutella",  # who the food is does not change with the votes
        "Nutella, Ferrero",
        530.0,
        56.0,
        "15 g",
        '["FR"]',
        "ODbL-1.0",
        "2026-11-01",
        4,
        3,
        1,
        "[]",
        '["usda:branded:3017620422003"]',
    )

    # Below the threshold a user's version is kept, listed, and not served.
    apricot = query(
        repository,
        "SELECT record_id, source, energy_kcal, votes, versions_count FROM foods"
        " WHERE id = 'ciqual:13000'",
    )
    assert apricot == [("ciqual:13000", "ciqual", 45.9, 0, 2)]
    assert [
        (v["version_id"], v["votes"], v["served"]) for v in versions_of(repository, "ciqual:13000")
    ] == [("ciqual:13000", 0, 1), ("kratheos:v:bbb", 2, 0)]

    # A version that makes no sense is served when people vote for it, and flagged.
    hamburger = query(
        repository,
        "SELECT record_id, energy_kcal, plausible, flags FROM foods WHERE id = 'ciqual:25999'",
    )[0]
    assert hamburger[:3] == ("kratheos:v:ddd", 2510.0, 0)
    assert schema.FLAG_ENERGY_OUT_OF_RANGE in json.loads(hamburger[3])

    # Votes for the only version of a food are kept on the food itself.
    assert query(repository, "SELECT votes, versions_count FROM foods WHERE id = 'swiss:273'") == [
        (7, 1)
    ]
    assert versions_of(repository, "swiss:273") == []

    names = query(
        repository,
        "SELECT f.id, n.lang, n.name, n.origin, n.kratheos_origin FROM names AS n"
        " JOIN foods AS f ON f.pk = n.food_pk"
        " WHERE f.id IN ('ciqual:13000', 'gtin:3017620422003') ORDER BY f.id, n.lang",
    )
    assert names == [
        ("ciqual:13000", "en", "Apricot, pitted, raw", "source", None),
        ("ciqual:13000", "fr", "Abricot, dénoyauté, cru", "source", None),  # not replaced
        ("ciqual:13000", "it", "Albicocca", "kratheos", "machine"),
        ("gtin:3017620422003", "de", "Nutella", "kratheos", "human"),
        ("gtin:3017620422003", "en", "Nutella hazelnut spread", "source", None),
        ("gtin:3017620422003", "fr", "Nutella", "source", None),
    ]

    # A product added in Kratheos under a barcode a source knows is one more
    # version of that food: without votes the record of the source is served.
    cola = query(
        repository,
        "SELECT record_id, source, name, other_records, source_count, votes, versions_count"
        " FROM foods WHERE id = 'gtin:5449000000996'",
    )
    assert cola == [
        ("off:5449000000996", "off", "Coca-Cola Original", '["kratheos:cola-ch"]', 2, 0, 2)
    ]
    assert [
        (v["version_id"], v["origin"], v["served"])
        for v in versions_of(repository, "gtin:5449000000996")
    ] == [("off:5449000000996", "source", 1), ("kratheos:cola-ch", "user", 0)]
    assert query(repository, "SELECT count(*) FROM foods") == [(EXPECTED_FOODS,)]
    assert query(repository, "SELECT value FROM meta WHERE key = 'user_version_votes'") == [("3",)]


def test_served_version_is_the_same_in_the_build_and_in_python(repository: Workspace) -> None:
    """The SQL of the build and `versions.choose_served` agree, whatever the threshold."""
    fill_layer_with_versions_and_votes(repository)
    layer = sqlite3.connect(repository.kratheos)
    # Equal votes between a source and a user's version.
    layer.execute("UPDATE votes SET votes = 4 WHERE version_id = 'off:3017620422003'")
    # A product nobody else knows and a later proposal for it, with equal votes.
    layer.execute(
        "INSERT INTO products (id, name, name_lang, basis, energy_kcal, protein_g,"
        " carbohydrate_g, fat_g, countries, updated_at) VALUES ('plat', 'Plat du test', 'fr',"
        " '100g', 150, 8, 12, 7, '[\"CH\"]', '2026-10-11')"
    )
    layer.executemany(
        VERSION_INSERT,
        [
            ("kratheos:v:eee", "kratheos:plat", 160, 8, 13, 2, 8, 2, 0.5)
            + (None, None, None, '["CH"]', "2026-10-20"),
            # The only source of this barcode gives values that make no sense (50 kcal
            # for 20 g of fat): one person's plausible entry, without a vote, replaces it.
            ("kratheos:v:fff", "gtin:7610000123451", 460, 10, 60, 30, 20, 4, 0.2)
            + (None, None, None, None, "2026-10-15"),
            # The same on a generic food, where the entry of the user makes no sense
            # either: nothing is usable, and the source stays in front.
            ("kratheos:v:ggg", "cofid:17-063", 40, 0, 105, 105, 0, 0, 0)
            + (None, None, None, None, "2026-10-16"),
        ],
    )
    layer.executescript(
        """
        INSERT INTO votes VALUES ('kratheos:plat', 'kratheos:plat', 2, '2026-10-21');
        INSERT INTO votes VALUES ('kratheos:v:eee', 'kratheos:plat', 2, '2026-10-21');
        """
    )
    layer.commit()
    layer.close()
    served: dict[int, dict[str, str]] = {}
    for threshold in (1, 3, 5):
        from_nothing(repository)
        build(repository, user_version_votes=threshold)
        database = sqlite3.connect(repository.dist / "food.sqlite")
        priorities = dict(database.execute("SELECT id, priority FROM sources"))
        several = [
            row[0]
            for row in database.execute("SELECT id FROM foods WHERE versions_count > 1 ORDER BY id")
        ]
        database.close()
        assert priorities["usda"] == 1 and priorities["kratheos"] == 1000
        assert len(several) == 8
        served[threshold] = {}
        for food_id in several:
            listed = versions_of(repository, food_id)
            for version in listed:
                version["plausible"] = bool(version["plausible"])
                version["has_macros"] = bool(version["has_macros"])
            assert versions.ordered(listed, priorities) == listed, food_id
            chosen = versions.choose_served(listed, None, threshold)
            assert [v["version_id"] for v in listed if v["served"]] == [chosen["version_id"]]
            record = query(repository, "SELECT record_id FROM foods WHERE id = ?", (food_id,))
            assert record == [(chosen["version_id"],)], (food_id, threshold)
            served[threshold][food_id] = chosen["version_id"]
    expected = {
        # Equal votes (4 and 4): the source stays; a user's version needs strictly more.
        "gtin:3017620422003": "off:3017620422003",
        "gtin:0049000028911": "usda:branded:0049000028911",
        # A product added under a barcode a source knows, without votes.
        "gtin:5449000000996": "off:5449000000996",
        # No usable source: the plausible entry of one person is served without a vote...
        "gtin:7610000123451": "kratheos:v:fff",
        # ...and an entry that makes no sense either stays behind the source.
        "cofid:17-063": "cofid:17-063",
        # 9 votes for values that make no sense: the votes decide first.
        "ciqual:25999": "kratheos:v:ddd",
        # Two users' versions with equal votes: the oldest, whatever came later.
        "kratheos:plat": "kratheos:plat",
    }
    # With one vote required the proposal of 2 votes passes the source of none.
    assert served[1] == {**expected, "ciqual:13000": "kratheos:v:bbb"}
    assert served[3] == served[5] == {**expected, "ciqual:13000": "ciqual:13000"}
    fixed = versions_of(repository, "gtin:7610000123451")
    assert [(v["version_id"], v["plausible"], v["votes"]) for v in fixed] == [
        ("kratheos:v:fff", 1, 0),  # plausible values come first in the order of the build
        ("off:7610000123451", 0, 0),
    ]
    assert query(
        repository, "SELECT source, energy_kcal, name FROM foods WHERE id = 'gtin:7610000123451'"
    ) == [("kratheos", 460.0, "Bircher Müesli")]
    # A version that lists no country can be the one of a country another version lists:
    # with one vote required, the proposal of 2 votes is served to France too.
    apricot = versions_of(repository, "ciqual:13000")
    for version in apricot:
        version["plausible"], version["has_macros"] = True, True
    assert [(v["version_id"], v["countries"]) for v in apricot] == [
        ("ciqual:13000", ["FR"]),
        ("kratheos:v:bbb", []),
    ]
    assert versions.choose_served(apricot, "FR", 1)["version_id"] == "kratheos:v:bbb"
    assert versions.choose_served(apricot, "FR", 3)["version_id"] == "ciqual:13000"


def a_version(
    version_id: str,
    countries: tuple[str, ...] = (),
    votes: int = 0,
    plausible: bool = True,
    has_macros: bool = True,
    updated_at: str = "2026-01-01",
) -> dict:
    """Describe a version the way `versions.choose_served` reads it."""
    source = version_id.split(":")[0]
    return {
        "version_id": version_id,
        "source": source,
        "origin": versions.origin_of(source),
        "votes": votes,
        "countries": list(countries),
        "plausible": plausible,
        "has_macros": has_macros,
        "updated_at": updated_at,
    }


PRIORITIES = {"usda": 1, "off": 2, "kratheos": 1000}


def test_threshold_only_protects_usable_sources() -> None:
    """A user's version waits for its votes while a source has plausible, complete values."""
    french = a_version("off:1", ("FR", "GB"))
    american = a_version("usda:1", ("US",), votes=2)
    proposed = a_version("kratheos:v:1", ("CH",), votes=2)
    listed = [french, american, proposed]
    assert versions.choose_served(listed) is american  # the most voted of those that count
    assert versions.choose_served(listed, user_version_votes=2) is american  # 2 and 2: the source
    proposed["votes"] = 3
    assert versions.choose_served(listed) is proposed
    american["votes"] = 3
    assert versions.choose_served(listed) is american  # strictly more votes are needed
    assert versions.listing_order(listed, french) == [french, american, proposed]
    assert versions.choose_served([]) is None

    # No usable source (implausible, or without the four macros): every user's version counts.
    for flaw in ({"plausible": False}, {"has_macros": False}):
        broken = a_version("off:2", ("FR",), **flaw)
        entry = a_version("kratheos:v:2", ("FR",))
        assert versions.usable(entry) and not versions.usable(broken)
        assert versions.choose_served([broken, entry]) is entry  # without a single vote
        broken["votes"] = 1
        assert versions.choose_served([broken, entry]) is broken  # votes still come first
    # One usable source among several is enough for the threshold to apply.
    assert (
        versions.choose_served(
            [a_version("off:3", plausible=False), french, a_version("kratheos:v:3", votes=1)]
        )
        is french
    )


def test_country_never_trades_usable_values_for_unusable_ones() -> None:
    """A person gets the version of their market, unless only another one can be relied on."""
    french = a_version("off:1", ("FR", "GB"))
    american = a_version("usda:1", ("US",), votes=2)
    proposed = a_version("kratheos:v:1", ("CH",), votes=2)
    listed = [french, american, proposed]
    assert versions.choose_served(listed, "fr") is french  # the one of that market
    assert versions.choose_served(listed, "US") is american
    assert versions.choose_served(listed, "DE") is american  # nobody lists it: all of them
    # Below the threshold a user's version is not served, even to the only country listing it.
    assert versions.choose_served(listed, "CH") is american
    assert versions.choose_served(listed, "CH", user_version_votes=2) is proposed

    # The record of the country is implausible or incomplete and another one is usable:
    # the country does not filter.
    for flaw in ({"plausible": False}, {"has_macros": False}):
        local = a_version("off:2", ("FR",), **flaw)
        foreign = a_version("usda:2", ("US",))
        assert versions.choose_served([foreign, local], "FR") is foreign
        # Nothing is usable anywhere: the record of the country is as good as any, and wins.
        foreign["plausible"] = False
        assert versions.choose_served([foreign, local], "FR") is local

    # A version that lists no country is not "elsewhere": it stays, behind the one that
    # lists the country when nothing else tells them apart, in front with more votes.
    local = a_version("usda:3", ("US",), updated_at="2020-01-01")
    unknown = a_version("off:3", (), updated_at="2026-01-01")
    elsewhere = a_version("off:4", ("FR",), votes=5)
    listed = versions.ordered([local, unknown, elsewhere], PRIORITIES)
    assert listed[0] is unknown  # the most recent comes first in the order of the build
    assert versions.choose_served(listed, "US") is local  # the 5 votes are for another country
    assert versions.choose_served(listed) is elsewhere
    unknown["votes"] = 1
    assert versions.choose_served(listed, "US") is unknown

    # Nobody lists the country and nothing is usable: unknown passes elsewhere, and when
    # every version is of another country none is dropped.
    abroad = a_version("usda:5", ("US",), plausible=False, updated_at="2026-06-01")
    nowhere = a_version("off:5", (), plausible=False, updated_at="2020-01-01")
    assert versions.choose_served(versions.ordered([abroad, nowhere], PRIORITIES)) is abroad
    assert versions.choose_served(versions.ordered([abroad, nowhere], PRIORITIES), "FR") is nowhere
    assert versions.choose_served([abroad], "FR") is abroad


def test_order_of_the_build_puts_sources_first_and_the_oldest_proposal_first() -> None:
    """Sources by date, then users by age: a later proposal never passes an earlier one."""
    older_source = a_version("usda:1", updated_at="2023-02-01")
    recent_source = a_version("off:1", updated_at="2026-10-10")
    undated_source = a_version("off:2", updated_at="")
    first_proposal = a_version("kratheos:v:b", updated_at="2026-10-12")
    later_proposal = a_version("kratheos:v:a", updated_at="2026-10-13")
    same_day = a_version("kratheos:v:c", updated_at="2026-10-12")
    product = a_version("kratheos:1234", updated_at="2026-10-12")
    incomplete = a_version("usda:9", has_macros=False, updated_at="2026-12-31")
    implausible = a_version("usda:8", plausible=False, updated_at="2026-12-31")
    everything = [later_proposal, implausible, same_day, older_source, incomplete, product]
    everything += [first_proposal, undated_source, recent_source]
    assert versions.ordered(everything, PRIORITIES) == [
        recent_source,
        older_source,
        undated_source,
        product,  # the same day as two proposals: the identifier decides
        first_proposal,
        same_day,
        later_proposal,
        incomplete,
        implausible,
    ]
    # Equal votes among users' versions: the oldest keeps its place.
    proposals = versions.ordered([later_proposal, first_proposal], PRIORITIES)
    for version in proposals:
        version["votes"] = 1
    assert versions.choose_served(proposals) is first_proposal
    later_proposal["votes"] = 2
    assert versions.choose_served(proposals) is later_proposal


def test_versions_disagree_uses_the_tolerance_of_the_sources() -> None:
    """Versions differ beyond the rounding of a label, or when their basis differs."""
    base = {"basis": "100g", "has_macros": True, "energy_kcal": 539, "protein_g": 6.3}
    base |= {"carbohydrate_g": 57.5, "fat_g": 30.9}
    assert not versions.disagree([base])
    assert not versions.disagree([base, dict(base, energy_kcal=545, fat_g=32.4)])
    assert versions.disagree([base, dict(base, energy_kcal=480)])
    assert versions.disagree([base, dict(base, protein_g=12)])
    assert versions.disagree([base, dict(base, basis="100ml")])
    assert not versions.disagree([base, dict(base, energy_kcal=10, has_macros=False)])


def test_chain_files_become_products_without_barcode(repository: Workspace) -> None:
    """The files of sources/chains/ are read as they are, and nothing without them."""
    baseline = build(repository)
    assert baseline["statistics"]["foods"]["foods"] == EXPECTED_FOODS
    shutil.copytree(CHAIN_FIXTURES, repository.sources_dir / "chains")
    manifest = build(repository)
    chain = next(entry for entry in manifest["sources"] if entry["id"] == "chain")
    assert [entry["stored_as"] for entry in chain["files"]] == [
        "sources/chains/exemple_ch.csv",
        "sources/chains/exemple_ch_de.csv",
        "sources/chains/exemple_us.csv",
    ]
    assert chain["files"][0]["sha256"] == checksum(CHAIN_FIXTURES / "exemple_ch.csv")
    assert chain["version"] == "3 files, collected up to 2026-10-12"
    assert chain["adapter"] == {
        "files": {"exemple_ch.csv": 6, "exemple_ch_de.csv": 1, "exemple_us.csv": 1},
        "rows_in_files": 8,
        "repeated_products_skipped": 1,
        "products_identified_by_name": 1,
        "rows_staged": 6,
    }
    assert chain["normalization"] == {"read": 6, "kept": 5, "dropped": {"no_energy": 1}}
    foods = {
        row[0]: row[1:]
        for row in query(
            repository,
            "SELECT id, kind, barcode, name, name_lang, brand, category, basis, energy_kcal,"
            " carbohydrate_g, carbohydrate_total_g, serving_size, serving_unit, is_drink,"
            " countries, source_url, updated_at, plausible, licence FROM foods"
            " WHERE source = 'chain'",
        )
    }
    assert set(foods) == {
        "chain:chaine-exemple:ch:b-001",
        "chain:chaine-exemple:ch:frites-moyennes",  # the site gives no key: the name
        "chain:chaine-exemple:ch:d-007",
        "chain:chaine-exemple:ch:w-009",
        "chain:chaine-exemple:us:b-001",
    }
    assert foods["chain:chaine-exemple:ch:b-001"] == (
        "product",
        None,
        "Burger du test",
        "fr",
        "Chaîne Exemple",
        "Burgers",
        "100g",
        250.0,
        20.0,
        None,
        200.0,
        "g",
        0,
        '["CH"]',
        "https://example.org/ch/fr/burger-du-test",
        "2026-10-11",
        1,
        "publisher-terms",
    )
    drink = foods["chain:chaine-exemple:ch:d-007"]
    assert (drink[6], drink[10], drink[11], drink[12]) == ("100ml", 400.0, "ml", 1)
    # A menu of the United States gives total carbohydrate: fibre is subtracted.
    american = foods["chain:chaine-exemple:us:b-001"]
    assert (american[8], american[9], american[10], american[11]) == (20.0, 22.0, None, None)
    assert foods["chain:chaine-exemple:ch:w-009"][16] == 0  # 4 kcal for these macros
    names = query(
        repository,
        "SELECT n.lang, n.name FROM names AS n JOIN foods AS f ON f.pk = n.food_pk"
        " WHERE f.id = 'chain:chaine-exemple:ch:b-001' ORDER BY n.lang",
    )
    assert names == [("de", "Testburger"), ("fr", "Burger du test")]  # one file per language
    database = repository.dist / "food.sqlite"
    assert [food["id"] for food in lookup.find(database, "frites moy")] == [
        "chain:chaine-exemple:ch:frites-moyennes"
    ]
    assert "chain:chaine-exemple:ch:b-001" in [
        food["id"] for food in lookup.find(database, "chaine exemple")
    ]
    assert manifest["statistics"]["foods"]["foods"] == EXPECTED_FOODS + 5


def test_layer_with_an_unknown_column_is_refused(repository: Workspace) -> None:
    """Nothing identifying a person can slip in: the layout is closed."""
    layer = sqlite3.connect(repository.kratheos)
    layer.execute("ALTER TABLE votes ADD COLUMN user_id TEXT")
    layer.commit()
    layer.close()
    with pytest.raises(LayerError, match="votes"):
        build(repository)


def test_layer_identifiers_cannot_be_mistaken(repository: Workspace) -> None:
    """A product is never named like a version, nor a version like a record."""
    layer = sqlite3.connect(repository.kratheos)
    with pytest.raises(sqlite3.IntegrityError):
        layer.execute(
            "INSERT INTO products (id, name, name_lang, basis, updated_at)"
            " VALUES ('v:1', 'x', 'fr', '100g', '2026-10-11')"
        )
    with pytest.raises(sqlite3.IntegrityError):
        layer.execute(
            "INSERT INTO versions (version_id, food_id, basis, energy_kcal, updated_at)"
            " VALUES ('off:123', 'gtin:3017620422003', '100g', 100, '2026-10-11')"
        )
    layer.close()


def test_command_line(
    repository: Workspace, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The commands work from the command line, on a built repository."""
    root = str(repository.sources_file.parent)
    assert cli.main(["--root", root, "build", "--only", "ciqual,swiss"]) == 0
    assert "Done in" in capsys.readouterr().out
    assert cli.main(["--root", root, "lookup", "amande", "--lang", "it"]) == 0
    assert "swiss:273: Mandorla" in capsys.readouterr().out
    assert cli.main(["--root", root, "lookup", "13000", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []  # not a barcode, not a name
    assert cli.main(["--root", root, "sources"]) == 0
    assert "disabled" in capsys.readouterr().out
    assert cli.main(["--root", root, "kratheos-init"]) == 0
    assert "already exists" in capsys.readouterr().out

    first = tmp_path / "first.txt.gz"
    shutil.copy(repository.dist / "identifiers.txt.gz", first)
    assert cli.main(["--root", root, "build", "--only", "ciqual"]) == 0
    capsys.readouterr()
    new = str(repository.dist / "identifiers.txt.gz")
    assert cli.main(["--root", root, "ids-diff", str(first), new]) == 0
    output = capsys.readouterr().out
    assert "old: 6 identifiers; new: 3" in output
    assert "removed: 3" in output and "swiss:273" in output

    assert cli.main(["--root", root, "build", "--only", "bls"]) == 1
    assert "disabled" in capsys.readouterr().err
    assert cli.main(["--root", root, "build", "--override", "swiss/nope.xlsx=/absent"]) == 1
    assert "does not exist" in capsys.readouterr().err
