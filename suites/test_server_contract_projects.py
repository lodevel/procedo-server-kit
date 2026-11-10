"""The contract's rules for projects, parts, the library, storages, large files and the import
(``procedo_server_contract.md`` §6.1–6.3, §6.5–6.7, §7), checked with raw HTTP against the
reference server or the server the environment names (``server_target``).

Raw HTTP, not the app's client: the client hides a server's faults (it checks a request before
sending it, sorts what it lists, ignores ``missing``, takes parts it did not ask for, reads two
different ``409`` alike, reads the kind and not the status). Each check here reads the status,
the ``kind``, the ``code`` and the body, so a server breaking the rule fails it.

Each case takes one fixture, ``api`` (:class:`Api`), and writes under ids and names of its own:
it never assumes an empty server and passes again on the same server. ``@pytest.mark.rule``
names the rules of ``docs/server_contract/coverage.md`` a case catches; for each, the reference
server can break it on purpose (``tests/contract_faults.py``), and ``test_contract_faults.py``
proves the case then fails.

The importer's cases need ``PROCEDO_KIT_IMPORTER_TOKEN``, and two library and upload cases
``PROCEDO_KIT_BENCH_ONLY_TOKEN``, on a server the environment names: unset, they are skipped,
saying so. The cases reading the reference server's own disk run against it alone.
"""

from __future__ import annotations

import functools
import gzip
import hashlib
import io
import json
import os
import secrets
import socket
import unicodedata
import urllib.parse
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from email.message import Message
from pathlib import Path
from typing import Any

import pytest
import server_target
from reference_server import start, stop

pytestmark = pytest.mark.servers

MIB = 1024 * 1024
MACHINE = "0000c0de"
PN, REV, SERIAL = "KIT-PN", "A", "SN-1"
T1_ID = "3f9c2a1e-7b4d-4e8a-9c61-2d5b8e0f4a17"
T2_ID = "8d02e6b1-5c3a-4f19-b7e2-90a4c6d1e358"
T3_ID = "c1a2b3c4-d5e6-4f70-8a91-b2c3d4e5f607"
OTHER_ID = "4b7e9d21-0c3f-4a56-8e1b-d7f2c6a9b304"
ZERO = "sha256:" + "0" * 64
TIME = "%Y-%m-%dT%H:%M:%SZ"
KINDS = {
    "bad_request": "invalid",
    "not_found": "not_found",
    "missing_files": "invalid",
    "exists": "invalid",
    "changed": "invalid",
    "same_as_release": "invalid",
    "version_exists": "invalid",
    "too_large": "invalid",
    "forbidden": "denied",
    "not_a_manager": "denied",
    "run_conflict": "corrupt",
    "digest_mismatch": "corrupt",
    "range_not_satisfiable": "corrupt",
    "gone": "not_found",
}
SECRET_NAMES = ("password", "passwd", "passphrase", "secret", "private_key", "token")


# ---- Bytes ------------------------------------------------------------------------------------


def hex_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def version(data: bytes) -> str:
    return "sha256:" + hex_of(data)


def noise(size: int) -> bytes:
    """*size* bytes of this case's own, nothing compresses."""
    out = bytearray()
    block = secrets.token_bytes(32)
    while len(out) < size:
        block = hashlib.sha256(block).digest()
        out += block
    return bytes(out[:size])


def binary(size: int) -> bytes:
    """*size* bytes of this case's own that are never UTF-8 (they start with 0xff)."""
    return b"\xff" + noise(size - 1)


def a_hex() -> str:
    return secrets.token_hex(32)


def text(content: str) -> dict:
    """A File travelling as text."""
    data = content.encode()
    return {"version": version(data), "size": len(data), "text": content}


def ref(data: bytes, store_id: int) -> dict:
    """A File travelling as a reference."""
    return {"version": version(data), "size": len(data), "store_id": store_id}


def the_test_json(test_id: str) -> str:
    return json.dumps({"active": "text", "test_id": test_id}) + "\n"


# ---- HTTP -------------------------------------------------------------------------------------


@dataclass
class Reply:
    status: int
    headers: Message
    body: bytes

    @property
    def content_type(self) -> str:
        return self.headers.get("Content-Type", "").split(";")[0].strip().lower()

    @property
    def json(self) -> Any:
        if self.content_type == "multipart/mixed":
            return json.loads(self._sections()[0][1])
        assert self.content_type == "application/json", (self.status, dict(self.headers))
        return json.loads(_undone(self.body, self.headers))

    @property
    def parts(self) -> dict[str, bytes]:
        """The file parts of a multipart answer, by name; none for plain JSON."""
        if self.content_type != "multipart/mixed":
            return {}
        return {name: data for name, data in self._sections()[1:]}

    def _sections(self) -> list[tuple[str, bytes]]:
        boundary = _param(self.headers.get("Content-Type", ""), "content-type", "boundary")
        assert boundary, self.headers
        out = []
        for chunk in (b"\r\n" + self.body).split(b"\r\n--" + boundary.encode())[1:]:
            if chunk.startswith(b"--"):
                break
            head, _, data = chunk.partition(b"\r\n\r\n")
            headers = Message()
            for line in head.decode("latin-1").split("\r\n")[1:]:
                name, _, value = line.partition(":")
                headers[name.strip()] = value.strip()
            name = _param(headers.get("Content-Disposition", ""), "content-disposition", "name")
            # §2.12: no part carries a Content-Encoding, the json part is plain JSON
            assert "Content-Encoding" not in headers, f"part {name} carries a Content-Encoding"
            out.append((name or "", data))
        assert out and out[0][0] == "json", "the first part of a multipart answer is json"
        return out


def _undone(data: bytes, headers: Message) -> bytes:
    if headers.get("Content-Encoding", "").strip().lower() == "gzip":
        return gzip.decompress(data)
    return data


def _param(value: str, header: str, param: str) -> str | None:
    message = Message()
    message[header] = value
    found = message.get_param(param, header=header)
    return found[2] if isinstance(found, tuple) else found


def check_signed(reply: Reply) -> None:
    """§3.2: every answer carries the server's signature, a request id and a date."""
    assert reply.headers.get("X-Bench-Store") == "v1", (reply.status, dict(reply.headers))
    assert reply.headers.get("X-Request-Id"), (reply.status, dict(reply.headers))
    assert reply.headers.get("Date"), (reply.status, dict(reply.headers))


def refused(reply: Reply, status: int, code: str, kind: str | None = None) -> dict:
    """The refusal, as §4 phrases it: the status, the envelope with its code and kind, the
    request id the answer's, a sentence. Its ``error`` object."""
    assert reply.status == status, (
        f"expected {status} {code}, the server answered {reply.status}: {reply.body[:600]!r}"
    )
    assert reply.content_type == "application/json", dict(reply.headers)
    error = json.loads(_undone(reply.body, reply.headers))["error"]
    assert error["code"] == code, error
    assert error["kind"] == (kind or KINDS[code]), error
    assert error["request_id"] == reply.headers["X-Request-Id"], error
    assert isinstance(error["message"], str) and error["message"].strip(), error
    return error


def ok(reply: Reply) -> Any:
    assert reply.status == 200, f"expected 200, got {reply.status}: {reply.body[:600]!r}"
    return reply.json


def form(obj: dict, parts: list[bytes]) -> tuple[bytes, str]:
    """A ``multipart/form-data`` body: the JSON first, then one part per content, each named
    by its bytes' sha256 (a part named otherwise: :func:`raw_form`)."""
    return raw_form(obj, [(version(data), data) for data in dict.fromkeys(parts)])


def raw_form(obj: dict, parts: list[tuple[str, bytes]]) -> tuple[bytes, str]:
    boundary = "kit-" + secrets.token_hex(12)
    chunks = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="json"\r\n'.encode(),
        b"Content-Type: application/json\r\n\r\n",
        json.dumps(obj).encode(),
        b"\r\n",
    ]
    for name, data in parts:
        chunks += [
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
            f'filename="f.bin"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode(),
            data,
            b"\r\n",
        ]
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


@dataclass
class Api:
    """The server under test, reached with plain HTTP. ``running``: the reference server when
    the case runs against one started for it (None against a server the environment names);
    ``root``: where a case may start another."""

    base_url: str
    running: Any = None
    root: Path | None = None
    _store: int | None = field(default=None, repr=False)

    @property
    def _split(self) -> urllib.parse.SplitResult:
        return urllib.parse.urlsplit(self.base_url)

    @property
    def prefix(self) -> str:
        return self._split.path.rstrip("/") + "/api/v1/"

    def _connection(self) -> Any:
        import http.client

        parts = self._split
        if parts.scheme == "https":
            context = server_target.tls_context()
            return http.client.HTTPSConnection(parts.netloc, timeout=60, context=context)
        return http.client.HTTPConnection(parts.netloc, timeout=60)

    def request(
        self,
        method: str,
        path: str,
        *,
        query: list[tuple[str, str]] | dict | None = None,
        body: bytes | None = None,
        content_type: str | None = None,
        headers: dict[str, str] | None = None,
        role: str | None = "bench",
    ) -> Reply:
        sent = {"Accept": "multipart/mixed, application/json"}
        if role is not None:
            sent["Authorization"] = f"Bearer {server_target.token(role)}"
        if content_type is not None:
            sent["Content-Type"] = content_type
        sent.update(headers or {})
        target = self.prefix + path
        if query:
            pairs = list(query.items()) if isinstance(query, dict) else query
            target += "?" + "&".join(
                f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(v, safe=',:')}"
                for k, v in pairs
            )
        if method == "POST" and body is None:
            body = b""
        connection = self._connection()
        try:
            connection.request(method, target, body=body, headers=sent)
            answer = connection.getresponse()
            reply = Reply(answer.status, answer.headers, answer.read())
        finally:
            connection.close()
        check_signed(reply)
        return reply

    def get(self, path: str, query: Any = None, *, role: str = "bench", **kw: Any) -> Reply:
        return self.request("GET", path, query=query, role=role, **kw)

    def post(self, path: str, obj: Any, query: Any = None, *, role: str = "bench") -> Reply:
        data = json.dumps(obj).encode()
        return self.request(
            "POST", path, query=query, body=data, content_type="application/json", role=role
        )

    def post_form(
        self, path: str, obj: dict, parts: list[bytes], query: Any = None, *, role: str = "bench"
    ) -> Reply:
        data, content_type = form(obj, parts)
        return self.request(
            "POST", path, query=query, body=data, content_type=content_type, role=role
        )

    def headers_only(self, path: str, size: int, content_type: str) -> Reply:
        """A POST announcing *size* bytes with ``Expect: 100-continue`` and never sending them:
        the answer a server gives before reading the body (§3.6). A server asking for the body
        (``100 Continue``) instead fails the case."""
        parts = self._split
        host = parts.hostname or "127.0.0.1"
        port = parts.port or (443 if parts.scheme == "https" else 80)
        sock = socket.create_connection((host, port), timeout=20)
        if parts.scheme == "https":
            sock = server_target.tls_context().wrap_socket(sock, server_hostname=host)
        lines = [
            f"POST {self.prefix}{path} HTTP/1.1",
            f"Host: {parts.netloc}",
            f"Authorization: Bearer {server_target.token('bench')}",
            f"Content-Type: {content_type}",
            f"Content-Length: {size}",
            "Expect: 100-continue",
            "Accept: application/json",
        ]
        with sock:
            sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
            reader = sock.makefile("rb")
            status = int(reader.readline().decode("latin-1").split(" ", 2)[1])
            headers = Message()
            while line := reader.readline().decode("latin-1").rstrip("\r\n"):
                name, _, value = line.partition(":")
                headers[name.strip()] = value.strip()
            assert status != 100, "the server asked for the body (100 Continue), not refusing it"
            body = reader.read(int(headers.get("Content-Length", "0")))
        reply = Reply(status, headers, body)
        check_signed(reply)
        return reply

    # ---- What the cases share ----

    def store(self) -> int:
        """The ``api`` storage's ``store_id`` (S1): the parts' storage."""
        if self._store is None:
            storages = ok(self.get("storages"))["storages"]
            found = [s["store_id"] for s in storages if s.get("backend") == "api"]
            if not found:
                pytest.skip("this server keeps no bytes itself (no api storage): no parts")
            self._store = found[0]
        return self._store

    def user(self, role: str = "bench") -> str:
        return ok(self.get("info", role=role))["user"]["name"]

    def server_now(self) -> str:
        """The server's clock, to the second (``/info``)."""
        return ok(self.get("info"))["server_time"][:19] + "Z"

    def create(
        self,
        files: dict[str, str | bytes],
        *,
        pid: str | None = None,
        name: str | None = None,
        drafts: list[dict] | None = None,
        expect: int = 200,
    ) -> Project:
        """P11: a project of this case's own; a ``str`` travels as text, ``bytes`` as a part."""
        pid = pid or server_target.fresh_id()
        name = name or f"Ripple bench {server_target.fresh_suffix()}"
        body, parts = project_body(self, pid, name, files)
        if drafts is not None:
            body["drafts"] = drafts
        reply = self.post_form("projects", body, parts)
        if expect == 200:
            ok(reply)
        return Project(self, pid, name, reply)

    def upload(self, owner: str, data: bytes, *, role: str = "bench") -> Reply:
        return self.request(
            "POST",
            f"storages/{self.store()}/blobs/{owner}/{hex_of(data)}",
            body=data,
            content_type="application/octet-stream",
            role=role,
        )

    @contextmanager
    def limited(self, **limits: int) -> Iterator[Api]:
        """Another reference server with these limits."""
        with self.another(f"it needs the server's own limits set to {limits}", **limits) as api:
            yield api

    @contextmanager
    def another(self, why: str, **options: Any) -> Iterator[Api]:
        """Another reference server, started with *options* (``reference_server.start``); a
        case run against a server the environment names is skipped, saying *why*."""
        server_target.local_only(why)
        assert self.root is not None
        root = self.root / f"another-{secrets.token_hex(4)}"
        base_url, running = start(root, server_target.LOCAL_LOGINS, **options)
        try:
            yield Api(base_url, running, root)
        finally:
            stop(running)

    def reference(self) -> Any:
        """The reference server itself, for a case reading its disk."""
        server_target.local_only("it damages a blob on the reference server's own disk")
        assert self.running is not None
        return self.running


def project_body(
    api: Api, pid: str, name: str, files: dict[str, str | bytes]
) -> tuple[dict, list[bytes]]:
    every = {"config/config.json": json.dumps({"project_id": pid}), **files}
    sent, parts = {}, []
    for path, content in every.items():
        if isinstance(content, str):
            sent[path] = text(content)
        else:
            sent[path] = ref(content, api.store())
            parts.append(content)
    return {"format_version": 1, "project_id": pid, "name": name, "files": sent}, parts


@dataclass
class Project:
    api: Api
    pid: str
    name: str
    made: Reply

    def path(self, rest: str = "") -> str:
        return f"projects/{self.pid}" + (f"/{rest}" if rest else "")

    def read(self, **query: str) -> dict:
        return ok(self.api.get(self.path(), query or None))

    def token(self) -> str:
        return self.read()["token"]

    def releases(self, test_id: str) -> list[dict]:
        """The releases filed under *test_id*, oldest first, as P2 answers them (a core call:
        P8 lists the same, on a server offering it)."""
        return self.read()["releases"].get(test_id, [])

    def mark(self) -> Any:
        """What :meth:`nothing_since` compares with: the change token on a server offering P3;
        on any other, the project as P2 answers it now (its files and its releases)."""
        if server_target.offers(self.api.base_url, "P3"):
            return self.token()
        found = self.read()
        return {"files": found["files"], "releases": found["releases"]}

    def since(self, token: str) -> dict:
        return ok(self.api.get(self.path(), {"since": token}))

    def push(self, changes: list[dict], parts: list[bytes] = (), drafts: Any = None) -> Reply:
        body: dict = {"changes": changes}
        if drafts is not None:
            body["drafts"] = drafts
        if parts:
            return self.api.post_form(self.path("changes"), body, list(parts))
        return self.api.post(self.path("changes"), body)

    def release(self, test_id: str, body: dict, *, role: str = "bench") -> Reply:
        return self.api.post(self.path(f"releases/{test_id}"), body, role=role)

    def nothing_since(self, mark: Any) -> None:
        """Nothing was written since *mark* (:meth:`mark`): P3 brings no file, no delete, no
        release; on a server without P3, P2 answers the files and releases it answered then.
        The P3 made only on a server offering it, the case runs on any server."""
        if isinstance(mark, str):
            with server_target.unrecorded():
                changes = self.since(mark)
            assert (changes["files"], changes["deleted"], changes.get("releases")) == ({}, [], {})
        else:
            found = self.read()
            assert {"files": found["files"], "releases": found["releases"]} == mark


def a_test(test_id: str = T1_ID, folder: str = "T1") -> dict[str, str]:
    return {
        f"tests/{folder}/test.json": the_test_json(test_id),
        f"tests/{folder}/procedure.json": '{"steps": []}',
    }


def a_draft(test: str = "T1", test_id: str = T1_ID, **more: Any) -> dict:
    return {"test": test, "test_id": test_id, "fingerprint": a_hex(), "content": a_hex(), **more}


def a_release(files: dict[str, str]) -> dict:
    return {"format": "text", "fingerprint": a_hex(), "content": a_hex(), "files": files}


def a_run(day: int, test_id: str = T1_ID, *, files: list[dict] = (), **more: Any) -> dict:
    return {
        "format_version": 2,
        "run_id": f"2026-04-{day:02d}T10-00-00Z_{secrets.token_hex(8)}",
        "serial": SERIAL,
        "part_number": PN,
        "board_revision": REV,
        "test": "T1",
        "test_id": test_id,
        "complete": True,
        "started": f"2026-04-{day:02d}T10:00:00Z",
        "ended": f"2026-04-{day:02d}T10:05:00Z",
        "verdict": "PASS",
        "machine_id": MACHINE,
        "draft": False,
        "phases": [],
        "files": list(files),
        **more,
    }


def run_file(api: Api, data: bytes, name: str = "log.txt") -> dict:
    return {
        "name": name,
        "role": "log",
        "size": len(data),
        "sha256": hex_of(data),
        "mimetype": "text/plain",
        "store_id": api.store(),
    }


def board_query() -> dict:
    return {"part_number": PN, "board_revision": REV}


def board_results(api: Api, pid: str) -> dict:
    return ok(api.get(f"projects/{pid}/boards/{SERIAL}", board_query()))


def pointer_of(api: Api, pid: str, test_id: str = T1_ID) -> dict | None:
    for test in board_results(api, pid)["tests"]:
        if test["test_id"] == test_id:
            return test.get("pointer")
    return None


def a_declared_storage(api: Api) -> dict:
    """S2: a storage of this case's own on a host no server reaches."""
    host = f"kit-{server_target.fresh_suffix()}.invalid"
    body = {
        "backend": "sftp",
        "params": {"host": host, "port": "22", "base_path": "/kit", "user": "kit", "auth": "key"},
        "identity": f"sftp://{host}:22/kit",
    }
    return ok(api.post("storages", body))["storage"]


@pytest.fixture
def api(tmp_path) -> Iterator[Api]:
    with server_target.serve(tmp_path / "server", server_target.LOCAL_LOGINS) as (url, running):
        yield Api(url, running, tmp_path)


# ---- §6.1 Rules shared by the calls -----------------------------------------------------------


@pytest.mark.rule("6.1.evidence-only-project")
def test_a_project_known_only_by_its_evidence_is_no_project_of_the_project_calls(api):
    pid = server_target.fresh_id()
    run = a_run(1, project_name=f"Evidence {server_target.fresh_suffix()}")
    ok(api.post(f"projects/{pid}/runs", run))
    listed = [p["project_id"] for p in ok(api.get("projects"))["projects"]]
    assert pid not in listed, "P1 lists a project known only by its evidence"
    p = f"projects/{pid}"
    reads = [
        api.get(p),
        api.post(f"{p}/read", {"have": []}),
        api.get(f"{p}/tests/T1"),
        api.post(f"{p}/tests/T1/read", {"have": []}),
        api.post(f"{p}/releases/{T1_ID}", a_release({"tests/T1/test.json": ZERO})),
        # P9 answers before reading the body: one that is not even JSON.
        api.request("POST", f"{p}/changes", body=b"{not json", content_type="application/json"),
    ]
    optional = [  # each made only when the server offers it
        ("P3", lambda: api.get(p, {"since": "anything"})),
        ("P4", lambda: api.get(f"{p}/tests")),
        ("P5", lambda: api.get(f"{p}/tests", {"names": "T1"})),
        ("P6", lambda: api.post(f"{p}/tests/read", {"names": ["T1"], "have": []})),
        ("P8", lambda: api.get(f"{p}/releases/{T1_ID}")),
        ("P8b", lambda: api.get(f"{p}/releases/{T1_ID}/1")),
        ("P8b", lambda: api.post(f"{p}/releases/{T1_ID}/1/read", {"have": []})),
    ]
    with server_target.unrecorded():
        reads += [read() for call, read in optional if server_target.offers(api.base_url, call)]
    for reply in reads:
        refused(reply, 404, "not_found")
    made = api.create(a_test(), pid=pid)
    assert ok(made.made)["project_id"] == pid
    assert pid in [p["project_id"] for p in ok(api.get("projects"))["projects"]]
    assert ok(api.get(f"{p}/runs/{run['run_id']}"))["run_id"] == run["run_id"], "evidence lost"
    runs = board_results(api, pid)["tests"][0]["runs"]
    assert [r["run_id"] for r in runs] == [run["run_id"]]


@pytest.mark.rule("6.1.unseen-project-empty")
def test_a_project_never_seen_reads_as_an_empty_one(api):
    """E4, E5, E3, E8 and E9 on any server; E2 when it offers it."""
    pid = server_target.fresh_id()
    assert ok(api.get(f"projects/{pid}/boards"))["boards"] == []
    assert board_results(api, pid)["tests"] == []
    refused(api.get(f"projects/{pid}/runs/{a_run(1)['run_id']}"), 404, "not_found")
    assert ok(api.get(f"projects/{pid}/artifacts"))["artifacts"] == []
    query = {"serial": SERIAL, **board_query()}
    refused(api.get(f"projects/{pid}/artifacts/report/r.html", query), 404, "not_found")
    if server_target.offers(api.base_url, "E2"):
        with server_target.unrecorded():
            assert ok(api.get("runs", {"project_id": pid}))["runs"] == []


# ---- §6.2 How file bytes travel ---------------------------------------------------------------


@pytest.mark.rule("6.2.request-content-type")
def test_a_body_neither_json_nor_multipart_is_400(api):
    project = api.create(a_test())
    before = project.mark()
    change = {"path": "tests/T1/notes.md", "replaces": None, "text": "notes"}
    body = json.dumps({"changes": [change]}).encode()
    for content_type in ("text/plain", "application/x-www-form-urlencoded"):
        reply = api.request("POST", project.path("changes"), body=body, content_type=content_type)
        refused(reply, 400, "bad_request")
    project.nothing_since(before)
    pid = server_target.fresh_id()
    body, _ = project_body(api, pid, f"Ripple bench {server_target.fresh_suffix()}", a_test())
    reply = api.request(
        "POST", "projects", body=json.dumps(body).encode(), content_type="text/plain"
    )
    refused(reply, 400, "bad_request")
    refused(api.get(f"projects/{pid}"), 404, "not_found")


@pytest.mark.rule("6.2.part-boundary")
def test_a_file_of_exactly_one_mib_rides_as_a_part_both_ways(api):
    exact = binary(MIB)
    project = api.create({**a_test(), "documents/exact.bin": exact})
    reply = api.get(project.path())
    assert reply.json["files"]["documents/exact.bin"]["size"] == MIB
    assert reply.parts.get(version(exact)) == exact, "a file of exactly 1 MiB rides as a part"


@pytest.mark.rule("6.2.part-over-1mib-400")
def test_a_part_over_one_mib_is_400_with_its_version_and_nothing_written(api):
    over = binary(MIB + 1)
    project = api.create(a_test())
    before = project.mark()
    changes = [
        {"path": "tests/T1/notes.md", "replaces": None, "text": "notes"},
        {"path": "documents/over.bin", "replaces": None, **ref(over, api.store())},
    ]
    error = refused(project.push(changes, [over]), 400, "bad_request")
    assert error["details"]["large"] == [version(over)], error
    project.nothing_since(before)
    pid = server_target.fresh_id()
    body, parts = project_body(api, pid, f"Ripple {server_target.fresh_suffix()}", a_test())
    body["files"]["documents/over.bin"] = ref(over, api.store())
    error = refused(api.post_form("projects", body, [*parts, over]), 400, "bad_request")
    assert error["details"]["large"] == [version(over)], error
    refused(api.get(f"projects/{pid}"), 404, "not_found")


@pytest.mark.rule("6.2.two-parts-one-name-400")
def test_two_parts_with_one_name_are_400(api):
    project = api.create(a_test())
    before = project.mark()
    data = binary(500)
    change = {"path": "documents/a.bin", "replaces": None, **ref(data, api.store())}
    body, content_type = raw_form({"changes": [change]}, [(version(data), data)] * 2)
    reply = api.request("POST", project.path("changes"), body=body, content_type=content_type)
    refused(reply, 400, "bad_request")
    project.nothing_since(before)


@pytest.mark.rule("6.2.digest-mismatch", "4.3.422")
def test_a_part_whose_bytes_are_not_its_name_is_422_digest_mismatch(api):
    project = api.create(a_test())
    before = project.mark()
    named, sent = binary(500), binary(500)
    change = {"path": "documents/a.bin", "replaces": None, **ref(named, api.store())}
    body, content_type = raw_form({"changes": [change]}, [(version(named), sent)])
    reply = api.request("POST", project.path("changes"), body=body, content_type=content_type)
    refused(reply, 422, "digest_mismatch")
    project.nothing_since(before)


@pytest.mark.rule("6.2.part-size-400")
def test_a_part_whose_size_is_not_its_files_size_is_400(api):
    project = api.create(a_test())
    before = project.mark()
    data = binary(500)
    change = {"path": "documents/a.bin", "replaces": None, **ref(data, api.store())}
    change["size"] = 501
    refused(project.push([change], [data]), 400, "bad_request")
    project.nothing_since(before)


@pytest.mark.rule("6.2.unreferenced-part-ignored")
def test_a_part_no_file_names_is_ignored_and_not_kept(api):
    project = api.create(a_test())
    stray = binary(700)
    write = {"path": "tests/T1/notes.md", "replaces": None, "text": "notes"}
    results = ok(project.push([write], [stray]))["results"]
    assert [r["status"] for r in results] == ["saved"]
    later = {"path": "documents/stray.bin", "replaces": None, **ref(stray, api.store())}
    error = refused(project.push([later]), 409, "missing_files")
    assert error["details"]["missing"] == [version(stray)], "an unreferenced part was kept"


@pytest.mark.rule(
    "6.2.answer-no-part-json", "2.12.no-gzip-multipart-answer", "2.12.plain-json-part"
)
def test_an_answer_takes_its_form_from_what_it_sends_never_from_accept(api):
    small = binary(800)
    project = api.create({**a_test(), "documents/a.bin": small})
    for accept in ("application/json", "text/html"):
        reply = api.get(project.path(), headers={"Accept": accept})
        assert reply.status == 200, (accept, reply.status, reply.body[:300])
        assert reply.content_type == "multipart/mixed", (accept, reply.content_type)
        assert reply.parts == {version(small): small}
    held = ",".join([version(small)])
    reply = api.get(project.path(), {"have": held})
    assert reply.status == 200 and reply.content_type == "application/json", reply.headers
    reply = api.get(project.path(), headers={"Accept-Encoding": "gzip"})
    assert reply.content_type == "multipart/mixed"
    assert reply.headers.get("Content-Encoding", "identity").lower() != "gzip", (
        "a multipart answer is never gzipped whole"
    )
    assert reply.parts == {version(small): small}


@pytest.mark.rule("6.2.have")
def test_have_leaves_out_the_parts_the_reader_holds(api):
    one, two = binary(600), binary(900)
    project = api.create({**a_test(), "documents/one.bin": one, "documents/two.bin": two})
    whole = api.get(project.path())
    assert whole.parts == {version(one): one, version(two): two}
    reply = api.get(project.path(), {"have": version(one)})
    assert reply.parts == {version(two): two}, "a part named in have was sent"
    assert reply.json["files"]["documents/one.bin"]["version"] == version(one), "still listed"
    twin = api.post(project.path("read"), {"have": [version(one), version(two)]})
    assert twin.parts == {} and twin.content_type == "application/json"
    assert set(twin.json["files"]) >= {"documents/one.bin", "documents/two.bin"}


@pytest.mark.rule("6.2.read-twins")
def test_each_read_twin_answers_as_its_get(api):
    """The twins of the core reads (P2b, P7, E3, E5) on any server; those of P3, P5/P6, P8b and
    L2 when the server offers them (made then only, the case runs on any server)."""
    offers = functools.partial(server_target.offers, api.base_url)
    pic, log, archive = binary(400), binary(300), binary(500)
    files = {**a_test(), "tests/T1/pic.png": pic}
    project = api.create(files)
    held = project.read()["files"]
    release = a_release({p: held[p]["version"] for p in files})
    ok(project.release(T1_ID, release))
    token = project.token()
    ok(project.push([{"path": "tests/T1/notes.md", "replaces": None, "text": "n"}]))
    run = a_run(1, files=[run_file(api, log)])
    ok(api.post_form(project.path("runs"), run, [log]))
    lib_id = f"kit-{server_target.fresh_suffix()}"
    if offers("L2", "L3"):
        with server_target.unrecorded():
            ok(publish(api, "config", lib_id, "1.0", archive))
    p = project.path()
    pairs = [
        ("P2", ("", {}), ("read", {})),
        ("P3", ("", {"since": token}), ("read", {"since": token})),
        ("P5", ("tests", {"names": "T1"}), ("tests/read", {"names": ["T1"]})),
        ("P7", ("tests/T1", {}), ("tests/T1/read", {})),
        ("P8b", (f"releases/{T1_ID}/1", {}), (f"releases/{T1_ID}/1/read", {})),
        ("E3", (f"runs/{run['run_id']}", {}), (f"runs/{run['run_id']}/read", {})),
    ]
    optional = {"P3": ("P3",), "P5": ("P5", "P6"), "P8b": ("P8b",)}
    pairs = [pair[1:] for pair in pairs if offers(*optional.get(pair[0], ()))]
    for have in ([], [version(pic), version(log)]):
        with server_target.unrecorded():  # every optional read here, made only when offered
            for (get_path, query), (twin_path, body) in pairs:
                asked = {**query, "have": ",".join(have)} if have else query
                got = api.get(f"{p}/{get_path}".rstrip("/"), asked or None)
                twin = api.post(f"{p}/{twin_path}", {**body, "have": have})
                assert (twin.status, twin.json, twin.parts) == (got.status, got.json, got.parts), (
                    twin_path,
                    have,
                )
            if offers("L2", "L3"):
                item = f"library/config/{lib_id}/1.0"
                got = api.get(item, {"have": ",".join(have)} if have else None)
                twin = api.post(f"{item}/read", {"have": have})
                assert (twin.json, twin.parts) == (got.json, got.parts), ("L2", have)
        board = f"{p}/boards/{SERIAL}"
        got = api.get(board, {**board_query(), "include": "current", "have": ",".join(have)})
        twin = api.post(f"{board}/read", {"include": "current", "have": have}, board_query())
        assert (twin.json, twin.parts) == (got.json, got.parts), ("E5", have)


@pytest.mark.rule("6.2.missing-files-409", "P9.missing-files-409", "4.3.409-invalid")
def test_a_reference_to_bytes_not_there_is_409_missing_files_and_nothing_written(api):
    project = api.create(a_test())
    before = project.mark()
    absent = binary(900)
    changes = [
        {"path": "tests/T1/notes.md", "replaces": None, "text": "notes"},
        {"path": "documents/absent.bin", "replaces": None, **ref(absent, api.store())},
    ]
    error = refused(project.push(changes), 409, "missing_files")
    assert error["details"]["missing"] == [version(absent)], error
    project.nothing_since(before)
    pid = server_target.fresh_id()
    body, _ = project_body(api, pid, f"Ripple {server_target.fresh_suffix()}", a_test())
    body["files"]["documents/absent.bin"] = ref(absent, api.store())
    error = refused(api.post("projects", body), 409, "missing_files")
    assert error["details"]["missing"] == [version(absent)], error
    refused(api.get(f"projects/{pid}"), 404, "not_found")
    run = a_run(1, files=[run_file(api, absent)])
    error = refused(api.post(project.path("runs"), run), 409, "missing_files")
    assert error["details"]["missing"] == [version(absent)], error
    refused(api.get(project.path(f"runs/{run['run_id']}")), 404, "not_found")
    results = ok(project.push(changes, [absent]))["results"]  # the bytes sent: it goes in
    assert [r["status"] for r in results] == ["saved", "saved"]


@pytest.mark.rule("6.2.blobs-per-project", "B2.owner-404")
def test_bytes_one_project_holds_are_missing_for_another(api):
    owner, other = server_target.fresh_id(), server_target.fresh_id()
    data = binary(MIB + 100)
    assert ok(api.upload(owner, data))["status"] == "saved"
    blob = f"storages/{api.store()}/blobs"
    assert api.get(f"{blob}/{owner}/{hex_of(data)}").body == data
    refused(api.get(f"{blob}/{other}/{hex_of(data)}"), 404, "not_found")
    body, parts = project_body(api, other, f"Ripple {server_target.fresh_suffix()}", a_test())
    body["files"]["documents/big.bin"] = ref(data, api.store())
    error = refused(api.post_form("projects", body, parts), 409, "missing_files")
    assert error["details"]["missing"] == [version(data)], error
    body, parts = project_body(api, owner, f"Ripple {server_target.fresh_suffix()}", a_test())
    body["files"]["documents/big.bin"] = ref(data, api.store())
    ok(api.post_form("projects", body, parts))


@pytest.mark.rule(
    "6.2.damaged-blob", "5.2.project-missing", "6.2.small-without-part-missing", "B1.already"
)
def test_a_damaged_blob_is_missing_and_the_bytes_sent_again_replace_it(api):
    running = api.reference()
    damaged, gone = binary(800), binary(600)
    project = api.create({**a_test(), "documents/a.bin": damaged, "documents/c.bin": gone})
    blobs = running.state.blobs
    blobs.path(project.pid, hex_of(damaged)).write_bytes(b"not the bytes")
    blobs.path(project.pid, hex_of(gone)).unlink()
    reply = api.get(project.path())
    whole = reply.json
    assert whole["missing"] == {"documents/a.bin": "damaged", "documents/c.bin": "not_found"}
    assert {"documents/a.bin", "documents/c.bin"} <= set(whole["files"]), "a missing file hides"
    assert reply.parts == {}, "a part of bytes the server cannot give"
    again = {"path": "documents/b.bin", "replaces": None, **ref(damaged, api.store())}
    error = refused(project.push([again]), 409, "missing_files")
    assert error["details"]["missing"] == [version(damaged)]
    assert [r["status"] for r in ok(project.push([again], [damaged]))["results"]] == ["saved"]
    assert ok(api.upload(project.pid, gone))["status"] == "saved", "a copy not held is saved"
    assert ok(api.upload(project.pid, gone))["status"] == "already"
    reply = api.get(project.path())
    assert "missing" not in reply.json, reply.json.get("missing")
    assert reply.parts == {version(damaged): damaged, version(gone): gone}


@pytest.mark.rule("6.2.unreachable-assumed")
def test_a_reference_into_a_storage_the_server_does_not_reach_is_assumed_to_hold(api):
    far = a_declared_storage(api)
    assert far["reachable"] is False, far
    data = binary(5000)
    pid = server_target.fresh_id()
    body, parts = project_body(api, pid, f"Ripple {server_target.fresh_suffix()}", a_test())
    body["files"]["documents/far.bin"] = ref(data, far["store_id"])
    made = ok(api.post_form("projects", body, parts))
    assert made["files"]["documents/far.bin"]["store_id"] == far["store_id"]
    project = Project(api, pid, body["name"], Reply(200, Message(), b""))
    other = binary(300)
    change = {"path": "documents/far2.bin", "replaces": None, **ref(other, far["store_id"])}
    assert [r["status"] for r in ok(project.push([change]))["results"]] == ["saved"]


@pytest.mark.rule("6.2.unknown-store-id-400")
def test_an_unknown_store_id_in_a_reference_is_400(api):
    unknown = max(s["store_id"] for s in ok(api.get("storages"))["storages"]) + 1_000_000
    project = api.create(a_test())
    before = project.mark()
    data = binary(400)
    changes = [
        {"path": "tests/T1/notes.md", "replaces": None, "text": "notes"},
        {"path": "documents/a.bin", "replaces": None, **ref(data, unknown)},
    ]
    refused(project.push(changes), 400, "bad_request")
    project.nothing_since(before)
    pid = server_target.fresh_id()
    body, _ = project_body(api, pid, f"Ripple {server_target.fresh_suffix()}", a_test())
    body["files"]["documents/a.bin"] = ref(data, unknown)
    refused(api.post("projects", body), 400, "bad_request")
    refused(api.get(f"projects/{pid}"), 404, "not_found")
    run = a_run(1, files=[{**run_file(api, data), "store_id": unknown}])
    refused(api.post(project.path("runs"), run), 400, "bad_request")
    refused(api.get(project.path(f"runs/{run['run_id']}")), 404, "not_found")


def mixed_sections(reply: Reply) -> list[tuple[Message, bytes]]:
    """A ``multipart/mixed`` answer's sections in the order sent, each its headers and bytes
    (no order assumed: :attr:`Reply.parts` assumes the json part first)."""
    boundary = _param(reply.headers.get("Content-Type", ""), "content-type", "boundary")
    assert boundary, dict(reply.headers)
    pieces = reply.body.split(b"--" + boundary.encode("latin-1"))
    assert pieces[-1].strip() in (b"--", b"--\r\n".strip()), "the multipart answer is not closed"
    sections = []
    for piece in pieces[1:-1]:
        assert piece.startswith(b"\r\n") and piece.endswith(b"\r\n"), piece[:80]
        head, _, data = piece[2:-2].partition(b"\r\n\r\n")
        headers = Message()
        for line in head.decode("latin-1").split("\r\n"):
            name, _, value = line.partition(":")
            headers[name.strip()] = value.strip()
        sections.append((headers, data))
    return sections


def check_mixed(reply: Reply, expected: dict[str, bytes]) -> dict:
    """§6.2: an answer with parts is ``multipart/mixed``, its json part first
    (``application/json``), then one ``attachment`` per content, named by its bytes' sha256,
    with a filename. Its JSON."""
    assert reply.status == 200, (reply.status, reply.body[:300])
    assert reply.content_type == "multipart/mixed", dict(reply.headers)
    sections = mixed_sections(reply)
    names = [_param(h.get("Content-Disposition", ""), "content-disposition", "name")
             for h, _ in sections]  # fmt: skip
    assert names and names[0] == "json", f"the json part is not first: {names}"
    head, data = sections[0]
    assert head.get("Content-Type", "").split(";")[0].strip().lower() == "application/json", head
    assert "Content-Encoding" not in head, "the json part is plain JSON (§2.12)"
    found = {}
    for (headers, part), name in zip(sections[1:], names[1:]):
        disposition = headers.get("Content-Disposition", "")
        assert disposition.split(";")[0].strip().lower() == "attachment", disposition
        assert name == version(part), f"part {name} holds other bytes"
        assert "Content-Encoding" not in headers, f"part {name} carries a Content-Encoding"
        filename = _param(disposition, "content-disposition", "filename")
        assert filename, f"part {name} has no filename: {disposition}"
        found[name] = part
    assert names[1:].count(names[0]) == 0 and len(found) == len(sections) - 1, names
    assert found == expected, sorted(found)
    return json.loads(data)


@pytest.mark.rule("6.2.answer-multipart")
def test_an_answer_with_parts_is_multipart_mixed_with_the_json_part_first(api):
    """P2, E3 and E9."""
    small, picture, report = binary(700), binary(900), binary(500)
    project = api.create({**a_test(), "documents/a.bin": small})
    run = a_run(1, files=[run_file(api, picture, "scope.png")])
    ok(api.post_form(project.path("runs"), run, [picture]))
    whole = check_mixed(api.get(project.path()), {version(small): small})
    assert whole["project_id"] == project.pid
    read = check_mixed(api.get(project.path(f"runs/{run['run_id']}")), {version(picture): picture})
    assert read["run_id"] == run["run_id"]
    art = an_artifact(api, report, "r.html")
    ok(api.post_form(project.path("artifacts"), art, [report]))
    query = {"serial": SERIAL, **board_query()}
    got = check_mixed(
        api.get(project.path("artifacts/report/r.html"), query), {version(report): report}
    )
    assert got["sha256"] == hex_of(report)


def an_artifact(api: Api, data: bytes, name: str, kind: str = "report") -> dict:
    """An Artifact (§5.10) of the case's board, its bytes a part of the ``api`` storage."""
    return {
        "format_version": 2,
        "kind": kind,
        "serial": SERIAL,
        "part_number": PN,
        "board_revision": REV,
        "name": name,
        "machine_id": MACHINE,
        "updated_at": "2026-04-01T10:00:00Z",
        "sha256": hex_of(data),
        "size": len(data),
        "store_id": api.store(),
    }


@pytest.mark.rule("6.2.held-blob-reference")
def test_a_write_may_reference_without_its_part_a_blob_its_project_holds(api):
    """Raw HTTP: the app's client sends a part again when the server answers ``409
    missing_files``, so only a write sent without it shows the server takes the reference."""
    held, alone = binary(800), binary(600)
    project = api.create({**a_test(), "documents/a.bin": held})
    change = {"path": "documents/b.bin", "replaces": None, **ref(held, api.store())}
    reply = project.push([change])
    assert reply.status == 200, f"a held blob referenced without its part: {reply.body[:400]!r}"
    assert [r["status"] for r in reply.json["results"]] == ["saved"]
    assert ok(api.upload(project.pid, alone))["status"] == "saved"  # held: sent alone first
    run = a_run(1, files=[run_file(api, held, "a.bin"), run_file(api, alone, "b.bin")])
    reply = api.post(project.path("runs"), run)
    assert reply.status == 200, f"a run referencing held blobs without parts: {reply.body[:400]!r}"
    assert reply.json["status"] == "saved"
    reply = api.post(project.path("artifacts"), an_artifact(api, held, "held.html"))
    assert reply.status == 200, f"a report referencing a held blob: {reply.body[:400]!r}"
    read = api.get(project.path(f"runs/{run['run_id']}"))
    assert read.parts == {version(held): held, version(alone): alone}
    assert "missing" not in read.json, read.json.get("missing")
    assert project.read()["files"]["documents/b.bin"]["version"] == version(held)


# ---- §6.3 Projects, tests and releases --------------------------------------------------------


@pytest.mark.rule("P1.light-with-path", "P1.order")
def test_p1_lists_each_project_light_with_its_path_by_folded_name(api):
    sfx = server_target.fresh_suffix()
    names = [f"kit {sfx} b", f"Kit {sfx} c", f"kit {sfx} a", f"KIT {sfx} é"]
    pids = {api.create(a_test(), name=name).pid: name for name in names}
    listed = [p for p in ok(api.get("projects"))["projects"] if p["project_id"] in pids]
    for project in listed:
        assert not {"files", "bundle", "releases"} & set(project), project
        assert project["path"] == unicodedata.normalize("NFC", pids[project["project_id"]])
    folded = [unicodedata.normalize("NFC", n) for n in sorted(names, key=str.casefold)]
    assert [p["path"] for p in listed] == folded, "P1 is ordered by name, case folded"


@pytest.mark.rule("5.2.project-name-400")
def test_p11_refuses_a_name_no_pc_can_hold_as_a_folder(api):
    sfx = server_target.fresh_suffix()
    for name in (
        f"a<b {sfx}",
        f"ends with a dot {sfx}.",
        f"ends with a blank {sfx} ",
        f" starts with a blank {sfx}",
        "CON",
        "com1.txt",
        f"a/b {sfx}",
        f"tab\t{sfx}",
        f"{sfx}" + "x" * 250,
    ):
        made = api.create(a_test(), name=name, expect=400)
        refused(made.made, 400, "bad_request")
        refused(api.get(f"projects/{made.pid}"), 404, "not_found")


@pytest.mark.rule("5.3.test-id-rules-p11")
def test_p11_refuses_a_project_breaking_the_test_id_rules_400_nothing_written(api):
    for files in (
        {"tests/T1/procedure.json": "{}"},
        {"tests/T1/test.json": json.dumps({"active": "text", "test_id": "T1"})},
        {"tests/T1/test.json": "not json"},
        {**a_test(T1_ID, "T1"), **a_test(T1_ID, "T2")},
    ):
        made = api.create(files, expect=400)
        refused(made.made, 400, "bad_request")
        refused(api.get(f"projects/{made.pid}"), 404, "not_found")


@pytest.mark.rule("P11.drafts", "5.3.draft-fields")
def test_p11_keeps_the_drafts_as_p9_does(api):
    kept = a_draft(based_on=2, updated_by="jdoe", machine_id=MACHINE)
    drafts = [kept, a_draft("T2", T3_ID), a_draft("T9", OTHER_ID)]
    project = api.create({**a_test(), **a_test(T2_ID, "T2")}, drafts=drafts)
    draft = ok(api.get(project.path("tests/T1")))["draft"]
    sent = {k: v for k, v in kept.items() if k != "test"}
    assert {k: draft.get(k) for k in sent} == sent, draft
    assert len(draft["updated_at"]) == 20 and draft["updated_at"].endswith("Z"), draft
    assert "draft" not in ok(api.get(project.path("tests/T2"))), "a draft of another id kept"


@pytest.mark.rule("P3.releases-changed", "P3.token-moves")
def test_p3_brings_each_release_made_since_its_token_and_the_token_moves(api):
    server_target.needs(api.base_url, "P3")  # without it every token is "" (§6.3, P2)
    project = api.create(a_test())
    files = project.read()["files"]
    first = a_release({p: files[p]["version"] for p in a_test()})
    t0 = project.token()
    ok(project.release(T1_ID, first))
    t1 = project.token()
    assert t1 != t0, "a release moves the change token"
    changes = project.since(t0)
    assert list(changes["releases"]) == [T1_ID]
    [made] = changes["releases"][T1_ID]
    assert (made["version"], made["content"]) == (1, first["content"])
    assert set(made["documents"]) == {"test.json", "procedure.json"}
    new = '{"steps": [1]}'
    write = {
        "path": "tests/T1/procedure.json",
        "replaces": files["tests/T1/procedure.json"]["version"],
    }
    ok(project.push([{**write, "text": new}]))
    second = a_release({"tests/T1/test.json": files["tests/T1/test.json"]["version"]})
    second["files"]["tests/T1/procedure.json"] = version(new.encode())
    ok(project.release(T1_ID, second))
    changes = project.since(t1)
    assert [r["version"] for r in changes["releases"][T1_ID]] == [1, 2], "the whole list"
    assert all("documents" in r for r in changes["releases"][T1_ID])


@pytest.mark.rule("5.3.draft-moves-no-token")
def test_a_request_carrying_only_drafts_moves_no_token(api):
    server_target.needs(api.base_url, "P3")  # a token that moves is seen by P3 alone
    project = api.create(a_test())
    t0 = project.token()
    draft = a_draft()
    answer = ok(project.push([], drafts=[draft]))
    assert answer["results"] == []
    assert project.token() == t0, "a draft alone moved the change token"
    project.nothing_since(t0)
    assert ok(api.get(project.path("tests/T1")))["draft"]["content"] == draft["content"]


@pytest.mark.rule("P3.empty-since-400")
def test_an_empty_since_is_400(api):
    project = api.create(a_test())
    refused(api.get(project.path(), {"since": ""}), 400, "bad_request")
    refused(api.post(project.path("read"), {"since": "", "have": []}), 400, "bad_request")


@pytest.mark.rule("P5.missing")
def test_p5_and_p6_key_the_tests_as_asked_and_list_the_missing_as_asked(api):
    project = api.create(a_test())
    answer = ok(api.get(project.path("tests"), {"names": "T1,Nope,t1"}))
    assert set(answer["tests"]) == {"T1", "t1"}
    assert answer["tests"]["t1"]["test"] == "T1"
    assert answer["missing"] == ["Nope"]
    answer = ok(api.post(project.path("tests/read"), {"names": ["a,b", "T1"], "have": []}))
    assert (list(answer["tests"]), answer["missing"]) == (["T1"], ["a,b"])


@pytest.mark.rule("P9.changed")
def test_a_write_over_a_file_the_server_does_not_hold_is_changed_without_current(api):
    project = api.create(a_test())
    changes = [
        {"path": "tests/T1/new.md", "replaces": ZERO, "text": "new"},
        {"path": "tests/T1/other.md", "replaces": None, "text": "other"},
    ]
    first, second = ok(project.push(changes))["results"]
    assert first["status"] == "changed" and "current" not in first, first
    assert second["status"] == "saved"


@pytest.mark.rule("P9.replay")
def test_a_move_already_made_is_a_replay_writing_nothing(api):
    project = api.create(a_test())
    files = project.read()["files"]
    folder = {p.split("/")[-1]: files[p]["version"] for p in a_test()}
    move = {"path": "tests/T1", "replaces_folder": folder, "move_to": "tests/T2"}
    assert ok(project.push([move]))["results"][0]["status"] == "saved"
    before = project.mark()
    [again] = ok(project.push([move]))["results"]
    assert again["status"] == "saved", again
    project.nothing_since(before)


@pytest.mark.rule("P9.replay-no-token")
def test_a_request_sent_again_moves_no_token_and_p3_brings_nothing(api):
    """The same P9 again (§6.3 P9): every change already satisfied, it writes nothing, so its
    answer's token is the one held before it, and P3 from that token brings nothing."""
    server_target.needs(api.base_url, "P3")  # without it every token is "" (§6.3, P2)
    project = api.create(a_test())
    held = project.read()["files"]["tests/T1/procedure.json"]["version"]
    changes = [
        {"path": "tests/T1/notes.md", "replaces": None, **text("kit notes\n")},
        {"path": "tests/T1/procedure.json", "replaces": held, **text('{"steps": [1]}')},
    ]
    first = ok(project.push(changes))
    assert [r["status"] for r in first["results"]] == ["saved", "saved"], first
    token = project.token()
    again = ok(project.push(changes))
    assert [r["status"] for r in again["results"]] == ["saved", "saved"], again
    assert again["token"] == token, ("a replay moved the token", token, again["token"])
    project.nothing_since(token)


@pytest.mark.rule(
    "P9.malformed-400", "2.10.path-names-400", "2.9.wrong-form-400", "2.9.text-size-version-400"
)
def test_a_malformed_change_refuses_the_whole_request(api):
    project = api.create(a_test())
    before = project.mark()
    utf8 = b"plain text, sent as a reference"
    bad = [
        ({"path": "tests/T1/a<b.md", "replaces": None, "text": "x"}, []),
        ({"path": "tests/T1/CON.md", "replaces": None, "text": "x"}, []),
        ({"path": "tests/T1/dot./x.md", "replaces": None, "text": "x"}, []),
        ({"path": "tests/T1/../x.md", "replaces": None, "text": "x"}, []),
        ({"path": " tests/T1/x.md", "replaces": None, "text": "x"}, []),
        ({"path": "tests//x.md", "replaces": None, "text": "x"}, []),
        ({"path": "documents/x.md", "replaces": None, "text": "x"}, []),
        ({"path": "tests/T1/v.md", "replaces": None, "text": "x", "version": ZERO, "size": 1}, []),
        ({"path": "tests/T1/r.md", "replaces": None, **ref(utf8, api.store())}, [utf8]),
    ]
    for change, parts in bad:
        good = {"path": "tests/T1/notes.md", "replaces": None, "text": "notes"}
        refused(project.push([good, change], parts), 400, "bad_request")
        project.nothing_since(before)
    made = api.create({**a_test(), "tests/T1/a<b.md": "x"}, expect=400)
    refused(made.made, 400, "bad_request")
    refused(api.get(f"projects/{made.pid}"), 404, "not_found")


@pytest.mark.rule("P9.move-conflict")
def test_a_move_meeting_other_files_is_changed_and_moves_nothing(api):
    project = api.create({**a_test(), **a_test(T2_ID, "T3")})
    files = project.read()["files"]
    whole = {p.split("/")[-1]: files[p]["version"] for p in a_test()}
    partial = {"test.json": whole["test.json"]}
    changes = [
        {"path": "tests/T1", "replaces_folder": partial, "move_to": "tests/T9"},
        {"path": "tests/T1", "replaces_folder": whole, "move_to": "tests/T3"},
        {"path": "tests/T1/notes.md", "replaces": None, "text": "applied"},
    ]
    first, second, third = ok(project.push(changes))["results"]
    for result in (first, second):
        assert result["status"] == "changed" and "current" not in result, result
    assert third["status"] == "saved"
    held = project.read()["files"]
    assert {"tests/T1/test.json", "tests/T1/procedure.json", "tests/T3/test.json"} <= set(held)
    assert not any(p.startswith("tests/T9/") for p in held)
    assert held["tests/T3/test.json"]["version"] == files["tests/T3/test.json"]["version"]


@pytest.mark.rule("P9.move-refused")
def test_a_move_into_itself_or_changing_a_files_form_or_never_travelling_is_refused(api):
    doc = binary(300)
    project = api.create({**a_test(), "scenarios/s1/a.md": "steps", "docs/a.bin": doc})
    files = project.read()["files"]
    tests_t1 = {p.split("/")[-1]: files[p]["version"] for p in a_test()}
    moves = [
        {"path": "tests/T1", "replaces_folder": tests_t1, "move_to": "tests/T1/inner"},
        {
            "path": "scenarios/s1",
            "replaces_folder": {"a.md": files["scenarios/s1/a.md"]["version"]},
            "move_to": "notes/s1",
        },
        {"path": "docs", "replaces_folder": {"a.bin": version(doc)}, "move_to": "results/docs"},
    ]
    for result in ok(project.push(moves))["results"]:
        assert result["status"] == "refused" and result["kind"] == "invalid", result
        assert result["message"].strip()
    assert set(files) == set(project.read()["files"]), "a refused move moved files"


@pytest.mark.rule("P9.rename-carries")
def test_a_renamed_test_keeps_its_id_and_its_draft(api):
    draft = a_draft(based_on=1)
    project = api.create(a_test(), drafts=[draft])
    files = project.read()["files"]
    folder = {p.split("/")[-1]: files[p]["version"] for p in a_test()}
    move = {"path": "tests/T1", "replaces_folder": folder, "move_to": "tests/T5"}
    assert ok(project.push([move]))["results"][0]["status"] == "saved"
    moved = ok(api.get(project.path("tests/T5")))
    assert moved["test_id"] == T1_ID
    assert "draft" in moved, "the renamed test lost its draft"
    for key in ("test_id", "fingerprint", "content", "based_on"):
        assert moved["draft"][key] == draft[key], (key, moved["draft"])
    refused(api.get(project.path("tests/T1")), 404, "not_found")


@pytest.mark.rule("P10.two-files-one-name-400")
def test_p10_refuses_two_files_taking_one_document_name(api):
    files = {**a_test(), "tests/T1/functions/h.py": "a = 1\n", "functions/h.py": "b = 2\n"}
    project = api.create(files)
    held = project.read()["files"]
    body = a_release({p: held[p]["version"] for p in files})
    refused(project.release(T1_ID, body), 400, "bad_request")
    assert project.releases(T1_ID) == []


@pytest.mark.rule("P10.body-test-id-ignored", "P10.answer-light")
def test_p10_releases_the_paths_id_whatever_the_body_says_and_answers_light(api):
    project = api.create(a_test())
    held = project.read()["files"]
    body = {**a_release({p: held[p]["version"] for p in a_test()}), "test_id": OTHER_ID}
    made = ok(project.release(T1_ID, body))
    assert (made["test_id"], made["version"], made["test"]) == (T1_ID, 1, "T1"), made
    assert "documents" not in made, "P10 answers the release without its documents"
    assert [r["version"] for r in project.releases(T1_ID)] == [1]
    assert project.releases(OTHER_ID) == []


# ---- Tests and releases through the core calls alone (P2, P7, P9, P10) --------------------------
# The store suites read releases and tests through P8, P8b, P4 and P5, optional calls (§6.1):
# these cases check the same rules with the core calls, on every server.


@pytest.mark.rule(
    "P2.whole", "P7.one-test", "P10.only-named-files", "P10.changed", "P10.same-as-release",
    "5.4.document-names", "5.4.release-fields", "5.3.latest-release",
    "5.3.test-files-with-functions", "5.3.format-from-test-json", "2.5.test-id-from-test-json",
)  # fmt: skip
def test_a_release_and_its_test_read_back_through_the_core_calls(api):
    """P10 keeps exactly the files named, each named in the test (a ``functions/`` path
    whole); P2 answers the release with its fields and documents, P7 the test (its id from its
    ``test.json``, its format, ``latest_release``, its files with ``functions/``). A stale
    version named is ``409 changed``; the same content again ``409 same_as_release``."""
    files = {
        "tests/Ripple/test.json": the_test_json(T1_ID),
        "tests/Ripple/procedure.json": '{"steps": [1]}',
        "tests/Ripple/notes.md": "not released\n",
        "functions/rails.py": "VALUE = 1\n",
    }
    project = api.create(files)
    held = project.read()["files"]
    named = ("tests/Ripple/test.json", "tests/Ripple/procedure.json", "functions/rails.py")
    body = {**a_release({p: held[p]["version"] for p in named}), "note": "first",
            "released_by": "kit", "pc": "BENCH-1", "machine_id": MACHINE}  # fmt: skip
    made = ok(project.release(T1_ID, body))
    [release] = project.releases(T1_ID)
    for key in ("format", "fingerprint", "content", "note", "released_by", "pc", "machine_id"):
        assert release[key] == body[key], (key, release)
    assert (release["version"], release["test_id"], release["test"]) == (1, T1_ID, "Ripple")
    assert release["released_at"] == made["released_at"], release
    documents = release["documents"]
    assert set(documents) == {"test.json", "procedure.json", "functions/rails.py"}, documents
    assert documents["procedure.json"]["version"] == held["tests/Ripple/procedure.json"]["version"]

    test = ok(api.get(project.path("tests/Ripple")))
    assert (test["test"], test["test_id"], test["format"]) == ("Ripple", T1_ID, "text"), test
    assert test["latest_release"] == 1, test
    assert set(test["files"]) == {*(p for p in files if p.startswith("tests/")),
                                  "functions/rails.py"}, sorted(test["files"])  # fmt: skip

    again = refused(project.release(T1_ID, body), 409, "same_as_release")
    assert again["details"]["version"] == 1, again
    stale = {**body, "content": a_hex(),
             "files": {**body["files"], "functions/rails.py": ZERO}}  # fmt: skip
    error = refused(project.release(T1_ID, stale), 409, "changed")
    assert error["details"]["changed"] == ["functions/rails.py"], error
    assert [r["version"] for r in project.releases(T1_ID)] == [1]


@pytest.mark.rule(
    "P10.not-found", "P10.number-and-name", "5.4.numbering-per-id", "5.3.other-id-another-test",
    "P9.shapes", "P9.last-file-deletes-test",
)  # fmt: skip
def test_releases_are_numbered_per_id_named_now_and_kept_through_a_rename_or_a_delete(api):
    """P10 for an id no test holds is ``404``; releases are numbered from 1 per id and take the
    test's name at the time (a release keeps the name it was made under, a record); a folder
    moved by P9 keeps its id and releases; a ``test.json`` written with another id makes
    another test, numbered from 1; a test whose last file is deleted goes, its releases stay
    under its id (P2)."""
    files = {**a_test(T1_ID, "T1"), **a_test(T2_ID, "T2")}
    project = api.create(files)
    refused(project.release(OTHER_ID, a_release({"tests/T1/test.json": ZERO})), 404, "not_found")

    def release(test_id: str, folder: str) -> dict:
        held = project.read()["files"]
        named = {p: f["version"] for p, f in held.items() if p.startswith(f"tests/{folder}/")}
        return ok(project.release(test_id, a_release(named)))

    assert [release(T1_ID, "T1")["version"], release(T2_ID, "T2")["version"]] == [1, 1]
    held = project.read()["files"]
    folder = {
        p.split("/", 2)[2]: f["version"] for p, f in held.items() if p.startswith("tests/T1/")
    }
    move = {"path": "tests/T1", "replaces_folder": folder, "move_to": "tests/T1 renamed"}
    assert [r["status"] for r in ok(project.push([move]))["results"]] == ["saved"]
    moved = release(T1_ID, "T1 renamed")
    assert (moved["version"], moved["test"]) == (2, "T1 renamed"), moved
    assert [r["test"] for r in project.releases(T1_ID)] == ["T1", "T1 renamed"]

    held = project.read()["files"]
    marker = held["tests/T2/test.json"]
    other = {"path": "tests/T2/test.json", "replaces": marker["version"],
             **text(the_test_json(T3_ID))}  # fmt: skip
    assert [r["status"] for r in ok(project.push([other]))["results"]] == ["saved"]
    assert ok(api.get(project.path("tests/T2")))["test_id"] == T3_ID
    assert release(T3_ID, "T2")["version"] == 1, "another id starts at 1"
    held = project.read()["files"]
    gone = [{"path": p, "replaces": f["version"], "delete": True}
            for p, f in held.items() if p.startswith("tests/T2/")]  # fmt: skip
    assert {r["status"] for r in ok(project.push(gone))["results"]} == {"saved"}
    refused(api.get(project.path("tests/T2")), 404, "not_found")
    assert [r["version"] for r in project.releases(T2_ID)] == [1]
    assert [r["version"] for r in project.releases(T3_ID)] == [1]


@pytest.mark.rule("2.5.malformed-test-id-400-first")
def test_a_test_name_where_a_test_id_goes_is_400_before_the_project_is_looked_at(api):
    unseen = f"projects/{server_target.fresh_id()}"
    refused(api.get(f"{unseen}/releases/T1"), 400, "bad_request")
    refused(api.get(f"{unseen}/releases/T1/1"), 400, "bad_request")
    refused(api.post(f"{unseen}/releases/T1/1/read", {"have": []}), 400, "bad_request")
    body = a_release({"tests/T1/test.json": ZERO})
    refused(api.post(f"{unseen}/releases/T1", body), 400, "bad_request")


@pytest.mark.rule("4.3.410-gone")
def test_a_since_token_the_server_cannot_answer_from_is_410_gone_kind_not_found(api):
    """Raw HTTP: the app's client reads the code ``gone``, never the kind it comes with."""
    project = api.create(a_test())
    for since in ("not a token of this server", f"{project.token()}x"):
        refused(api.get(project.path(), {"since": since}), 410, "gone", "not_found")
        refused(api.post(project.path("read"), {"since": since}), 410, "gone", "not_found")


def a_zip(files: dict[str, bytes]) -> bytes:
    """A zip of *files*, deflated, as the app packs a bundle's folder."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return out.getvalue()


@pytest.mark.rule("5.2.bundle-archive-kept")
def test_a_bundles_archive_is_kept_as_sent_never_repacked(api):
    """Raw HTTP: the app's client unpacks the archive it reads back, so an archive repacked
    under a new version still gives it the same files."""
    filler = noise(3000).hex().encode()
    first = a_zip({"defaults.json": b'{"kit": 1}\n', "rules/doc.md": b"# rules\n" + filler})
    second = a_zip({"defaults.json": b'{"kit": 2}\n', "rules/doc.md": b"# edited\n" + filler})
    pid = server_target.fresh_id()
    body, parts = project_body(api, pid, f"Ripple bench {server_target.fresh_suffix()}", a_test())
    body["bundle"] = {"bundle_id": "kit-bundle", "bundle_version": "1.0",
                      "archive": ref(first, api.store())}  # fmt: skip
    made = ok(api.post_form("projects", body, [*parts, first]))
    assert made["bundle"]["archive"]["version"] == version(first), made["bundle"]
    project = Project(api, pid, body["name"], Reply(200, Message(), b""))
    token = project.token()

    def kept(archive: bytes) -> None:
        reply = api.get(project.path())
        held = reply.json["bundle"]["archive"]
        assert (held["version"], held["size"]) == (version(archive), len(archive)), held
        assert reply.parts.get(version(archive)) == archive, "the archive's bytes, as sent"
        alone = api.get(f"storages/{api.store()}/blobs/{pid}/{hex_of(archive)}")
        assert (alone.status, alone.body) == (200, archive)

    kept(first)
    change = {"path": "bundle/", "replaces": version(first), **ref(second, api.store()),
              "bundle": {"bundle_id": "kit-bundle", "bundle_version": "2.0"}}  # fmt: skip
    [result] = ok(project.push([change], [second]))["results"]
    assert (result["status"], result.get("version")) == ("saved", version(second)), result
    kept(second)
    if server_target.offers(api.base_url, "P3"):  # P3 too, when the server offers it
        with server_target.unrecorded():
            since = project.since(token)
        assert since["bundle"]["archive"]["version"] == version(second), since.get("bundle")


@pytest.mark.rule("P4.tests")
def test_p4_lists_each_test_with_its_id_format_and_release_without_files(api):
    """Raw HTTP: the app's client ignores ``files`` in P4's list."""
    code = json.dumps({"active": "code", "test_id": T2_ID}) + "\n"
    files = {**a_test(), "tests/T2/test.json": code, "tests/T2/test.py": "print('T2')\n"}
    project = api.create(files)
    held = project.read()["files"]
    named = {p: held[p]["version"] for p in ("tests/T1/test.json", "tests/T1/procedure.json")}
    ok(project.release(T1_ID, a_release(named)))
    reply = api.get(project.path("tests"))
    assert reply.status == 200 and reply.content_type == "application/json", dict(reply.headers)
    listed = {test["test"]: test for test in reply.json["tests"]}
    assert set(listed) == {"T1", "T2"}, listed
    for test in listed.values():
        assert "files" not in test, f"P4 lists {test['test']} with its files"
        assert {"test", "test_id", "format"} <= set(test), test
    assert (listed["T1"]["test_id"], listed["T1"]["format"]) == (T1_ID, "text")
    assert (listed["T2"]["test_id"], listed["T2"]["format"]) == (T2_ID, "code")
    assert listed["T1"]["latest_release"] == 1 and "latest_release" not in listed["T2"]


@pytest.mark.rule("5.3.format-neither-is-code")
def test_a_test_json_naming_neither_text_nor_code_is_code(api):
    def marker(test_id: str, **more: Any) -> str:
        return json.dumps({"test_id": test_id, **more}) + "\n"

    files = {
        "tests/T1/test.json": marker(T1_ID),
        "tests/T1/procedure.json": '{"steps": []}',
        "tests/T2/test.json": marker(T2_ID, active="graph"),
        "tests/T2/procedure.json": '{"steps": []}',
        "tests/T3/test.json": marker(T3_ID, active="text"),
        "tests/T3/procedure.json": '{"steps": []}',
    }
    project = api.create(files)
    if server_target.offers(api.base_url, "P4"):  # P4's list too, when the server offers it
        with server_target.unrecorded():
            listed = {t["test"]: t["format"] for t in ok(api.get(project.path("tests")))["tests"]}
        assert listed == {"T1": "code", "T2": "code", "T3": "text"}, listed
    for name, expected in (("T1", "code"), ("T2", "code"), ("T3", "text")):
        assert ok(api.get(project.path(f"tests/{name}")))["format"] == expected, name


@pytest.mark.rule("5.2.project-store-id")
def test_a_projects_store_id_names_the_storage_its_new_files_go_to(api):
    """Absent, the server's active storage (S1); given, one S1 lists. A new file written there
    is taken and reads back from it."""
    project = api.create(a_test())
    whole = project.read()
    storages = {s["store_id"]: s for s in ok(api.get("storages"))["storages"]}
    store = whole.get("store_id")
    if store is None:
        [store] = [sid for sid, s in storages.items() if s["active"] is True]
    assert isinstance(store, int) and not isinstance(store, bool), whole.get("store_id")
    assert store in storages, f"the project's store_id {store} names no storage S1 lists"
    if storages[store]["backend"] != "api":
        pytest.skip("the project's files go to a storage the kit cannot write to")
    data = binary(700)
    change = {"path": "documents/new.bin", "replaces": None, **ref(data, store)}
    assert [r["status"] for r in ok(project.push([change], [data]))["results"]] == ["saved"]
    reply = api.get(project.path())
    assert reply.json["files"]["documents/new.bin"]["store_id"] == store
    assert reply.parts.get(version(data)) == data


@pytest.mark.rule("5.4.release-missing")
def test_a_releases_document_the_server_cannot_give_is_missing_by_its_name(api):
    running = api.reference()
    picture, gone = binary(800), binary(500)
    files = {**a_test(), "tests/T1/pic.png": picture, "tests/T1/scope/trace.bin": gone}
    project = api.create(files)
    held = project.read()["files"]
    ok(project.release(T1_ID, a_release({p: held[p]["version"] for p in files})))
    blobs = running.state.blobs
    blobs.path(project.pid, hex_of(picture)).write_bytes(b"not the bytes")
    blobs.path(project.pid, hex_of(gone)).unlink()
    expected = {"pic.png": "damaged", "scope/trace.bin": "not_found"}
    for reply in (api.get(project.path(f"releases/{T1_ID}/1")),
                  api.post(project.path(f"releases/{T1_ID}/1/read"), {"have": []})):  # fmt: skip
        release = ok(reply)
        assert release.get("missing") == expected, release.get("missing")
        assert {"pic.png", "scope/trace.bin", "procedure.json"} <= set(release["documents"])
        assert reply.parts == {}, "a part of bytes the server cannot give"
    assert project.read()["releases"][T1_ID][0].get("missing") == expected


@pytest.mark.rule("5.4.numbered-before-releases")
def test_a_number_carried_before_releases_is_kept_and_the_next_release_follows_it(api):
    """An importer files a release a test carried before releases existed (§6.7)."""
    project = api.create(a_test())
    sent = {
        "import": True,
        "version": 4,
        "released_at": "2020-03-03T03:03:03Z",
        "test": "T1",
        "format": "text",
        "fingerprint": a_hex(),
        "content": a_hex(),
        "numbered_before_releases": True,
        "documents": {},
    }
    made = ok(project.release(T1_ID, sent, role="importer"))
    assert (made["version"], made.get("numbered_before_releases")) == (4, True), made
    [listed] = project.releases(T1_ID)
    assert listed.get("numbered_before_releases") is True, listed
    [one] = project.releases(T1_ID)  # with its documents
    assert one.get("numbered_before_releases") is True, one
    assert project.read()["releases"][T1_ID][0].get("numbered_before_releases") is True
    held = project.read()["files"]
    named = {p: held[p]["version"] for p in a_test()}
    after = ok(project.release(T1_ID, a_release(named)))
    assert after["version"] == 5 and "numbered_before_releases" not in after, after


# ---- §6.5 The library ---------------------------------------------------------------------------


def item_body(api: Api, kind: str, item_id: str, item_version: str, archive: bytes, **more: Any):
    return {
        "format_version": 1,
        "kind": kind,
        "id": item_id,
        "version": item_version,
        "content_sha256": hex_of(archive + b"/content"),
        "content_bytes": len(archive),
        "contains_code": False,
        "manifest": {"id": item_id, "version": item_version},
        "machine_id": MACHINE,
        "archive": ref(archive, api.store()),
        **more,
    }


def publish(
    api: Api,
    kind: str,
    item_id: str,
    item_version: str,
    archive: bytes,
    *,
    role: str = "bench",
    **more: Any,
) -> Reply:
    body = item_body(api, kind, item_id, item_version, archive, **more)
    return api.post_form(f"library/{kind}", body, [archive], role=role)


@pytest.mark.rule("L1.order", "L1.light", "5.16.item-roundtrip")
def test_l1_lists_by_kind_id_and_publication_without_manifests(api):
    base = f"kit-{server_target.fresh_suffix()}"
    manifest = {"version": "1.0", "id": f"{base}-a", "zeta": {"b": 1, "a": 2}, "packs": []}
    published = [
        ("bundle", f"{base}-a", "1.0", {"manifest": manifest}),
        ("config", f"{base}-m", "1.0", {}),
        ("bundle", f"{base}-Z", "2.0", {}),
        ("bundle", f"{base}-Z", "1.0", {}),
    ]
    archives = {}
    for kind, item_id, item_version, more in published:
        archives[item_id, item_version] = binary(200)
        ok(publish(api, kind, item_id, item_version, archives[item_id, item_version], **more))

    def mine(items: list[dict]) -> list[tuple]:
        for item in items:
            if item["id"].startswith(base):
                assert "manifest" not in item, item
        return [(i["kind"], i["id"], i["version"]) for i in items if i["id"].startswith(base)]

    expected = [
        ("bundle", f"{base}-Z", "2.0"),
        ("bundle", f"{base}-Z", "1.0"),
        ("bundle", f"{base}-a", "1.0"),
        ("config", f"{base}-m", "1.0"),
    ]
    assert mine(ok(api.get("library"))["items"]) == expected
    assert mine(ok(api.get("library", {"kind": "bundle"}))["items"]) == expected[:3]
    reply = api.get(f"library/bundle/{base}-a/1.0")
    item = reply.json
    assert list(item["manifest"].items()) == list(manifest.items()), "the manifest, verbatim"
    assert list(item["manifest"]["zeta"]) == ["b", "a"]
    archive = archives[f"{base}-a", "1.0"]
    assert (item["content_bytes"], item["contains_code"]) == (len(archive), False)
    assert item["published_by"] == api.user(), "published_by is the login"
    assert item["machine_id"] == MACHINE
    assert reply.parts == {version(archive): archive}


@pytest.mark.rule("L1.kind-order")
def test_l1_orders_the_kinds_by_their_names_not_by_the_table(api):
    """§6.5 L1: kinds by their names' code points (``capability`` before ``export`` before
    ``report``), where 5.16's table lists ``report``, ``export``, ``capability``."""
    base = f"kit-{server_target.fresh_suffix()}"
    for kind in ("report", "export", "capability"):
        more = {"contains_code": True} if kind == "capability" else {}
        ok(publish(api, kind, f"{base}-{kind}", "1.0", binary(200), **more))
    items = ok(api.get("library"))["items"]
    kinds = [item["kind"] for item in items if item["id"].startswith(base)]
    assert kinds == ["capability", "export", "report"], kinds


@pytest.mark.rule("L1.kind-400")
def test_l1_refuses_a_kind_that_is_none(api):
    refused(api.get("library", {"kind": "nope"}), 400, "bad_request")


@pytest.mark.rule("5.16.created-once")
def test_a_library_version_is_created_once(api):
    item_id = f"kit-{server_target.fresh_suffix()}"
    archive = binary(300)
    first = ok(publish(api, "config", item_id, "1.0", archive))
    assert first["status"] == "saved"
    again = ok(publish(api, "config", item_id, "1.0", archive))
    assert again["status"] == "already" and again["item"]["content_sha256"] == hex_of(
        archive + b"/content"
    )
    other = binary(300)
    error = refused(publish(api, "config", item_id, "1.0", other), 409, "version_exists")
    assert error["details"]["content_sha256"] == hex_of(archive + b"/content"), error
    got = ok(api.get(f"library/config/{item_id}/1.0"))
    assert got["content_sha256"] == hex_of(archive + b"/content")


@pytest.mark.rule("5.16.case-twin-409", "2.4.exact-exceptions")
def test_a_library_version_differing_only_by_case_is_409_exists(api):
    item_id = f"kit-{server_target.fresh_suffix()}-x"
    ok(publish(api, "config", item_id, "1.0a", binary(300)))
    refused(publish(api, "config", item_id.upper(), "1.0A", binary(300)), 409, "exists")
    refused(api.get(f"library/config/{item_id.upper()}/1.0A"), 404, "not_found")


@pytest.mark.rule("5.16.contains-code-400")
def test_contains_code_false_for_a_kind_that_holds_code_is_400(api):
    for kind in ("capability", "wizard", "tool", "package"):
        item_id = f"kit-{server_target.fresh_suffix()}"
        refused(publish(api, kind, item_id, "1.0", binary(200)), 400, "bad_request")
        refused(api.get(f"library/{kind}/{item_id}/1.0"), 404, "not_found")


@pytest.mark.rule("5.16.code-needs-publisher")
def test_only_a_publisher_publishes_an_item_holding_code(api):
    item_id = f"kit-{server_target.fresh_suffix()}"
    reply = publish(
        api, "config", item_id, "1.0", binary(200), role="bench_only", contains_code=True
    )
    refused(reply, 403, "forbidden")
    refused(api.get(f"library/config/{item_id}/1.0"), 404, "not_found")
    assert (
        ok(publish(api, "config", item_id, "1.0", binary(200), role="bench_only"))["status"]
        == "saved"
    )
    reply = publish(api, "config", f"{item_id}-m", "1.0", binary(200), role="manager")
    refused(reply, 403, "forbidden")


# ---- §6.6, §7 Storages and large files --------------------------------------------------------


@pytest.mark.rule("S1.storages", "5.15.storage-fields", "7.1.params-no-secret")
def test_s1_names_this_server_and_its_storages_without_a_secret(api):
    """Its storages, a declared one among them when the server offers S2."""
    if server_target.offers(api.base_url, "S2"):
        with server_target.unrecorded():
            a_declared_storage(api)
    answer = ok(api.get("storages"))
    assert isinstance(answer["instance_id"], str) and answer["instance_id"], answer
    storages = answer["storages"]
    assert sum(1 for s in storages if s["active"] is True) == 1, "one active storage"
    for storage in storages:
        assert storage["format_version"] == 1
        assert isinstance(storage["store_id"], int) and not isinstance(storage["store_id"], bool)
        assert storage["backend"] in ("api", "filesystem", "ftps", "sftp"), storage
        assert isinstance(storage["identity"], str) and storage["identity"], storage
        for flag in ("active", "declared", "reachable"):
            assert isinstance(storage[flag], bool), (flag, storage)
        if storage["declared"]:
            assert storage["active"] is False, storage
        params = storage["params"]
        assert all(isinstance(value, str) for value in params.values()), storage
        assert not [k for k in params if k.casefold() in SECRET_NAMES], storage
    for storage in storages:  # §7.1: a reference into one the server reaches itself is checked
        if storage["backend"] != "api" and not storage["declared"] and storage["reachable"]:
            project = api.create(a_test())
            absent = binary(300)
            change = {"path": "documents/a.bin", "replaces": None}
            change.update(ref(absent, storage["store_id"]))
            refused(project.push([change]), 409, "missing_files")


@pytest.mark.rule("S2.declare", "2.4.exact-exceptions")
def test_s2_declares_a_storage_once_by_its_exact_identity(api):
    host = f"kit-{server_target.fresh_suffix()}.invalid"
    params = {"host": host, "port": "22", "base_path": "/kit", "user": "kit", "auth": "key"}
    body = {
        "backend": "sftp",
        "params": {**params, "custom": "kept"},
        "identity": f"sftp://{host}:22/kit",
    }
    first = ok(api.post("storages", body))
    assert first["status"] == "saved"
    storage = first["storage"]
    assert (storage["declared"], storage["active"], storage["reachable"]) == (True, False, False)
    assert storage["params"] == body["params"] and storage["identity"] == body["identity"]
    again = ok(api.post("storages", {**body, "params": {**params, "user": "other"}}))
    assert again["status"] == "already"
    assert again["storage"]["store_id"] == storage["store_id"]
    assert again["storage"]["params"] == body["params"], "the params first declared"
    upper = ok(api.post("storages", {**body, "identity": f"sftp://{host.upper()}:22/kit"}))
    assert upper["status"] == "saved" and upper["storage"]["store_id"] != storage["store_id"]
    listed = {s["store_id"] for s in ok(api.get("storages"))["storages"]}
    assert {storage["store_id"], upper["storage"]["store_id"]} <= listed


@pytest.mark.rule("S2.reachable-as-found")
def test_s2_declares_a_storage_it_cannot_open_and_says_it_does_not_reach_it(api):
    """§7.3 S2: a host that never resolves (``.invalid``, RFC 6761) and a folder the server does
    not have are declared all the same (``saved``), ``reachable: false``, and stay so."""
    suffix = server_target.fresh_suffix()
    host, folder = f"kit-{suffix}.invalid", f"/kit-nowhere-{suffix}/evidence"
    params = {"host": host, "port": "22", "base_path": "/kit", "user": "kit", "auth": "key"}
    declarations = [
        {"backend": "sftp", "params": params, "identity": f"sftp://{host}:22/kit"},
        {"backend": "filesystem", "params": {"root": folder}, "identity": folder},
    ]
    for body in declarations:
        first = ok(api.post("storages", body))
        assert (first["status"], first["storage"]["reachable"]) == ("saved", False), first
        again = ok(api.post("storages", body))
        assert (again["status"], again["storage"]["reachable"]) == ("already", False), again
        [listed] = [
            s
            for s in ok(api.get("storages"))["storages"]
            if s["store_id"] == first["storage"]["store_id"]
        ]
        assert listed["reachable"] is False, listed


@pytest.mark.rule("S2.params-strings-400", "S2.secret-key-400")
def test_s2_refuses_params_that_are_not_strings_or_name_a_secret(api):
    identities = []
    for extra in ({"port": 22}, {"Password": "x"}, {"PRIVATE_KEY": "x"}, {"Token": "x"}):
        host = f"kit-{server_target.fresh_suffix()}.invalid"
        identity = f"sftp://{host}:22/kit"
        identities.append(identity)
        params = {"host": host, "base_path": "/kit", "user": "kit", **extra}
        body = {"backend": "sftp", "params": params, "identity": identity}
        refused(api.post("storages", body), 400, "bad_request")
    listed = {s["identity"] for s in ok(api.get("storages"))["storages"]}
    assert not listed & set(identities)


@pytest.mark.rule("S3.to-delete", "S4.deleted", "7.3.to-delete-in-purge", "E13.to-delete-scope")
def test_s3_and_s4_settle_the_blobs_nothing_references_in_a_declared_storage(api):
    far = a_declared_storage(api)
    sid = far["store_id"]
    one, two, three, never = binary(100), binary(110), binary(120), binary(130)
    files = {f"documents/{n}.bin": d for n, d in (("one", one), ("two", two), ("three", three))}
    pid = server_target.fresh_id()
    body, parts = project_body(api, pid, f"Ripple {server_target.fresh_suffix()}", a_test())
    for path, data in files.items():
        body["files"][path] = ref(data, sid)
    ok(api.post_form("projects", body, parts))
    project = Project(api, pid, body["name"], Reply(200, Message(), b""))
    deletes = [{"path": p, "replaces": version(d), "delete": True} for p, d in files.items()]
    assert {r["status"] for r in ok(project.push(deletes))["results"]} == {"saved"}

    def listed() -> set[str]:
        return set(ok(api.get(f"storages/{sid}/to_delete"))["to_delete"].get(pid, []))

    assert listed() == {version(one), version(two), version(three)}
    again = {"path": "documents/again.bin", "replaces": None, **ref(two, sid)}
    assert ok(project.push([again]))["results"][0]["status"] == "saved"
    aside = [version(one), version(two), version(never)]
    settled = ok(api.post(f"storages/{sid}/deleted", {"set_aside": {pid: aside}}))
    assert set(settled["delete"].get(pid, [])) == {version(one)}
    assert set(settled["keep"].get(pid, [])) == {version(two), version(never)}
    assert set(settled["to_delete"].get(pid, [])) == {version(three)}
    assert listed() == {version(three)}
    purged = ok(api.post(project.path("purge"), {"machine_id": MACHINE}, role="manager"))
    assert purged["to_delete"] == {str(sid): [version(three)]}, purged


@pytest.mark.rule("E13.to-delete-absent")
def test_e13_leaves_to_delete_out_when_there_is_none(api):
    """§6.4 E13: no blob of the project unreferenced in a declared storage, no ``to_delete``
    (not ``{}``, no storage with an empty list): before any, and once S4 settled the one
    there was."""
    sid = a_declared_storage(api)["store_id"]
    data = binary(100)
    pid = server_target.fresh_id()
    body, parts = project_body(api, pid, f"Ripple {server_target.fresh_suffix()}", a_test())
    body["files"]["documents/one.bin"] = ref(data, sid)
    ok(api.post_form("projects", body, parts))
    purge = (f"projects/{pid}/purge", {"machine_id": MACHINE})
    nothing = ok(api.post(*purge, role="manager"))
    assert "to_delete" not in nothing, nothing
    project = Project(api, pid, body["name"], Reply(200, Message(), b""))
    gone = {"path": "documents/one.bin", "replaces": version(data), "delete": True}
    assert ok(project.push([gone]))["results"][0]["status"] == "saved"
    assert ok(api.post(*purge, role="manager"))["to_delete"] == {str(sid): [version(data)]}
    settled = ok(api.post(f"storages/{sid}/deleted", {"set_aside": {pid: [version(data)]}}))
    assert settled["delete"] == {pid: [version(data)]}, settled
    after = ok(api.post(*purge, role="manager"))
    assert "to_delete" not in after, after


@pytest.mark.rule("S3S4.declared-only")
def test_s3_and_s4_take_a_declared_storage_only(api):
    unknown = max(s["store_id"] for s in ok(api.get("storages"))["storages"]) + 1_000_000
    own = api.store()
    refused(api.get(f"storages/{unknown}/to_delete"), 404, "not_found")
    refused(api.post(f"storages/{unknown}/deleted", {"set_aside": {}}), 404, "not_found")
    refused(api.get(f"storages/{own}/to_delete"), 400, "bad_request")
    refused(api.post(f"storages/{own}/deleted", {"set_aside": {}}), 400, "bad_request")


@pytest.mark.rule("B1.already")
def test_b1_saves_bytes_once_then_answers_already(api):
    owner, data = server_target.fresh_id(), binary(2000)
    answered = ("status", "version", "size")
    first = ok(api.upload(owner, data))
    assert {k: first.get(k) for k in answered} == {
        "status": "saved",
        "version": version(data),
        "size": len(data),
    }, first
    again = ok(api.upload(owner, data))
    assert {k: again.get(k) for k in answered} == {
        "status": "already",
        "version": version(data),
        "size": len(data),
    }, again


@pytest.mark.rule("B1.content-type-400", "B1.bare-sha-400", "B1.api-storage-only", "B1.digest-422")
def test_b1_and_b2_refuse_what_is_not_one_file_in_the_api_storage(api):
    owner, data = server_target.fresh_id(), binary(1500)
    blob = f"storages/{api.store()}/blobs/{owner}"
    for content_type in ("text/plain", "application/json"):
        reply = api.request("POST", f"{blob}/{hex_of(data)}", body=data, content_type=content_type)
        refused(reply, 400, "bad_request")
    refused(api.get(f"{blob}/{hex_of(data)}"), 404, "not_found")
    octet = "application/octet-stream"
    reply = api.request("POST", f"{blob}/{version(data)}", body=data, content_type=octet)
    refused(reply, 400, "bad_request")
    refused(api.get(f"{blob}/{hex_of(data)}"), 404, "not_found")
    wrong = binary(1500)
    reply = api.request("POST", f"{blob}/{hex_of(wrong)}", body=data, content_type=octet)
    refused(reply, 422, "digest_mismatch")
    refused(api.get(f"{blob}/{hex_of(wrong)}"), 404, "not_found")
    listed = ok(api.get("storages"))["storages"]
    stores = [s["store_id"] for s in listed]
    checked = [(max(stores) + 1_000_000, 404, "not_found")]
    others = [s["store_id"] for s in listed if s["backend"] != "api"]
    if others:  # another backend S1 lists (the server's own share, say)
        checked.append((others[0], 400, "bad_request"))
    elif server_target.offers(api.base_url, "S2"):  # another backend: a declared storage
        with server_target.unrecorded():
            checked.append((a_declared_storage(api)["store_id"], 400, "bad_request"))
    for store_id, status, code in checked:
        path = f"storages/{store_id}/blobs/{owner}/{hex_of(data)}"
        refused(api.request("POST", path, body=data, content_type=octet), status, code)
        refused(api.get(path), status, code)
    ok(api.upload(owner, data))
    refused(api.get(f"{blob}/{version(data)}"), 400, "bad_request")


@pytest.mark.rule("B1.owner-before-project")
def test_b1_takes_a_file_for_a_project_not_held_yet_and_the_json_after_finds_it(api):
    """§6.6 B1: a large file goes before the JSON, so for a project the server holds nothing
    for yet: ``saved``; P11 creating that project then references it without a part, as E1
    does for a project's first run; B2 reads both back under their owner."""
    store = api.store()
    pid, big = server_target.fresh_id(), binary(MIB + 100)
    assert ok(api.upload(pid, big))["status"] == "saved"
    body, parts = project_body(api, pid, f"Ripple {server_target.fresh_suffix()}", a_test())
    body["files"]["documents/big.bin"] = ref(big, store)
    made = ok(api.post_form("projects", body, parts))
    assert "missing" not in made, made
    assert api.get(f"storages/{store}/blobs/{pid}/{hex_of(big)}").body == big
    first, log = server_target.fresh_id(), binary(MIB + 200)
    assert ok(api.upload(first, log))["status"] == "saved"
    run = a_run(1, files=[run_file(api, log, "big.log")])
    assert ok(api.post(f"projects/{first}/runs", run))["status"] == "saved"
    read = ok(api.get(f"projects/{first}/runs/{run['run_id']}"))
    assert "missing" not in read, read
    assert api.get(f"storages/{store}/blobs/{first}/{hex_of(log)}").body == log


@pytest.mark.rule("B1.roles-403")
def test_b1_takes_a_projects_bytes_from_bench_or_importer_and_the_librarys_from_bench(api):
    owner = server_target.fresh_id()
    for target in (owner, "library"):
        refused(api.upload(target, binary(500), role="manager"), 403, "forbidden")
    if server_target.offers(api.base_url, "import"):  # the importer role: only with import
        assert ok(api.upload(owner, binary(500), role="importer"))["status"] == "saved"
        refused(api.upload("library", binary(500), role="importer"), 403, "forbidden")
    elif not server_target.external() or os.environ.get(server_target.TOKENS["importer"][0]):
        # a server taking no import grants no importer: that login uploads nothing (§3.5)
        refused(api.upload(owner, binary(500), role="importer"), 403, "forbidden")
    assert ok(api.upload(owner, binary(500), role="bench_only"))["status"] == "saved"
    assert ok(api.upload("library", binary(500), role="bench_only"))["status"] == "saved"


@pytest.mark.rule("B1.max-file-413")
def test_b1_over_max_file_bytes_is_413_before_it_is_sent(api):
    """A file over ``max_file_bytes`` is refused from its head alone, for an owner no project
    holds yet (B1 needs none: ``B1.owner-before-project``), so any server checks it."""
    limits = ok(api.get("info"))["limits"]
    owner = server_target.fresh_id()
    path = f"storages/{api.store()}/blobs/{owner}/{'a' * 64}"
    size = limits["max_file_bytes"] + 1
    reply = api.headers_only(path, size, "application/octet-stream")
    error = refused(reply, 413, "too_large")
    assert error["details"]["max_file_bytes"] == limits["max_file_bytes"], error


@pytest.mark.rule("3.6.too-large-before-body")
def test_a_body_over_its_limit_is_413_before_it_is_sent(api):
    limits = ok(api.get("info"))["limits"]
    project = api.create(a_test())
    reply = api.headers_only(
        project.path("changes"), limits["max_upload_bytes"] + 1, "application/json"
    )
    error = refused(reply, 413, "too_large")
    assert error["details"]["max_upload_bytes"] == limits["max_upload_bytes"], error


@pytest.mark.rule("B1.max-file-413")
def test_b1_is_held_to_max_file_bytes_never_to_max_upload_bytes(api):
    with api.limited(max_upload_bytes=2 * MIB, max_file_bytes=3 * MIB) as small:
        owner = server_target.fresh_id()
        data = binary(2 * MIB + MIB // 2)
        assert ok(small.upload(owner, data))["status"] == "saved"
        error = refused(small.upload(owner, binary(3 * MIB + 1)), 413, "too_large")
        assert error["details"]["max_file_bytes"] == 3 * MIB, error


@pytest.mark.rule("B2.range", "4.3.416")
def test_b2_answers_one_range_206_and_past_the_end_416(api):
    owner, data = server_target.fresh_id(), binary(1000)
    ok(api.upload(owner, data))
    path = f"storages/{api.store()}/blobs/{owner}/{hex_of(data)}"
    whole = api.get(path)
    assert (whole.status, whole.body, whole.content_type) == (200, data, "application/octet-stream")
    assert whole.headers.get("Accept-Ranges") == "bytes"
    for asked, first, last in (
        ("bytes=10-19", 10, 19),
        ("bytes=990-", 990, 999),
        ("bytes=-5", 995, 999),
    ):
        part = api.get(path, headers={"Range": asked})
        assert part.status == 206, (asked, part.status)
        assert part.headers.get("Content-Range") == f"bytes {first}-{last}/1000", asked
        assert part.body == data[first : last + 1], asked
    several = api.get(path, headers={"Range": "bytes=0-1,4-5"})
    assert (several.status, several.body) == (200, data)
    past = api.get(path, headers={"Range": "bytes=1000-"})
    refused(past, 416, "range_not_satisfiable")
    assert past.headers.get("Content-Range") == "bytes */1000"


@pytest.mark.rule("B2.any-size")
def test_b2_answers_a_file_of_at_most_one_mib_too(api):
    small = binary(400)
    project = api.create({**a_test(), "documents/small.bin": small})
    reply = api.get(f"storages/{api.store()}/blobs/{project.pid}/{hex_of(small)}")
    assert (reply.status, reply.body) == (200, small)
    if not server_target.offers(api.base_url, "L3"):  # a library archive, with the library
        return
    archive, item_id = binary(300), f"kit-{server_target.fresh_suffix()}"
    with server_target.unrecorded():
        ok(publish(api, "config", item_id, "1.0", archive))
    reply = api.get(f"storages/{api.store()}/blobs/library/{hex_of(archive)}")
    assert (reply.status, reply.body) == (200, archive)


def put_blob(root: Path, owner: str, data: bytes) -> Path:
    """Write *data* into a file storage as the app does (§7.2): ``<root>/<owner>/<sha[0:2]>/<sha>``."""
    target = root / owner / hex_of(data)[:2] / hex_of(data)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target


def writable_root(storage: dict, instance_id: str) -> Path | None:
    """The folder of a ``filesystem`` storage of this server the kit can write to as the app
    does: there, and marked for this server (§7.2); else None."""
    root = storage.get("params", {}).get("root")
    if storage["backend"] != "filesystem" or not root or not Path(root).is_dir():
        return None
    try:
        marker = json.loads((Path(root) / "procedo-attachments.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return Path(root) if marker.get("instance_id") == instance_id else None


def check_own_storage_references(api: Api, storage: dict, instance_id: str) -> None:
    sid = storage["store_id"]
    project = api.create(a_test())
    before = project.mark()
    absent = binary(300)
    change = {"path": "documents/own.bin", "replaces": None, **ref(absent, sid)}
    error = refused(project.push([change]), 409, "missing_files")
    assert error["details"]["missing"] == [version(absent)], error
    project.nothing_since(before)
    run = a_run(1, files=[{**run_file(api, absent, "own.bin"), "store_id": sid}])
    error = refused(api.post(project.path("runs"), run), 409, "missing_files")
    assert error["details"]["missing"] == [version(absent)], error
    refused(api.get(project.path(f"runs/{run['run_id']}")), 404, "not_found")
    root = writable_root(storage, instance_id)
    if root is None:
        return  # the kit cannot put the bytes there: the refusal is what it checks
    put_blob(root, project.pid, absent)
    assert [r["status"] for r in ok(project.push([change]))["results"]] == ["saved"]
    assert project.read()["files"]["documents/own.bin"]["store_id"] == sid
    assert ok(api.post(project.path("runs"), run))["status"] == "saved"


@pytest.mark.rule("7.1.references-checked")
def test_a_reference_into_the_servers_own_file_storage_is_checked(api):
    """§7.1: a reference into one of the server's own storages (not ``api``) to absent bytes
    is ``409 missing_files``, nothing written; the bytes put there, it is taken. A server
    keeping every byte itself (one ``api`` storage) skips 7.1, and so does this case; the
    reference server is started with an own ``filesystem`` storage."""
    answer = ok(api.get("storages"))
    own = [s for s in answer["storages"] if s["backend"] != "api" and not s["declared"]]
    if own:
        for storage in own:
            check_own_storage_references(api, storage, answer["instance_id"])
        return
    if server_target.external():
        pytest.skip("this server keeps every byte itself (no own storage but api): §7.1 is not its")
    assert api.root is not None
    folder = api.root / f"own-{secrets.token_hex(4)}"
    with api.another("an own file storage", own_storage=folder) as other:
        answer = ok(other.get("storages"))
        [storage] = [s for s in answer["storages"] if s["backend"] != "api"]
        assert writable_root(storage, answer["instance_id"]) is not None, storage
        check_own_storage_references(other, storage, answer["instance_id"])


def check_never_cleaned(api: Api, folder: Path) -> None:
    pid = server_target.fresh_id()
    one, two, three = binary(400), binary(500), binary(600)
    kept = {hex_of(d): put_blob(folder, pid, d) for d in (one, two, three)}
    identity = str(folder)
    body = {"backend": "filesystem", "params": {"root": identity}, "identity": identity}
    storage = ok(api.post("storages", body))["storage"]
    sid = storage["store_id"]
    assert storage["declared"] is True, storage
    if storage["reachable"]:  # it reads that storage: it checks the references into it
        absent = binary(300)
        made = api.create(a_test())
        change = {"path": "documents/absent.bin", "replaces": None, **ref(absent, sid)}
        refused(made.push([change]), 409, "missing_files")
    name = f"Ripple bench {server_target.fresh_suffix()}"
    body, parts = project_body(api, pid, name, a_test())
    body["files"]["documents/one.bin"] = ref(one, sid)
    body["files"]["documents/two.bin"] = ref(two, sid)
    ok(api.post_form("projects", body, parts))
    project = Project(api, pid, name, Reply(200, Message(), b""))
    run = a_run(1, files=[{**run_file(api, three, "three.bin"), "store_id": sid}])
    assert ok(api.post(project.path("runs"), run))["status"] == "saved"
    deletes = [{"path": f"documents/{n}.bin", "replaces": version(d), "delete": True}
               for n, d in (("one", one), ("two", two))]  # fmt: skip
    assert {r["status"] for r in ok(project.push(deletes))["results"]} == {"saved"}
    ok(api.post(project.path(f"runs/{run['run_id']}/delete"),
                {"machine_id": MACHINE, "reason": "Wrong board"}, role="manager"))  # fmt: skip
    purged = ok(api.post(project.path("purge"), {"machine_id": MACHINE, "items": [
        {"run_id": run["run_id"]}]}, role="manager"))  # fmt: skip
    assert purged["purged"]["runs"] == [run["run_id"]], purged
    listed = ok(api.get(f"storages/{sid}/to_delete"))["to_delete"].get(pid, [])
    assert set(listed) == {version(one), version(two), version(three)}, listed
    for sha, path in kept.items():
        assert path.is_file(), f"the server deleted {path.name} in a storage the app declared"
        assert hex_of(path.read_bytes()) == sha, f"the server changed {path.name}"


@pytest.mark.rule("7.3.declared-never-cleaned")
def test_the_server_never_deletes_in_a_storage_the_app_declares(api):
    """§7.3: blobs nothing references any more in a declared storage are listed for the app to
    delete (S3), never deleted by the server, even one it reaches. The case declares a folder of
    its own; the reference server is started mounting it, so it reaches it."""
    assert api.root is not None
    if server_target.external():
        folder = api.root / f"declared-{secrets.token_hex(4)}"
        folder.mkdir()
        check_never_cleaned(api, folder)
        return
    mounted = api.root / f"mounted-{secrets.token_hex(4)}"
    with api.another("it mounts the folder the case declares", reaches=(mounted,)) as other:
        folder = mounted / "declared"
        folder.mkdir(parents=True)
        check_never_cleaned(other, folder)


# ---- §6.7 The import --------------------------------------------------------------------------


def two_runs(api: Api, pid: str) -> tuple[dict, dict]:
    """Two complete runs of T1 pushed by the bench, the second current."""
    older, newer = a_run(1), a_run(2)
    for run in (older, newer):
        ok(api.post(f"projects/{pid}/runs", run))
    assert pointer_of(api, pid)["run_id"] == newer["run_id"]
    return older, newer


@pytest.mark.rule("import.role-and-flag", "import.E1-no-pointer", "import.bench-seen-not-imported")
def test_an_imported_run_keeps_its_pushed_at_and_moves_no_pointer(api):
    """§6.7: the run keeps the ``pushed_at`` sent; on a server offering E6 it moves no pointer
    (one not offering E6 picks the current run itself: ``test_server_contract_calls``)."""
    pid, machine = server_target.fresh_id(), secrets.token_hex(4)
    started = api.server_now()
    run = {**a_run(3, machine_id=machine), "import": True, "pushed_at": "2020-01-02T03:04:05Z"}
    answer = ok(api.post(f"projects/{pid}/runs", run, role="importer"))
    assert answer["status"] == "saved", answer
    read = ok(api.get(f"projects/{pid}/runs/{run['run_id']}"))
    assert read["pushed_at"] == "2020-01-02T03:04:05Z", read
    if server_target.offers(api.base_url, "E6"):  # the importer sets the pointers itself
        assert answer["current"] is False and read["current"] is False, (answer, read)
        assert pointer_of(api, pid) is None, "an imported run moved the pointer"
    if server_target.offers(api.base_url, "E14"):  # a bench's times, with the benches
        with server_target.unrecorded():
            benches = ok(api.get("benches"))["benches"]
        [bench] = [b for b in benches if b["machine_id"] == machine]
        assert bench["first_seen"] >= started, ("first_seen is the server's own", bench)


@pytest.mark.rule("5.13.incomplete-never-current")
def test_an_imported_incomplete_run_never_becomes_current(api):
    """A stopped run is never current (5.13), imported too, with E6 or without (§6.7): an
    imported incomplete run, alone of its test on its board, is not current, and E5 names no
    current run for its test, with ``include=current`` too."""
    pid = server_target.fresh_id()
    run = {**a_run(3), "complete": False, "import": True, "pushed_at": "2020-01-02T03:04:05Z"}
    del run["ended"], run["verdict"]
    answer = ok(api.post(f"projects/{pid}/runs", run, role="importer"))
    assert (answer["status"], answer["current"]) == ("saved", False), answer
    assert ok(api.get(f"projects/{pid}/runs/{run['run_id']}"))["current"] is False
    assert pointer_of(api, pid) is None, "an imported incomplete run became current"
    full = ok(api.get(f"projects/{pid}/boards/{SERIAL}", {**board_query(), "include": "current"}))
    current = [r["run_id"] for r in full.get("current_runs", [])]
    assert run["run_id"] not in current, ("an imported incomplete run is a current run", full)


@pytest.mark.rule("3.5.importer-beside-bench")
def test_a_login_holding_importer_beside_bench_imports_and_pushes_as_a_bench(api):
    """§3.5: the app moves data in with the login it always signs in with, once the server's
    administrator has given it the importer role beside bench."""
    pid = server_target.fresh_id()
    run = {**a_run(3), "import": True, "pushed_at": "2020-01-02T03:04:05Z"}
    answer = ok(api.post(f"projects/{pid}/runs", run, role="mover"))
    assert answer["status"] == "saved", answer
    if server_target.offers(api.base_url, "E6"):  # without E6 the server picks: no claim here
        assert answer["current"] is False, answer
    read = ok(api.get(f"projects/{pid}/runs/{run['run_id']}"))
    assert read["pushed_at"] == "2020-01-02T03:04:05Z", ("its import was ignored", read)
    # Its board is listed, so registered on a server registering its boards (§3.3).
    boards = ok(api.get(f"projects/{pid}/boards"))["boards"]
    assert [b["serial"] for b in boards] == [SERIAL], ("an import's board not listed", boards)
    started = api.server_now()
    pushed = ok(api.post(f"projects/{pid}/runs", a_run(4), role="mover"))
    assert pushed["status"] == "saved" and pushed["current"] is True, pushed
    assert pushed["pushed_at"] >= started, ("without import it writes as a bench", pushed)


@pytest.mark.rule("import.ignored-without")
def test_the_import_fields_are_ignored_without_the_importer_role(api):
    pid = server_target.fresh_id()
    started = api.server_now()
    run = {**a_run(3), "import": True, "pushed_at": "2020-01-02T03:04:05Z"}
    # a bench's run, which a server registering its boards takes on a board it lists only
    board = (run.get("part_number"), run.get("board_revision"), run["serial"])
    server_target.register_board(api.base_url, pid, board)
    ok(api.post(f"projects/{pid}/runs", run))
    pushed = ok(api.get(f"projects/{pid}/runs/{run['run_id']}"))["pushed_at"]
    assert pushed >= started, ("a bench's import: true keeps the server's pushed_at", pushed)
    for flag in ({}, {"import": False}):
        refused(
            api.post(f"projects/{pid}/runs", {**a_run(4), **flag}, role="importer"),
            403,
            "forbidden",
        )


@pytest.mark.rule("import.never-restore-purge")
def test_an_importer_never_restores_or_purges(api):
    pid = server_target.fresh_id()
    _, newer = two_runs(api, pid)
    run = f"projects/{pid}/runs/{newer['run_id']}"
    for flag in ({}, {"import": True}):
        body = {"machine_id": MACHINE, **flag}
        refused(api.post(f"{run}/restore", body, role="importer"), 403, "not_a_manager")
        refused(api.post(f"projects/{pid}/purge", body, role="importer"), 403, "not_a_manager")


@pytest.mark.rule("import.E6-as-sent")
def test_an_imported_pointer_is_set_exactly_as_sent(api):
    pid = server_target.fresh_id()
    older, newer = two_runs(api, pid)
    started = api.server_now()
    current = f"projects/{pid}/boards/{SERIAL}/tests/{T1_ID}/current"
    sent = {
        "import": True,
        "run_id": older["run_id"],
        "ended": "2020-05-05T05:05:05Z",
        "machine_id": "abcdef01",
        "updated_at": "2020-06-06T06:06:06Z",
        "replaces": {"run_id": newer["run_id"], "ended": "1999-01-01T00:00:00Z"},
    }
    answer = ok(api.post(current, sent, board_query(), role="importer"))
    expected = {k: sent[k] for k in ("run_id", "ended", "machine_id", "updated_at")}
    assert answer["status"] == "saved"
    assert {k: answer["pointer"].get(k) for k in expected} == expected, answer
    assert {k: pointer_of(api, pid).get(k) for k in expected} == expected
    bare = {"import": True, "run_id": newer["run_id"], "machine_id": "abcdef01"}
    pointer = ok(api.post(current, bare, board_query(), role="importer"))["pointer"]
    assert pointer["ended"] == newer["ended"] and pointer["updated_at"] >= started, pointer
    unknown = {**bare, "run_id": a_run(9)["run_id"]}
    refused(api.post(current, unknown, board_query(), role="importer"), 404, "not_found")
    stopped = {**a_run(5), "complete": False}
    del stopped["ended"], stopped["verdict"]
    ok(api.post(f"projects/{pid}/runs", stopped))
    incomplete = {**bare, "run_id": stopped["run_id"]}
    refused(api.post(current, incomplete, board_query(), role="importer"), 400, "bad_request")


def an_entry(**more: Any) -> dict:
    return {
        "entry_id": secrets.token_hex(16),
        "replaces": None,
        "by": "kit",
        "machine_id": MACHINE,
        "saved_at": "2026-04-01T10:00:00Z",
        **more,
    }


@pytest.mark.offer_aware  # E7 on any server taking imports; E15 when the server offers it
@pytest.mark.rule("import.E7-E15-received-at")
def test_imported_entries_keep_their_received_at_or_none(api):
    server_target.needs(api.base_url, "import")
    pid = server_target.fresh_id()
    run = a_run(1)
    ok(api.post(f"projects/{pid}/runs", run))
    stamped = an_entry(kind="comment", key="test", text="a", received_at="2020-01-01T00:00:00Z")
    bare = an_entry(kind="comment", key="c2", text="b", current=False)
    body = {"import": True, "entries": [stamped, bare]}
    ok(api.post(f"projects/{pid}/runs/{run['run_id']}/annotation", body, role="importer"))
    entries = ok(api.get(f"projects/{pid}/runs/{run['run_id']}"))["annotation"]["entries"]
    by_id = {e["entry_id"]: e for e in entries}
    assert by_id[stamped["entry_id"]]["received_at"] == "2020-01-01T00:00:00Z"
    assert "received_at" not in by_id[bare["entry_id"]], by_id[bare["entry_id"]]
    assert by_id[bare["entry_id"]]["current"] is True, "current is computed, never imported"
    if not server_target.offers(api.base_url, "E15"):
        return
    lot = an_entry(kind="lot", value="L1", received_at="2020-02-02T00:00:00Z")
    named = an_entry(kind="field", name="Customer", value="ACME")
    path = f"projects/{pid}/boards/{SERIAL}/entries"
    ok(api.post(path, {"import": True, "entries": [lot, named]}, board_query(), role="importer"))
    entries = {e["entry_id"]: e for e in board_results(api, pid)["board"]["entries"]}
    assert entries[lot["entry_id"]]["received_at"] == "2020-02-02T00:00:00Z"
    assert "received_at" not in entries[named["entry_id"]]


def a_report(api: Api, data: bytes, **more: Any) -> dict:
    return {
        "format_version": 2,
        "kind": "report",
        "serial": SERIAL,
        "part_number": PN,
        "board_revision": REV,
        "name": "full.html",
        "machine_id": MACHINE,
        "updated_at": "2026-04-01T10:00:00Z",
        "sha256": hex_of(data),
        "size": len(data),
        "store_id": api.store(),
        **more,
    }


@pytest.mark.rule("import.E10-generation")
def test_an_imported_report_keeps_its_generation(api):
    pid = server_target.fresh_id()
    first, other = binary(300), binary(300)
    body = {**a_report(api, first), "import": True, "generation": 3}
    made = ok(api.post_form(f"projects/{pid}/artifacts", body, [first], role="importer"))
    assert made["artifact"]["generation"] == 3, made
    query = {"serial": SERIAL, **board_query(), "generation": "3"}
    assert ok(api.get(f"projects/{pid}/artifacts/report/full.html", query))["generation"] == 3
    body = {**a_report(api, other), "import": True, "generation": 3}
    reply = api.post_form(f"projects/{pid}/artifacts", body, [other], role="importer")
    error = refused(reply, 409, "changed")
    held = error["details"]["current"]
    assert (held["generation"], held["sha256"]) == (3, hex_of(first)), error


@pytest.mark.rule("import.E11-E12-delete")
def test_an_imported_delete_keeps_its_fields_and_hands_nothing_on(api):
    pid = server_target.fresh_id()
    _, newer = two_runs(api, pid)
    deleted = {
        "deleted_at": "2020-02-02T02:02:02Z",
        "deleted_by": "source-login",
        "deleted_bench": "abcdef01",
    }
    body = {"import": True, "machine_id": MACHINE, "reason": "moved in", **deleted}
    body["deleted_was_current"] = False
    path = f"projects/{pid}/runs/{newer['run_id']}"
    answer = ok(api.post(f"{path}/delete", body, role="importer"))
    read = ok(api.get(path))
    assert {k: read.get(k) for k in deleted} == deleted and read["deleted_was_current"] is False
    if server_target.offers(api.base_url, "E6"):  # without E6 it hands on: calls' case
        assert "pointer" not in answer, ("an imported delete hands nothing on", answer)
        assert pointer_of(api, pid)["run_id"] == newer["run_id"], "the pointer was handed on"
    data = binary(200)
    ok(api.post_form(f"projects/{pid}/artifacts", a_report(api, data), [data]))
    name = {"kind": "report", "serial": SERIAL, **board_query(), "name": "full.html"}
    artifact = ok(api.post(f"projects/{pid}/artifacts/delete", {**body, **name}, role="importer"))
    assert {k: artifact.get(k) for k in deleted} == deleted, artifact


@pytest.mark.rule("import.delete-keeps-pointer")
def test_after_an_imported_delete_the_pointer_keeps_naming_the_run_until_set(api):
    """§6.7: an imported delete hands nothing on, so the pointer still names the deleted run:
    E5 answers it so, and the run reads ``current``; the importer's E6 then sets it as the
    source had it."""
    pid = server_target.fresh_id()
    older, newer = two_runs(api, pid)
    body = {"import": True, "machine_id": MACHINE, "reason": "moved in"}
    body["deleted_was_current"] = True
    ok(api.post(f"projects/{pid}/runs/{newer['run_id']}/delete", body, role="importer"))
    assert pointer_of(api, pid)["run_id"] == newer["run_id"]
    assert ok(api.get(f"projects/{pid}/runs/{newer['run_id']}"))["current"] is True
    current = f"projects/{pid}/boards/{SERIAL}/tests/{T1_ID}/current"
    sent = {"import": True, "run_id": older["run_id"], "machine_id": MACHINE}
    ok(api.post(current, sent, board_query(), role="importer"))
    assert pointer_of(api, pid)["run_id"] == older["run_id"]
    assert ok(api.get(f"projects/{pid}/runs/{newer['run_id']}"))["current"] is False


@pytest.mark.rule("import.P10")
def test_an_imported_release_is_filed_under_its_id_as_sent(api):
    project = api.create(a_test())
    document = text('{"steps": ["old"]}')
    sent = {
        "import": True,
        "version": 3,
        "released_at": "2020-03-03T03:03:03Z",
        "test": "Old name",
        "format": "text",
        "fingerprint": a_hex(),
        "content": a_hex(),
        "documents": {"procedure.json": document},
    }
    made = ok(project.release(OTHER_ID, sent, role="importer"))
    assert (made["version"], made["test"], made["released_at"]) == (
        3,
        "Old name",
        sent["released_at"],
    )
    [listed] = project.releases(OTHER_ID)
    assert (listed["version"], listed["test"]) == (3, "Old name")
    [full] = project.releases(OTHER_ID)  # with its documents
    assert full["documents"]["procedure.json"]["text"] == document["text"]
    assert ok(project.release(OTHER_ID, sent, role="importer"))["version"] == 3, "answered again"
    refused(
        project.release(OTHER_ID, {**sent, "content": a_hex()}, role="importer"), 409, "changed"
    )
    for missing in ("test", "documents"):
        body = {k: v for k, v in sent.items() if k != missing}
        refused(project.release(T3_ID, body, role="importer"), 400, "bad_request")


@pytest.mark.rule("import.contradiction")
def test_an_imported_run_contradicting_a_held_one_is_409_run_conflict(api):
    pid = server_target.fresh_id()
    run = a_run(1)
    ok(api.post(f"projects/{pid}/runs", run))
    other = {**run, "verdict": "FAIL", "import": True, "pushed_at": "2020-01-02T03:04:05Z"}
    refused(api.post(f"projects/{pid}/runs", other, role="importer"), 409, "run_conflict")
    assert ok(api.get(f"projects/{pid}/runs/{run['run_id']}"))["verdict"] == "PASS"
