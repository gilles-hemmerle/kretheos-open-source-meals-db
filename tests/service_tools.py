"""Helpers of the service tests: a running service over a copy of the miniature build.

`start_service` copies the repository built once for the session (stored
originals, layer and `dist/`), puts the main database in a data directory of
its own and binds the endpoints to it, with a live database created for the
test. Requests are made by calling `Api.handle` directly; the tests of the
HTTP server open real sockets themselves.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from conftest import quiet

from kratheos_food_data.build import FOOD_DATABASE_NAME, IDENTIFIERS_NAME, Workspace
from kratheos_food_data.service.api import Api
from kratheos_food_data.service.catalogue import Catalogue
from kratheos_food_data.service.httpd import open_service
from kratheos_food_data.service.live import LiveStore
from kratheos_food_data.service.settings import MANIFEST_NAME, Settings, load_settings

TOKEN = "token-of-the-tests-0123456789"
KEY_SECRET = "secret-of-the-tests-0123456789"
# Contributor keys as the backend sends them: opaque, one per person.
ALICE, BOB, CAROL, DAVE = (f"contributor-key-{name}" for name in ("alice", "bob", "carol", "dave"))


@dataclass
class Running:
    """A service bound to its own copy of the miniature repository.

    Attributes:
        api: The endpoints.
        settings: The settings of the service.
        workspace: The copy of the repository, which a rebuild may change.
        catalogue: The main database.
        store: The live database.
    """

    api: Api
    settings: Settings
    workspace: Workspace
    catalogue: Catalogue
    store: LiveStore

    def get(self, target: str, token: str | None = None) -> tuple[int, dict]:
        """Make a GET request.

        Args:
            target: Path and query.
            token: Service token to send, if any.

        Returns:
            The status and the JSON object of the answer.
        """
        headers = {"authorization": f"Bearer {token}"} if token else {}
        response = self.api.handle("GET", target, headers, b"")
        return response.status, response.payload

    def post(self, target: str, body: object = None, token: str | None = TOKEN) -> tuple[int, dict]:
        """Make a POST request with a JSON body.

        Args:
            target: Path and query.
            body: The object to send; nothing when None.
            token: Service token to send; None to send none.

        Returns:
            The status and the JSON object of the answer.
        """
        headers = {"authorization": f"Bearer {token}"} if token else {}
        raw = b"" if body is None else json.dumps(body).encode("utf-8")
        response = self.api.handle("POST", target, headers, raw)
        return response.status, response.payload

    def food(self, food_id: str, query: str = "") -> dict:
        """Read a food that must exist.

        Args:
            food_id: Identifier of the food.
            query: Parameters of the request, without the question mark.

        Returns:
            The food record.
        """
        status, payload = self.get(f"/v1/foods/{food_id}" + (f"?{query}" if query else ""))
        assert status == 200, payload
        return payload

    def versions(self, food_id: str, query: str = "") -> list[dict]:
        """List the versions of a food that must exist.

        Args:
            food_id: Identifier of the food.
            query: Parameters of the request, without the question mark.

        Returns:
            The version objects, the one served first.
        """
        status, payload = self.get(f"/v1/foods/{food_id}/versions" + (f"?{query}" if query else ""))
        assert status == 200, payload
        return payload["versions"]

    def found(self, query: str) -> list[str]:
        """Search and return the identifiers found, in order.

        Args:
            query: Parameters of the search, without the question mark.

        Returns:
            The identifiers of the first page.
        """
        status, payload = self.get(f"/v1/foods/search?{query}")
        assert status == 200, payload
        return [item["id"] for item in payload["items"]]

    def close(self) -> None:
        """Close the databases."""
        self.api.close()
        self.catalogue.close()
        self.store.close()


def copy_repository(built: Workspace, root: Path) -> Workspace:
    """Copy the built miniature repository, without its work files.

    Args:
        built: The repository built once for the session.
        root: Where to put the copy.

    Returns:
        The workspace of the copy.
    """
    source = built.sources_file.parent
    shutil.copytree(source, root, ignore=shutil.ignore_patterns("work"))
    return Workspace.at(root)


def start_service(
    built: Workspace, base: Path, token: str = TOKEN, user_version_votes: int | None = None
) -> Running:
    """Start a service over a copy of the miniature repository.

    Args:
        built: The repository built once for the session.
        base: Directory of the test.
        token: Service token; empty to disable writes.
        user_version_votes: Vote threshold, when not the default.

    Returns:
        The running service.
    """
    workspace = copy_repository(built, base / "repository")
    data = base / "data"
    data.mkdir()
    for name in (FOOD_DATABASE_NAME, MANIFEST_NAME, IDENTIFIERS_NAME):
        shutil.copy(workspace.dist / name, data / name)
    settings = load_settings(
        workspace.sources_file.parent,
        database=data / FOOD_DATABASE_NAME,
        live=data / "live.sqlite",
        layer=workspace.kratheos,
        user_version_votes=user_version_votes,
        environment={"FOOD_DATA_TOKEN": token, "FOOD_DATA_KEY_SECRET": KEY_SECRET},
    )
    api, catalogue, store = open_service(settings, log=quiet)
    return Running(api, settings, workspace, catalogue, store)


PRODUCT = {
    "barcode": None,
    "names": {"fr": "Burger maison du test", "en": "Home-made test burger"},
    "brand": "Chez Test",
    "category": "Burgers",
    "countries": ["ch", "FR"],
    "basis": "100g",
    "is_drink": False,
    "nutrients": {
        "kcal": 250,
        "protein_g": 13,
        "carbs_g": 25,
        "sugars_g": 4,
        "fat_g": 11,
        "saturated_fat_g": 4,
        "fibre_g": 2,
        "salt_g": 1.2,
    },
    "servings": [{"label": "1 burger", "amount": 180, "unit": "g"}],
    "contributor": ALICE,
}
