"""The HTTP side: routing, bearer tokens, bodies, answers. Stdlib ``http.server`` only.

``start(tmp_path, tokens)`` serves on 127.0.0.1 (a free port) and returns ``(base_url,
server)``; ``base_url`` is the configured base address (one trailing slash), to which the
client adds ``api/v1/`` (procedo_api.openapi.yaml, servers). ``stop(server)`` ends it.
"""

from __future__ import annotations

import gzip
import json
import secrets
import threading
import traceback
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit

from . import (
    artifacts,
    blob_calls,
    boards,
    changes,
    faults,
    library,
    offer,
    pointers,
    project_tests,
    projects,
    purge,
    releases,
    runs,
    search,
    storages,
)
from .call import Answer, Call, Query
from .errors import ApiError, bad_request, forbidden, not_found
from .multipart import MAX_PART_BYTES, build_mixed, gunzip, parse_form_data
from .state import State, User
from .validate import PROJECT_ID, RUN_ID, RUN_ID_SHAPE, TEST_ID, load_json

SERVER_NAME = "procedo-reference"
ANY = None  # any login the server knows: every login reads (§5.2)
OWN = "own"  # the handler checks the role itself (the manager calls)
BENCH = frozenset({"bench"})
PUBLISH = frozenset({"bench", "publisher"})  # L3: the handler checks an item with code
UPLOAD = frozenset({"bench", "publisher", "importer"})  # B1: the handler checks the owner's role
BLOB = ("storages", "{store_id}", "blobs", "{owner}", "{sha256}")


def info(call: Call) -> Answer:
    """``GET /api/v1/info``: the contract's ``Info``, exactly its fields (web_api_spec.md §3.1)."""
    state, user = call.state, call.user
    return Answer(
        {
            "server": SERVER_NAME,
            "api_versions": ["1"],
            "server_time": state.now_ms(),
            "limits": {
                "max_upload_bytes": state.max_upload_bytes,
                "max_file_bytes": state.max_file_bytes,
                "retention_days": state.retention_days,
            },
            "calls": offer.listed(state.calls),
            "boards_registered": state.registered != faults.on("3.3.info-boards-registered"),
            "user": {"name": user.name, "roles": sorted(user.roles)}
            | (
                {"token_expires": "2001-01-01T00:00:00Z"}
                if faults.on("3.3.info-token-expires")
                else {}
            ),
        }
    )


def users(call: Call) -> Answer:
    """U1 ``GET /users``: the server's active users (§6.8)."""
    return Answer(offer.users_answer(call.state.people or []))


@dataclass(frozen=True)
class Route:
    method: str
    pattern: tuple[str, ...]
    handler: Callable[[Call], Answer]
    roles: frozenset | str | None
    multipart: bool = False
    importable: bool = False  # it takes "import": true (§6.7), with the importer role
    raw: bool = False  # the body is a file's bytes themselves (§3.4b)
    held_first: bool = False  # 404 for a project whose files are not here, before the body
    #: The query parameters the call defines, checked with the request's form, before the
    #: login's roles and before what the call names (§2.11, §2.13); None: the call checks its
    #: own (E2, E8: an undefined one is ``unsupported_filter``).
    query: frozenset[str] | None = frozenset()
    #: The call's id in §6.1 (``info`` for discovery): an optional one this server does not
    #: offer answers ``501`` (``offer``). P2 with ``since`` is P3, P4 with ``names`` is P5.
    call: str = ""

    def call_id(self, query: Query) -> str:
        if self.call == "P2" and query.has("since"):
            return "P3"
        if self.call == "P4" and query.has("names"):
            return "P5"
        return self.call


P = ("projects", "{project_id}")
T = (*P, "tests", "{test}")
R = (*P, "releases", "{test_id}")
LIB = ("library", "{kind}", "{id}", "{version}")
HAVE = frozenset({"have"})
BOARD = frozenset({"part_number", "board_revision"})
SEARCH = None  # E2 and E8 check their own parameters: an undefined one is unsupported_filter
ROUTES = (
    Route("GET", ("info",), info, ANY, call="info"),
    Route("GET", ("users",), users, ANY, call="U1"),
    Route("GET", ("projects",), projects.list_projects, ANY, call="P1"),
    Route("POST", ("projects",), projects.create_project, BENCH, multipart=True, call="P11"),
    Route("GET", P, projects.read_project, ANY, query=frozenset({"have", "since"}), call="P2"),
    Route("POST", (*P, "read"), projects.read_project, ANY, call="P2b"),
    Route(
        "GET",
        (*P, "tests"),
        project_tests.list_tests,
        ANY,
        query=frozenset({"names", "have"}),
        call="P4",
    ),
    Route("POST", (*P, "tests", "read"), project_tests.read_tests, ANY, call="P6"),
    Route("GET", T, project_tests.get_test, ANY, query=HAVE, call="P7"),
    Route("POST", (*T, "read"), project_tests.get_test, ANY, call="P7"),
    Route("GET", R, releases.list_releases, ANY, call="P8"),
    Route("POST", R, releases.release, BENCH, multipart=True, importable=True, call="P10"),
    Route("GET", (*R, "{version}"), releases.get_release, ANY, query=HAVE, call="P8b"),
    Route("POST", (*R, "{version}", "read"), releases.get_release, ANY, call="P8b"),
    Route("GET", ("library",), library.list_library, ANY, query=frozenset({"kind"}), call="L1"),
    Route("POST", ("library", "{kind}"), library.publish, PUBLISH, multipart=True, call="L3"),
    Route("GET", LIB, library.get_item, ANY, query=HAVE, call="L2"),
    Route("POST", (*LIB, "read"), library.get_item, ANY, call="L2"),
    Route("GET", ("storages",), storages.list_storages, ANY, call="S1"),
    Route("POST", ("storages",), storages.declare_storage, BENCH, call="S2"),
    Route("GET", ("storages", "{store_id}", "to_delete"), storages.to_delete, BENCH, call="S3"),
    Route("POST", ("storages", "{store_id}", "deleted"), storages.deleted, BENCH, call="S4"),
    Route("GET", BLOB, blob_calls.download, ANY, call="B2"),
    Route("POST", BLOB, blob_calls.upload, UPLOAD, raw=True, call="B1"),
    Route("GET", ("benches",), storages.list_benches, ANY, call="E14"),
    Route("GET", ("runs",), search.search_runs, ANY, query=SEARCH, call="E2"),
    Route(
        "POST",
        (*P, "changes"),
        changes.push_changes,
        BENCH,
        multipart=True,
        held_first=True,
        call="P9",
    ),
    Route("POST", (*P, "runs"), runs.push_run, BENCH, multipart=True, importable=True, call="E1"),
    Route("GET", (*P, "runs", "{run_id}"), runs.read_run, ANY, query=HAVE, call="E3"),
    Route("POST", (*P, "runs", "{run_id}", "read"), runs.read_run, ANY, call="E3"),
    Route(
        "POST",
        (*P, "runs", "{run_id}", "annotation"),
        runs.save_annotation,
        BENCH,
        importable=True,
        call="E7",
    ),
    Route(
        "POST",
        (*P, "runs", "{run_id}", "delete"),
        runs.delete_run,
        OWN,
        importable=True,
        call="E11",
    ),
    Route("POST", (*P, "runs", "{run_id}", "restore"), runs.restore_run, OWN, call="E11"),
    Route("GET", (*P, "boards"), boards.list_boards, ANY, query=BOARD, call="E4"),
    Route("GET", (*P, "operators"), search.list_operators, ANY, call="E16"),
    Route(
        "GET",
        (*P, "readings"),
        search.list_readings,
        ANY,
        query=frozenset({"test"}),
        call="E17",
    ),
    Route(
        "GET",
        (*P, "boards", "{serial}"),
        boards.board_results,
        ANY,
        query=BOARD | {"include", "have"},
        call="E5",
    ),
    Route(
        "POST",
        (*P, "boards", "{serial}", "read"),
        boards.board_results,
        ANY,
        query=BOARD,
        call="E5",
    ),
    Route(
        "POST",
        (*P, "boards", "{serial}", "tests", "{test_id}", "current"),
        pointers.set_current,
        BENCH,
        importable=True,
        query=BOARD,
        call="E6",
    ),
    Route(
        "POST",
        (*P, "boards", "{serial}", "entries"),
        boards.save_entries,
        BENCH,
        importable=True,
        query=BOARD,
        call="E15",
    ),
    Route("GET", (*P, "artifacts"), artifacts.list_artifacts, ANY, query=SEARCH, call="E8"),
    Route(
        "POST",
        (*P, "artifacts"),
        artifacts.push_artifact,
        BENCH,
        multipart=True,
        importable=True,
        call="E10",
    ),
    Route(
        "POST",
        (*P, "artifacts", "delete"),
        artifacts.delete_artifact,
        OWN,
        importable=True,
        call="E12",
    ),
    Route("POST", (*P, "artifacts", "restore"), artifacts.restore_artifact, OWN, call="E12"),
    Route(
        "GET",
        (*P, "artifacts", "{kind}", "{name}"),
        artifacts.get_artifact,
        ANY,
        query=BOARD | {"serial", "generation", "have"},
        call="E9",
    ),
    Route("POST", (*P, "purge"), purge.purge, OWN, call="E13"),
)


def _match(method: str, segments: list[str]) -> tuple[Route, dict[str, str]]:
    allowed = set()
    for route in ROUTES:
        if len(route.pattern) != len(segments):
            continue
        params = {}
        for pattern, segment in zip(route.pattern, segments, strict=True):
            if pattern.startswith("{"):
                if segment == "":
                    break
                params[pattern[1:-1]] = segment
            elif pattern != segment:
                break
        else:
            if route.method == method:
                return route, params
            allowed.add(route.method)
    if allowed:
        raise ApiError(
            405,
            "unsupported",
            "invalid",
            f"This call takes {' or '.join(sorted(allowed))} only.",
            headers={"Allow": ", ".join(sorted(allowed))},
        )
    # §4.3: a call this server does not take is 501 unsupported, never a 404 (which the app
    # reads as proof that an item is absent)
    if faults.on("2.1.two-methods"):
        raise not_found("No such call.")
    raise ApiError(501, "unsupported", "invalid", "This server has no such call.")


def _check_params(params: dict[str, str]) -> None:
    if "project_id" in params and PROJECT_ID.fullmatch(params["project_id"]) is None:
        raise bad_request("A project is named by its project_id, a lower-case UUID.")
    if "run_id" in params and RUN_ID.fullmatch(params["run_id"]) is None:
        raise bad_request(f"A run_id is {RUN_ID_SHAPE}.")
    # §2.5: a malformed {test_id} (P8, P8b, P10, E6) is refused before the project is looked at
    malformed = "test_id" in params and TEST_ID.fullmatch(params["test_id"]) is None
    if malformed and not faults.on("2.5.malformed-test-id-400-first"):
        raise bad_request("A test is named here by its test_id, a lower-case UUID.")


def _query_pairs(query: str) -> list[tuple[str, str]]:
    """The query's parameters, each value percent-decoded once; a ``+`` is a plus, never a
    blank (§3.1: a part number may hold one)."""
    if not faults.on("3.1.query-plus"):
        query = query.replace("+", "%2B")
    pairs = parse_qsl(query, keep_blank_values=True)
    if faults.on("2.6.filter-matches-nothing"):
        pairs = [(name, value.strip()) for name, value in pairs]
    return pairs


def _named_first(call: Call) -> None:
    """The fault of §2.13's roles before the item: what the path names looked for before the
    login's roles, so an unknown project, run or storage is ``404`` whoever asks."""
    state, params = call.state, call.params
    project = state.projects.get(params.get("project_id", ""))
    if "project_id" in params and project is None:
        raise not_found("This server holds nothing for this project.")
    if "run_id" in params and project is not None and params["run_id"] not in project.runs:
        raise not_found("No such run in this project.")
    store_id = params.get("store_id", "")
    if store_id.isdigit() and state.storage_of(int(store_id)) is None:
        raise not_found("No such storage.")


def _check_role(route: Route, call: Call) -> None:
    """A write needs a role that allows it: the call's, or the importer's with ``"import":
    true`` where the call takes an import (§5.2). Else ``403 forbidden``."""
    roles = call.user.roles
    if faults.on("3.5.every-login-reads") and route.call == "E8":  # a read needing a role
        if "bench" not in roles:
            raise forbidden("This login may not read.")
    if route.roles in (ANY, OWN) or roles & route.roles:  # type: ignore[operator]
        return
    if faults.on("3.5.write-needs-role") and route.handler is not runs.push_run:
        return
    if (
        faults.on("3.5.manager-only-no-push")
        and route.handler is runs.push_run
        and "manager" in roles
    ):
        return
    if route.raw and call.state.broken("B1.roles-403"):
        return
    if route.importable and "importer" in call.user.roles and call.import_asked():
        return
    needs = " or ".join(sorted(route.roles))  # type: ignore[arg-type]
    hint = ', or the importer role with "import": true' if route.importable else ""
    raise forbidden(f"This login may not make this call (it needs the {needs} role{hint}).")


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = SERVER_NAME
    server: ReferenceServer

    def log_message(self, format: str, *args: object) -> None:
        pass

    # ---- Methods ---------------------------------------------------------------------------

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def _refuse_method(self) -> None:
        self.close_connection = True
        if faults.on("2.1.two-methods"):
            self._send_error(not_found("No such call."), secrets.token_hex(8))
            return
        error = ApiError(
            405,
            "unsupported",
            "invalid",
            "This API takes GET and POST only.",
            headers={"Allow": "GET, POST"},
        )
        self._send_error(error, secrets.token_hex(8))
        length = self.headers.get("Content-Length", "")
        self._discard(int(length) if length.isdigit() else 0)

    def _discard(self, size: int) -> None:
        """Read and drop a body the server refused unread, so closing never resets the answer."""
        try:
            self.connection.settimeout(5)
            remaining = min(size, 256 * 1024 * 1024)
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, 65536))
                if not chunk:
                    break
                remaining -= len(chunk)
        except OSError:
            pass

    do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _refuse_method

    def handle_expect_100(self) -> bool:
        """A body over the call's limit is refused before it is sent (§3.3, §3.4b, web §7)."""
        length = self.headers.get("Content-Length", "")
        if faults.on("3.6.too-large-before-body"):
            return super().handle_expect_100()
        if length.isdigit() and int(length) > self._limit()[1]:
            self.close_connection = True
            self._send_error(self._names_nothing() or self._too_large(), secrets.token_hex(8))
            return False
        return super().handle_expect_100()

    def _names_nothing(self) -> ApiError | None:
        """The fault of §2.13's size first: a request over its limit naming a project or a
        storage the server does not hold answered ``404``, not ``413``. None otherwise."""
        if not faults.on("2.13.too-large-first"):
            return None
        state = self.server.state
        segments = urlsplit(self.path).path.split("/")[3:]
        if len(segments) > 1 and segments[0] == "projects" and segments[1] not in state.projects:
            return not_found("This server holds nothing for this project.")
        if len(segments) > 1 and segments[0] == "storages" and segments[1].isdigit():
            if state.storage_of(int(segments[1])) is None:
                return not_found("No such storage.")
        return None

    def _limit(self) -> tuple[str, int]:
        """The limit on this request's body: ``max_file_bytes`` for a large file's upload (B1,
        §3.4b), ``max_upload_bytes`` for every other request."""
        state = self.server.state
        segments = urlsplit(self.path).path.split("/")[3:]
        upload = self.command == "POST" and len(segments) == len(BLOB)
        if upload and segments[0] == BLOB[0] and segments[2] == BLOB[2]:
            if state.broken("B1.max-file-413"):
                return "max_upload_bytes", state.max_upload_bytes
            return "max_file_bytes", state.max_file_bytes
        if state.broken("3.6.too-large-before-body"):
            return "max_upload_bytes", 1 << 62
        return "max_upload_bytes", state.max_upload_bytes

    # ---- One request ------------------------------------------------------------------------

    def _handle(self) -> None:
        request_id = secrets.token_hex(8)
        state = self.server.state
        self._unread = 0
        # §2.13, the order of checks: the body's size (411, 413) from the headers, the token
        # (401), the call (405, 501), the request's form (400: path, query, body), the login's
        # roles (403), then the call's own checks (404, 409, ...).
        try:
            body = self._read_body()
            segments = self._segments()
            if faults.on("2.13.token-first"):  # the query looked at before the token
                early, _ = _match(self.command, segments)
                if early.query is not None:
                    Query(_query_pairs(urlsplit(self.path).query)).only(early.query)
            user = self._authenticate(segments)
            route, params = _match(self.command, segments)
            query = Query(_query_pairs(urlsplit(self.path).query))
            offer.refuse_unoffered(state.calls, route.call_id(query))  # §6.1: 501, as the call
            if route.call != "E1":  # a run: push_run notes it when a bench's (not an import)
                offer.note(route.call_id(query))
            _check_params(params)
            call = Call(
                state=state,
                user=user,
                method=self.command,
                params=params,
                query=query,
                headers={name.lower(): value for name, value in self.headers.items()},
                wire=self._wire,
            )
            query_late = faults.on("2.13.parameters-before-item")
            if route.query is not None and not query_late:
                call.query.only(route.query)
            if faults.on("2.13.role-before-item"):
                with state.lock:
                    _named_first(call)
            if route.held_first:  # P9: a project whose files are elsewhere, before the body
                _check_role(route, call)  # the call takes no import: its role needs no body
                with state.lock:
                    projects.held_project(call)
            if self.command == "POST":
                self._parse_body(call, route, body)
            if route.importable and call.import_asked():  # §6.1: an import, an optional right
                offer.refuse_import(state.calls, route.call)
                offer.note(offer.IMPORT)
            _check_role(route, call)
            with state.lock:
                answer = route.handler(call)
            if route.query is not None and query_late:  # after what the call names
                call.query.only(route.query)
            if route.pattern[-1] == "read" and state.broken("6.2.read-twins"):
                answer.obj["twin"] = True
            self._send_answer(answer, request_id)
        except ApiError as error:
            self._send_error(error, request_id)
            if self._unread:
                self._discard(self._unread)
        except Exception:  # any other failure is the server's own
            self.server.internal_errors.append(traceback.format_exc())
            self._send_error(
                ApiError(500, "internal", "internal", "The reference server failed."), request_id
            )

    def _too_large(self) -> ApiError:
        name, limit = self._limit()
        return ApiError(
            413,
            "too_large",
            "invalid",
            f"This request is over the server's limit of {limit} bytes.",
            details={name: limit},
        )

    def _read_body(self) -> bytes:
        self._wire = b""
        if self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            if faults.on("3.6.length-required"):
                return b""
            raise ApiError(411, "length_required", "invalid", "A body needs its Content-Length.")
        length = self.headers.get("Content-Length")
        if length is None:
            if self.command == "POST":
                self.close_connection = True
                raise ApiError(
                    411, "length_required", "invalid", "A body needs its Content-Length."
                )
            return b""
        if not length.isdigit():
            self.close_connection = True
            raise bad_request("Content-Length is not a number.")
        size = int(length)
        if size > self._limit()[1] and faults.on("3.6.too-large-before-body"):
            self.close_connection = True
            self.rfile.read(size)
            raise self._too_large()
        if size > self._limit()[1]:
            self.close_connection = True
            self._unread = size  # refused before it is read; dropped after the answer
            raise self._names_nothing() or self._too_large()
        data = self._read_within(size)
        if len(data) < size:
            self.close_connection = True
            raise ApiError(
                400, "short_body", "invalid", "The body is shorter than its Content-Length."
            )
        self._wire = data
        multipart = self.headers.get("Content-Type", "").lower().startswith("multipart/")
        if multipart and self.headers.get("Content-Encoding"):
            # §2.12: a multipart body travels plain; this server refuses one that is not
            raise bad_request("A multipart body is sent plain, with no Content-Encoding.")
        if self.headers.get("Content-Encoding", "").strip().lower() == "gzip":
            data = gunzip(data, "body")
        return data

    def _read_within(self, size: int) -> bytes:
        """The body's *size* bytes, each read waiting at most ``body_timeout`` seconds: a body
        that stops arriving is ``408 request_timeout`` (§4.3), and the connection closes."""
        state = self.server.state
        self.connection.settimeout(state.body_timeout)
        try:
            return self.rfile.read(size)
        except TimeoutError as exc:
            self.close_connection = True
            kind = "invalid" if state.broken("4.3.408") else "timeout"
            raise ApiError(
                408, "request_timeout", kind, "The request's body stopped arriving: send it again."
            ) from exc
        finally:
            self.connection.settimeout(None)

    def _segments(self) -> list[str]:
        raw = urlsplit(self.path).path.split("/")
        if faults.on("3.1.segment-decoding"):
            raw = unquote(urlsplit(self.path).path).split("/")
        if raw[:3] != ["", "api", "v1"]:
            raise not_found("Every call is under /api/v1.")
        try:
            return [unquote(segment, errors="strict") for segment in raw[3:]]
        except UnicodeDecodeError as exc:
            raise bad_request("The path is not UTF-8.") from exc

    def _authenticate(self, segments: list[str]) -> User:
        header = self.headers.get("Authorization")
        is_info = segments == ["info"]
        details = {"api_versions": ["1"], "server": SERVER_NAME} if is_info else {}
        query = dict(parse_qsl(urlsplit(self.path).query))
        if header is None and "access_token" in query and faults.on("3.4.header-only"):
            header = f"Bearer {query['access_token']}"
            self.path = urlsplit(self.path).path  # the token taken, the query read no further
        if header is None:  # no credentials at all: no error code (RFC 6750 §3.1, web §6)
            raise ApiError(
                401,
                "unauthorized" if faults.on("3.4.no-token-401") else "invalid_token",
                "auth",
                "A login is needed for this server.",
                details=details,
                headers={"WWW-Authenticate": "Bearer"},
            )
        scheme, _, token = header.partition(" ")
        user = self.server.state.users.get(token.strip()) if scheme.lower() == "bearer" else None
        if user is None:
            raise ApiError(
                401,
                "invalid_token",
                "auth",
                "The server refused this login.",
                details=details,
                headers={
                    "WWW-Authenticate": "Bearer"
                    if faults.on("3.4.bad-token-401") and not is_info
                    else 'Bearer error="invalid_token"'
                },
            )
        return offer.granted(self.server.state.calls, user)  # §3.5: importer only with import

    def _parse_body(self, call: Call, route: Route, body: bytes) -> None:
        state = call.state
        content_type = self.headers.get("Content-Type", "")
        main = content_type.split(";")[0].strip().lower()
        if route.raw:  # B1: the file's bytes, application/octet-stream (§6.6), checked as form
            if main != "application/octet-stream" and not state.broken("B1.content-type-400"):
                raise bad_request("A file's bytes are sent as application/octet-stream.")
            call.raw_body = body
            return
        if main == "application/json" or (
            main != "multipart/form-data" and state.broken("6.2.request-content-type")
        ):
            raw = body
        elif main == "multipart/form-data" and route.multipart:
            raw, call.parts = parse_form_data(body, content_type, state.broken)
            most = MAX_PART_BYTES - 1 if state.broken("6.2.part-boundary") else MAX_PART_BYTES
            large = sorted(f"sha256:{sha}" for sha, data in call.parts.items() if len(data) > most)
            if large and not (
                state.broken("6.2.part-over-1mib-400")
                or (state.broken("E1.large-file") and route.handler is runs.push_run)
            ):
                raise bad_request(
                    f"A file over {MAX_PART_BYTES} bytes travels alone, by POST "
                    "/storages/{store_id}/blobs/{owner}/{sha256}, never as a part "
                    "(data_format.md §3.4b).",
                    large=large,
                )
        elif main == "multipart/form-data":
            raise bad_request("This call takes a JSON body, not multipart.")
        else:
            raise bad_request("A body is application/json or multipart/form-data.")
        call.raw_json = raw
        call.body = load_json(raw, "JSON body")

    # ---- Answers ----------------------------------------------------------------------------

    def _gzip_ok(self) -> bool:
        accepted = self.headers.get("Accept-Encoding", "") if self.headers else ""
        return any(token.split(";")[0].strip().lower() == "gzip" for token in accepted.split(","))

    def _send_answer(self, answer: Answer, request_id: str) -> None:
        if answer.raw is not None:  # a file itself: never compressed
            self._send(
                answer.status,
                answer.raw,
                "application/octet-stream",
                request_id,
                answer.headers,
                compress=False,
            )
            return
        # §3.4b: a file over 1 MB never rides as a part; the JSON still lists it.
        parts = [part for part in answer.parts if len(part.data) <= MAX_PART_BYTES]
        broken = self.server.state.broken
        if parts and broken("6.2.answer-no-part-json"):
            if "multipart/mixed" not in self.headers.get("Accept", ""):
                refusal = ApiError(406, "not_acceptable", "invalid", "Accept names no multipart.")
                self._send_error(refusal, request_id)
                return
        obj = _faulty(answer.obj)
        if parts:
            payload, content_type = build_mixed(obj, parts)
            gzipped = broken("2.12.no-gzip-multipart-answer") and self._gzip_ok()
            self._send(200, payload, content_type, request_id, {}, compress=gzipped)
        else:
            payload = json.dumps(obj, ensure_ascii=False, allow_nan=False).encode("utf-8")
            self._send(200, payload, "application/json", request_id, {}, compress=self._gzip_ok())

    def _send_error(self, error: ApiError, request_id: str) -> None:
        error = _faulty_error(error)
        envelope = error.envelope(request_id)
        if error.status == 403 and faults.on("4.1.envelope"):
            envelope = envelope["error"]
        payload = json.dumps(envelope, ensure_ascii=False).encode("utf-8")
        self._send(
            error.status, payload, "application/json", request_id, error.headers, compress=False
        )

    def _send(
        self,
        status: int,
        payload: bytes,
        content_type: str,
        request_id: str,
        headers: dict[str, str],
        *,
        compress: bool,
    ) -> None:
        if faults.on("3.2.date"):
            self.log_request(status)
            self.send_response_only(status)
            self.send_header("Server", self.version_string())
        else:
            self.send_response(status)
        self.send_header("X-Bench-Store", "v1")
        if not (status < 300 and faults.on("3.2.x-request-id")):
            self.send_header("X-Request-Id", request_id)
        if faults.on("3.1.no-absolute-url"):
            where = f"http://{self.headers.get('Host', '')}{urlsplit(self.path).path}"
            self.send_header("Content-Location", where)
        self.send_header("Content-Type", content_type)
        if compress:
            payload = gzip.compress(payload)
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(payload)))
        for name, value in headers.items():
            self.send_header(name, value)
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)


_TIMES = ("pushed_at", "received_at", "created_at", "released_at", "deleted_at", "published_at")


def _faulty(obj: object) -> object:
    """The answer as the faults switched on break it (``faults``); itself when none is on."""
    if isinstance(obj, list):
        return [_faulty(item) for item in obj]
    if not isinstance(obj, dict):
        return obj
    out = {}
    for key, value in obj.items():
        if key.startswith("x_") and faults.on("2.2.unknown-fields-kept"):
            continue
        if key in _TIMES and isinstance(value, str) and faults.on("2.7.time-format"):
            value = value.replace("Z", ".000Z")
        out[key] = _faulty(value)
    if "boards" in out and isinstance(out["boards"], list):
        for board in out["boards"]:
            if "lot" not in board and faults.on("2.2.no-null-in-answers"):
                board["lot"] = None
            if faults.on("9.twin-schemas"):
                board["format_version"] = str(board.get("format_version"))
    return out


def _faulty_error(error: ApiError) -> ApiError:
    """The refusal as the faults switched on break it; itself when none is on."""
    status, code, kind, details = error.status, error.code, error.kind, dict(error.details)
    if code == "digest_mismatch" and faults.on("4.3.422"):
        status, code, kind = 400, "bad_request", "invalid"
    if status == 400 and faults.on("4.3.400-invalid"):
        kind = "denied"
    if status in (405, 501) and faults.on("4.3.405-501"):
        kind = "denied"
    if status == 409 and code != "run_conflict" and faults.on("4.3.409-invalid"):
        kind = "denied"
    if code == "run_conflict" and faults.on("4.3.409-run-conflict"):
        kind = "invalid"
    if "missing" in details and faults.on("2.8.digest-forms"):
        details["missing"] = [v.removeprefix("sha256:") for v in details["missing"]]
    if faults.on("4.4.details"):
        details = {}
    if (status, code, kind, details) == (error.status, error.code, error.kind, error.details):
        return error
    return ApiError(status, code, kind, error.message, details=details, path=error.path,
                    headers=error.headers)  # fmt: skip


class ReferenceServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, state: State) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.state = state
        self.internal_errors: list[str] = []
        self.thread: threading.Thread | None = None


def start(
    tmp_path: str | Path,
    tokens: dict,
    *,
    max_upload_bytes: int = 64 * 1024 * 1024,
    max_file_bytes: int = 256 * 1024 * 1024,
    retention_days: int = 90,
    instance_id: str | None = None,
    max_changes: int = 10000,
    own_storage: str | Path | None = None,
    reaches: tuple[str | Path, ...] = (),
    body_timeout: float = 60.0,
    calls: Iterable[str] | None = None,
    users: list[dict] | None = None,
    boards: dict[str, list[dict]] | None = None,
    unnamed_refused: bool = False,
) -> tuple[str, ReferenceServer]:
    """Serve the JSON API on 127.0.0.1, a free port. ``tokens``: ``{token: [roles]}``.

    A token's value may also be ``{"name": login, "roles": [...]}``; otherwise the login's
    name is its roles joined with ``+`` (``"manager"`` for a manager-only token).
    ``max_changes``: the changes a project keeps for P3's ``since``; older is ``410 gone``.
    ``own_storage``: a folder the server keeps as an own ``filesystem`` storage besides ``api``
    (§7.1); ``reaches``: the folders it mounts, so a declared ``filesystem`` storage inside one
    is reached (§7.3); ``body_timeout``: the seconds a body may stall before ``408``.
    ``calls``: the optional calls it offers (§6.1, ``offer.OPTIONAL``), the others answering
    ``501``; None: every one, U1 only with ``users``. ``users``: the users U1 lists (§6.8),
    each ``{"name", "display_name"}``; with U1 offered, a run's operator and a release's
    ``released_by`` must be one of them. ``boards``: the boards its administrator registered,
    ``{project_id: [{"serial", "part_number"?, "board_revision"?}]}``: given, ``/info`` says
    ``boards_registered: true``, E4 lists them before any run and E1 refuses a run on any other
    (``409 unknown_board``); None (the default), any serial run becomes a board (§3.3).
    ``unnamed_refused``: with U1, a run or a release naming nobody is refused ``409
    unknown_operator`` (§6.8, the server's choice); taken by default.
    """
    state = State(
        Path(tmp_path),
        tokens,
        max_upload_bytes=max_upload_bytes,
        max_file_bytes=max_file_bytes,
        retention_days=retention_days,
        instance_id=instance_id,
        max_changes=max_changes,
        own_storage=Path(own_storage) if own_storage is not None else None,
        reaches=tuple(Path(folder) for folder in reaches),
        body_timeout=body_timeout,
        calls=calls,
        people=users,
        boards=boards,
        unnamed_refused=unnamed_refused,
    )
    server = ReferenceServer(state)
    server.thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.05},
        name="procedo-reference-server",
        daemon=True,
    )
    server.thread.start()
    port = server.server_address[1]
    return f"http://127.0.0.1:{port}/", server


def stop(server: ReferenceServer) -> None:
    server.shutdown()
    server.server_close()
    if server.thread is not None:
        server.thread.join(timeout=5)
