"""The endpoints of the food service, independent of how HTTP reaches them.

`Api.handle` receives a method, a target (path and query), the headers and
the body, and returns a status and a JSON object. The HTTP server
(`httpd.py`) is a thin shell around it, and the tests call it directly.

Reads need no authentication. Writes and moderation need the shared token
(`Authorization: Bearer <token>`), compared in constant time; without a
configured token they are refused altogether. Every error is
`{"code": ..., "message": ...}` with the status of the contract.

Writes are applied one at a time: the check that a barcode, a version or a
name is not there yet and the write that follows cannot be separated by
another write.
"""

from __future__ import annotations

import contextlib
import hmac
import queue
import sqlite3
import threading
import uuid
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from urllib.parse import parse_qs, unquote, urlsplit

from .. import __version__, kratheos, manifest
from . import catalogue, foods, live, search, validation
from .catalogue import Catalogue, DatabaseUnavailable
from .foods import Context
from .live import LiveStore
from .settings import Settings
from .validation import ApiError, invalid

API_VERSION = 2
MAX_USER_VERSIONS = 20
READ_PARAMETERS = ("lang", "country")
SEARCH_PARAMETERS = ("q", "lang", "country", "kind", "drink", "limit", "offset")
PRODUCT_FIELDS = (
    "barcode",
    "names",
    "brand",
    "category",
    "countries",
    "basis",
    "is_drink",
    "nutrients",
    "servings",
    "contributor",
)
VERSION_FIELDS = ("basis", "nutrients", "servings", "countries", "contributor")
VOTE_FIELDS = ("version_id", "contributor")
TRANSLATION_FIELDS = ("language", "name", "contributor")
REPORT_FIELDS = ("reason", "note", "contributor")
REPORT_STATES = (live.REPORT_OPEN, live.REPORT_CLOSED, "all")
ERROR_CODES = {
    "invalid_json": 400,
    "unauthorized": 401,
    "not_found": 404,
    "method_not_allowed": 405,
    "barcode_exists": 409,
    "version_exists": 409,
    "name_exists": 409,
    "too_many_versions": 409,
    "payload_too_large": 413,
    "invalid_request": 422,
    "invalid_barcode": 422,
    "implausible_values": 422,
    "internal_error": 500,
    "database_unavailable": 503,
    "writes_disabled": 503,
}


@dataclass
class Response:
    """The answer to a request.

    Attributes:
        status: HTTP status.
        payload: The JSON object of the answer.
        headers: Extra HTTP headers.
    """

    status: int
    payload: dict
    headers: dict[str, str] = field(default_factory=dict)


def error_response(error: ApiError) -> Response:
    """Write an error as the contract gives errors.

    Args:
        error: The error.

    Returns:
        The answer, with the stable code and the sentence.
    """
    headers = {"WWW-Authenticate": "Bearer"} if error.status == 401 else {}
    return Response(
        error.status, {"code": error.code, "message": error.message, **error.extra}, headers
    )


def not_found(what: str = "food") -> ApiError:
    """Build the error of something that does not exist.

    Args:
        what: What was asked for.

    Returns:
        The error, to be raised.
    """
    return ApiError(404, "not_found", f"no such {what}")


class Api:
    """The endpoints, over a main database and a live database.

    Attributes:
        settings: The settings of the service.
        catalogue: The main database.
        store: The live database.
    """

    def __init__(self, settings: Settings, catalogue_: Catalogue, store: LiveStore) -> None:
        """Bind the endpoints to their databases.

        Args:
            settings: The settings of the service.
            catalogue_: The main database.
            store: The live database.
        """
        self.settings = settings
        self.catalogue = catalogue_
        self.store = store
        self._live_readers: queue.LifoQueue = queue.LifoQueue()
        self._writing = threading.Lock()
        # Measured on the real database: inside one process, the full-text
        # index answers fewer searches per second with eight threads than
        # with two. So a few searches are ranked at a time and the others
        # wait their turn; lookups by barcode or identifier never wait.
        self._searching = threading.BoundedSemaphore(max(1, settings.searches))

    def close(self) -> None:
        """Close the connections the endpoints kept open."""
        while True:
            try:
                self._live_readers.get_nowait().close()
            except queue.Empty:
                return

    @contextlib.contextmanager
    def _context(self) -> Iterator[Context]:
        """Lend both databases for the time of a request.

        Yields:
            The context of the request.

        Raises:
            DatabaseUnavailable: When no main database can be served.
        """
        with self.catalogue.reader() as (generation, base):
            try:
                reader: sqlite3.Connection = self._live_readers.get_nowait()
            except queue.Empty:
                reader = self.store.reader()
            try:
                yield Context(generation, base, reader, self.settings.user_version_votes)
            finally:
                self._live_readers.put(reader)

    def handle(self, method: str, target: str, headers: Mapping[str, str], body: bytes) -> Response:
        """Answer one request.

        Args:
            method: HTTP method.
            target: Path and query of the request.
            headers: Headers of the request, names in lower case.
            body: Body of the request.

        Returns:
            The answer; never raises.
        """
        try:
            return self._route(method, target, headers, body)
        except ApiError as error:
            return error_response(error)
        except DatabaseUnavailable:
            return error_response(
                ApiError(503, "database_unavailable", "the food database is not installed yet")
            )

    def _route(self, method: str, target: str, headers: Mapping[str, str], body: bytes) -> Response:
        """Find the endpoint of a request and run it.

        Args:
            method: HTTP method.
            target: Path and query of the request.
            headers: Headers of the request, names in lower case.
            body: Body of the request.

        Returns:
            The answer of the endpoint.

        Raises:
            ApiError: When the request cannot be answered as asked.
        """
        parts = urlsplit(target)
        segments = [unquote(segment) for segment in parts.path.strip("/").split("/")]
        if segments[0] != "v1":
            raise not_found("endpoint")
        try:
            query = {
                name: values[-1]
                for name, values in parse_qs(
                    parts.query, keep_blank_values=True, max_num_fields=20
                ).items()
            }
        except ValueError as error:
            raise invalid("too many parameters") from error
        route = self._match(segments[1:], method)
        if route is None:
            raise not_found("endpoint")
        allowed, handler, arguments = route
        if method not in allowed.split(" or "):
            raise ApiError(405, "method_not_allowed", f"this endpoint answers {allowed}")
        return handler(*arguments, query, headers, body)

    def _match(self, segments: list[str], method: str) -> tuple[str, Callable, tuple] | None:
        """Match the path of a request to an endpoint.

        Args:
            segments: The parts of the path after `/v1`.
            method: HTTP method, for the one path that answers two.

        Returns:
            The method the endpoint answers, its handler and the identifiers
            taken from the path; None when no endpoint has this path.
        """
        match segments:
            case ["health"]:
                return "GET", self._health, ()
            case ["meta"]:
                return "GET", self._meta, ()
            case ["foods", "search"]:
                return "GET", self._search, ()
            case ["foods", food_id]:
                return "GET", self._food, (food_id,)
            case ["foods", food_id, "versions"]:
                handler = self._propose_version if method == "POST" else self._list_versions
                return "GET or POST", handler, (food_id,)
            case ["foods", food_id, "votes"]:
                return "POST", self._vote, (food_id,)
            case ["foods", food_id, "translations"]:
                return "POST", self._translate, (food_id,)
            case ["foods", food_id, "reports"]:
                return "POST", self._report, (food_id,)
            case ["barcodes", code]:
                return "GET", self._barcode, (code,)
            case ["products"]:
                return "POST", self._create_product, ()
            case ["moderation", "reports"]:
                return "GET", self._reports, ()
            case ["moderation", "reports", report_id, "close"]:
                return "POST", self._close_report, (report_id,)
            case ["moderation", "foods", record_id, "hide"]:
                return "POST", self._hide, (record_id, True)
            case ["moderation", "foods", record_id, "restore"]:
                return "POST", self._hide, (record_id, False)
        return None

    def _authorize(self, headers: Mapping[str, str]) -> None:
        """Refuse a write or a moderation request without the shared token.

        Args:
            headers: Headers of the request, names in lower case.

        Raises:
            ApiError: `writes_disabled` when the service has no token,
                `unauthorized` when the request does not carry it.
        """
        if not self.settings.token:
            raise ApiError(503, "writes_disabled", "the service has no token: writes are disabled")
        scheme, _, given = headers.get("authorization", "").partition(" ")
        expected = self.settings.token.encode("utf-8")
        if scheme.lower() != "bearer" or not hmac.compare_digest(
            given.strip().encode("utf-8"), expected
        ):
            raise ApiError(401, "unauthorized", "the service token is missing or wrong")

    @staticmethod
    def _parameters(query: Mapping[str, str], allowed: tuple[str, ...]) -> None:
        """Refuse a parameter the endpoint does not know.

        Args:
            query: The parameters of the request.
            allowed: The parameters the endpoint knows.

        Raises:
            ApiError: When the request has another parameter.
        """
        unknown = sorted(set(query) - set(allowed))
        if unknown:
            raise invalid(f"unknown parameter: {unknown[0]}")

    def _locale(self, query: Mapping[str, str]) -> tuple[str | None, str | None]:
        """Read the language and the country of a request.

        Args:
            query: The parameters of the request.

        Returns:
            The language and the country, None when not given.
        """
        return validation.language(query.get("lang")), validation.country(query.get("country"))

    # --- reads ---------------------------------------------------------

    def _health(self, query: Mapping, headers: Mapping, body: bytes) -> Response:
        """Say whether the service can answer."""
        with self._context() as context:
            generation = context.generation
            return Response(
                200,
                {"status": "ok", "foods": generation.foods, "built_at": generation.built_at},
            )

    def _meta(self, query: Mapping, headers: Mapping, body: bytes) -> Response:
        """Describe the database served: build date, sources, licences, counts."""
        self._parameters(query, ())
        with self._context() as context:
            generation = context.generation
            statistics = ((generation.manifest or {}).get("statistics") or {}).get("foods") or {}
            sources = [
                {
                    "id": source["id"],
                    "title": source["name"],
                    "publisher": source["publisher"],
                    "homepage": source["homepage"] or None,
                    "version": source["version"] or None,
                    "released": source["released"] or None,
                    "licence": source["licence"],
                    "licence_name": source["licence_name"],
                    "licence_url": source["licence_url"] or None,
                    "attribution": source["attribution"],
                    "foods": source["foods"],
                }
                for source in generation.sources.values()
            ]
            counts = {
                "foods": generation.foods,
                "generic_foods": statistics.get("generic_foods"),
                "products_with_barcode": statistics.get("products_with_barcode"),
                "products_without_barcode": statistics.get("products_without_valid_barcode"),
                "names": statistics.get("names"),
                "foods_with_several_versions": (
                    ((generation.manifest or {}).get("kratheos") or {}).get("statistics") or {}
                ).get("foods_with_several_versions"),
                "by_source": {source["id"]: source["foods"] for source in sources},
                "added_since_the_build": live.counts(context.live),
            }
            return Response(
                200,
                {
                    "service": {
                        "name": "kratheos-food-data",
                        "version": __version__,
                        "contract": API_VERSION,
                        "user_version_votes": self.settings.user_version_votes,
                    },
                    "built_at": generation.built_at,
                    "served_since": generation.loaded_at,
                    "layout_version": int(generation.meta["layout_version"]),
                    "licence": {
                        "id": generation.meta.get("licence"),
                        "name": generation.meta.get("licence_name"),
                        "url": generation.meta.get("licence_url"),
                        "notice": generation.meta.get("licence_notice"),
                        "contents": manifest.DATABASE_LICENCE["contents"],
                    },
                    "sources": sources,
                    "counts": counts,
                },
            )

    def _search(self, query: Mapping, headers: Mapping, body: bytes) -> Response:
        """Search foods by name, brand or barcode."""
        self._parameters(query, SEARCH_PARAMETERS)
        language, country = self._locale(query)
        kind = validation.choice(query.get("kind"), "kind", validation.KINDS)
        drink = validation.flag(query.get("drink"), "drink")
        limit = validation.integer(
            query.get("limit"), "limit", validation.DEFAULT_LIMIT, 1, validation.MAX_LIMIT
        )
        offset = validation.integer(query.get("offset"), "offset", 0, 0, validation.MAX_OFFSET)
        text = query.get("q", "")
        if len(text) > search.MAX_QUERY_CHARS:
            raise invalid(f"q must have at most {search.MAX_QUERY_CHARS} characters")
        with self._searching, self._context() as context:
            found = search.search(
                context,
                text,
                language=language,
                country=country,
                kind=kind,
                drink=drink,
                budget=self.settings.search_budget_seconds,
            )
            page = found[offset : offset + limit]
            return Response(
                200,
                {
                    "items": [foods.render_food(context, food, language, country) for food in page],
                    "next_offset": offset + limit if len(found) > offset + limit else None,
                },
            )

    def _food(self, food_id: str, query: Mapping, headers: Mapping, body: bytes) -> Response:
        """Read a food by its identifier."""
        self._parameters(query, READ_PARAMETERS)
        language, country = self._locale(query)
        with self._context() as context:
            food = foods.find(context, validation.food_id(food_id))
            if food is None:
                raise not_found()
            return Response(200, foods.render_food(context, food, language, country))

    def _barcode(self, code: str, query: Mapping, headers: Mapping, body: bytes) -> Response:
        """Read the food carrying a barcode."""
        self._parameters(query, READ_PARAMETERS)
        language, country = self._locale(query)
        canonical = validation.barcode(code)
        with self._context() as context:
            food = foods.find_by_barcode(context, canonical)
            if food is None:
                raise not_found()
            return Response(200, foods.render_food(context, food, language, country))

    def _list_versions(
        self, food_id: str, query: Mapping, headers: Mapping, body: bytes
    ) -> Response:
        """List the versions of a food, the one served first."""
        self._parameters(query, ("country",))
        country = validation.country(query.get("country"))
        with self._context() as context:
            food = foods.find(context, validation.food_id(food_id))
            if food is None:
                raise not_found()
            return Response(200, {"versions": foods.render_versions(context, food, country)})

    # --- writes --------------------------------------------------------

    def _create_product(self, query: Mapping, headers: Mapping, body: bytes) -> Response:
        """Add a product nobody knows yet."""
        self._authorize(headers)
        self._parameters(query, READ_PARAMETERS)
        language, country = self._locale(query)
        data = validation.parse_body(body, PRODUCT_FIELDS)
        key = validation.contributor(data)
        barcode = validation.barcode(data.get("barcode"), required=False)
        names = validation.names(data.get("names"))
        basis = validation.basis(data.get("basis"))
        is_drink = data.get("is_drink", False)
        if not isinstance(is_drink, bool):
            raise invalid("is_drink must be true or false")
        values = validation.nutrients(data.get("nutrients"))
        values.setdefault(validation.OPTIONAL_NUTRIENT, None)
        validation.plausible(values)
        main_language = next(iter(names))
        fields = {
            "kind": "product",
            "barcode": barcode,
            "name": names[main_language],
            "name_lang": main_language,
            "names": names,
            "brand": validation.text(data.get("brand"), "brand", validation.MAX_LABEL),
            "category": validation.text(data.get("category"), "category", validation.MAX_LABEL),
            "basis": basis,
            **values,
            **validation.serving(data.get("servings"), basis),
            "is_drink": int(is_drink),
            "countries": validation.countries(data.get("countries")),
        }
        with self._writing, self._context() as context:
            if barcode:
                owner = foods.barcode_owner(context, barcode)
                if owner is not None:
                    raise ApiError(
                        409, "barcode_exists", "a food already has this barcode", id=owner
                    )
            try:
                food_id = self.store.create_product(str(uuid.uuid4()), fields, key)
            except live.Conflict as error:
                raise ApiError(
                    409,
                    "barcode_exists",
                    "a food already has this barcode",
                    id=live.GTIN_PREFIX + barcode,
                ) from error
            food = foods.find(context, food_id)
            return Response(201, foods.render_food(context, food, language, country))

    def _propose_version(
        self, food_id: str, query: Mapping, headers: Mapping, body: bytes
    ) -> Response:
        """Propose other values for a food: a new version, with its author's vote."""
        self._authorize(headers)
        self._parameters(query, ("country",))
        country = validation.country(query.get("country"))
        data = validation.parse_body(body, VERSION_FIELDS)
        key = validation.contributor(data)
        basis = validation.basis(data.get("basis"))
        values = validation.nutrients(data.get("nutrients"))
        portion = validation.serving(data.get("servings"), basis)
        countries = validation.countries(data.get("countries"))
        with self._writing, self._context() as context:
            food = foods.find(context, validation.food_id(food_id))
            if food is None:
                raise not_found()
            if validation.OPTIONAL_NUTRIENT not in values:
                # Alcohol was left out: the food keeps the one it is known with.
                known = [v["alcohol_g"] for v in food.versions if v["alcohol_g"] is not None]
                values[validation.OPTIONAL_NUTRIENT] = known[0] if known else None
            validation.plausible(values)
            for version in food.versions:
                if version["basis"] == basis and all(
                    version[column] == values[column] for _, column in foods.NUTRIENT_FIELDS
                ):
                    raise ApiError(
                        409,
                        "version_exists",
                        "a version of this food already has exactly these values",
                        version_id=version["version_id"],
                    )
            proposed = [
                v for v in food.versions if v["version_id"].startswith(kratheos.VERSION_PREFIX)
            ]
            if len(proposed) >= MAX_USER_VERSIONS:
                raise ApiError(
                    409,
                    "too_many_versions",
                    f"this food already has {MAX_USER_VERSIONS} versions proposed by users",
                )
            version_id = kratheos.VERSION_PREFIX + str(uuid.uuid4())
            fields = {"basis": basis, **values, **portion, "countries": countries}
            self.store.add_version(version_id, food.id, fields, key)
            food = foods.find(context, food.id)
            return Response(201, {"versions": foods.render_versions(context, food, country)})

    def _vote(self, food_id: str, query: Mapping, headers: Mapping, body: bytes) -> Response:
        """Give or move the vote of a person among the versions of a food."""
        self._authorize(headers)
        self._parameters(query, ("country",))
        country = validation.country(query.get("country"))
        data = validation.parse_body(body, VOTE_FIELDS)
        key = validation.contributor(data)
        version_id = data.get("version_id")
        if not isinstance(version_id, str):
            raise invalid("version_id is required")
        with self._writing, self._context() as context:
            food = foods.find(context, validation.food_id(food_id))
            if food is None:
                raise not_found()
            if version_id not in {version["version_id"] for version in food.versions}:
                raise not_found("version of this food")
            self.store.vote(food.id, version_id, key)
            food = foods.find(context, food.id)
            return Response(200, {"versions": foods.render_versions(context, food, country)})

    def _translate(self, food_id: str, query: Mapping, headers: Mapping, body: bytes) -> Response:
        """Give a food a name in a language it lacks."""
        self._authorize(headers)
        self._parameters(query, READ_PARAMETERS)
        language, country = self._locale(query)
        data = validation.parse_body(body, TRANSLATION_FIELDS)
        key = validation.contributor(data)
        lang = validation.language(data.get("language"), "language", required=True)
        name = validation.text(data.get("name"), "name", validation.MAX_NAME, required=True)
        with self._writing, self._context() as context:
            food = foods.find(context, validation.food_id(food_id))
            if food is None:
                raise not_found()
            if lang in food.names and food.names[lang][1] == foods.NAME_ORIGIN_SOURCE:
                raise ApiError(
                    409, "name_exists", "the source already gives a name in this language"
                )
            self.store.translate(food.id, lang, name, key)
            food = foods.find(context, food.id)
            return Response(200, foods.render_food(context, food, language, country))

    def _report(self, food_id: str, query: Mapping, headers: Mapping, body: bytes) -> Response:
        """Record what a person reports about a food."""
        self._authorize(headers)
        self._parameters(query, ())
        data = validation.parse_body(body, REPORT_FIELDS)
        key = validation.contributor(data)
        reason = validation.report_reason(data.get("reason"))
        note = validation.text(data.get("note"), "note", validation.MAX_NOTE) or ""
        with self._writing, self._context() as context:
            food = foods.find(context, validation.food_id(food_id))
            if food is None:
                raise not_found()
            report_id = self.store.report(food.id, reason, note, key)
            return Response(202, {"id": report_id, "state": live.REPORT_OPEN})

    # --- moderation ----------------------------------------------------

    def _reports(self, query: Mapping, headers: Mapping, body: bytes) -> Response:
        """List the reports with the food each one is about."""
        self._authorize(headers)
        self._parameters(query, ("state", "limit", "offset", *READ_PARAMETERS))
        language, country = self._locale(query)
        state = validation.choice(query.get("state"), "state", REPORT_STATES) or live.REPORT_OPEN
        limit = validation.integer(
            query.get("limit"), "limit", validation.MAX_LIMIT, 1, validation.MAX_REPORTS_LIMIT
        )
        offset = validation.integer(query.get("offset"), "offset", 0, 0, 10**8)
        with self._context() as context:
            rows = live.reports(context.live, None if state == "all" else state, limit + 1, offset)
            food_ids = [row["food_id"] for row in rows[:limit]]
            found = foods.assemble(
                context,
                catalogue.foods_by_id(context.base, food_ids),
                food_ids,
                include_hidden=True,
            )
            items = []
            for row in rows[:limit]:
                food = found.get(row["food_id"])
                items.append(
                    {
                        "id": row["id"],
                        "food_id": row["food_id"],
                        "reason": row["reason"],
                        "note": row["note"],
                        "state": row["state"],
                        "created_at": row["created_at"],
                        "closed_at": row["closed_at"],
                        "hidden": bool(food and food.hidden),
                        "food": food and foods.render_food(context, food, language, country),
                    }
                )
            return Response(
                200, {"items": items, "next_offset": offset + limit if len(rows) > limit else None}
            )

    def _close_report(
        self, report_id: str, query: Mapping, headers: Mapping, body: bytes
    ) -> Response:
        """Close a report."""
        self._authorize(headers)
        self._parameters(query, ())
        number = validation.integer(report_id, "report_id", 0, 1, 2**62)
        with self._writing:
            if not self.store.close_report(number):
                raise not_found("report")
        return Response(200, {"id": number, "state": live.REPORT_CLOSED})

    def _hide(
        self, record_id: str, hidden: bool, query: Mapping, headers: Mapping, body: bytes
    ) -> Response:
        """Hide a food, or one of its versions, or show it again."""
        self._authorize(headers)
        self._parameters(query, ())
        record_id = validation.food_id(record_id)
        with self._writing, self._context() as context:
            known = (
                foods.find(context, record_id, include_hidden=True) is not None
                or live.version_owner(context.live, record_id) is not None
                or catalogue.food_of_version(context.base, record_id) is not None
            )
            if not known:
                raise not_found("food or version")
            self.store.set_hidden(record_id, hidden)
        return Response(200, {"id": record_id, "hidden": hidden})
