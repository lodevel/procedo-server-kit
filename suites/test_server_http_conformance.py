"""The rules of the contract every answer follows whatever the call (``procedo_server_contract.md``
§3, §4, §8.1), checked with plain HTTP against the reference server or the server the
environment names (``server_target``): discovery and its public answer, ``401`` for a token that
is not one and ``403`` for a login without the right, ``404`` with the server's signature, the
headers on every answer, the error envelope, one connection kept open, the base with and
without its trailing slash, no absolute URL of the API in an answer.

What the store-then-read-back suites cover (every call, through the app's client) is not
repeated here.
"""

from __future__ import annotations

import http.client
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import pytest
import server_target

from procedo.core.api_session import ApiSession
from procedo.core.project_store_api import ApiProjectSource

#: Every answer of every test here passes ``Server.check_signed`` (``X-Bench-Store: v1``).
pytestmark = [pytest.mark.servers, pytest.mark.rule("3.2.x-bench-store")]

MACHINE = "0000a001"
TEST_ID = "7d1c0f52-3b8e-4a61-9f27-0c5e2d8a4b13"
TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z")
ENVELOPE = {"code", "kind", "request_id", "message"}
#: The largest request, and the largest file sent alone, the suites send: a project's text
#: documents in one request, its board archive alone (bytes, rounded up).
SUITES_NEED = 4 * 1024 * 1024
LOCAL_TOKENS = {
    "t-bench": {"name": "bench", "roles": ["bench", "publisher"]},
    "t-manager": {"name": "manager-login", "roles": ["manager"]},
}


@dataclass
class Reply:
    status: int
    headers: http.client.HTTPMessage
    body: bytes

    @property
    def json(self) -> dict:
        assert self.headers.get("Content-Type", "").startswith("application/json"), self.headers
        return json.loads(self.body)

    @property
    def error(self) -> dict:
        return self.json["error"]


class Server:
    """The server under test, reached with ``http.client`` on one connection."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        parts = urlsplit(base_url)
        self.prefix = parts.path.rstrip("/") + "/api/v1/"
        self.address = parts.netloc + self.prefix  # the API's address, scheme left out
        if parts.scheme == "https":
            context = server_target.tls_context()
            self.connection: http.client.HTTPConnection = http.client.HTTPSConnection(
                parts.netloc, timeout=30, context=context
            )
        else:
            self.connection = http.client.HTTPConnection(parts.netloc, timeout=30)

    def call(self, method: str, path: str, *, token: str | None, body: dict | None = None) -> Reply:
        headers = {"Accept": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        elif method == "POST":
            data = b""
        self.connection.request(method, self.prefix + path, body=data, headers=headers)
        answer = self.connection.getresponse()
        reply = Reply(answer.status, answer.headers, answer.read())
        self.check_signed(reply)
        return reply

    def check_signed(self, reply: Reply) -> None:
        """§3.2 and §3.1: the server's signature, a request id and a date on every answer; no
        header nor body holds the API's own address."""
        assert reply.headers.get("X-Bench-Store") == "v1", (reply.status, dict(reply.headers))
        assert reply.headers.get("X-Request-Id"), (reply.status, dict(reply.headers))
        assert parsedate_to_datetime(reply.headers["Date"]).tzinfo is not None
        for name, value in reply.headers.items():
            assert self.address not in value, f"header {name} holds the API's address: {value}"
        assert self.address.encode() not in reply.body, "the body holds the API's address"

    def close(self) -> None:
        self.connection.close()


def check_envelope(reply: Reply, status: int, kind: str, *codes: str) -> dict:
    """§4.1: the envelope, its ``request_id`` the answer's ``X-Request-Id``, its message a
    sentence."""
    assert reply.status == status, (reply.status, reply.body[:500])
    error = reply.error
    assert ENVELOPE <= set(error), error
    assert error["kind"] == kind, error
    if codes:
        assert error["code"] in codes, error
    assert error["request_id"] == reply.headers["X-Request-Id"]
    assert isinstance(error["message"], str) and error["message"].strip(), error
    details = error.get("details")
    assert details is None or isinstance(details, dict), error
    return error


@pytest.fixture
def served(tmp_path):
    with server_target.serve(tmp_path / "server", LOCAL_TOKENS) as (base_url, _running):
        yield base_url


@pytest.fixture
def server(served):
    server = Server(served)
    yield server
    server.close()


@pytest.mark.rule("3.3.info-fields", "3.3.info-api-version-1", "3.3.info-roles")
def test_info_answers_exactly_its_fields_to_a_valid_token(server):
    reply = server.call("GET", "info", token=server_target.token("bench"))
    assert reply.status == 200
    info = reply.json
    assert set(info) == {"server", "api_versions", "server_time", "limits", "user"}
    assert isinstance(info["server"], str) and info["server"]
    assert "1" in info["api_versions"]
    assert TIME.fullmatch(info["server_time"]), info["server_time"]
    for limit in ("max_upload_bytes", "max_file_bytes", "retention_days"):
        value = info["limits"][limit]
        assert isinstance(value, int) and not isinstance(value, bool) and value >= 1, limit
    assert isinstance(info["user"]["name"], str) and info["user"]["name"]
    assert {"bench", "publisher"} <= set(info["user"]["roles"]), info["user"]
    manager = server.call("GET", "info", token=server_target.token("manager")).json
    assert "manager" in manager["user"]["roles"], manager["user"]


def test_the_limits_leave_room_for_the_suites(server):
    """Not a rule of the contract (a server sets the limits it likes): what the suites need of
    them, so that a limit set lower fails here, saying so, rather than as a refused write in
    another case."""
    limits = server.call("GET", "info", token=server_target.token("bench")).json["limits"]
    low = {k: limits[k] for k in ("max_upload_bytes", "max_file_bytes") if limits[k] < SUITES_NEED}
    assert not low, f"the suites send up to {SUITES_NEED} bytes; this server's limits: {low}"


def test_the_date_header_is_within_two_seconds_of_the_server_time(server):
    reply = server.call("GET", "info", token=server_target.token("bench"))
    date = parsedate_to_datetime(reply.headers["Date"])
    said = datetime.strptime(reply.json["server_time"], "%Y-%m-%dT%H:%M:%S.%fZ")
    assert abs((date - said.replace(tzinfo=timezone.utc)).total_seconds()) <= 2


@pytest.mark.parametrize("token", [None, "not-a-token-of-this-server"], ids=["none", "garbage"])
@pytest.mark.rule("3.3.info-401-public", "4.3.401-auth")
def test_info_without_a_valid_token_is_401_with_only_the_public_details(server, token):
    server_name = server.call("GET", "info", token=server_target.token("bench")).json["server"]
    reply = server.call("GET", "info", token=token)
    error = check_envelope(reply, 401, "auth", "invalid_token", "expired_token")
    challenge = "Bearer" if token is None else 'Bearer error="invalid_token"'
    assert reply.headers.get("WWW-Authenticate") == challenge
    assert set(error["details"]) == {"api_versions", "server"}
    assert "1" in error["details"]["api_versions"]
    assert error["details"]["server"] == server_name


@pytest.mark.parametrize("token", [None, "not-a-token-of-this-server"], ids=["none", "garbage"])
@pytest.mark.rule("4.3.401-auth")
def test_any_other_call_without_a_valid_token_is_401(server, token):
    reply = server.call("GET", "storages", token=token)
    check_envelope(reply, 401, "auth", "invalid_token", "expired_token")
    challenge = "Bearer" if token is None else 'Bearer error="invalid_token"'
    assert reply.headers.get("WWW-Authenticate") == challenge


@pytest.mark.rule("4.3.403-denied")
def test_a_valid_login_without_the_right_is_403_never_401(server, tmp_path):
    """§3.5: a purge from a login that is not a manager, of a project the server holds (the
    bench made it just before), so the answer cannot be the project's absence: the contract
    does not say whether a server looks at the role or at the project first."""
    project = server_target.fresh_id()
    bench = server_target.token("bench")
    session = ApiSession(
        server_target.endpoint(server.base_url), lambda _identity: ("bench", bench)
    )
    try:
        config = json.dumps({"project_id": project}).encode()
        name = f"Ripple bench {server_target.fresh_suffix()}"
        made = ApiProjectSource(session, tmp_path / "cache").create_project(
            name, {"config/config.json": config}
        )
        assert made.project_id == project
    finally:
        session.close()
    reply = server.call(
        "POST",
        f"projects/{project}/purge",
        token=server_target.token("bench"),
        body={"machine_id": MACHINE},
    )
    check_envelope(reply, 403, "denied", "not_a_manager")


@pytest.mark.rule("4.3.404-signed", "6.1.item-not-held-404")
def test_an_item_the_server_does_not_hold_is_404_with_its_signature(server):
    reply = server.call(
        "GET", f"projects/{server_target.fresh_id()}", token=server_target.token("bench")
    )
    check_envelope(reply, 404, "not_found", "not_found")


@pytest.mark.rule("3.6.keep-alive")
def test_one_connection_serves_five_requests_in_a_row(server):
    """§8.1 rule 5: the connection stays open from one request to the next."""
    server.call("GET", "info", token=server_target.token("bench"))
    first = server.connection.sock
    assert first is not None, "the server closed the connection after one answer"
    for _ in range(4):
        reply = server.call("GET", "info", token=server_target.token("bench"))
        assert reply.status == 200
        assert server.connection.sock is first, "the server closed the connection"


@pytest.mark.parametrize("slash", [True, False], ids=["with", "without"])
@pytest.mark.rule("3.3.info-api-version-1")
def test_the_apps_discovery_takes_the_base_with_or_without_its_trailing_slash(served, slash):
    base = served.rstrip("/") + ("/" if slash else "")
    bench = server_target.token("bench")
    session = ApiSession(server_target.endpoint(base), lambda _identity: ("bench", bench))
    try:
        session.ready()
        assert "bench" in session.roles()
    finally:
        session.close()


@pytest.mark.rule("3.1.base-slash")
def test_a_project_written_through_one_form_of_the_base_reads_back_through_the_other(
    served, tmp_path
):
    """§3.1, §8.1 rule 6: the base typed without its trailing slash and with it are one server.
    A bench given one form creates a project, a bench given the other reads it back whole."""
    bare = served.rstrip("/")
    bench = server_target.token("bench")
    project = server_target.fresh_id()
    files = {
        "config/config.json": json.dumps({"project_id": project}).encode(),
        "tests/T1/test.json": json.dumps({"active": "text", "test_id": TEST_ID}).encode(),
        "tests/T1/procedure.json": b'{"steps": []}\n',
        "documents/board.bin": b"\xff" + bytes(range(256)) * 8,
    }
    name = f"Ripple bench {server_target.fresh_suffix()}"
    sources = []
    for base, folder in ((bare, "without"), (bare + "/", "with")):
        session = ApiSession(server_target.endpoint(base), lambda _identity: ("bench", bench))
        sources.append((session, ApiProjectSource(session, tmp_path / folder)))
    try:
        (_, writer), (_, reader) = sources
        made = writer.create_project(name, files)
        read = reader.read_project(project)
        assert (read.project_id, read.name, read.token) == (project, name, made.token)
        assert {path: ref.version for path, ref in read.files.items()} == {
            path: ref.version for path, ref in made.files.items()
        }
        fetched = reader.fetch(project, read.files["documents/board.bin"], tmp_path / "board.bin")
        assert fetched.read_bytes() == files["documents/board.bin"]
    finally:
        for session, _source in sources:
            session.close()
