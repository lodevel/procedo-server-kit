"""A known, varied data set written to the server under test, and the check that it is all still
there, byte for byte: for a backup and its restore, and for an upgrade.

:func:`fill` writes, under ids and names of its own, what a lab's server holds: a project (text
and binary files, one over 1 MiB sent alone first, a bundle), changes to it, two releases of a
test, a library item, runs with their files (a picture, a large file) on a board, annotation and
board entries, a report in two generations and an export, a run and an export deleted (in the
trash). It then reads everything back once and writes a **manifest**, a local JSON file: each
read (call, path, query) with the answer the server gave, and every blob (owner, sha256, size).

:func:`verify` makes the same reads again and compares each answer with the manifest's (the
change token aside: opaque, a server may issue a new one), then downloads every blob alone
(B2) and checks its hash: what a restore or an upgrade lost, changed or added is listed.

The kit's ``kit_data.py`` runs both against the server the environment names
(``server_target``): ``fill`` before the backup or the upgrade, ``verify`` after.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from pathlib import Path
from typing import Any

import server_target
from test_server_contract_evidence import (
    MACHINE,
    TEST_A,
    Evidence,
    Wire,
    board_entry,
    comment,
    override,
    parse,
)
from test_server_contract_projects import a_hex, a_zip, form, ref, text

#: The manifest's format.
FORMAT = 1
#: Answer fields a server may give anew on the same data: the change token (opaque, §3.1).
VOLATILE = ("token",)
MIB = 1024 * 1024


class DatasetError(Exception):
    """A write of :func:`fill` the server refused, or a manifest that cannot be read."""


def _bytes(size: int, label: str) -> bytes:
    """*size* bytes of this data set's own, never UTF-8 (they start with 0xff)."""
    out, block = bytearray(b"\xff"), hashlib.sha256(f"{label}{secrets.token_hex(8)}".encode())
    while len(out) < size:
        out += block.digest()
        block = hashlib.sha256(block.digest())
    return bytes(out[:size])


def _store(wire: Wire) -> int:
    storages = _answer(wire.send("GET", "storages"), "S1")["storages"]
    found = [s["store_id"] for s in storages if s["backend"] == "api"]
    if not found:
        raise DatasetError("the server lists no api storage: the data set sends its bytes there")
    return found[0]


def _answer(reply: Any, what: str) -> dict:
    if reply.status != 200:
        raise DatasetError(f"{what}: the server answered {reply.status}: {reply.body[:400]!r}")
    return parse(reply)[0]


class _Filler:
    """The writes of one data set, and what it reads back."""

    def __init__(self, wire: Wire) -> None:
        self.wire = wire
        self.store = _store(wire)
        self.suffix = secrets.token_hex(4)
        self.pid = server_target.fresh_id()
        self.ev = Evidence(wire, self.pid, self.store)
        self.blobs: list[tuple[str, str, int]] = []  # (owner, sha256 hex, size)
        self.reads: list[dict] = []

    # ---- Writing -------------------------------------------------------------------------

    def blob(self, owner: str, data: bytes) -> None:
        self.blobs.append((owner, hashlib.sha256(data).hexdigest(), len(data)))

    def alone(self, owner: str, data: bytes) -> None:
        """B1: a file over 1 MiB sent alone first."""
        path = f"storages/{self.store}/blobs/{owner}/{hashlib.sha256(data).hexdigest()}"
        reply = self.wire.send("POST", path, data=data, content_type="application/octet-stream")
        _answer(reply, "B1")
        self.blob(owner, data)

    def post(self, path: str, obj: dict, parts: list[bytes] = (), role: str = "bench") -> dict:
        if parts:
            body, kind = form(obj, list(parts))
            reply = self.wire.send("POST", path, role=role, data=body, content_type=kind)
        else:
            reply = self.wire.send("POST", path, role=role, body=obj)
        return _answer(reply, f"POST {path}")

    def project(self) -> None:
        """P11, P9, P10: a project, changed, a test released twice."""
        board_pdf, big = _bytes(6000, "board"), _bytes(MIB + 4096, "big")
        bundle = a_zip({"defaults.json": b'{"kit": 1}\n', "rules/doc.md": b"# rules\n"})
        self.alone(self.pid, big)
        for data in (board_pdf, bundle):
            self.blob(self.pid, data)
        code = json.dumps({"active": "code", "test_id": "5e5e5e5e-0000-4000-8000-00000000000e"})
        files = {
            "config/config.json": text(json.dumps({"project_id": self.pid}) + "\n"),
            "tests/Ripple/test.json": text(json.dumps({"active": "text", "test_id": TEST_A}) + "\n"),
            "tests/Ripple/procedure.json": text('{"steps": ["measure"]}\n'),
            "tests/Ripple/notes.md": text("# Notes\nkept until the change below\n"),
            "tests/Probe/test.json": text(code + "\n"),
            "tests/Probe/test.py": text("print('probe')\n"),
            "functions/helpers.py": text("def helper():\n    return 1\n"),
            "documents/board.pdf": ref(board_pdf, self.store),
            "documents/scope_capture.bin": ref(big, self.store),
        }
        body = {
            "format_version": 1,
            "project_id": self.pid,
            "name": f"Dataset {self.suffix}",
            "files": files,
            "bundle": {"bundle_id": "kit-dataset", "bundle_version": "1.0",
                       "archive": ref(bundle, self.store)},  # fmt: skip
        }
        self.post("projects", body, [board_pdf, bundle])
        project = f"projects/{self.pid}"
        procedure = files["tests/Ripple/procedure.json"]
        self.release(files)
        edited = text('{"steps": ["measure", "report"]}\n')
        changes = [
            {"path": "tests/Ripple/procedure.json", "replaces": procedure["version"], **edited},
            {"path": "tests/Ripple/notes.md", "replaces": files["tests/Ripple/notes.md"]["version"],
             "delete": True},
        ]  # fmt: skip
        results = self.post(f"{project}/changes", {"changes": changes})["results"]
        if {r["status"] for r in results} != {"saved"}:
            raise DatasetError(f"P9 did not save every change: {results}")
        self.release({**files, "tests/Ripple/procedure.json": edited})

    def release(self, files: dict) -> None:
        named = {
            path: files[path]["version"]
            for path in ("tests/Ripple/test.json", "tests/Ripple/procedure.json")
        }
        body = {"format": "text", "fingerprint": a_hex(), "content": a_hex(), "files": named,
                "note": f"Dataset {self.suffix}", "released_by": "kit"}  # fmt: skip
        self.post(f"projects/{self.pid}/releases/{TEST_A}", body)

    def library(self) -> str:
        """L3: one library item; its path for L2."""
        archive = _bytes(3000, "library")
        item = f"kit-{self.suffix}-dataset"
        body = {
            "format_version": 1,
            "kind": "config",
            "id": item,
            "version": "1.0",
            "content_sha256": hashlib.sha256(archive + b"/content").hexdigest(),
            "content_bytes": len(archive),
            "contains_code": False,
            "manifest": {"id": item, "version": "1.0"},
            "machine_id": MACHINE,
            "archive": ref(archive, self.store),
        }
        self.post("library/config", body, [archive])
        self.blob("library", archive)
        return f"library/config/{item}/1.0"

    def evidence(self) -> tuple[list[str], list[dict]]:
        """E1, E7, E10, E15, E11, E12: runs with files on a board, entries, artifacts, the
        trash. The run ids and the artifacts (each with its generations)."""
        ev, serial = self.ev, f"DS-{self.suffix}"
        big = _bytes(MIB + 512, "run-big")
        self.alone(self.pid, big)
        run_ids = []
        for number in range(3):
            run = ev.run(serial=serial, start=number * 60, ended=number * 60 + 30)
            picture = _bytes(40_000 + number, f"picture{number}")
            run["files"].append(ev.file(f"scope_{number}.png", picture, "attachment", "image/png"))
            if number == 1:
                run["files"].append(ev.file("trace.bin", big, "attachment", None))
            ev.pushed(run)
            run_ids.append(run["run_id"])
            self.blob(self.pid, picture)
            for item in run["files"]:
                self.blob(self.pid, ev.blobs[item["sha256"]])
        note = comment("test", "Probe B re-seated", None)
        verdict = override("test", "PASS", None)
        _answer(ev.annotate(run_ids[0], [note, verdict]), "E7")
        entries = [board_entry("lot", f"L-{self.suffix}", None),
                   board_entry("field", "ACME", None, "Customer")]  # fmt: skip
        ev.entries(serial, "AX", "B", entries)
        report = ev.artifact("report", "report.html", serial, "AX", "B", data=b"<p>v1</p>")
        second = ev.artifact("report", "report.html", serial, "AX", "B", data=b"<p>v2</p>")
        export = ev.artifact("export", "export.csv", "", data=b"a,b\n1,2\n")
        for art in (report, second, export):
            if "store_id" in ev.artifact_pushed(art):  # a report's page may not be kept (§5.10)
                self.blob(self.pid, ev.blobs[art["sha256"]])
        _answer(ev.delete_run(run_ids[2]), "E11")
        _answer(ev.delete_artifact(export), "E12")
        return run_ids, [
            {**ev.named(report), "generations": [1, 2]},
            {**ev.named(export), "generations": [], "deleted": True},
        ]

    # ---- Reading back ----------------------------------------------------------------------

    def read(self, label: str, path: str, query: dict | None = None) -> None:
        self.reads.append({"label": label, "path": path, "query": query or {}})

    def manifest(self, base_url: str, library: str, run_ids: list[str], arts: list[dict]) -> dict:
        p = f"projects/{self.pid}"
        board = {"part_number": "AX", "board_revision": "B"}
        serial = f"DS-{self.suffix}"
        self.read("P2 the project", p)
        self.read("P4 its tests", f"{p}/tests")
        self.read("P8 the releases", f"{p}/releases/{TEST_A}")
        for number in (1, 2):
            self.read(f"P8b release {number}", f"{p}/releases/{TEST_A}/{number}")
        self.read("L2 the library item", library)
        self.read("E2 the runs, deleted included", "runs",
                  {"project_id": self.pid, "deleted": "include", "limit": "500"})  # fmt: skip
        for run_id in run_ids:
            self.read(f"E3 run {run_id}", f"{p}/runs/{run_id}")
        self.read("E4 the boards", f"{p}/boards")
        self.read("E5 the board", f"{p}/boards/{serial}", {**board, "include": "current"})
        self.read("E8 the artifacts, every generation, deleted included", f"{p}/artifacts",
                  {"deleted": "include", "generations": "all", "include": "info"})  # fmt: skip
        for art in arts:
            for generation in art["generations"]:
                query = {"serial": art["serial"], **board, "generation": str(generation)}
                self.read(f"E9 {art['name']} generation {generation}",
                          f"{p}/artifacts/{art['kind']}/{art['name']}", query)  # fmt: skip
        answers = {read["label"]: _read(self.wire, read) for read in self.reads}
        return {
            "format": FORMAT,
            "server": base_url,
            "store": self.store,
            "project_id": self.pid,
            "reads": self.reads,
            "answers": answers,
            "blobs": sorted(set(self.blobs)),
        }


def _read(wire: Wire, read: dict) -> Any:
    """One read: its answer's JSON, the volatile fields left out; every part checked against
    its name (``parse``)."""
    answer = _answer(wire.send("GET", read["path"], query=read["query"] or None), read["label"])
    return _stable(answer)


def _stable(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _stable(v) for k, v in value.items() if k not in VOLATILE}
    if isinstance(value, list):
        return [_stable(v) for v in value]
    return value


def fill(base_url: str, manifest: Path) -> dict:
    """Write the data set to the server at *base_url*, read it back, write the manifest."""
    wire = Wire(base_url)
    try:
        filler = _Filler(wire)
        filler.project()
        library = filler.library()
        run_ids, arts = filler.evidence()
        written = filler.manifest(base_url, library, run_ids, arts)
    finally:
        wire.close()
    manifest.write_text(json.dumps(written, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return written


def verify(base_url: str, manifest: Path) -> list[str]:
    """What differs between the server at *base_url* and the manifest: each read whose answer
    changed, each blob gone or changed. Empty: everything is there and the same."""
    try:
        held = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DatasetError(f"cannot read the manifest {manifest}: {exc}") from exc
    if held.get("format") != FORMAT:
        raise DatasetError(f"{manifest} is no manifest of this kit (format {held.get('format')})")
    problems: list[str] = []
    wire = Wire(base_url)
    try:
        for read in held["reads"]:
            label = read["label"]
            try:
                now = _read(wire, read)
            except (DatasetError, AssertionError, ValueError) as exc:
                problems.append(f"{label}: {exc}")
                continue
            if now != held["answers"][label]:
                problems.append(f"{label}: {_difference(held['answers'][label], now)}")
        for owner, sha, size in held["blobs"]:
            reply = wire.send("GET", f"storages/{held['store']}/blobs/{owner}/{sha}")
            if reply.status != 200:
                problems.append(f"blob {owner}/{sha}: answered {reply.status}")
            elif (len(reply.body), hashlib.sha256(reply.body).hexdigest()) != (size, sha):
                problems.append(f"blob {owner}/{sha}: other bytes ({len(reply.body)} bytes)")
    finally:
        wire.close()
    return problems


def _difference(was: Any, now: Any, where: str = "$") -> str:
    """The first place two answers differ, as ``$.key[index]: was → now``."""
    if isinstance(was, dict) and isinstance(now, dict):
        for key in sorted(set(was) | set(now)):
            if key not in now:
                return f"{where}.{key} is gone"
            if key not in was:
                return f"{where}.{key} is new: {json.dumps(now[key])[:200]}"
            if was[key] != now[key]:
                return _difference(was[key], now[key], f"{where}.{key}")
    if isinstance(was, list) and isinstance(now, list):
        if len(was) != len(now):
            return f"{where} held {len(was)} items, now {len(now)}"
        for index, (one, other) in enumerate(zip(was, now)):
            if one != other:
                return _difference(one, other, f"{where}[{index}]")
    return f"{where}: {json.dumps(was)[:200]} → {json.dumps(now)[:200]}"
