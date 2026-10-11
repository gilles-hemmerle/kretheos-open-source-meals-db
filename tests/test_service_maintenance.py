"""Tests of what replaces the data of the service: export, rebuild, installation.

The whole loop is played on the miniature build: write through the API,
export the public layer, rebuild, swap the new database in under a running
service, and find the same foods, the same versions and the same votes,
nothing counted twice. Then the ways it must fail safely: a rebuild that
loses too much, a download that is not the file announced, an export that
would empty the layer.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from conftest import FileServer, quiet
from service_tools import ALICE, BOB, CAROL, DAVE, PRODUCT, Running, start_service

from kratheos_food_data import cli, kratheos, mirror, sqlite_export
from kratheos_food_data.build import BuildOptions, Workspace, run_build
from kratheos_food_data.download import DownloadError
from kratheos_food_data.service import export, maintenance

NUTELLA = "gtin:3017620422003"
VALUES = {
    "kcal": 530,
    "protein_g": 6,
    "carbs_g": 56,
    "sugars_g": 55,
    "fat_g": 31,
    "saturated_fat_g": 11,
    "fibre_g": None,
    "salt_g": 0.1,
}


@pytest.fixture
def service(built_repository: Workspace, tmp_path: Path) -> Iterator[Running]:
    """Give a running service over a copy of the miniature repository."""
    running = start_service(built_repository, tmp_path)
    yield running
    running.close()


def write_through_the_api(service: Running) -> dict[str, str]:
    """Add products, a version, votes, a name, a report, and hide one product.

    Args:
        service: The running service.

    Returns:
        The identifiers of what was created, by a short name.
    """
    burger = service.post("/v1/products", PRODUCT)[1]["id"]
    bar = service.post(
        "/v1/products", dict(PRODUCT, barcode="7613034626844", names={"fr": "Barre du test"})
    )[1]
    spam = service.post("/v1/products", dict(PRODUCT, names={"fr": "Produit indésirable"}))[1]["id"]
    proposal = {"basis": "100g", "nutrients": VALUES, "countries": ["FR"], "contributor": ALICE}
    proposal["servings"] = [{"label": "1 cuillère", "amount": 15, "unit": "g"}]
    listed = service.post(f"/v1/foods/{NUTELLA}/versions", proposal)[1]["versions"]
    version = next(v["version_id"] for v in listed if v["origin"] == "user")
    for contributor in (BOB, CAROL, DAVE):
        service.post(
            f"/v1/foods/{NUTELLA}/votes", {"version_id": version, "contributor": contributor}
        )
    # One vote moves to the record of a source; a generic food gets a vote too.
    service.post(
        f"/v1/foods/{NUTELLA}/votes", {"version_id": "off:3017620422003", "contributor": DAVE}
    )
    service.post("/v1/foods/swiss:273/votes", {"version_id": "swiss:273", "contributor": DAVE})
    service.post(
        "/v1/foods/ciqual:13000/translations",
        {"language": "de", "name": "Aprikose, entsteint, roh", "contributor": BOB},
    )
    service.post(
        f"/v1/foods/{spam}/reports",
        {"reason": "inappropriate", "note": "note du signalement", "contributor": CAROL},
    )
    assert service.post(f"/v1/moderation/foods/{spam}/hide")[0] == 200
    return {
        "burger": burger,
        "bar": bar["id"],
        "bar_record": bar["version_id"],
        "spam": spam,
        "version": version,
    }


def answers(service: Running, ids: dict[str, str]) -> dict:
    """Ask the service everything the loop compares before and after a rebuild.

    Args:
        service: The running service.
        ids: The identifiers created by `write_through_the_api`.

    Returns:
        Food records, versions and search results, by request.
    """
    targets = [
        f"/v1/foods/{NUTELLA}?lang=fr",
        f"/v1/foods/{NUTELLA}?lang=en&country=US",
        f"/v1/foods/{NUTELLA}/versions",
        f"/v1/foods/{NUTELLA}/versions?country=US",
        f"/v1/foods/{ids['burger']}?lang=fr",
        f"/v1/foods/{ids['burger']}/versions",
        f"/v1/foods/{ids['bar']}",
        "/v1/barcodes/7613034626844",
        "/v1/foods/ciqual:13000?lang=de",
        "/v1/foods/swiss:273",
        f"/v1/foods/{ids['spam']}",
        "/v1/foods/search?q=burger&country=CH",
        "/v1/foods/search?q=aprikose",
        "/v1/foods/search?q=barre+du+test",
        "/v1/foods/search?q=nutella&country=FR",
        "/v1/foods/search?q=indesirable",
        "/v1/foods/search?q=7613034626844",
    ]
    return {target: service.get(target) for target in targets}


def test_export_is_public_complete_and_always_the_same(service: Running, tmp_path: Path) -> None:
    """What users see and nothing else, in the layout the build enforces, byte for byte."""
    empty = tmp_path / "empty.sqlite"
    summary = export.export_layer(service.settings.live, empty)
    fresh = tmp_path / "fresh.sqlite"
    kratheos.create_database(fresh)
    assert empty.read_bytes() == fresh.read_bytes()  # nothing entered: the empty layer
    assert summary["products"] == 0 and summary["exported_at"] == ""

    ids = write_through_the_api(service)
    first, second = tmp_path / "first.sqlite", tmp_path / "second.sqlite"
    summary = export.export_layer(service.settings.live, first)
    assert export.export_layer(service.settings.live, second)["sha256"] == summary["sha256"]
    assert first.read_bytes() == second.read_bytes()
    again = export.export_layer(service.settings.live, first)
    assert again["changed"] is False and again["sha256"] == summary["sha256"]
    assert {key: summary[key] for key in ("products", "versions", "votes", "translations")} == {
        "products": 2,  # the hidden one is left out
        "versions": 1,
        "votes": 7,
        "translations": 1,
    }

    layer = kratheos.read_layer(first)  # the layout check of the build passes
    assert sorted(product["name"] for product in layer.products) == [
        "Barre du test",
        "Burger maison du test",
    ]
    burger = next(p for p in layer.products if p["name"] == "Burger maison du test")
    assert "kratheos:" + burger["id"] == ids["burger"]
    assert json.loads(burger["names"]) == {"en": "Home-made test burger"}
    assert (burger["name_lang"], burger["countries"], burger["serving_text"]) == (
        "fr",
        '["CH", "FR"]',
        "1 burger",
    )
    assert len(burger["updated_at"]) == 10
    (version,) = layer.versions
    assert (version["version_id"], version["food_id"]) == (ids["version"], NUTELLA)
    assert (version["energy_kcal"], version["fiber_g"], version["countries"]) == (
        530.0,
        None,
        '["FR"]',
    )
    votes = {row["version_id"]: (row["food_id"], row["votes"]) for row in layer.votes}
    assert votes == {
        ids["version"]: (NUTELLA, 3),
        "off:3017620422003": (NUTELLA, 1),
        ids["burger"]: (ids["burger"], 1),
        ids["bar_record"]: (ids["bar"], 1),
        "swiss:273": ("swiss:273", 1),
    }
    assert [(row["record_id"], row["lang"], row["origin"]) for row in layer.translations] == [
        ("ciqual:13000", "de", "human")
    ]

    # Never who: no key, no hash of a key, no report, nothing that was hidden.
    raw = first.read_bytes()
    for secret in (ALICE, BOB, CAROL, DAVE, "note du signalement", "indésirable", "inappropriate"):
        assert secret.encode("utf-8") not in raw, secret
    for contributor in (ALICE, BOB, CAROL, DAVE):
        assert service.store.contributor_hash(contributor).encode() not in raw
    database = sqlite3.connect(first)
    tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master")}
    database.close()
    assert not tables & {"reports", "hidden", "history", "anonymous_votes"}

    # Hiding one version leaves it out, with its votes.
    service.post(f"/v1/moderation/foods/{ids['version']}/hide")
    without = kratheos.read_layer(_exported(service, tmp_path / "third.sqlite"))
    assert without.versions == [] and ids["version"] not in [v["version_id"] for v in without.votes]


def _exported(service: Running, path: Path) -> Path:
    """Export the live database of a service to a file and return its path."""
    export.export_layer(service.settings.live, path)
    return path


def test_an_empty_live_database_does_not_empty_the_layer(service: Running, tmp_path: Path) -> None:
    """A lost live database is noticed before it erases what was published."""
    write_through_the_api(service)
    layer = _exported(service, tmp_path / "kratheos.sqlite")
    published = layer.read_bytes()
    lost = tmp_path / "lost" / "live.sqlite"
    from kratheos_food_data.service.live import LiveStore

    LiveStore(lost, b"secret-of-another-installation").close()
    with pytest.raises(export.ExportRefused, match="holds nothing"):
        export.export_layer(lost, layer)
    assert layer.read_bytes() == published
    assert export.export_layer(lost, layer, force=True)["products"] == 0
    with pytest.raises(FileNotFoundError):
        export.export_layer(tmp_path / "absent.sqlite", layer)

    # A live database that holds much less than the layer does not shrink it either.
    full = tmp_path / "full.sqlite"
    kratheos.create_database(full)
    database = sqlite3.connect(full)
    database.executemany(
        "INSERT INTO translations VALUES (?, 'de', 'Name', 'human', '2026-10-11')",
        [(f"ciqual:{number}",) for number in range(12)],
    )
    database.commit()
    database.close()
    with pytest.raises(export.ExportRefused, match="12 translations and the export would leave 1"):
        export.export_layer(service.settings.live, full)
    assert len(kratheos.read_layer(full).translations) == 12
    # Once the layer is taken over, the export holds both and goes through.
    assert service.store.import_layer(kratheos.read_layer(full))["translations"] == 12
    assert export.export_layer(service.settings.live, full)["translations"] == 13


def test_whole_loop_export_rebuild_swap(service: Running) -> None:
    """After a rebuild the same identifiers answer the same, nothing twice, nothing lost."""
    ids = write_through_the_api(service)
    before = answers(service, ids)
    assert before[f"/v1/foods/{ids['spam']}"][0] == 404
    assert before[f"/v1/foods/{NUTELLA}?lang=fr"][1]["version_id"] == ids["version"]
    assert before[f"/v1/foods/{NUTELLA}?lang=en&country=US"][1]["source"]["id"] == "usda"
    served_before = service.catalogue.current()

    report = maintenance.rebuild(
        service.workspace,
        BuildOptions(from_mirror=True),
        service.settings.database,
        service.settings.live,
        log=quiet,
    )
    assert report["swapped"] and report["export"]["products"] == 2
    assert report["kratheos"]["products"] == 2 and report["kratheos"]["versions_applied"] == 1
    assert report["kratheos"]["votes_applied"] == 5
    assert report["kratheos"]["foods_served_from_a_user_version"] == 1
    comparison = report["comparison"]
    assert comparison["reasons"] == []
    assert (comparison["foods"]["served"], comparison["foods"]["new"]) == (31, 33)
    assert comparison["identifiers"]["removed"] == 0
    assert sorted(comparison["identifiers"]["first_added"]) == sorted([ids["burger"], ids["bar"]])
    assert not (service.workspace.work / "rebuild").exists()

    # The running service picked the new file up, without being restarted.
    served_after = service.catalogue.current()
    assert served_after is not served_before and served_after.foods == 33
    assert service.get("/v1/meta")[1]["built_at"] == served_after.manifest["built_at"]
    after = answers(service, ids)
    assert after == before

    # The main database now holds what the live database held: folded, not doubled.
    database = sqlite3.connect(service.settings.database)
    folded = database.execute(
        "SELECT record_id, source, energy_kcal, votes, versions_count FROM foods WHERE id = ?",
        (NUTELLA,),
    ).fetchone()
    assert folded == (ids["version"], "kratheos", 530.0, 3, 3)
    assert database.execute(
        "SELECT source, name, votes FROM foods WHERE id = ?", (ids["burger"],)
    ).fetchone() == ("kratheos", "Burger maison du test", 1)
    assert database.execute("SELECT votes FROM foods WHERE id = 'swiss:273'").fetchone() == (1,)
    assert database.execute(
        "SELECT count(*) FROM foods WHERE id = ?", (ids["spam"],)
    ).fetchone() == (0,)
    database.close()
    assert service.food(NUTELLA)["votes"] == 3 and service.food(NUTELLA)["versions_count"] == 3
    assert len(service.found("q=burger+maison")) == 1

    # The service goes on after the swap: a vote moves, a second rebuild changes nothing else.
    service.post(
        f"/v1/foods/{NUTELLA}/votes", {"version_id": "off:3017620422003", "contributor": BOB}
    )
    assert {v["version_id"]: v["votes"] for v in service.versions(NUTELLA)} == {
        ids["version"]: 2,
        "off:3017620422003": 2,
        "usda:branded:3017620422003": 0,
    }
    moved = answers(service, ids)
    again = maintenance.rebuild(
        service.workspace,
        BuildOptions(from_mirror=True),
        service.settings.database,
        service.settings.live,
        log=quiet,
    )
    assert again["comparison"]["identifiers"] == {
        "served": 33,
        "new": 33,
        "added": 0,
        "removed": 0,
        "first_added": [],
        "first_removed": [],
    }
    assert answers(service, ids) == moved

    # A live database lost and created again takes the published layer over.
    lost = start_service(service.workspace, service.settings.database.parent.parent / "again")
    assert lost.food(NUTELLA)["versions_count"] == 3
    assert {v["version_id"]: v["votes"] for v in lost.versions(NUTELLA)} == {
        ids["version"]: 2,
        "off:3017620422003": 2,
        "usda:branded:3017620422003": 0,
    }
    assert lost.get("/v1/meta")[1]["counts"]["added_since_the_build"] == {
        "products": 2,
        "versions": 1,
        "translations": 1,
        "votes": 7,
    }
    lost.close()


def test_rebuild_that_loses_too_much_is_refused(service: Running) -> None:
    """A suspicious rebuild never replaces the database being served."""
    database = service.settings.database
    served = database.read_bytes()
    with pytest.raises(maintenance.Refused) as refusal:
        maintenance.rebuild(
            service.workspace,
            BuildOptions(from_mirror=True, only=["ciqual", "swiss"]),
            database,
            service.settings.live,
            log=quiet,
        )
    reasons = " | ".join(refusal.value.report["reasons"])
    assert "25 foods fewer than the 31 being served" in reasons
    assert "25 of the 31 identifiers being served are gone" in reasons
    assert "source off goes from 9 foods to 0" not in reasons  # below the size that counts
    assert database.read_bytes() == served
    assert service.food(NUTELLA)["id"] == NUTELLA
    kept = service.workspace.work / "rebuild"
    assert (kept / "dist" / "food.sqlite").exists()  # left there to be looked at
    report = json.loads((kept / "rebuild.json").read_text(encoding="utf-8"))
    assert report["swapped"] is False and report["comparison"]["identifiers"]["removed"] == 25

    # The same from the command line: a distinct exit status, the old file stays.
    root = str(service.workspace.sources_file.parent)
    arguments = ["--root", root, "rebuild", "--from-mirror", "--database", str(database)]
    arguments += ["--live", str(service.settings.live)]
    assert cli.main([*arguments, "--only", "ciqual"]) == cli.EXIT_REFUSED
    assert database.read_bytes() == served
    assert cli.main(arguments) == 0
    assert service.food(NUTELLA)["id"] == NUTELLA


def test_a_source_that_vanishes_is_noticed_even_when_it_is_small(
    service: Running, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A source losing a fifth of its foods refuses the swap, whatever the total."""
    small = tmp_path / "small"
    workspace = service.workspace
    run_build(
        Workspace(
            workspace.sources_file,
            workspace.sources_dir,
            workspace.mirror,
            small / "work",
            small / "dist",
            small / "no-layer.sqlite",
        ),
        BuildOptions(from_mirror=True, only=["off", "usda", "swiss", "cnf", "cofid", "chain"]),
        log=quiet,
    )
    arguments = (
        service.settings.database,
        small / "dist" / "food.sqlite",
        small / "dist" / "identifiers.txt.gz",
        tmp_path,
    )
    tolerant = maintenance.compare(*arguments, max_loss=0.5)
    assert tolerant["reasons"] == []  # 3 foods of 31, and the sources here are tiny
    assert tolerant["by_source"]["ciqual"] == {"served": 3, "new": 0}
    monkeypatch.setattr(maintenance, "MIN_SOURCE_FOODS", 3)
    assert maintenance.compare(*arguments, max_loss=0.5)["reasons"] == [
        "source ciqual goes from 3 foods to 0: more than 20% fewer"
    ]


def test_identifier_list_is_written_for_a_database_that_has_none(
    service: Running, tmp_path: Path
) -> None:
    """The comparison works from the database alone."""
    out = tmp_path / "identifiers.txt.gz"
    assert maintenance.write_identifier_list(service.settings.database, out) == 31
    listed = service.settings.database.with_name("identifiers.txt.gz")
    assert out.read_bytes() == listed.read_bytes()
    assert sqlite_export.read_identifiers(out) == sqlite_export.read_identifiers(listed)


def test_ready_made_database_is_published_and_installed(
    service: Running, file_server: FileServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Someone hosting the service installs the database without the originals."""
    ids = write_through_the_api(service)
    maintenance.rebuild(
        service.workspace,
        BuildOptions(from_mirror=True),
        service.settings.database,
        service.settings.live,
        log=quiet,
    )
    published = file_server.root / "v1"
    monkeypatch.setattr(mirror, "MAX_ASSET_BYTES", 70_000)  # force the database into parts
    files = maintenance.database_files(
        service.settings.database, service.workspace.kratheos, published, log=quiet
    )
    names = [entry["asset"] for entry in files]
    assert names[0] == "DATABASE.json" and "food.sqlite.part-01-of-02" in names
    assert {"manifest.json", "identifiers.txt.gz", "kratheos.sqlite"} <= set(names)
    assert not (published / "food.sqlite").exists()
    index = json.loads((published / "DATABASE.json").read_text(encoding="utf-8"))
    assert index["foods"] == 33 and index["layout_version"] == 2 and len(index["parts"]) == 2

    # A new host: no database, no live database, no original.
    host = tmp_path / "host"
    database, live_path = host / "food.sqlite", host / "live.sqlite"
    report = maintenance.install_database(
        f"{file_server.url}/v1", database, live_path, b"secret-of-the-new-host", log=quiet
    )
    assert report["installed"] and report["foods"] == 33
    assert report["layer"] == {"products": 2, "versions": 1, "translations": 1, "votes": 7}
    assert database.read_bytes() == service.settings.database.read_bytes()
    assert (host / "manifest.json").exists() and (host / "identifiers.txt.gz").exists()
    assert not (host / "incoming").exists()
    again = maintenance.install_database(f"{file_server.url}/v1", database, log=quiet)
    assert again["installed"] is False

    # The new host serves the same answers, votes included, and exports the same layer.
    shutil.copy(host / "kratheos.sqlite", tmp_path / "published-layer.sqlite")
    exported = tmp_path / "exported-by-the-host.sqlite"
    summary = export.export_layer(live_path, exported)
    assert (summary["products"], summary["versions"], summary["votes"]) == (2, 1, 7)
    assert (
        kratheos.read_layer(exported).votes
        == kratheos.read_layer(tmp_path / "published-layer.sqlite").votes
    )
    assert ids["version"] in [v["version_id"] for v in kratheos.read_layer(exported).versions]

    # A file that is not the one announced is refused, and nothing is replaced.
    part = published / "food.sqlite.part-02-of-02"
    part.write_bytes(part.read_bytes()[:-1] + b"X")
    other = tmp_path / "other" / "food.sqlite"
    with pytest.raises(DownloadError):
        maintenance.install_database(f"{file_server.url}/v1", other, log=quiet)
    assert not other.exists()
    with pytest.raises(DownloadError):
        maintenance.install_database(f"{file_server.url}/nowhere", other, log=quiet)

    # A database that loses too much is refused too, unless forced.
    small = tmp_path / "small"
    workspace = service.workspace
    run_build(
        Workspace(
            workspace.sources_file,
            workspace.sources_dir,
            workspace.mirror,
            small / "work",
            small / "dist",
            small / "no-layer.sqlite",
        ),
        BuildOptions(from_mirror=True, only=["ciqual"]),
        log=quiet,
    )
    monkeypatch.setattr(mirror, "MAX_ASSET_BYTES", 1_900_000_000)
    maintenance.database_files(small / "dist" / "food.sqlite", None, file_server.root / "small")
    with pytest.raises(maintenance.Refused, match="foods fewer"):
        maintenance.install_database(f"{file_server.url}/small", database, log=quiet)
    assert database.read_bytes() == service.settings.database.read_bytes()
    forced = maintenance.install_database(
        f"{file_server.url}/small", database, force=True, log=quiet
    )
    assert forced["foods"] == 3


def git(repository: Path, *arguments: str) -> str:
    """Run git in a throwaway repository and return its output."""
    process = subprocess.run(  # noqa: S603
        ["git", "-C", str(repository), *arguments],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    )
    return process.stdout.strip()


def test_export_is_committed_alone_with_a_dated_message(
    service: Running, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The commit holds the layer and nothing else; without a change, no commit."""
    clone = tmp_path / "clone"
    clone.mkdir()
    git(clone, "init", "--quiet", "--initial-branch", "main")
    git(clone, "config", "user.name", "A Test")
    git(clone, "config", "user.email", "test@example.org")
    kratheos.create_database(clone / "kratheos.sqlite")
    (clone / "notes.txt").write_text("first\n", encoding="utf-8")
    git(clone, "add", "kratheos.sqlite", "notes.txt")
    git(clone, "commit", "--quiet", "-m", "first")
    # Something else changes, and is even staged: it must stay out of the commit.
    (clone / "notes.txt").write_text("changed\n", encoding="utf-8")
    git(clone, "add", "notes.txt")

    write_through_the_api(service)
    root = str(clone)
    arguments = ["--root", root, "export-layer", "--live", str(service.settings.live)]
    assert cli.main([*arguments, "--commit"]) == 0
    assert git(clone, "rev-list", "--count", "HEAD") == "2"
    assert git(clone, "show", "--name-only", "--format=", "HEAD") == "kratheos.sqlite"
    subject, _, body = git(clone, "log", "-1", "--format=%s%n%b").partition("\n")
    assert subject.startswith("data: the Kratheos layer exported on 20")
    assert "2 products, 1 versions, 7 votes on 5 versions, 1 names" in body
    assert git(clone, "status", "--porcelain") == "M  notes.txt"
    assert kratheos.read_layer(clone / "kratheos.sqlite").rows == 9

    # Nothing new: nothing committed. No remote: nothing pushed, and it is said.
    logged: list[str] = []
    summary = export.export_layer(service.settings.live, clone / "kratheos.sqlite")
    assert export.commit_layer(
        clone / "kratheos.sqlite", summary, push=True, log=logged.append
    ) == ("unchanged")
    assert git(clone, "rev-list", "--count", "HEAD") == "2"
    service.post("/v1/products", dict(PRODUCT, names={"fr": "Un autre produit"}))
    summary = export.export_layer(service.settings.live, clone / "kratheos.sqlite")
    state = export.commit_layer(clone / "kratheos.sqlite", summary, push=True, log=logged.append)
    assert state == "committed, no remote to push to"
    assert git(clone, "rev-list", "--count", "HEAD") == "3"
    assert "the repository has no remote: nothing pushed" in logged

    # With a remote the push is asked of git, once, after the commit. It is
    # not run here: the call is only observed.
    git(clone, "remote", "add", "origin", str(tmp_path / "nowhere.git"))
    service.post("/v1/products", dict(PRODUCT, names={"fr": "Encore un produit"}))
    summary = export.export_layer(service.settings.live, clone / "kratheos.sqlite")
    asked: list[tuple[str, ...]] = []
    real = export._git

    def observed(repository: Path, *arguments: str, check: bool = True) -> object:
        """Record the git commands, and let all of them run except the push."""
        asked.append(arguments)
        if arguments[0] == "push":
            return subprocess.CompletedProcess(arguments, 0, "", "")
        return real(repository, *arguments, check=check)

    monkeypatch.setattr(export, "_git", observed)
    state = export.commit_layer(clone / "kratheos.sqlite", summary, push=True, log=logged.append)
    assert state == "committed and pushed"
    assert [arguments for arguments in asked if arguments[0] == "push"] == [("push", "--quiet")]
    assert asked[-1] == ("push", "--quiet")

    # Outside a repository the commit fails with a clear message.
    alone = tmp_path / "alone"
    assert (
        cli.main(
            [
                "--root",
                str(alone),
                "export-layer",
                "--live",
                str(service.settings.live),
                "--out",
                str(alone / "kratheos.sqlite"),
                "--commit",
            ]
        )
        == 1
    )


def test_import_layer_only_adds_what_is_missing(service: Running, tmp_path: Path) -> None:
    """Taking a layer over twice, or over live data, changes nothing that is there."""
    ids = write_through_the_api(service)
    layer_file = _exported(service, tmp_path / "kratheos.sqlite")
    layer = kratheos.read_layer(layer_file)
    assert service.store.import_layer(layer) == {
        "products": 0,
        "versions": 0,
        "translations": 0,
        "votes": 0,
    }
    root = str(service.workspace.sources_file.parent)
    fresh = tmp_path / "fresh" / "live.sqlite"
    arguments = ["--root", root, "import-layer", str(layer_file), "--live", str(fresh)]
    assert cli.main(arguments) == 0 and cli.main(arguments) == 0
    assert (
        kratheos.read_layer(_exported_from(fresh, tmp_path / "again.sqlite")).votes == layer.votes
    )
    assert ids["version"] in [v["version_id"] for v in layer.versions]


def _exported_from(live_path: Path, path: Path) -> Path:
    """Export a live database to a file and return its path."""
    export.export_layer(live_path, path)
    return path
