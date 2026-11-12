"""The contract's conventions, address, authentication, transport, errors and twin
(``procedo_server_contract.md`` §2, §3, §4, §9), checked with raw HTTP against the reference
server or the server the environment names (``server_target``).

Raw, never through the app's client: the client checks a request before sending it, sorts and
tolerates what it reads, and acts on an error's ``kind`` alone, so a server breaking these rules
would pass through it. Every answer seen here is held to the rules every answer follows
(:meth:`Api.check`): ``X-Bench-Store``, ``X-Request-Id`` and ``Date``; no absolute URL of the
API; the error envelope on every refusal; no ``null`` where the contract has none; a multipart
answer never gzipped, as a whole or a part; and its JSON valid against the twin,
``procedo_api.openapi.yaml`` (:mod:`openapi_twin`).

Each check names the rules it holds (``@pytest.mark.rule``, the ids of
``docs/server_contract/coverage.md``). Nothing here assumes an empty server: every write goes
under a project id, a name or a library id of its own, so the file passes again on the same
server.
"""

from __future__ import annotations

import gzip
import hashlib
import http.client
import json
import re
import socket
import unicodedata
import uuid
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote, urlsplit

import pytest
import server_target
from openapi_twin import Twin
from reference_server import start, stop
from reference_server.offer import OPTIONAL

pytestmark = pytest.mark.servers

MACHINE = "0000c0de"
LOCAL_TOKENS = {
    "t-bench": {"name": "bench", "roles": ["bench", "publisher"]},
    "t-manager": {"name": "manager-login", "roles": ["manager"]},
}
KINDS = {
    "invalid", "not_found", "auth", "denied", "unreachable", "timeout", "corrupt", "foreign",
    "cancelled", "internal",
}  # fmt: skip
#: Where an answer may hold ``null`` (§2.2): a pointer's ``run_id``, an entry's ``text``,
#: ``value`` and ``replaces``, a draft's ``replaces``, a list's ``cursor``, E5's and E11's
#: ``pointer``; the envelope's ``retry_after_s`` (§4.1).
NULL_KEYS = {"run_id", "text", "value", "replaces", "cursor", "pointer", "retry_after_s"}
TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
VERSION = re.compile(r"sha256:[0-9a-f]{64}")
PART_LIMIT = 1024 * 1024
BOUNDARY = "kit-boundary-5e1f0c"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def seg(name: str) -> str:
    """A name as one path segment: UTF-8, percent-encoded on its own (§3.1)."""
    return quote(name, safe="")


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def text_file(text: str) -> dict:
    data = text.encode("utf-8")
    return {"version": f"sha256:{sha(data)}", "size": len(data), "text": text}


def id_json(test_id: str) -> str:
    return json.dumps({"active": "text", "test_id": test_id}) + "\n"


# ---- Raw HTTP --------------------------------------------------------------------------------


@dataclass
class Reply:
    method: str
    path: str
    status: int
    headers: http.client.HTTPMessage
    body: bytes
    json: Any = None
    parts: dict[str, bytes] = field(default_factory=dict)

    @property
    def error(self) -> dict:
        return self.json["error"]

    def __repr__(self) -> str:
        return f"<{self.method} {self.path}: {self.status} {self.body[:400]!r}>"


def form_data(obj: dict, parts: dict[str, bytes]) -> bytes:
    """A ``multipart/form-data`` body: the ``json`` part, plain JSON, then one part per file
    (§6.2); no part carries a ``Content-Encoding`` (§2.12)."""
    data = json.dumps(obj).encode()
    head = 'Content-Disposition: form-data; name="json"\r\nContent-Type: application/json\r\n'
    chunks = [f"--{BOUNDARY}\r\n{head}\r\n".encode(), data, b"\r\n"]
    for name, content in parts.items():
        chunks += [
            f"--{BOUNDARY}\r\n".encode(),
            f'Content-Disposition: form-data; name="{name}"; filename="f.bin"\r\n'.encode(),
            b"Content-Type: application/octet-stream\r\n\r\n",
            content,
            b"\r\n",
        ]
    chunks.append(f"--{BOUNDARY}--\r\n".encode())
    return b"".join(chunks)


def parse_mixed(body: bytes, content_type: str) -> tuple[Any, dict[str, bytes]]:
    boundary = re.search(r'boundary="?([^";]+)"?', content_type).group(1).encode()
    parts: dict[str, bytes] = {}
    obj = None
    for chunk in body.split(b"--" + boundary)[1:-1]:
        head, _, data = chunk[2:-2].partition(b"\r\n\r\n")
        lines = head.decode("utf-8").split("\r\n")
        headers = {k.lower(): v for k, _, v in (line.partition(": ") for line in lines)}
        name = re.search(r'name="([^"]+)"', headers["content-disposition"]).group(1)
        # §2.12: no part carries a Content-Encoding, the json part is plain JSON
        assert "content-encoding" not in headers, f"part {name} carries a Content-Encoding"
        if name == "json":
            obj = json.loads(data)
        else:
            parts[name] = data
    return obj, parts


class Api:
    """The server under test over ``http.client``: one connection, opened again when the server
    closes it. Every answer goes through :meth:`check`."""

    def __init__(self, base_url: str, twin: Twin) -> None:
        self.base_url = base_url
        self.twin = twin
        parts = urlsplit(base_url)
        self.https = parts.scheme == "https"
        self.netloc = parts.netloc
        self.host = parts.hostname or ""
        self.port = parts.port or (443 if self.https else 80)
        self.prefix = parts.path.rstrip("/") + "/api/v1/"
        self.address = parts.netloc + self.prefix  # the API's address, scheme left out
        self.connection: http.client.HTTPConnection | None = None

    def connect(self) -> http.client.HTTPConnection:
        if self.connection is None:
            if self.https:
                self.connection = http.client.HTTPSConnection(
                    self.netloc, timeout=60, context=server_target.tls_context()
                )
            else:
                self.connection = http.client.HTTPConnection(self.netloc, timeout=60)
        return self.connection

    def close(self) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def offers(self, *calls: str) -> bool:
        """Whether the server offers every one of *calls* (optional calls, §6.1): a case marked
        ``offer_aware`` makes them only then, and checks the core calls on any server."""
        return server_target.offers(self.base_url, *calls)

    def call(
        self,
        method: str,
        path: str,
        *,
        role: str | None = "bench",
        token: str | None = None,
        json_body: Any = None,
        parts: dict[str, bytes] | None = None,
        data: bytes | None = None,
        query: str = "",
        headers: dict[str, str] | None = None,
        chunked: bool = False,
    ) -> Reply:
        sent = {"Accept": "multipart/mixed, application/json", "Accept-Encoding": "gzip"}
        if token is None and role is not None:
            token = server_target.token(role)
        if token is not None:
            sent["Authorization"] = f"Bearer {token}"
        if parts is not None:
            data = form_data(json_body, parts)
            sent["Content-Type"] = f"multipart/form-data; boundary={BOUNDARY}"
        elif json_body is not None:
            data = json.dumps(json_body).encode()
            sent["Content-Type"] = "application/json"
        if data is None and method == "POST":
            data = b""
        sent.update(headers or {})
        target = self.prefix + path + (f"?{query}" if query else "")
        connection = self.connect()
        if chunked:
            sent["Transfer-Encoding"] = "chunked"
        try:
            if chunked:
                connection.request(method, target, body=iter([data]), headers=sent,
                                   encode_chunked=True)  # fmt: skip
            else:
                connection.request(method, target, body=data, headers=sent)
        except (BrokenPipeError, ConnectionResetError):
            # A server may answer from the head alone (411) and close before the body is all
            # sent; that is allowed, and its answer is read below.
            pass
        answer = connection.getresponse()
        raw = answer.read()
        reply = Reply(method, path, answer.status, answer.headers, raw)
        if answer.will_close:
            self.close()
        self.check(reply)
        return reply

    # -- what every answer follows --

    def check(self, reply: Reply) -> None:
        headers = reply.headers
        # §3.2: the server's signature, a request id and a date, on every answer
        assert headers.get("X-Bench-Store") == "v1", (reply, dict(headers))
        assert headers.get("X-Request-Id"), (reply, dict(headers))
        assert headers.get("Date") and parsedate_to_datetime(headers["Date"]).tzinfo, reply
        # §3.1: no absolute URL of the API in a header or a body
        for name, value in headers.items():
            assert self.address not in value, f"{reply}: header {name} holds the API's address"
        content_type = headers.get("Content-Type", "")
        encoding = headers.get("Content-Encoding", "").lower()
        if content_type.startswith("multipart/"):
            # §2.12: a server never gzips a whole multipart answer
            assert encoding != "gzip", f"{reply}: a multipart answer gzipped whole"
        body = gzip.decompress(reply.body) if encoding == "gzip" else reply.body
        reply.body = body
        if not content_type.startswith("application/octet-stream"):
            assert self.address.encode() not in body, f"{reply}: the body holds the API's address"
        if reply.method == "HEAD":
            return
        if content_type.startswith("application/json"):
            reply.json = json.loads(body)
        elif content_type.startswith("multipart/mixed"):
            reply.json, reply.parts = parse_mixed(body, content_type)
            for name, data in reply.parts.items():
                assert name == f"sha256:{sha(data)}", f"{reply}: a part not named by its bytes"
        if reply.status >= 400:
            check_envelope_shape(reply)
        elif reply.json is not None:
            nulls = list(null_paths(reply.json, self.twin.properties))
            assert not nulls, f"{reply}: null where the contract has none: {nulls}"
        if reply.json is not None:
            self.check_twin(reply, content_type)

    def check_twin(self, reply: Reply, content_type: str) -> None:
        """§9: the answer validates against the twin's schema for its call and status."""
        path = "/" + reply.path.split("?")[0]
        if self.twin.response(reply.method, path, reply.status) is None:
            return  # a call the contract does not have (an unknown call's refusal)
        schema = self.twin.answer_schema(reply.method, path, reply.status, content_type)
        errors = self.twin.errors(reply.json, schema)
        assert not errors, f"{reply}: breaks procedo_api.openapi.yaml: {errors[:8]}"


def null_paths(value: Any, known: set[str], where: str = "$") -> Any:
    """Where *value* holds ``null`` in a field the contract names (*known*) outside
    :data:`NULL_KEYS`. A field the contract does not name is a client's, kept as sent (§2.2)."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key not in known:
                continue
            if item is None and key not in NULL_KEYS:
                yield f"{where}.{key}"
            else:
                yield from null_paths(item, known, f"{where}.{key}")
    elif isinstance(value, list):
        for n, item in enumerate(value):
            if item is None:
                yield f"{where}[{n}]"
            else:
                yield from null_paths(item, known, f"{where}[{n}]")


def check_envelope_shape(reply: Reply) -> None:
    """§4.1: every non-2xx body is the envelope, ``application/json``, its ``request_id`` the
    answer's ``X-Request-Id``, its ``message`` a sentence, ``kind`` one of §4.2."""
    assert reply.headers.get("Content-Type", "").startswith("application/json"), (
        f"{reply}: a refusal that is not application/json"
    )
    assert isinstance(reply.json, dict) and isinstance(reply.json.get("error"), dict), (
        f"{reply}: no error envelope"
    )
    error = reply.error
    assert {"code", "kind", "request_id", "message"} <= set(error), f"{reply}: {error}"
    assert isinstance(error["code"], str) and error["code"], reply
    assert error["kind"] in KINDS, f"{reply}: kind {error['kind']!r} is none of §4.2"
    assert error["request_id"] == reply.headers["X-Request-Id"], reply
    assert isinstance(error["message"], str) and error["message"].strip(), reply
    details = error.get("details")
    assert details is None or isinstance(details, dict), reply


def refused(reply: Reply, status: int, kind: str, *codes: str) -> dict:
    """The refusal *reply* must be: its status, kind and code (one of *codes*)."""
    assert reply.status == status, f"{reply}: expected {status}"
    error = reply.error
    assert error["kind"] == kind, f"{reply}: expected kind {kind}"
    if codes:
        assert error["code"] in codes, f"{reply}: expected code {codes}"
    return error


def ok(reply: Reply) -> Any:
    assert reply.status == 200, f"{reply}: expected 200"
    return reply.json


# ---- What the checks write -------------------------------------------------------------------


def project_body(project_id: str, test_id: str, **extra_files: dict) -> dict:
    files = {
        "config/config.json": text_file(json.dumps({"project_id": project_id}) + "\n"),
        "tests/T1/test.json": text_file(id_json(test_id)),
        "tests/T1/procedure_text.md": text_file("# T1\n"),
        **extra_files,
    }
    name = f"Ripple bench {server_target.fresh_suffix()}"
    return {"format_version": 1, "project_id": project_id, "name": name, "files": files}


def new_project(api: Api, **extra_files: dict) -> SimpleNamespace:
    project_id, test_id = server_target.fresh_id(), str(uuid.uuid4())
    body = project_body(project_id, test_id, **extra_files)
    ok(api.call("POST", "projects", json_body=body))
    return SimpleNamespace(pid=project_id, test_id=test_id, files=body["files"])


def run_id(day: int = 26, hour: int = 8) -> str:
    return f"2026-10-{day:02d}T{hour:02d}-00-00Z_{uuid.uuid4().hex[:16]}"


def run_body(test_id: str, *, serial: str = "SN-1", day: int = 26, **fields: Any) -> dict:
    run = {
        "format_version": 2,
        "run_id": run_id(day),
        "serial": serial,
        "part_number": "PN-1",
        "board_revision": "A",
        "test": "T1",
        "test_id": test_id,
        "started": f"2026-10-{day:02d}T08:00:00Z",
        "ended": f"2026-10-{day:02d}T08:00:05Z",
        "verdict": "PASS",
        "complete": True,
        "machine_id": MACHINE,
        "draft": False,
        "phases": [],
        "files": [],
    }
    run.update(fields)
    return {key: value for key, value in run.items() if value is not None}


def run_file(name: str, data: bytes, store_id: int) -> dict:
    return {"name": name, "role": "attachment", "size": len(data), "sha256": sha(data),
            "store_id": store_id}  # fmt: skip


def report_body(store_id: int, data: bytes, **fields: Any) -> dict:
    report = {
        "format_version": 2,
        "kind": "report",
        "serial": "SN-1",
        "part_number": "PN-1",
        "board_revision": "A",
        "name": "kit-report.html",
        "sha256": sha(data),
        "size": len(data),
        "store_id": store_id,
        "machine_id": MACHINE,
        "updated_at": now(),
        "info": {"version": "1.0", "metadata": {"report": "kit-report.html"}},
    }
    report.update(fields)
    return report


def library_body(store_id: int, archive: bytes, item_id: str, **fields: Any) -> dict:
    item = {
        "format_version": 1,
        "kind": "config",
        "id": item_id,
        "version": "1.0",
        "content_sha256": sha(archive),
        "contains_code": False,
        "archive": {
            "version": f"sha256:{sha(archive)}",
            "size": len(archive),
            "store_id": store_id,
        },  # fmt: skip
    }
    item.update(fields)
    return item


def board_entry(kind: str = "lot", value: Any = "L-1", **fields: Any) -> dict:
    entry = {"entry_id": str(uuid.uuid4()), "kind": kind, "value": value, "replaces": None,
             "by": "kit", "machine_id": MACHINE, "saved_at": now()}  # fmt: skip
    entry.update(fields)
    return entry


def board_query(part_number: str = "PN-1", board_revision: str = "A") -> str:
    return (
        f"part_number={quote(part_number, safe='')}&board_revision={quote(board_revision, safe='')}"
    )


# ---- Fixtures --------------------------------------------------------------------------------


@pytest.fixture(scope="session")
def twin() -> Twin:
    return Twin()


@pytest.fixture(scope="module")
def served(tmp_path_factory):
    with server_target.serve(tmp_path_factory.mktemp("conventions"), LOCAL_TOKENS) as (base, _):
        yield base


@pytest.fixture
def api(served, twin):
    api = Api(served, twin)
    yield api
    api.close()


@pytest.fixture(scope="module")
def store(served, twin) -> int | None:
    """The server's ``api`` storage (§5.15), where parts go; None when it keeps none."""
    api = Api(served, twin)
    try:
        storages = ok(api.call("GET", "storages"))["storages"]
    finally:
        api.close()
    found = [s["store_id"] for s in storages if s["backend"] == "api"]
    return found[0] if found else None


def needs_store(store: int | None) -> int:
    if store is None:
        pytest.skip("the server lists no api storage, so no file travels as a part")
    return store


@pytest.fixture(scope="module")
@server_target.offer_aware
def held(served, twin, store) -> SimpleNamespace:
    """What the reads look at: a project with the test T1, a release of it, two complete runs of
    it on the board PN-1/A/SN-1 (the later one current); a lot entry when the server offers E15;
    with an api storage, a report (E10, core) and a library item when the server offers L3. Only
    the core calls otherwise (§6.1): every case using it runs on any server."""
    api = Api(served, twin)
    try:
        project = new_project(api)
        pid, tid = project.pid, project.test_id
        files = {
            path: f["version"] for path, f in project.files.items() if path.startswith("tests/")
        }
        release = {"format": "text", "fingerprint": "a" * 64, "content": "b" * 64, "files": files}
        ok(api.call("POST", f"projects/{pid}/releases/{tid}", json_body=release))
        first = run_body(tid, day=20)
        ok(api.call("POST", f"projects/{pid}/runs", json_body=first))
        run = run_body(tid, day=21)
        assert ok(api.call("POST", f"projects/{pid}/runs", json_body=run))["current"] is True
        held = SimpleNamespace(
            pid=pid, test_id=tid, run=run, first=first, report=None, library=None, store=store,
            base=served, lot=False,
        )  # fmt: skip
        if server_target.offers(served, "E15"):
            entries = {"entries": [board_entry()]}
            ok(api.call("POST", f"projects/{pid}/boards/SN-1/entries", query=board_query(),
                        json_body=entries))  # fmt: skip
            held.lot = True
        if store is not None:
            data = b"<html>kit report</html>"
            report = report_body(store, data)
            ok(api.call("POST", f"projects/{pid}/artifacts", json_body=report,
                        parts={f"sha256:{sha(data)}": data}))  # fmt: skip
            held.report = report
        if store is not None and server_target.offers(served, "L3"):
            archive = b"PK kit library archive " + uuid.uuid4().bytes
            item = library_body(store, archive, f"kit-held-{server_target.fresh_suffix()}")
            ok(api.call("POST", "library/config", json_body=item,
                        parts={item["archive"]["version"]: archive}))  # fmt: skip
            held.library = item
        return held
    finally:
        api.close()


def offered_calls(held: SimpleNamespace, calls: list[tuple]) -> list[tuple]:
    """Of *calls* (each led by its id), those of the core and those the server offers (§6.1)."""
    listed = server_target.offered(held.base)
    return [call for call in calls if call[0] not in OPTIONAL or call[0] in listed]


def read_calls(held: SimpleNamespace) -> list[tuple[str, str, str]]:
    """``(name, path, query)`` of a read of every kind the server takes, each naming something
    *held* holds: the core reads always, an optional one when the server offers it."""
    pid, tid, rid = held.pid, held.test_id, held.run["run_id"]
    calls = [
        ("info", "info", ""),
        ("P1", "projects", ""),
        ("P2", f"projects/{pid}", ""),
        ("P4", f"projects/{pid}/tests", ""),
        ("P7", f"projects/{pid}/tests/T1", ""),
        ("P8", f"projects/{pid}/releases/{tid}", ""),
        ("P8b", f"projects/{pid}/releases/{tid}/1", ""),
        ("E2", "runs", f"project_id={pid}"),
        ("E3", f"projects/{pid}/runs/{rid}", ""),
        ("E4", f"projects/{pid}/boards", ""),
        ("E5", f"projects/{pid}/boards/SN-1", board_query()),
        ("E8", f"projects/{pid}/artifacts", ""),
        ("E14", "benches", ""),
        ("E16", f"projects/{pid}/operators", ""),
        ("E17", f"projects/{pid}/readings", "test=T1"),
        ("L1", "library", ""),
        ("S1", "storages", ""),
    ]
    if held.report is not None:
        name = seg(held.report["name"])
        calls.append(
            ("E9", f"projects/{pid}/artifacts/report/{name}", "serial=SN-1&" + board_query())
        )
    if held.library is not None:
        item = held.library
        calls.append(("L2", f"library/config/{seg(item['id'])}/{seg(item['version'])}", ""))
    return offered_calls(held, calls)


# ---- §9 The twin -----------------------------------------------------------------------------


def test_the_twin_is_read_whole_and_every_answer_is_checked_against_it(twin):
    """The twin parses in full, every schema keyword in it is one the validator applies (or an
    annotation), and every call has its answers. The answers themselves are checked as each
    check of this file receives them (``Api.check_twin``)."""
    assert twin.check_keywords() == []
    for template, item in twin.paths.items():
        for method in ("get", "post"):
            if method in item:
                responses = item[method]["responses"]
                assert "default" in responses or any(str(s).startswith("2") for s in responses), (
                    template,
                    method,
                )


@pytest.mark.offer_aware
@pytest.mark.rule(
    "9.twin-schemas", "2.7.time-format", "3.1.no-absolute-url", "3.2.x-request-id", "3.2.date",
)
def test_every_read_answers_what_the_twin_says(api, held):
    """A read of every kind, each answer held to the rules every answer follows (``Api.check``):
    the headers, no absolute URL, no ``null``, valid against the twin."""
    for name, path, query in read_calls(held):
        reply = api.call("GET", path, query=query)
        assert reply.status == 200, f"{name}: {reply}"


# ---- §2.1 Two methods; §4.3 405/501 ---------------------------------------------------------


@pytest.mark.rule("2.1.two-methods", "4.3.405-501")
@pytest.mark.parametrize("method", ["PUT", "DELETE", "PATCH"])
@pytest.mark.parametrize("path", ["projects", "info"])
def test_another_method_is_unsupported(api, method, path):
    reply = api.call(method, path, data=b"")
    assert reply.status in (405, 501), reply
    refused(reply, reply.status, "invalid", "unsupported")


@pytest.mark.rule("2.1.two-methods")
def test_head_is_refused_with_the_servers_signature(api):
    reply = api.call("HEAD", "projects")
    assert reply.status in (405, 501), reply


@pytest.mark.rule("2.1.two-methods", "4.3.405-501")
@pytest.mark.parametrize(
    "method, path",
    [
        ("GET", "no-such-call"),
        ("POST", "projects/{pid}/no-such-call"),
        ("GET", "projects/{pid}/runs"),
    ],
    ids=["unknown-get", "unknown-post", "get-on-a-post-call"],
)
def test_a_call_the_server_does_not_take_is_unsupported_never_not_found(api, held, method, path):
    reply = api.call(method, path.format(pid=held.pid), json_body={} if method == "POST" else None)
    assert reply.status in (405, 501), reply
    refused(reply, reply.status, "invalid", "unsupported")


# ---- §2.2 Unknown fields, answer fields, null ------------------------------------------------


@pytest.mark.offer_aware
@pytest.mark.rule("2.2.unknown-fields-kept")
def test_a_field_the_server_does_not_know_is_kept(api, store):
    """In a project, a file, a run, its phases and measurements, a draft, an annotation entry
    (E7), a report (E10, E9); a board entry (E15) when the server offers it."""
    note = {"x_kit_note": "kept as sent", "x_kit_nested": {"b": [1, "two"], "a": {"c": True}}}
    pid, tid = server_target.fresh_id(), str(uuid.uuid4())
    body = project_body(pid, tid) | note
    body["files"]["tests/T1/procedure_text.md"] |= note
    ok(api.call("POST", "projects", json_body=body))
    project = ok(api.call("GET", f"projects/{pid}"))
    assert {k: project.get(k) for k in note} == note, project
    file = project["files"]["tests/T1/procedure_text.md"]
    assert {k: file.get(k) for k in note} == note, file

    measurement = {"name": "V", "ord": 1, "value_num": 1.5} | note
    phase = {"ord": 1, "name": "Step 1", "attempt": 1, "measurements": [measurement]} | note
    run = run_body(tid, phases=[phase]) | note
    ok(api.call("POST", f"projects/{pid}/runs", json_body=run))
    entry = {"entry_id": str(uuid.uuid4()), "kind": "comment", "key": "test", "text": "hi",
             "replaces": None, "by": "kit", "machine_id": MACHINE, "saved_at": now()} | note  # fmt: skip
    ok(api.call("POST", f"projects/{pid}/runs/{run['run_id']}/annotation",
                json_body={"entries": [entry]}))  # fmt: skip
    got = ok(api.call("GET", f"projects/{pid}/runs/{run['run_id']}"))
    assert {k: got.get(k) for k in note} == note, got
    assert {k: got["phases"][0].get(k) for k in note} == note, got["phases"]
    assert {k: got["phases"][0]["measurements"][0].get(k) for k in note} == note
    [read_entry] = got["annotation"]["entries"]
    assert {k: read_entry.get(k) for k in note} == note, read_entry

    if api.offers("E15"):
        lot = board_entry() | note
        ok(api.call("POST", f"projects/{pid}/boards/SN-1/entries", query=board_query(),
                    json_body={"entries": [lot]}))  # fmt: skip
        board = ok(api.call("GET", f"projects/{pid}/boards/SN-1", query=board_query()))["board"]
        [read_lot] = board["entries"]
        assert {k: read_lot.get(k) for k in note} == note, read_lot

    text = "# T1, changed\n"
    change = {"path": "tests/T1/procedure_text.md",
              "replaces": body["files"]["tests/T1/procedure_text.md"]["version"], "text": text}  # fmt: skip
    draft = {"test": "T1", "test_id": tid, "fingerprint": "c" * 64, "content": "d" * 64} | note
    result = ok(api.call("POST", f"projects/{pid}/changes",
                         json_body={"changes": [change], "drafts": [draft]}))  # fmt: skip
    assert [r["status"] for r in result["results"]] == ["saved"], result
    test = ok(api.call("GET", f"projects/{pid}/tests/T1"))
    assert {k: test["draft"].get(k) for k in note} == note, test["draft"]

    if store is not None:
        data = b"<html>unknown fields</html>"
        report = report_body(store, data, name="kit-unknown.html") | note
        ok(api.call("POST", f"projects/{pid}/artifacts", json_body=report,
                    parts={f"sha256:{sha(data)}": data}))  # fmt: skip
        query = "serial=SN-1&" + board_query()
        got = ok(api.call("GET", f"projects/{pid}/artifacts/report/kit-unknown.html", query=query))
        assert {k: got.get(k) for k in note} == note, got


@pytest.mark.rule("2.2.unknown-null-absent")
def test_an_unknown_field_sent_null_reads_back_absent(api):
    """``null`` in a request is "not known" (§2.2), an unknown field's too: not kept, it reads
    back absent. Inside an unknown field's value, everything is kept, ``null`` included."""
    deep = {"x_kit_deep": {"a": None, "b": [None, 1]}}
    pid, tid = server_target.fresh_id(), str(uuid.uuid4())
    body = project_body(pid, tid) | {"x_kit_gone": None} | deep
    body["files"]["tests/T1/procedure_text.md"] |= {"x_kit_gone": None}
    ok(api.call("POST", "projects", json_body=body))
    project = ok(api.call("GET", f"projects/{pid}"))
    assert "x_kit_gone" not in project and project.get("x_kit_deep") == deep["x_kit_deep"]
    assert "x_kit_gone" not in project["files"]["tests/T1/procedure_text.md"], project["files"]
    run = run_body(tid) | {"x_kit_gone": None} | deep
    ok(api.call("POST", f"projects/{pid}/runs", json_body=run))
    got = ok(api.call("GET", f"projects/{pid}/runs/{run['run_id']}"))
    assert "x_kit_gone" not in got and got.get("x_kit_deep") == deep["x_kit_deep"], got


@pytest.mark.offer_aware
@pytest.mark.rule("2.2.answer-fields-ignored", "2.7.time-format", "3.7.G3-one-clock")
def test_answer_fields_a_client_sends_are_ignored_and_every_server_time_is_its_clock(api):
    """Fields the server sets (§5, *answer*) sent by a bench (no import) are ignored; the times
    the server sets are UTC seconds with ``Z``, within 3 s of the answer's ``Date`` (one clock,
    §3.7 G3; seconds truncated). A run, its board and a release; a board entry (E15) and a
    delete (E11) when the server offers them."""
    project = new_project(api)
    pid, tid = project.pid, project.test_id
    old = "2001-01-01T00:00:00Z"

    def close_to_date(value: str, reply: Reply, what: str) -> None:
        assert TIME.fullmatch(value), f"{what}: {value!r} is not UTC seconds with Z"
        stamped = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        date = parsedate_to_datetime(reply.headers["Date"])
        assert abs((date - stamped).total_seconds()) <= 3, f"{what} {value} vs Date {date}"

    run = run_body(tid, pushed_at=old, current=False, deleted_at=old, deleted_by="someone")
    pushed = api.call("POST", f"projects/{pid}/runs", json_body=run)
    assert ok(pushed)["current"] is True, pushed
    read = api.call("GET", f"projects/{pid}/runs/{run['run_id']}")
    got = ok(read)
    assert got["current"] is True and "deleted_at" not in got and "deleted_by" not in got, got
    close_to_date(got["pushed_at"], read, "pushed_at")

    if api.offers("E15"):
        entry = board_entry(received_at=old, current=False)
        ok(api.call("POST", f"projects/{pid}/boards/SN-1/entries", query=board_query(),
                    json_body={"entries": [entry]}))  # fmt: skip
    read = api.call("GET", f"projects/{pid}/boards/SN-1", query=board_query())
    board = ok(read)["board"]
    if api.offers("E15"):
        [stored] = board["entries"]
        assert stored["current"] is True, stored
        close_to_date(stored["received_at"], read, "received_at")
    close_to_date(board["created_at"], read, "created_at")

    files = {path: f["version"] for path, f in project.files.items() if path.startswith("tests/")}
    release = {"format": "text", "fingerprint": "a" * 64, "content": "b" * 64, "files": files,
               "version": 7, "released_at": old}  # fmt: skip
    made = api.call("POST", f"projects/{pid}/releases/{tid}", json_body=release)
    assert ok(made)["version"] == 1, made
    close_to_date(made.json["released_at"], made, "released_at")

    if not api.offers("E11"):
        return
    deleted = api.call(
        "POST",
        f"projects/{pid}/runs/{run['run_id']}/delete",
        role="manager",
        json_body={"machine_id": MACHINE, "reason": "Kit check", "deleted_at": old},
    )
    run_now = ok(deleted)["run"]
    close_to_date(run_now["deleted_at"], deleted, "deleted_at")
    assert run_now["deleted_by"] != "someone", run_now


@pytest.mark.rule("2.7.time-format", "3.7.G3-one-clock")
def test_a_library_item_names_the_login_and_the_servers_time(api, store):
    store = needs_store(store)
    archive = b"PK kit " + uuid.uuid4().bytes
    item = library_body(store, archive, f"kit-pub-{server_target.fresh_suffix()}",
                        published_by="someone-else", published_at="2001-01-01T00:00:00Z")  # fmt: skip
    reply = api.call(
        "POST", "library/config", json_body=item, parts={item["archive"]["version"]: archive}
    )
    got = ok(reply)["item"]
    me = ok(api.call("GET", "info"))["user"]["name"]
    assert got["published_by"] == me, got
    stamped = datetime.strptime(got["published_at"], "%Y-%m-%dT%H:%M:%SZ")
    date = parsedate_to_datetime(reply.headers["Date"])
    assert abs((date - stamped.replace(tzinfo=timezone.utc)).total_seconds()) <= 3, got


@pytest.mark.rule("2.2.no-null-in-answers")
def test_a_field_with_no_value_is_left_out_not_null(api):
    """A run on the "no part number" board and its board read back with ``part_number``,
    ``board_revision``, ``lot`` and the like absent, never ``null`` (every answer is also
    walked for ``null`` by ``Api.check``)."""
    project = new_project(api)
    run = run_body(project.test_id, part_number=None, board_revision=None, serial="SN-bare")
    pushed = ok(api.call("POST", f"projects/{project.pid}/runs", json_body=run))
    assert "part_number" not in pushed and "board_revision" not in pushed, pushed
    [board] = ok(api.call("GET", f"projects/{project.pid}/boards"))["boards"]
    assert set(board) & {"part_number", "board_revision", "lot"} == set(), board
    results = ok(
        api.call("GET", f"projects/{project.pid}/boards/SN-bare", query=board_query("", ""))
    )
    assert results["tests"][0]["pointer"]["run_id"] == run["run_id"], results


# ---- §2.4 Names ------------------------------------------------------------------------------


@pytest.mark.offer_aware
@pytest.mark.rule("2.4.nfc")
def test_a_name_sent_in_another_unicode_form_is_stored_in_nfc(api, store):
    """A test's name, a board's serial (a run's), and a report's name (E10, E8)."""
    nfc_name = f"Sé-{server_target.fresh_suffix()}"
    nfd_name = unicodedata.normalize("NFD", nfc_name)
    assert nfd_name != nfc_name
    pid, tid = server_target.fresh_id(), str(uuid.uuid4())
    test_nfc = "Tést"
    body = project_body(
        pid,
        tid,
        **{
            f"tests/{unicodedata.normalize('NFD', test_nfc)}/test.json": text_file(
                id_json(str(uuid.uuid4()))
            )
        },
    )
    ok(api.call("POST", "projects", json_body=body))
    test = ok(
        api.call("GET", f"projects/{pid}/tests/{seg(unicodedata.normalize('NFD', test_nfc))}")
    )
    assert test["test"] == test_nfc, test

    ok(api.call("POST", f"projects/{pid}/runs", json_body=run_body(tid, serial=nfd_name)))
    boards = ok(api.call("GET", f"projects/{pid}/boards"))["boards"]
    assert [b["serial"] for b in boards] == [nfc_name], boards
    board = ok(api.call("GET", f"projects/{pid}/boards/{seg(nfc_name)}", query=board_query()))
    board = server_target.kit_tests(board)
    assert board["board"]["serial"] == nfc_name and len(board["tests"]) == 1, board

    if store is not None:
        data = b"<html>nfc</html>"
        name = unicodedata.normalize("NFD", "Rapport-é.html")
        report = report_body(store, data, name=name)
        ok(api.call("POST", f"projects/{pid}/artifacts", json_body=report,
                    parts={f"sha256:{sha(data)}": data}))  # fmt: skip
        listed = ok(api.call("GET", f"projects/{pid}/artifacts"))["artifacts"]
        assert [a["name"] for a in listed] == ["Rapport-é.html"], listed


@pytest.mark.offer_aware
@pytest.mark.rule("2.4.case-first-spelling")
def test_a_name_differing_only_by_case_answers_its_first_spelling(api, store):
    project = new_project(api)
    suffix = server_target.fresh_suffix()
    first = run_body(
        project.test_id, serial=f"sn-AbC-{suffix}", part_number="pn-Q", board_revision="rEv"
    )
    ok(api.call("POST", f"projects/{project.pid}/runs", json_body=first))
    second = run_body(project.test_id, serial=f"SN-abc-{suffix}", part_number="PN-q",
                      board_revision="REV", day=27)  # fmt: skip
    pushed = ok(api.call("POST", f"projects/{project.pid}/runs", json_body=second))
    assert (pushed["serial"], pushed["part_number"], pushed["board_revision"]) == (
        f"sn-AbC-{suffix}", "pn-Q", "rEv"), pushed  # fmt: skip
    boards = ok(api.call("GET", f"projects/{project.pid}/boards"))["boards"]
    assert [b["serial"] for b in boards] == [f"sn-AbC-{suffix}"], boards
    if store is not None:
        for n, name in enumerate(["Case-Report.html", "CASE-report.HTML"]):
            data = f"<html>{n}</html>".encode()
            report = report_body(store, data, name=name, serial=first["serial"],
                                 part_number="pn-Q", board_revision="rEv")  # fmt: skip
            answer = ok(api.call("POST", f"projects/{project.pid}/artifacts", json_body=report,
                                 parts={f"sha256:{sha(data)}": data}))  # fmt: skip
            assert answer["artifact"]["name"] == "Case-Report.html", answer
            assert answer["artifact"]["generation"] == n + 1, answer


@pytest.mark.offer_aware
@pytest.mark.rule("2.4.exact-exceptions")
def test_a_board_fields_name_and_a_never_travel_name_compare_exactly(api):
    """A never-travel name; a board field's name when the server offers E15."""
    project = new_project(api, **{"tests/T1/__PyCache__/kit.md": text_file("kept\n")})
    files = ok(api.call("GET", f"projects/{project.pid}"))["files"]
    assert "tests/T1/__PyCache__/kit.md" in files, sorted(files)
    if not api.offers("E15"):
        return
    entries = [
        board_entry("field", "ACME", name="Customer"),
        board_entry("field", "other", name="customer"),
    ]
    result = ok(api.call("POST", f"projects/{project.pid}/boards/SN-1/entries", query=board_query(),
                         json_body={"entries": entries}))  # fmt: skip
    assert [r["status"] for r in result["results"]] == ["saved", "saved"], result
    board = ok(api.call("GET", f"projects/{project.pid}/boards/SN-1", query=board_query()))["board"]
    assert board["details"] == {"Customer": "ACME", "customer": "other"}, board


# ---- §2.5 The test id ------------------------------------------------------------------------


@pytest.mark.rule("2.5.malformed-test-id-400-first", "4.3.400-invalid")
@pytest.mark.parametrize(
    "bad", ["T1", "3F9C2A1E-7B4D-4E8A-9C61-2D5B8E0F4A17"], ids=["name", "upper"]
)
@pytest.mark.parametrize("call", ["P8", "P8b", "P8b-read", "P10", "E6", "E2"])
def test_a_malformed_test_id_is_400_before_the_project_is_looked_at(api, call, bad):
    """In a project the server never saw, so a server looking at the project first answers
    ``404``."""
    pid, test = server_target.fresh_id(), seg(bad)
    release = {"format": "text", "fingerprint": "a" * 64, "content": "b" * 64, "files": {}}
    pointer = {"run_id": None, "replaces": None, "machine_id": MACHINE}
    method, path, query, body = {
        "P8": ("GET", f"projects/{pid}/releases/{test}", "", None),
        "P8b": ("GET", f"projects/{pid}/releases/{test}/1", "", None),
        "P8b-read": ("POST", f"projects/{pid}/releases/{test}/1/read", "", {"have": []}),
        "P10": ("POST", f"projects/{pid}/releases/{test}", "", release),
        "E6": ("POST", f"projects/{pid}/boards/SN-1/tests/{test}/current", board_query(), pointer),
        "E2": ("GET", "runs", f"project_id={pid}&test_id={test}", None),
    }[call]
    refused(api.call(method, path, query=query, json_body=body), 400, "invalid", "bad_request")


# ---- §2.6 The board --------------------------------------------------------------------------


@pytest.mark.rule("2.6.empty-value-400", "4.3.400-invalid")
@pytest.mark.parametrize("name", ["serial", "part_number", "board_revision"])
def test_an_empty_board_name_is_refused(api, name):
    project_id, test_id = server_target.fresh_id(), str(uuid.uuid4())
    run = run_body(test_id, **{name: ""})
    refused(
        api.call("POST", f"projects/{project_id}/runs", json_body=run),
        400,
        "invalid",
        "bad_request",
    )
    refused(
        api.call("GET", f"projects/{project_id}/runs/{run['run_id']}"),
        404,
        "not_found",
        "not_found",
    )


@pytest.mark.rule("2.6.blanks-400", "4.3.400-invalid")
@pytest.mark.parametrize(
    "case",
    ["E1-serial", "E1-part_number", "E1-board_revision", "E15-serial", "E15-part_number", "E5-serial",
     "E5-board_revision", "E6-serial", "E9-serial"],
)  # fmt: skip
def test_a_board_name_with_a_blank_around_it_is_refused(api, held, case):
    pid, tid = held.pid, held.test_id
    entries = {"entries": [board_entry()]}
    pointer = {"run_id": held.run["run_id"], "replaces": None, "machine_id": MACHINE}
    method, path, query, body = {
        "E1-serial": ("POST", f"projects/{pid}/runs", "", run_body(tid, serial=" SN-1")),
        "E1-part_number": ("POST", f"projects/{pid}/runs", "", run_body(tid, part_number="PN-1 ")),
        "E1-board_revision": ("POST", f"projects/{pid}/runs", "", run_body(tid, board_revision=" A")),
        "E15-serial": ("POST", f"projects/{pid}/boards/{seg('SN-1 ')}/entries", board_query(), entries),
        "E15-part_number": ("POST", f"projects/{pid}/boards/SN-1/entries", board_query(" PN-1"), entries),
        "E5-serial": ("GET", f"projects/{pid}/boards/{seg(' SN-1')}", board_query(), None),
        "E5-board_revision": ("GET", f"projects/{pid}/boards/SN-1", board_query("PN-1", "A "), None),
        "E6-serial": ("POST", f"projects/{pid}/boards/{seg('SN-1 ')}/tests/{tid}/current",
                      board_query(), pointer),
        "E9-serial": ("GET", f"projects/{pid}/artifacts/report/kit-report.html",
                      "serial=%20SN-1&" + board_query(), None),
    }[case]  # fmt: skip
    refused(api.call(method, path, query=query, json_body=body), 400, "invalid", "bad_request")


@pytest.mark.offer_aware
@pytest.mark.rule("2.6.filter-matches-nothing")
def test_a_filter_with_a_blank_around_it_matches_nothing_and_is_not_refused(api, held):
    """E4's and E8's filters; E2's when the server offers it."""
    pid = held.pid
    boards = ok(api.call("GET", f"projects/{pid}/boards", query="part_number=PN-1"))["boards"]
    assert [b["serial"] for b in boards] == ["SN-1"], boards
    assert (
        ok(api.call("GET", f"projects/{pid}/boards", query="part_number=%20PN-1"))["boards"] == []
    )
    assert (
        ok(api.call("GET", f"projects/{pid}/boards", query="board_revision=A%20"))["boards"] == []
    )
    if api.offers("E2"):
        found = ok(api.call("GET", "runs", query=f"project_id={pid}&serial=SN-1"))
        found = server_target.kit_runs(found)["runs"]  # less the kit's own board registration
        assert len(found) == 2, found
        none = ok(api.call("GET", "runs", query=f"project_id={pid}&serial=SN-1%20"))["runs"]
        assert none == [], none
        query = f"project_id={pid}&part_number=%20PN-1"
        assert ok(api.call("GET", "runs", query=query))["runs"] == []
    listed = ok(api.call("GET", f"projects/{pid}/artifacts", query="serial=%20SN-1"))["artifacts"]
    assert listed == [], listed


# ---- §2.8 Digests, format_version, own fields ------------------------------------------------


@pytest.mark.rule("2.8.format-version-400", "4.4.details", "4.3.400-invalid")
@pytest.mark.parametrize("case", ["E1-3", "E1-0", "P11-2", "E10-3", "L3-2"])
def test_a_format_version_the_object_does_not_have_is_refused_with_the_known_one(api, store, case):
    pid, tid = server_target.fresh_id(), str(uuid.uuid4())
    what, sent = case.split("-")
    sent = int(sent)
    parts = None
    if what == "E1":
        path, body, known = f"projects/{pid}/runs", run_body(tid, format_version=sent), 2
    elif what == "P11":
        path, body, known = "projects", project_body(pid, tid) | {"format_version": sent}, 1
    elif what == "E10":
        data = b"<html>fv</html>"
        path, known = f"projects/{pid}/artifacts", 2
        body, parts = (
            report_body(needs_store(store), data, format_version=sent),
            {f"sha256:{sha(data)}": data},
        )
    else:
        archive = b"PK fv " + uuid.uuid4().bytes
        path, known = "library/config", 1
        body = library_body(needs_store(store), archive, f"kit-fv-{server_target.fresh_suffix()}",
                            format_version=sent)  # fmt: skip
        parts = {body["archive"]["version"]: archive}
    error = refused(
        api.call("POST", path, json_body=body, parts=parts), 400, "invalid", "bad_request"
    )
    assert error.get("details", {}).get("format_version") == sent, error
    assert error["details"].get("known") == known, error


@pytest.mark.rule("2.8.own-fields-strings", "4.3.400-invalid")
@pytest.mark.parametrize("case", ["run-extra", "report-fields", "board-field"])
def test_a_projects_own_field_that_is_not_a_string_is_refused(api, held, store, case):
    pid = held.pid
    parts = None
    if case == "run-extra":
        path, query = f"projects/{pid}/runs", ""
        body = run_body(held.test_id, extra={"Customer": 5})
    elif case == "report-fields":
        data = b"<html>fields</html>"
        path, query = f"projects/{pid}/artifacts", ""
        body = report_body(needs_store(store), data, name="kit-fields.html", fields={"Order": True})
        parts = {f"sha256:{sha(data)}": data}
    else:
        path, query = f"projects/{pid}/boards/SN-own/entries", board_query()
        body = {"entries": [board_entry("field", 5, name="Customer")]}
    refused(
        api.call("POST", path, query=query, json_body=body, parts=parts),
        400,
        "invalid",
        "bad_request",
    )


# ---- §2.9 Text or reference; §2.10 paths -----------------------------------------------------


def changes_of(api: Api, pid: str, changes: list[dict], parts: dict | None = None) -> Reply:
    return api.call("POST", f"projects/{pid}/changes", json_body={"changes": changes}, parts=parts)


def check_nothing_written(api: Api, pid: str, *paths: str) -> None:
    files = ok(api.call("GET", f"projects/{pid}"))["files"]
    assert not set(paths) & set(files), f"written although the request was refused: {sorted(files)}"


@pytest.mark.rule("2.9.wrong-form-400", "4.3.400-invalid")
def test_a_file_in_the_wrong_form_refuses_the_whole_request(api, store):
    project = new_project(api)
    good = {"path": "tests/T1/kept.md", "replaces": None} | text_file("never written\n")
    del good["version"], good["size"]
    good |= text_file("never written\n")
    outside = {"path": "documents/readme.md", "replaces": None} | text_file("text outside\n")
    refused(changes_of(api, project.pid, [good, outside]), 400, "invalid", "bad_request")
    check_nothing_written(api, project.pid, "tests/T1/kept.md", "documents/readme.md")

    pid, tid = server_target.fresh_id(), str(uuid.uuid4())
    body = project_body(pid, tid, **{"documents/readme.md": text_file("text outside\n")})
    refused(api.call("POST", "projects", json_body=body), 400, "invalid", "bad_request")
    refused(api.call("GET", f"projects/{pid}"), 404, "not_found", "not_found")

    if store is not None:
        data = b"plain UTF-8 text\n"
        reference = {"path": "tests/T1/data.txt", "replaces": None, "version": f"sha256:{sha(data)}",
                     "size": len(data), "store_id": store}  # fmt: skip
        reply = changes_of(api, project.pid, [good, reference], {f"sha256:{sha(data)}": data})
        refused(reply, 400, "invalid", "bad_request")
        check_nothing_written(api, project.pid, "tests/T1/kept.md", "tests/T1/data.txt")


@pytest.mark.rule("2.9.text-size-version-400", "4.3.400-invalid")
@pytest.mark.parametrize("wrong", ["version", "size"])
def test_a_text_whose_bytes_are_not_its_version_and_size_is_refused(api, held, wrong):
    path = f"tests/T1/kit-{server_target.fresh_suffix()}.md"
    change = {"path": path, "replaces": None} | text_file("some text\n")
    if wrong == "version":
        change["version"] = f"sha256:{sha(b'other bytes')}"
    else:
        change["size"] += 1
    refused(changes_of(api, held.pid, [change]), 400, "invalid", "bad_request")
    check_nothing_written(api, held.pid, path)


@pytest.mark.rule("2.10.move-case-respells")
def test_a_move_differing_only_by_case_respells_the_folder(api):
    project = new_project(api)
    folder = {name: f["version"] for name, f in
              ((p.split("/", 2)[2], f) for p, f in project.files.items() if p.startswith("tests/T1/"))}  # fmt: skip
    move = {"path": "tests/T1", "replaces_folder": folder, "move_to": "tests/t1"}
    result = ok(changes_of(api, project.pid, [move]))
    assert [r["status"] for r in result["results"]] == ["saved"], result
    files = ok(api.call("GET", f"projects/{project.pid}"))["files"]
    assert sorted(p for p in files if p.lower().startswith("tests/t1/")) == [
        "tests/t1/procedure_text.md", "tests/t1/test.json"], sorted(files)  # fmt: skip


BAD_PATHS = {
    "empty": "tests/T1//x.md",
    "dot": "tests/T1/./x.md",
    "dotdot": "tests/T1/../x.md",
    "backslash": "tests/T1/a\\b.md",
    "lt": "tests/T1/a<b.md",
    "gt": "tests/T1/a>b.md",
    "colon": "tests/T1/a:b.md",
    "quote": 'tests/T1/a"b.md',
    "pipe": "tests/T1/a|b.md",
    "question": "tests/T1/a?b.md",
    "star": "tests/T1/a*b.md",
    "control": "tests/T1/a\x01b.md",
    "trailing-dot": "tests/T1/x.md.",
    "trailing-blank-name": "tests/T1/x /y.md",
    "device": "tests/T1/CON",
    "device-extension": "tests/T1/lpt9.md",
    "long": "tests/T1/" + "x" * 253 + ".md",
    "path-leading-blank": " tests/T1/x.md",
    "path-trailing-blank": "tests/T1/x.md ",
}


@pytest.mark.rule("2.10.path-names-400", "4.3.400-invalid")
@pytest.mark.parametrize("bad", sorted(BAD_PATHS))
def test_a_path_no_pc_can_hold_refuses_the_whole_request(api, held, bad):
    good_path = f"tests/T1/kit-{server_target.fresh_suffix()}.md"
    good = {"path": good_path, "replaces": None} | text_file("never written\n")
    wrong = {"path": BAD_PATHS[bad], "replaces": None} | text_file("bad path\n")
    refused(changes_of(api, held.pid, [good, wrong]), 400, "invalid", "bad_request")
    check_nothing_written(api, held.pid, good_path)


@pytest.mark.rule("2.10.path-names-400", "4.3.400-invalid")
def test_a_new_project_with_a_path_no_pc_can_hold_is_refused(api):
    pid, tid = server_target.fresh_id(), str(uuid.uuid4())
    body = project_body(pid, tid, **{"tests/T1/AUX.txt": text_file("device\n")})
    refused(api.call("POST", "projects", json_body=body), 400, "invalid", "bad_request")
    refused(api.call("GET", f"projects/{pid}"), 404, "not_found", "not_found")


# ---- §2.11 Query parameters ------------------------------------------------------------------


@pytest.mark.offer_aware
@pytest.mark.rule("2.11.unknown-query-param", "4.3.400-invalid")
def test_a_query_parameter_a_call_does_not_define_is_refused(api, held):
    for name, path, query in read_calls(held):
        reply = api.call("GET", path, query="&".join(q for q in (query, "kit_unknown=1") if q))
        code = "unsupported_filter" if name in ("E2", "E8") else "bad_request"
        assert reply.status == 400, f"{name}: {reply}"
        refused(reply, 400, "invalid", code)
    read = api.call(
        "POST", f"projects/{held.pid}/read", query="kit_unknown=1", json_body={"have": []}
    )
    refused(read, 400, "invalid", "bad_request")
    path = f"tests/T1/kit-{server_target.fresh_suffix()}.md"
    change = {"path": path, "replaces": None} | text_file("never written\n")
    reply = api.call("POST", f"projects/{held.pid}/changes", query="kit_unknown=1",
                     json_body={"changes": [change]})  # fmt: skip
    refused(reply, 400, "invalid", "bad_request")
    check_nothing_written(api, held.pid, path)


# ---- §2.12 Compression and the replay digest -------------------------------------------------


@pytest.mark.rule("2.12.no-gzip-multipart-answer")
def test_a_multipart_answer_is_never_gzipped_whole(api, held, store):
    store = needs_store(store)
    data = b"answer part " + uuid.uuid4().bytes
    run = run_body(held.test_id, serial="SN-mx", files=[run_file("b.bin", data, store)])
    ok(
        api.call(
            "POST", f"projects/{held.pid}/runs", json_body=run, parts={f"sha256:{sha(data)}": data}
        )
    )
    got = api.call("GET", f"projects/{held.pid}/runs/{run['run_id']}",
                   headers={"Accept-Encoding": "gzip"})  # fmt: skip
    assert got.headers.get("Content-Type", "").startswith("multipart/mixed"), got
    assert got.headers.get("Content-Encoding", "").lower() != "gzip", got


@pytest.mark.rule("2.12.plain-json-part")
def test_no_part_of_a_multipart_answer_carries_a_content_encoding(api, held, store):
    """The ``json`` part of a multipart answer is plain JSON and a file part its bytes, even
    for a large JSON and a client accepting gzip (:func:`parse_mixed` checks every part)."""
    store = needs_store(store)
    data = b"plain part " + uuid.uuid4().bytes
    files = [run_file(f"c{n:03}.bin", data, store) for n in range(40)]
    run = run_body(held.test_id, serial="SN-plain", files=files)
    ok(api.call("POST", f"projects/{held.pid}/runs", json_body=run,
                parts={f"sha256:{sha(data)}": data}))  # fmt: skip
    got = api.call("GET", f"projects/{held.pid}/runs/{run['run_id']}",
                   headers={"Accept-Encoding": "gzip"})  # fmt: skip
    assert got.headers.get("Content-Type", "").startswith("multipart/mixed"), got
    assert ok(got)["run_id"] == run["run_id"] and got.parts == {f"sha256:{sha(data)}": data}


@pytest.mark.rule("2.12.digest-decoded", "4.3.409-run-conflict", "E1.replay")
def test_a_replay_is_the_same_json_whatever_its_encoding(api, held):
    """The run's JSON sent gzipped twice with other gzip headers, plain, and as the ``json``
    part of a multipart body: one run, the first ``saved``, the others ``already``.
    One byte more is another JSON: ``409 run_conflict``, kind ``corrupt``, the run unchanged."""
    run = run_body(held.test_id, serial="SN-replay")
    data = json.dumps(run).encode()
    path = f"projects/{held.pid}/runs"
    first = gzip.compress(data, compresslevel=9, mtime=1)
    second = gzip.compress(data, compresslevel=1, mtime=2)
    assert first != second
    json_type = {"Content-Type": "application/json", "Content-Encoding": "gzip"}
    assert ok(api.call("POST", path, data=first, headers=json_type))["status"] == "saved"
    assert ok(api.call("POST", path, data=second, headers=json_type))["status"] == "already"
    assert ok(api.call("POST", path, data=data, headers={"Content-Type": "application/json"}))[
        "status"] == "already"  # fmt: skip
    multipart = api.call("POST", path, json_body=run, parts={})
    assert ok(multipart)["status"] == "already", multipart
    other = data + b" "
    reply = api.call("POST", path, data=other, headers={"Content-Type": "application/json"})
    refused(reply, 409, "corrupt", "run_conflict")
    changed = dict(run, verdict="FAIL")
    refused(api.call("POST", path, json_body=changed), 409, "corrupt", "run_conflict")
    got = ok(api.call("GET", f"{path}/{run['run_id']}"))
    assert got["verdict"] == "PASS", got


# ---- §2.13 The order of checks ----------------------------------------------------------------


def too_large_head(api: Api, path: str, size: int, content_type: str, token: str | None,
                   expect: bool) -> str:  # fmt: skip
    """A POST's head announcing *size* bytes, with *token* (none: no ``Authorization``)."""
    return (
        f"POST {api.prefix}{path} HTTP/1.1\r\nHost: {api.netloc}\r\n"
        + (f"Authorization: Bearer {token}\r\n" if token else "")
        + f"Content-Type: {content_type}\r\nContent-Length: {size}\r\n"
        + ("Expect: 100-continue\r\n" if expect else "")
        + "Connection: close\r\n\r\n"
    )


@pytest.mark.rule("2.13.too-large-first")
@pytest.mark.parametrize("expect", [True, False], ids=["expect-100", "no-expect"])
def test_a_body_over_its_limit_is_413_before_anything_else_is_looked_at(api, expect):
    """§2.13 item 2: the size, from the headers, before the token and before what the path
    names: a project never seen (P9, P10), a storage the server does not hold (B1), no token."""
    limits = ok(api.call("GET", "info"))["limits"]
    storages = ok(api.call("GET", "storages"))["storages"]
    unknown_store = max(s["store_id"] for s in storages) + 1_000_000
    pid, tid = server_target.fresh_id(), str(uuid.uuid4())
    bench = server_target.token("bench")
    json_type, octets = "application/json", "application/octet-stream"
    cases = [
        (f"projects/{pid}/changes", "max_upload_bytes", json_type, bench),
        (f"projects/{pid}/releases/{tid}", "max_upload_bytes", json_type, bench),
        (f"storages/{unknown_store}/blobs/{pid}/{'2' * 64}", "max_file_bytes", octets, bench),
        (f"projects/{pid}/runs", "max_upload_bytes", json_type, None),
    ]
    for path, name, content_type, token in cases:
        head = too_large_head(api, path, limits[name] + 1, content_type, token, expect)
        status, headers, body = raw_head(api, head)
        assert status.split()[1:2] == ["413"], (path, token is not None, status, body[:300])
        assert headers.get("x-bench-store") == "v1", headers
        error = json.loads(body)["error"]
        assert (error["code"], error["kind"]) == ("too_large", "invalid"), (path, error)
        assert error.get("details", {}).get(name) == limits[name], error


@pytest.mark.rule("2.13.token-first")
@pytest.mark.parametrize("token", [None, "not-a-token-of-this-server"], ids=["none", "garbage"])
def test_a_request_without_a_valid_token_is_401_whatever_else_is_wrong(api, held, token):
    """§2.13 item 3: the token before the request's form: an undefined query parameter, a body
    that is no change list, from no login, is ``401``."""
    calls = [
        ("GET", "info", None),
        ("GET", "projects", None),
        ("GET", f"projects/{held.pid}/runs/{held.run['run_id']}", None),
        ("GET", f"projects/{server_target.fresh_id()}/tests/T1", None),
        ("POST", f"projects/{held.pid}/changes", {"changes": "not a list"}),
    ]
    for method, path, body in calls:
        reply = api.call(method, path, role=None, token=token, query="kit_unknown=1",
                         json_body=body)  # fmt: skip
        assert reply.status == 401, f"{method} {path}: {reply}"
        refused(reply, 401, "auth", "invalid_token", "expired_token")


@pytest.mark.offer_aware
@pytest.mark.rule("2.13.parameters-before-item")
def test_an_undefined_parameter_is_400_even_where_the_path_names_nothing_held(api, held, store):
    """§2.13 item 5 before item 7: a query parameter the call does not define is ``400``, also
    where the item named is not held (else ``404``): a project never seen, a test, a run and a
    blob that are not there, an artifact (E9), and P9 to a project never seen; a project's tests,
    a release and a library item when the server offers their calls."""
    gone, suffix = server_target.fresh_id(), server_target.fresh_suffix()
    pid = held.pid
    calls = [
        ("P2", f"projects/{gone}", ""),
        ("P4", f"projects/{gone}/tests", ""),
        ("P7", f"projects/{pid}/tests/kit-none-{suffix}", ""),
        ("P8b", f"projects/{pid}/releases/{held.test_id}/99", ""),
        ("E3", f"projects/{pid}/runs/{run_id()}", ""),
        ("E9", f"projects/{pid}/artifacts/report/kit-none-{suffix}.html",
         f"serial=SN-1&{board_query()}"),
        ("L2", f"library/config/kit-none-{suffix}/1.0", ""),
    ]  # fmt: skip
    if store is not None:
        calls.append(("B2", f"storages/{store}/blobs/{gone}/{'3' * 64}", ""))
    for _call, path, query in offered_calls(held, calls):
        reply = api.call("GET", path, query="&".join(q for q in (query, "kit_unknown=1") if q))
        assert reply.status == 400, f"{path}: {reply}"
        refused(reply, 400, "invalid", "bad_request")
    change = {"path": "tests/T1/kit.md", "replaces": None} | text_file("never written\n")
    reply = api.call("POST", f"projects/{gone}/changes", query="kit_unknown=1",
                     json_body={"changes": [change]})  # fmt: skip
    refused(reply, 400, "invalid", "bad_request")


@pytest.mark.offer_aware
@pytest.mark.rule("2.13.role-before-item")
def test_a_login_the_call_is_not_for_is_403_even_where_nothing_named_is_held(api, held, store):
    """§2.13 item 6 before item 7: a login whose roles do not allow the call is ``403``, also
    where what it names does not exist (else ``404``): the manager writing to a project never
    seen, its release, a storage not held; a bench deleting, restoring, purging what is not
    there, when the server offers those calls."""
    gone, tid, missing_run = server_target.fresh_id(), str(uuid.uuid4()), run_id()
    artifact = {"kind": "report", "serial": "SN-1", "part_number": "PN-1", "board_revision": "A",
                "name": f"kit-none-{server_target.fresh_suffix()}.html"}  # fmt: skip
    managers = [
        ("E11", f"projects/{held.pid}/runs/{missing_run}/delete", {"reason": "kit"}),
        ("E11", f"projects/{gone}/runs/{missing_run}/restore", {}),
        ("E12", f"projects/{gone}/artifacts/delete", {**artifact, "reason": "kit"}),
        ("E13", f"projects/{gone}/purge", {}),
    ]
    for _call, path, body in offered_calls(held, managers):
        reply = api.call("POST", path, json_body={"machine_id": MACHINE, **body})
        refused(reply, 403, "denied", "not_a_manager")
    change = {"path": "tests/T1/kit.md", "replaces": None} | text_file("never written\n")
    release = {"format": "text", "fingerprint": "a" * 64, "content": "b" * 64, "files": {}}
    writes = [
        (f"projects/{gone}/changes", {"changes": [change]}),
        (f"projects/{gone}/releases/{tid}", release),
    ]
    for path, body in writes:
        refused(api.call("POST", path, role="manager", json_body=body), 403, "denied", "forbidden")
    if store is not None:
        data = b"kit bytes for no storage"
        path = f"storages/{store + 1_000_000}/blobs/{gone}/{sha(data)}"
        reply = api.call("POST", path, role="manager", data=data,
                         headers={"Content-Type": "application/octet-stream"})  # fmt: skip
        refused(reply, 403, "denied", "forbidden")


# ---- §3.1 Address --------------------------------------------------------------------------


@pytest.mark.offer_aware
@pytest.mark.parametrize("slash", [True, False], ids=["with", "without"])
@pytest.mark.rule("3.1.base-slash")
def test_the_key_calls_answer_on_the_base_with_or_without_its_trailing_slash(
    served, twin, held, slash
):
    """§8.1 rule 6: the base as a user types it, with or without its slash, normalized as the
    app does (one trailing slash) before ``api/v1/``."""
    base = served.rstrip("/") + ("/" if slash else "")
    api = Api(base, twin)
    try:
        for name, path, query in read_calls(held):
            reply = api.call("GET", path, query=query)
            assert reply.status == 200 and "Location" not in reply.headers, f"{name}: {reply}"
    finally:
        api.close()


@pytest.mark.offer_aware
@pytest.mark.rule("3.1.opaque-continuation")
def test_a_cursor_and_a_token_are_taken_back_verbatim(api, store):
    """E2's cursor and P3's change token, each when the server offers that call; E8's."""
    project = new_project(api)
    pushed = []
    for day in (10, 11, 12):
        run = run_body(project.test_id, day=day)
        ok(api.call("POST", f"projects/{project.pid}/runs", json_body=run))
        pushed.append(run["run_id"])
    if api.offers("P3"):
        token = ok(api.call("GET", f"projects/{project.pid}"))["token"]
        since = ok(api.call("GET", f"projects/{project.pid}",
                            query=f"since={quote(token, safe='')}"))  # fmt: skip
        assert since["files"] == {} and since.get("deleted", []) == [], since
    if api.offers("E2"):
        seen, cursor = [], None
        for _ in range(10):
            query = f"project_id={project.pid}&limit=1" + (
                f"&cursor={quote(cursor, safe='')}" if cursor else ""
            )
            page = server_target.kit_runs(ok(api.call("GET", "runs", query=query)))
            assert len(page["runs"]) <= 1, page
            seen += [r["run_id"] for r in page["runs"]]
            cursor = page["cursor"]
            if cursor is None:
                break
            assert isinstance(cursor, str) and cursor, page
        assert seen == sorted(pushed, reverse=True), seen

    if store is not None:
        names = []
        for n in range(2):
            data = f"<html>page {n}</html>".encode()
            report = report_body(store, data, name=f"kit-page-{n}.html")
            ok(api.call("POST", f"projects/{project.pid}/artifacts", json_body=report,
                        parts={f"sha256:{sha(data)}": data}))  # fmt: skip
            names.append(report["name"])
        seen, cursor = [], None
        for _ in range(10):
            query = "limit=1" + (f"&cursor={quote(cursor, safe='')}" if cursor else "")
            page = ok(api.call("GET", f"projects/{project.pid}/artifacts", query=query))
            seen += [a["name"] for a in page["artifacts"]]
            cursor = page["cursor"]
            if cursor is None:
                break
        assert seen == names, seen


SEGMENT_SERIAL = "A/B #+ é?x%y"
SEGMENT_PART = "P+Q/R #2"


@pytest.mark.rule("3.1.segment-decoding", "3.1.query-plus")
def test_a_name_holding_slash_hash_plus_and_blanks_reaches_the_server_as_one_name(api):
    project = new_project(api)
    run = run_body(project.test_id, serial=SEGMENT_SERIAL, part_number=SEGMENT_PART,
                   board_revision="r+1")  # fmt: skip
    ok(api.call("POST", f"projects/{project.pid}/runs", json_body=run))
    boards = ok(api.call("GET", f"projects/{project.pid}/boards"))["boards"]
    assert [(b["serial"], b.get("part_number"), b.get("board_revision")) for b in boards] == [
        (SEGMENT_SERIAL, SEGMENT_PART, "r+1")], boards  # fmt: skip
    read = ok(api.call("GET", f"projects/{project.pid}/boards/{seg(SEGMENT_SERIAL)}",
                       query=board_query(SEGMENT_PART, "r+1")))  # fmt: skip
    read = server_target.kit_tests(read)
    assert read["board"]["serial"] == SEGMENT_SERIAL, read
    assert [t["pointer"]["run_id"] for t in read["tests"]] == [run["run_id"]], read
    # A literal "+" in a query value is a plus, never a blank.
    plus = ok(api.call("GET", f"projects/{project.pid}/boards",
                       query="part_number=P+Q/R%20%232&board_revision=r+1"))  # fmt: skip
    assert [b["serial"] for b in plus["boards"]] == [SEGMENT_SERIAL], plus


# ---- §3.3 Discovery, §3.4 Authentication, §3.5 Roles -----------------------------------------


@pytest.mark.rule("3.3.info-token-expires")
def test_the_tokens_expiry_when_given_is_a_time_to_come(api):
    """A SHOULD: absent, the kit warns; given, it is a UTC time, later than now."""
    user = ok(api.call("GET", "info"))["user"]
    if "token_expires" not in user:
        warnings.warn("/info gives no user.token_expires (a SHOULD, §3.3)", stacklevel=1)
        return
    value = user["token_expires"]
    assert TIME.fullmatch(value), value
    expires = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    assert expires > datetime.now(timezone.utc), value


UNAUTHENTICATED = [
    ("GET", "projects", None),
    ("GET", "projects/{pid}", None),
    ("GET", "projects/{pid}/boards", None),
    ("GET", "runs", None),
    ("GET", "library", None),
    ("GET", "benches", None),
    ("GET", "storages/1/blobs/{pid}/" + "0" * 64, None),
    ("POST", "projects", {}),
    ("POST", "projects/{pid}/runs", {}),
    ("POST", "projects/{pid}/changes", {"changes": []}),
    ("POST", "projects/{pid}/purge", {"machine_id": MACHINE}),
    ("POST", "library/config", {}),
]


@pytest.mark.rule("3.4.no-token-401", "3.4.bad-token-401")
@pytest.mark.parametrize("token", [None, "not-a-token-of-this-server"], ids=["none", "garbage"])
@pytest.mark.parametrize("method, path, body", UNAUTHENTICATED,
                         ids=[f"{m}-{p.split('/')[0]}-{n}" for n, (m, p, _) in enumerate(UNAUTHENTICATED)])  # fmt: skip
def test_every_call_without_a_valid_token_is_401(api, method, path, body, token):
    reply = api.call(method, path.format(pid=server_target.fresh_id()), role=None, token=token,
                     json_body=body)  # fmt: skip
    if token is None:
        refused(reply, 401, "auth", "invalid_token")
        assert reply.headers.get("WWW-Authenticate") == "Bearer", reply.headers
    else:
        refused(reply, 401, "auth", "invalid_token", "expired_token")
        assert reply.headers.get("WWW-Authenticate") == 'Bearer error="invalid_token"', (
            reply.headers
        )


@pytest.mark.rule("3.4.header-only")
def test_a_token_anywhere_but_the_authorization_header_is_no_token(api):
    bench = server_target.token("bench")
    in_query = api.call("GET", "projects", role=None, query=f"access_token={quote(bench, safe='')}")
    assert in_query.status in (400, 401), in_query
    in_cookie = api.call("GET", "projects", role=None, headers={"Cookie": f"token={bench}"})
    refused(in_cookie, 401, "auth", "invalid_token")


@pytest.mark.offer_aware
@pytest.mark.rule("3.5.every-login-reads")
def test_every_login_reads_whatever_its_roles(api, held):
    for name, path, query in read_calls(held):
        reply = api.call("GET", path, query=query, role="manager")
        assert reply.status == 200, f"{name} read by the manager login: {reply}"


@pytest.mark.rule("3.5.write-needs-role", "4.1.envelope")
@pytest.mark.parametrize("call", ["P11", "P9", "P10", "E6", "E7", "E10", "E15", "L3", "B1"])
def test_a_write_the_logins_roles_do_not_allow_is_403_forbidden(api, held, store, call):
    """The manager login holds only ``manager``: each write of a bench is refused it, ``403
    forbidden``, nothing written."""
    pid, tid = held.pid, held.test_id
    parts, data, headers, query = None, None, None, ""
    if call == "P11":
        new = server_target.fresh_id()
        path, body = "projects", project_body(new, str(uuid.uuid4()))
    elif call == "P9":
        written = f"tests/T1/kit-{server_target.fresh_suffix()}.md"
        path = f"projects/{pid}/changes"
        body = {"changes": [{"path": written, "replaces": None} | text_file("no\n")]}
    elif call == "P10":
        files = {"tests/T1/test.json": f"sha256:{sha(id_json(tid).encode())}"}
        path = f"projects/{pid}/releases/{tid}"
        body = {"format": "text", "fingerprint": "e" * 64, "content": "f" * 64, "files": files}
    elif call == "E6":
        path, query = f"projects/{pid}/boards/SN-1/tests/{tid}/current", board_query()
        body = {"run_id": held.first["run_id"], "replaces": None, "machine_id": MACHINE}
    elif call == "E7":
        path = f"projects/{pid}/runs/{held.run['run_id']}/annotation"
        body = {"entries": [{"entry_id": str(uuid.uuid4()), "kind": "comment", "key": "test",
                             "text": "no", "replaces": None, "by": "kit", "machine_id": MACHINE,
                             "saved_at": now()}]}  # fmt: skip
    elif call == "E10":
        content = b"<html>forbidden</html>"
        path = f"projects/{pid}/artifacts"
        body = report_body(needs_store(store), content, name="kit-forbidden.html")
        parts = {f"sha256:{sha(content)}": content}
    elif call == "E15":
        path, query = f"projects/{pid}/boards/SN-forbidden/entries", board_query()
        body = {"entries": [board_entry()]}
    elif call == "L3":
        archive = b"PK forbidden " + uuid.uuid4().bytes
        path = "library/config"
        body = library_body(needs_store(store), archive, f"kit-no-{server_target.fresh_suffix()}")
        parts = {body["archive"]["version"]: archive}
    else:
        data = b"forbidden upload " + uuid.uuid4().bytes
        path = f"storages/{needs_store(store)}/blobs/{pid}/{sha(data)}"
        body, headers = None, {"Content-Type": "application/octet-stream"}
    reply = api.call("POST", path, role="manager", json_body=body, parts=parts, data=data,
                     headers=headers, query=query)  # fmt: skip
    refused(reply, 403, "denied", "forbidden")
    if call == "P11":
        refused(api.call("GET", f"projects/{new}"), 404, "not_found", "not_found")
    if call == "P9":
        check_nothing_written(api, pid, written)
    if call == "E6":
        board = ok(api.call("GET", f"projects/{pid}/boards/SN-1", query=board_query()))
        assert board["tests"][0]["pointer"]["run_id"] == held.run["run_id"], board


@pytest.mark.rule("3.5.manager-only-no-push")
def test_a_login_holding_only_manager_cannot_push_a_run(api, held):
    run = run_body(held.test_id, serial="SN-manager")
    reply = api.call("POST", f"projects/{held.pid}/runs", role="manager", json_body=run)
    refused(reply, 403, "denied", "forbidden")
    refused(
        api.call("GET", f"projects/{held.pid}/runs/{run['run_id']}"), 404, "not_found", "not_found"
    )


@pytest.mark.rule("3.5.not-a-manager")
@pytest.mark.parametrize("call", ["E11-delete", "E11-restore", "E12-delete", "E12-restore", "E13"])
def test_a_delete_restore_or_purge_from_a_bench_is_403_not_a_manager(api, held, call):
    pid = held.pid
    reason = {"machine_id": MACHINE, "reason": "Kit check"}
    if call.startswith("E11"):
        path = f"projects/{pid}/runs/{held.run['run_id']}/{call.split('-')[1]}"
        body = reason if call.endswith("delete") else {"machine_id": MACHINE}
    elif call.startswith("E12"):
        if held.report is None:
            pytest.skip("the server lists no api storage, so the kit holds no report")
        name = {
            k: held.report[k] for k in ("kind", "serial", "part_number", "board_revision", "name")
        }
        path = f"projects/{pid}/artifacts/{call.split('-')[1]}"
        body = name | (reason if call.endswith("delete") else {"machine_id": MACHINE})
    else:
        path, body = (
            f"projects/{pid}/purge",
            {"machine_id": MACHINE, "items": [{"run_id": held.run["run_id"]}]},
        )
    refused(api.call("POST", path, json_body=body), 403, "denied", "not_a_manager")
    run = ok(api.call("GET", f"projects/{pid}/runs/{held.run['run_id']}"))
    assert "deleted_at" not in run, run


# ---- §3.6 Transport --------------------------------------------------------------------------


@pytest.mark.rule("3.6.length-required")
def test_a_body_without_its_length_is_411(api):
    pid, tid = server_target.fresh_id(), str(uuid.uuid4())
    body = json.dumps(project_body(pid, tid)).encode()
    reply = api.call("POST", "projects", data=body, chunked=True,
                     headers={"Content-Type": "application/json"})  # fmt: skip
    refused(reply, 411, "invalid", "length_required")
    refused(api.call("GET", f"projects/{pid}"), 404, "not_found", "not_found")


def raw_head(api: Api, head: str) -> tuple[str, dict[str, str], bytes]:
    """Send a request's head alone (no body) on a new connection; the first answer: its status
    line, headers and body."""
    sock = socket.create_connection((api.host, api.port), timeout=30)
    if api.https:
        sock = server_target.tls_context().wrap_socket(sock, server_hostname=api.host)
    try:
        sock.sendall(head.encode("latin-1"))
        stream = sock.makefile("rb")
        status = stream.readline().decode("latin-1").strip()
        headers: dict[str, str] = {}
        while True:
            line = stream.readline().decode("latin-1").strip()
            if not line:
                break
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
        body = stream.read(int(headers.get("content-length", "0")))
        return status, headers, body
    except TimeoutError:
        pytest.fail("no answer within 30 s to a request whose body was never sent: the server "
                    "waits for the body instead of refusing it first")  # fmt: skip
    finally:
        sock.close()


def stalled_answer(api: Api, path: str, body: bytes, wait: float) -> tuple[str, dict[str, str], bytes]:
    """POST *body*'s first half on a new connection, announcing it whole, then stall: the
    server's answer within *wait* seconds (its status line, headers and body)."""
    sock = socket.create_connection((api.host, api.port), timeout=30)
    if api.https:
        sock = server_target.tls_context().wrap_socket(sock, server_hostname=api.host)
    head = (
        f"POST {api.prefix}{path} HTTP/1.1\r\nHost: {api.netloc}\r\n"
        f"Authorization: Bearer {server_target.token('bench')}\r\n"
        f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n"
    )
    try:
        sock.sendall(head.encode("latin-1") + body[: len(body) // 2])
        sock.settimeout(wait)
        stream = sock.makefile("rb")
        status = stream.readline().decode("latin-1").strip()
        headers: dict[str, str] = {}
        while line := stream.readline().decode("latin-1").strip():
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
        return status, headers, stream.read(int(headers.get("content-length", "0")))
    except TimeoutError:
        pytest.fail(f"no answer within {wait:.0f} s to a body that stopped arriving "
                    f"({server_target.SLOW_WAIT_ENV} sets the wait)")  # fmt: skip
    finally:
        sock.close()


@pytest.mark.switch(server_target.SLOW_ENV)
@pytest.mark.rule("4.3.408")
def test_a_body_that_stops_arriving_is_408_request_timeout(served, twin, tmp_path):
    """§4.3: the server gives up on a body that stalls, answering ``408 request_timeout``, kind
    ``timeout``, with its signature and the envelope; nothing is written. Slow against a server
    the environment names (it waits for that server to give up): run with
    ``PROCEDO_KIT_SLOW=1``. The reference server is started giving up after 1 s."""
    server_target.slow("it waits for the server to give up on a body that stops arriving")
    pid, tid = server_target.fresh_id(), str(uuid.uuid4())
    body = json.dumps(project_body(pid, tid)).encode()
    if server_target.external():
        base, running, wait = served, None, server_target.slow_wait()
    else:
        base, running = start(tmp_path / "stall", LOCAL_TOKENS, body_timeout=1)
        wait = 30.0
    api = Api(base, twin)
    try:
        status, headers, data = stalled_answer(api, "projects", body, wait)
        assert status.split()[1:2] == ["408"], status
        assert headers.get("x-bench-store") == "v1" and headers.get("x-request-id"), headers
        assert headers.get("date"), headers
        error = json.loads(data)["error"]
        assert (error["code"], error["kind"]) == ("request_timeout", "timeout"), error
        assert error["request_id"] == headers["x-request-id"], error
        assert isinstance(error["message"], str) and error["message"].strip(), error
        refused(api.call("GET", f"projects/{pid}"), 404, "not_found", "not_found")
    finally:
        api.close()
        if running is not None:
            stop(running)


@pytest.mark.rule("3.6.too-large-before-body", "4.4.details")
@pytest.mark.parametrize("expect", [True, False], ids=["expect-100", "no-expect"])
@pytest.mark.parametrize("call", ["P11", "B1"])
def test_a_body_over_the_limit_is_413_before_it_is_read(api, held, store, call, expect):
    limits = ok(api.call("GET", "info"))["limits"]
    if call == "P11":
        path, name = "projects", "max_upload_bytes"
        content_type = "application/json"
    else:
        path = f"storages/{needs_store(store)}/blobs/{held.pid}/{'1' * 64}"
        name, content_type = "max_file_bytes", "application/octet-stream"
    size = limits[name] + 1
    head = (
        f"POST {api.prefix}{path} HTTP/1.1\r\nHost: {api.netloc}\r\n"
        f"Authorization: Bearer {server_target.token('bench')}\r\n"
        f"Content-Type: {content_type}\r\nContent-Length: {size}\r\n"
        + ("Expect: 100-continue\r\n" if expect else "")
        + "Connection: close\r\n\r\n"
    )
    status, headers, body = raw_head(api, head)
    assert not status.split()[1:2] == ["100"], "the server asked for the body (100 Continue)"
    assert status.split()[1:2] == ["413"], status
    assert headers.get("x-bench-store") == "v1" and headers.get("x-request-id"), headers
    error = json.loads(body)["error"]
    assert error["code"] == "too_large" and error["kind"] == "invalid", error
    assert error["request_id"] == headers["x-request-id"], error
    assert error.get("details", {}).get(name) == limits[name], error


# ---- §3.7 G2, §4 Errors and their details ----------------------------------------------------


@pytest.mark.rule("4.3.409-invalid", "4.4.details", "2.8.digest-forms", "3.7.G2-all-or-nothing")
def test_a_reference_to_bytes_not_there_is_409_missing_files_and_nothing_is_written(
    api, held, store
):
    store = needs_store(store)
    absent = b"never sent " + uuid.uuid4().bytes
    version = f"sha256:{sha(absent)}"
    path = f"documents/kit-{server_target.fresh_suffix()}.bin"
    change = {
        "path": path,
        "replaces": None,
        "version": version,
        "size": len(absent),
        "store_id": store,
    }
    error = refused(changes_of(api, held.pid, [change]), 409, "invalid", "missing_files")
    assert error.get("details", {}).get("missing") == [version], error
    check_nothing_written(api, held.pid, path)

    run = run_body(held.test_id, serial="SN-missing", files=[run_file("gone.bin", absent, store)])
    error = refused(api.call("POST", f"projects/{held.pid}/runs", json_body=run), 409, "invalid",
                    "missing_files")  # fmt: skip
    assert error.get("details", {}).get("missing") == [version], error
    refused(
        api.call("GET", f"projects/{held.pid}/runs/{run['run_id']}"), 404, "not_found", "not_found"
    )
    board = ok(api.call("GET", f"projects/{held.pid}/boards/SN-missing", query=board_query()))
    assert server_target.kit_tests(board)["tests"] == [], ("a refused run is listed", board)
    # On a server registering its boards the board may be the kit's own (a refusal for the
    # board alone came first, server_target.BoardSeeds): E4 lists it there.
    if not server_target.seeds_boards():
        boards = ok(api.call("GET", f"projects/{held.pid}/boards"))["boards"]
        assert "SN-missing" not in [b["serial"] for b in boards], boards


@pytest.mark.rule("4.3.409-invalid", "4.4.details")
def test_a_release_conflict_is_409_with_what_the_server_holds(api):
    project = new_project(api)
    pid, tid = project.pid, project.test_id
    files = {path: f["version"] for path, f in project.files.items() if path.startswith("tests/")}
    release = {"format": "text", "fingerprint": "a" * 64, "content": "b" * 64, "files": files}
    ok(api.call("POST", f"projects/{pid}/releases/{tid}", json_body=release))
    error = refused(api.call("POST", f"projects/{pid}/releases/{tid}", json_body=release), 409,
                    "invalid", "same_as_release")  # fmt: skip
    assert error.get("details", {}).get("version") == 1, error
    stale = dict(
        release,
        content="c" * 64,
        files=dict(files, **{"tests/T1/procedure_text.md": f"sha256:{sha(b'stale')}"}),
    )
    error = refused(api.call("POST", f"projects/{pid}/releases/{tid}", json_body=stale), 409,
                    "invalid", "changed")  # fmt: skip
    details = error.get("details", {})
    assert details.get("changed") == ["tests/T1/procedure_text.md"], error
    assert isinstance(details.get("current"), dict) and details["current"], error
    held_now = {p: f["version"] for p, f in details["current"].items()}
    assert held_now.get("tests/T1/procedure_text.md") == files["tests/T1/procedure_text.md"], error


@pytest.mark.offer_aware
@pytest.mark.rule("4.3.409-invalid", "4.4.details")
def test_a_project_or_library_version_held_already_is_409(api, store):
    """A project (P11); a library version (L3) when the server offers it."""
    project = new_project(api)
    again = project_body(project.pid, project.test_id)
    refused(api.call("POST", "projects", json_body=again), 409, "invalid", "exists")
    if not api.offers("L3"):
        return
    store = needs_store(store)
    archive = b"PK first " + uuid.uuid4().bytes
    item = library_body(store, archive, f"kit-ver-{server_target.fresh_suffix()}")
    ok(
        api.call(
            "POST", "library/config", json_body=item, parts={item["archive"]["version"]: archive}
        )
    )
    other = b"PK other " + uuid.uuid4().bytes
    changed = library_body(store, other, item["id"])
    error = refused(api.call("POST", "library/config", json_body=changed,
                             parts={changed["archive"]["version"]: other}), 409, "invalid",
                    "version_exists")  # fmt: skip
    assert error.get("details", {}).get("content_sha256") == item["content_sha256"], error
    twin = library_body(store, archive, item["id"].upper())
    refused(api.call("POST", "library/config", json_body=twin,
                     parts={twin["archive"]["version"]: archive}), 409, "invalid", "exists")  # fmt: skip


@pytest.mark.rule("4.3.409-invalid")
def test_a_write_to_what_a_manager_deleted_is_409_deleted(api, store):
    project = new_project(api)
    run = run_body(project.test_id)
    ok(api.call("POST", f"projects/{project.pid}/runs", json_body=run))
    ok(api.call("POST", f"projects/{project.pid}/runs/{run['run_id']}/delete", role="manager",
                json_body={"machine_id": MACHINE, "reason": "Kit check"}))  # fmt: skip
    entry = {"entry_id": str(uuid.uuid4()), "kind": "comment", "key": "test", "text": "late",
             "replaces": None, "by": "kit", "machine_id": MACHINE, "saved_at": now()}  # fmt: skip
    reply = api.call("POST", f"projects/{project.pid}/runs/{run['run_id']}/annotation",
                     json_body={"entries": [entry]})  # fmt: skip
    refused(reply, 409, "invalid", "deleted")
    if store is not None:
        data = b"<html>gone</html>"
        report = report_body(store, data, name="kit-deleted.html")
        ok(api.call("POST", f"projects/{project.pid}/artifacts", json_body=report,
                    parts={f"sha256:{sha(data)}": data}))  # fmt: skip
        name = {k: report[k] for k in ("kind", "serial", "part_number", "board_revision", "name")}
        ok(api.call("POST", f"projects/{project.pid}/artifacts/delete", role="manager",
                    json_body=name | {"machine_id": MACHINE, "reason": "Kit check"}))  # fmt: skip
        other = b"<html>again</html>"
        again = report_body(store, other, name="kit-deleted.html")
        refused(api.call("POST", f"projects/{project.pid}/artifacts", json_body=again,
                         parts={f"sha256:{sha(other)}": other}), 409, "invalid", "deleted")  # fmt: skip


@pytest.mark.rule("4.3.416")
def test_a_range_past_the_end_is_416_with_the_files_size(api, held, store):
    store = needs_store(store)
    data = bytes(range(256)) * 4 + uuid.uuid4().bytes
    path = f"storages/{store}/blobs/{held.pid}/{sha(data)}"
    upload = api.call("POST", path, data=data, headers={"Content-Type": "application/octet-stream"})
    assert ok(upload)["version"] == f"sha256:{sha(data)}" and upload.json["size"] == len(data)
    whole = api.call("GET", path)
    assert whole.status == 200 and whole.body == data, whole
    assert whole.headers.get("Accept-Ranges") == "bytes", whole.headers
    part = api.call("GET", path, headers={"Range": "bytes=2-5"})
    assert part.status == 206 and part.body == data[2:6], part
    assert part.headers.get("Content-Range") == f"bytes 2-5/{len(data)}", part.headers
    past = api.call("GET", path, headers={"Range": f"bytes={len(data) + 10}-"})
    refused(past, 416, "corrupt", "range_not_satisfiable")
    assert past.headers.get("Content-Range") == f"bytes */{len(data)}", past.headers


@pytest.mark.rule("4.3.422")
def test_bytes_not_matching_their_name_are_422_digest_mismatch(api, held, store):
    store = needs_store(store)
    data = b"sent " + uuid.uuid4().bytes
    named = b"named " + uuid.uuid4().bytes
    upload = api.call("POST", f"storages/{store}/blobs/{held.pid}/{sha(named)}", data=data,
                      headers={"Content-Type": "application/octet-stream"})  # fmt: skip
    refused(upload, 422, "corrupt", "digest_mismatch")
    run = run_body(held.test_id, serial="SN-422", files=[run_file("x.bin", named, store)])
    reply = api.call("POST", f"projects/{held.pid}/runs", json_body=run,
                     parts={f"sha256:{sha(named)}": data})  # fmt: skip
    refused(reply, 422, "corrupt", "digest_mismatch")
    refused(
        api.call("GET", f"projects/{held.pid}/runs/{run['run_id']}"), 404, "not_found", "not_found"
    )


@pytest.mark.rule("4.4.details", "4.3.400-invalid")
def test_a_part_over_one_mib_is_400_naming_it_in_large(api, held, store):
    store = needs_store(store)
    data = b"L" * (PART_LIMIT + 1)
    run = run_body(held.test_id, serial="SN-large", files=[run_file("big.bin", data, store)])
    reply = api.call(
        "POST", f"projects/{held.pid}/runs", json_body=run, parts={f"sha256:{sha(data)}": data}
    )
    error = refused(reply, 400, "invalid", "bad_request")
    assert error.get("details", {}).get("large") == [f"sha256:{sha(data)}"], error
    refused(
        api.call("GET", f"projects/{held.pid}/runs/{run['run_id']}"), 404, "not_found", "not_found"
    )


@pytest.mark.rule("4.5.refusal-inside-200")
def test_a_change_made_on_the_server_meanwhile_is_200_changed_not_an_error(api):
    """P9: a change naming a version the server no longer holds answers ``200`` with that
    change ``changed`` (its ``current`` what the server holds), and the other changes apply."""
    project = new_project(api)
    held = project.files["tests/T1/procedure_text.md"]
    first = {"path": "tests/T1/procedure_text.md", "replaces": held["version"]} | text_file(
        "# T1, first\n"
    )
    assert [r["status"] for r in ok(changes_of(api, project.pid, [first]))["results"]] == ["saved"]
    stale = {"path": "tests/T1/procedure_text.md", "replaces": held["version"]} | text_file(
        "# T1, stale\n"
    )
    new = {"path": f"tests/T1/kit-{server_target.fresh_suffix()}.md", "replaces": None} | text_file(
        "new\n"
    )
    reply = changes_of(api, project.pid, [stale, new])
    assert reply.status == 200, reply
    results = ok(reply)["results"]
    assert [r["status"] for r in results] == ["changed", "saved"], results
    assert results[0]["current"]["version"] == first["version"], results


@pytest.mark.rule("4.5.refusal-inside-200")
def test_a_pointer_moved_meanwhile_is_200_changed_not_an_error(api):
    project = new_project(api)
    older = run_body(project.test_id, day=20)
    newer = run_body(project.test_id, day=21)
    for run in (older, newer):
        ok(api.call("POST", f"projects/{project.pid}/runs", json_body=run))
    stale = {"run_id": older["run_id"], "ended": older["ended"]}
    body = {"run_id": older["run_id"], "replaces": stale, "machine_id": MACHINE}
    path = f"projects/{project.pid}/boards/SN-1/tests/{project.test_id}/current"
    answer = ok(api.call("POST", path, query=board_query(), json_body=body))
    assert answer["status"] == "changed", answer
    assert answer["pointer"]["run_id"] == newer["run_id"], answer
