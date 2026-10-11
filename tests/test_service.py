"""Tests of the food service on the miniature build: reads, writes, moderation.

The endpoints are called the way the Kratheos backend calls them. What is
checked is the contract (`docs/api.md`): the shape of a food record, the
errors and their codes, that what is written is visible at once, and the rule
choosing the version served.
"""

from __future__ import annotations

import copy
import os
import shutil
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from service_tools import ALICE, BOB, CAROL, DAVE, PRODUCT, TOKEN, Running, start_service

from kratheos_food_data.build import Workspace
from kratheos_food_data.service import search
from kratheos_food_data.service.api import ERROR_CODES
from kratheos_food_data.service.settings import SettingsError, load_settings

NUTELLA = "gtin:3017620422003"
FOOD_KEYS = {
    "id",
    "kind",
    "barcode",
    "names",
    "name",
    "name_language",
    "name_origin",
    "brand",
    "category",
    "countries",
    "basis",
    "is_drink",
    "nutrients",
    "servings",
    "source",
    "version_id",
    "version_origin",
    "votes",
    "versions_count",
    "flags",
    "updated_at",
}
VERSION_KEYS = {
    "version_id",
    "origin",
    "source",
    "countries",
    "basis",
    "nutrients",
    "servings",
    "votes",
    "served",
    "flags",
    "updated_at",
}
NUTRIENT_KEYS = {
    "kcal",
    "protein_g",
    "carbs_g",
    "sugars_g",
    "fat_g",
    "saturated_fat_g",
    "fibre_g",
    "salt_g",
    "alcohol_g",
}
OTHER_VALUES = {
    "basis": "100g",
    "nutrients": {
        "kcal": 530,
        "protein_g": 6,
        "carbs_g": 56,
        "sugars_g": 55,
        "fat_g": 31,
        "saturated_fat_g": 11,
        "fibre_g": None,
        "salt_g": 0.1,
    },
    "servings": [{"label": "1 cuillère", "amount": 15, "unit": "g"}],
    "countries": ["FR"],
    "contributor": ALICE,
}


@pytest.fixture
def service(built_repository: Workspace, tmp_path: Path) -> Iterator[Running]:
    """Give a running service over a copy of the miniature repository."""
    running = start_service(built_repository, tmp_path)
    yield running
    running.close()


def test_food_record_has_the_shape_of_the_contract(service: Running) -> None:
    """Every field of the contract, under its name, and nothing else."""
    food = service.food(NUTELLA, "lang=fr&country=FR")
    assert set(food) == FOOD_KEYS and set(food["nutrients"]) == NUTRIENT_KEYS
    assert food == {
        "id": NUTELLA,
        "kind": "product",
        "barcode": "3017620422003",
        "names": {"en": "Nutella hazelnut spread", "fr": "Nutella"},
        "name": "Nutella",
        "name_language": "fr",
        "name_origin": "source",
        "brand": "Nutella, Ferrero",
        "category": "en:hazelnut-spreads",
        "countries": ["FR", "GB"],
        "basis": "100g",
        "is_drink": False,
        "nutrients": {
            "kcal": 539.0,
            "protein_g": 6.3,
            "carbs_g": 57.5,
            "sugars_g": 56.3,
            "fat_g": 30.9,
            "saturated_fat_g": 10.6,
            "fibre_g": 0.0,
            "salt_g": 0.107,
            "alcohol_g": 0.0,
        },
        "servings": [],
        "source": {
            "id": "off",
            "title": "Open Food Facts",
            "licence": "ODbL-1.0",
            "url": "https://world.openfoodfacts.org/product/3017620422003",
        },
        "version_id": "off:3017620422003",
        "version_origin": "source",
        "votes": 0,
        "versions_count": 2,
        "flags": [],
        "updated_at": "2026-10-10T00:00:00Z",
    }
    # The name: the language asked for, else English, else whatever exists.
    assert service.food(NUTELLA, "lang=de")["name_language"] == "en"
    assert service.food(NUTELLA, "lang=fr-CH")["name"] == "Nutella"
    bircher = service.food("gtin:7610000123451", "lang=fr")
    assert (bircher["name"], bircher["name_language"]) == ("Bircher Müesli", "de")
    generic = service.food("ciqual:13000", "lang=fr")
    assert (generic["kind"], generic["barcode"], generic["versions_count"]) == ("generic", None, 1)
    assert generic["source"]["id"] == "ciqual" and generic["version_id"] == "ciqual:13000"
    # A portion, and a drink per 100 ml.
    juice = service.food("swiss:605")
    assert (juice["basis"], juice["is_drink"]) == ("100ml", True)
    cola = service.food("gtin:5449000000996")
    assert cola["servings"] == [{"label": "1 portion (330 ml)", "amount": 330.0, "unit": "ml"}]


def test_unknown_food_bad_identifier_and_unknown_parameter(service: Running) -> None:
    """Errors carry a stable code and a sentence."""
    assert service.get("/v1/foods/nothing:here") == (
        404,
        {"code": "not_found", "message": "no such food"},
    )
    assert service.get("/v1/foods/" + "x" * 81)[1]["code"] == "invalid_request"
    assert service.get(f"/v1/foods/{NUTELLA}?country=France")[1]["code"] == "invalid_request"
    assert service.get(f"/v1/foods/{NUTELLA}?lang=français")[1]["code"] == "invalid_request"
    status, error = service.get(f"/v1/foods/{NUTELLA}?county=FR")
    assert (status, error["message"]) == (422, "unknown parameter: county")
    assert service.get("/v1/nothing")[0] == 404 and service.get("/v2/meta")[0] == 404
    status, error = service.post("/v1/meta")
    assert (status, error["code"]) == (405, "method_not_allowed")
    for code, status in ERROR_CODES.items():
        assert 400 <= status < 600, code


def test_barcode_is_normalised_whatever_the_way_it_is_written(service: Running) -> None:
    """A UPC on 12 digits, with zeros or spaces in front, is the same product."""
    for written in ("049000028911", "0049000028911", "00049000028911", "0490-0002-8911"):
        status, food = service.get(f"/v1/barcodes/{written}")
        assert (status, food["id"]) == (200, "gtin:0049000028911"), written
    assert service.get("/v1/barcodes/3017620422003?lang=fr")[1]["name"] == "Nutella"
    assert service.get("/v1/barcodes/3017620422004") == (
        422,
        {
            "code": "invalid_barcode",
            "message": "the barcode must be a GTIN of 8, 12, 13 or 14 digits with a valid"
            " check digit",
        },
    )
    assert service.get("/v1/barcodes/abc")[1]["code"] == "invalid_barcode"
    assert service.get("/v1/barcodes/4006381333931")[1]["code"] == "not_found"  # valid, unknown


def test_meta_gives_build_date_sources_licences_and_counts(service: Running) -> None:
    """What an "About" screen needs is served with the data."""
    status, meta = service.get("/v1/meta")
    assert status == 200
    assert meta["built_at"] == service.catalogue.current().manifest["built_at"]
    assert meta["licence"]["id"] == "ODbL-1.0" and "Open Food Facts" in meta["licence"]["notice"]
    sources = {source["id"]: source for source in meta["sources"]}
    assert set(sources) == {"off", "usda", "ciqual", "swiss", "cnf", "cofid", "chain", "kratheos"}
    assert sources["off"]["licence"] == "ODbL-1.0"
    assert "https://openfoodfacts.org" in sources["off"]["attribution"]
    assert sources["ciqual"]["attribution"].startswith("Anses. 2025.")
    assert sources["swiss"]["version"] == "7.1" and sources["swiss"]["foods"] == 3
    assert meta["counts"]["foods"] == 31 == sum(meta["counts"]["by_source"].values())
    assert meta["counts"]["generic_foods"] == 16
    assert meta["counts"]["added_since_the_build"] == {
        "products": 0,
        "versions": 0,
        "translations": 0,
        "votes": 0,
    }
    assert meta["service"]["user_version_votes"] == 3
    assert service.get("/v1/health")[1]["status"] == "ok"


def test_search_by_any_name_brand_and_barcode(service: Running) -> None:
    """Names in every language, the brand, and digits as a barcode."""
    assert service.found("q=mandel") == ["swiss:273"]  # German name
    assert service.found("q=Mandorla") == ["swiss:273"]  # Italian name
    assert service.found("q=muesli") == ["gtin:7610000123451"]  # accents are ignored
    assert service.found("q=ferrero") == [NUTELLA]  # the brand
    assert service.found("q=souffle+fromage") == ["cnf:2"]  # every word must be found
    assert service.found('q="quoted"+(odd)+input*') == []
    assert service.found("q=") == [] and service.found("q=%2B%2B") == []
    assert service.found("q=3017620422003") == [NUTELLA]
    assert service.found("q=049000028911") == ["gtin:0049000028911"]  # a UPC on 12 digits
    assert service.found("q=0490000") == ["gtin:0049000028911"]  # the beginning of a UPC
    assert service.found("q=761000") == ["gtin:7610000123451"]  # the beginning of an EAN
    status, page = service.get("/v1/foods/search?q=cola&lang=fr&limit=2")
    assert status == 200 and len(page["items"]) == 2 and page["next_offset"] == 2
    assert set(page["items"][0]) == FOOD_KEYS
    status, rest = service.get("/v1/foods/search?q=cola&lang=fr&limit=2&offset=2")
    assert len(rest["items"]) == 2 and rest["next_offset"] is None
    assert service.get("/v1/foods/search?q=cola&limit=51")[1]["code"] == "invalid_request"
    assert service.get("/v1/foods/search?q=cola&limit=0")[1]["code"] == "invalid_request"
    assert service.get("/v1/foods/search?q=" + "a" * 101)[1]["code"] == "invalid_request"


def test_search_ranks_the_country_first_and_filters(service: Running) -> None:
    """The foods of the country of the request come first; kind and drink filter."""
    swiss_first = service.found("q=cola&country=CH")
    american_first = service.found("q=cola&country=US")
    assert set(swiss_first) == set(american_first) and len(swiss_first) == 4
    assert swiss_first[0] == "gtin:5449000000996"  # sold in Switzerland
    assert american_first[:2] == ["gtin:0049000028911", "usda:sr_legacy:14400"]
    # A name equal to what was typed comes before one that only holds it.
    assert service.found("q=cola")[0] == "gtin:0049000028911"
    assert service.found("q=cola&kind=generic") == ["cnf:502", "usda:sr_legacy:14400"]
    assert set(service.found("q=cola&kind=product")) == {
        "gtin:0049000028911",
        "gtin:5449000000996",
    }
    assert service.found("q=the&drink=true") == ["chain:chaine-exemple:ch:d-007"]
    assert "chain:chaine-exemple:ch:d-007" not in service.found("q=chaine+exemple&drink=false")
    assert service.get("/v1/foods/search?q=x&kind=dish")[1]["code"] == "invalid_request"
    assert service.get("/v1/foods/search?q=x&drink=yes")[1]["code"] == "invalid_request"


def test_reference_foods_come_in_an_order_a_person_can_predict(service: Running) -> None:
    """Language, a name that is or begins with the text, country, then the match."""
    generic = "kind=generic"
    # Both tables have the apple juice: the table of the country comes first...
    assert service.found(f"q=apple+juice&{generic}&lang=en&country=FR") == [
        "ciqual:2069",  # Apple juice, pure juice
        "swiss:605",  # Apple juice
    ]
    assert service.found(f"q=apple+juice&{generic}&lang=en&country=CH") == [
        "swiss:605",
        "ciqual:2069",
    ]
    # ... and without one, the name that is the text beats the name whose first part is.
    assert service.found(f"q=apple+juice&{generic}&lang=en") == ["swiss:605", "ciqual:2069"]
    # A name in the language of the request beats the table of the country.
    assert service.found(f"q=cola&{generic}&lang=fr&country=US") == [
        "cnf:502",  # Boisson gazeuse, cola
        "usda:sr_legacy:14400",  # no French name
    ]
    assert service.found(f"q=cola&{generic}&lang=en&country=US") == [
        "usda:sr_legacy:14400",
        "cnf:502",
    ]
    # A name beginning with the word typed beats a longer word beginning with it,
    # whatever the country: "Cheese souffle" before "Cheeseburger".
    assert service.found(f"q=cheese&{generic}&lang=en&country=US") == [
        "cnf:2",
        "usda:fndds:27510389",
    ]
    # No name in the language: another language answers, the country first again.
    assert service.found(f"q=jus&{generic}&lang=de&country=FR") == ["ciqual:2069", "swiss:605"]
    assert service.found(f"q=apfel&{generic}&lang=de") == ["swiss:605"]  # Apfelsaft
    # Every reference food matching is read, two letters included.
    assert service.found(f"q=br&{generic}") == ["cofid:11-001"]
    assert service.found(f"q=mc&{generic}&lang=en") == [
        "usda:sr_legacy:21237",  # McDONALD'S, BIG MAC: a whole word typed would be "mcdonald"
        "usda:fndds:27510389",
    ]


def test_in_store_labels_come_after_every_other_product_and_own_brands_do_not(
    service: Running, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both forms are flagged; only the 13-digit one is left for the end of a search by name."""
    own_brand = "gtin:20095314"  # Edel-Zartbitter-Schokolade, 8 digits beginning with 2
    assert service.food(own_brand)["flags"] == ["barcode_restricted", "in_store_barcode"]
    assert "in_store_barcode" not in service.food(NUTELLA)["flags"]
    # By barcode nothing changes.
    status, by_barcode = service.get("/v1/barcodes/20095314")
    assert status == 200 and by_barcode["id"] == own_brand
    assert service.found("q=20095314") == [own_brand]
    # The 8-digit number ranks by the ordinary order: its name begins with the text,
    # so it comes before a product that only holds it; the 13-digit label is last,
    # although its name is the text typed.
    other = dict(PRODUCT, names={"de": "Schokolade Edelbitter vom Test"})
    label = dict(PRODUCT, barcode="2000000000039", names={"de": "Edel"})
    other_id = service.post("/v1/products", other)[1]["id"]
    created = service.post("/v1/products", label)[1]
    assert created["id"] == "gtin:2000000000039" and "in_store_barcode" in created["flags"]
    assert service.found("q=edel") == [own_brand, other_id, "gtin:2000000000039"]
    # The country goes first, as for any product, and the label stays last.
    assert service.found("q=edel&country=CH") == [other_id, own_brand, "gtin:2000000000039"]
    assert service.get("/v1/barcodes/2000000000039")[1]["id"] == "gtin:2000000000039"

    # The same in the main database: the cola of the United States, whose name is the
    # text typed, with the 13-digit in-store number of a counter.
    assert service.found("q=cola&kind=product") == ["gtin:0049000028911", "gtin:5449000000996"]
    changed = tmp_path / "changed.sqlite"
    shutil.copy(service.settings.database, changed)
    connection = sqlite3.connect(changed)
    connection.execute(
        "UPDATE foods SET id = 'gtin:0249000028915', barcode = '0249000028915'"
        " WHERE id = 'gtin:0049000028911'"
    )
    connection.commit()
    connection.close()
    os.replace(changed, service.settings.database)
    with service.api._context() as context:
        assert context.generation.in_store_blocks == ((14, 14), (15, 15))
        assert context.generation.in_store_ranked == {15}  # the 8-digit number
    assert service.found("q=cola&kind=product") == ["gtin:5449000000996", "gtin:0249000028915"]
    assert service.found("q=zartbitter&kind=product") == [own_brand]
    assert service.get("/v1/barcodes/0249000028915")[1]["id"] == "gtin:0249000028915"
    # When the other products fill their list, no label is in the answer.
    monkeypatch.setattr(search, "POOL_ALL", 1)
    assert service.found("q=cola&kind=product") == ["gtin:5449000000996"]


def test_without_a_kind_the_reference_food_typed_comes_before_the_products(
    service: Running,
) -> None:
    """The reference foods whose name, or first part, is the text; nothing else changes."""
    juice = dict(PRODUCT, names={"en": "Apple juice"}, brand="Test Orchard", countries=["US"])
    juice_id = service.post("/v1/products", juice)[1]["id"]
    # A product of the country whose name is the text typed: it was first before.
    assert service.found("q=apple+juice&lang=en&country=US") == [
        "swiss:605",  # Apple juice
        "ciqual:2069",  # Apple juice, pure juice
        juice_id,
    ]
    assert service.found("q=apple+juice&lang=en&country=US&kind=product") == [juice_id]
    # A reference food that merely holds the word keeps its place among the products:
    # the country first, then generic foods before products.
    assert service.found("q=juice&lang=en&country=US") == [
        juice_id,
        "swiss:605",
        "ciqual:2069",
        "gtin:9400547001910",
    ]
    assert service.found("q=cola&country=US")[:2] == ["gtin:0049000028911", "usda:sr_legacy:14400"]


def test_what_was_read_of_the_reference_foods_is_not_asked_of_the_index_again(
    service: Running, tmp_path: Path
) -> None:
    """Where only reference foods come before the barcoded products, as in the real build."""
    with service.api._context() as context:
        generation = context.generation
        # The blocks never span the barcoded products, however few they are.
        assert generation.reference_blocks == ((6, 13), (23, 31))
        assert generation.in_store_blocks == ((15, 15),)
        assert sorted(generation.reference_by_country) == ["CA", "CH", "FR", "GB", "US"]
        # Here the dishes of the chains come first: the index is asked.
        assert not generation.reference_only_below and not generation.reference_only_above
        assert not generation.only_reference(6, 13)
        # The best matches taken from the ranks are those the index gives itself.
        match = ('"b"*',)
        in_block = "FROM foods_fts WHERE foods_fts MATCH ? AND rowid BETWEEN 6 AND 13"
        rows = [tuple(row) for row in context.base.execute(f"SELECT rowid, rank {in_block}", match)]
        rank_of = dict(rows)
        # Boisson gazeuse, Bread, Beer and bitter: three foods, three ranks.
        assert len(rows) == 3 and len(set(rank_of.values())) == 3
        for count in (1, 2, 3, 4):
            asked = context.base.execute(
                f"SELECT rank {in_block} ORDER BY rank LIMIT ?", (*match, count)
            ).fetchall()
            assert [rank_of[pk] for pk in search.best_of(rows, count)] == [row[0] for row in asked]
    searches = (
        "q=cola&country=US&lang=en",
        "q=cola&country=CH",
        "q=cola&kind=product&country=CH",
        "q=apple+juice&lang=en&country=FR",
        "q=jus&lang=fr&country=FR",
        "q=bread",
        "q=sugar+white&country=GB",
        "q=br",
        "q=hamburger&lang=fr&country=FR",
    )
    before = {query: service.found(query) for query in searches}
    assert all(before.values())
    # The same file without the dishes of the chains: nothing but reference foods is
    # left before the barcoded products.
    changed = tmp_path / "changed.sqlite"
    shutil.copy(service.settings.database, changed)
    connection = sqlite3.connect(changed)
    connection.execute("DELETE FROM foods WHERE id LIKE 'chain:%'")
    connection.commit()
    connection.close()
    os.replace(changed, service.settings.database)
    with service.api._context() as context:
        generation = context.generation
        assert generation.reference_only_below and not generation.reference_only_above
        assert generation.only_reference(6, 13) and not generation.only_reference(23, 31)
    assert {query: service.found(query) for query in searches} == before


def test_short_texts_only_search_generic_foods_and_dishes(service: Running) -> None:
    """One or two letters never rank the barcoded products."""
    # Two letters are the beginning of a word, among the foods without a barcode.
    assert service.found("q=am") == ["swiss:273"]  # Almond, Amande
    assert service.found("q=nu") == []  # Nutella has a barcode
    assert NUTELLA in service.found("q=nut")
    assert "chain:chaine-exemple:ch:b-001" in service.found("q=bu")
    # One letter is a whole word.
    assert service.found("q=a") == []
    assert service.found("q=b") == []
    query = search.parse("  Thé  GLACÉ ")
    assert (query.text, query.words, query.short) == ("the glace", ("the", "glace"), False)
    assert search.parse("ab").short and search.parse("a b").short
    assert search.match_expression(search.parse("vitamine c")) == '"vitamine"* "c"'
    assert search.parse("3017 6204-22003").digits == "3017620422003"
    assert search.parse("1664 blonde").digits == ""


def test_a_search_gives_up_ranking_when_its_time_is_spent(service: Running) -> None:
    """With no time at all the search answers at once, with what it has."""
    with service.api._context() as context:
        nothing = search.search(context, "cola", budget=0.0)
        everything = search.search(context, "cola", budget=5.0)
    assert nothing == [] and len(everything) == 4


def test_writes_need_the_service_token(
    service: Running, built_repository: Workspace, tmp_path: Path
) -> None:
    """Reads are open; writes and moderation are not."""
    for token in (None, "wrong-token-0123456789"):
        status, error = service.post("/v1/products", PRODUCT, token=token)
        assert (status, error["code"]) == (401, "unauthorized")
        assert service.post(f"/v1/foods/{NUTELLA}/votes", {}, token=token)[0] == 401
        assert service.post(f"/v1/moderation/foods/{NUTELLA}/hide", token=token)[0] == 401
    assert service.get("/v1/moderation/reports")[0] == 401
    assert service.found("q=burger+maison") == []  # nothing was written
    without_token = start_service(built_repository, tmp_path / "other", token="")
    status, error = without_token.post("/v1/products", PRODUCT)
    assert (status, error["code"]) == (503, "writes_disabled")
    assert without_token.food(NUTELLA)["id"] == NUTELLA
    without_token.close()
    with pytest.raises(SettingsError, match="FOOD_DATA_TOKEN"):
        load_settings(tmp_path, environment={"FOOD_DATA_TOKEN": "short"}, with_secret=False)


def test_product_is_created_and_visible_at_once(service: Running) -> None:
    """A product added is found by identifier, by search and by barcode at once."""
    status, created = service.post("/v1/products?lang=en", PRODUCT)
    assert status == 201 and set(created) == FOOD_KEYS
    food_id = created["id"]
    assert food_id.startswith("kratheos:") and len(food_id) == len("kratheos:") + 36
    assert created["names"] == {"en": "Home-made test burger", "fr": "Burger maison du test"}
    assert (created["name"], created["name_language"], created["name_origin"]) == (
        "Home-made test burger",
        "en",
        "source",
    )
    assert created["countries"] == ["CH", "FR"] and created["barcode"] is None
    assert created["nutrients"]["carbs_g"] == 25.0 and created["nutrients"]["fibre_g"] == 2.0
    assert created["servings"] == [{"label": "1 burger", "amount": 180.0, "unit": "g"}]
    assert created["source"] == {
        "id": "kratheos",
        "title": "Kratheos",
        "licence": "ODbL-1.0",
        "url": None,
    }
    # The values of a product are a version like any other, with its author's vote.
    assert (created["version_id"], created["version_origin"]) == (food_id, "user")
    assert (created["votes"], created["versions_count"], created["flags"]) == (1, 1, [])
    assert service.food(food_id, "lang=en") == created
    assert service.found("q=burger+maison&country=CH")[0] == food_id
    assert service.found("q=chez+test") == [food_id]  # its brand
    assert service.found("q=home-made") == [food_id]  # its other name
    assert service.get("/v1/meta")[1]["counts"]["added_since_the_build"]["products"] == 1

    with_barcode = dict(PRODUCT, barcode="7 613 034 626 844", names={"fr": "Barre du test"})
    status, bar = service.post("/v1/products", with_barcode)
    assert status == 201 and bar["id"] == "gtin:7613034626844"
    assert bar["version_id"].startswith("kratheos:") and bar["version_id"] != bar["id"]
    assert service.get("/v1/barcodes/7613034626844")[1] == bar
    assert service.found("q=7613034626844") == ["gtin:7613034626844"]
    # The same barcode again, or the barcode of a food of a source: refused with its identifier.
    assert service.post("/v1/products", with_barcode) == (
        409,
        {
            "code": "barcode_exists",
            "message": "a food already has this barcode",
            "id": "gtin:7613034626844",
        },
    )
    known = dict(PRODUCT, barcode="049000028911")
    assert service.post("/v1/products", known)[1]["id"] == "gtin:0049000028911"
    in_store = dict(PRODUCT, barcode="2000000000039", names={"fr": "Fromage à la coupe"})
    # An in-store number: flagged on the record of its source by the build, and on the
    # food by the service.
    assert service.post("/v1/products", in_store)[1]["flags"] == [
        "barcode_restricted",
        "in_store_barcode",
    ]


def test_product_that_makes_no_sense_is_refused(service: Running) -> None:
    """The same plausibility checks as the build, and every field validated."""

    def refused(**changes: object) -> dict:
        """Post the product with some fields changed and return the error."""
        body = copy.deepcopy(PRODUCT) | changes
        status, error = service.post("/v1/products", body)
        assert status in (400, 422), error
        return error

    error = refused(nutrients=dict(PRODUCT["nutrients"], kcal=25))
    assert error["code"] == "implausible_values" and error["checks"] == ["energy_macro_mismatch"]
    error = refused(nutrients=dict(PRODUCT["nutrients"], sugars_g=40, saturated_fat_g=30))
    assert error["checks"] == ["sugars_exceed_carbohydrate", "saturated_exceed_fat"]
    assert refused(nutrients=dict(PRODUCT["nutrients"], kcal=None))["message"] == (
        "nutrients.kcal is required"
    )
    assert refused(nutrients=dict(PRODUCT["nutrients"], iron=1))["code"] == "invalid_request"
    assert refused(nutrients=dict(PRODUCT["nutrients"], fat_g="11"))["code"] == "invalid_request"
    assert refused(barcode="3017620422004")["code"] == "invalid_barcode"
    assert refused(basis="100 g")["code"] == "invalid_request"
    assert refused(names={})["code"] == "invalid_request"
    assert refused(names={"français": "x"})["code"] == "invalid_request"
    assert refused(names={"fr": "x" * 201})["code"] == "invalid_request"
    assert refused(countries=["Suisse"])["code"] == "invalid_request"
    assert refused(servings=[{"amount": -1, "unit": "g"}])["code"] == "invalid_request"
    assert refused(servings=[{"amount": 30, "unit": "oz"}])["code"] == "invalid_request"
    assert refused(is_drink="no")["code"] == "invalid_request"
    assert refused(contributor="x")["code"] == "invalid_request"
    assert refused(colour="red")["message"] == "unknown field: colour"
    status, error = (
        service.api.handle(
            "POST", "/v1/products", {"authorization": f"Bearer {TOKEN}"}, b"{not json"
        ),
        None,
    )
    assert (status.status, status.payload["code"]) == (400, "invalid_json")
    not_a_number = b'{"nutrients": {"kcal": NaN}}'
    response = service.api.handle(
        "POST", "/v1/products", {"authorization": f"Bearer {TOKEN}"}, not_a_number
    )
    assert response.payload["code"] == "invalid_json"
    # An alcoholic drink needs its alcohol for its energy to make sense.
    wine = dict(
        PRODUCT,
        names={"fr": "Vin du test"},
        basis="100ml",
        is_drink=True,
        servings=[],
        nutrients={"kcal": 82, "protein_g": 0.1, "carbs_g": 2.6, "fat_g": 0},
    )
    assert service.post("/v1/products", wine)[1]["checks"] == ["energy_macro_mismatch"]
    wine["nutrients"]["alcohol_g"] = 10
    status, created = service.post("/v1/products", wine)
    assert status == 201 and set(created["nutrients"]) == NUTRIENT_KEYS
    assert created["nutrients"]["alcohol_g"] == 10.0 and created["nutrients"]["sugars_g"] is None
    # Alcohol is one of the values of a version: read with them, kept when a proposal
    # leaves it out, and another alcohol is another version.
    wine_id = created["id"]
    assert service.versions(wine_id)[0]["nutrients"]["alcohol_g"] == 10.0
    sweeter = {
        "basis": "100ml",
        "nutrients": {"kcal": 90, "protein_g": 0.1, "carbs_g": 4.6, "fat_g": 0},
        "contributor": BOB,
    }
    status, answer = service.post(f"/v1/foods/{wine_id}/versions", sweeter)
    assert status == 201
    assert sorted(v["nutrients"]["alcohol_g"] for v in answer["versions"]) == [10.0, 10.0]
    assert service.post(f"/v1/foods/{wine_id}/versions", sweeter)[1]["code"] == "version_exists"
    stronger = copy.deepcopy(sweeter)
    stronger["nutrients"] |= {"kcal": 104, "alcohol_g": 12}
    status, answer = service.post(f"/v1/foods/{wine_id}/versions", stronger)
    assert status == 201
    assert sorted(v["nutrients"]["alcohol_g"] for v in answer["versions"]) == [10.0, 10.0, 12.0]
    unknown = copy.deepcopy(sweeter)
    unknown["nutrients"]["alcohol_g"] = None  # said to be unknown: the energy no longer adds up
    assert service.post(f"/v1/foods/{wine_id}/versions", unknown)[1]["checks"] == [
        "energy_macro_mismatch"
    ]
    assert service.food("ciqual:13000")["nutrients"]["alcohol_g"] == 0.0


def test_versions_and_votes(service: Running) -> None:
    """Nothing is overwritten: a version beside the others, and the votes decide."""
    listed = service.versions(NUTELLA)
    assert [set(version) for version in listed] == [VERSION_KEYS, VERSION_KEYS]
    assert [(v["version_id"], v["origin"], v["votes"], v["served"]) for v in listed] == [
        ("off:3017620422003", "source", 0, True),
        ("usda:branded:3017620422003", "source", 0, False),
    ]
    assert listed[1]["source"]["title"] == "USDA FoodData Central"
    assert listed[1]["servings"] == [{"label": "2 Tbsp (37 g)", "amount": 37.0, "unit": "g"}]

    # Proposing other values adds a version, with the vote of its author.
    status, answer = service.post(f"/v1/foods/{NUTELLA}/versions", OTHER_VALUES)
    assert status == 201
    proposed = next(v for v in answer["versions"] if v["origin"] == "user")
    version_id = proposed["version_id"]
    assert version_id.startswith("kratheos:v:") and proposed["source"]["id"] == "kratheos"
    assert (proposed["votes"], proposed["served"], proposed["countries"]) == (1, False, ["FR"])
    assert proposed["nutrients"]["kcal"] == 530.0 and proposed["nutrients"]["fibre_g"] is None
    assert proposed["servings"] == [{"label": "1 cuillère", "amount": 15.0, "unit": "g"}]
    assert [v["version_id"] for v in answer["versions"]] == [
        "off:3017620422003",  # the one served, then by votes
        version_id,
        "usda:branded:3017620422003",
    ]
    food = service.food(NUTELLA)
    assert (food["version_id"], food["versions_count"], food["votes"]) == (
        "off:3017620422003",
        3,
        0,
    )

    # The same values again: vote for the version that has them instead.
    again = dict(OTHER_VALUES, contributor=BOB, countries=["CH"])
    assert service.post(f"/v1/foods/{NUTELLA}/versions", again) == (
        409,
        {
            "code": "version_exists",
            "message": "a version of this food already has exactly these values",
            "version_id": version_id,
        },
    )
    as_the_source = dict(OTHER_VALUES, contributor=BOB)
    as_the_source["nutrients"] = {
        "kcal": 539,
        "protein_g": 6.3,
        "carbs_g": 57.5,
        "sugars_g": 56.3,
        "fat_g": 30.9,
        "saturated_fat_g": 10.6,
        "fibre_g": 0,
        "salt_g": 0.107,
    }
    status, error = service.post(f"/v1/foods/{NUTELLA}/versions", as_the_source)
    assert (status, error["version_id"]) == (409, "off:3017620422003")
    absurd = dict(OTHER_VALUES, nutrients=dict(OTHER_VALUES["nutrients"], kcal=5))
    assert service.post(f"/v1/foods/{NUTELLA}/versions", absurd)[1]["checks"] == [
        "energy_macro_mismatch"
    ]

    def vote(contributor: str, target: str) -> dict[str, int]:
        """Vote and return the votes of every version."""
        status, answer = service.post(
            f"/v1/foods/{NUTELLA}/votes", {"version_id": target, "contributor": contributor}
        )
        assert status == 200, answer
        return {version["version_id"]: version["votes"] for version in answer["versions"]}

    # Two votes are not enough for a user's version to be served instead of a source.
    assert vote(BOB, version_id)[version_id] == 2
    assert vote(BOB, version_id)[version_id] == 2  # one vote per person
    assert vote(ALICE, version_id)[version_id] == 2  # the author already votes for it
    assert service.food(NUTELLA)["version_id"] == "off:3017620422003"
    # The third vote makes it the version served, to everyone.
    assert vote(CAROL, version_id)[version_id] == 3
    food = service.food(NUTELLA, "lang=fr")
    assert (food["version_id"], food["version_origin"], food["votes"]) == (version_id, "user", 3)
    assert food["nutrients"]["kcal"] == 530.0 and food["source"]["id"] == "kratheos"
    assert food["countries"] == ["FR"] and food["updated_at"] is not None
    assert food["name"] == "Nutella" and food["brand"] == "Nutella, Ferrero"  # who it is stays
    assert service.versions(NUTELLA)[0]["served"] is True
    assert service.get("/v1/barcodes/3017620422003")[1]["version_id"] == version_id
    assert service.get("/v1/foods/search?q=nutella")[1]["items"][0]["version_id"] == version_id

    # A vote moves: the counts follow, and the version served with them.
    counts = vote(CAROL, "off:3017620422003")
    assert counts == {
        "off:3017620422003": 1,
        version_id: 2,
        "usda:branded:3017620422003": 0,
    }
    assert service.food(NUTELLA)["version_id"] == "off:3017620422003"
    assert service.food(NUTELLA)["votes"] == 1
    counts = vote(DAVE, "usda:branded:3017620422003")
    counts = vote(CAROL, "usda:branded:3017620422003")
    assert counts["usda:branded:3017620422003"] == 2 and counts["off:3017620422003"] == 0
    assert service.food(NUTELLA)["version_id"] == "usda:branded:3017620422003"

    status, error = service.post(
        f"/v1/foods/{NUTELLA}/votes", {"version_id": "off:nothing", "contributor": DAVE}
    )
    assert (status, error["code"]) == (404, "not_found")
    # A version of another food is unknown to this one.
    status, error = service.post(
        "/v1/foods/ciqual:13000/votes", {"version_id": version_id, "contributor": DAVE}
    )
    assert (status, error["code"]) == (404, "not_found")
    assert service.post("/v1/foods/nothing:here/versions", OTHER_VALUES)[0] == 404
    assert service.get("/v1/foods/nothing:here/versions")[0] == 404
    assert service.get("/v1/meta")[1]["counts"]["added_since_the_build"] == {
        "products": 0,
        "versions": 1,
        "translations": 0,
        "votes": 4,
    }


def test_the_country_of_the_request_chooses_among_the_versions(service: Running) -> None:
    """A person gets the version of their market when one lists their country."""
    assert service.food(NUTELLA, "country=FR")["version_id"] == "off:3017620422003"
    american = service.food(NUTELLA, "country=US")
    assert american["version_id"] == "usda:branded:3017620422003"
    assert american["source"]["id"] == "usda" and american["countries"] == ["US"]
    assert american["nutrients"]["kcal"] == 541.0
    assert american["flags"] == ["salt_from_sodium", "carbohydrate_fiber_subtracted"]
    assert american["name"] == "Nutella hazelnut spread"  # who the food is does not change
    assert service.food(NUTELLA, "country=DE")["version_id"] == "off:3017620422003"  # nobody's
    assert service.get("/v1/barcodes/3017620422003?country=us")[1]["source"]["id"] == "usda"
    assert [v["served"] for v in service.versions(NUTELLA, "country=US")] == [True, False]
    assert service.versions(NUTELLA, "country=US")[0]["version_id"].startswith("usda:")
    found = service.get("/v1/foods/search?q=nutella&country=US")[1]["items"][0]
    assert found["version_id"] == "usda:branded:3017620422003"

    # A user's version for Switzerland is not served below the threshold, even
    # to the only country that lists it; with enough votes it is.
    swiss = dict(OTHER_VALUES, countries=["CH"])
    status, answer = service.post(f"/v1/foods/{NUTELLA}/versions?country=CH", swiss)
    version_id = next(v["version_id"] for v in answer["versions"] if v["origin"] == "user")
    assert answer["versions"][0]["version_id"] == "off:3017620422003"
    for contributor in (BOB, CAROL):
        service.post(
            f"/v1/foods/{NUTELLA}/votes", {"version_id": version_id, "contributor": contributor}
        )
    assert service.food(NUTELLA, "country=CH")["version_id"] == version_id
    assert service.food(NUTELLA, "country=US")["version_id"] == "usda:branded:3017620422003"
    assert service.food(NUTELLA, "country=FR")["version_id"] == "off:3017620422003"
    assert service.food(NUTELLA)["version_id"] == version_id  # the most voted of all

    # A version that lists no country is not "elsewhere": with its votes it is served
    # to a country another version lists, and without them it stays behind that one.
    anywhere = dict(OTHER_VALUES, countries=[], contributor=DAVE)
    anywhere["nutrients"] = dict(OTHER_VALUES["nutrients"], kcal=535)
    status, answer = service.post(f"/v1/foods/{NUTELLA}/versions?country=FR", anywhere)
    unlisted = next(
        v["version_id"] for v in answer["versions"] if v["origin"] == "user" and not v["countries"]
    )
    assert answer["versions"][0]["version_id"] == "off:3017620422003"
    for contributor in ("contributor-key-erin", "contributor-key-frank"):
        service.post(
            f"/v1/foods/{NUTELLA}/votes", {"version_id": unlisted, "contributor": contributor}
        )
    assert service.food(NUTELLA, "country=FR")["version_id"] == unlisted
    assert service.food(NUTELLA, "country=US")["version_id"] == unlisted
    # Switzerland has a version of its own with as many votes: listing the country decides.
    assert service.food(NUTELLA, "country=CH")["version_id"] == version_id


def test_versions_that_differ_are_flagged(service: Running) -> None:
    """`versions_disagree` says when the versions of a food do not give the same values."""
    assert "versions_disagree" not in service.food(NUTELLA)["flags"]  # 539 and 541 kcal agree
    cola = service.food("gtin:0049000028911")
    assert cola["versions_count"] == 2 and "versions_disagree" in cola["flags"]  # 42 and 80 kcal
    lighter = dict(OTHER_VALUES, nutrients=dict(OTHER_VALUES["nutrients"], kcal=450, fat_g=22))
    service.post(f"/v1/foods/{NUTELLA}/versions", lighter)
    assert "versions_disagree" in service.food(NUTELLA)["flags"]
    assert service.versions(NUTELLA)[0]["flags"] == []  # a flag of the food, not of a version


def test_a_product_added_keeps_being_improved(service: Running) -> None:
    """On a food no source knows, a later proposal needs more votes than what is served."""
    created = service.post("/v1/products", PRODUCT)[1]
    food_id = created["id"]
    better = dict(OTHER_VALUES, contributor=BOB)
    better["nutrients"] = dict(PRODUCT["nutrients"], kcal=260, fat_g=12)
    status, answer = service.post(f"/v1/foods/{food_id}/versions", better)
    assert status == 201 and len(answer["versions"]) == 2
    # One vote each: the values the product was created with stay, they came first.
    assert [(v["version_id"], v["votes"], v["served"]) for v in answer["versions"]] == [
        (created["version_id"], 1, True),
        (answer["versions"][1]["version_id"], 1, False),
    ]
    assert service.food(food_id)["nutrients"]["kcal"] == 250.0
    service.post(
        f"/v1/foods/{food_id}/votes",
        {"version_id": answer["versions"][1]["version_id"], "contributor": CAROL},
    )
    served = service.food(food_id)
    assert served["version_id"] == answer["versions"][1]["version_id"] and served["votes"] == 2
    assert served["nutrients"]["kcal"] == 260.0


def test_one_plausible_entry_replaces_source_values_that_make_no_sense(service: Running) -> None:
    """The threshold protects usable sources only."""
    bircher = "gtin:7610000123451"  # 50 kcal for 20 g of fat: flagged by the build
    food = service.food(bircher)
    assert food["flags"] == ["basis_assumed", "energy_macro_mismatch"]
    assert (food["version_origin"], food["versions_count"]) == ("source", 1)
    entry = dict(OTHER_VALUES, countries=["CH"])
    entry["nutrients"] = {"kcal": 460, "protein_g": 10, "carbs_g": 60, "fat_g": 20}
    status, answer = service.post(f"/v1/foods/{bircher}/versions", entry)
    assert status == 201
    assert [(v["origin"], v["votes"], v["served"]) for v in answer["versions"]] == [
        ("user", 1, True),  # one person, one vote: served at once
        ("source", 0, False),
    ]
    food = service.food(bircher, "country=CH")
    assert (food["version_origin"], food["nutrients"]["kcal"], food["flags"]) == (
        "user",
        460.0,
        ["versions_disagree"],
    )
    assert food["name"] == "Bircher Müesli"
    # A usable source is not replaced that way: the same on Nutella changes nothing.
    service.post(f"/v1/foods/{NUTELLA}/versions", dict(OTHER_VALUES, contributor=BOB))
    assert service.food(NUTELLA)["version_origin"] == "source"


def test_translation_adds_a_name_and_never_replaces_the_source(service: Running) -> None:
    """A name in a missing language is visible and searchable at once."""
    body = {"language": "de", "name": "  Aprikose,   entsteint ", "contributor": ALICE}
    status, food = service.post("/v1/foods/ciqual:13000/translations?lang=de", body)
    assert status == 200 and set(food) == FOOD_KEYS
    assert food["names"]["de"] == "Aprikose, entsteint"
    assert (food["name"], food["name_language"], food["name_origin"]) == (
        "Aprikose, entsteint",
        "de",
        "kratheos",
    )
    assert service.found("q=aprikose") == ["ciqual:13000"]
    assert service.food("ciqual:13000", "lang=fr")["name_origin"] == "source"
    # A name the source gives is never replaced.
    body = {"language": "fr", "name": "Autre nom", "contributor": ALICE}
    assert service.post("/v1/foods/ciqual:13000/translations", body) == (
        409,
        {"code": "name_exists", "message": "the source already gives a name in this language"},
    )
    # A name added in Kratheos can be improved.
    body = {"language": "de", "name": "Aprikose, entsteint, roh", "contributor": BOB}
    assert service.post("/v1/foods/ciqual:13000/translations", body)[0] == 200
    assert service.food("ciqual:13000", "lang=de")["name"] == "Aprikose, entsteint, roh"
    assert service.found("q=entsteint+roh") == ["ciqual:13000"]
    assert service.post("/v1/foods/nothing:here/translations", body)[0] == 404
    for bad in ({"language": "de"}, {"language": "deutsch", "name": "x"}, {"name": "x"}):
        status, error = service.post(
            "/v1/foods/ciqual:13000/translations", dict(bad, contributor=ALICE)
        )
        assert (status, error["code"]) == (422, "invalid_request")
    # The names of a product added in Kratheos are its own: they are not replaced either.
    food_id = service.post("/v1/products", PRODUCT)[1]["id"]
    body = {"language": "fr", "name": "Autre burger", "contributor": BOB}
    assert service.post(f"/v1/foods/{food_id}/translations", body)[0] == 409
    body = {"language": "de", "name": "Hausgemachter Testburger", "contributor": BOB}
    assert service.post(f"/v1/foods/{food_id}/translations", body)[1]["names"]["de"] == (
        "Hausgemachter Testburger"
    )
    assert service.found("q=hausgemachter") == [food_id]


def test_reports_and_moderation(service: Running) -> None:
    """A report is recorded; a hidden food does not exist for any read."""
    food_id = service.post("/v1/products", PRODUCT)[1]["id"]
    report = {"reason": "inappropriate", "note": "Ce n'est pas un aliment.", "contributor": BOB}
    assert service.post(f"/v1/foods/{food_id}/reports", report) == (
        202,
        {
            "id": 1,
            "state": "open",
            "replaced": False,
            "reporters": 1,
            "reasons": {"inappropriate": 1},
        },
    )
    second = {"reason": "wrong_values", "contributor": CAROL}
    assert service.post(f"/v1/foods/{NUTELLA}/reports", second)[1]["id"] == 2
    assert service.post(f"/v1/foods/{NUTELLA}/reports", dict(second, reason="boring"))[0] == 422
    assert service.post("/v1/foods/nothing:here/reports", report)[0] == 404

    status, listed = service.get("/v1/moderation/reports?state=open&lang=fr", token=TOKEN)
    assert status == 200 and listed["next_offset"] is None
    assert [(item["id"], item["food_id"], item["reason"]) for item in listed["items"]] == [
        (1, food_id, "inappropriate"),
        (2, NUTELLA, "wrong_values"),
    ]
    first = listed["items"][0]
    assert set(first) == {
        "id",
        "food_id",
        "reason",
        "note",
        "state",
        "created_at",
        "closed_at",
        "hidden",
        "food",
    }
    assert first["note"] == "Ce n'est pas un aliment." and first["hidden"] is False
    assert first["food"]["name"] == "Burger maison du test"
    assert "contributor" not in str(listed) and BOB not in str(listed)

    # Hidden: no read endpoint returns it any more.
    assert service.post(f"/v1/moderation/foods/{food_id}/hide") == (
        200,
        {"id": food_id, "hidden": True},
    )
    assert service.get(f"/v1/foods/{food_id}")[0] == 404
    assert service.get(f"/v1/foods/{food_id}/versions")[0] == 404
    assert service.found("q=burger+maison") == []
    assert service.post(f"/v1/foods/{food_id}/votes", {"version_id": food_id})[0] in (404, 422)
    # Moderation still sees it, marked as hidden.
    hidden = service.get("/v1/moderation/reports", token=TOKEN)[1]["items"][0]
    assert hidden["hidden"] is True and hidden["food"]["id"] == food_id
    assert service.post(f"/v1/moderation/foods/{food_id}/restore")[1]["hidden"] is False
    assert service.food(food_id)["id"] == food_id
    assert service.found("q=burger+maison") == [food_id]

    # A food of a source can be hidden too, and so can its barcode.
    service.post(f"/v1/moderation/foods/{NUTELLA}/hide")
    assert service.get("/v1/barcodes/3017620422003")[0] == 404
    assert service.found("q=nutella") == [] and service.found("q=3017620422003") == []
    taken = dict(PRODUCT, barcode="3017620422003")
    assert service.post("/v1/products", taken)[1]["code"] == "barcode_exists"
    service.post(f"/v1/moderation/foods/{NUTELLA}/restore")
    assert service.post("/v1/moderation/foods/nothing:here/hide")[0] == 404

    # One version can be hidden: it is neither listed nor served.
    status, answer = service.post(f"/v1/foods/{NUTELLA}/versions", OTHER_VALUES)
    version_id = next(v["version_id"] for v in answer["versions"] if v["origin"] == "user")
    for contributor in (BOB, CAROL):
        service.post(
            f"/v1/foods/{NUTELLA}/votes", {"version_id": version_id, "contributor": contributor}
        )
    assert service.food(NUTELLA)["version_id"] == version_id
    assert service.post(f"/v1/moderation/foods/{version_id}/hide")[0] == 200
    food = service.food(NUTELLA)
    assert (food["version_id"], food["versions_count"]) == ("off:3017620422003", 2)
    assert version_id not in [v["version_id"] for v in service.versions(NUTELLA)]
    assert service.post("/v1/moderation/foods/usda:branded:3017620422003/hide")[0] == 200
    assert service.food(NUTELLA, "country=US")["version_id"] == "off:3017620422003"
    assert service.food(NUTELLA)["versions_count"] == 1

    assert service.post("/v1/moderation/reports/1/close") == (200, {"id": 1, "state": "closed"})
    assert service.post("/v1/moderation/reports/99/close")[0] == 404
    assert service.post("/v1/moderation/reports/first/close")[0] == 422
    still_open = service.get("/v1/moderation/reports", token=TOKEN)[1]["items"]
    assert [item["id"] for item in still_open] == [2]
    closed = service.get("/v1/moderation/reports?state=closed", token=TOKEN)[1]["items"]
    assert [(item["id"], item["state"]) for item in closed] == [(1, "closed")]
    assert closed[0]["closed_at"] is not None
    assert len(service.get("/v1/moderation/reports?state=all", token=TOKEN)[1]["items"]) == 2
    page = service.get("/v1/moderation/reports?state=all&limit=1", token=TOKEN)[1]
    assert len(page["items"]) == 1 and page["next_offset"] == 1


def test_contributor_keys_are_stored_hashed_and_owner_only(service: Running) -> None:
    """The live database never holds a key as it was received."""
    service.post("/v1/products", PRODUCT)
    service.post(f"/v1/foods/{NUTELLA}/versions", dict(OTHER_VALUES, contributor=BOB))
    service.post(
        f"/v1/foods/{NUTELLA}/reports", {"reason": "other", "note": "x", "contributor": CAROL}
    )
    database = sqlite3.connect(service.settings.live)
    hashes = [row[0] for row in database.execute("SELECT contributor FROM votes")]
    hashes += [row[0] for row in database.execute("SELECT contributor FROM reports")]
    hashes += [row[0] for row in database.execute("SELECT contributor FROM history")]
    database.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    database.close()
    assert len(hashes) == 5 and all(len(value) == 64 for value in hashes)
    assert service.store.contributor_hash(ALICE) in hashes
    raw = service.settings.live.read_bytes()
    for key in (ALICE, BOB, CAROL):
        assert key.encode() not in raw
    assert service.settings.live.stat().st_mode & 0o077 == 0
    # Another installation hashes the same key differently.
    other = load_settings(
        service.settings.live.parent,
        environment={"FOOD_DATA_KEY_SECRET": "another-secret-0123456789"},
    )
    assert other.key_secret != service.settings.key_secret


def test_key_secret_is_created_once_next_to_the_live_database(tmp_path: Path) -> None:
    """Without a configured secret, one is created, readable by its owner only."""
    first = load_settings(tmp_path, environment={})
    key_file = tmp_path / "data" / "contributor.key"
    assert key_file.stat().st_mode & 0o777 == 0o600 and len(first.key_secret) == 64
    assert load_settings(tmp_path, environment={}).key_secret == first.key_secret
    assert first.database == tmp_path / "data" / "food.sqlite"
    assert first.live == tmp_path / "data" / "live.sqlite" and first.token == ""
    elsewhere = load_settings(
        tmp_path, environment={"FOOD_DATA_DIR": str(tmp_path / "srv"), "FOOD_DATA_PORT": "9000"}
    )
    assert elsewhere.database == tmp_path / "srv" / "food.sqlite" and elsewhere.port == 9000
    with pytest.raises(SettingsError, match="FOOD_DATA_PORT"):
        load_settings(tmp_path, environment={"FOOD_DATA_PORT": "http"})
    with pytest.raises(SettingsError, match="FOOD_DATA_KEY_SECRET"):
        load_settings(tmp_path, environment={"FOOD_DATA_KEY_SECRET": "short"})
