"""Tests of moderation on the miniature build: reports counted per person, and contributor keys.

A food is reported once per person, and the moderation list tells how many
different people reported each food. A moderator can ask what a contributor
key wrote, hide all of it and refuse its later writes in one call, and undo
both: the undo must give back exactly what was served before. Nothing about
who ever reaches a read endpoint or the export.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from service_tools import (
    ALICE,
    BOB,
    CAROL,
    DAVE,
    KEY_SECRET,
    PRODUCT,
    TOKEN,
    Running,
    start_service,
)

from kratheos_food_data import kratheos
from kratheos_food_data.build import Workspace
from kratheos_food_data.service import export, live
from kratheos_food_data.service.live import LiveStore

NUTELLA = "gtin:3017620422003"
APRICOT = "ciqual:13000"
OFF_VERSION = "off:3017620422003"
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
    "servings": [],
    "countries": ["FR"],
}
BLOCKED = (
    403,
    {
        "code": "contributor_blocked",
        "message": "this contributor may no longer write to the catalogue",
    },
)


@pytest.fixture
def service(built_repository: Workspace, tmp_path: Path) -> Iterator[Running]:
    """Give a running service over a copy of the miniature repository."""
    running = start_service(built_repository, tmp_path)
    yield running
    running.close()


def report(service: Running, food_id: str, contributor: str, reason: str, note: str = "") -> dict:
    """Report a food and return the answer of the service.

    Args:
        service: The running service.
        food_id: Identifier of the food.
        contributor: The contributor key.
        reason: Reason of the report.
        note: What the person wrote.

    Returns:
        The JSON object of the answer.
    """
    body = {"reason": reason, "note": note, "contributor": contributor}
    status, answer = service.post(f"/v1/foods/{food_id}/reports", body)
    assert status == 202, answer
    return answer


def propose(service: Running, food_id: str, contributor: str, kcal: int = 530) -> str:
    """Propose other values for a food and return the identifier of the new version.

    Args:
        service: The running service.
        food_id: Identifier of the food.
        contributor: The contributor key.
        kcal: Energy of the proposal, to tell two proposals apart.

    Returns:
        The identifier of the version created.
    """
    body = dict(OTHER_VALUES, contributor=contributor)
    body["nutrients"] = dict(OTHER_VALUES["nutrients"], kcal=kcal)
    status, answer = service.post(f"/v1/foods/{food_id}/versions", body)
    assert status == 201, answer
    assert answer["version_id"] in [version["version_id"] for version in answer["versions"]]
    return answer["version_id"]


def vote(service: Running, food_id: str, version_id: str, contributor: str) -> tuple[int, dict]:
    """Vote for a version of a food.

    Args:
        service: The running service.
        food_id: Identifier of the food.
        version_id: Version voted for.
        contributor: The contributor key.

    Returns:
        The status and the JSON object of the answer.
    """
    body = {"version_id": version_id, "contributor": contributor}
    return service.post(f"/v1/foods/{food_id}/votes", body)


def moderation(service: Running, target: str) -> dict:
    """Read a moderation endpoint with the service token.

    Args:
        service: The running service.
        target: Path and query.

    Returns:
        The JSON object of the answer.
    """
    status, answer = service.get(target, token=TOKEN)
    assert status == 200, answer
    return answer


def test_one_report_per_person_and_the_most_reported_food_first(service: Running) -> None:
    """Reporting a food again replaces the earlier report; foods are listed by people."""
    first = report(service, APRICOT, DAVE, "duplicate")
    assert first == {
        "id": 1,
        "state": "open",
        "replaced": False,
        "reporters": 1,
        "reasons": {"duplicate": 1},
    }
    assert report(service, NUTELLA, BOB, "wrong_values")["id"] == 2
    again = report(service, NUTELLA, BOB, "inappropriate", "Ce nom est une insulte.")
    assert (again["id"], again["replaced"], again["reporters"]) == (2, True, 1)
    assert again["reasons"] == {"inappropriate": 1}
    third = report(service, NUTELLA, CAROL, "inappropriate")
    assert (third["id"], third["replaced"], third["reporters"]) == (3, False, 2)
    last = report(service, NUTELLA, DAVE, "other")
    assert last["reasons"] == {"inappropriate": 2, "other": 1} and last["reporters"] == 3

    listed = moderation(service, "/v1/moderation/foods?lang=fr")
    assert listed["next_offset"] is None
    assert [(item["food_id"], item["reporters"]) for item in listed["items"]] == [
        (NUTELLA, 3),
        (APRICOT, 1),
    ]
    nutella = listed["items"][0]
    assert set(nutella) == {
        "food_id",
        "reporters",
        "reasons",
        "first_reported_at",
        "last_reported_at",
        "reports",
        "hidden",
        "food",
    }
    assert list(nutella["reasons"].items()) == [("inappropriate", 2), ("other", 1)]
    assert [(row["id"], row["reason"], row["note"]) for row in nutella["reports"]] == [
        (2, "inappropriate", "Ce nom est une insulte."),
        (3, "inappropriate", ""),
        (4, "other", ""),
    ]
    assert set(nutella["reports"][0]) == {
        "id",
        "reason",
        "note",
        "state",
        "created_at",
        "closed_at",
    }
    assert nutella["food"]["name"] == "Nutella" and nutella["hidden"] is False
    # Never who: no key, no hash of a key.
    for key in (BOB, CAROL, DAVE):
        assert key not in str(listed) and service.store.contributor_hash(key) not in str(listed)
    # The list of the reports one by one is still there, one line per person and food.
    assert len(moderation(service, "/v1/moderation/reports")["items"]) == 4
    assert service.get("/v1/moderation/foods")[0] == 401
    page = moderation(service, "/v1/moderation/foods?limit=1")
    assert [item["food_id"] for item in page["items"]] == [NUTELLA] and page["next_offset"] == 1

    # Reports hide nothing by themselves, whatever their number.
    assert service.food(NUTELLA)["id"] == NUTELLA

    # Every report about a food is closed in one call; a later one starts again from one.
    assert service.post(f"/v1/moderation/foods/{NUTELLA}/reports/close") == (
        200,
        {"id": NUTELLA, "closed": 3},
    )
    assert service.post(f"/v1/moderation/foods/{NUTELLA}/reports/close")[1]["closed"] == 0
    assert [item["food_id"] for item in moderation(service, "/v1/moderation/foods")["items"]] == [
        APRICOT
    ]
    closed = moderation(service, "/v1/moderation/foods?state=closed")["items"]
    assert [(item["food_id"], item["reporters"]) for item in closed] == [(NUTELLA, 3)]
    assert all(row["closed_at"] for row in closed[0]["reports"])
    fresh = report(service, NUTELLA, BOB, "wrong_values")
    assert (fresh["id"], fresh["replaced"], fresh["reporters"]) == (5, False, 1)
    everything = moderation(service, "/v1/moderation/foods?state=all")["items"]
    assert [(item["food_id"], item["reporters"]) for item in everything] == [
        (NUTELLA, 3),
        (APRICOT, 1),
    ]


def test_what_a_key_wrote_and_who_wrote_a_record(service: Running) -> None:
    """The history links a product, a version or a name to the hashed key that wrote it."""
    burger = service.post("/v1/products", PRODUCT)[1]
    bar = service.post(
        "/v1/products", dict(PRODUCT, barcode="7613034626844", names={"fr": "Barre du test"})
    )[1]
    version = propose(service, NUTELLA, ALICE)
    assert vote(service, NUTELLA, version, BOB)[0] == 200
    name = {"language": "de", "name": "Aprikose", "contributor": ALICE}
    assert service.post(f"/v1/foods/{APRICOT}/translations", name)[0] == 200
    better = {"language": "de", "name": "Aprikose, roh", "contributor": BOB}
    assert service.post(f"/v1/foods/{APRICOT}/translations", better)[0] == 200
    italian = {"language": "it", "name": "Albicocca", "contributor": ALICE}
    assert service.post(f"/v1/foods/{APRICOT}/translations", italian)[0] == 200
    report(service, APRICOT, ALICE, "other", "note d'Alice")
    alice, bob = (service.store.contributor_hash(key) for key in (ALICE, BOB))

    status, written = service.post("/v1/moderation/contributors/lookup", {"contributor": ALICE})
    assert status == 200
    assert (written["contributor"], written["blocked"], written["blocked_at"]) == (
        alice,
        False,
        None,
    )
    assert written["counts"] == {
        "products": 2,
        "versions": 1,
        "translations": 2,
        "portions": 0,
        "votes": 3,
        "reports": 1,
    }
    assert [(row["food_id"], row["record_id"], row["hidden"]) for row in written["products"]] == [
        (bar["id"], bar["version_id"], False),  # most recent first
        (burger["id"], burger["id"], False),
    ]
    assert written["products"][0]["name"] == "Barre du test"
    assert [(row["version_id"], row["food_id"]) for row in written["versions"]] == [
        (version, NUTELLA)
    ]
    assert [(row["language"], row["name"], row["current"]) for row in written["translations"]] == [
        ("it", "Albicocca", True),
        ("de", "Aprikose", False),  # Bob gave another name since
    ]
    assert {(row["food_id"], row["version_id"]) for row in written["votes"]} == {
        (burger["id"], burger["version_id"]),
        (bar["id"], bar["version_id"]),
        (NUTELLA, version),
    }
    assert [(row["food_id"], row["reason"], row["note"]) for row in written["reports"]] == [
        (APRICOT, "other", "note d'Alice")
    ]
    assert ALICE not in str(written)
    nobody = service.post("/v1/moderation/contributors/lookup", {"contributor": DAVE})[1]
    assert set(nobody["counts"].values()) == {0} and nobody["products"] == []

    def writers(record_id: str) -> list[tuple[str, list[str]]]:
        """Return the hashed keys that wrote a record, with what each did."""
        answer = moderation(service, f"/v1/moderation/foods/{record_id}/contributors")
        assert answer["id"] == record_id
        keys = {"contributor", "actions", "first_at", "last_at"}
        assert all(set(row) == keys for row in answer["contributors"])
        return [(row["contributor"], row["actions"]) for row in answer["contributors"]]

    assert writers(burger["id"]) == [(alice, ["create_product"])]
    # A product with a barcode is found under its food and under its own record.
    assert writers(bar["id"]) == [(alice, ["create_product"])]
    assert writers(bar["version_id"]) == [(alice, ["create_product"])]
    assert writers(version) == [(alice, ["add_version"]), (bob, ["vote"])]
    assert writers(APRICOT) == [(alice, ["translate"]), (bob, ["translate"])]
    assert writers(NUTELLA) == []  # a food of a source: nobody wrote it
    assert service.get("/v1/moderation/foods/nothing:here/contributors", token=TOKEN)[0] == 404
    assert service.get(f"/v1/moderation/foods/{burger['id']}/contributors")[0] == 401
    assert (
        service.post("/v1/moderation/contributors/lookup", {"contributor": ALICE}, None)[0] == 401
    )
    for bad in ({}, {"contributor": "short"}, {"contributor": ALICE, "note": "x"}):
        for action in ("lookup", "block", "unblock"):
            assert service.post(f"/v1/moderation/contributors/{action}", bad)[0] == 422


def seen(service: Running, targets: list[str]) -> dict:
    """Ask the service what people and moderators are served.

    Args:
        service: The running service.
        targets: Paths and queries to read.

    Returns:
        The status and the answer of each request, by target.
    """
    return {target: service.get(target, token=TOKEN) for target in targets}


def test_blocking_a_key_hides_what_it_wrote_and_the_undo_restores_exactly(
    service: Running, tmp_path: Path
) -> None:
    """One call hides the products, the versions and the names of a key; its votes stop counting."""
    spam = service.post("/v1/products", dict(PRODUCT, names={"fr": "Produit indésirable"}))[1]
    kept = service.post("/v1/products", dict(PRODUCT, contributor=BOB))[1]
    # Hidden by a moderator before the block: it must stay hidden after the undo.
    apart = service.post("/v1/products", dict(PRODUCT, names={"fr": "Masqué à part"}))[1]
    assert service.post(f"/v1/moderation/foods/{apart['id']}/hide")[0] == 200
    # A version of Bob that the vote of Alice makes the one served.
    by_bob = propose(service, NUTELLA, BOB)
    assert vote(service, NUTELLA, by_bob, CAROL)[0] == 200
    assert vote(service, NUTELLA, by_bob, ALICE)[0] == 200
    by_alice = propose(service, kept["id"], ALICE, kcal=520)
    name = {"language": "de", "name": "Unsinn", "contributor": ALICE}
    assert service.post(f"/v1/foods/{APRICOT}/translations", name)[0] == 200
    report(service, kept["id"], ALICE, "inappropriate")
    report(service, kept["id"], DAVE, "duplicate")
    assert service.food(NUTELLA)["version_id"] == by_bob
    assert service.food(NUTELLA)["votes"] == 3

    targets = [
        f"/v1/foods/{NUTELLA}",
        f"/v1/foods/{NUTELLA}/versions",
        f"/v1/foods/{spam['id']}?lang=fr",
        f"/v1/foods/{spam['id']}/versions",
        f"/v1/foods/{apart['id']}",
        f"/v1/foods/{kept['id']}",
        f"/v1/foods/{kept['id']}/versions",
        f"/v1/foods/{APRICOT}?lang=de",
        "/v1/foods/search?q=indesirable",
        "/v1/foods/search?q=unsinn",
        "/v1/foods/search?q=burger",
        "/v1/foods/search?q=nutella",
        "/v1/moderation/foods",
        "/v1/moderation/reports",
        "/v1/meta",
    ]
    before = seen(service, targets)
    exported_before = export.read_live(service.settings.live)
    hidden_before = _table(service, "hidden")
    assert before[f"/v1/foods/{spam['id']}?lang=fr"][0] == 200
    assert before[f"/v1/foods/{apart['id']}"][0] == 404

    alice = service.store.contributor_hash(ALICE)
    status, done = service.post("/v1/moderation/contributors/block", {"contributor": ALICE})
    assert status == 200
    assert set(done) == {"blocked", "contributor", "blocked_at", "withdrawn"}
    assert (done["blocked"], done["contributor"]) == (True, alice)
    assert done["withdrawn"] == {"products": 2, "versions": 1, "translations": 1, "portions": 0}

    # What the key wrote is invisible, like any hidden record.
    assert service.get(f"/v1/foods/{spam['id']}")[0] == 404
    assert service.get(f"/v1/foods/{spam['id']}/versions")[0] == 404
    assert service.found("q=indesirable") == [] and service.found("q=unsinn") == []
    assert by_alice not in [v["version_id"] for v in service.versions(kept["id"])]
    assert service.food(kept["id"])["versions_count"] == 1
    assert "de" not in service.food(APRICOT)["names"]
    # Its vote no longer counts: the version served changes back.
    food = service.food(NUTELLA)
    assert (food["version_id"], food["version_origin"]) == (OFF_VERSION, "source")
    votes = {v["version_id"]: v["votes"] for v in service.versions(NUTELLA)}
    assert votes[by_bob] == 2
    # Its report is left out of the moderation lists.
    listed = moderation(service, "/v1/moderation/foods")["items"]
    assert [(item["food_id"], item["reporters"], item["reasons"]) for item in listed] == [
        (kept["id"], 1, {"duplicate": 1})
    ]
    assert len(moderation(service, "/v1/moderation/reports")["items"]) == 1
    looked = service.post("/v1/moderation/contributors/lookup", {"contributor": ALICE})[1]
    assert looked["blocked"] is True and looked["blocked_at"] == done["blocked_at"]
    assert [row["hidden"] for row in looked["products"]] == [True, True]
    assert [row["hidden"] for row in looked["versions"]] == [True]

    # Every later write of the key is refused; the others go on.
    writes = [
        ("/v1/products", dict(PRODUCT, names={"fr": "Encore un"})),
        (f"/v1/foods/{NUTELLA}/versions", dict(OTHER_VALUES, contributor=ALICE)),
        (f"/v1/foods/{NUTELLA}/votes", {"version_id": by_bob, "contributor": ALICE}),
        (
            f"/v1/foods/{APRICOT}/translations",
            {"language": "it", "name": "Albicocca", "contributor": ALICE},
        ),
        (f"/v1/foods/{NUTELLA}/reports", {"reason": "other", "contributor": ALICE}),
    ]
    for target, body in writes:
        assert service.post(target, body) == BLOCKED, target
    assert vote(service, NUTELLA, by_bob, CAROL)[0] == 200  # she already votes for it

    # Nothing of it is exported, and the vote is not in the counts.
    layer = export.read_live(service.settings.live)
    assert spam["id"] not in ["kratheos:" + product["id"] for product in layer.products]
    assert [version["version_id"] for version in layer.versions] == [by_bob]
    assert layer.translations == []
    assert {row["version_id"]: row["votes"] for row in layer.votes}[by_bob] == 2
    assert all(row["version_id"] != spam["version_id"] for row in layer.votes)
    out = tmp_path / "blocked.sqlite"
    export.export_layer(service.settings.live, out)
    raw = out.read_bytes()
    assert alice.encode() not in raw and ALICE.encode() not in raw
    database = sqlite3.connect(out)
    tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master")}
    database.close()
    assert not tables & {"blocked", "withdrawn", "history", "reports", "hidden"}
    kratheos.read_layer(out)  # the closed layout of the build still holds

    # Blocking twice changes nothing; what moderation hid one by one is untouched.
    again = service.post("/v1/moderation/contributors/block", {"contributor": ALICE})[1]
    assert again == done
    assert _table(service, "hidden") == hidden_before

    # The undo: exactly what was served before, to people and to moderators.
    status, undone = service.post("/v1/moderation/contributors/unblock", {"contributor": ALICE})
    assert status == 200
    assert undone == {
        "blocked": False,
        "blocked_at": None,
        "contributor": alice,
        "restored": {"products": 2, "versions": 1, "translations": 1, "portions": 0},
    }
    assert seen(service, targets) == before
    assert service.food(NUTELLA)["version_id"] == by_bob and service.food(NUTELLA)["votes"] == 3
    assert service.get(f"/v1/foods/{apart['id']}")[0] == 404  # hidden one by one: still hidden
    assert _table(service, "withdrawn") == [] and _table(service, "blocked") == []
    restored = export.read_live(service.settings.live)
    assert restored.products == exported_before.products
    assert restored.versions == exported_before.versions
    assert restored.votes == exported_before.votes
    assert restored.translations == exported_before.translations
    assert restored.meta == exported_before.meta
    assert vote(service, NUTELLA, OFF_VERSION, ALICE)[0] == 200  # the key writes again
    unknown = service.post("/v1/moderation/contributors/unblock", {"contributor": CAROL})[1]
    assert unknown["restored"] == {"products": 0, "versions": 0, "translations": 0, "portions": 0}


def _table(service: Running, table: str) -> list[tuple]:
    """Read a whole table of the live database.

    Args:
        service: The running service.
        table: Name of the table.

    Returns:
        Its rows, in a stable order.
    """
    database = sqlite3.connect(service.settings.live)
    try:
        return sorted(database.execute(f"SELECT * FROM {table}").fetchall())
    finally:
        database.close()


def test_a_name_given_by_somebody_else_during_a_block_stays(service: Running) -> None:
    """Undoing a block never brings back a name that was replaced meanwhile."""
    name = {"language": "de", "name": "Unsinn", "contributor": ALICE}
    assert service.post(f"/v1/foods/{APRICOT}/translations", name)[0] == 200
    service.post("/v1/moderation/contributors/block", {"contributor": ALICE})
    assert "de" not in service.food(APRICOT)["names"]
    better = {"language": "de", "name": "Aprikose, roh", "contributor": BOB}
    status, food = service.post(f"/v1/foods/{APRICOT}/translations?lang=de", better)
    assert status == 200 and food["name"] == "Aprikose, roh"
    undone = service.post("/v1/moderation/contributors/unblock", {"contributor": ALICE})[1]
    assert undone["restored"] == {"products": 0, "versions": 0, "translations": 0, "portions": 0}
    assert service.food(APRICOT, "lang=de")["name"] == "Aprikose, roh"
    assert service.found("q=aprikose") == [APRICOT] and service.found("q=unsinn") == []


def test_a_live_database_of_the_earlier_layout_is_upgraded(tmp_path: Path) -> None:
    """Layout 1 gains the tables of layouts 2 and 3 when it is opened, and loses nothing."""
    path = tmp_path / "live.sqlite"
    store = LiveStore(path, KEY_SECRET.encode())
    store.report(NUTELLA, "other", "note", BOB)
    store.close()
    database = sqlite3.connect(path)
    database.executescript(
        """
        DROP TABLE blocked;
        DROP TABLE withdrawn;
        DROP TABLE portions;
        DROP INDEX reports_food;
        DROP INDEX history_record;
        DROP INDEX history_contributor;
        UPDATE meta SET value = '1' WHERE key = 'schema_version';
        """
    )
    database.close()
    assert export.read_live(path).rows == 0  # the export reads a layout 1 as it is

    store = LiveStore(path, KEY_SECRET.encode())
    assert store.created is False
    reader = store.reader()
    assert reader.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0] == (
        str(live.SCHEMA_VERSION)
    )
    assert live.SCHEMA_VERSION == 3
    assert live.open_reports_of(reader, NUTELLA) == {"reporters": 1, "reasons": {"other": 1}}
    assert store.block(ALICE)["withdrawn"] == {
        "products": 0,
        "versions": 0,
        "translations": 0,
        "portions": 0,
    }
    assert live.blocked_at(reader, store.contributor_hash(ALICE)) is not None
    reader.close()
    store.close()
    database = sqlite3.connect(path)
    database.execute("UPDATE meta SET value = '7' WHERE key = 'schema_version'")
    database.commit()
    database.close()
    with pytest.raises(ValueError, match="layout 7"):
        LiveStore(path, KEY_SECRET.encode())
