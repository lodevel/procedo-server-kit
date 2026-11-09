"""Keeping a working copy and a project on a server alike — "like a git with a local copy".

The app never learns to read a project over FTP. It opens a **working copy** (an
ordinary folder under the per-user app data root) and this module keeps that folder
and the server in step: *retrieve on the fly, and when saving just push*. Nothing is
decided by age: a file changed on both sides (or changed on one and deleted on the
other) is a conflict that nothing acts on until somebody answers it (:func:`resolve`).

Four entry points, each one pass of scan → list → :func:`project_sync_plan.plan` →
apply → manifest. Below them: :mod:`project_sync_scan` sees both sides,
:mod:`project_sync_transfer` moves one file, :mod:`project_sync_trash` takes one out,
:mod:`project_sync_apply` carries a plan out, :mod:`project_sync_pass` scopes and ends a
pass, and :mod:`project_sync_conflicts` settles and answers conflicts.

* :func:`checkout` — first contact. Fetches the project's ``config/config.json`` by
  hand to learn its ``project_id`` (the state folder is keyed by it and it lives on
  the server), resolves the working copy through the ``project_local_state`` seam,
  writes the origin record and pulls. A working copy this machine already has for
  another origin is refused; one it has for THIS origin is simply pulled.
* :func:`pull` — the server is listed and the full table applies, in both
  directions. On open and on *Refresh from server*.
* :func:`push` — the server is NOT listed. The pass hands the planner the manifest
  as the listing, so only uploads and server-side deletes can come out of it, and
  each of those looks its own file up on the server before it acts: a file another
  bench changed since this copy last synced is a conflict, not an overwrite.
* :func:`publish` — a project scaffolded locally, pushed to a server folder for the
  first time; it refuses a folder that already holds something that is not this
  project, and names both.

The server is any :class:`evidence_tree.TreeIO`, so the tests drive a ``FsTree`` in
a temp folder as the server and FTPS is nothing but the production tree
(:func:`project_transport.tree_for`). Credentials are never in the record: they are looked up by
endpoint identity and injected as a callable, exactly as ``open_store`` does it.

**Three refusals guard the one thing a sync can do that nothing undoes — delete.**
A plan is built from a scan, and a scan that is WRONG in the direction of "fewer
files" reads as "somebody deleted these":

* a pass refuses unless the working copy is a folder holding ``config/config.json``
  (:func:`_require_project_folder`) — a working copy that was moved, unmounted or
  quarantined used to read as every file deleted, and the deletes' own proof passed,
  because the files really were not there;
* a scan that could not read everything raises (``project_sync_scan.scan_local``,
  ``scan_server``) rather than returning what it managed;
* and **no plan may delete ``config/config.json``** on either side
  (:func:`_refuse_identity_delete`): no legitimate sync removes the project's
  identity, so a plan that wants to is a plan built on a bad scan.

**One pull before any push.** The record carries ``needs_pull``, set when a pull or
a checkout starts and cleared only when one finishes. ``push`` refuses while it is
set, because a push after a failed pull uploads over server changes the pull never
saw — the GUI cannot be relied upon to remember that, so the engine does.

**Nothing leaves the server unless somebody named it** *(owner, 2026-09-21)*. A
pass removes a server file only when its path is in ``approved_deletes``, and what
it removes it MOVES into ``<base>/.trash/<UTC stamp>/`` rather than unlinking. Two
consequences, both deliberate: approving ``{a, b}`` cannot delete ``c`` however the
plan changed between the question and the answer, and a delete that turns out to be
a mistake is a file somebody moves back. Everything else a pass wanted to delete
there comes back in ``SyncReport.pending_deletes`` and stays on the server.

**A pull does not destroy a local file either.** The rule above was first written
with "a delete in the working copy needs no approval, the server's copy is in
``.trash``" — which is only true when THIS APP did the deleting. An administrator's
cleanup or a restored backup takes a file off the server without passing through
anything here, and the working copy may hold its last copy. So a local removal is a
MOVE too, into this PC's own trash
(:func:`project_local_state.local_trash_dir`), named in ``SyncReport.local_trash``.
Nothing purges either trash.

**Cancellation and failures.** An unreachable server, a rejected credential and a
cancel end the pass with the evidence store's typed ``EvidenceError`` kinds. A file
that fails on its own is reported in ``SyncReport.skipped`` and the pass continues.

**Concurrency.** One in-process lock per working copy is held for the whole of every
pass (:func:`_lock_for`), so slice 6's watcher, its pull-on-open and its
push-before-a-run cannot overlap. Two app PROCESSES on one working copy are NOT
covered — the record is a whole-file last-writer-wins document.

Qt-free, stdlib only.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Any

from . import (
    odb_archive,
    project_intents,
    project_local_state,
    project_origin,
    project_sync_apply,
    project_sync_conflicts,
    project_sync_pass,
    project_sync_scan,
    project_sync_transfer,
    project_sync_trash,
    project_transport,
)
from .evidence_store import EvidenceError, OpContext
from .evidence_store_fs import os_error
from .evidence_tree import TreeIO, make_dirs, name_key
from .project_never_travels import is_object
from .project_origin import Origin, Stamp
from .project_sync_conflicts import MINE, THEIRS
from .project_sync_plan import (
    DELETE_LOCAL,
    DELETE_REMOTE,
    DOWNLOAD,
    MAX_LOCAL_PATH,
    UPLOAD,
    Action,
    Plan,
    deferred,
    plan,
)
from .project_sync_report import SyncReport
from .project_sync_scan import scan_server

log = logging.getLogger(__name__)

__all__ = [
    "SyncReport",
    "SyncStatus",
    "MINE",
    "THEIRS",
    "checkout",
    "publish",
    "pull",
    "push",
    "remote_deletes",
    "resolve",
    "restore",
    "scan_local",
    "scan_server",
    "status",
]


@dataclass(frozen=True)
class SyncStatus:
    """What a pass WOULD do. ``asked_server`` is false for the offline count."""

    uploads: int
    downloads: int
    deletes_local: int
    deletes_remote: int
    refused: int
    asked_server: bool
    #: Files changed (or deleted) on both sides that nothing acts on until somebody
    #: answers (``resolve``); with no tree, only those the manifest alone shows.
    conflicts: int = 0
    needs_pull: bool = False
    #: A pass is running on this working copy and the counts are unknown. ``status``
    #: is called from a status-bar timer and must never wait for one to finish.
    busy: bool = False
    #: The PATHS a server-side delete would be asked about, not only how many. The
    #: GUI has to list them — nobody can approve "21 files" — and the approval it
    #: comes back with is exactly this set, path by path.
    remote_delete_paths: tuple[str, ...] = ()
    #: Server-side deletes the app itself asked for (``project_intents``): they go with the
    #: next push, no question, and are not in :attr:`remote_delete_paths`.
    noted_deletes: int = 0


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


def _require_project_folder(folder: Path) -> None:
    """Refuse a pass over a folder that is not (any more) this project's.

    The mirror of ``scan_server``'s absent-base rule, and for the same reason: a
    working copy the user moved, a volume that is not mounted, an antivirus
    quarantine — ``os.walk`` answers "no files" to all three and raises nothing, so
    the plan became DELETE_REMOTE for every file in the project and every one of
    those deletes proved itself, because the local file really was gone.
    """
    if not folder.is_dir():
        raise EvidenceError(
            "not_found",
            f"{folder} is not there, so nothing can be synced from it. Nothing was "
            "deleted on the server.",
        )
    if not folder.joinpath(*project_origin.CONFIG_FILE.split("/")).is_file():
        raise EvidenceError(
            "not_found",
            f"{folder} holds no {project_origin.CONFIG_FILE}, so it is not a project "
            "folder. Nothing was deleted on the server.",
        )


def _require_same_project(folder: Path, origin: Origin) -> None:
    """Refuse a pass over a folder that is not the one this record describes.

    Two questions, and the project id alone answers only the second:

    * **Is this the recorded FOLDER?** One ``project_id`` in several folders is
      ordinary here — an Explorer copy of the project off the share carries it, so
      does a restored backup. Measured: opening such a copy pushed a bench's
      private notes onto the shared server and overwrote a server file, because
      every folder holding the id answered "I am the working copy".
    * **Is it still the same PROJECT?** Somebody restores a backup of ANOTHER
      project into the working copy, or a half-written save leaves
      ``config/config.json`` empty, and every structural check still passes. What
      followed was measured too: project X's config and tests went up over project
      Y and every file of Y was deleted.
    """
    if not project_origin.describes(origin, folder):
        raise EvidenceError(
            "foreign",
            f"{folder} is not this machine's working copy of {origin.project_id}: that "
            f"is {origin.working_copy}. A folder holding the same project is not the "
            "same folder. Nothing was changed on the server.",
        )
    found = project_origin.project_id_in(folder)
    if found != origin.project_id:
        raise EvidenceError(
            "foreign",
            f"{folder} now holds project {found}, and it is the working copy of "
            f"{origin.project_id} at {origin.identity} {origin.base}. Nothing was "
            "changed on the server.",
        )


def _refuse_identity_change(made: Plan, working_copy: Path, origin: Origin) -> None:
    """A plan that would delete OR replace the project's identity is refused WHOLE.

    ``config/config.json`` carries the ``project_id`` everything is keyed by. No
    legitimate sync removes it, so a plan that wants to was built on a scan that
    lied — and the honest answer is to do none of it rather than the part that looks
    harmless. An UPLOAD of it is checked too, and for the same reason: erasing the
    identity by writing an empty file over it destroys exactly as much as deleting
    it, and the file can change between :func:`_require_same_project` and the scan.
    Matched case-insensitively, because the name comes off a server that may spell
    it ``Config/``.
    """
    identity = project_origin.CONFIG_FILE.casefold()
    for action in made.actions:
        if action.path.casefold() != identity:
            continue
        if action.kind in (DELETE_LOCAL, DELETE_REMOTE):
            raise EvidenceError(
                "invalid",
                f"the plan for {working_copy} would {action.kind} "
                f"{project_origin.CONFIG_FILE}, which is the project's identity. "
                "Nothing was synced; check that the working copy and the project "
                "folder on the server are both complete.",
            )
        if action.kind == UPLOAD:
            _require_same_project(working_copy, origin)


def _refuse_blind_push(working_copy: Path, origin: Origin) -> None:
    """A push refuses while the record says a pull is owed: the manifest is then not a
    picture of the server, and every upload would be made against the wrong version."""
    if origin.needs_pull:
        raise EvidenceError(
            "invalid",
            f"{working_copy} owes a pull before anything is pushed: the last pull did "
            "not finish, so the record is not a picture of the server and every upload "
            "would be blind. Refresh from the server, then save again.",
        )


def _carried(local: dict[str, Stamp], origin: Origin) -> dict[str, Stamp]:
    """*local* less what the origin's storage carries as objects (``bundle/``, ``releases/``
    over the JSON API: ``project_sync_api``), which no manifest row ever names."""
    if not project_transport.reads_by_changes(origin.backend):
        return local
    return {rel: stamp for rel, stamp in local.items() if not is_object(rel)}


def _bundle_removed(local: dict[str, Stamp], origin: Origin) -> tuple[str, ...]:
    """``("bundle",)`` when the origin's storage carries the bundle as one object (the JSON
    API) and this copy's ``bundle/`` folder, synced once, holds no file now: a delete waiting
    for its answer, as a file's is (``project_sync_api_steps.bundle_both_ways``)."""
    if not project_transport.reads_by_changes(origin.backend) or not origin.bundle_local:
        return ()
    if any(rel.split("/", 1)[0].casefold() == "bundle" for rel in local):
        return ()
    return ("bundle",)


_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def remote_deletes(made: Plan) -> tuple[str, ...]:
    """The paths a pass would ask about before removing them from the server."""
    return tuple(action.path for action in made.actions if action.kind == DELETE_REMOTE)


def _lock_for(working_copy: Path) -> threading.RLock:
    """The one lock for this working copy, held for the whole of a pass.

    Keyed the way :func:`project_sync_scan.local_path` proves containment — the
    RESOLVED, case-folded path — because one folder has several spellings (an 8.3
    short name, a junction, a drive mapping) and each of them used to get a lock of
    its own, which is no lock at all.

    Re-entrant, because ``checkout`` is a pass that contains a pass. It covers THIS
    process only: two applications open on one working copy would still write the
    record over each other, and nothing here detects that.
    """
    key = os.path.normcase(os.path.realpath(working_copy))
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = _LOCKS[key] = threading.RLock()
    return lock


def _base_of(tree: TreeIO, base: PurePath | None) -> PurePath:
    """The project's folder on the server: the tree's own root unless told otherwise."""
    return tree.root if base is None else base


def _origin_of(working_copy: Path) -> Origin:
    origin = project_origin.load(working_copy)
    if origin is None:
        raise EvidenceError(
            "invalid",
            f"{working_copy} has no origin record, so it is not a working copy of a "
            "project on a server",
        )
    return origin


def _budget(working_copy: Path) -> int:
    """How long a relative path may be below this working copy."""
    return MAX_LOCAL_PATH - len(str(working_copy)) - 1


def scan_local(working_copy: Path, *, refused: list | None = None) -> dict[str, Stamp]:
    """The working copy's files, with the scan's failure typed like every other one.

    A scan that could not read a folder is a FAILED pass, and it reaches the caller
    as the kind of error a refused listing on the server gives — never as a bare
    ``PermissionError`` out of ``os.walk``. This is the one every caller outside
    this package wants; ``project_sync_scan.scan_local`` is the raw walk.
    """
    try:
        return project_sync_scan.scan_local(working_copy, refused=refused)
    except OSError as exc:
        raise os_error(exc) from exc


def _seed(made: Plan) -> SyncReport:
    """A report carrying what the PLAN already refused, before a byte moves. Its
    conflicts are reported once a pass has looked at their bytes
    (``project_sync_conflicts.settle``)."""
    return SyncReport(refused=[(r.path, f"{r.side}: {r.why}") for r in made.refused])


# ---------------------------------------------------------------------------
# The passes
# ---------------------------------------------------------------------------


def pull(
    working_copy: Path,
    tree: TreeIO,
    base: PurePath | None = None,
    *,
    ctx: OpContext | None = None,
    approved_deletes: frozenset[str] = frozenset(),
    defer: bool = False,
    hold: Callable[[], bool] | None = None,
    progress: Callable[[str, Stamp | None], None] | None = None,
) -> SyncReport:
    """One full pass: list the server and apply the whole table, both directions.
    *progress* is told each download as it starts (``project_sync_apply.moves``).

    *approved_deletes* names the paths the user has agreed may leave the server
    (they go to ``.trash``, never to nothing), and every path the app itself removed
    (``project_intents``) counts as agreed. Anything else the plan wants to delete
    there is left alone and reported in ``pending_deletes``.

    *hold* is asked before each download and each removal from the working copy: while
    it answers ``True`` (a test is running from the copy) nothing lands and nothing
    leaves; those paths are reported in ``held``, their rows untouched, so the next pass
    brings them. Uploads go on.

    The downloads of the deferred class (``project_sync_plan.deferred``: the board
    archive, documents, anything large) come last, the largest last; with *defer* they
    are left for a later pass and reported in ``deferred`` — what a first open does so
    the project shows before its board archive has arrived.
    """
    working_copy = Path(working_copy)
    _require_project_folder(working_copy)
    base = _base_of(tree, base)
    ctx = ctx or OpContext()
    writer = project_sync_pass.tag()
    started = time.time()  # a note the app writes from here on is not this pass's to drop
    with _lock_for(working_copy):
        origin = _origin_of(working_copy)
        _require_same_project(working_copy, origin)
        origin.needs_pull = True  # until THIS pull finishes, a push would be blind
        project_origin.save(origin, working_copy)
        try:
            with project_sync_pass.operation(tree, ctx):
                refused: list = []
                local = scan_local(working_copy, refused=refused)
                listed = scan_server(tree, base)
                made = plan(local, listed, origin.files, path_budget=_budget(working_copy))
                _refuse_identity_change(made, working_copy, origin)
                report = _seed(made)
                report.refused.extend((r.path, f"{r.side}: {r.why}") for r in refused)
                project_sync_conflicts.settle(tree, base, working_copy, origin, made, report, ctx)
                project_sync_apply.moves(
                    tree,
                    base,
                    working_copy,
                    origin,
                    made,
                    ctx,
                    writer,
                    report,
                    listed=listed,
                    scanned=local,
                    deferred=_deferred_rule(working_copy),
                    defer=defer,
                    hold=hold,
                    progress=progress,
                )
        except OSError as exc:
            raise os_error(exc) from exc
        project_sync_apply.deletes(
            tree,
            base,
            working_copy,
            origin,
            made,
            ctx,
            report,
            approved_deletes
            | project_intents.approved(working_copy, remote_deletes(made), origin.files),
            writer,
            hold=hold,
        )
        # What this pull met is the whole set: one answered or settled elsewhere is gone.
        origin.conflicts = {conflict.path: conflict for conflict in report.conflicts}
        origin.needs_pull = False
        project_origin.save(origin, working_copy)
        project_intents.settle(working_copy, origin.files, started=started)
        return report


def push(
    working_copy: Path,
    tree: TreeIO,
    base: PurePath | None = None,
    *,
    ctx: OpContext | None = None,
    approved_deletes: frozenset[str] = frozenset(),
) -> SyncReport:
    """One pass that does NOT list the server, and still never sends blind.

    The manifest stands in for the listing, so only uploads and server-side
    deletes can come out of the planner, and each one looks its own file up on the
    server before it acts. An upload whose server copy is no longer the version
    this working copy last synced — another bench saved it, or deleted it — is a
    CONFLICT: nothing is sent, both copies stay as they are, and the path is
    reported in ``conflicts`` (the same bytes on both sides are not a conflict:
    the row is recorded and nothing moves). The deletes were already re-proved
    that way.

    It refuses while the record says a pull is owed, because the manifest is then
    not a picture of the server and every check would be made against the wrong
    version.

    A push with no *approved_deletes* — which is what every automatic push is —
    sends uploads and the deletes the app itself asked for (``project_intents``: a
    Studio *Delete test* or *Rename test*), and nothing else; what else it would have
    deleted comes back in ``pending_deletes`` for somebody to look at.
    """
    working_copy = Path(working_copy)
    _require_project_folder(working_copy)
    with _lock_for(working_copy):
        origin = _origin_of(working_copy)
        _require_same_project(working_copy, origin)
        _refuse_blind_push(working_copy, origin)
        return _push(
            working_copy,
            origin,
            tree,
            _base_of(tree, base),
            ctx or OpContext(),
            approved_deletes=approved_deletes,
        )


def _push(
    working_copy: Path,
    origin: Origin,
    tree: TreeIO,
    base: PurePath,
    ctx: OpContext,
    *,
    approved_deletes: frozenset[str] = frozenset(),
) -> SyncReport:
    """The upload half, for :func:`push` and for the first :func:`publish`."""
    writer = project_sync_pass.tag()
    started = time.time()  # a note the app writes from here on is not this pass's to drop
    refused: list = []
    local = scan_local(working_copy, refused=refused)
    as_listed = {rel: row.server for rel, row in origin.files.items()}
    made = _without_conflicts(
        plan(local, as_listed, origin.files, path_budget=_budget(working_copy)), origin
    )
    _refuse_identity_change(made, working_copy, origin)
    report = _seed(made)
    report.refused.extend((r.path, f"{r.side}: {r.why}") for r in refused)
    report.conflicts.extend(origin.conflicts.values())
    try:
        with project_sync_pass.operation(tree, ctx):
            project_sync_apply.moves(
                tree, base, working_copy, origin, made, ctx, writer, report, listed=as_listed
            )
    except OSError as exc:
        raise os_error(exc) from exc
    finally:
        # What this push met is kept, so the next one sends none of it and asks nothing
        # of the server about it until a pull or an answer settles it.
        origin.conflicts.update((c.path, c) for c in report.conflicts)
        project_origin.save(origin, working_copy)
    project_sync_apply.deletes(
        tree,
        base,
        working_copy,
        origin,
        made,
        ctx,
        report,
        approved_deletes
        | project_intents.approved(working_copy, remote_deletes(made), origin.files),
        writer,
    )
    project_intents.settle(working_copy, origin.files, started=started)
    return report


def _deferred_rule(working_copy: Path) -> Any:
    """``project_sync_plan.deferred`` with this project's board archive (its config names
    it; a first checkout has the config before anything else)."""
    archive = odb_archive.configured(working_copy)
    return lambda path, size: deferred(path, size, archive)


def _without_conflicts(made: Plan, origin: Origin) -> Plan:
    """*made* with nothing planned on a path the record holds in conflict: a push (and the
    offline count) leaves those to the pull that met them and to the answer they wait for,
    instead of looking at the server about them on every tick."""
    held = {name_key(path) for path in origin.conflicts}
    if not held:
        return made
    return Plan(
        tuple(a for a in made.actions if name_key(a.path) not in held),
        made.refused,
        made.conflicts,
    )


def status(
    working_copy: Path,
    tree: TreeIO | None = None,
    base: PurePath | None = None,
    *,
    ctx: OpContext | None = None,
) -> SyncStatus:
    """What a pass would do, without doing any of it.

    With no tree it is the OFFLINE count — what is waiting to go up, from the
    manifest alone, which is what the status bar shows while the server is out of
    reach. With one, it is the whole plan.

    It writes NOTHING, on either side, and reads no file's bytes: a file changed on
    both sides is counted in ``conflicts``, even one a pull would find holds the same
    bytes on both (and settle). It RAISES what a pass would refuse — a working copy that is not
    there, a folder that is no longer this project, a project folder that is not on
    the server, a plan that would delete the project's identity — because those are
    errors to show, not counts to display.

    **It never waits.** A status bar asks on a timer, and a pass can take minutes;
    blocking on the lock froze the caller for the length of a pull. A pass already
    running is answered with ``busy`` and no counts.
    """
    working_copy = Path(working_copy)
    _require_project_folder(working_copy)
    lock = _lock_for(working_copy)
    if not lock.acquire(blocking=False):
        return SyncStatus(0, 0, 0, 0, 0, asked_server=tree is not None, busy=True)
    try:
        origin = _origin_of(working_copy)
        _require_same_project(working_copy, origin)
        scanned = scan_local(working_copy)
        local = _carried(scanned, origin)
        if tree is None:
            made = _without_conflicts(
                plan(
                    local,
                    {rel: row.server for rel, row in origin.files.items()},
                    origin.files,
                    path_budget=_budget(working_copy),
                ),
                origin,
            )
        else:
            base = _base_of(tree, base)
            ctx = ctx or OpContext()
            try:
                with project_sync_pass.operation(tree, ctx):
                    made = plan(
                        local,
                        scan_server(tree, base),
                        origin.files,
                        path_budget=_budget(working_copy),
                    )
            except OSError as exc:
                raise os_error(exc) from exc
        _refuse_identity_change(made, working_copy, origin)
        counts = Counter(action.kind for action in made.actions)
        asked = project_intents.explained(working_copy, remote_deletes(made))
        removed = _bundle_removed(scanned, origin)
        doomed = tuple(path for path in remote_deletes(made) if path not in asked) + removed
        return SyncStatus(
            uploads=counts[UPLOAD],
            downloads=counts[DOWNLOAD],
            deletes_local=counts[DELETE_LOCAL],
            deletes_remote=counts[DELETE_REMOTE] + len(removed),
            noted_deletes=len(asked),
            refused=len(made.refused),
            asked_server=tree is not None,
            conflicts=len(made.conflicts) if tree is not None else len(origin.conflicts),
            needs_pull=origin.needs_pull,
            remote_delete_paths=doomed,
        )
    finally:
        lock.release()


def checkout(
    tree: TreeIO,
    base: PurePath | None = None,
    *,
    backend: str = project_transport.FTPS,
    params: dict[str, Any] | None = None,
    ctx: OpContext | None = None,
    defer: bool = False,
) -> Path:
    """Make this machine's working copy of the project at *base*, and fill it.

    The chicken and the egg: the working copy is named and keyed by ``project_id``,
    which is in the project's ``config/config.json`` — on the server. So that one
    file is fetched first, by hand, and landed and recorded like any other download;
    everything after it is an ordinary pull.

    **One working copy belongs to one origin.** A copied project keeps its id, so
    two servers can hold one ``project_id``; without this refusal the second
    checkout took the first one's folder, reset the manifest and then merged one
    the two projects file by file. A checkout of the SAME origin is not a fresh
    copy either — it is a pull, and it must not land ``config/config.json`` over
    whatever the bench has been editing.

    *backend* and *params* are what rebuilds this tree later (:func:`project_transport.tree_for`);
    they never hold a credential — that is looked up by endpoint identity.

    *defer* leaves the deferred class (the board archive, documents, large files) for a later
    pass, as :func:`pull` does: a first open shows the project before those have arrived.
    """
    base = _base_of(tree, base)
    ctx = ctx or OpContext()
    writer = project_sync_pass.tag()
    try:
        with project_sync_pass.operation(tree, ctx):
            remote = base / project_origin.CONFIG_FILE
            server = project_sync_scan.server_stamp(tree, remote)
            data = tree.read_bytes(remote)
    except OSError as exc:
        raise os_error(exc) from exc
    project_id = project_origin.project_id_of(data)
    working_copy = project_local_state.working_copy_dir(project_id, str(base))
    with _lock_for(working_copy):
        # A folder made just now (nothing in it but this machine's marker) has nothing to protect:
        # whatever record the id still finds (a copy of the same project being removed beside
        # it) does not describe it. A copy that only lost its config is still that copy.
        fresh = all(project_local_state.is_machine_state(p.name) for p in working_copy.iterdir())
        held = None if fresh else project_origin.load(working_copy)
        if held is not None:
            _refuse_other_origin(held, tree, base, working_copy)
            # The same origin in the state folder this project's marker owns: a
            # checkout of it is a pull. Re-stamped, so a working copy that was moved
            # or renamed is adopted here — the one place that is safe, because the
            # marker has just proved whose folder it is.
            project_origin.save(held, working_copy)
        else:
            origin = Origin(
                backend=backend,
                identity=str(tree.identity),
                base=str(base),
                params=dict(params or {}),
                project_id=project_id,
                needs_pull=True,
            )
            project_sync_transfer.land(
                working_copy, project_origin.CONFIG_FILE, data, server, origin, writer
            )
            project_origin.save(origin, working_copy)
            log.info("Checked out %s from %s into %s", project_id, tree.identity, working_copy)
        pull(working_copy, tree, base, ctx=ctx, defer=defer)
    return working_copy


def restore(
    working_copy: Path,
    tree: TreeIO,
    base: PurePath | None = None,
    paths: frozenset[str] | tuple[str, ...] = (),
    *,
    ctx: OpContext | None = None,
) -> SyncReport:
    """Bring NAMED files back from the server into the working copy.

    The other half of the owner's rule. A file that is missing here is either a
    delete somebody meant (approve it, and the server's copy goes to ``.trash``) or
    one nobody meant — a slip in Explorer, a folder dragged aside — and then the
    server still has it and the bench wants it back.

    It downloads exactly the paths given and touches NOTHING else: the plan is
    built from those paths alone rather than from a scan, so it cannot grow between
    the question and the answer, and it plans no delete in either direction. A path
    with no manifest row is not restored — the server never had it as part of this
    project — and is reported.
    """
    working_copy = Path(working_copy)
    _require_project_folder(working_copy)
    base = _base_of(tree, base)
    ctx = ctx or OpContext()
    writer = project_sync_pass.tag()
    with _lock_for(working_copy):
        origin = _origin_of(working_copy)
        _require_same_project(working_copy, origin)
        report = SyncReport()
        wanted = []
        for rel in sorted(set(paths)):
            if rel in origin.files:
                wanted.append(Action(DOWNLOAD, rel, "restored from the server"))
            else:
                report.skipped.append((rel, "the manifest has no row for it to come back from"))
        made = Plan(tuple(wanted))
        try:
            with project_sync_pass.operation(tree, ctx):
                project_sync_apply.moves(
                    tree, base, working_copy, origin, made, ctx, writer, report, listed={}
                )
        except OSError as exc:
            raise os_error(exc) from exc
        project_origin.save(origin, working_copy)
        return report


def resolve(
    working_copy: Path,
    tree: TreeIO,
    paths: Iterable[str],
    keep: str,
    base: PurePath | None = None,
    *,
    ctx: OpContext | None = None,
) -> SyncReport:
    """Answer the recorded conflicts on *paths*: *keep* :data:`MINE` (the working copy's
    file) or :data:`THEIRS` (the server's), the other copy put in its side's trash, never
    destroyed (``project_sync_conflicts.resolve_one``).

    Only a conflict a pass recorded (``Origin.conflicts``) is answered, and only while
    both copies are still the ones it recorded: the answer was given about those bytes.
    A path that is not in conflict, that the sync never carries, or whose copies moved
    since is in ``skipped`` with why, both copies as they are. The done ones are in
    ``uploaded``, ``downloaded``, ``deleted_remote`` or ``deleted_local``."""
    if keep not in (MINE, THEIRS):
        raise ValueError(f"keep must be {MINE!r} or {THEIRS!r}, not {keep!r}")
    working_copy = Path(working_copy)
    _require_project_folder(working_copy)
    base = _base_of(tree, base)
    ctx = ctx or OpContext()
    writer = project_sync_pass.tag()
    grave_of = project_sync_trash.grave_name(writer)
    local_grave = project_local_state.local_trash_dir(working_copy)
    local_grave = None if local_grave is None else local_grave / grave_of
    done_lists = {
        UPLOAD: "uploaded",
        DOWNLOAD: "downloaded",
        DELETE_REMOTE: "deleted_remote",
        DELETE_LOCAL: "deleted_local",
    }
    with _lock_for(working_copy):
        origin = _origin_of(working_copy)
        _require_same_project(working_copy, origin)
        report = SyncReport()
        for rel in sorted(set(paths)):
            project_sync_pass.check_cancel(ctx)
            asked = origin.conflicts.get(rel)
            why = project_sync_conflicts.answerable(rel) or (
                "" if asked is not None else "no pass has found it in conflict"
            )
            done = ""
            if not why:
                try:
                    with project_sync_pass.operation(tree, ctx):
                        why, done = project_sync_conflicts.resolve_one(
                            tree,
                            base,
                            working_copy,
                            origin,
                            asked,  # type: ignore[arg-type]
                            keep,
                            writer,
                            local_grave,
                            grave_of,
                        )
                except (OSError, EvidenceError) as exc:
                    failure = project_sync_pass.typed_failure(exc)
                    if failure.kind in project_sync_pass.FATAL:
                        raise failure from exc
                    why = str(failure)
            if why:
                report.skipped.append((rel, why))
                continue
            origin.conflicts.pop(rel, None)
            project_origin.save(origin, working_copy)
            getattr(report, done_lists[done]).append(rel)
            if done in (DOWNLOAD, DELETE_LOCAL) and asked.local is not None and local_grave:  # type: ignore[union-attr]
                report.local_trash = str(local_grave)
        return report


def _tidy_base(base: str) -> str:
    """A base path as a comparison key: no doubled, trailing or backslash separators.

    ``FtpsEndpoint.identity`` already tidies its own copy of the base, so two
    spellings of one folder give one identity and must give one base too — otherwise
    a ``//projects/acme`` typed in a dialog reads as a second origin and refuses the
    project it IS. Case is left alone: a POSIX server really does tell ``ACME`` from
    ``acme``.
    """
    return "/" + "/".join(part for part in base.replace("\\", "/").split("/") if part)


def _refuse_other_origin(held: Origin, tree: TreeIO, base: PurePath, working_copy: Path) -> None:
    if held.identity == str(tree.identity) and _tidy_base(held.base) == _tidy_base(str(base)):
        return
    raise EvidenceError(
        "foreign",  # it belongs to another origin: an identity refusal, not an ACL
        f"{working_copy} is already this machine's working copy of the same project at "
        f"{held.identity} {held.base}, and {tree.identity} {base} is a different origin. "
        "Two servers holding one project_id cannot share one working copy; give one of "
        "them a project of its own.",
    )


def publish(
    working_copy: Path,
    tree: TreeIO,
    base: PurePath | None = None,
    *,
    backend: str = project_transport.FTPS,
    params: dict[str, Any] | None = None,
    ctx: OpContext | None = None,
) -> SyncReport:
    """Put a project scaffolded locally on the server for the first time.

    **A FIRST publish only.** A working copy that already has an origin record is
    refused, whichever origin it names, because publish is the one pass that writes
    a record from scratch — and every rule the other passes carry lives in the
    record it was replacing. It was a way round both of them: past ``needs_pull``
    (a failed pull, then publish, and another bench's minute-old file gone and the
    flag cleared), and past one-working-copy-one-origin (checkout refused, publish
    re-pointed the copy at the second server and overwrote it). Send changes with
    ``push``, and pull first if one is owed.

    Refuses a server folder that already holds anything that is not this project,
    naming both project ids: publishing into somebody else's folder would merge two
    projects into one by last-one-wins, silently. A folder that already holds THIS
    project is accepted, which is what makes an interrupted first publish
    resumable — it left ``needs_pull`` set, so the next push asks for a pull.
    """
    working_copy = Path(working_copy)
    _require_project_folder(working_copy)
    project_id = project_origin.project_id_in(working_copy)
    base = _base_of(tree, base)
    ctx = ctx or OpContext()
    with _lock_for(working_copy):
        held = project_origin.load(working_copy)
        if held is not None:
            _refuse_other_origin(held, tree, base, working_copy)  # names both origins
            raise EvidenceError(
                "denied",
                f"{working_copy} has already been published to {held.identity} "
                f"{held.base}. Use push to send changes — and pull first if one is "
                "owed; publish is for a project that has never been on a server.",
            )
        try:
            with project_sync_pass.operation(tree, ctx):
                _refuse_foreign(tree, base, project_id)
        except OSError as exc:
            raise os_error(exc) from exc
        origin = Origin(
            backend=backend,
            identity=str(tree.identity),
            base=str(base),
            params=dict(params or {}),
            project_id=project_id,
            needs_pull=True,
        )
        project_origin.save(origin, working_copy)
        log.info("Publishing %s to %s at %s", project_id, tree.identity, base)
        # A first publish has nothing on the server to delete: the folder was proved
        # empty (or this project's) a moment ago, so no approval can be needed.
        report = _push(working_copy, origin, tree, base, ctx)
        origin.needs_pull = False
        project_origin.save(origin, working_copy)
        return report


def _refuse_foreign(tree: TreeIO, base: PurePath, project_id: str) -> None:
    """Let a publish into *base* go ahead, or say whose folder it is."""
    names = tree.list_names(base)
    if names is None:
        if base == tree.root:  # a mistyped base path is reported, never grown
            raise EvidenceError("not_found", f"the base path does not exist on the server: {base}")
        make_dirs(tree, tree.root, base)
        return
    if not [name for name in names if not name.endswith(".part")]:
        return
    try:
        theirs = project_origin.project_id_of(tree.read_bytes(base / project_origin.CONFIG_FILE))
    except (OSError, EvidenceError):
        theirs = ""
    if theirs == project_id:
        return
    raise EvidenceError(
        "foreign",  # somebody else's project folder — the kind that says exactly that
        f"{base} is not empty and does not hold this project: it holds "
        f"{'project_id ' + theirs if theirs else 'files with no project_id'}, and this "
        f"project is {project_id}. Publish to a folder of its own.",
    )
