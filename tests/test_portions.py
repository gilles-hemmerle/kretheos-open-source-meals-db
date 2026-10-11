"""Tests of the household portions, from the files of the sources to the service.

The miniature sources hold the cases the real files have: the three ways the
datasets of FoodData Central word a portion, the measures of the Canadian
file in two languages, and what must be left out (no weight, no wording, a
weight taken in the laboratory, "Quantity not specified", a label that is
only a weight, a repeated line).

Then the whole way: the table of `food.sqlite`, the food record of the
service with its list of portions, the portion a person adds (refused twice,
refused when implausible, ten per food), its moderation, and the loop of the
layer: add, export, rebuild, the same portions and no duplicate.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import duckdb
import pytest
from conftest import FileServer, make_repository, quiet
from service_tools import ALICE, BOB, KEY_SECRET, PRODUCT, TOKEN, Running, start_service

from kratheos_food_data import kratheos, schema, sqlite_export
from kratheos_food_data.adapters import get_adapter
from kratheos_food_data.build import BuildOptions, Workspace, run_build
from kratheos_food_data.config import Source
from kratheos_food_data.kratheos import LayerError
from kratheos_food_data.service import api, export, foods, live, maintenance, validation
from kratheos_food_data.service.live import LiveStore

HUMMUS = "usda:foundation:16158"
OIL = "usda:foundation:100147"
BIG_MAC = "usda:sr_legacy:21237"
COLA_US = "usda:sr_legacy:14400"
MILK = "usda:fndds:11111000"
CHEESEBURGER = "usda:fndds:27510389"
SOUFFLE = "cnf:2"
COLA_CA = "cnf:502"
APRICOT = "ciqual:13000"
COLA_BOTTLE = "gtin:5449000000996"
EGG = {
    "language": "fr",
    "label": "1 abricot moyen",
    "amount": 45,
    "unit": "g",
    "contributor": ALICE,
}


@pytest.fixture
def repository(tmp_path: Path, miniature_sources: Path, file_server: FileServer) -> Workspace:
    """Give a miniature repository, as after a fresh checkout."""
    return make_repository(tmp_path / "repository", miniature_sources, file_server)


@pytest.fixture
def service(built_repository: Workspace, tmp_path: Path) -> Iterator[Running]:
    """Give a running service over a copy of the miniature repository."""
    running = start_service(built_repository, tmp_path)
    yield running
    running.close()


def staged_portions(
    source_id: str, declared_sources: dict[str, Source], adapter_context: callable
) -> tuple[dict[str, list[tuple]], dict]:
    """Run the adapter of a source and read the portions it staged.

    Args:
        source_id: Identifier of the source.
        declared_sources: The real source list.
        adapter_context: Factory of adapter contexts.

    Returns:
        For each record, its portions in order as (labels, amount, unit),
        and the statistics the adapter reports about them.
    """
    source = declared_sources[source_id]
    context = adapter_context(source_id)
    result = get_adapter(source.adapter)(source, context)
    described = context.con.execute(
        f"DESCRIBE SELECT * FROM read_parquet('{context.portions_path}')"
    ).fetchall()
    assert [(row[0], row[1]) for row in described] == [
        (column.name, column.type) for column in schema.PORTION_COLUMNS
    ]
    rows = context.con.execute(
        f"SELECT * FROM read_parquet('{context.portions_path}') ORDER BY 1, 2"
    ).fetchall()
    staged: dict[str, list[tuple]] = {}
    for record, position, labels, amount, unit in rows:
        staged.setdefault(record, []).append((labels, amount, unit))
        assert position == len(staged[record])  # dense, from 1, in the order of the source
    return staged, result.stats["portions"]


def portions_of(database: Path, food_id: str) -> list[tuple]:
    """Read the portions of a food from a built database, in order.

    Args:
        database: Path of a `food.sqlite`.
        food_id: Identifier of the food.

    Returns:
        (portion_id, origin, labels, amount, unit) of each portion.
    """
    connection = sqlite3.connect(database)
    try:
        rows = connection.execute(
            "SELECT p.portion_id, p.origin, p.labels, p.amount, p.unit FROM portions AS p"
            " JOIN foods AS f ON f.pk = p.food_pk WHERE f.id = ? ORDER BY p.position",
            (food_id,),
        ).fetchall()
    finally:
        connection.close()
    return [(row[0], row[1], json.loads(row[2]), row[3], row[4]) for row in rows]


def test_usda_words_a_portion_the_way_of_each_dataset(
    declared_sources: dict[str, Source], adapter_context: callable
) -> None:
    """A measure and a precision, a free text, a whole wording; nothing guessed."""
    staged, stats = staged_portions("usda", declared_sources, adapter_context)
    # Foundation Foods: a number, a measure, sometimes a precision; a line
    # without order number comes after those that have one.
    assert staged["foundation:16158"] == [
        ({"en": "1 tablespoon"}, 15.0, "g"),
        ({"en": "2 tablespoon"}, 30.0, "g"),
        ({"en": "1 cup, commercial"}, 246.0, "g"),
    ]
    assert staged["foundation:100147"] == [({"en": "1 tablespoon"}, 13.5, "g")]
    # SR Legacy: a number and the measure as text; the weight is the one of
    # the number written ("0.5 cup" weighs 123 g).
    assert staged["sr_legacy:21237"] == [({"en": "1 item"}, 219.0, "g")]
    assert staged["sr_legacy:14400"] == [
        ({"en": "1 fl oz"}, 30.7, "g"),
        ({"en": "1 can (12 fl oz)"}, 368.0, "g"),
        ({"en": "0.5 cup"}, 123.0, "g"),
    ]
    # FNDDS: the whole wording.
    assert staged["fndds:11111000"] == [
        ({"en": "1 cup"}, 244.0, "g"),
        ({"en": "1 fl oz"}, 30.5, "g"),
    ]
    assert staged["fndds:27510389"] == [({"en": "1 cheeseburger"}, 119.0, "g")]
    # The portions of an earlier version of a food, or of a sample, are not read.
    assert set(staged) == {
        "foundation:16158",
        "foundation:100147",
        "sr_legacy:21237",
        "sr_legacy:14400",
        "sr_legacy:99999",
        "fndds:11111000",
        "fndds:27510389",
    }
    assert stats == {
        "foundation": {
            "in_source": 6,
            "kept": 4,
            "foods": 2,
            "left_out": {"laboratory_weight": 1, "no_wording": 1},
        },
        "sr_legacy": {
            "in_source": 9,
            "kept": 5,
            "foods": 3,
            "left_out": {"no_weight": 1, "no_wording": 1, "only_a_weight": 1, "repeated": 1},
        },
        "fndds": {
            "in_source": 6,
            "kept": 3,
            "foods": 2,
            "left_out": {"no_weight": 1, "quantity_not_specified": 1, "only_a_weight": 1},
        },
    }


def test_canadian_measures_in_both_languages_and_in_the_order_of_the_file(
    declared_sources: dict[str, Source], adapter_context: callable
) -> None:
    """The measures a person uses, worded in English and in French."""
    staged, stats = staged_portions("cnf", declared_sources, adapter_context)
    assert staged == {
        "2": [
            ({"en": "250 ml", "fr": "250 ml"}, 100.38, "g"),  # first in the file, not by code
            ({"en": "100 ml", "fr": "100 ml"}, 40.152, "g"),
            ({"en": "1 serving"}, 110.0, "g"),  # no French wording: none is made up
        ],
        "502": [({"en": "1 can (355 ml)", "fr": "1 canette (355 ml)"}, 372.0, "g")],
    }
    # The part that is not eaten and the cooking yield are not measures; "50 g" is not a portion.
    assert stats == {
        "in_source": 7,
        "kept": 4,
        "foods": 2,
        "left_out": {"no_weight": 1, "no_wording": 1, "only_a_weight": 1},
    }


def test_a_label_that_is_only_a_weight_is_not_a_portion() -> None:
    """A label such as "100 g" or "4 oz" says nothing about a food; a volume does."""
    con = duckdb.connect()

    def only_a_weight(label: str) -> bool:
        """Tell whether the build leaves a label out as a mere weight."""
        from kratheos_food_data.adapters.base import WEIGHT_ONLY_LABEL

        return con.execute(
            "SELECT regexp_matches(lower(?), ?)", [label, WEIGHT_ONLY_LABEL]
        ).fetchone()[0]

    for label in ("100 g", "50 g", "0.5g", "4 oz", "1 lb", "1.5 kg", "100 grams"):
        assert only_a_weight(label), label
    for label in ("250 ml", "1 fl oz", "1 large", "1 oz, cooked", "1 food guide serving = 75g"):
        assert not only_a_weight(label), label
    con.close()


def test_the_build_gives_each_food_the_portions_of_its_source(built_repository: Workspace) -> None:
    """A table of portions in food.sqlite, and the counts in the manifest."""
    database = built_repository.dist / "food.sqlite"
    assert portions_of(database, HUMMUS) == [
        (f"{HUMMUS}:p:1", "source", {"en": "1 tablespoon"}, 15.0, "g"),
        (f"{HUMMUS}:p:2", "source", {"en": "2 tablespoon"}, 30.0, "g"),
        (f"{HUMMUS}:p:3", "source", {"en": "1 cup, commercial"}, 246.0, "g"),
    ]
    assert portions_of(database, COLA_CA) == [
        (
            f"{COLA_CA}:p:1",
            "source",
            {"en": "1 can (355 ml)", "fr": "1 canette (355 ml)"},
            372.0,
            "g",
        )
    ]
    # A table without portions has none, and nothing is borrowed from the same food elsewhere.
    assert portions_of(database, APRICOT) == [] and portions_of(database, "swiss:273") == []
    assert portions_of(database, COLA_BOTTLE) == []  # a product keeps the serving of its label
    connection = sqlite3.connect(database)
    assert connection.execute(
        "SELECT serving_size FROM foods WHERE id = ?", (COLA_BOTTLE,)
    ).fetchone() == (330.0,)
    assert connection.execute("SELECT value FROM meta WHERE key = 'layout_version'").fetchone() == (
        "3",
    )
    # The portion of a record the build drops (no energy) goes nowhere.
    assert connection.execute(
        "SELECT count(*) FROM portions WHERE portion_id LIKE 'usda:sr_legacy:99999%'"
    ).fetchone() == (0,)
    connection.close()

    manifest = json.loads((built_repository.dist / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["statistics"]["foods"]["portions"] == {
        "portions": 15,
        "foods": 8,
        "by_source": {"cnf": {"foods": 2, "portions": 4}, "usda": {"foods": 6, "portions": 11}},
    }
    by_id = {source["id"]: source for source in manifest["sources"]}
    assert by_id["cnf"]["adapter"]["portions"]["kept"] == 4
    assert by_id["usda"]["adapter"]["portions"]["fndds"]["left_out"]["quantity_not_specified"] == 1
    assert "portions" not in by_id["ciqual"]["adapter"]


def test_portions_of_the_layer_come_after_those_of_the_source(repository: Workspace) -> None:
    """People's portions are folded in by the build, oldest first, and counted."""
    layer = sqlite3.connect(repository.kratheos)
    layer.executemany(
        "INSERT INTO portions VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            ("kratheos:p:bbb", APRICOT, "fr", "1 abricot  moyen", 45, "g", "2026-11-02"),
            ("kratheos:p:aaa", APRICOT, "en", "1 small apricot", 35, "g", "2026-11-02"),
            ("kratheos:p:ccc", APRICOT, "fr", "1 poignée", 80, "g", "2026-11-01"),
            ("kratheos:p:ddd", COLA_CA, "fr", "1 verre", 200, "g", "2026-11-03"),
            ("kratheos:p:eee", "nothing:here", "fr", "1 rien", 10, "g", "2026-11-03"),
        ],
    )
    layer.commit()
    layer.close()
    manifest = run_build(repository, BuildOptions(), log=quiet)
    database = repository.dist / "food.sqlite"
    assert portions_of(database, APRICOT) == [
        ("kratheos:p:ccc", "kratheos", {"fr": "1 poignée"}, 80.0, "g"),
        ("kratheos:p:aaa", "kratheos", {"en": "1 small apricot"}, 35.0, "g"),  # same day: by id
        ("kratheos:p:bbb", "kratheos", {"fr": "1 abricot moyen"}, 45.0, "g"),
    ]
    assert [row[0] for row in portions_of(database, COLA_CA)] == [
        f"{COLA_CA}:p:1",
        "kratheos:p:ddd",
    ]
    statistics = manifest["kratheos"]["statistics"]
    assert (
        statistics["portions"],
        statistics["portions_applied"],
        statistics["portions_without_target"],
    ) == (5, 4, 1)
    assert manifest["statistics"]["foods"]["portions"]["by_source"]["kratheos"] == {
        "foods": 2,
        "portions": 4,
    }
    # The same inputs give the same file, portions included.
    first = database.read_bytes()
    shutil.rmtree(repository.work)
    shutil.rmtree(repository.dist)
    run_build(repository, BuildOptions(), log=quiet)
    assert database.read_bytes() == first


def test_layer_refuses_a_portion_that_is_not_one(repository: Workspace) -> None:
    """The identifier, the unit and the amount of a portion are checked by the layout."""
    layer = sqlite3.connect(repository.kratheos)
    insert = (
        "INSERT INTO portions VALUES (?, 'ciqual:13000', 'fr', '1 abricot', ?, ?, '2026-11-01')"
    )
    for bad in (("kratheos:v:1", 45, "g"), ("kratheos:p:1", 0, "g"), ("kratheos:p:1", 45, "oz")):
        with pytest.raises(sqlite3.IntegrityError):
            layer.execute(insert, bad)
    with pytest.raises(sqlite3.IntegrityError):
        layer.execute(
            "INSERT INTO products (id, name, name_lang, basis, updated_at)"
            " VALUES ('p:1', 'x', 'fr', '100g', '2026-10-11')"
        )
    layer.execute("ALTER TABLE portions ADD COLUMN author TEXT")
    layer.commit()
    layer.close()
    with pytest.raises(LayerError, match="portions"):
        run_build(repository, BuildOptions(), log=quiet)


def test_food_record_lists_its_portions_with_their_label_resolved(service: Running) -> None:
    """Several portions per food; the label in the language asked, else English, else any."""
    assert service.food(HUMMUS, "lang=fr")["servings"] == [
        {"label": "1 tablespoon", "amount": 15.0, "unit": "g"},  # English only
        {"label": "2 tablespoon", "amount": 30.0, "unit": "g"},
        {"label": "1 cup, commercial", "amount": 246.0, "unit": "g"},
    ]
    assert service.food(COLA_CA, "lang=fr")["servings"] == [
        {"label": "1 canette (355 ml)", "amount": 372.0, "unit": "g"}
    ]
    assert service.food(COLA_CA, "lang=fr-CA")["servings"][0]["label"] == "1 canette (355 ml)"
    assert service.food(COLA_CA, "lang=de")["servings"][0]["label"] == "1 can (355 ml)"
    assert service.food(COLA_CA)["servings"][0]["label"] == "1 can (355 ml)"
    assert [serving["label"] for serving in service.food(SOUFFLE, "lang=fr")["servings"]] == [
        "250 ml",
        "100 ml",
        "1 serving",
    ]
    # A table that publishes none, and the portion of a packaged product, as before.
    assert service.food(APRICOT)["servings"] == []
    assert service.food(COLA_BOTTLE)["servings"] == [
        {"label": "1 portion (330 ml)", "amount": 330.0, "unit": "ml"}
    ]
    # A search answers the same records.
    status, found = service.get("/v1/foods/search?q=cola&kind=generic&lang=fr&country=CA")
    assert status == 200 and found["items"][0]["id"] == COLA_CA
    assert found["items"][0]["servings"][0]["label"] == "1 canette (355 ml)"
    # The versions of a food keep the portion of their own record, nothing else.
    assert all(version["servings"] == [] for version in service.versions(HUMMUS))
    assert foods.resolve_label({"it": "1 uovo", "de": "1 Ei"}, "fr") == "1 Ei"


def test_a_person_adds_a_portion_for_everyone(service: Running) -> None:
    """A label, an amount and a unit; refused twice, implausible, or beyond ten."""
    target = f"/v1/foods/{APRICOT}/portions"
    assert service.post(target, EGG, token=None)[0] == 401
    status, added = service.post(target + "?lang=fr&country=FR", EGG)
    assert status == 201 and added["portion_id"].startswith("kratheos:p:")
    assert added["food"]["id"] == APRICOT and added["food"]["name"] == "Abricot, dénoyauté, cru"
    assert added["food"]["servings"] == [{"label": "1 abricot moyen", "amount": 45.0, "unit": "g"}]
    # Everyone gets it, whatever the language asked; a food of a source keeps its own first.
    assert service.food(APRICOT, "lang=de")["servings"][0]["label"] == "1 abricot moyen"
    cup = {"language": "fr", "label": "1 verre", "amount": 200, "unit": "g", "contributor": BOB}
    assert service.post(f"/v1/foods/{COLA_CA}/portions", cup)[0] == 201
    assert [s["label"] for s in service.food(COLA_CA, "lang=fr")["servings"]] == [
        "1 canette (355 ml)",
        "1 verre",
    ]
    # A packaged product keeps the serving of its label first.
    spoon = {"language": "fr", "label": "1 verre", "amount": 250, "unit": "ml", "contributor": BOB}
    assert service.post(f"/v1/foods/{COLA_BOTTLE}/portions", spoon)[0] == 201
    assert [s["label"] for s in service.food(COLA_BOTTLE)["servings"]] == [
        "1 portion (330 ml)",
        "1 verre",
    ]

    # The same portion twice: same label once folded, same amount within a gram.
    again = dict(EGG, label="1  Abricot MOYEN", amount=45.9, contributor=BOB)
    status, error = service.post(target, again)
    assert (status, error["code"], error["portion_id"]) == (
        409,
        "portion_exists",
        added["portion_id"],
    )
    # A portion of the source counts too, in any of its languages.
    can = {"language": "en", "label": "1 Can (355 ml)", "amount": 371.2, "unit": "g"}
    status, error = service.post(f"/v1/foods/{COLA_CA}/portions", dict(can, contributor=BOB))
    assert (status, error["code"], error["portion_id"]) == (409, "portion_exists", f"{COLA_CA}:p:1")
    # Another amount, another label or another unit is another portion.
    assert service.post(target, dict(EGG, amount=47))[0] == 201
    assert service.post(target, dict(EGG, label="1 gros abricot"))[0] == 201
    assert service.post(target, dict(EGG, unit="ml"))[0] == 201

    for amount in (0.9, 2000.5, -3):
        status, error = service.post(target, dict(EGG, label="trop", amount=amount))
        assert (status, error["code"]) == (422, "implausible_portion"), amount
    assert service.post(target, dict(EGG, label="1 pincée", amount=1))[0] == 201
    assert service.post(target, dict(EGG, label="1 cageot", amount=2000))[0] == 201
    for bad in (
        dict(EGG, amount="45"),
        dict(EGG, amount=True),
        dict(EGG, unit="oz"),
        dict(EGG, label=" "),
        dict(EGG, label="x" * 121),
        dict(EGG, language="français"),
        dict(EGG, weight=45),
        {key: value for key, value in EGG.items() if key != "contributor"},
    ):
        status, error = service.post(target, bad)
        assert (status, error["code"]) == (422, "invalid_request"), bad
    assert service.post("/v1/foods/nothing:here/portions", EGG)[0] == 404
    assert service.get(target)[0] == 405

    # Ten added portions per food, whoever added them; those of the source do not count.
    assert len(service.food(APRICOT)["servings"]) == 6
    for number in range(4):
        assert (
            service.post(target, dict(EGG, label=f"portion {number}", amount=100 + 5 * number))[0]
            == 201
        )
    status, error = service.post(target, dict(EGG, label="une de trop", amount=300))
    assert (status, error["code"]) == (409, "too_many_portions")
    assert len(service.food(APRICOT)["servings"]) == api.MAX_USER_PORTIONS
    assert service.get("/v1/meta")[1]["counts"]["added_since_the_build"]["portions"] == 12
    assert validation.fold("1 Œuf  Moyen") == validation.fold("1 oeuf moyen") == "1 oeuf moyen"


def test_a_portion_that_reads_like_the_one_of_the_label_is_listed_once(service: Running) -> None:
    """The serving of a version and a portion of the food with the same words and weight: once."""
    burger = service.post("/v1/products", PRODUCT)[1]["id"]
    same = {"language": "fr", "label": "1 burger", "amount": 180, "unit": "g", "contributor": BOB}
    assert service.post(f"/v1/foods/{burger}/portions", same)[0] == 201
    assert service.food(burger)["servings"] == [{"label": "1 burger", "amount": 180.0, "unit": "g"}]
    other = dict(same, label="1 demi-burger", amount=90)
    assert service.post(f"/v1/foods/{burger}/portions", other)[0] == 201
    assert [s["label"] for s in service.food(burger)["servings"]] == ["1 burger", "1 demi-burger"]


def test_moderation_reaches_the_portions(service: Running) -> None:
    """What a key wrote includes its portions; blocking it, or hiding a food, hides them."""
    target = f"/v1/foods/{APRICOT}/portions"
    mine = service.post(target, EGG)[1]["portion_id"]
    other = service.post(target, dict(EGG, label="1 poignée", amount=80, contributor=BOB))[1][
        "portion_id"
    ]
    alice = service.store.contributor_hash(ALICE)

    written = service.post("/v1/moderation/contributors/lookup", {"contributor": ALICE})[1]
    assert written["counts"]["portions"] == 1
    (listed,) = written["portions"]
    assert {key: listed[key] for key in listed if key != "at"} == {
        "portion_id": mine,
        "food_id": APRICOT,
        "language": "fr",
        "label": "1 abricot moyen",
        "amount": 45.0,
        "unit": "g",
        "hidden": False,
    }
    status, who = service.get(f"/v1/moderation/foods/{mine}/contributors", token=TOKEN)
    assert status == 200 and [
        (row["contributor"], row["actions"]) for row in who["contributors"]
    ] == [(alice, ["add_portion"])]

    # Blocking the key hides its portions, refuses its next one, and the undo restores them.
    done = service.post("/v1/moderation/contributors/block", {"contributor": ALICE})[1]
    assert done["withdrawn"] == {"products": 0, "versions": 0, "translations": 0, "portions": 1}
    assert [s["label"] for s in service.food(APRICOT)["servings"]] == ["1 poignée"]
    status, error = service.post(target, dict(EGG, label="1 autre", amount=60))
    assert (status, error["code"]) == (403, "contributor_blocked")
    lookup = service.post("/v1/moderation/contributors/lookup", {"contributor": ALICE})[1]
    assert lookup["portions"][0]["hidden"] is True
    assert [row["portion_id"] for row in export.read_live(service.settings.live).portions] == [
        other
    ]
    undone = service.post("/v1/moderation/contributors/unblock", {"contributor": ALICE})[1]
    assert undone["restored"]["portions"] == 1
    # Two portions added the same day are ordered by their identifier, which does not change.
    both = [s["label"] for s in service.food(APRICOT)["servings"]]
    assert sorted(both) == ["1 abricot moyen", "1 poignée"]
    assert both == [s["label"] for s in service.food(APRICOT)["servings"]]

    # One portion can be hidden alone, a portion of a source too; then shown again.
    assert service.post(f"/v1/moderation/foods/{other}/hide")[0] == 200
    assert service.post(f"/v1/moderation/foods/{COLA_CA}:p:1/hide")[0] == 200
    assert service.post("/v1/moderation/foods/kratheos:p:unknown/hide")[0] == 404
    assert [s["label"] for s in service.food(APRICOT)["servings"]] == ["1 abricot moyen"]
    assert service.food(COLA_CA)["servings"] == []
    assert [row["portion_id"] for row in export.read_live(service.settings.live).portions] == [mine]
    assert service.post(f"/v1/moderation/foods/{COLA_CA}:p:1/restore")[0] == 200
    assert len(service.food(COLA_CA)["servings"]) == 1

    # Hiding a food hides its portions with it, in the reads and in the export.
    assert service.post(f"/v1/moderation/foods/{APRICOT}/hide")[0] == 200
    assert service.get(f"/v1/foods/{APRICOT}")[0] == 404
    assert export.read_live(service.settings.live).portions == []
    # Never who: neither the key nor its hash is in what is published.
    service.post(f"/v1/moderation/foods/{APRICOT}/restore")
    layer_file = service.settings.live.with_name("exported.sqlite")
    summary = export.export_layer(service.settings.live, layer_file)
    assert summary["portions"] == 1
    raw = layer_file.read_bytes()
    assert ALICE.encode() not in raw and alice.encode() not in raw


def test_added_portions_survive_export_and_rebuild_without_a_duplicate(service: Running) -> None:
    """Add, export, rebuild: the same portions, each once, and the service goes on."""
    first = service.post(f"/v1/foods/{APRICOT}/portions", EGG)[1]["portion_id"]
    second = service.post(
        f"/v1/foods/{COLA_CA}/portions",
        {"language": "fr", "label": "1 verre", "amount": 200, "unit": "g", "contributor": BOB},
    )[1]["portion_id"]
    burger = service.post("/v1/products", PRODUCT)[1]["id"]
    half = {
        "language": "fr",
        "label": "1 demi-burger",
        "amount": 90,
        "unit": "g",
        "contributor": BOB,
    }
    third = service.post(f"/v1/foods/{burger}/portions", half)[1]["portion_id"]
    targets = [f"/v1/foods/{food}?lang=fr" for food in (APRICOT, COLA_CA, burger, HUMMUS)]
    before = {target: service.get(target)[1]["servings"] for target in targets}
    assert before[f"/v1/foods/{burger}?lang=fr"] == [
        {"label": "1 burger", "amount": 180.0, "unit": "g"},
        {"label": "1 demi-burger", "amount": 90.0, "unit": "g"},
    ]

    report = maintenance.rebuild(
        service.workspace,
        BuildOptions(from_mirror=True),
        service.settings.database,
        service.settings.live,
        log=quiet,
    )
    assert report["swapped"] and report["export"]["portions"] == 3
    assert (report["kratheos"]["portions"], report["kratheos"]["portions_applied"]) == (3, 3)
    layer = kratheos.read_layer(service.workspace.kratheos)
    assert [row["portion_id"] for row in layer.portions] == sorted([first, second, third])
    assert len(layer.portions[0]["updated_at"]) == 10  # a day, never a time

    # Folded into the main database, once; the live database still holds them: served once.
    database = service.settings.database
    assert portions_of(database, APRICOT) == [
        (first, "kratheos", {"fr": "1 abricot moyen"}, 45.0, "g")
    ]
    assert [row[0] for row in portions_of(database, COLA_CA)] == [f"{COLA_CA}:p:1", second]
    assert [row[0] for row in portions_of(database, burger)] == [third]
    after = {target: service.get(target)[1]["servings"] for target in targets}
    assert after == before

    # The loop goes on: the same portion is still refused, a new one is added after the others,
    # a second export gives the same layer plus the new portion, and blocking still hides.
    status, error = service.post(f"/v1/foods/{APRICOT}/portions", dict(EGG, contributor=BOB))
    assert (status, error["portion_id"]) == (409, first)
    assert (
        service.post(f"/v1/foods/{APRICOT}/portions", dict(EGG, label="1 gros", amount=60))[0]
        == 201
    )
    assert [s["label"] for s in service.food(APRICOT)["servings"]] == ["1 abricot moyen", "1 gros"]
    assert len(export.read_live(service.settings.live).portions) == 4
    service.post("/v1/moderation/contributors/block", {"contributor": ALICE})
    assert service.food(APRICOT)["servings"] == []  # the folded one is hidden too
    # Her product goes with her key, and the portion somebody else added to it with the product.
    assert [row["portion_id"] for row in export.read_live(service.settings.live).portions] == [
        second
    ]


def test_a_database_built_before_the_portions_is_still_served(
    built_repository: Workspace, tmp_path: Path
) -> None:
    """Layout 2 has no table of portions: its foods have those people add, and no other."""
    service = start_service(built_repository, tmp_path)
    try:
        older = tmp_path / "older.sqlite"
        shutil.copy(service.settings.database, older)
        database = sqlite3.connect(older)
        database.executescript(
            "DROP TABLE portions; UPDATE meta SET value = '2' WHERE key = 'layout_version';"
        )
        database.close()
        maintenance.move_into_place(older, service.settings.database)
        assert service.get("/v1/meta")[1]["layout_version"] == 2
        assert service.food(HUMMUS)["servings"] == []
        assert service.food(COLA_BOTTLE)["servings"] == [
            {"label": "1 portion (330 ml)", "amount": 330.0, "unit": "ml"}
        ]
        assert service.post(f"/v1/foods/{HUMMUS}/portions", EGG)[0] == 201
        assert [s["label"] for s in service.food(HUMMUS)["servings"]] == ["1 abricot moyen"]
        assert service.post(f"/v1/moderation/foods/{HUMMUS}:p:1/hide")[0] == 404
        assert sqlite_export.LAYOUT_VERSION == 3
    finally:
        service.close()


def test_a_live_database_without_portions_gains_the_table(tmp_path: Path) -> None:
    """Layout 2 of the live database is brought to layout 3 when opened; the export reads both."""
    path = tmp_path / "live.sqlite"
    LiveStore(path, KEY_SECRET.encode()).close()
    database = sqlite3.connect(path)
    database.executescript(
        "DROP TABLE portions; UPDATE meta SET value = '2' WHERE key = 'schema_version';"
    )
    database.close()
    assert export.read_live(path).portions == []  # read as it is, before any upgrade
    store = LiveStore(path, KEY_SECRET.encode())
    fields = {"lang": "fr", "label": "1 abricot", "amount": 45.0, "unit": "g"}
    store.add_portion("kratheos:p:1", APRICOT, fields, ALICE)
    reader = store.reader()
    assert live.portion_owner(reader, "kratheos:p:1") == APRICOT
    assert live.counts(reader)["portions"] == 1
    reader.close()
    store.close()
    layer = export.read_live(path)
    assert [(row["portion_id"], row["label"], row["amount"]) for row in layer.portions] == [
        ("kratheos:p:1", "1 abricot", 45.0)
    ]
    # Taken over by another live database, once.
    other = LiveStore(tmp_path / "other.sqlite", KEY_SECRET.encode())
    assert other.import_layer(layer)["portions"] == 1
    assert other.import_layer(layer)["portions"] == 0
    other.close()
