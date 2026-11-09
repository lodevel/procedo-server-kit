"""Where an outbox entry goes — the routing concern of :mod:`evidence_outbox`.

The owner's rule (2026-10-24): an upload goes to the evidence folder of the ORIGINAL
project — the copy that queued it decides, entry by entry (:func:`route`); any
process may carry it there. An entry records its destination
(``evidence_local_staging.destination``): the store it was queued for, the
``config.json`` entry that opens it and the project folder that queued it.

* A store LISTS what is recorded for it (:func:`routed_to`, a string compare — no disk;
  drive letters resolved once per session, so a mapped drive and its share are one place).
* A flush CARRIES the entries recorded for its store or queued from its own folder
  (:func:`carried_by`) and pushes each one :func:`route` sends to it; the rest goes with
  the open project's other request (:func:`flush_elsewhere`), listed while it waits
  (:func:`elsewhere`).
* A Move, a Re-point or a store re-opened on a changed entry re-addresses what the
  CALLING copy queued (:func:`redirect`).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from . import evidence_backoff, evidence_config, evidence_outbox, evidence_outbox_lock, paths
from .evidence_local_staging import OWNER, destination, queued_from
from .evidence_owner import marker_owner
from .evidence_store import (
    EvidenceError,
    OpContext,
    OutboxItem,
    open_entry_store,
    open_project_store,
)
from .evidence_store_fs import _read_json, require_volume
from .evidence_store_fs_shared import _staged_manifest, _write_json_atomic
from .project_identity import _release_lock

log = logging.getLogger(__name__)


def routed_to(e: dict, store: evidence_outbox.OutboxStore) -> bool:
    """The entry is recorded for this store: the store identity it recorded and the
    project it was queued for. What a store LISTS. The project is part of it: two
    projects configured on one folder spelling are refused by the folder's marker, and
    pushing one's run through the other's store would quarantine it as ``foreign``. A
    string compare (:func:`paths.same_place`) — no disk; drive letters resolved once per
    session."""
    return e.get("project_id") == store.project_id and paths.same_place(
        e.get("root"), store.io.identity
    )


def carried_by(e: dict, store: evidence_outbox.OutboxStore) -> bool:
    """The entries ``store``'s flush looks at: recorded for it, or queued from the
    project folder it was opened for (its own copy's, wherever they are addressed)."""
    return e.get("project_id") == store.project_id and (
        paths.same_place(e.get("root"), store.io.identity)
        or paths.same_place(evidence_outbox._text(e, "project_folder"), queued_from(store))
    )


# ---------------------------------------------------------------------------
# The decision, per entry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Route:
    """Where one entry goes now: the store identity, the ``config.json`` entry that
    opens it, and the project folder whose configuration named it (``""``: the copy
    is gone, or none was recorded — the store the entry was queued for)."""

    root: str
    spec: dict | None
    folder: str


_GONE = "gone"
ROUTE_WAIT = "when its project folder answers"  # what an upload waits for when its copy cannot say where it goes
MARKER_WAIT = "when its evidence folder is confirmed as the project's"  # ... when the folder's marker does not name the project


def route(e: dict, memo: dict) -> Route | None:
    """Where the entry goes, decided by the copy that queued it (``None``: it waits).

    1. Its project folder still holds the project and its configuration can be read:
       the store that configuration names NOW.
    2. That folder is confirmed absent on a volume that answers (the copy is gone), or
       none was recorded: the store the entry was queued for — which receives it only
       when its marker names the project for certain (:func:`may_push`).
    3. Otherwise it waits: the folder's volume does not answer (an unmapped drive, an
       unplugged disk, a share that is down), or it holds another project now.

    A configuration that cannot be read raises (``unreachable``): for the copy's own
    window that is its flush failing, as before; any other carrier lets it wait.
    ``memo`` holds one answer per project folder for one pass."""
    folder = evidence_outbox._text(e, "project_folder")
    recorded = Route(e["root"], e.get("store") if isinstance(e.get("store"), dict) else None, "")
    if not folder:
        return recorded
    if folder not in memo:
        memo[folder] = _named_by(Path(folder), e["project_id"])
    found = memo[folder]
    if isinstance(found, EvidenceError):
        raise found
    return recorded if found == _GONE else found


def _named_by(folder: Path, project_id: str) -> Route | str | EvidenceError | None:
    """What the project folder says now (see :func:`route`). Read on a worker, at a
    flush; a listing never asks."""
    try:
        config = evidence_config.load_config(folder)
        if config.get("project_id") == project_id:
            spec = evidence_config.read_spec(folder)
            store = open_project_store(spec, project_root=folder)
            try:
                return Route(store.io.identity, spec, str(folder))
            finally:
                store.close()
        require_volume(folder)  # an unmapped drive, an unplugged disk: unreachable, never gone
        return _GONE if paths.is_absent(folder) else None
    except EvidenceError as exc:
        return exc
    except OSError as exc:
        log.info("%s does not answer: its uploads wait (%s)", folder, exc)
        return None


def may_push(e: dict, store: evidence_outbox.OutboxStore, marked: dict) -> bool:
    """Whether ``store`` may receive ``e``: always through the window of the copy that
    queued it (its usual first-upload adoption of the folder), otherwise only when the
    folder's marker names the project for certain — never adopted, never on a marker
    that could not be read. ``marked`` remembers a proof for one pass. A store with no tree
    (``send_entry``) has no folder to mark: the server files the entry under its project."""
    if getattr(store, "send_entry", None) is not None:
        return True
    if not store.requires_marker and paths.same_place(
        evidence_outbox._text(e, "project_folder"), queued_from(store)
    ):
        return True
    if not marked.get("ours"):
        marked["ours"] = marker_owner(store.io, store.root) == store.project_id
    return marked["ours"]


def waiting_for(e: dict, answer: dict, waits_for: str) -> dict:
    """``answer`` (the record to push, ``{}``: none), noting for the Waiting uploads window
    that an entry given none waits for ``waits_for`` — no push is tried, so no retry applies."""
    if answer:
        evidence_backoff.BACKOFF.release(evidence_outbox._entry_id(e))
    else:
        evidence_backoff.BACKOFF.hold(evidence_outbox._entry_id(e), waits_for)
    return answer


def readdress(path: Path, e: dict, to: Route, tag: str, *, detach: bool = False) -> dict:
    """Rewrite one record for ``to`` — the caller holds the project's push turn, so a
    push never drops it meanwhile; a record already gone is never written back
    (``{}``). The project folder that queued it stays — unless ``detach`` (Send to another
    folder): then it names none, so that copy's configuration never pulls it back and it
    goes where it records, on the folder marker's proof (:func:`route`, :func:`may_push`),
    and keeps the project's name for the window that lists it."""
    now = _read_json(path)
    if not now:
        return {}
    record = {**now, "root": to.root, "store": to.spec}
    if detach:
        record.update(project_folder="", project_name=evidence_outbox._item(now).project_label)
    evidence_outbox._retrying(lambda: _write_json_atomic(path, record, tag))
    return record


# ---------------------------------------------------------------------------
# A store switched under one copy
# ---------------------------------------------------------------------------


def redirect(
    outbox: Path,
    project_id: str,
    old_root: str,
    new: evidence_outbox.OutboxStore,
    *,
    turn_held: bool = False,
) -> None:
    """The calling copy's evidence store switched from the one ``old_root`` names to
    ``new`` — a Move, a Re-point, or the window re-opening its store on a changed
    entry: what THIS copy queued (its project folder, ``new``'s) for the old store
    follows to the new one, a staged run's lease first, so a run committed meanwhile is
    published for the new store, and a parked record too (its payload left as it is),
    so it stays listed. Other copies' and other projects' entries are theirs to decide.

    Under the project's push turn: the Move says it holds it (``turn_held``); any
    other caller takes it, and when a push holds it now leaves everything — the next
    flush routes what this copy queued after its configuration (:func:`route`). A
    record that cannot be rewritten is logged and keeps its destination."""
    to, folder = destination(new), queued_from(new)
    if paths.same_place(to["root"], old_root):
        return
    turn = None if turn_held else evidence_outbox_lock._try_lock(outbox, project_id, "push", 0)
    if not turn_held and turn is None:
        log.info(
            "A push is running: what waits for %s follows its project at the next flush", old_root
        )
        return
    try:
        leases, records = outbox.glob(f"*/{OWNER}"), outbox.glob("*.json")
        quarantined = (outbox / evidence_outbox.QUARANTINE).glob("*.json")
        for path in [*sorted(leases), *sorted(records), *sorted(quarantined)]:
            e = _read_json(path)
            if (
                e.get("project_id") != project_id
                or not paths.same_place(e.get("root"), old_root)
                or not paths.same_place(evidence_outbox._text(e, "project_folder"), folder)
            ):
                continue
            try:
                readdress(path, e, Route(to["root"], to["store"], folder), new.tag)
            except OSError as exc:
                log.warning(
                    "%s still waits for %s: it could not be re-addressed (%s)",
                    path.name,
                    old_root,
                    exc,
                )
    finally:
        if turn is not None:
            _release_lock(turn)


# ---------------------------------------------------------------------------
# What the open project's flush does not carry
# ---------------------------------------------------------------------------


def flush_elsewhere(store: evidence_outbox.OutboxStore, ctx: OpContext) -> None:
    """Send what waits in this PC's outbox that ``store``'s own flush does not carry
    (:func:`carried_by`): each entry — and each orphaned staged run, by its lease — is
    routed by the copy that queued it (:func:`route`), re-addressed when that copy's
    configuration names another store (under the project's push turn), then pushed
    through a store opened for where it goes: bound to that copy's configuration, or
    the recorded one when the copy is gone. Such a store writes only into a folder whose
    marker names the project (``requires_marker``). Never raises: what waits elsewhere
    is no failure of ``store``'s own push."""
    memo: dict = {}
    targets: dict[tuple[str, str], Route] = {}
    for path, e in _waiting(store.outbox):
        if carried_by(e, store):
            continue
        try:
            to = route(e, memo)
        except EvidenceError as exc:
            log.info("Uploads for %s wait: %s", e["root"], exc)
            waiting_for(e, {}, ROUTE_WAIT)
            continue
        if not waiting_for(e, {} if to is None else e, ROUTE_WAIT):
            continue
        if (
            not paths.same_place(to.root, e["root"])
            and _readdress_under_turn(store.outbox, path, e, to, store.tag) is None
        ):
            continue
        targets.setdefault(
            (e["project_id"], paths.place_key(to.root)), to
        )  # one store spelled two ways: once
    for (project_id, _place), to in targets.items():
        root = to.root
        if not evidence_backoff.BACKOFF.due(root):
            continue  # it failed lately: the schedule says when it is tried again
        try:
            if project_id == store.project_id and paths.same_place(root, store.io.identity):
                # re-addressed to this store: its own flush carries them now
                evidence_backoff.BACKOFF.attempt(root, lambda: store.flush_outbox(ctx))
            else:
                evidence_backoff.BACKOFF.attempt(
                    root, lambda project_id=project_id, to=to: _push_through(project_id, to, ctx)
                )
        except (EvidenceError, OSError) as exc:
            log.info("Uploads for %s wait in the outbox: %s", root, exc)


def _readdress_under_turn(outbox: Path, path: Path, e: dict, to: Route, tag: str) -> dict | None:
    """:func:`readdress` under the project's push turn; ``None`` when a push holds it."""
    turn = evidence_outbox_lock._try_lock(outbox, e["project_id"], "push", 0)
    if turn is None:
        return None
    try:
        return readdress(path, e, to, tag) or None
    finally:
        _release_lock(turn)


def _push_through(project_id: str, to: Route, ctx: OpContext) -> list[str]:
    """Open the store ``to`` names and flush it: its own pass routes and pushes each
    entry again, under the push turn (``evidence_outbox._flush_once``)."""
    if to.spec is None:
        log.info("Uploads for %s wait: they record no store entry to open it from", to.root)
        return []
    if to.folder:
        other = open_project_store(to.spec, project_root=Path(to.folder))
        other.bind_to_config()  # the configuration names it this moment
        other.requires_marker = True
    else:
        other = open_entry_store(to.spec, project_id)
    try:
        if not paths.same_place(other.io.identity, to.root):
            log.warning("Uploads for %s wait: their store entry no longer opens it", to.root)
            return []
        return other.flush_outbox(ctx)
    finally:
        other.close()


def _waiting(outbox: Path) -> list[tuple[Path, dict]]:
    """Every well-formed entry, and the lease of every orphaned staged run (a manifest,
    no entry: :func:`evidence_outbox.scan_orphans` publishes it) shaped as a run entry."""
    found = [(p, e) for p, e in evidence_outbox._records_in(outbox) if evidence_outbox._is_entry(e)]
    if outbox.is_dir():
        for lease in sorted(outbox.glob(f"*/{OWNER}")):
            run_dir = lease.parent
            if run_dir.name == evidence_outbox.QUARANTINE or (
                evidence_outbox.entry_path_in(outbox, run_dir.name).exists()
            ):
                continue
            staged = _staged_manifest(run_dir)
            if staged:
                m = {"run_id": run_dir.name, "test": staged.get("test", "")}
                found.append((lease, {**_read_json(lease), "manifest": m}))
    return [
        (p, e)
        for p, e in found
        if isinstance(e.get("project_id"), str) and isinstance(e.get("root"), str)
    ]


def elsewhere(store: evidence_outbox.OutboxStore) -> list[OutboxItem]:
    """What waits in this PC's outbox recorded for any OTHER store than ``store``'s —
    orphaned staged runs included, by their lease — so it is never invisible (owner
    2026-10-24). From the records alone: no store is asked."""
    return [evidence_outbox._item(e) for _, e in _waiting(store.outbox) if not routed_to(e, store)]


def waiting_within(project_id: str, folder: Path) -> int:
    """How many uploads of ``project_id`` wait in this PC's outbox for an evidence
    folder inside ``folder`` — what a move of that folder would strand, since an entry
    goes to the folder it recorded. By spelling, no disk; drive letters resolved once per
    session."""
    records = evidence_outbox._records_in(evidence_outbox.default_outbox_dir())
    roots = (e.get("root") for _, e in records if e.get("project_id") == project_id)
    # Each root in its universal spelling FIRST (a mapped drive is its share), then the
    # full-path test: the one spelling both answers are given about.
    spelled = (paths.universal(r) for r in roots if isinstance(r, str))
    return sum(os.path.isabs(r) and paths.inside_place(r, folder) for r in spelled)
