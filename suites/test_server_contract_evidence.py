"""The evidence rules of the contract (``procedo_server_contract.md`` §5.5–§5.14 and §6.4,
E1–E15) that the store-then-read-back suites cannot see, checked with raw HTTP against the
reference server or the server the environment names (``server_target``).

The app's client hides some faults of a server: it checks a request before sending it, sorts
lists itself, recomputes ``current``, a board's lot and details, tolerates fields it does not
read, and reads an error's kind, never its status. So nothing here goes through it: each case
builds the JSON the contract names, sends it with ``http.client``, and checks the answer's
status, ``kind``, ``code`` and body, with the headers every answer carries
(:meth:`test_server_http_conformance.Server.check_signed`).

Each case writes under a project id of its own (and a serial, a machine id or a storage identity
of its own where a call reads across projects), so it passes on a server holding anything, run
after run. A case needing what only the reference server gives (its clock moved, an importer's
login, a blob taken away) runs only there (:func:`server_target.local_only`).

Each case names the rules it checks, by their ids in ``docs/server_contract/coverage.md``
(``@pytest.mark.rule``).
"""

from __future__ import annotations

import copy
import gzip
import hashlib
import http.client
import json
import re
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.message import Message
from typing import Any
from urllib.parse import quote, urlencode

import pytest
import server_target
from test_server_http_conformance import LOCAL_TOKENS, Reply, Server, check_envelope

pytestmark = pytest.mark.servers

rule = pytest.mark.rule

MIB = 1024 * 1024
MACHINE = "0000e5a1"
OTHER_MACHINE = "0000e5b2"
TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
#: The reference server's logins: the kit's two, and an importer the local-only cases use.
TOKENS = {**LOCAL_TOKENS, "t-importer": {"name": "importer-login", "roles": ["importer"]}}
TEST_A = "1a1a1a1a-0000-4000-8000-00000000000a"
TEST_B = "2b2b2b2b-0000-4000-8000-00000000000b"
TEST_C = "3c3c3c3c-0000-4000-8000-00000000000c"
TEST_D = "4d4d4d4d-0000-4000-8000-00000000000d"
#: A run's fields RunSummary leaves out (§5.5).
SUMMARY_DROPS = ("phases", "files", "missing", "bench_values", "extra")
#: The fields a server adds to every Run it answers (§5.5, *answer*).
RUN_ANSWER = {"project_id", "pushed_at", "current"}
DAY = datetime(2026, 3, 2, 8, 0, 0, tzinfo=timezone.utc)


def at(minutes: float) -> datetime:
    return DAY + timedelta(minutes=minutes)


def iso(minutes: float) -> str:
    return at(minutes).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_ms(minutes: float) -> str:
    moment = at(minutes)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def new_run_id(start: float) -> str:
    return f"{at(start):%Y-%m-%dT%H-%M-%SZ}_{secrets.token_hex(8)}"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_sha(content: dict) -> str:
    """A draft's version as the app computes it (§5.10)."""
    text = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return sha(text.encode("utf-8"))


# ---- The wire ---------------------------------------------------------------------------------


class Wire(Server):
    """The server under test reached with ``http.client``: any method, path, query and body,
    each answer checked for the headers every answer carries."""

    def send(
        self,
        method: str,
        path: str,
        *,
        role: str = "bench",
        query: dict | list | None = None,
        body: Any = None,
        data: bytes | None = None,
        content_type: str | None = None,
        headers: dict | None = None,
    ) -> Reply:
        url = self.prefix + path
        if query:
            url += "?" + urlencode(query, quote_via=quote)
        sent = {
            "Accept": "multipart/mixed, application/json",
            "Authorization": f"Bearer {token(role)}",
            **(headers or {}),
        }
        if body is not None:
            data, content_type = json.dumps(body).encode(), "application/json"
        if data is None and method == "POST":
            data = b""
        if content_type:
            sent["Content-Type"] = content_type
        reply = self._exchange(method, url, data, sent)
        self.check_signed(reply)
        return reply

    def _exchange(self, method: str, url: str, data: bytes | None, headers: dict) -> Reply:
        """One request; sent once more on a new connection when the server closed the kept one
        before answering (an idle keep-alive it dropped)."""
        reused = self.connection.sock is not None
        try:
            self.connection.request(method, url, body=data, headers=headers)
            answer = self.connection.getresponse()
        except (http.client.RemoteDisconnected, ConnectionResetError, BrokenPipeError):
            if not reused:
                raise
            self.connection.close()
            self.connection.request(method, url, body=data, headers=headers)
            answer = self.connection.getresponse()
        return Reply(answer.status, answer.headers, answer.read())


def token(role: str) -> str:
    if role == "importer":
        server_target.local_only("the kit has no importer login")
        return "t-importer"
    return server_target.token(role)


def _param(header: str, value: str, name: str) -> str | None:
    message = Message()
    message[header] = value
    found = message.get_param(name, header=header)
    return found[2] if isinstance(found, tuple) else found


def parse(reply: Reply) -> tuple[dict, dict[str, bytes]]:
    """An answer's JSON, and its file parts by name (``sha256:<hex>``): ``application/json``,
    or ``multipart/mixed`` with the JSON first (§6.2)."""
    kind = reply.headers.get("Content-Type", "")
    if kind.startswith("application/json"):
        return json.loads(reply.body), {}
    assert kind.startswith("multipart/mixed"), kind
    boundary = _param("content-type", kind, "boundary")
    assert boundary, kind
    delimiter = b"--" + boundary.encode("latin-1")
    pieces = reply.body.split(delimiter)
    assert pieces[-1].strip() in (b"--", b""), "the multipart answer is not closed"
    found: list[tuple[str | None, bytes]] = []
    for piece in pieces[1:-1]:
        assert piece.startswith(b"\r\n") and piece.endswith(b"\r\n"), piece[:80]
        head, _, data = piece[2:-2].partition(b"\r\n\r\n")
        headers = dict(
            (k.strip().lower(), v.strip())
            for k, _, v in (line.partition(":") for line in head.decode("utf-8").split("\r\n"))
        )
        found.append((_param("content-disposition", headers["content-disposition"], "name"), data))
    assert found and found[0][0] == "json", [name for name, _ in found]
    parts = {name: data for name, data in found[1:]}
    assert len(parts) == len(found) - 1, "two parts with one name"
    for name, data in parts.items():
        assert name == f"sha256:{sha(data)}", f"part {name} holds other bytes"
    return json.loads(found[0][1]), parts


def ok(reply: Reply) -> dict:
    """A ``200`` answer's JSON."""
    assert reply.status == 200, (reply.status, reply.body[:800])
    return parse(reply)[0]


def refused(reply: Reply, status: int, code: str) -> dict:
    """A refusal: its status, the kind 4.3 gives that status and code, the code, the envelope."""
    kinds = {400: "invalid", 404: "not_found", 409: "invalid"}
    kind = "corrupt" if code == "run_conflict" else kinds[status]
    return check_envelope(reply, status, kind, code)


def form_data(json_bytes: bytes, files: dict[str, bytes]) -> tuple[bytes, str]:
    """A ``multipart/form-data`` body: the JSON first, one part per content (§6.2)."""
    boundary = "kit-" + secrets.token_hex(12)
    chunks = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="json"\r\n'.encode(),
        b"Content-Type: application/json\r\n\r\n",
        json_bytes,
        b"\r\n",
    ]
    for digest, data in files.items():
        chunks += [
            f'--{boundary}\r\nContent-Disposition: form-data; name="sha256:{digest}"; '
            f'filename="{digest[:12]}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode(),
            data,
            b"\r\n",
        ]
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def none(value: str | None) -> str:
    """A board part in a query: empty for none (§2.6)."""
    return "" if value is None else value


# ---- One project's evidence --------------------------------------------------------------------


@dataclass
class Evidence:
    """One project's evidence on the server under test, written and read with raw calls."""

    wire: Wire
    project: str
    store: int  # the ``api`` storage's store_id
    blobs: dict[str, bytes] = field(default_factory=dict)  # every file's bytes, by sha256

    def offers(self, *calls: str) -> bool:
        """Whether the server offers every one of *calls* (optional calls, §6.1): a case marked
        ``offer_aware`` makes them only then."""
        return server_target.offers(self.wire.base_url, *calls)

    def other(self) -> Evidence:
        """Another project on the same server."""
        return Evidence(self.wire, server_target.fresh_id(), self.store, self.blobs)

    def path(self, *segments: str) -> str:
        return "/".join(["projects", self.project, *(quote(s, safe="") for s in segments)])

    # ---- Objects -------------------------------------------------------------------------

    def file(
        self, name: str, data: bytes, role: str, mimetype: str | None, store: int | None = None
    ) -> dict:
        self.blobs[sha(data)] = data
        item = {"name": name, "role": role, "size": len(data), "sha256": sha(data)}
        if mimetype is not None:
            item["mimetype"] = mimetype
        item["store_id"] = self.store if store is None else store
        return item

    def run(
        self,
        *,
        serial: str = "SN-1",
        part_number: str | None = "AX",
        board_revision: str | None = "B",
        test_id: str = TEST_A,
        test: str = "Ripple",
        start: float = 0,
        ended: float | None = 1,
        complete: bool = True,
        verdict: str = "PASS",
        format_version: int = 2,
        machine_id: str = MACHINE,
        files: list | None = None,
        **fields: Any,
    ) -> dict:
        """A Run (§5.5): complete with one phase and its record, or stopped with its log."""
        run_id = new_run_id(start)
        run: dict = {"format_version": format_version, "run_id": run_id, "serial": serial}
        if part_number is not None:
            run["part_number"] = part_number
        if board_revision is not None:
            run["board_revision"] = board_revision
        run.update(test=test, test_id=test_id, started=iso(start), complete=complete)
        run.update(machine_id=machine_id, draft=False)
        if complete:
            run.update(ended=iso(ended), verdict=verdict)
            run["phases"] = [
                {"ord": 1, "name": "Step 1", "attempt": 1, "outcome": verdict, "measurements": []}
            ]
            record = json.dumps({"run_id": run_id, "kit": True}).encode()
            default = [self.file("results.json", record, "record", "application/json")]
        else:
            run["phases"] = []
            log = f"{run_id} stopped\n".encode()
            default = [self.file("console.log", log, "log", "text/plain")]
        run["files"] = default if files is None else files
        run.update(fields)
        return run

    def artifact(
        self,
        kind: str,
        name: str,
        serial: str,
        part_number: str | None = None,
        board_revision: str | None = None,
        *,
        data: bytes | None = None,
        content: dict | None = None,
        replaces: str | None = None,
        **fields: Any,
    ) -> dict:
        """An Artifact (§5.10): a report's or export's file, or a draft's content."""
        art: dict = {"format_version": 2, "kind": kind, "serial": serial}
        if part_number is not None:
            art["part_number"] = part_number
        if board_revision is not None:
            art["board_revision"] = board_revision
        art.update(name=name, machine_id=MACHINE, updated_at=iso(0))
        if kind == "draft":
            content = {"blocks": [name]} if content is None else content
            art.update(content=content, sha256=canonical_sha(content), replaces=replaces)
        else:
            data = f"<p>{name} {secrets.token_hex(4)}</p>".encode() if data is None else data
            self.blobs[sha(data)] = data
            art.update(sha256=sha(data), size=len(data), store_id=self.store)
        art.update(fields)
        return art

    # ---- Writes --------------------------------------------------------------------------

    def _send_with_parts(self, path: str, obj: dict, refs: list[dict], parts: bool, **kw: Any):
        data = kw.pop("data", None) or json.dumps(obj).encode()
        files = {}
        if parts:
            for item in refs:
                held = self.blobs.get(item.get("sha256", ""))
                if item.get("store_id") == self.store and held is not None and len(held) <= MIB:
                    files[item["sha256"]] = held
        if not files:
            return self.wire.send("POST", path, data=data, content_type="application/json", **kw)
        body, kind = form_data(data, files)
        return self.wire.send("POST", path, data=body, content_type=kind, **kw)

    def push(self, run: dict, *, parts: bool = True, **kw: Any) -> Reply:
        """E1."""
        return self._send_with_parts(self.path("runs"), run, run["files"], parts, **kw)

    def pushed(self, run: dict, *, parts: bool = True) -> dict:
        answer = ok(self.push(run, parts=parts))
        assert answer["status"] == "saved", answer
        return answer

    def push_artifact(self, art: dict, *, parts: bool = True) -> Reply:
        """E10."""
        refs = [] if art["kind"] == "draft" else [art]
        return self._send_with_parts(self.path("artifacts"), art, refs, parts)

    def artifact_pushed(self, art: dict) -> dict:
        answer = ok(self.push_artifact(art))
        assert answer["status"] == "saved", answer
        return answer["artifact"]

    def entries(
        self, serial: str, part_number: str | None, board_revision: str | None, entries: list
    ) -> list[dict]:
        """E15: each entry's result."""
        query = {"part_number": none(part_number), "board_revision": none(board_revision)}
        reply = self.wire.send(
            "POST", self.path("boards", serial, "entries"), query=query, body={"entries": entries}
        )
        return ok(reply)["results"]

    def annotate(self, run_id: str, entries: list) -> Reply:
        """E7."""
        return self.wire.send(
            "POST", self.path("runs", run_id, "annotation"), body={"entries": entries}
        )

    def set_current(
        self,
        serial: str,
        part_number: str | None,
        board_revision: str | None,
        test_id: str,
        run_id: str | None,
        replaces: dict | None,
        machine_id: str = MACHINE,
    ) -> Reply:
        """E6."""
        query = {"part_number": none(part_number), "board_revision": none(board_revision)}
        body = {"run_id": run_id, "replaces": replaces, "machine_id": machine_id}
        return self.wire.send(
            "POST", self.path("boards", serial, "tests", test_id, "current"), query=query, body=body
        )

    def delete_run(self, run_id: str, reason: Any = "Wrong board tested", **fields: Any) -> Reply:
        """E11's delete, as the manager."""
        body = {"machine_id": MACHINE, **fields}
        if reason is not None:
            body["reason"] = reason
        return self.wire.send(
            "POST", self.path("runs", run_id, "delete"), role="manager", body=body
        )

    def restore_run(self, run_id: str) -> Reply:
        body = {"machine_id": MACHINE}
        return self.wire.send(
            "POST", self.path("runs", run_id, "restore"), role="manager", body=body
        )

    def named(self, art: dict) -> dict:
        """The fields naming an artifact in E12 and E13."""
        return {
            k: art[k]
            for k in ("kind", "serial", "part_number", "board_revision", "name")
            if k in art
        }

    def delete_artifact(self, art: dict, reason: str = "Wrong board") -> Reply:
        body = {"machine_id": MACHINE, **self.named(art), "reason": reason}
        return self.wire.send("POST", self.path("artifacts", "delete"), role="manager", body=body)

    def restore_artifact(self, art: dict) -> Reply:
        body = {"machine_id": MACHINE, **self.named(art)}
        return self.wire.send("POST", self.path("artifacts", "restore"), role="manager", body=body)

    def purge(self, items: list | None = None) -> dict:
        """E13, as the manager."""
        body: dict = {"machine_id": MACHINE}
        if items is not None:
            body["items"] = items
        return ok(self.wire.send("POST", self.path("purge"), role="manager", body=body))

    # ---- Reads ---------------------------------------------------------------------------

    def read_run(self, run_id: str, *, have: list[str] | None = None) -> dict:
        """E3."""
        query = {"have": ",".join(have)} if have else None
        return ok(self.wire.send("GET", self.path("runs", run_id), query=query))

    def held_versions(self) -> list[str]:
        return [f"sha256:{digest}" for digest in self.blobs]

    def board(
        self,
        serial: str,
        part_number: str | None = "AX",
        board_revision: str | None = "B",
        **query: str,
    ) -> dict:
        """E5, less the run the kit imports to register the board on a server registering its
        boards (``server_target.BoardSeeds``)."""
        query = {"part_number": none(part_number), "board_revision": none(board_revision), **query}
        return server_target.kit_tests(
            ok(self.wire.send("GET", self.path("boards", serial), query=query))
        )

    def pointer(
        self,
        serial: str,
        test_id: str,
        part_number: str | None = "AX",
        board_revision: str | None = "B",
    ) -> dict | None:
        """The test id's pointer on the board, from E5 (None: no pointer, or not listed)."""
        for item in self.board(serial, part_number, board_revision)["tests"]:
            if item["test_id"] == test_id:
                return item["pointer"]
        return None

    def boards(self, **query: str) -> list[dict]:
        """E4."""
        return ok(self.wire.send("GET", self.path("boards"), query=query))["boards"]

    def search(self, *, across: bool = False, **query: str) -> dict:
        """E2, in this project unless *across*, less the runs the kit imports to register its
        boards on a server registering them (``server_target.kit_runs``)."""
        if not across:
            query = {"project_id": self.project, **query}
        return server_target.kit_runs(ok(self.wire.send("GET", "runs", query=query)))

    def found(self, **query: str) -> list[str]:
        """E2's rows (one page), as run ids."""
        return [row["run_id"] for row in self.search(**query)["runs"]]

    def get_artifact(
        self,
        kind: str,
        name: str,
        serial: str,
        part_number: str | None = None,
        board_revision: str | None = None,
        **query: str,
    ) -> Reply:
        """E9."""
        query = {
            "serial": serial,
            "part_number": none(part_number),
            "board_revision": none(board_revision),
            **query,
        }
        return self.wire.send("GET", self.path("artifacts", kind, name), query=query)

    def list_artifacts(self, **query: str) -> dict:
        """E8."""
        return ok(self.wire.send("GET", self.path("artifacts"), query=query))


def comment(key: str, text: str | None, replaces: str | None, **fields: Any) -> dict:
    return {
        "entry_id": str(uuid.uuid4()),
        "kind": "comment",
        "key": key,
        "text": text,
        "replaces": replaces,
        "by": "AL",
        "machine_id": MACHINE,
        "saved_at": iso(30),
        **fields,
    }


def override(key: str, value: str | None, replaces: str | None) -> dict:
    entry = comment(key, None, replaces)
    del entry["text"]
    entry.update(kind="override", value=value)
    return entry


def board_entry(kind: str, value: str | None, replaces: str | None, name: str | None = None):
    entry: dict = {"entry_id": str(uuid.uuid4()), "kind": kind}
    if name is not None:
        entry["name"] = name
    entry.update(value=value, replaces=replaces, by="AL", machine_id=MACHINE, saved_at=iso(5))
    return entry


def pointer_of(run: dict) -> dict:
    """``replaces`` naming *run* as the pointer (E6)."""
    return {"run_id": run["run_id"], "ended": run["ended"]}


def summary(run: dict) -> dict:
    return {k: v for k, v in run.items() if k not in SUMMARY_DROPS}


@pytest.fixture
def ev(tmp_path):
    with server_target.serve(tmp_path / "server", TOKENS) as (base_url, running):
        wire = Wire(base_url)
        try:
            storages = ok(wire.send("GET", "storages"))["storages"]
            api = [s["store_id"] for s in storages if s["backend"] == "api"]
            if not api:
                pytest.skip("the server lists no api storage: these cases send bytes as parts")
            yield Evidence(wire, server_target.fresh_id(), api[0])
        finally:
            wire.close()
        if running is not None:
            assert not running.internal_errors, running.internal_errors


@pytest.fixture
def local(tmp_path):
    """The reference server itself, for the cases only it can serve; with ``ev`` on it."""
    server_target.local_only(
        "it reaches into the reference server (its clock, its blobs, an importer's login)"
    )
    with server_target.serve(tmp_path / "server", TOKENS) as (base_url, running):
        wire = Wire(base_url)
        try:
            yield running, Evidence(wire, server_target.fresh_id(), 1)
        finally:
            wire.close()
        assert not running.internal_errors, running.internal_errors


# ---- §5.5–5.8 A run, read back -----------------------------------------------------------------


def rich_run(ev: Evidence) -> dict:
    """A run holding every field of §5.5–5.8, attempts and repeated names included."""
    run_id = new_run_id(0)
    png = b"\x89PNG\r\n\x1a\n\x00\x00" + secrets.token_bytes(64) + b"\r\n\x00end"
    files = [
        ev.file(
            "results.json", json.dumps({"run_id": run_id}).encode(), "record", "application/json"
        ),
        ev.file("step_4_SCOPE1.png", png, "attachment", "image/png"),
        ev.file("console.log", b"step 1\r\nstep 2\n", "log", "text/plain"),
        ev.file("functions/rails.py", b"RAIL = 28.0\n", "functions", "text/x-python"),
        ev.file("definition/procedure.json", b'{"id": "T1"}', "definition", "application/json"),
        ev.file("notes.bin", secrets.token_bytes(32), "attachment", None),
    ]
    tolerance = {
        "kind": "tolerance_abs",
        "ref": 1,
        "nominal_value": 28.0,
        "unit": "V",
        "tol_value": 0.2,
        "tol_unit": "V",
    }
    phases = [
        {
            "ord": 1,
            "name": "Phase 0 setup",
            "attempt": 1,
            "outcome": "PASS",
            "started": iso_ms(0.002),
            "ended": iso_ms(0.009),
            "measurements": [],
        },
        {
            "ord": 2,
            "name": "Step 5 (i=2)",
            "attempt": 1,
            "outcome": "FAIL",
            "started": iso_ms(0.01),
            "ended": iso_ms(0.5),
            "measurements": [
                {
                    "name": "{1} = 28.0 V +/- 0.2 V",
                    "ord": 1,
                    "value_num": 27.96,
                    "units": "V",
                    "outcome": "PASS",
                    "validators": ["27.8 <= x <= 28.2"],
                    "criterion_kind": "tolerance",
                    "limit_low": 27.8,
                    "limit_high": 28.2,
                    "criterion": tolerance,
                },
                {
                    "name": "Ripple",
                    "ord": 2,
                    "value_num": 0.051,
                    "units": "V",
                    "outcome": "FAIL",
                    "validators": ["x <= 0.045"],
                    "criterion_kind": "max",
                    "limit_high": 0.045,
                    "criterion": {"kind": "lte", "value": 0.045, "zeta": 1, "alpha": [2, 1]},
                },
                {
                    "name": "Label",
                    "ord": 3,
                    "value_text": "REV B",
                    "outcome": "PASS",
                    "criterion_kind": "equals",
                    "criterion": {"kind": "eq_text", "value": "REV B"},
                },
                {
                    "name": "Count",
                    "ord": 4,
                    "value_num": 1.2345678901234567e19,
                    "value_text": "12345678901234567891",
                    "outcome": "UNSET",
                },
                {"name": "Noise", "ord": 5, "value_text": "NaN", "outcome": "PARTIALLY_SET"},
            ],
        },
        {
            "ord": 3,
            "name": "Step 5 (i=2)",
            "attempt": 2,
            "outcome": "PASS",
            "measurements": [
                {"name": "Ripple", "ord": 6, "value_num": 0.04, "units": "V", "outcome": "PASS"}
            ],
        },
    ]
    return {
        "format_version": 2,
        "run_id": run_id,
        "serial": "SN0042",
        "part_number": "AX-2210",
        "board_revision": "B",
        "test": "Ripple Tést",
        "test_id": TEST_A,
        "firmware_version": "2.4.1",
        "extra": {"Customer": "ACME", "Line": "3"},
        "started": iso(0),
        "ended": iso(1),
        "verdict": "FAIL",
        "complete": True,
        "operator": "AL",
        "machine_id": MACHINE,
        "station_id": "BENCH-3",
        "test_version": 4,
        "draft": True,
        "definition_sha256": sha(b"what ran"),
        "bundle_id": "baseline",
        "bundle_version": "3.0.0",
        "app_version": "5.1.0",
        "bench_values": {
            "PSU1_VISA": "TCPIP0::10.0.0.9::INSTR",
            "GAIN": 2.5,
            "MAP": {"b": 1, "a": 2},
        },
        "phases": phases,
        "files": files,
        "kit_extension": {"z": 1, "a": [True, "x"]},
    }


def same_files(read: list[dict], sent: list[dict]) -> None:
    """A run's files as sent; an absent ``mimetype`` may read back as its meaning (§5.8)."""

    def normal(files: list[dict]) -> list[dict]:
        found = []
        for item in files:
            item = dict(item)
            if item.get("mimetype") == "application/octet-stream":
                item.pop("mimetype")
            found.append(item)
        return found

    assert normal(read) == normal(sent)


@rule(
    "5.5.run-full",
    "5.5.annotation-absent",
    "5.5.run-project-id",
    "5.5.run-current",
    "5.5.run-summary-light",
    "5.8.runfile-role-mimetype",
)
@pytest.mark.offer_aware
def test_a_run_reads_back_every_field_exactly_as_pushed(ev):
    """E1 then E3: every field of the Run, its phases and measurements (attempts, a repeated
    phase name, a criterion's own keys in their order, a value in both forms), its files with
    their roles and types, an unknown field; plus exactly the server's own fields. E5 (and E2
    when the server offers it) answer the same run as RunSummary: the same fields less the five
    §5.5 leaves out."""
    run = rich_run(ev)
    answer = ev.pushed(run)
    assert answer["run_id"] == run["run_id"] and answer["test_id"] == TEST_A
    assert (answer["serial"], answer["part_number"], answer["board_revision"]) == (
        "SN0042",
        "AX-2210",
        "B",
    )
    assert answer["current"] is True
    assert TIME.fullmatch(answer["pushed_at"]), answer

    read = ev.read_run(run["run_id"], have=ev.held_versions())
    assert set(read) == set(run) | RUN_ANSWER, sorted(set(read) ^ (set(run) | RUN_ANSWER))
    for key, value in run.items():
        if key == "files":
            same_files(read["files"], value)
        else:
            assert read[key] == value, key
    assert json.dumps(read["phases"][1]["measurements"][1]["criterion"]) == json.dumps(
        run["phases"][1]["measurements"][1]["criterion"]
    ), "the criterion is stored as sent, its keys in their order"
    assert read["project_id"] == ev.project
    assert read["pushed_at"] == answer["pushed_at"]
    assert read["current"] is True

    reply = ev.wire.send("GET", ev.path("runs", run["run_id"]))
    whole, parts = parse(ok_reply(reply))
    assert whole == read
    assert parts == {f"sha256:{item['sha256']}": ev.blobs[item["sha256"]] for item in run["files"]}

    board = ev.board("SN0042", "AX-2210", "B")
    assert board["tests"][0]["runs"] == [summary(read)]
    if ev.offers("E2"):
        assert ev.search()["runs"] == [summary(read)]


def ok_reply(reply: Reply) -> Reply:
    assert reply.status == 200, (reply.status, reply.body[:800])
    return reply


@rule(
    "2.4.record-kept-as-sent", "2.5.filed-under-id", "5.13.newer-complete-current",
    "5.13.one-mark-per-test-id",
)  # fmt: skip
def test_runs_are_filed_by_test_id_and_the_newest_complete_one_is_current(ev):
    """Core calls alone (E1, E3, E5): a run keeps the test name it recorded, as sent; two test
    ids recording one name are two tests, each with its runs and its mark; a complete run
    ending later takes its id's mark, one ending earlier or incomplete does not, and no other
    id's mark moves."""
    a1 = ev.run(test_id=TEST_A, test="Ripple Ä", start=0, ended=10)
    b1 = ev.run(test_id=TEST_B, test="Ripple Ä", start=1, ended=11)
    for run in (a1, b1):
        ev.pushed(run)
    assert ev.read_run(a1["run_id"], have=ev.held_versions())["test"] == "Ripple Ä"
    shouted = ev.run(test_id=TEST_A, test="RIPPLE ä", start=2, ended=12)
    ev.pushed(shouted)
    assert ev.read_run(shouted["run_id"], have=ev.held_versions())["test"] == "RIPPLE ä"

    def marks() -> dict[str, str | None]:
        return {t["test_id"]: t["pointer"]["run_id"] for t in ev.board("SN-1")["tests"]}

    assert marks() == {TEST_A: shouted["run_id"], TEST_B: b1["run_id"]}
    tests = {t["test_id"]: [r["run_id"] for r in t["runs"]] for t in ev.board("SN-1")["tests"]}
    assert sorted(tests[TEST_A]) == sorted([a1["run_id"], shouted["run_id"]]), tests
    assert tests[TEST_B] == [b1["run_id"]], tests
    ev.pushed(ev.run(test_id=TEST_A, start=3, ended=5))  # ended earlier: no mark
    ev.pushed(ev.run(test_id=TEST_A, start=30, complete=False))  # incomplete: no mark
    assert marks() == {TEST_A: shouted["run_id"], TEST_B: b1["run_id"]}
    later = ev.run(test_id=TEST_A, start=40, ended=50)
    ev.pushed(later)
    assert marks() == {TEST_A: later["run_id"], TEST_B: b1["run_id"]}


@rule("E1.answer")
def test_the_push_answer_names_the_board_as_first_stored_and_whether_the_run_is_current(ev):
    first = ev.run(serial="Sn-7", part_number="Ax-1", board_revision="b", start=0, ended=10)
    assert ev.pushed(first)["current"] is True
    newer = ev.run(serial="SN-7", part_number="AX-1", board_revision="B", start=20, ended=30)
    answer = ev.pushed(newer)
    assert {k: answer.get(k) for k in ("serial", "part_number", "board_revision")} == {
        "serial": "Sn-7",
        "part_number": "Ax-1",
        "board_revision": "b",
    }
    assert answer["current"] is True and answer["test_id"] == TEST_A
    older = ev.run(serial="sn-7", part_number="ax-1", board_revision="B", start=1, ended=2)
    answer = ev.pushed(older)
    assert answer["current"] is False and answer["serial"] == "Sn-7"
    read = ev.read_run(older["run_id"], have=ev.held_versions())
    assert (read["serial"], read["part_number"], read["board_revision"]) == ("Sn-7", "Ax-1", "b")
    assert read["current"] is False


@rule("E1.project-id-match")
def test_a_run_naming_another_project_is_refused(ev):
    run = ev.run(project_id=server_target.fresh_id())
    refused(ev.push(run), 400, "bad_request")
    assert ev.wire.send("GET", ev.path("runs", run["run_id"])).status == 404
    run = ev.run(project_id=ev.project)
    ev.pushed(run)
    assert ev.read_run(run["run_id"], have=ev.held_versions())["project_id"] == ev.project


@rule("5.5.lot-format-2-400")
def test_a_format_2_run_sending_a_lot_is_refused(ev):
    run = ev.run(lot="L2611-04")
    refused(ev.push(run), 400, "bad_request")
    assert ev.wire.send("GET", ev.path("runs", run["run_id"])).status == 404
    assert ev.board(run["serial"])["tests"] == []  # less the kit's own run, if any
    # On a server registering its boards a refusal for the board alone may come first, and the
    # kit registers it (server_target.BoardSeeds): E4 lists it there.
    if not server_target.seeds_boards():
        assert ev.boards() == []


@pytest.mark.offer_aware
@rule("5.5.format-1-board", "E1.answer", "E2.filters")
def test_a_format_1_run_belongs_to_the_no_part_number_board(ev):
    """What it recorded stays a record; every call naming a board names the "no part
    number" one, and E2's part number and revision filters (when the server offers E2) find it
    only with both empty."""
    run = ev.run(
        format_version=1, serial="Old-1", part_number="AX-9", board_revision="C", lot="L-OLD"
    )
    answer = ev.pushed(run)
    assert answer["serial"] == "Old-1"
    assert "part_number" not in answer and "board_revision" not in answer, answer
    read = ev.read_run(run["run_id"], have=ev.held_versions())
    assert read["format_version"] == 1
    assert (read["part_number"], read["board_revision"], read["lot"]) == ("AX-9", "C", "L-OLD")
    boards = ev.boards()
    assert [(b["serial"], b.get("part_number"), b.get("board_revision")) for b in boards] == [
        ("Old-1", None, None)
    ]
    assert [t["test_id"] for t in ev.board("Old-1", None, None)["tests"]] == [TEST_A]
    assert ev.board("Old-1", "AX-9", "C")["tests"] == []
    if not ev.offers("E2"):
        return
    assert ev.found(part_number="", board_revision="") == [run["run_id"]]
    assert ev.found(part_number="") == [run["run_id"]]
    assert ev.found(part_number="AX-9") == []
    assert ev.found(board_revision="C") == []
    assert ev.found(lot="l-old") == [run["run_id"]]


@rule("E1.run-conflict")
def test_other_json_under_a_held_run_id_is_a_conflict(ev):
    """The replay check is of the JSON's bytes (§6.4 E1, §2.12): the same bytes, gzipped, are
    a replay; the same run written with other bytes, or another verdict, is a conflict."""
    run = ev.run(verdict="FAIL")
    ev.pushed(run)
    data = json.dumps(run).encode()
    replay = ev.wire.send(
        "POST",
        ev.path("runs"),
        data=gzip.compress(data),
        content_type="application/json",
        headers={"Content-Encoding": "gzip"},
    )
    assert ok(replay)["status"] == "already"
    for other in (dict(run, verdict="PASS"), run):
        raw = json.dumps(other, indent=1).encode()
        refused(ev.push(other, data=raw), 409, "run_conflict")
    assert ev.read_run(run["run_id"], have=ev.held_versions())["verdict"] == "FAIL"


@rule("E1.large-file")
def test_a_run_file_over_one_mib_goes_alone_first_and_one_of_exactly_one_mib_as_a_part(ev):
    big = secrets.token_bytes(MIB + 1)
    edge = secrets.token_bytes(MIB)
    run = ev.run()
    run["files"] += [
        ev.file("capture.bin", big, "attachment", "application/octet-stream"),
        ev.file("edge.bin", edge, "attachment", "application/octet-stream"),
    ]
    data = json.dumps(run).encode()
    record = run["files"][0]["sha256"]
    as_parts = {record: ev.blobs[record], sha(big): big, sha(edge): edge}
    body, kind = form_data(data, as_parts)
    error = refused(
        ev.wire.send("POST", ev.path("runs"), data=body, content_type=kind), 400, "bad_request"
    )
    assert f"sha256:{sha(big)}" in error["details"]["large"], error
    assert ev.wire.send("GET", ev.path("runs", run["run_id"])).status == 404

    error = refused(ev.push(run), 409, "missing_files")
    assert f"sha256:{sha(big)}" in error["details"]["missing"], error
    assert ev.wire.send("GET", ev.path("runs", run["run_id"])).status == 404

    blob = f"storages/{ev.store}/blobs/{ev.project}/{sha(big)}"
    uploaded = ok(ev.wire.send("POST", blob, data=big, content_type="application/octet-stream"))
    assert uploaded == {"status": "saved", "version": f"sha256:{sha(big)}", "size": len(big)}
    ev.pushed(run)
    whole, parts = parse(ok_reply(ev.wire.send("GET", ev.path("runs", run["run_id"]))))
    assert "missing" not in whole
    same_files(whole["files"], run["files"])
    assert set(parts) == {f"sha256:{record}", f"sha256:{sha(edge)}"}
    assert parts[f"sha256:{sha(edge)}"] == edge
    download = ev.wire.send("GET", blob)
    assert download.status == 200 and download.body == big


# ---- §5.5 Deleted runs, E11 ---------------------------------------------------------------------


def manager_login(ev: Evidence) -> str:
    return ok(ev.wire.send("GET", "info", role="manager"))["user"]["name"]


@rule("5.5.deleted-by-login")
def test_deleted_by_is_the_login_the_server_authenticated(ev):
    run = ev.run()
    ev.pushed(run)
    login = manager_login(ev)
    answer = ok(
        ev.delete_run(
            run["run_id"], deleted_by="mallory", deleted_by_name="alice", station="BENCH-9"
        )
    )
    assert answer["run"]["deleted_by"] == login, answer
    read = ev.read_run(run["run_id"], have=ev.held_versions())
    assert read["deleted_by"] == login
    assert (read["deleted_by_name"], read["deleted_station"]) == ("alice", "BENCH-9")
    assert read["deleted_bench"] == MACHINE and TIME.fullmatch(read["deleted_at"])
    [row] = ev.search(deleted="only")["runs"]
    assert row["deleted_by"] == login


@rule("E11.never-empty-strings")
def test_a_delete_with_an_empty_station_or_name_is_400_and_deletes_nothing(ev):
    """``station`` and ``deleted_by_name`` are never ``""`` (§6.4 E11, §2.2): a run's delete
    (E11) or an artifact's (E12) sending one so is ``400``, never taken as "not known"."""
    run = ev.run()
    ev.pushed(run)
    report = ev.artifact("report", "r.html", "SN-1", "AX", "B")
    ev.artifact_pushed(report)
    for blank in ({"station": ""}, {"deleted_by_name": ""}):
        refused(ev.delete_run(run["run_id"], **blank), 400, "bad_request")
        body = {"machine_id": MACHINE, **ev.named(report), "reason": "Wrong board", **blank}
        reply = ev.wire.send("POST", ev.path("artifacts", "delete"), role="manager", body=body)
        refused(reply, 400, "bad_request")
    assert "deleted_at" not in ev.read_run(run["run_id"], have=ev.held_versions())
    assert ev.found() == [run["run_id"]]
    assert [a["name"] for a in ev.list_artifacts()["artifacts"]] == ["r.html"]


@rule("E11.reason-400")
def test_a_delete_without_its_reason_is_refused(ev):
    run = ev.run()
    ev.pushed(run)
    for reason in (None, "", "   "):
        refused(ev.delete_run(run["run_id"], reason=reason), 400, "bad_request")
    read = ev.read_run(run["run_id"], have=ev.held_versions())
    assert "deleted_at" not in read and read["current"] is True
    assert ev.found() == [run["run_id"]]


@rule("E11.restore-stays-history", "E11.answer-pointer")
def test_a_restored_run_a_newer_one_replaced_stays_in_the_history(ev):
    first = ev.run(start=0, ended=10)
    ev.pushed(first)
    gone = ok(ev.delete_run(first["run_id"]))
    assert "pointer" in gone and gone["pointer"] is None, gone
    newer = ev.run(start=20, ended=30)
    assert ev.pushed(newer)["current"] is True
    back = ok(ev.restore_run(first["run_id"]))
    assert "pointer" not in back, back
    assert not any(k.startswith("deleted_") for k in back["run"]), back
    assert ev.pointer("SN-1", TEST_A)["run_id"] == newer["run_id"]
    [item] = ev.board("SN-1")["tests"]
    assert [r["run_id"] for r in item["runs"]] == [newer["run_id"], first["run_id"]]
    assert ev.read_run(first["run_id"], have=ev.held_versions())["current"] is False


# ---- §5.9 Annotation, E7 ------------------------------------------------------------------------


@rule("5.9.current-flag", "5.9.data")
def test_an_annotations_current_entries_and_data_are_the_servers(ev):
    """``current`` is computed from the order received, never taken as sent; ``data`` holds
    each slot's current entry in the shape §5.9 states, a cleared slot left out."""
    run = ev.run()
    ev.pushed(run)
    e1 = comment("test", "First", None, current=False, received_at="2000-01-01T00:00:00Z")
    e2 = comment("test", "Second", e1["entry_id"])
    e3 = comment("test", "Late", e1["entry_id"], current=True)
    e4 = override("criterion range 9", "FAIL", None)
    e5 = override("criterion range 9", None, e4["entry_id"])
    e6 = override("test", "PASS", "not-an-entry-held")
    e7 = comment("check 2", "probe", None)
    replay = dict(e1, text="Changed")
    sent = [e1, e2, e3, e4, e5, e6, e7, replay]
    results = ok(ev.annotate(run["run_id"], sent))["results"]
    assert [(r["entry_id"], r["status"]) for r in results] == [
        (e1["entry_id"], "saved"),
        (e2["entry_id"], "saved"),
        (e3["entry_id"], "changed"),
        (e4["entry_id"], "saved"),
        (e5["entry_id"], "saved"),
        (e6["entry_id"], "saved"),
        (e7["entry_id"], "saved"),
        (e1["entry_id"], "already"),
    ]
    assert results[2]["current"]["entry_id"] == e2["entry_id"]
    assert results[2]["current"]["current"] is True

    annotation = ev.read_run(run["run_id"], have=ev.held_versions())["annotation"]
    entries = annotation["entries"]
    assert [e["entry_id"] for e in entries] == [e["entry_id"] for e in sent[:7]]
    assert [e["current"] for e in entries] == [False, True, False, False, True, True, True]
    for read, wrote in zip(entries, sent[:7], strict=True):
        assert TIME.fullmatch(read["received_at"]) and read["received_at"] != "2000-01-01T00:00:00Z"
        mine = {k: v for k, v in wrote.items() if k not in ("current", "received_at")}
        assert {k: v for k, v in read.items() if k not in ("current", "received_at")} == mine

    def slot(entry: dict, field_name: str) -> dict:
        return {
            field_name: entry[field_name],
            "by": "AL",
            "at": entry["saved_at"],
            "entry_id": entry["entry_id"],
        }

    assert annotation["data"] == {
        "version": "2.0",
        "comments": {"test": slot(e2, "text"), "criteria": {"check 2": slot(e7, "text")}},
        "verdict_overrides": {"test": slot(e6, "value"), "criteria": {}},
    }
    assert annotation["run_id"] == run["run_id"] and annotation["format_version"] == 1


@rule("E7.override-value-400")
def test_an_override_outside_the_six_verdicts_is_refused(ev):
    run = ev.run()
    ev.pushed(run)
    note = comment("test", "seen", None)
    refused(ev.annotate(run["run_id"], [note, override("test", "OK", None)]), 400, "bad_request")
    assert "annotation" not in ev.read_run(run["run_id"], have=ev.held_versions())
    for value in ("PASS", "FAIL", "ERROR", "ABORTED", "PARTIAL", "SKIP"):
        assert (
            ok(ev.annotate(run["run_id"], [override(value, value, None)]))["results"][0]["status"]
            == "saved"
        )


# ---- §5.11 Boards, E4, E15 ----------------------------------------------------------------------


@rule("5.11.lot-details", "5.11.field-name-exact")
def test_a_boards_lot_and_details_are_its_current_entries(ev):
    l1 = board_entry("lot", "L2611-01", None)
    f1 = board_entry("field", "ACME", None, "Customer")
    f2 = board_entry("field", "Other", None, "customer")
    l2 = board_entry("lot", "L2611-04", l1["entry_id"])
    l3 = board_entry("lot", "L-WRONG", l1["entry_id"])
    f3 = board_entry("field", "Lyon", None, "Site")
    f4 = board_entry("field", None, f3["entry_id"], "Site")
    sent = [l1, f1, f2, l2, l3, f3, f4]
    results = ev.entries("L-1", "AX", "B", sent)
    assert [r["status"] for r in results] == [
        "saved",
        "saved",
        "saved",
        "saved",
        "changed",
        "saved",
        "saved",
    ], results
    assert results[4]["current"]["entry_id"] == l2["entry_id"]
    board = ev.board("L-1")["board"]
    assert board["lot"] == "L2611-04"
    assert board["details"] == {"Customer": "ACME", "customer": "Other"}
    assert [e["entry_id"] for e in board["entries"]] == [e["entry_id"] for e in sent]
    assert [e["current"] for e in board["entries"]] == [False, True, True, True, False, False, True]
    [listed] = ev.boards()
    assert (listed["lot"], listed["details"]) == ("L2611-04", board["details"])
    assert "entries" not in listed
    clear = board_entry("lot", None, l2["entry_id"])
    ev.entries("L-1", "AX", "B", [clear])
    assert "lot" not in ev.board("L-1")["board"]


@rule("5.11.board-times")
def test_a_boards_times_are_its_creation_and_its_last_run(ev):
    """A board's ``created_at`` is when its first run made it, kept; ``last_ended`` its newest
    run's ``ended``."""
    first = ev.run(serial="T-0", start=20, ended=30)
    ev.pushed(first)
    [board] = ev.boards()
    created = board["created_at"]
    assert TIME.fullmatch(created) and board["last_ended"] == iso(30), board
    ev.pushed(ev.run(serial="T-0", start=0, ended=10))
    [board] = ev.boards()
    assert (board["created_at"], board["last_ended"]) == (created, iso(30)), board
    ev.pushed(ev.run(serial="T-0", start=40, ended=50))
    assert ev.boards()[0]["last_ended"] == iso(50)


@rule("5.11.board-times", "5.11.board-never-removed")
def test_a_boards_times_and_a_board_whose_runs_are_all_gone_is_still_listed(ev):
    ev.entries("T-1", "AX", "B", [board_entry("lot", "L1", None)])
    [board] = ev.boards()
    created = board["created_at"]
    assert TIME.fullmatch(created) and "last_ended" not in board, board
    first, second = (
        ev.run(serial="T-1", start=0, ended=10),
        ev.run(serial="T-1", start=20, ended=30),
    )
    ev.pushed(first)
    ev.pushed(second)
    assert ev.boards()[0]["last_ended"] == iso(30)
    ok(ev.delete_run(second["run_id"]))
    assert ev.boards()[0]["last_ended"] == iso(10)
    ok(ev.delete_run(first["run_id"]))
    [board] = ev.boards()
    assert "last_ended" not in board and board["created_at"] == created, board
    purged = ev.purge([{"run_id": first["run_id"]}, {"run_id": second["run_id"]}])
    assert purged["runs"] == 2
    [board] = ev.boards()
    assert (board["serial"], board["created_at"], board["lot"]) == ("T-1", created, "L1")
    assert ev.board("T-1")["tests"] == []


@rule("5.11.board-created")
def test_a_report_or_a_draft_creates_its_board_and_an_export_none(ev):
    ev.artifact_pushed(ev.artifact("report", "q.html", "R-1", "AX", "B"))
    ev.artifact_pushed(ev.artifact("draft", "q.html", "D-1"))
    ev.artifact_pushed(ev.artifact("export", "all.md", ""))
    boards = ev.boards()
    assert [(b["serial"], b.get("part_number"), b.get("board_revision")) for b in boards] == [
        ("D-1", None, None),
        ("R-1", "AX", "B"),
    ]
    for board in boards:
        assert TIME.fullmatch(board["created_at"]), board


@rule("E4.every-board")
def test_e4_lists_the_board_of_a_report_made_before_any_run(ev):
    """A report (E10) on a board no run was pushed to: E4 lists that board."""
    ev.artifact_pushed(ev.artifact("report", "early.html", "R-1", "AX", "B"))
    assert [(b["serial"], b.get("part_number")) for b in ev.boards()] == [("R-1", "AX")]


@rule("E4.filters", "E4.order", "E4.list")
def test_e4_filters_and_orders_the_boards_by_case_folding(ev):
    """Part number, revision, serial, each by case folding, "none" first: byte order would
    put ``AZ`` before ``ay`` and ``S1x`` before ``s0``."""
    boards = [
        ("S9", "AZ", "A"),
        ("S1", "ay", "A"),
        ("s0", "AX", "C"),
        ("S1x", "AX", "C"),
        ("S2", "AX", "b"),
        ("S3", "AX", None),
        ("zeta", None, None),
    ]
    for start, (serial, part, revision) in enumerate(boards):
        ev.pushed(ev.run(serial=serial, part_number=part, board_revision=revision, start=start))

    def listed(**query: str) -> list[str]:
        return [b["serial"] for b in ev.boards(**query)]

    assert listed() == ["zeta", "S3", "S2", "s0", "S1x", "S1", "S9"]
    assert not [b for b in ev.boards() if "entries" in b], "E4 lists a board with its entries"
    assert listed(part_number="ax") == ["S3", "S2", "s0", "S1x"]
    assert listed(part_number="") == ["zeta"]
    assert listed(board_revision="") == ["zeta", "S3"]
    assert listed(board_revision="C") == ["s0", "S1x"]
    assert listed(part_number="AX", board_revision="") == ["S3"]
    assert listed(part_number="nothing") == []


@pytest.mark.offer_aware
@rule("E5.addressing-400")
def test_a_call_naming_a_board_needs_its_three_parts(ev):
    """E5 and its read twin, E9; E6 and E15 when the server offers them: each refuses a call
    missing one of ``serial``, ``part_number`` and ``board_revision`` (§6.4 Addressing), and
    writes nothing."""
    run = ev.run(serial="A-1", part_number=None, board_revision=None)
    ev.pushed(run)
    ev.artifact_pushed(ev.artifact("report", "a.html", "A-1"))
    before = ev.pointer("A-1", TEST_A, None, None)
    board = ev.path("boards", "A-1")
    for query in ({"part_number": ""}, {"board_revision": ""}, {}):
        refused(ev.wire.send("GET", board, query=query), 400, "bad_request")
        read = ev.wire.send("POST", board + "/read", query=query, body={"have": []})
        refused(read, 400, "bad_request")
        if not ev.offers("E6", "E15"):
            continue
        current = ev.wire.send(
            "POST",
            ev.path("boards", "A-1", "tests", TEST_A, "current"),
            query=query,
            body={"run_id": None, "replaces": pointer_of(run), "machine_id": MACHINE},
        )
        refused(current, 400, "bad_request")
        entries = ev.wire.send(
            "POST",
            ev.path("boards", "B-9", "entries"),
            query=query,
            body={"entries": [board_entry("lot", "L", None)]},
        )
        refused(entries, 400, "bad_request")
    for query in (
        {"part_number": "", "board_revision": ""},
        {"serial": "A-1", "part_number": ""},
    ):
        artifact = ev.wire.send("GET", ev.path("artifacts", "report", "a.html"), query=query)
        refused(artifact, 400, "bad_request")
    assert ev.pointer("A-1", TEST_A, None, None) == before
    assert [b["serial"] for b in ev.boards()] == ["A-1"]
    assert ok(ev.get_artifact("report", "a.html", "A-1"))["name"] == "a.html"


# ---- §5.12 BoardResults, E5 ---------------------------------------------------------------------


@rule("5.12.tests-by-test-id", "5.12.include-current", "5.12.all-deleted-not-listed", "E5.board")
def test_e5_lists_tests_by_test_id_and_adds_their_current_runs(ev):
    runs = {
        TEST_C: [ev.run(test_id=TEST_C, test="C", start=0, ended=5)],
        TEST_A: [ev.run(test_id=TEST_A, start=1, ended=6), ev.run(start=2, ended=3)],
        TEST_B: [ev.run(test_id=TEST_B, test="B", start=3, ended=4)],
    }
    for test_runs in runs.values():
        for run in test_runs:
            ev.pushed(run)
    if ev.offers("E11"):  # a test whose runs are all deleted is not listed
        with server_target.unrecorded():
            gone = ev.run(test_id=TEST_D, test="D", start=4, ended=5)
            ev.pushed(gone)
            ok(ev.delete_run(gone["run_id"]))

    results = ev.board("SN-1", have=",".join(ev.held_versions()))
    assert results["format_version"] == 2
    assert [t["test_id"] for t in results["tests"]] == [TEST_A, TEST_B, TEST_C]
    by_test = {t["test_id"]: t for t in results["tests"]}
    assert [r["run_id"] for r in by_test[TEST_A]["runs"]] == sorted(
        (r["run_id"] for r in runs[TEST_A]), reverse=True
    )
    assert "current_runs" not in results

    have = ev.held_versions()
    full = ev.board("SN-1", include="current", have=",".join(have))
    current = {test_id: test_runs[0]["run_id"] for test_id, test_runs in runs.items()}
    assert {t["test_id"]: t["pointer"]["run_id"] for t in full["tests"]} == current
    assert sorted(r["run_id"] for r in full["current_runs"]) == sorted(current.values())
    for read in full["current_runs"]:
        assert read == ev.read_run(read["run_id"], have=have)
        assert read["current"] is True and "phases" in read and "files" in read

    query = {"part_number": "AX", "board_revision": "B"}
    twin = ev.wire.send(
        "POST",
        ev.path("boards", "SN-1", "read"),
        query=query,
        body={"include": "current", "have": have},
    )
    assert server_target.kit_tests(ok(twin)) == full

    whole, parts = parse(
        ok_reply(
            ev.wire.send("GET", ev.path("boards", "SN-1"), query={**query, "include": "current"})
        )
    )
    records = {
        f"sha256:{item['sha256']}" for read in full["current_runs"] for item in read["files"]
    }
    assert set(parts) == records
    assert server_target.kit_tests(whole) == full
    unknown = ev.board("SN-none-of-these")  # a board the server does not know: no run, no entry
    assert unknown["tests"] == [] and unknown["board"]["serial"] == "SN-none-of-these", unknown


# ---- §5.13 The pointer rule, E6 -----------------------------------------------------------------


@rule("5.13.tie-or-older-leaves")
def test_a_tie_an_older_run_and_an_incomplete_one_leave_the_pointer(ev):
    first = ev.run(start=0, ended=10)
    assert ev.pushed(first)["current"] is True
    tie = ev.run(start=5, ended=10)
    older = ev.run(start=20, ended=5)
    stopped = ev.run(start=30, complete=False)
    for run in (tie, older, stopped):
        assert ev.pushed(run)["current"] is False
    pointer = ev.pointer("SN-1", TEST_A)
    assert (pointer["run_id"], pointer["ended"]) == (first["run_id"], iso(10))
    have = ev.held_versions()
    assert [
        ev.read_run(r["run_id"], have=have)["current"] for r in (first, tie, older, stopped)
    ] == [
        True,
        False,
        False,
        False,
    ]


@rule("5.13.demoted-keeps-ended", "E6.replay")
def test_a_demoted_pointer_keeps_its_ended_and_only_a_newer_run_revives_it(ev):
    first = ev.run(start=0, ended=10)
    ev.pushed(first)
    answer = ok(ev.set_current("SN-1", "AX", "B", TEST_A, None, pointer_of(first)))
    assert answer["status"] == "saved"
    assert (answer["pointer"]["run_id"], answer["pointer"]["ended"]) == (None, iso(10))
    demoted = ev.pointer("SN-1", TEST_A)
    assert (demoted["run_id"], demoted["ended"]) == (None, iso(10))
    again = ev.set_current(
        "SN-1", "AX", "B", TEST_A, None, {"run_id": "x", "ended": iso(1)}, OTHER_MACHINE
    )
    assert ok(again)["status"] == "saved"
    assert ev.pointer("SN-1", TEST_A) == demoted
    for run in (ev.run(start=1, ended=5), ev.run(start=2, ended=10)):
        assert ev.pushed(run)["current"] is False
    assert ev.pointer("SN-1", TEST_A) == demoted
    newer = ev.run(start=3, ended=15)
    assert ev.pushed(newer)["current"] is True
    pointer = ev.pointer("SN-1", TEST_A)
    assert (pointer["run_id"], pointer["ended"]) == (newer["run_id"], iso(15))


@rule("5.13.set-by-hand-ended")
def test_a_run_set_by_hand_gives_the_pointer_its_ended(ev):
    late, early = ev.run(start=0, ended=30), ev.run(start=1, ended=10)
    ev.pushed(late)
    ev.pushed(early)
    answer = ok(ev.set_current("SN-1", "AX", "B", TEST_A, early["run_id"], pointer_of(late)))
    assert answer["status"] == "saved"
    assert (answer["pointer"]["run_id"], answer["pointer"]["ended"]) == (early["run_id"], iso(10))
    middle = ev.run(start=2, ended=20)
    assert ev.pushed(middle)["current"] is True
    assert ev.pointer("SN-1", TEST_A)["run_id"] == middle["run_id"]


@rule(
    "5.13.handoff-tiebreak", "5.13.handoff-none", "5.13.handoff-complete-only", "E11.answer-pointer"
)
def test_deleting_the_current_run_hands_the_pointer_on_by_ended_then_run_id(ev):
    """Y and Z end together, Y with the greater run id; X has the greatest run id but ended
    first; Z was pushed first; S, the newest, stopped before its end. Each delete hands on to
    the one §5.13 names, never S: the last delete leaves no pointer."""
    z = ev.run(start=10, ended=50)
    y = ev.run(start=20, ended=50)
    x = ev.run(start=30, ended=40)
    w = ev.run(start=0, ended=60)
    stopped = ev.run(start=70, complete=False)
    for run in (z, y, x, w, stopped):
        ev.pushed(run)
    assert ev.pointer("SN-1", TEST_A)["run_id"] == w["run_id"]
    for gone, heir in ((w, y), (y, z), (z, x)):
        answer = ok(ev.delete_run(gone["run_id"]))
        assert (answer["pointer"]["run_id"], answer["pointer"]["ended"]) == (
            heir["run_id"],
            heir["ended"],
        ), answer
        assert answer["pointer"]["test_id"] == TEST_A
        assert ev.pointer("SN-1", TEST_A)["run_id"] == heir["run_id"]
    answer = ok(ev.delete_run(x["run_id"]))
    assert "pointer" in answer and answer["pointer"] is None, ("never the stopped run", answer)
    assert ev.pointer("SN-1", TEST_A) is None  # S alone is left: no pointer
    oldest = ev.run(start=1, ended=2)
    assert ev.pushed(oldest)["current"] is True
    assert ev.pointer("SN-1", TEST_A)["run_id"] == oldest["run_id"]


@rule("E6.changed", "E6.replay")
def test_e6_answers_changed_over_a_moved_pointer_and_saved_to_a_replay(ev):
    older, newer = ev.run(start=0, ended=10), ev.run(start=1, ended=20)
    ev.pushed(older)
    ev.pushed(newer)
    before = ev.pointer("SN-1", TEST_A)
    stale = pointer_of(older)
    answer = ok(ev.set_current("SN-1", "AX", "B", TEST_A, older["run_id"], stale))
    assert answer["status"] == "changed"
    assert answer["pointer"] == before
    assert ev.pointer("SN-1", TEST_A) == before
    replay = ok(ev.set_current("SN-1", "AX", "B", TEST_A, newer["run_id"], stale, OTHER_MACHINE))
    assert replay["status"] == "saved"
    assert ev.pointer("SN-1", TEST_A) == before, "a replay writes nothing"
    moved = ok(ev.set_current("SN-1", "AX", "B", TEST_A, older["run_id"], pointer_of(newer)))
    assert moved["status"] == "saved"
    after = ev.pointer("SN-1", TEST_A)
    assert (after["run_id"], after["ended"]) == (older["run_id"], iso(10))
    assert moved["pointer"] == after


@rule("E6.no-pointer-yet", "E11.answer-pointer")
def test_e6_with_no_pointer_yet_takes_only_replaces_null(ev):
    """A test with a complete run and no pointer: its run was deleted when not current,
    the current one deleted after, then the first restored (it stays in the history)."""
    first, second = ev.run(start=0, ended=10), ev.run(start=1, ended=20)
    ev.pushed(first)
    ev.pushed(second)
    assert "pointer" not in ok(ev.delete_run(first["run_id"]))
    gone = ok(ev.delete_run(second["run_id"]))
    assert gone["pointer"] is None
    assert "pointer" not in ok(ev.restore_run(first["run_id"]))
    [item] = ev.board("SN-1")["tests"]
    assert item["pointer"] is None and [r["run_id"] for r in item["runs"]] == [first["run_id"]]

    answer = ok(ev.set_current("SN-1", "AX", "B", TEST_A, first["run_id"], pointer_of(second)))
    assert answer["status"] == "changed"
    assert answer["pointer"]["test_id"] == TEST_A and answer["pointer"]["run_id"] is None
    assert "ended" not in answer["pointer"], answer
    assert ev.pointer("SN-1", TEST_A) is None
    clear = ok(ev.set_current("SN-1", "AX", "B", TEST_A, None, None))
    assert clear["status"] == "saved"
    assert ev.pointer("SN-1", TEST_A) is None, "a clear with no pointer writes nothing"
    answer = ok(ev.set_current("SN-1", "AX", "B", TEST_A, first["run_id"], None))
    assert answer["status"] == "saved"
    pointer = ev.pointer("SN-1", TEST_A)
    assert (pointer["run_id"], pointer["ended"]) == (first["run_id"], iso(10))


@rule("E6.clear-no-pointer")
def test_e6_clearing_with_no_pointer_is_saved_whatever_replaces_says(ev):
    """A clear with no pointer is already satisfied (§6.4 E6): ``saved``, nothing written,
    even over a ``replaces`` naming a pointer that never was."""
    stale = {"run_id": new_run_id(0), "ended": iso(1)}
    for replaces in (stale, None):
        answer = ok(ev.set_current("SN-1", "AX", "B", TEST_C, None, replaces))
        assert answer == {"status": "saved", "pointer": {"test_id": TEST_C, "run_id": None}}, answer
    run = ev.run(test_id=TEST_C, start=0, ended=1)
    assert ev.pushed(run)["current"] is True, "the clear wrote a pointer"


@rule("E6.not-its-run-404", "E6.incomplete-400")
def test_e6_refuses_a_deleted_an_unknown_or_an_incomplete_run(ev):
    current = ev.run(start=0, ended=10)
    deleted = ev.run(start=1, ended=5)
    stopped = ev.run(start=2, complete=False)
    for run in (current, deleted, stopped):
        ev.pushed(run)
    ok(ev.delete_run(deleted["run_id"]))
    before = ev.pointer("SN-1", TEST_A)
    held = pointer_of(current)
    unknown = new_run_id(3)
    for run_id in (deleted["run_id"], unknown):
        refused(ev.set_current("SN-1", "AX", "B", TEST_A, run_id, held), 404, "not_found")
    other = ev.run(start=4, ended=6, test_id=TEST_B)
    ev.pushed(other)
    refused(ev.set_current("SN-1", "AX", "B", TEST_A, other["run_id"], held), 404, "not_found")
    refused(ev.set_current("SN-1", "AX", "B", TEST_A, stopped["run_id"], held), 400, "bad_request")
    assert ev.pointer("SN-1", TEST_A) == before


# ---- §6.4 E2: the search ------------------------------------------------------------------------


@rule("E2.filters", "E2.bound-without-measurement-400", "5.5.run-summary-light")
def test_e2_each_filter_finds_exactly_its_runs_newest_first(ev):
    def measured(name: str, **value: Any) -> list:
        return [
            {
                "ord": 1,
                "name": "Step 1",
                "attempt": 1,
                "measurements": [{"name": name, "ord": 1, **value}],
            }
        ]

    r1 = ev.run(
        serial="Fx-1", operator="Ann", machine_id="0000aaa1", firmware_version="2.4.1",
        test_version=3, definition_sha256=sha(b"d1"), start=1440, ended=1441,
        extra={"Customer": "ACME"}, phases=measured("Vout", value_num=5.0),
    )  # fmt: skip
    r2 = ev.run(
        serial="fx-2", board_revision="C", test="Ripple v2", operator="bob",
        machine_id="0000aaa2", firmware_version="2.5", verdict="FAIL", test_version=4,
        definition_sha256=sha(b"d2"), draft=True, start=2880, ended=2881,
        extra={"customer": "acme"}, phases=measured("vout", value_num=7.5),
    )  # fmt: skip
    r3 = ev.run(
        format_version=1, serial="FX-3", test_id=TEST_B, verdict="ERROR", lot="L-OLD",
        start=4320, ended=4321, phases=measured("Vout", value_text="n/a"),
    )  # fmt: skip
    r4 = ev.run(serial="Fx-1", start=7200, complete=False)
    r5 = ev.run(serial="Fx-1", test_id=TEST_B, start=5760, ended=5761)
    for run in (r1, r2, r3, r4, r5):
        ev.pushed(run)
    ok(ev.delete_run(r5["run_id"]))
    ev.entries(
        "fx-1",
        "ax",
        "b",
        [board_entry("lot", "L7", None), board_entry("field", "Lyon", None, "Site")],
    )
    i1, i2, i3, i4, i5 = (r["run_id"] for r in (r1, r2, r3, r4, r5))

    expected = {
        (): [i4, i3, i2, i1],
        (("serial", "FX-1"),): [i4, i1],
        (("operator", "ANN"),): [i1],
        (("machine_id", "0000AAA2"),): [i2],
        (("firmware_version", "2.4.1"),): [i1],
        (("test_id", TEST_A),): [i4, i2, i1],
        (("test", "RIPPLE"),): [i4, i3, i1],
        (("part_number", "ax"),): [i4, i2, i1],
        (("part_number", ""), ("board_revision", "")): [i3],
        (("board_revision", "c"),): [i2],
        (("lot", "l7"),): [i4, i1],
        (("lot", "l-old"),): [i3],
        (("field", "Site"), ("field_value", "LYON")): [i4, i1],
        (("field", "Customer"), ("field_value", "acme")): [i1],
        (("field", "site"), ("field_value", "lyon")): [],
        (("verdict", "FAIL,ERROR"),): [i3, i2],
        (("verdict", "PASS"),): [i1],
        (("test_version", "4"),): [i2],
        (("definition_sha256", sha(b"d1")),): [i1],
        (("draft", "true"),): [i2],
        (("draft", "false"),): [i4, i3, i1],
        (("from", r2["ended"]), ("to", r3["ended"])): [i2],
        (("from", r1["ended"]),): [i3, i2, i1],
        (("measurement", "VOUT"),): [i3, i2, i1],
        (("measurement", "vout"), ("value_min", "5"), ("value_max", "7.5")): [i2, i1],
        (("measurement", "Vout"), ("value_min", "5.1")): [i2],
        (("measurement", "Vout"), ("value_max", "4.9")): [],
        (("deleted", "include"),): [i4, i5, i3, i2, i1],
        (("deleted", "only"),): [i5],
    }
    for query, run_ids in expected.items():
        answer = ev.search(**dict(query))
        assert [row["run_id"] for row in answer["runs"]] == run_ids, query
        assert answer["cursor"] is None, query
    for row in ev.search(deleted="include")["runs"]:
        assert not set(SUMMARY_DROPS) & set(row), row
        assert row["project_id"] == ev.project
        assert row["current"] is (row["run_id"] in (i1, i2, i3)), row
    [gone] = ev.search(deleted="only")["runs"]
    assert gone["deleted_reason"] == "Wrong board tested" and TIME.fullmatch(gone["deleted_at"])

    def search(**query: str) -> Reply:
        return ev.wire.send("GET", "runs", query={"project_id": ev.project, **query})

    for bound in ({"value_min": "1"}, {"value_max": "9"}):
        refused(search(**bound), 400, "bad_request")
    refused(search(test_id="Ripple"), 400, "bad_request")
    refused(search(colour="red"), 400, "unsupported_filter")


@rule("E2.across-projects", "E2.order")
def test_e2_without_a_project_searches_every_project_by_run_id_then_project_id(ev):
    serial = f"X-{server_target.fresh_suffix()}"
    first, second = sorted([ev, ev.other()], key=lambda e: e.project)
    shared = first.run(serial=serial, start=10, ended=11)
    first.pushed(shared)
    second.pushed(copy.deepcopy(shared))
    newer = second.run(serial=serial, start=20, ended=21)
    older = first.run(serial=serial, start=0, ended=1)
    second.pushed(newer)
    first.pushed(older)
    rows = first.search(across=True, serial=serial)["runs"]
    assert [(r["run_id"], r["project_id"]) for r in rows] == [
        (newer["run_id"], second.project),
        (shared["run_id"], first.project),
        (shared["run_id"], second.project),
        (older["run_id"], first.project),
    ]
    for project in (first, second):
        assert {r["project_id"] for r in project.search(serial=serial)["runs"]} == {project.project}


def page_through(fetch, limit: str | None) -> list[list[dict]]:
    """Every page of a paged answer, following its cursor until ``null``."""
    pages, cursor = [], None
    while True:
        query = {} if limit is None else {"limit": limit}
        if cursor is not None:
            query["cursor"] = cursor
        answer = fetch(**query)
        rows = answer.get("runs", answer.get("artifacts"))
        pages.append(rows)
        cursor = answer["cursor"]
        if cursor is None:
            return pages
        assert rows, "a page with a cursor holds rows"
        assert len(pages) < 1000


@rule("E2.limit-cursor")
def test_e2_pages_501_runs_to_completion(ev):
    """100 rows by default; ``limit`` honoured up to 500 at least; the cursor, sent back as
    given, pages to completion, ``null`` on the last page, each run once, in order."""
    run_ids = []
    for minute in range(501):
        run = ev.run(start=minute, complete=False, files=[])
        ev.pushed(run, parts=False)
        run_ids.append(run["run_id"])
    run_ids.sort(reverse=True)
    first = ev.search()
    assert [r["run_id"] for r in first["runs"]] == run_ids[:100]
    assert isinstance(first["cursor"], str) and first["cursor"]
    pages = page_through(ev.search, "500")
    assert [len(p) for p in pages] == [500, 1]
    assert [r["run_id"] for p in pages for r in p] == run_ids
    big = ev.search(limit="100000")
    assert len(big["runs"]) >= 500
    assert [r["run_id"] for r in big["runs"]] == run_ids[: len(big["runs"])]
    pages = page_through(ev.search, "7")
    assert all(len(p) == 7 for p in pages[:-1]) and 0 < len(pages[-1]) <= 7
    assert [r["run_id"] for p in pages for r in p] == run_ids


# ---- §5.10 Artifacts, E8–E10, E12 ---------------------------------------------------------------


@rule("5.10.artifact-fields", "5.10.info-key-order", "E8.light")
def test_an_artifacts_fields_and_records_read_back_as_pushed(ev):
    """Every field of §5.10, ``info`` and a draft's ``content`` with their keys in their own
    order at every level; E8's rows leave out ``info`` unless asked, a draft's ``content``
    always."""
    info = {"version": "1.0", "metadata": {"z": "last", "a": "first", "m": [{"y": 1, "b": 2}]}}
    report = ev.artifact(
        "report", "2026-09-20_full-qual.html", "SN0042", "AX-2210", "B",
        info=info, work_order="WO-7", fields={"Customer": "ACME", "Bay": "3"},
        generated=iso(1), verdict="FAIL", operator="jdoe",
        tests=["dcdc_efficiency", "Ripple"], kit_extension={"b": 1, "a": 2},
    )  # fmt: skip
    content = {"zeta": {"y": [3, 1], "b": None}, "alpha": "Tést", "mid": {"k": True, "c": 1.5}}
    draft = ev.artifact("draft", "edit.html", "SN0042", "AX-2210", "B", content=content)
    saved = ev.artifact_pushed(report)
    assert saved["generation"] == 1
    ev.artifact_pushed(draft)
    have = ",".join(ev.held_versions())
    read = ok(ev.get_artifact("report", report["name"], "SN0042", "AX-2210", "B", have=have))
    assert set(read) == set(report) | {"generation"}, sorted(set(read) ^ set(report))
    for key, value in report.items():
        assert read[key] == value, key
    assert json.dumps(read["info"]) == json.dumps(info)
    read = ok(ev.get_artifact("draft", "edit.html", "SN0042", "AX-2210", "B"))
    assert json.dumps(read["content"]) == json.dumps(content)
    assert read["sha256"] == draft["sha256"] and "generation" not in read

    rows = ev.list_artifacts()["artifacts"]
    assert [(r["kind"], r["name"]) for r in rows] == [  # a time first, then none
        ("report", report["name"]),
        ("draft", "edit.html"),
    ]
    for row in rows:
        assert "info" not in row and "content" not in row, row
    assert {k: v for k, v in rows[0].items()} == {
        k: v for k, v in dict(report, generation=1).items() if k != "info"
    }
    rows = ev.list_artifacts(include="info")["artifacts"]
    assert "content" not in rows[1] and json.dumps(rows[0]["info"]) == json.dumps(info)


@rule("5.10.serial-by-kind-400")
def test_an_export_with_a_serial_and_a_report_or_draft_without_one_are_refused(ev):
    for art in (
        ev.artifact("export", "all.md", "SN-1"),
        ev.artifact("report", "r.html", ""),
        ev.artifact("draft", "d.html", ""),
    ):
        refused(ev.push_artifact(art), 400, "bad_request")
    assert ev.list_artifacts(deleted="include")["artifacts"] == []
    assert ev.boards() == []


@rule("E10.info-only-new-generation")
def test_the_same_file_with_another_info_is_the_next_generation(ev):
    report = ev.artifact("report", "r.html", "SN-1", "AX", "B", info={"run": 1})
    assert ev.artifact_pushed(report)["generation"] == 1
    assert ok(ev.push_artifact(report))["status"] == "already"
    changed = dict(report, info={"run": 2})
    answer = ok(ev.push_artifact(changed))
    assert answer["status"] == "saved" and answer["artifact"]["generation"] == 2
    rows = ev.list_artifacts(generations="all", include="info")["artifacts"]
    assert [(r["generation"], r["info"]) for r in rows] == [(2, {"run": 2}), (1, {"run": 1})]


@rule("E9.draft-generation-404")
def test_a_drafts_generation_is_not_found(ev):
    ev.artifact_pushed(ev.artifact("draft", "d.html", "SN-1", "AX", "B"))
    refused(ev.get_artifact("draft", "d.html", "SN-1", "AX", "B", generation="1"), 404, "not_found")
    assert ok(ev.get_artifact("draft", "d.html", "SN-1", "AX", "B"))["name"] == "d.html"


@pytest.mark.offer_aware
@rule("E8.order-ties", "E8.filters", "E8.light")
def test_e8_orders_filters_and_pages_the_artifacts(ev):
    """Artifacts with no ``generated``: E8's ties alone order them, by kind, the board and
    the name, then the generation newest first. ``deleted`` when the server offers E12."""
    zeta = ev.artifact("export", "Zeta.docx", "")
    alpha = ev.artifact("export", "alpha.md", "")
    r1 = ev.artifact(
        "report", "r1.html", "S1", "AX", "B", work_order="WO-7", tests=["Ripple", "Load"]
    )
    r1b = dict(
        ev.artifact("report", "r1.html", "S1", "AX", "B", work_order="WO-7", tests=["Ripple"])
    )
    r0 = ev.artifact("report", "R0.html", "S1")
    gone = ev.artifact("report", "a.html", "S0", "AX", "B")
    later = ev.artifact("report", "a2.html", "S2", "AX", "B")
    draft = ev.artifact("draft", "d.html", "S1", "AX", "B", work_order="wo-7")
    for art in (zeta, r1, later, gone, alpha, r0, draft, r1b):
        ev.artifact_pushed(art)
    deletes = ev.offers("E12")
    if deletes:
        ok(ev.delete_artifact(gone))

    def listed(**query: str) -> list[tuple]:
        rows = ev.list_artifacts(**query)["artifacts"]
        for row in rows:
            assert "info" not in row and "content" not in row, row
        return [(r["kind"], r["name"], r.get("generation")) for r in rows]

    d, a, z = ("draft", "d.html", None), ("export", "alpha.md", 1), ("export", "Zeta.docx", 1)
    rr0, rr1, ra = ("report", "R0.html", 1), ("report", "r1.html", 2), ("report", "a.html", 1)
    ra2 = ("report", "a2.html", 1)
    live = [] if deletes else [ra]  # a.html deleted when the server offers E12
    assert listed() == [d, a, z, *live, rr0, rr1, ra2]
    rr1_1 = ("report", "r1.html", 1)
    assert listed(generations="all") == [d, a, z, *live, rr0, rr1, rr1_1, ra2]
    if deletes:
        assert listed(deleted="include") == [d, a, z, ra, rr0, rr1, ra2]
        assert listed(deleted="only") == [ra]
    assert listed(work_order="wo-7") == [d, rr1]
    assert listed(test="RIPPLE") == [rr1]
    assert listed(test="load") == []
    assert listed(test="load", generations="all") == [("report", "r1.html", 1)]
    assert listed(kind="report", serial="s1") == [rr0, rr1]
    assert listed(serial="") == [a, z]
    assert listed(serial="S1", part_number="", board_revision="") == [rr0]
    assert listed(part_number="ax") == [d, *live, rr1, ra2]
    pages = page_through(ev.list_artifacts, "2")
    assert [len(p) for p in pages] == ([2, 2, 2] if deletes else [2, 2, 2, 1])
    assert [(r["kind"], r["name"]) for p in pages for r in p] == [x[:2] for x in listed()]
    refused(
        ev.wire.send("GET", ev.path("artifacts"), query={"colour": "red"}),
        400,
        "unsupported_filter",
    )


@rule("E8.order", "E8.order-ties")
def test_e8_lists_newest_first_by_generated_and_pages_in_that_order(ev):
    """Newest first by ``generated``, whatever the kind, board and name; equal times by the
    ties; no ``generated`` after every time; a name's generations each placed by its own time;
    pages follow that order."""
    pushed = [
        ev.artifact("report", "a-old.html", "S1", "AX", "B", generated=iso(1)),
        ev.artifact("report", "z-new.html", "S1", "AX", "B", generated=iso(9)),
        ev.artifact("export", "m-mid.md", "", generated=iso(5)),
        ev.artifact("report", "b-tie.html", "S2", "AX", "B", generated=iso(5)),
        ev.artifact("report", "c-tie.html", "S1", "AX", "B", generated=iso(5)),
        ev.artifact("report", "A-none.html", "S1", "AX", "B"),
        ev.artifact("draft", "d.html", "S1", "AX", "B"),
    ]
    for art in pushed:
        ev.artifact_pushed(art)
    again = ev.artifact("report", "a-old.html", "S1", "AX", "B", generated=iso(7), info={"n": 2})
    assert ev.artifact_pushed(again)["generation"] == 2

    def names(**query: str) -> list[tuple]:
        return [(r["name"], r.get("generation")) for r in ev.list_artifacts(**query)["artifacts"]]

    newest = [("z-new.html", 1), ("a-old.html", 2), ("m-mid.md", 1), ("c-tie.html", 1),
              ("b-tie.html", 1), ("d.html", None), ("A-none.html", 1)]  # fmt: skip
    assert names() == newest
    every = [*newest[:5], ("a-old.html", 1), *newest[5:]]
    assert names(generations="all") == every
    pages = page_through(ev.list_artifacts, "2")
    assert [len(p) for p in pages] == [2, 2, 2, 1]
    assert [(r["name"], r.get("generation")) for p in pages for r in p] == newest


@rule("E8.from-to", "E8.from-to-bounds")
def test_e8_keeps_generated_within_from_inclusive_to_exclusive(ev):
    """``from`` and ``to`` keep ``generated`` within ``[from, to)`` (raw: the client would
    narrow again); with a bound, no ``generated`` never matches; each generation by its own
    time, the latest alone by default."""
    for name, minutes in (("t1.html", 1), ("t2.html", 2), ("t3.html", 3), ("t4.html", 4)):
        ev.artifact_pushed(ev.artifact("report", name, "S1", "AX", "B", generated=iso(minutes)))
    ev.artifact_pushed(ev.artifact("report", "none.html", "S1", "AX", "B"))
    ev.artifact_pushed(ev.artifact("report", "g.html", "S1", "AX", "B", generated=iso(0)))
    second = ev.artifact("report", "g.html", "S1", "AX", "B", generated=iso(6), info={"n": 2})
    assert ev.artifact_pushed(second)["generation"] == 2

    def names(**query: str) -> list[tuple]:
        return [(r["name"], r.get("generation")) for r in ev.list_artifacts(**query)["artifacts"]]

    t1, t2, t3, t4 = ("t1.html", 1), ("t2.html", 1), ("t3.html", 1), ("t4.html", 1)
    g1, g2 = ("g.html", 1), ("g.html", 2)
    assert names(**{"from": iso(2), "to": iso(4)}) == [t3, t2]  # from in, to out
    assert names(**{"from": iso(2)}) == [g2, t4, t3, t2]
    assert names(to=iso(2)) == [t1]  # g's latest is at 6
    assert names(to=iso(1)) == []
    assert names(**{"from": iso(4), "to": iso(4)}) == []
    assert names(to=iso(2), generations="all") == [t1, g1]
    assert names(**{"from": iso(3), "to": iso(7)}, generations="all") == [g2, t4, t3]
    assert ("none.html", 1) in names() and ("none.html", 1) not in names(to=iso(60))


@rule("E8.verdict")
def test_e8_keeps_the_verdicts_asked(ev):
    """``verdict``: one or more, comma-separated, compared exactly (raw: the client would
    narrow again)."""
    for name, verdict in (("p.html", "PASS"), ("f.html", "FAIL"), ("e.html", "ERROR")):
        ev.artifact_pushed(ev.artifact("report", name, "S1", "AX", "B", verdict=verdict))
    ev.artifact_pushed(ev.artifact("report", "n.html", "S1", "AX", "B"))

    def names(verdict: str) -> list[str]:
        return [r["name"] for r in ev.list_artifacts(verdict=verdict)["artifacts"]]

    assert names("FAIL") == ["f.html"]
    assert names("PASS,ERROR") == ["e.html", "p.html"]
    assert names("ERROR,FAIL,PASS") == ["e.html", "f.html", "p.html"]
    assert names("SKIP") == []


@rule("E8.limit-cursor")
def test_e8_pages_501_artifacts_to_completion(ev):
    names = [f"d-{i:04d}.html" for i in range(501)]
    for name in names:
        ev.artifact_pushed(ev.artifact("draft", name, "SN-1", content={"n": name}))
    first = ev.list_artifacts()
    assert [r["name"] for r in first["artifacts"]] == names[:100] and first["cursor"]
    pages = page_through(ev.list_artifacts, "500")
    assert [len(p) for p in pages] == [500, 1]
    assert [r["name"] for p in pages for r in p] == names
    big = ev.list_artifacts(limit="100000")["artifacts"]
    assert len(big) >= 500 and [r["name"] for r in big] == names[: len(big)]


@rule("E12.replays")
def test_e12_replays_change_nothing(ev):
    report = ev.artifact("report", "r.html", "SN-1", "AX", "B")
    ev.artifact_pushed(report)
    ev.artifact_pushed(dict(report, info={"again": True}))
    first = ok(ev.delete_artifact(report, reason="first"))
    assert first["deleted_reason"] == "first"
    again = ok(ev.delete_artifact(report, reason="second"))
    assert {k: again[k] for k in again if k.startswith("deleted_")} == {
        k: first[k] for k in first if k.startswith("deleted_")
    }
    assert ev.list_artifacts(generations="all")["artifacts"] == []
    back = ok(ev.restore_artifact(report))
    assert not any(k.startswith("deleted_") for k in back), back
    assert ok(ev.restore_artifact(report)) == back
    assert [r["generation"] for r in ev.list_artifacts(generations="all")["artifacts"]] == [2, 1]


@rule("E12.acts-on-name")
def test_e12_acts_on_the_name_and_answers_its_newest_generation(ev):
    """A delete or a restore naming a generation still acts on the whole name, and answers the
    newest generation (§6.4 E12)."""
    report = ev.artifact("report", "r.html", "SN-1", "AX", "B")
    assert ev.artifact_pushed(report)["generation"] == 1
    assert ev.artifact_pushed(dict(report, info={"again": True}))["generation"] == 2
    named = {"machine_id": MACHINE, **ev.named(report), "generation": 1}
    body = {**named, "reason": "Wrong board"}
    gone = ok(ev.wire.send("POST", ev.path("artifacts", "delete"), role="manager", body=body))
    assert (gone["generation"], gone["deleted_reason"]) == (2, "Wrong board"), gone
    assert ev.list_artifacts(generations="all")["artifacts"] == []
    back = ok(ev.wire.send("POST", ev.path("artifacts", "restore"), role="manager", body=named))
    assert back["generation"] == 2 and not any(k.startswith("deleted_") for k in back), back
    assert [r["generation"] for r in ev.list_artifacts(generations="all")["artifacts"]] == [2, 1]


# ---- E16 the operators --------------------------------------------------------------------------


@rule("E16.operators", "E16.deleted-included", "E16.order")
def test_e16_lists_each_operator_once_deleted_runs_included_by_case_folding(ev):
    """Raw: the app would fold and sort again. Each operator once (another case of a name is
    that name, in its first spelling), a deleted run's included, a run with none adding none,
    ascending by case folding; a project never seen has none."""
    assert ok(ev.wire.send("GET", ev.path("operators"))) == {"operators": []}
    for start, operator in ((0, "jdoe"), (2, "Bob"), (4, "JDOE"), (6, "alice"), (8, "Zoé")):
        ev.pushed(ev.run(start=start, ended=start + 1, operator=operator))
    ev.pushed(ev.run(start=10, ended=11))  # no operator
    gone = ev.run(start=12, ended=13, operator="carol")
    ev.pushed(gone)
    ok(ev.delete_run(gone["run_id"]))
    listed = ok(ev.wire.send("GET", ev.path("operators")))
    assert listed == {"operators": ["alice", "Bob", "carol", "jdoe", "Zoé"]}


# ---- E17 a test's readings ----------------------------------------------------------------------


def _phase(ord_: int, attempt: int, *measurements: dict) -> dict:
    """A phase holding *measurements* (their ``name``, ``ord`` and value, as sent)."""
    return {"ord": ord_, "name": "Step 1", "attempt": attempt, "measurements": list(measurements)}


def readings(ev: Evidence, **query: str) -> Reply:
    """E17."""
    return ev.wire.send("GET", ev.path("readings"), query=query)


@rule("E17.readings", "E17.units", "E17.deleted-included", "E17.order")
def test_e17_lists_a_tests_readings_once_with_their_units_deleted_runs_included(ev):
    """Raw: the app would fold and sort again. Every measurement name recorded by the runs that
    recorded the test's name (upper or lower case alike), in any phase and attempt, each once in
    its first spelling with the first unit received under it (none: no ``units``), a deleted
    run's included, ascending by case folding; another test's never; a project never seen has
    none."""
    assert ok(readings(ev, test="Ripple")) == {"readings": []}
    first = _phase(1, 1, {"name": "{2}", "ord": 1, "value_num": 1.0},
                   {"name": "vout", "ord": 2, "value_num": 5.0, "units": "V"})  # fmt: skip
    ev.pushed(ev.run(start=0, ended=1, test="Ripple", phases=[first]))
    retried = [
        _phase(1, 1, {"name": "VOUT", "ord": 1, "value_num": 5.1, "units": "mV"}),
        _phase(2, 2, {"name": "{2}", "ord": 2, "value_text": "x", "units": "s"},
               {"name": "Zéro", "ord": 3, "value_num": 0}),
    ]  # fmt: skip
    ev.pushed(ev.run(start=2, ended=3, test="RIPPLE", phases=retried))
    other = _phase(1, 1, {"name": "Alpha", "ord": 1, "value_num": 1.0, "units": "A"})
    ev.pushed(ev.run(start=4, ended=5, test="Other", test_id=TEST_B, phases=[other]))
    gone = ev.run(start=6, ended=7, test="ripple",
                  phases=[_phase(1, 1, {"name": "Beta", "ord": 1, "value_num": 2.0, "units": "W"})])  # fmt: skip
    ev.pushed(gone)
    ok(ev.delete_run(gone["run_id"]))
    listed = ok(readings(ev, test="rIPPLE"))
    assert listed == {"readings": [
        {"name": "Beta", "units": "W"}, {"name": "vout", "units": "V"}, {"name": "Zéro"},
        {"name": "{2}", "units": "s"},
    ]}  # fmt: skip
    assert ok(readings(ev, test="other")) == {"readings": [{"name": "Alpha", "units": "A"}]}
    assert ok(readings(ev, test="Never ran")) == {"readings": []}


@rule("E17.test-required")
def test_e17_names_its_test_or_is_refused(ev):
    """``test`` missing or empty: ``400 bad_request``, for a project holding readings too, never
    every test's readings; a fault of the request's form, so a project never seen is refused
    the same (2.13, step 5)."""
    ev.pushed(ev.run(phases=[_phase(1, 1, {"name": "Vout", "ord": 1, "value_num": 5.0})]))
    refused(readings(ev), 400, "bad_request")
    refused(readings(ev, test=""), 400, "bad_request")
    refused(readings(ev.other()), 400, "bad_request")


# ---- E13 the purge, E14 the benches -------------------------------------------------------------


@rule("E13.counts-to-delete", "5.8.runfile-role-mimetype")
def test_e13_counts_what_went_and_lists_the_declared_storages_blobs(ev):
    """A run whose picture sits in a storage the project declared (S2): read back with each
    file's storage; purged, its picture is listed in ``to_delete`` under that storage. An
    artifact's generations count once."""
    identity = f"/srv/kit-evidence-{server_target.fresh_suffix()}"
    declared = ok(
        ev.wire.send(
            "POST",
            "storages",
            body={"backend": "filesystem", "params": {"root": identity}, "identity": identity},
        )
    )["storage"]
    assert declared["declared"] is True and declared["active"] is False
    run = ev.run()
    picture = ev.file(
        "scope.png", secrets.token_bytes(40), "attachment", "image/png", declared["store_id"]
    )
    run["files"].append(picture)
    if declared["reachable"]:
        pytest.skip("the server reaches the folder this case declares: it cannot hold its blob")
    ev.pushed(run)
    same_files(ev.read_run(run["run_id"], have=ev.held_versions())["files"], run["files"])
    other = ev.run(start=5, ended=6)
    ev.pushed(other)
    report = ev.artifact("report", "r.html", "SN-1", "AX", "B")
    ev.artifact_pushed(report)
    ev.artifact_pushed(dict(report, info={"second": True}))
    for run_id in (run["run_id"], other["run_id"]):
        ok(ev.delete_run(run_id))
    ok(ev.delete_artifact(report))
    items = [{"run_id": run["run_id"]}, {"run_id": other["run_id"]}, ev.named(report)]
    answer = ev.purge(items)
    assert (answer["runs"], answer["artifacts"]) == (2, 1), answer
    assert sorted(answer["purged"]["runs"]) == sorted([run["run_id"], other["run_id"]])
    assert answer["purged"]["artifacts"] == [ev.named(report)]
    assert answer["to_delete"] == {str(declared["store_id"]): [f"sha256:{picture['sha256']}"]}
    again = ev.purge(items)
    assert (again["runs"], again["artifacts"]) == (0, 0)
    assert again["purged"] == {"runs": [], "artifacts": []}


@rule("5.8.mimetype-absent")
def test_a_run_file_sent_without_its_type_reads_back_without_one(ev):
    """No default filled in (§5.8): a file sent without ``mimetype`` reads back without it,
    beside one sent with its type."""
    run = ev.run()
    run["files"].append(ev.file("capture.bin", secrets.token_bytes(40), "attachment", None))
    ev.pushed(run)
    files = {f["name"]: f for f in ev.read_run(run["run_id"], have=ev.held_versions())["files"]}
    assert "mimetype" not in files["capture.bin"], files["capture.bin"]
    assert files["results.json"]["mimetype"] == "application/json", files["results.json"]


@rule("5.14.label")
def test_a_benchs_label_is_the_station_the_latest_request_named(ev):
    """§5.14: absent until a request names a station; then the latest one named, a run's
    ``station_id`` or an entry's ``bench``; a request naming none leaves it."""
    machine = secrets.token_hex(4)

    def bench() -> dict:
        benches = ok(ev.wire.send("GET", "benches"))["benches"]
        [mine] = [b for b in benches if b["machine_id"] == machine]
        return mine

    ev.pushed(ev.run(machine_id=machine, start=0, ended=1))
    assert "label" not in bench(), bench()
    ev.pushed(ev.run(machine_id=machine, start=2, ended=3, station_id=f"KIT-A-{machine}"))
    assert bench()["label"] == f"KIT-A-{machine}"
    entry = board_entry("lot", "L", None)
    entry.update(machine_id=machine, bench=f"KIT-B-{machine}")
    ev.entries("SN-1", "AX", "B", [entry])
    assert bench()["label"] == f"KIT-B-{machine}"
    ev.pushed(ev.run(machine_id=machine, start=4, ended=5))
    assert bench()["label"] == f"KIT-B-{machine}", "a request naming no station changed it"


@rule("E14.benches", "5.14.bench")
def test_e14_lists_a_bench_by_the_requests_that_named_it(ev):
    machine = secrets.token_hex(4)
    station = f"KIT-{machine}"

    def bench() -> dict:
        benches = ok(ev.wire.send("GET", "benches"))["benches"]
        mine = [b for b in benches if b["machine_id"] == machine]
        assert len(mine) == 1, mine
        return mine[0]

    ev.pushed(ev.run(machine_id=machine, station_id=station))
    first = bench()
    assert first["format_version"] == 1 and first["label"] == station
    assert TIME.fullmatch(first["first_seen"]) and TIME.fullmatch(first["last_seen"])
    assert first["first_seen"] <= first["last_seen"]
    entry = board_entry("lot", "L", None)
    entry["machine_id"] = machine
    ev.entries("SN-1", "AX", "B", [entry])
    later = bench()
    assert later["first_seen"] == first["first_seen"]
    assert later["last_seen"] >= first["last_seen"]


# ---- What only the reference server can show ----------------------------------------------------


@rule("E13.expired-purged")
def test_e13_purges_what_was_deleted_strictly_longer_ago_than_the_retention(local):
    """The server's clock moved on: exactly the retention period after a delete keeps it; a
    second more purges it, with no item named. (No call moves a server's clock: the kit
    checks this on the reference server only.)"""
    running, ev = local
    run = ev.run()
    ev.pushed(run)
    deleted_at = ok(ev.delete_run(run["run_id"]))["run"]["deleted_at"]
    report = ev.artifact("report", "r.html", "SN-1", "AX", "B")
    ev.artifact_pushed(report)
    ok(ev.delete_artifact(report))
    days = ok(ev.wire.send("GET", "info"))["limits"]["retention_days"]
    moment = datetime.strptime(deleted_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    running.state.clock = lambda: moment + timedelta(days=days)
    kept = ev.purge()
    assert (kept["runs"], kept["artifacts"]) == (0, 0), kept
    running.state.clock = lambda: moment + timedelta(days=days, seconds=2)
    went = ev.purge()
    assert went["purged"]["runs"] == [run["run_id"]] and went["runs"] == 1
    assert went["purged"]["artifacts"] == [ev.named(report)]
    refused(ev.push(run), 409, "deleted")


@rule("E13.pointer-of-purged")
def test_e13_purging_a_run_a_pointer_still_names_hands_the_pointer_on(local):
    """Only an imported delete on a server offering E6 leaves a pointer naming a deleted run
    (§6.7; without E6 it hands the pointer on): the kit has no importer login, so this runs on
    the reference server only."""
    _running, ev = local
    server_target.needs(ev.wire.base_url, "E6")
    older, current = ev.run(start=0, ended=10), ev.run(start=1, ended=20)
    ev.pushed(older)
    ev.pushed(current)
    body = {"machine_id": MACHINE, "reason": "moved", "import": True}
    reply = ev.wire.send(
        "POST", ev.path("runs", current["run_id"], "delete"), role="importer", body=body
    )
    assert "pointer" not in ok(reply)
    assert ev.pointer("SN-1", TEST_A)["run_id"] == current["run_id"]
    ev.purge([{"run_id": current["run_id"]}])
    pointer = ev.pointer("SN-1", TEST_A)
    assert (pointer["run_id"], pointer["ended"]) == (older["run_id"], iso(10))


@rule("5.5.run-missing", "5.10.artifact-missing")
def test_a_file_gone_from_the_api_storage_is_reported_missing(local):
    """A run or a report whose bytes are gone still opens, the file in ``missing`` (§2.1 rule
    11). No call takes a blob away: the kit checks this on the reference server only."""
    running, ev = local
    run = ev.run()
    ev.pushed(run)
    report = ev.artifact("report", "r.html", "SN-1", "AX", "B")
    ev.artifact_pushed(report)
    for digest in (run["files"][0]["sha256"], report["sha256"]):
        running.state.blobs.remove(ev.project, digest)
    whole, parts = parse(ok_reply(ev.wire.send("GET", ev.path("runs", run["run_id"]))))
    assert whole["missing"] == {"results.json": "not_found"} and parts == {}
    whole, parts = parse(ok_reply(ev.get_artifact("report", "r.html", "SN-1", "AX", "B")))
    assert whole["missing"] == {"r.html": "not_found"} and parts == {}
