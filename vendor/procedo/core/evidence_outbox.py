"""The durable outbox in front of every shared-layout evidence store.

``%LOCALAPPDATA%/Procedo/outbox/<run_id>/`` holds a staged payload
(with its ``.owner`` lease + routing) and ``outbox/<run_id>.json`` is the entry
that makes the run *safe*: published by temp-write + ``os.replace`` + fsync
after the manifest. Every new run goes this way, whatever the root — a local disk,
a share, a server — and the push is the only writer of new runs and artifacts.
Artifacts (reports, exports, drafts) land the same way: ``outbox/<entry_id>/`` with the payload and its
sidecar, ``outbox/<entry_id>.json`` carrying the routing and the files' sizes and digests. A
comment or verdict override written on a run that is still queued here waits the same way: an
``annotation`` entry holding the annotation entries one save added (:func:`queue_annotation`),
one file per save. A board's lot and fields always wait here (:func:`queue_board_entries`, a
``board`` entry per save), so a bench saves them with the store out of reach, before the
board's first run included.
:func:`flush` first re-publishes orphans (manifest + owner, no entry — a crash
between the two), then pushes entries: runs in ``ended`` order (files, manifest,
pointer), then artifacts in staging order (files; a report or an export sent again keeps the
one it replaces, :mod:`evidence_artifact_generations`), then annotations in the order they were
saved, each added to its run's entries by the same rule as a direct save
(:mod:`annotation_entries`: one that arrives after someone else's is kept, not current), then
each board's entries the same way, the push creating the board's folder when it is new;
afterwards the entry is dropped. A replay redoes the pointer step, finds the same artifact
there already, or finds its entries there already (by id). An entry the store cannot accept
(``invalid`` / ``corrupt``) is moved to ``outbox/quarantine/`` and the flush goes on; a dead
share stops it, and so does a store the project no longer names (``switched``,
checked before every entry): the entries wait for the store it names now.

Where an entry goes is decided by the copy that queued it, entry by entry
(:mod:`evidence_outbox_route`): a store lists what is recorded for it, its flush pushes
each entry it carries that the route sends to it, and the open project's other request
carries the rest; a Move, a Re-point or a store re-opened on a changed entry
re-addresses what the calling copy queued.

Every entry is pushed under this project's push lock on this PC
(:class:`evidence_outbox_lock.PushHold`), taken without waiting: an evidence Move
holds it from its first check until the project's configuration names the new store
and the window has re-opened it, so a flush that meets it stops and the entries wait
for the new store — nothing lands in the store being left, and nothing is quarantined.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import shutil
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path, PurePath
from typing import Protocol

from . import (
    app_identity,
    evidence_artifact_generations,
    evidence_backoff,
    evidence_draft,
    evidence_index,
    evidence_outbox_lock,
    evidence_outbox_route,
    evidence_trash,
    paths,
)
from .annotation_entries import legacy_entries
from .evidence_board import key_of_path
from .evidence_local_staging import OWNER, destination, queued_from
from .evidence_owner import FOREIGN, check_owner, mkdir_owned
from .evidence_shared_layout import board_folder_to_write, check_new_key
from .evidence_shared_manifest import (
    BOARD_ENTRIES,
    MANIFEST,
    _advance_pointer,
    _manifest,
    add_board_entries,
    add_entries,
    run_info,
)
from .evidence_store import (
    ArtifactInfo,
    EvidenceError,
    OpContext,
    OutboxItem,
    check_test_id,
    guard_component,
    is_safe_component,
    safe_component,
)
from .evidence_store_fs import _read_json, _sidecars, default_sidecar, os_error
from .evidence_store_fs_shared import _staged_manifest, _write_file, _write_json_atomic
from .evidence_tree import TreeIO, safe_relative
from .paths import rmtree
from .project_identity import _release_lock
from .test_format import is_test_id

log = logging.getLogger(__name__)


class OutboxStore(Protocol):
    """What the outbox needs from a shared-layout store: its local outbox folder, the
    tree of its root (``io``) and the project it was opened for — together the
    destination an entry is pushed to (:func:`_routed_to`)."""

    io: TreeIO
    outbox: Path
    project_id: str
    on_outbox_entry: Callable[[], None] | None
    _staging: set[str]
    _flushing: threading.Lock
    _flush_again: bool

    @property
    def machine_id(self) -> str: ...

    @property
    def tag(self) -> str: ...

    def _namespace(self, project_id: str) -> PurePath: ...

    def _mkdir_safe(self, target: PurePath) -> None: ...

    def check_binding(self) -> None: ...


QUARANTINE = "quarantine"
#: Why a record no flush can push is set aside, as Waiting uploads says it.
DAMAGED = "This upload is damaged and can't be sent \u2014 it was kept on this PC."
NO_TEST_ID = "This upload names no test id, so it can't be sent \u2014 it was kept on this PC."
ARTIFACT = "artifact"
ANNOTATION = "annotation"
BOARD = "board"  # a board's lot and fields, one save
# A Windows sharing violation (WinError 32) on an entry file is another handle held
# for a moment — the GUI thread listing pending items while a worker moves the entry.
_SHARING_ATTEMPTS = 5
_SHARING_PAUSE_S = 0.02


def _retrying(operation):
    """One outbox file operation; a ``PermissionError`` is retried a few times, tens
    of ms apart, then raised for the caller to classify."""
    for attempt in range(_SHARING_ATTEMPTS):
        try:
            return operation()
        except PermissionError:
            if attempt == _SHARING_ATTEMPTS - 1:
                raise
            time.sleep(_SHARING_PAUSE_S * (attempt + 1))


def default_outbox_dir() -> Path:
    return app_identity.local_data_dir() / "outbox"


def entry_path(store: OutboxStore, entry_id: str) -> Path:
    return entry_path_in(store.outbox, entry_id)


def entry_path_in(outbox: Path, entry_id: str) -> Path:
    return located(outbox, entry_id)[0]


def located(outbox: Path, entry_id: str, *, parked: bool = False) -> tuple[Path, Path]:
    """Where an entry lives: ``(its record, its payload folder)`` in the outbox, or in its
    quarantine once ``parked``."""
    folder = outbox / QUARANTINE if parked else outbox
    return folder / f"{entry_id}.json", folder / entry_id


# ---------------------------------------------------------------------------
# Entry shapes (each one's ``serial`` holds its board's KEY): a run entry carries ``manifest``;
# an artifact entry ``kind: artifact``; an annotation entry ``kind: annotation`` (the run it
# annotates and the entries one save added); a board's save ``kind: board`` (the entries).
# ---------------------------------------------------------------------------


def _strs(e: dict, *keys: str) -> bool:
    return all(isinstance(e.get(k), str) for k in keys)


def _is_artifact(e: dict) -> bool:
    return (
        e.get("kind") == ARTIFACT
        and _strs(e, "id", "serial", "artifact_kind", "name")
        and isinstance(e.get("files"), list)
        and isinstance(e.get("digests"), dict)
    )


def _is_annotation(e: dict) -> bool:
    return (
        e.get("kind") == ANNOTATION
        and _strs(e, "id", "run_id", "serial")
        and is_test_id(e.get("test_id"))
        and isinstance(e.get("seq"), int)
        and (isinstance(e.get("entries"), list) or isinstance(e.get("data"), dict))
    )


def _is_board(e: dict) -> bool:
    return (
        e.get("kind") == BOARD
        and _strs(e, "id", "serial")
        and isinstance(e.get("seq"), int)
        and isinstance(e.get("entries"), list)
    )


def _annotation_entries(e: dict) -> list[dict]:
    """The annotation entries an outbox entry carries. One queued before entries holds the
    run's whole annotation (``data``) instead: each value in it is an entry named after the
    outbox entry, so a replay is the same entries (``annotation_entries.legacy_entries``)."""
    if "entries" in e:
        return e["entries"]
    return legacy_entries(e["data"], e["id"], _text(e, "machine_id"))


def _is_run(e: dict) -> bool:
    m = e.get("manifest")
    return (
        not e.get("kind")
        and isinstance(m, dict)
        and _strs(m, "run_id")
        and _strs(e, "serial")
        and is_test_id(e.get("test_id"))
    )


def _without_test_id(e: dict) -> bool:
    """A run's or a comment's record that names no test id: it would be filed under no test, so
    it is never pushed (never under a name either) but set aside, kept on this PC."""
    return ("manifest" in e or e.get("kind") == ANNOTATION) and not is_test_id(e.get("test_id"))


def _is_entry(e: dict) -> bool:
    """A well-formed entry, one a flush can push and a reader can show."""
    return _is_artifact(e) or _is_annotation(e) or _is_board(e) or _is_run(e)


def _is_record(e: dict) -> bool:
    """Anything shaped like an entry, well-formed or not (a malformed one is parked)."""
    return bool(e.get("kind")) or "manifest" in e


def _entry_id(e: dict) -> str:
    return e["manifest"]["run_id"] if _is_run(e) else e["id"]


def _text(e: dict, key: str) -> str:
    value = e.get(key)
    return value if isinstance(value, str) else ""


def _order(e: dict) -> tuple:
    """Runs first, oldest ``ended`` first; then artifacts in staging order; then
    annotations, after every run they may annotate, each run's in the order saved; then the
    boards' entries, each board's in the order saved. Any record sorts, a malformed one too."""
    seq = e.get("seq") if isinstance(e.get("seq"), int) else 0
    if e.get("kind") == ARTIFACT:
        return (1, _text(e, "at"), _text(e, "id"), 0)
    if e.get("kind") == ANNOTATION:
        return (2, _text(e, "run_id"), "", seq)
    if e.get("kind") == BOARD:
        return (3, _text(e, "serial"), _text(e, "project_id"), seq)
    m = e.get("manifest") if isinstance(e.get("manifest"), dict) else {}
    return (0, _text(m, "ended"), _text(m, "run_id"), 0)


def _item(e: dict, reason: str = "") -> OutboxItem:
    """The record as the window lists it — a parked malformed one too. A run's test is named
    as its manifest recorded it, a comment's as the entry does; each is the test's by its id."""
    kind = e.get("kind")
    board, test, test_id = _text(e, "serial"), _text(e, "test") or None, _text(e, "test_id")
    where = {
        "reason": reason,
        "root": _text(e, "root"),
        "queued_from": _text(e, "project_folder"),
        "project_name": _text(e, "project_name"),
    }
    if kind == ARTIFACT:
        return OutboxItem(
            _text(e, "id"),
            ARTIFACT,
            _text(e, "project_id"),
            board,
            artifact_kind=_text(e, "artifact_kind"),
            name=_text(e, "name"),
            **where,
        )
    if kind == ANNOTATION:
        return OutboxItem(
            _text(e, "id"),
            ANNOTATION,
            _text(e, "project_id"),
            board,
            test=test,
            test_id=test_id,
            **where,
        )
    if kind == BOARD:
        return OutboxItem(_text(e, "id"), BOARD, _text(e, "project_id"), board, **where)
    m = e.get("manifest") if isinstance(e.get("manifest"), dict) else {}
    return OutboxItem(
        _text(m, "run_id"),
        "run",
        _text(e, "project_id"),
        board,
        test=_text(m, "test") or None,
        test_id=test_id,
        **where,
    )


def set_aside_text(count: int) -> str:
    """``2 set aside``: how every window says the store refused that many uploads."""
    return f"{count} set aside"


def waiting_counts(items: list[OutboxItem]) -> tuple[int, int, int]:
    """``(runs, artifacts, comments)`` among outbox items: a queued comment or verdict
    override is said on its own, never counted as a run or an artifact."""
    runs = sum(1 for i in items if i.kind == "run")
    comments = sum(1 for i in items if i.kind == ANNOTATION)
    return runs, len(items) - runs - comments, comments


# ---------------------------------------------------------------------------
# Staging (what makes a write safe) — nothing here touches the root.
# ---------------------------------------------------------------------------


def _manifest_parts(io: TreeIO, m: dict) -> list[tuple[dict, tuple[str, ...]]]:
    """Every file the manifest names, with the validated components the push may join
    below the generation. A manifest that does not describe ``{name, size, sha256}``
    records, or that names a file the push could not place inside the generation, is
    ``corrupt`` — quarantined, never pushed."""
    try:
        files = [(f, safe_relative(f["name"], io.flavour)) for f in m["files"]]
    except (KeyError, TypeError) as exc:
        raise EvidenceError("corrupt", f"unusable manifest file list: {exc}") from exc
    for f, _parts in files:
        if not isinstance(f.get("size"), int) or not isinstance(f.get("sha256"), str):
            raise EvidenceError("corrupt", f"{f.get('name')!r} has no size/sha256 in the manifest")
    return files


def publish(store: OutboxStore, run_dir: Path, *, notify: bool = True) -> None:
    """Write the entry for a staged run dir from its ``.owner`` routing and manifest."""
    owner = _read_json(run_dir / OWNER)
    m = _staged_manifest(run_dir)
    if not m or not owner:
        raise EvidenceError("corrupt", f"{run_dir.name}: no manifest/owner to publish from")
    _manifest_parts(store.io, m)  # a name the push could not join never becomes an entry
    entry = {
        "schema_version": 1,
        "root": owner["root"],
        "store": owner.get("store"),
        "project_folder": owner.get("project_folder", ""),
        "project_id": owner["project_id"],
        "serial": owner["serial"],
        "test_id": owner.get("test_id", ""),  # what it is filed under; its name is in ``m``
        "machine_id": owner["machine_id"],
        "manifest": m,
    }
    _retrying(lambda: _write_json_atomic(entry_path(store, m["run_id"]), entry, store.tag))
    hook = store.on_outbox_entry
    if notify and hook is not None:
        hook()


def stage_artifact(
    store: OutboxStore,
    kind: str,
    project_id: str,
    board: str,
    name: str,
    data: bytes,
    info: ArtifactInfo,
    sidecar: bytes | None = None,
    replaces: str | None = None,
) -> str:
    """``write_artifact`` of the shared layout: payload + sidecar (fsynced) under
    ``outbox/<entry_id>/``, then the entry with the routing. A draft carries the version
    it replaces (:mod:`evidence_draft`). Returns the entry id."""
    payload = safe_component(name)
    if board:
        check_new_key(board)
    if sidecar is None:
        sidecar = default_sidecar(kind, info)
    sidecars = _sidecars(kind, Path(payload))
    at = datetime.now(timezone.utc)  # wall clock, microseconds: staging order is push order
    entry_id = f"{ARTIFACT}_{at.strftime('%Y-%m-%dT%H-%M-%SZ')}_{secrets.token_hex(4)}"
    d = store.outbox / entry_id
    d.mkdir(parents=True)
    _write_file(d / payload, data, store.tag)
    files = [payload]
    if sidecars and sidecar is not None:
        _write_file(d / sidecars[0].name, sidecar, store.tag)
        files.append(sidecars[0].name)
    entry = {
        "schema_version": 1,
        "kind": ARTIFACT,
        "id": entry_id,
        "artifact_kind": kind,
        **destination(store),
        "project_id": project_id,
        "serial": board,
        "name": payload,
        "files": files,
        # What the push compares with what the store holds under that name: the same bytes
        # are the same artifact sent again (:mod:`evidence_artifact_generations`).
        "digests": {
            f: {"size": len(b), "sha256": hashlib.sha256(b).hexdigest()}
            for f, b in zip(files, (data, sidecar))
        },
        "at": at.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "machine_id": store.machine_id,
        **({"replaces": replaces} if kind == "draft" else {}),
        # The listing-index line for this artifact, built HERE from the sidecar the
        # entry carries and appended once the push makes the artifact real. Building
        # it at push time would mean reading back what was just sent, on a transport
        # whose whole cost is round trips; ``None`` for a kind the index does not
        # hold (see :mod:`evidence_index`).
        "index": evidence_index.from_sidecar(
            kind, board, payload, sidecar, evidence_index.sizes_of(data, sidecar), store.machine_id
        ),
    }
    _retrying(lambda: _write_json_atomic(entry_path(store, entry_id), entry, store.tag))
    hook = store.on_outbox_entry
    if hook is not None:
        hook()
    return entry_id


def queue_annotation(
    store: OutboxStore,
    project_id: str,
    board: str,
    test_id: str,
    run_id: str,
    added: list[dict],
    *,
    name: str,
) -> str:
    """Queue the annotation entries one save ``added`` to a run (comments, verdict
    overrides) for their push (:func:`_queue_save`): the run is still queued here, or an
    earlier save of its annotation is. *test_id* is the run's test's hidden id; *name* the name
    the run recorded, which the entry carries for a person to read. Returns the id."""
    check_new_key(board)
    check_test_id(test_id)
    safe_component(run_id)
    fields = {
        "project_id": project_id,
        "serial": board,
        "test": name,
        "test_id": test_id,
        "run_id": run_id,
    }
    return _queue_save(store, ANNOTATION, "run_id", fields, added)


def queue_board_entries(store: OutboxStore, project_id: str, board: str, added: list[dict]) -> str:
    """Queue the board entries one save ``added`` to the board ``board`` names (its lot, its
    fields) for their push (:func:`_queue_save`). The board need not be on the store yet: the
    push creates its folder. Returns the id."""
    check_new_key(board)
    return _queue_save(store, BOARD, "serial", {"project_id": project_id, "serial": board}, added)


def _queue_save(store: OutboxStore, kind: str, of: str, fields: dict, added: list[dict]) -> str:
    """One save's entries as an outbox entry of ``kind`` (``fields``: what it is of, and the
    project): one outbox entry per save, never rewritten — a push removes exactly the file it
    pushed, so a save made while it runs is never lost — and numbered (``seq``) after the
    earlier saves of the same run or board (its ``of`` field), so the store receives them in
    the order saved. Returns its id."""
    same = (fields["project_id"], fields[of])
    earlier = [
        e for _, e in entries(store) if e.get("kind") == kind and (e["project_id"], e[of]) == same
    ]
    seq = 1 + max((e["seq"] for e in earlier), default=0)
    at = datetime.now(timezone.utc)
    entry_id = f"{kind}_{at.strftime('%Y-%m-%dT%H-%M-%SZ')}_{secrets.token_hex(4)}"
    entry = {
        "kind": kind,
        "id": entry_id,
        **destination(store),
        **fields,
        "seq": seq,
        "at": at.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "machine_id": store.machine_id,
        "entries": added,
    }
    try:
        store.outbox.mkdir(parents=True, exist_ok=True)  # a board's lot may be this PC's first
        _retrying(lambda: _write_json_atomic(entry_path(store, entry_id), entry, store.tag))
    except OSError as exc:
        raise os_error(exc) from exc
    hook = store.on_outbox_entry
    if hook is not None:
        hook()
    return entry_id


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


def _records_in(folder: Path) -> list[tuple[Path, dict]]:
    """Every record in ``folder`` (this PC's outbox or its quarantine), whatever its
    destination, in push order."""
    if not folder.is_dir():
        return []
    found = [(p, _read_json(p)) for p in sorted(folder.glob("*.json"))]
    return sorted(((p, e) for p, e in found if _is_record(e)), key=lambda pe: _order(pe[1]))


def _entries_in(store: OutboxStore, folder: Path) -> list[tuple[Path, dict]]:
    """The records in ``folder`` whose destination is this store, in push order."""
    return [(p, e) for p, e in _records_in(folder) if evidence_outbox_route.routed_to(e, store)]


def entries(store: OutboxStore) -> list[tuple[Path, dict]]:
    """This project's well-formed entries, in push order."""
    return [(p, e) for p, e in _entries_in(store, store.outbox) if _is_entry(e)]


def pending(store: OutboxStore, project_id: str) -> list[OutboxItem]:
    return [_item(e) for _, e in entries(store) if e["project_id"] == project_id]


def waiting(
    store: OutboxStore, project_id: str
) -> tuple[
    list[dict], dict[str, list[dict]], dict[str, list[dict]], list[OutboxItem], dict[str, str]
]:
    """What :mod:`outbox_reads` reads this project's outbox as, in ONE pass over it:
    the queued run entries whose payload is on this PC (newest ``ended`` first), the
    annotation entries queued for each run in the order saved (``run_id -> entries``), the
    board entries queued for each board likewise (its key -> entries), every pending
    item, and the name each run waiting here, or commented here, recorded (``run_id -> name``,
    a run's manifest's, else its first queued comment's)."""
    found = [e for _, e in entries(store) if e["project_id"] == project_id]
    runs = [
        e
        for e in found
        if _is_run(e)
        and is_safe_component(e["manifest"]["run_id"])
        and (store.outbox / e["manifest"]["run_id"]).is_dir()
    ]
    notes: dict[str, list[dict]] = {}
    boards: dict[str, list[dict]] = {}
    names = {e["manifest"]["run_id"]: _text(e["manifest"], "test") for e in runs}
    for e in found:  # seq order
        if _is_annotation(e):
            notes.setdefault(e["run_id"], []).extend(_annotation_entries(e))
            names.setdefault(e["run_id"], _text(e, "test"))
        elif _is_board(e):
            boards.setdefault(e["serial"], []).extend(e["entries"])
    return runs[::-1], notes, boards, [_item(e) for e in found], names


def quarantined(store: OutboxStore, project_id: str) -> list[OutboxItem]:
    return [
        _item(e, e.get("reason", ""))
        for _, e in _entries_in(store, store.outbox / QUARANTINE)
        if e.get("project_id") == project_id
    ]


def pending_artifacts(
    store: OutboxStore, kind: str, project_id: str, board: str
) -> dict[str, Path]:
    """``name -> this PC's copy`` of every ``kind`` artifact of ``board`` not yet pushed,
    the newest copy of a name (a writer reads its own write)."""
    found: dict[str, Path] = {}
    for _, e in entries(store):  # push order: a later copy of a name replaces an earlier one
        if _is_artifact(e) and (e["project_id"], e["artifact_kind"], e["serial"]) == (
            project_id,
            kind,
            board,
        ):
            p = store.outbox / e["id"] / e["name"]
            if p.is_file():
                found[e["name"]] = p
    return found


def pending_artifact(
    store: OutboxStore, kind: str, project_id: str, board: str, name: str
) -> Path | None:
    """The newest not-yet-pushed copy of one artifact (:func:`pending_artifacts`)."""
    return pending_artifacts(store, kind, project_id, board).get(name)


def scan_orphans(store: OutboxStore) -> list[str]:
    """Staged run dirs with a manifest and an owner but no entry (crash between
    the two, process gone) are published now; run ids this process still holds
    are left to their own commit. Artifact dirs have no manifest: never orphans."""
    if not store.outbox.is_dir():
        return []
    found = []
    for d in sorted(store.outbox.iterdir()):
        if (
            not d.is_dir()
            or d.name == QUARANTINE
            or d.name in store._staging
            or entry_path(store, d.name).exists()
            or _staged_manifest(d) is None
            or not evidence_outbox_route.carried_by(_read_json(d / OWNER), store)
        ):
            continue
        try:
            publish_or_quarantine(store, d, notify=False)
        except EvidenceError as exc:
            if exc.kind not in ("invalid", "corrupt"):
                raise
            continue  # parked with its reason; the rest of the queue still drains
        found.append(d.name)
    return found


def quarantine(store: OutboxStore, entry: Path, e: dict, reason: str) -> None:
    """Move an entry the store refuses out of the way (payload + entry + reason).
    A move that still fails after the sharing retries is a typed error: the flush
    stops and the entries wait for the next one (a half-moved entry converges then —
    its payload missing, it is quarantined again)."""
    _park(store, _entry_id(e), e, reason, entry)


def quarantine_payload(store: OutboxStore, run_dir: Path, reason: str) -> None:
    """Park a staged payload the store can NEVER accept, before it has an entry.

    A payload whose own manifest names a file outside the generation cannot be
    published, and leaving it where it lay made every later flush retry the same
    refusal and push nothing at all — one poisoned run blocking the whole queue, which
    is the outcome quarantine exists to prevent. The record is rebuilt from the
    payload's ``.owner`` and its manifest, so the parked run still lists with its project and
    test."""
    owner = _read_json(run_dir / OWNER)
    staged = _staged_manifest(run_dir)
    record = {
        "schema_version": 1,
        "root": owner.get("root", store.io.identity),
        "store": owner.get("store"),
        "project_folder": owner.get("project_folder", ""),
        "project_id": owner.get("project_id", ""),
        "serial": owner.get("serial", ""),
        "test_id": owner.get("test_id", ""),
        "machine_id": owner.get("machine_id", ""),
        "manifest": {**(staged if isinstance(staged, dict) else {}), "run_id": run_dir.name},
    }
    _park(store, run_dir.name, record, reason, None)


def publish_or_quarantine(store: OutboxStore, run_dir: Path, *, notify: bool = True) -> None:
    """:func:`publish`, except that a payload the store can never accept is parked
    rather than left in the queue. The caller still sees the refusal — a run whose
    evidence cannot be published fails loudly — but the queue keeps draining."""
    try:
        publish(store, run_dir, notify=notify)
    except EvidenceError as exc:
        if exc.kind not in ("invalid", "corrupt"):
            raise
        quarantine_payload(store, run_dir, str(exc))
        raise


def _park(store: OutboxStore, entry_id: str, e: dict, reason: str, entry: Path | None) -> None:
    """Payload, record and reason into ``outbox/quarantine/``; ``entry`` (when there is
    one) goes last, so a crash mid-way leaves the entry to be quarantined again."""
    evidence_backoff.BACKOFF.release(entry_id)
    src = located(store.outbox, entry_id)[1]
    record, files = located(store.outbox, entry_id, parked=True)
    try:
        record.parent.mkdir(exist_ok=True)
        if src.is_dir():
            _retrying(lambda: shutil.move(str(src), str(files)))
        _retrying(lambda: _write_json_atomic(record, {**e, "reason": reason}, store.tag))
        if entry is not None:
            _retrying(entry.unlink)
    except OSError as exc:
        raise os_error(exc) from exc
    log.error("Upload %s set aside: %s", entry_id, reason)


def _park_under_turn(store: OutboxStore, entry: Path, e: dict, reason: str) -> None:
    """:func:`_park` a record no flush can push, under its project's push turn — the turn a
    Retry of it holds while it puts the record back (:func:`unpark`), so the two never
    interleave and lose the only copy. A turn another holds leaves it for a later flush; the
    record is read again under the turn, and one that is gone or well-formed now is left."""
    project = e.get("project_id")
    if not isinstance(project, str) or not is_safe_component(project):
        project = store.project_id  # no project of its own: parked as this store's
    turn = evidence_outbox_lock._try_lock(store.outbox, project, "push", 0)
    if turn is None:
        return
    try:
        now = _read_json(entry)
        if now and not _is_entry(now):
            _park(store, entry.stem, {**now, "root": e["root"]}, reason, entry)
    finally:
        _release_lock(turn)


def unpark(outbox: Path, entry_id: str, e: dict, tag: str) -> None:
    """A parked entry back in the queue, as ``e`` says (its reason dropped) — under the
    project's push turn. The entry first, then its payload, then the parked record goes: a
    move that fails takes the entry back, and a crash between leaves an entry whose
    payload is still parked, which the next push parks again — never a lost upload."""
    entry, payload = located(outbox, entry_id)
    parked, files = located(outbox, entry_id, parked=True)
    if entry.exists() or payload.exists():
        raise OSError(f"{entry_id} already waits in the outbox")
    _retrying(lambda: _write_json_atomic(entry, {k: v for k, v in e.items() if k != "reason"}, tag))
    try:
        if files.is_dir():
            _retrying(lambda: files.rename(payload))
    except OSError:
        entry.unlink(missing_ok=True)
        raise
    _retrying(parked.unlink)


# ---------------------------------------------------------------------------
# Flush
# ---------------------------------------------------------------------------


def flush(store: OutboxStore, ctx: OpContext) -> list[str]:
    """Push every entry; one flush at a time per store, and a flush that finds
    another one running is not dropped: it leaves a mark and the running flush
    goes round once more. Returns the entry ids this call pushed."""
    pushed: list[str] = []
    store._flush_again = True
    while True:
        if not store._flushing.acquire(blocking=False):
            return pushed  # the running flush will pick our mark up
        try:
            while store._flush_again:
                store._flush_again = False
                pushed += _flush_once(store, ctx)
        finally:
            store._flushing.release()
        if not store._flush_again:  # a mark left between our last round and the release
            return pushed


def _unroutable(store: OutboxStore) -> list[tuple[Path, dict]]:
    """Entries NO project can ever claim: their ``project_id`` is missing or is not a
    string, so :func:`_entries_in` can never select them and no flush can ever push
    them. Invisible and unpushable, they are parked instead, where they are seen."""
    if not store.outbox.is_dir():
        return []
    found = []
    for p in sorted(store.outbox.glob("*.json")):
        e = _read_json(p)
        if _is_entry(e) and not isinstance(e.get("project_id"), str):
            found.append((p, e))
    return found


def _flush_once(store: OutboxStore, ctx: OpContext) -> list[str]:
    turn = evidence_outbox_lock._take_turn(store)
    if turn is not None:  # busy: another window is mid-push, a Move holds the uploads
        try:
            # Under the turn: a push of another window on this PC is never caught between
            # dropping its entry and deleting its payload (:func:`_finish`), which would
            # publish that payload again and then lose it under the new entry.
            scan_orphans(store)
        finally:
            _release_lock(turn)
    for entry, e in _unroutable(store):
        # Under the project the PAYLOAD names, so the parked entry still lists with its
        # reason for the project whose run it actually is.
        owner = _read_json(store.outbox / _entry_id(e) / OWNER)
        quarantine(
            store,
            entry,
            {**e, "project_id": owner.get("project_id", "")},
            "the outbox entry names no project, so no flush can ever push it",
        )
    for entry, e in _records_in(store.outbox):
        # Never pushable, never shown: parked with why, visible there. A run or a comment that
        # names no test id is, whatever project and folder it records (no flush could ever
        # carry it, so none would park it); any other damaged record, when it is this
        # project's for this store — one that names no destination at all as this store's.
        if _is_entry(e):
            continue
        no_id = _without_test_id(e)
        if not no_id and e.get("project_id") != store.project_id:
            continue
        if not isinstance(e.get("root"), str):
            e = {**e, "root": store.io.identity}
        if no_id or evidence_outbox_route.routed_to(e, store):
            _park_under_turn(store, entry, e, NO_TEST_ID if no_id else DAMAGED)
    todo = carried(store)
    pushed = []
    memo: dict = {}
    marked: dict = {}
    for i, (entry, e) in enumerate(todo):
        if ctx.cancel.is_set():
            break
        turn = evidence_outbox_lock._take_turn(store)
        if turn is None:
            break  # a Move holds this project's uploads: the entries wait, nothing failed
        try:
            # Under the turn, as it is now: pushed by another window since the listing,
            # or re-addressed.
            e = _read_json(entry)
            if not _is_entry(e):
                continue
            e = _routed_here(store, entry, e, memo, marked)
            if not e:
                continue
            push(store, entry, e, ctx)
        except EvidenceError as exc:
            if exc.kind not in ("invalid", "corrupt"):
                raise  # unreachable / denied / switched: stop, the entries wait
            quarantine(store, entry, e, str(exc))
            continue
        except OSError as exc:
            raise os_error(exc) from exc
        finally:
            _release_lock(turn)
        pushed.append(_entry_id(e))
        if ctx.progress is not None:
            ctx.progress(i + 1, len(todo))
    return pushed


def carried(store: OutboxStore) -> list[tuple[Path, dict]]:
    """The well-formed entries ``store``'s flush looks at
    (:func:`evidence_outbox_route.carried_by`), in push order."""
    return [
        (p, e)
        for p, e in _records_in(store.outbox)
        if _is_entry(e) and evidence_outbox_route.carried_by(e, store)
    ]


def _routed_here(store: OutboxStore, entry: Path, e: dict, memo: dict, marked: dict) -> dict:
    """The record this pass pushes (``{}``: none) — ``e``, or what it was re-addressed
    to, so a refusal parks the address now on disk. It is pushed when the copy that queued it sends it to this store
    (:func:`evidence_outbox_route.route`, the entry re-addressed when that copy names
    another store now) and this store may receive it (``may_push``). A configuration
    of THIS window's copy that cannot be read stops the flush (``unreachable``), as a
    push meeting it always did; another copy's lets that entry wait. What this copy
    queued for this very store needs no reading: the store is the one its
    configuration names, which the push's binding proves (``check_binding``)."""
    route = evidence_outbox_route
    own_folder = paths.same_place(_text(e, "project_folder"), queued_from(store))
    if own_folder and paths.same_place(e["root"], store.io.identity):
        return route.waiting_for(
            e, e if route.may_push(e, store, marked) else {}, route.MARKER_WAIT
        )
    try:
        to = route.route(e, memo)
    except EvidenceError:
        if own_folder:
            raise
        return route.waiting_for(e, {}, route.ROUTE_WAIT)
    if to is None:
        return route.waiting_for(e, {}, route.ROUTE_WAIT)
    if not paths.same_place(to.root, e["root"]):
        e = route.readdress(entry, e, to, store.tag)
    if not e or not paths.same_place(to.root, store.io.identity):
        evidence_backoff.BACKOFF.release(entry.stem)  # it is another store's now
        return {}
    return route.waiting_for(e, e if route.may_push(e, store, marked) else {}, route.MARKER_WAIT)


def push(store: OutboxStore, entry: Path, e: dict, ctx: OpContext | None = None) -> None:
    """The binding first, then ownership, then the payload.

    A store the project's configuration no longer names (a Move, an Apply, another
    bench re-pointing a shared config) refuses as ``switched`` before anything of the
    entry is sent: the flush stops, the entry waits for the store the project names
    now, and nothing is quarantined — the configuration can switch while a flush
    runs, so it is asked again for every entry, not only when the flush starts.

    A folder another project's marker owns will never accept this entry, whatever the
    network does, so ``foreign`` — and only ``foreign``, never a plain ``denied`` ACL
    that may well clear — is reported as ``invalid``: the entry is quarantined with the
    reason, where the status line counts it and the test's badge says "upload failed",
    instead of stopping the whole queue behind a refusal nothing ever showed the user
    (a background flush has no channel to fail on). The whole push is inside it, not
    just the proof: the marker can be claimed between the two, and ``mark_owner``'s own
    re-read is where that is found out — before any payload byte is sent.

    A store with no tree (``send_entry``: the JSON API's) has no folder and no marker: the
    server files each entry under its project's id, and the store sends it by its call."""
    try:
        store.check_binding()
        send = getattr(store, "send_entry", None)
        if send is not None:  # a store with no tree: the entry goes by its own call
            payload = located(store.outbox, _entry_id(e))[1]
            send(e, payload, ctx)
            _finish(entry, payload)
            return
        check_owner(store, e["project_id"])
        if _is_artifact(e):
            _push_artifact(store, entry, e, ctx)
        elif _is_annotation(e):
            _push_annotation(store, entry, e)
        elif _is_board(e):
            _push_board(store, entry, e)
        else:
            _push_run(store, entry, e, ctx)
    except EvidenceError as exc:
        if exc.kind != FOREIGN:
            raise  # unreachable, denied, corrupt…: the existing rules decide
        raise EvidenceError("invalid", str(exc)) from exc


def _push_run(store: OutboxStore, entry: Path, e: dict, ctx: OpContext | None = None) -> None:
    """Files, then manifest, then pointer, then the entry goes. A replay finds the
    manifest in place and only redoes the pointer step. A run a manager deleted is done and
    stays deleted (a replay, ``already``); one a manager purged is refused (``deleted``): its
    place's tombstone says which (:func:`evidence_trash.tombstone`).

    The manifest that is VERIFIED is the manifest that is PUBLISHED — the payload's
    own ``manifest.json``. The entry's copy routes and orders the push and nothing
    else, so a hand-edited entry can no longer make the store publish a generation
    whose files nothing checked, nor name a file outside it."""
    run_id = e["manifest"]["run_id"]
    src = store.outbox / run_id
    if not src.is_dir():
        raise EvidenceError("corrupt", f"{run_id}: payload missing from the outbox")
    m = _staged_manifest(src)
    if m is None or m.get("run_id") != run_id:
        raise EvidenceError("corrupt", f"{run_id}: the outbox payload carries no matching manifest")
    io = store.io
    files = _manifest_parts(io, m)
    test_dir = _test_dir_of(store, e)
    board = key_of_path(store._namespace(e["project_id"]), test_dir.parent.parent)
    dst = test_dir / "runs" / run_id
    published = _manifest(io, dst) is not None
    tomb = {} if published else evidence_trash.tombstone_of(io, dst)
    if tomb.get(evidence_trash.PURGED_AT):
        raise evidence_trash.refused_deleted(f"the run {run_id}, and purged it")
    if tomb:
        _finish(entry, src)  # deleted since its push: it stays deleted
        return
    if not published:
        mkdir_owned(store, e["project_id"], dst)
        for _f, parts in files:
            io.put_file(src.joinpath(*parts), dst.joinpath(*parts), store.tag, verify=False)
    _verify_pushed(io, dst, files, run_id)  # before the manifest makes the generation visible
    if not published:
        io.put_file(src / MANIFEST, dst / MANIFEST, store.tag, verify=False)
    _advance_pointer(io, test_dir, run_info(m), store.tag, ctx)
    # The run is on the store: its shard search row follows it, under the RESOLVED board, the
    # name it recorded and the id it is filed under (a replay may append the line twice; same
    # key, latest ``at`` wins).
    evidence_index.note_run_published(
        store,
        e["project_id"],
        board,
        m.get("test", ""),
        test_dir.name,
        run_id,
        m.get("verdict", ""),
        m.get("ended", ""),
        m.get("operator", ""),
        ctx,
    )
    _finish(entry, src)


def _finish(entry: Path, payload: Path) -> None:
    """The entry is on the store: the entry goes FIRST, then the payload, best-effort. A
    file of it held open on this PC (a window reading it) cannot fail an entry the store
    already holds; what is left behind is published again by :func:`scan_orphans` while
    it still has its manifest (a replay finds the run on the store) and swept otherwise."""
    _retrying(entry.unlink)
    evidence_backoff.BACKOFF.release(entry.stem)
    rmtree(payload, ignore_errors=True)


def _board_dir_of(store: OutboxStore, e: dict) -> PurePath:
    """The entry's board folder on the store (its ``serial`` is the board's key), in the
    spelling that already exists there. The push is where a board is created, so a NEW
    board no folder may be is refused here (:func:`board_folder_to_write`)."""
    return board_folder_to_write(store.io, store._namespace(e["project_id"]), e["serial"])


def _test_dir_of(store: OutboxStore, e: dict) -> PurePath:
    """``<board>/results/<test_id>`` on the store for a run or annotation entry: filed under its
    test's hidden id, never a name."""
    return _board_dir_of(store, e) / "results" / check_test_id(e.get("test_id"))


def _push_annotation(store: OutboxStore, entry: Path, e: dict) -> None:
    """The save's annotation entries added to its run's (the rule of a direct save: one that
    arrives after someone else's is kept there, not current, never set aside), then the entry
    goes. Its run is pushed first (:func:`_order`); a run that is not on the store —
    quarantined, or deleted since — will never take it, so the entry is ``invalid`` and
    parked with the reason."""
    run = _test_dir_of(store, e) / "runs" / guard_component(e["run_id"])
    if _manifest(store.io, run) is None:
        tomb = evidence_trash.tombstone_of(store.io, run)
        if tomb and not tomb.get(evidence_trash.PURGED_AT):
            raise evidence_trash.refused_deleted(f"the run {e['run_id']}")
        raise EvidenceError(
            "invalid", f"{e['id']}: the run {e['run_id']} it annotates is not on the store"
        )
    if "entries" in e:
        add_entries(store.io, run, e["entries"], store.tag)
    else:  # a whole-file save queued before entries: only what it changed, none replacing
        add_entries(store.io, run, _annotation_entries(e), store.tag, changes_only=True)
    _retrying(entry.unlink)


def _push_board(store: OutboxStore, entry: Path, e: dict) -> None:
    """The save's board entries added to the board's (the rule of a direct save: one that
    arrives after someone else's is kept there, not current, never set aside), the board's
    folder created when it is new; then the entry goes."""
    folder = _board_dir_of(store, e)
    mkdir_owned(store, e["project_id"], folder / BOARD_ENTRIES)
    add_board_entries(store.io, folder, e["entries"], store.tag)
    _retrying(entry.unlink)


def _verify_pushed(
    io: TreeIO, dst: PurePath, files: list[tuple[dict, tuple[str, ...]]], entry_id: str
) -> None:
    """Every pushed file has the manifest's size and sha256 (a mismatch is ``corrupt``:
    the entry is quarantined, nothing points at the generation)."""
    for f, parts in files:
        p = dst.joinpath(*parts)
        if not io.is_file(p) or io.stat(p)[0] != f["size"] or io.sha256(p) != f["sha256"]:
            raise EvidenceError(
                "corrupt", f"{entry_id}: {f['name']} does not match its manifest after the push"
            )


def _artifact_root_of(store: OutboxStore, e: dict) -> PurePath:
    """Where the entry's artifact lands: ``exports/`` beside the boards, or the
    board's ``reports/`` (reports and the board's draft)."""
    ns = store._namespace(e["project_id"])
    kind = e["artifact_kind"]
    if kind == "export":
        return ns / "exports"
    if kind not in ("report", "draft"):
        raise EvidenceError("invalid", f"unknown artifact kind: {kind!r}")
    return _board_dir_of(store, e) / "reports"


def _push_artifact(store: OutboxStore, entry: Path, e: dict, ctx: OpContext | None = None) -> None:
    """Payload and sidecar land atomically in the artifact folder; then the entry goes. A
    report or an export keeps the generation it replaces, and the same bytes again are not
    written (:func:`evidence_artifact_generations.publish`); a draft is replaced only when
    unchanged since it was read (:func:`evidence_draft.publish`). A name a manager deleted
    takes nothing (``deleted``), except the very artifact deleted: a replay of a push made
    before the delete, done."""
    src = store.outbox / e["id"]
    if not src.is_dir():
        raise EvidenceError("corrupt", f"{e['id']}: payload missing from the outbox")
    dst = _artifact_root_of(store, e)
    mkdir_owned(store, e["project_id"], dst)
    for f in e["files"]:
        if len(Path(f).parts) != 1 or f in (".", ".."):
            raise EvidenceError("corrupt", f"entry {e['id']} names {f!r}")
    kind = e["artifact_kind"]
    place = dst / e["files"][0]
    if not store.io.is_file(place) and evidence_trash.tombstone_of(store.io, place):
        copy = evidence_trash.trashed(store, place)
        if copy is None or not evidence_artifact_generations.same_as(
            store.io, copy.parent, kind, e["files"], e["digests"]
        ):
            raise evidence_trash.refused_deleted(f"the {kind} {place.name}")
        _finish(entry, src)  # pushed before the delete: it stays deleted
        return
    if kind in evidence_artifact_generations.KEPT:
        written = evidence_artifact_generations.publish(
            store, kind, dst, src, e["files"], e["digests"], ctx
        )
    else:
        written = evidence_draft.publish(store, dst, src, e["files"], e.get("replaces"), ctx)
    if written and e.get("index"):  # on the store: its index line follows it, as a run's does
        # Under the RESOLVED board folder: the entry carries the spelling the writer
        # used, and only the push knows which folder that actually landed in.
        record = {
            **e["index"],
            "serial": key_of_path(store._namespace(e["project_id"]), dst.parent),
        }
        evidence_index.append_records(store, e["project_id"], [record])
    _finish(entry, src)
