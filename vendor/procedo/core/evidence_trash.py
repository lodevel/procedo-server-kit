"""The evidence trash: what a Delete takes out of the evidence folder, kept 90 days.

::

    <root>/.trash/<utc>__<machine id>__<hex>/deleted.json     the record, written FIRST
                                            /<the path it had> the run folder, or a report
                                                               and its sidecars

A Delete on a run or a report was an ``rmtree`` / ``unlink`` that anyone could do and
nobody could see afterwards (F9, ``docs/plans/company_storage_design.md``). It is a
RENAME now — one atomic move on the store's tree, a whole run history included — into
an entry of its own, beside a record of what it was, why it went (the reason is
required), who deleted it, from which PC and when. Nothing is destroyed by a Delete:
:func:`restore` moves it back, and only an entry older than :data:`RETENTION_DAYS`, or
one somebody removes with *Delete for good…*, is removed (:func:`purge`).

A move the store refuses removes nothing: what was already moved goes back, the entry
is dropped, and the error says the store could not move it — a server that cannot
rename a folder is told, never answered with a delete.

**The place remembers** (``data_format.md`` §6.1, §6.6, §6.7; the owner, 2026-11-05: *"yes
refuse purged ids"*). A deleted run's folder, or a deleted report's, export's or draft's
file, leaves a **tombstone** where it was, ``<its name>.deleted.json`` naming its entry
(:func:`tombstone`), written before the move and removed by a restore. So, with one look
where the item was, a push and an entry find out what became of it: a replay of a deleted
run answers "already" and the run stays deleted; a run a manager **purged** keeps its
tombstone, marked purged, for good, and a push of it is refused (``deleted``) — a purged run
never comes back; an artifact pushed under a deleted name is refused unless it is the very
artifact deleted (a replay). A purged artifact's name is free again. An entry restored keeps
its record, marked restored, until its retention ends: restoring it again answers done and
changes nothing (a replay), and a purge naming it is refused — it is not deleted.

``.trash`` is reserved at the evidence root (``evidence_shared_layout.is_reserved``): it
is never a board, so no listing — boards, History, Last runs, reports, the index — ever
reaches into it. The project sync keeps its own graves in a ``.trash`` too
(``project_sync_trash``, ``<stamp>-<writer>``, no record): where the evidence folder is
the project's folder both live side by side, and an entry without this module's record
is not this module's — never listed, restored or purged.

Every store read and write goes through the store's tree (``tests/test_evidence_tree_seam.py``);
the store methods over it are ``SharedLayout.delete_run`` / ``delete_artifact`` /
``list_trash`` / ``restore_trash`` / ``purge_trash``. Qt-free, stdlib only.
"""

from __future__ import annotations

import calendar
import logging
import secrets
import time
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from pathlib import PurePath
from typing import Any

from . import evidence_index
from .evidence_shared_manifest import POINTER, _manifest, _pointer_of, _read_pointer, advances
from .evidence_shared_manifest import run_info as _run_info
from .evidence_store import DELETED, EvidenceError, OpContext, guard_component
from .evidence_store_fs import os_error
from .evidence_tree import (
    TreeIO,
    forget_listing,
    lock_wait_s,
    read_json,
    safe_relative,
    tree_lock,
)

log = logging.getLogger(__name__)

TRASH = ".trash"
RECORD = "deleted.json"
#: How long a deleted run or report can be restored on a folder store (and on any server
#: that publishes no retention of its own); after that it is removed for good.
RETENTION_DAYS = 90
_DAY_S = 24 * 3600
_STAMP = "%Y-%m-%dT%H:%M:%SZ"
REASON_REQUIRED = "Type a reason: a deletion is kept with its reason."
GONE = "That deletion is no longer in the evidence trash."
#: Beside a deleted item's place: what became of it (:func:`tombstone`).
TOMBSTONE = ".deleted.json"
RESTORED_AT, PURGED_AT = "restored_at", "purged_at"


@dataclass(frozen=True)
class Deletion:
    """Who deletes and why — what the confirmation asked. The store adds this PC's
    machine id and the time."""

    reason: str
    deleted_by: str = ""
    station: str = ""


@dataclass(frozen=True)
class TrashItem:
    """One entry of the trash, as its record describes it."""

    entry: str  # the entry's folder name under ``.trash``
    what: str  # "run" | "report" (or the artifact kind it was)
    board: str  # the board's key (:mod:`evidence_board`); its record's ``serial``
    test: str  # a run's: the name its test had when it ran (a record); "" for a report
    test_id: str  # a run's: its test's hidden id, which it is filed under; "" for a report
    name: str  # the run id, or the report's file name
    reason: str
    deleted_by: str
    machine_id: str
    station: str
    deleted_at: float  # epoch seconds, UTC
    was_current: bool
    files: tuple[str, ...]  # what was moved, relative to the evidence root ("/"-separated)
    place: str  # the run's folder or the artifact's file, relative to the evidence root
    #: How long its store keeps a deletion: this module's on a folder, the server's own on a
    #: server that publishes it (``data_format.md`` §6.7, ``limits.retention_days``).
    retention_days: int = RETENTION_DAYS

    @property
    def expires_at(self) -> float:
        """When it is removed for good."""
        return self.deleted_at + self.retention_days * _DAY_S


def retention_days(store: Any) -> int:
    """How long *store* keeps a deletion: what its server publishes
    (``limits.retention_days``, the API store's ``retention_days``), else
    :data:`RETENTION_DAYS`."""
    return int(getattr(store, "retention_days", RETENTION_DAYS))


def require_reason(deletion: Deletion) -> None:
    """A deletion without a reason is refused before anything is touched."""
    if not deletion.reason.strip():
        raise EvidenceError("invalid", REASON_REQUIRED)


def _trash(store: Any) -> PurePath:
    return store.root / TRASH


def tombstone(place: PurePath) -> PurePath:
    """Where what became of the item deleted from *place* is kept: beside it,
    ``<name>.deleted.json`` — a file, so no run listing (folders with a manifest), report
    listing (``*.html``), draft listing (``*.draft.json``) or sweep (folders) takes it."""
    return place.with_name(place.name + TOMBSTONE)


def tombstone_of(io: TreeIO, place: PurePath) -> dict:
    """The tombstone at *place* (``{}``: none): ``{"entry", "deleted_at"}``, and
    ``purged_at`` once a manager purged a run."""
    return read_json(io, tombstone(place))


def trashed(store: Any, place: PurePath) -> PurePath | None:
    """Where the item deleted from *place* is in the trash now (``None``: it is not)."""
    tomb = tombstone_of(store.io, place)
    if not tomb or tomb.get(PURGED_AT) or not isinstance(tomb.get("entry"), str):
        return None
    entry = _trash(store) / guard_component(tomb["entry"])
    return entry.joinpath(*place.relative_to(store.root).parts)


def refused_deleted(what: str) -> EvidenceError:
    """A push or an entry for an item a manager deleted or purged (``409 deleted``)."""
    return EvidenceError(
        "invalid", f"A manager deleted {what}: it is not stored again.", code=DELETED
    )


def _relative(store: Any, path: PurePath) -> str:
    return "/".join(path.relative_to(store.root).parts)


def _below(root: PurePath, rel: str) -> PurePath:
    """*rel* (a record's ``files`` entry) as a path below *root*: relative, no ``..``, never
    into the trash itself — a hand-edited record names nothing else."""
    parts = safe_relative(rel, "posix")
    if parts[0] == TRASH:
        raise EvidenceError("corrupt", f"{rel!r} is not a place in the evidence folder")
    return root.joinpath(*parts)


def _refused(what: str, exc: OSError | EvidenceError) -> EvidenceError:
    """The store would not move *what*: say so, and that nothing was deleted."""
    err = exc if isinstance(exc, EvidenceError) else os_error(exc)
    if err.kind == "cancelled":
        return err
    return EvidenceError(
        err.kind,
        f"The evidence store could not move {what} into the evidence trash, "
        f"so nothing was deleted: {err}",
    )


def _move_all(io: TreeIO, moves: Sequence[tuple[PurePath, PurePath]]) -> None:
    """Rename each ``(src, dst)``; a failure puts back what already moved, then raises."""
    done: list[tuple[PurePath, PurePath]] = []
    try:
        for src, dst in moves:
            io.rename(src, dst)
            done.append((src, dst))
    except (OSError, EvidenceError):
        for src, dst in reversed(done):
            try:
                io.rename(dst, src)
            except (OSError, EvidenceError):
                log.error("Could not move %s back to %s", dst, src, exc_info=True)
        raise


def _linked(io: TreeIO, root: PurePath, path: PurePath) -> bool:
    """Whether *path* is reached from the evidence root through a link (a transport that can
    have one says): what is under a link belongs to whatever it points at."""
    through = getattr(io, "through_link", None)
    return through is not None and through(root, path)


def _drop_entry(io: TreeIO, root: PurePath, entry: PurePath) -> None:
    """Forget an entry whose payload is gone (moved back, or never moved in): its record
    first — without it the entry is nobody's — then the empty folders, best effort, never
    through a link."""
    try:
        io.unlink(entry / RECORD, missing_ok=True)
        if not _linked(io, root, entry):
            io.rmtree(entry)
    except (OSError, EvidenceError):
        log.warning("Could not remove the emptied trash entry %s", entry, exc_info=True)


def move_in(
    store: Any,
    what: str,
    paths: Sequence[PurePath],
    deletion: Deletion,
    *,
    project_id: str,
    board: str,
    place: PurePath,
    test_id: str = "",
    name: str,
    was_current: bool = False,
    now: float | None = None,
) -> str:
    """Move *paths* (a run folder, or a report and its sidecars, with the folder of its
    earlier generations) into a new entry of the trash, its record written first, then the
    tombstone at *place* (the run's folder, the artifact's file: :func:`tombstone`); the
    entry's name. The search's line for a run or a report follows it
    (:func:`evidence_index.trash_line`).

    The caller has checked the reason (:func:`require_reason`, before anything is
    touched), holds whatever lock the paths need (a run: its test's pointer lock) and has
    proved them to be the ones it decided on. A move the store refuses removes nothing
    (:func:`_refused`), its tombstone included."""
    io = store.io
    at = time.time() if now is None else now
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(at))
    entry = _trash(store) / guard_component(f"{stamp}__{store.machine_id}__{secrets.token_hex(3)}")
    files = [_relative(store, p) for p in paths]
    targets = [entry.joinpath(*p.relative_to(store.root).parts) for p in paths]
    label = f"the run {name}" if what == "run" else f"the {what} {name}"
    deleted_at = time.strftime(_STAMP, time.gmtime(at))
    # A run's line holds the name its test had when it ran, as its manifest recorded it.
    line = evidence_index.trash_line(store, project_id, what, place, deleted=True)
    record = {
        "what": what,
        "original": files[0],
        "files": files,
        "place": _relative(store, place),
        "project_id": project_id,
        "serial": board,
        "test": (line or {}).get("test", "") if what == "run" else "",
        "test_id": test_id,
        "run_id" if what == "run" else "name": name,
        "reason": deletion.reason.strip(),
        "deleted_by": deletion.deleted_by,
        "machine_id": store.machine_id,
        "station": deletion.station,
        "deleted_at": deleted_at,
        "was_current": was_current,
    }
    for parent in dict.fromkeys(t.parent for t in targets):
        store._mkdir_safe(parent)
    io.write_json(entry / RECORD, record, store.tag)
    try:
        io.write_json(tombstone(place), {"entry": entry.name, "deleted_at": deleted_at}, store.tag)
        _move_all(io, list(zip(paths, targets)))
    except (OSError, EvidenceError) as exc:
        _forget_tombstone(io, place, entry.name)
        _drop_entry(io, store.root, entry)
        raise _refused(label, exc) from exc
    log.info("Moved %s into the evidence trash %s (%s)", files[0], entry.name, record["reason"])
    evidence_index.note_records(store, project_id, [line] if line else [])
    return entry.name


def _keep_record_only(io: TreeIO, root: PurePath, entry: PurePath, record: dict, tag: str) -> None:
    """An entry whose payload went back: *record* (marked restored) is all it keeps, the
    emptied folders go — best effort, never through a link."""
    try:
        io.write_json(entry / RECORD, record, tag)
        for name in io.list_names(entry) or []:
            if name != RECORD and not _linked(io, root, entry / name):
                io.rmtree(entry / name)
    except (OSError, EvidenceError):
        log.warning("Could not mark the trash entry %s restored", entry, exc_info=True)


def _forget_tombstone(io: TreeIO, place: PurePath, entry: str) -> None:
    """Remove *place*'s tombstone when it is *entry*'s (a later deletion's stays), best
    effort: a tombstone left beside a live item is never read (the item answers first)."""
    try:
        if tombstone_of(io, place).get("entry") == entry:
            io.unlink(tombstone(place), missing_ok=True)
    except (OSError, EvidenceError):
        log.warning("Could not remove the tombstone of %s", place, exc_info=True)


def _epoch(text: Any) -> float | None:
    try:
        return float(calendar.timegm(time.strptime(str(text), _STAMP)))
    except ValueError:
        return None


def _item(entry: str, record: dict) -> TrashItem | None:
    """The record as an item; ``None`` for what is not one (a project-sync grave, a record
    without its files or its time, an entry restored since — never shown, so never purged by
    a guess)."""
    files, place = record.get("files"), record.get("place")
    at = _epoch(record.get("deleted_at"))
    if not record.get("what") or not isinstance(files, list) or not files or at is None:
        return None
    if not isinstance(place, str) or not place or record.get(RESTORED_AT):
        return None
    return TrashItem(
        entry=entry,
        what=str(record["what"]),
        board=str(record.get("serial", "")),
        test=str(record.get("test", "")),
        test_id=str(record.get("test_id", "")),
        name=str(record.get("run_id") or record.get("name") or ""),
        reason=str(record.get("reason", "")),
        deleted_by=str(record.get("deleted_by", "")),
        machine_id=str(record.get("machine_id", "")),
        station=str(record.get("station", "")),
        deleted_at=at,
        was_current=bool(record.get("was_current")),
        files=tuple(str(f) for f in files),
        place=place,
    )


def _records(store: Any) -> list[tuple[str, dict]]:
    """``(entry, record)`` of every entry folder of the trash. A refused listing raises."""
    io, trash = store.io, _trash(store)
    found = []
    for name in io.list_names(trash) or []:
        entry = trash / name
        if io.is_dir(entry):
            found.append((name, read_json(io, entry / RECORD)))
    return found


def listing(store: Any) -> list[TrashItem]:
    """The trash's entries, newest deletion first. A refused listing raises."""
    items = [i for i in (_item(n, r) for n, r in _records(store)) if i is not None]
    return sorted(items, key=lambda i: (i.deleted_at, i.entry), reverse=True)


def _record(store: Any, entry: str) -> dict:
    """*entry*'s record, read now (``{}``: it went meanwhile)."""
    io, folder = store.io, _trash(store) / guard_component(entry)
    forget_listing(store.io, folder)
    return read_json(io, folder / RECORD) if io.is_dir(folder) else {}


def _put_back(io: TreeIO, moves: Sequence[tuple[PurePath, PurePath]], root: PurePath) -> None:
    """Move each ``(in the trash, where it was)`` back — none when any place is taken again."""
    for src, dst in moves:
        forget_listing(io, src.parent)
        forget_listing(io, dst.parent)
        if not io.exists(src):
            raise EvidenceError("not_found", f"{src.name} is missing from the evidence trash")
        if io.exists(dst):
            where = "/".join(dst.relative_to(root).parts)
            raise EvidenceError(
                "denied", f"Nothing was restored: {where} is in the evidence folder again."
            )
    _move_all(io, moves)


def _repoint(io: TreeIO, test_dir: PurePath, run_dir: PurePath, tag: str) -> bool:
    """A restored run that was Current is Current again when no newer complete run took its
    place — THE pointer rule (:func:`evidence_shared_manifest.advances`). Under the lock."""
    m = _manifest(io, run_dir)
    if m is None or not advances(_read_pointer(io, test_dir), _run_info(m)):
        return False
    io.write_json(test_dir / POINTER, _pointer_of(_run_info(m)), tag)
    return True


def restore(store: Any, entry: str, ctx: OpContext | None = None) -> bool:
    """Put *entry* back where it was; whether it became the Current run again. An entry
    restored already answers ``False`` and changes nothing (a replay, ``data_format.md``
    §6.7); one no longer in the trash is ``not_found``.

    Refused, and nothing moved, when something is at its place again (a run of the same
    id, a report of the same name). A run is moved back under its test's pointer lock,
    the lock its delete held. Its tombstone goes, and its record stays, marked restored."""
    io, root = store.io, store.root
    record = _record(store, entry)
    if record.get(RESTORED_AT):
        return False
    item = _item(entry, record)
    if item is None:
        raise EvidenceError("not_found", GONE)
    folder = _trash(store) / entry
    moves = [(_below(folder, f), _below(root, f)) for f in item.files]
    place = _below(root, item.place)
    current = False
    if item.what == "run":
        run_dir = moves[0][1]
        test_dir = run_dir.parent.parent
        if run_dir.parent.name != "runs" or len(moves) != 1:
            raise EvidenceError("corrupt", f"{item.files[0]!r} is not a run's place")
        store._mkdir_safe(run_dir.parent)
        with tree_lock(
            io,
            test_dir / ".pointer.lock",
            store.tag,
            timeout_s=lock_wait_s(ctx),
            cancel=ctx.cancel if ctx else None,
            ctx=ctx,
        ):
            _put_back(io, moves, root)
            _forget_tombstone(io, place, entry)
            current = item.was_current and _repoint(io, test_dir, run_dir, store.tag)
    else:
        for parent in dict.fromkeys(dst.parent for _, dst in moves):
            store._mkdir_safe(parent)
        _put_back(io, moves, root)
        _forget_tombstone(io, place, entry)
    _keep_record_only(
        io, root, folder, {**record, RESTORED_AT: time.strftime(_STAMP, time.gmtime())}, store.tag
    )
    log.info("Restored %s from the evidence trash %s", item.files[0], entry)
    line = evidence_index.trash_line(store, str(record.get("project_id", "")), item.what, place)
    evidence_index.note_records(store, str(record.get("project_id", "")), [line] if line else [])
    return current


def _remove(store: Any, entry: str, record: dict) -> None:
    """Remove *entry* for good: a run's tombstone says purged first (its id is never stored
    again), an artifact's goes (its name is free again); then the entry."""
    io, folder = store.io, _trash(store) / entry
    if _linked(io, store.root, folder):
        raise EvidenceError("denied", f"{folder} is a link, not a trash entry")
    item = _item(entry, record)
    if item is not None:
        place = _below(store.root, item.place)
        if item.what == "run":
            held = tombstone_of(io, place)
            if held.get("entry") == entry:
                stamp = time.strftime(_STAMP, time.gmtime())
                io.write_json(tombstone(place), {**held, PURGED_AT: stamp}, store.tag)
        else:
            _forget_tombstone(io, place, entry)
    io.rmtree(folder)


def purge(store: Any, entries: Collection[str] = (), *, now: float | None = None) -> list[str]:
    """Remove for good *entries* (Delete for good…) and every entry older than
    :data:`RETENTION_DAYS` (a restored entry's record too); the entries removed. One of
    *entries* no longer in the trash is skipped (a retried request); one that is not deleted
    (restored since) refuses the whole request before anything is removed (``invalid``). One
    of *entries* that cannot be removed raises; an expired one is logged and left for the
    next time."""
    at = time.time() if now is None else now
    held = sorted(  # newest deletion first, as listed
        _records(store),
        key=lambda nr: (_epoch(nr[1].get("deleted_at")) or 0.0, nr[0]),
        reverse=True,
    )
    restored = {n for n, r in held if r.get(RESTORED_AT)}
    if set(entries) & restored:
        names = ", ".join(sorted(set(entries) & restored))
        raise EvidenceError("invalid", f"Nothing was purged: {names} was restored, not deleted.")
    removed = []
    for name, record in held:
        at_deleted = _epoch(record.get("deleted_at"))
        asked = name in entries and _item(name, record) is not None
        expired = at_deleted is not None and at >= at_deleted + RETENTION_DAYS * _DAY_S
        if not asked and not (expired and (name in restored or _item(name, record))):
            continue
        try:
            _remove(store, name, record)
        except (OSError, EvidenceError) as exc:
            if asked:
                raise
            log.warning("Could not remove the expired trash entry %s: %s", name, exc)
            continue
        log.info("Removed for good from the evidence trash: %s", name)
        if name not in restored:
            removed.append(name)
    return removed
