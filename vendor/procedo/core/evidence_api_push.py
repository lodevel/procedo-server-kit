"""What this PC's outbox holds, sent to the JSON API: each entry by its action's call.

The outbox (:mod:`evidence_outbox`) decides what is pushed, in which order, what is
quarantined and what waits; for a store with no tree it hands each entry to the store
(``send_entry``), which sends it here:

* **a run** (E1, ``pushRun``): the Run (:func:`evidence_api_run.run_object`) and its files as
  parts. Its JSON is built ONCE and its bytes kept beside the staged run (:data:`RUN_JSON`), so
  a replay sends exactly the bytes first sent and the server answers ``already`` (§6.1). A
  file this bench knows the server holds (:class:`Seen`) is left out; a ``409 missing_files``
  puts the ones named back and sends the same JSON again (§3.5).
* **an artifact** (E10, ``pushArtifact``): a report or an export with its file as a part and
  its record (the sidecar) as ``info``; a draft as its ``content``, naming the version it
  replaces, the one the person read (the entry's ``replaces``, :mod:`evidence_draft`, §6.6).
* **a save of a run's annotation** (E7, ``saveAnnotation``) or **of a board's lot and fields**
  (E15, ``saveBoardEntries``): the entries, each answered on its own.

A refusal keeps its kind, so the outbox decides as for any store: ``invalid`` / ``corrupt``
quarantine the entry with the server's sentence, anything else stops the flush and the entries
wait. The two the app names on every store are said as every store says them: a draft changed
since it was read (:func:`evidence_draft.changed`) and an item a manager deleted
(:func:`evidence_trash.refused_deleted`). Qt-free.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from . import atomic_write, evidence_draft, evidence_trash, utc_time
from .evidence_api_cache import RECORD_BYTES
from .evidence_api_http import ApiError, ApiWire, Part, json_bytes, version
from .evidence_api_run import mimetype, run_object
from .evidence_board import board_of
from .evidence_index import info_of_report
from .evidence_store import DELETED, RESULT_FILE, EvidenceError
from .evidence_store_fs import _read_json
from .evidence_store_fs_shared import _staged_manifest
from .evidence_tree import safe_relative

#: The Run a push first built, its bytes kept beside the staged run until the push lands.
RUN_JSON = ".api-run.json"
#: A pointer this bench has not seen yet.
UNKNOWN = object()


def board_query(key: str) -> dict[str, str]:
    """The rest of a board, in the query of a call that names one by its serial (§5.2,
    Addressing a board): both always sent, an empty one meaning none."""
    board = board_of(key)
    return {"part_number": board.part_number, "board_revision": board.revision}


class Seen:
    """What this bench last saw the server hold, for the writes that name it: a test's
    pointer on a board, by the test's hidden id (E6's ``replaces``) and the blobs a push need not send again (§3.5). Filled by
    every answer that tells it, read or write, and kept for the store's life: a hint, never
    trusted further — a stale one is answered ``changed`` or ``missing_files``."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pointers: dict[tuple, dict | None] = {}
        self._blobs: set[tuple[str, str]] = set()

    @staticmethod
    def _slot(project_id: str, key: str, name: str) -> tuple:
        board = board_of(key)
        names = (board.part_number, board.revision, board.serial, name)
        return (project_id, *(n.casefold() for n in names))

    def pointer(self, project_id: str, key: str, test_id: str) -> Any:
        """The pointer of the test whose id is *test_id* as last seen (``None``: none), or
        :data:`UNKNOWN`."""
        with self._lock:
            return self._pointers.get(self._slot(project_id, key, test_id), UNKNOWN)

    def set_pointer(self, project_id: str, key: str, test_id: str, pointer: dict | None) -> None:
        """A pointer as an answer gave it; one naming no run and no time is no pointer."""
        found = None
        if pointer is not None and (pointer.get("run_id") or pointer.get("ended")):
            found = {"run_id": pointer.get("run_id"), "ended": pointer.get("ended")}
        with self._lock:
            self._pointers[self._slot(project_id, key, test_id)] = found

    def held(self, project_id: str, sha256: str) -> bool:
        with self._lock:
            return (project_id, sha256) in self._blobs

    def hold(self, project_id: str, sha256s: Iterable[str]) -> None:
        with self._lock:
            self._blobs.update((project_id, s) for s in sha256s)


def push_run(api: ApiWire, seen: Seen, e: dict, payload: Path, store_id: int) -> dict:
    """E1: the staged run's JSON and parts; the answer (``saved`` or ``already``)."""
    run_id, project = e["manifest"]["run_id"], e["project_id"]
    m = _staged_manifest(payload)
    if m is None or m.get("run_id") != run_id:
        raise EvidenceError("corrupt", f"{run_id}: the outbox payload carries no matching manifest")
    kept = payload / RUN_JSON
    if not kept.is_file():
        record = _read_json(payload / RESULT_FILE) if m.get("complete") else None
        built = run_object(project, e["serial"], m, record, store_id)
        atomic_write.write_bytes(kept, json_bytes(built))
    sent = kept.read_bytes()
    files = {f["sha256"]: f for f in json.loads(sent.decode("utf-8")).get("files") or []}

    def part(sha256: str) -> Part:
        f = files[sha256]
        data = payload.joinpath(*safe_relative(f["name"], "posix")).read_bytes()
        return Part(version(sha256), f["name"], f.get("mimetype") or mimetype(f["name"]), data)

    wanted = [s for s in files if not seen.held(project, s)]
    try:
        answer = _sent_whole(api, "pushRun", ("projects", project, "runs"), sent, wanted, part)
    except ApiError as exc:
        if exc.code != DELETED:
            raise
        raise evidence_trash.refused_deleted(f"the run {run_id}, and purged it") from exc
    seen.hold(project, files)
    if answer.get("current"):
        pointer = {"run_id": run_id, "ended": m.get("ended")}
        seen.set_pointer(project, e["serial"], e["test_id"], pointer)
    return answer


def push_artifact(api: ApiWire, seen: Seen, e: dict, payload: Path, store_id: int) -> dict:
    """E10: a report or an export with its file as a part and its record as ``info``; a draft
    as its content, naming the version the person read. A draft someone else saved since is
    refused (``changed``), and so is a name a manager deleted (``deleted``): kept on this PC,
    said why."""
    kind, name, key, project = e["artifact_kind"], e["name"], e["serial"], e["project_id"]
    data = (payload / name).read_bytes()
    board = board_of(key if kind != "export" else "")
    body: dict[str, Any] = {
        "format_version": 2,
        "kind": kind,
        "serial": board.serial,
        "part_number": board.part_number or None,
        "board_revision": board.revision or None,
        "name": name,
        "machine_id": e.get("machine_id"),
        "updated_at": f"{str(e.get('at') or '')[:19]}Z",
    }
    sha256 = hashlib.sha256(data).hexdigest()
    if kind == "draft":
        content = _json(data, f"the draft {name}")
        body.update(content=content, sha256=evidence_draft.version_of_content(content))
    else:
        body.update(sha256=sha256, size=len(data), store_id=store_id)
        sidecar = e["files"][1] if len(e["files"]) > 1 else None
        if sidecar is not None:
            body["info"] = _json((payload / sidecar).read_bytes(), f"the record of {name}")
            if kind == "report":
                body.update(_report_fields(body["info"]))
    body = {k: v for k, v in body.items() if v is not None}
    if kind == "draft":
        body["replaces"] = e.get("replaces")

    def part(found: str) -> Part:
        if found != sha256 or kind == "draft":
            raise KeyError(found)
        return Part(version(sha256), name, mimetype(name), data)

    wanted = [] if kind == "draft" or seen.held(project, sha256) else [sha256]
    path = ("projects", project, "artifacts")
    try:
        answer = _sent_whole(api, "pushArtifact", path, json_bytes(body), wanted, part)
    except ApiError as exc:
        if exc.code != DELETED:
            raise
        raise evidence_trash.refused_deleted(f"the {kind} {name}") from exc
    if kind != "draft":
        seen.hold(project, [sha256])
    elif answer.get("status") == "changed":
        held = answer.get("artifact") if isinstance(answer.get("artifact"), dict) else {}
        raise evidence_draft.changed(name, RECORD_BYTES["draft"](held.get("content") or {}))
    return answer


def save_annotation(api: ApiWire, e: dict, entries: list[dict]) -> list[dict]:
    """E7: one save's entries added to its run's; a run the server does not hold (never
    pushed, quarantined, purged) will never take them, so the save is ``invalid``."""
    path = ("projects", e["project_id"], "runs", e["run_id"], "annotation")
    try:
        answer = api.call("saveAnnotation", "POST", path, body={"entries": entries})
    except ApiError as exc:
        if exc.kind != "not_found":
            raise
        raise EvidenceError(
            "invalid", f"{e['id']}: the run {e['run_id']} it annotates is not on the server"
        ) from exc
    return list(answer.data.get("results") or [])


def save_board_entries(api: ApiWire, project_id: str, key: str, entries: list[dict]) -> list[dict]:
    """E15: entries added to a board's lot and fields; a board the server does not know yet is
    created by them."""
    path = ("projects", project_id, "boards", board_of(key).serial, "entries")
    body = {"entries": entries}
    answer = api.call("saveBoardEntries", "POST", path, query=board_query(key), body=body)
    return list(answer.data.get("results") or [])


def _sent_whole(
    api: ApiWire,
    op: str,
    path: tuple[str, ...],
    body: bytes,
    wanted: list[str],
    part: Callable[[str], Part],
) -> dict:
    """The write with the parts ``wanted``; a ``409 missing_files`` puts the ones it names
    back and sends the same JSON once more (§3.5). One this push does not hold is ``corrupt``:
    the JSON names a file the outbox does not have."""
    sending = [part(s) for s in wanted]
    try:
        return api.call(op, "POST", path, body=body, parts=sending or None).data
    except ApiError as exc:
        missing = exc.details.get("missing") if exc.code == "missing_files" else None
        if not isinstance(missing, list):
            raise
        named = {p.version for p in sending}
        try:
            sending += [part(str(v).removeprefix("sha256:")) for v in missing if v not in named]
        except (KeyError, OSError) as lost:
            raise EvidenceError(
                "corrupt", f"the server lacks a file this push does not hold ({op})"
            ) from lost
    return api.call(op, "POST", path, body=body, parts=sending or None).data


def _json(data: bytes, what: str) -> dict:
    """A record or a draft's content: the API keeps it as a JSON object (§4.10)."""
    try:
        found = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        found = None
    if not isinstance(found, dict):
        raise EvidenceError(
            "invalid", f"{what} is not a JSON object, which is all the server keeps"
        )
    return found


def _report_fields(info: dict) -> dict[str, Any]:
    """What a report's record says that the server indexes (§4.10): its verdict, operator,
    tests and when it was made (``generated``, which E8 orders and filters by), in UTC as the
    record writes it (:mod:`utc_time`); a record giving its time another way gives none."""
    row = info_of_report("", info)
    meta = info.get("metadata") if isinstance(info.get("metadata"), dict) else {}
    generated = meta.get("generated")
    found: dict[str, Any] = {
        "verdict": row.overall_verdict or None,
        "operator": row.operator or None,
        "tests": list(row.tests) or None,
        "generated": generated if utc_time.is_recorded(generated) else None,
    }
    return {k: v for k, v in found.items() if isinstance(v, (str, list))}
