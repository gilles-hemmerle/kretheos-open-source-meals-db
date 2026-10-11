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

from kratheos_food_data import cli, lookup, schema, sqlite_export
from kratheos_food_data.build import BuildOptions, Workspace, run_build
from kratheos_food_data.kratheos import LayerError
from kratheos_food_data.mirror import parse_overrides

EXPECTED_RECORDS = {"usda": 10, "off": 7, "ciqual": 3, "swiss": 3, "cnf": 2, "cofid": 3}
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
    assert all(entry["origin"] == "upstream" for entry in manifest["sources"])
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


def test_corrections_confirmations_and_translations_rules(repository: Workspace) -> None:
    """Corrections replace values, sources stay available, names are never replaced."""
    layer = sqlite3.connect(repository.kratheos)
    layer.executescript(
        """
        INSERT INTO corrections (record_id, names, energy_kcal, carbohydrate_g, updated_at)
            VALUES ('gtin:3017620422003', '{"fr": "Nutella (pot de 400 g)"}', 530, 56,
                    '2026-11-01');
        INSERT INTO corrections (record_id, energy_kcal, updated_at)
            VALUES ('ciqual:25999', 2510, '2026-11-01');
        INSERT INTO corrections (record_id, energy_kcal, updated_at)
            VALUES ('off:does-not-exist', 100, '2026-11-01');
        INSERT INTO confirmations VALUES ('ciqual:13000', 12, '2026-11-01');
        INSERT INTO confirmations VALUES ('nothing:here', 3, '2026-11-01');
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
    manifest = build(repository)
    assert manifest["kratheos"]["statistics"] == {
        "products": 1,
        "products_with_valid_barcode": 1,
        "corrections": 3,
        "corrections_applied": 2,
        "corrections_without_target": 1,
        "confirmations": 2,
        "confirmations_applied": 1,
        "confirmations_without_target": 1,
        "translations": 4,
        "translations_applied": 2,
        "translations_ignored_name_already_given": 1,
        "translations_without_target": 1,
        "products_dropped": {},
    }

    nutella = query(
        repository,
        "SELECT name, energy_kcal, carbohydrate_g, fat_g, flags, plausible, updated_at, record_id"
        " FROM foods WHERE id = 'gtin:3017620422003'",
    )[0]
    assert nutella[:4] == ("Nutella (pot de 400 g)", 530.0, 56.0, 30.9)
    assert json.loads(nutella[4]) == [schema.FLAG_KRATHEOS_CORRECTED]
    assert nutella[5:] == (1, "2026-11-01", "off:3017620422003")
    kept = query(
        repository,
        "SELECT name, energy_kcal, carbohydrate_g, source FROM source_values"
        " WHERE id = 'gtin:3017620422003'",
    )
    assert kept == [("Nutella", 539.0, 57.5, "off")]  # the source's own values stay available

    # A correction that makes no sense is applied, and flagged by the same checks.
    hamburger = query(
        repository, "SELECT energy_kcal, plausible, flags FROM foods WHERE id = 'ciqual:25999'"
    )[0]
    assert hamburger[:2] == (2510.0, 0)
    assert schema.FLAG_ENERGY_OUT_OF_RANGE in json.loads(hamburger[2])

    assert query(repository, "SELECT confirmations FROM foods WHERE id = 'ciqual:13000'") == [(12,)]
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
        ("gtin:3017620422003", "fr", "Nutella (pot de 400 g)", "kratheos", "correction"),
    ]

    # A Kratheos product with a barcode competes for it like any source record.
    cola = query(
        repository,
        "SELECT record_id, source, other_records, source_count FROM foods"
        " WHERE id = 'gtin:5449000000996'",
    )
    assert cola == [("kratheos:cola-ch", "kratheos", '["off:5449000000996"]', 2)]
    assert query(repository, "SELECT count(*) FROM foods") == [(EXPECTED_FOODS,)]


def test_layer_with_an_unknown_column_is_refused(repository: Workspace) -> None:
    """Nothing identifying a person can slip in: the layout is closed."""
    layer = sqlite3.connect(repository.kratheos)
    layer.execute("ALTER TABLE confirmations ADD COLUMN user_id TEXT")
    layer.commit()
    layer.close()
    with pytest.raises(LayerError, match="confirmations"):
        build(repository)


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
