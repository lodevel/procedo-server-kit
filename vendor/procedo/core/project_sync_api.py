"""Keeping a working copy and a project on a JSON API server alike: the passes of
:mod:`project_sync` over a :class:`project_store.ProjectSource` (``web_api_projects.md``).

The same working copy, the same record (:mod:`project_origin`), the same planner
(:func:`project_sync_plan.plan`) and the same guards as the sync over a folder or FTPS: only what
is asked of the server differs.

* **What the server holds** is read by what changed since the last read (``P3``, from the token
  the record keeps), and whole when there is no token or the server cannot answer from it
  (``gone``): every file with its version and size, a text document with its text. The record
  keeps every file the server held (:attr:`Origin.listing`), so a file left for a later pass
  (deferred, held, failed) is still known when the next read brings only what changed.
* **A download** is a file of that read: its text, or its bytes fetched (a part of the read, or
  ``B2``). One the server names ``missing`` is not asked for: it is reported, and the next pass
  tries again.
* **The uploads and the deletes go in ONE request** (``P9``), each naming the version it
  replaces. One another bench changed since comes back ``changed`` with what the server holds
  now, and nothing of it is written: a conflict, settled by its bytes when they are the same.
  The server refuses, so there is no window between a look and a write. Every test whose
  documents the request changes goes with its draft (:func:`drafts`).
* **``bundle/`` and ``releases/`` are objects, never files** (``data_format.md`` §4.2): the
  bundle is sent whole when it changed here and written whole when it changed there (a change on
  both sides is a conflict on ``bundle``); the releases are written from the records each read
  brings (:func:`project_tree_releases.write`), under each test's hidden id. Neither is in the
  manifest.
* **A test is renamed in one move** (:func:`rename_test`): its draft follows it on the server,
  and the working copy's folder and rows follow it here. Its ``test.json`` moves with it, so
  its releases, runs and current-run marks stay where they are, under its id.

Each step (the read, a download, the one request and its outcomes, the bundle, the releases) is
:mod:`project_sync_api_steps`'s; this module holds the passes and the answers to conflicts.

What the sync over a tree guards, this does too: no plan deletes or empties
``config/config.json``, a delete on the server only for an approved path (the server keeps it in
its trash), a delete here into this PC's trash, nothing lands while a test runs from the copy
(*hold*), and a pull before any push.

Every call may wait on the server: on a worker, never on the GUI thread. Qt-free.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable, Mapping
from contextlib import suppress
from pathlib import Path, PurePosixPath
from typing import Any

from . import (
    project_intents,
    project_local_state,
    project_origin,
    project_sync,
    project_sync_pass,
    server_offer,
    test_release,
)
from . import project_sync_api_steps as steps
from .evidence_store import EvidenceError, OpContext
from .evidence_tree import name_key
from .project_origin import CONFIG_FILE, Conflict, Origin, Row, Stamp
from .project_store import (
    CHANGED,
    RELEASES_DIR,
    SAVED,
    TESTS_DIR,
    BundleWrite,
    Delete,
    ProjectSource,
    Write,
    pack,
    version_of,
)
from .project_sync_conflicts import MINE, THEIRS, answerable
from .project_sync_pass import FATAL, check_cancel, typed_failure
from .project_sync_plan import (
    DELETE_LOCAL,
    DELETE_REMOTE,
    DOWNLOAD,
    UPLOAD,
    plan,
)
from .project_sync_report import SyncReport
from .project_sync_scan import local_now, local_path
from .project_sync_transfer import part_of, record
from .project_sync_trash import move_aside

log = logging.getLogger(__name__)

__all__ = [
    "checkout",
    "compare",
    "publish",
    "pull",
    "push",
    "remote_test",
    "Stranded",
    "rename_test",
    "where_it_is",
    "resolve",
    "restore",
]


# ---------------------------------------------------------------------------
# The passes
# ---------------------------------------------------------------------------


def pull(
    working_copy: Path,
    source: ProjectSource,
    *,
    ctx: OpContext | None = None,
    approved_deletes: frozenset[str] = frozenset(),
    defer: bool = False,
    hold: Callable[[], bool] | None = None,
    progress: Callable[[str, Stamp | None], None] | None = None,
) -> SyncReport:
    """One full pass: read what the server holds and apply the whole table, both directions
    (:func:`project_sync.pull`'s rules and arguments)."""
    working_copy = Path(working_copy)
    project_sync._require_project_folder(working_copy)
    with project_sync._lock_for(working_copy):
        origin = project_sync._origin_of(working_copy)
        project_sync._require_same_project(working_copy, origin)
        return _pull(
            working_copy,
            source,
            origin,
            ctx or OpContext(),
            approved_deletes,
            defer,
            hold,
            progress,
        )


def _pull(
    working_copy: Path,
    source: ProjectSource,
    origin: Origin,
    ctx: OpContext,
    approved_deletes: frozenset[str],
    defer: bool,
    hold: Callable[[], bool] | None,
    progress: Callable[[str, Stamp | None], None] | None,
    read: steps.Read | None = None,
) -> SyncReport:
    writer = project_sync_pass.tag()
    started = time.time()  # a note the app writes from here on is not this pass's to drop
    origin.needs_pull = True  # until THIS pull finishes, a push would be blind
    project_origin.save(origin, working_copy)
    refused: list = []
    local, bundle_here = steps.scan(working_copy, refused)
    budget = project_sync._budget(working_copy)
    read = read or steps.read(source, origin, ctx)
    made = plan(local, read.listing, origin.files, path_budget=budget)
    if steps.needs_whole(read, made):
        read = steps.whole(source, origin, ctx)
        made = plan(local, read.listing, origin.files, path_budget=budget)
    project_sync._refuse_identity_change(made, working_copy, origin)
    report = project_sync._seed(made)
    report.refused.extend((r.path, f"{r.side}: {r.why}") for r in refused)
    steps.settle(working_copy, origin, made, report)
    steps.downloads(
        source,
        working_copy,
        origin,
        made,
        read,
        local,
        ctx,
        report,
        defer=defer,
        hold=hold,
        progress=progress,
    )
    approved = approved_deletes | project_intents.approved(
        working_copy, project_sync.remote_deletes(made), origin.files
    )
    sent = steps.changes_of(working_copy, origin, made, approved, report)
    bundle = steps.bundle_both_ways(
        working_copy, origin, read, bundle_here, report, writer, hold, approved
    )
    steps.send(
        source, working_copy, origin, sent + ([bundle] if bundle else []), read.listing, ctx, report
    )
    steps.local_deletes(working_copy, origin, made, ctx, report, writer, hold)
    failed = steps.releases(working_copy, read, report, hold)
    # The bundle and the releases come in the read that saw them change, and only in it: one
    # this pass did not apply keeps the token where it was, so the next read brings it again.
    # Releases that fail again on that next read do not hold it any more (a test name this PC's
    # file system refuses would hold it for good): they are said at every pass instead, until
    # a read brings them again and they are written.
    owed = failed is None or bool(set(failed) - set(origin.releases_failed))
    if failed is not None:
        kept = {t: why for t, why in origin.releases_failed.items() if t not in read.releases}
        origin.releases_failed = {**kept, **failed}
    report.skipped.extend(
        (f"{RELEASES_DIR}/{test}", why) for test, why in sorted(origin.releases_failed.items())
    )
    if not owed and not steps.bundle_owed(read, origin, report):
        origin.since = read.token
    origin.listing, origin.stores = read.listing, steps.kept_stores(read)
    # What this pull met is the whole set: one answered or settled elsewhere is gone.
    origin.conflicts = {conflict.path: conflict for conflict in report.conflicts}
    origin.needs_pull = False
    project_origin.save(origin, working_copy)
    project_intents.settle(working_copy, origin.files, started=started)
    return report


def push(
    working_copy: Path,
    source: ProjectSource,
    *,
    ctx: OpContext | None = None,
    approved_deletes: frozenset[str] = frozenset(),
) -> SyncReport:
    """What changed here, sent without reading the server (:func:`project_sync.push`'s rules):
    the manifest stands in for the listing, and the server answers ``changed`` for a file
    another bench saved since, so nothing is sent blind."""
    working_copy = Path(working_copy)
    project_sync._require_project_folder(working_copy)
    ctx = ctx or OpContext()
    with project_sync._lock_for(working_copy):
        origin = project_sync._origin_of(working_copy)
        project_sync._require_same_project(working_copy, origin)
        project_sync._refuse_blind_push(working_copy, origin)
        started = time.time()
        refused: list = []
        local, bundle_here = steps.scan(working_copy, refused)
        as_listed = {rel: row.server for rel, row in origin.files.items()}
        made = project_sync._without_conflicts(
            plan(local, as_listed, origin.files, path_budget=project_sync._budget(working_copy)),
            origin,
        )
        project_sync._refuse_identity_change(made, working_copy, origin)
        report = project_sync._seed(made)
        report.refused.extend((r.path, f"{r.side}: {r.why}") for r in refused)
        report.conflicts.extend(origin.conflicts.values())
        approved = approved_deletes | project_intents.approved(
            working_copy, project_sync.remote_deletes(made), origin.files
        )
        sent = steps.changes_of(working_copy, origin, made, approved, report)
        bundle = steps.bundle_both_ways(
            working_copy, origin, None, bundle_here, report, "", None, approved
        )
        try:
            steps.send(
                source,
                working_copy,
                origin,
                sent + ([bundle] if bundle else []),
                origin.listing,
                ctx,
                report,
            )
        finally:
            origin.conflicts.update((c.path, c) for c in report.conflicts)
            project_origin.save(origin, working_copy)
        project_intents.settle(working_copy, origin.files, started=started)
        return report


def _named(source: ProjectSource, base: str, ctx: OpContext) -> str:
    """The id of the project the server lists under the name *base* ends with."""
    name = PurePosixPath(base).name
    for project in source.list_projects(ctx):
        if project.name.casefold() == name.casefold():
            return project.project_id
    raise EvidenceError("not_found", f"The server holds no project named {name}.")


def _refuse_other_origin(held: Origin, identity: str, base: str, working_copy: Path) -> None:
    if held.identity == identity and held.base.casefold() == base.casefold():
        return
    raise EvidenceError(
        "foreign",
        f"{working_copy} is already this machine's working copy of the same project at "
        f"{held.identity} {held.base}, and {identity} {base} is a different origin. "
        "Two servers holding one project_id cannot share one working copy; give one of "
        "them a project of its own.",
    )


def checkout(
    source: ProjectSource,
    base: str,
    *,
    backend: str,
    identity: str,
    params: dict[str, Any],
    ctx: OpContext | None = None,
    defer: bool = False,
) -> Path:
    """This machine's working copy of the project the server lists as *base*'s name, made and
    filled (:func:`project_sync.checkout`'s rules: one working copy per origin, and a checkout
    of the same origin is a pull)."""
    ctx = ctx or OpContext()
    project_id = _named(source, base, ctx)
    working_copy = project_local_state.working_copy_dir(project_id, base)
    with project_sync._lock_for(working_copy):
        fresh = all(project_local_state.is_machine_state(p.name) for p in working_copy.iterdir())
        held = None if fresh else project_origin.load(working_copy)
        if held is not None:
            _refuse_other_origin(held, identity, base, working_copy)
            project_origin.save(held, working_copy)
            pull(working_copy, source, ctx=ctx, defer=defer)
            return working_copy
        origin = Origin(
            backend=backend,
            identity=identity,
            base=base,
            params=dict(params),
            project_id=project_id,
            needs_pull=True,
        )
        read = steps.whole(source, origin, ctx)
        config = read.refs.get(CONFIG_FILE)
        data = config.text.encode("utf-8") if config is not None and config.text else b""
        if project_origin.project_id_of(data) != project_id:
            raise EvidenceError(
                "corrupt",
                f"The project {project_id} on the server holds no {CONFIG_FILE} of its own.",
            )
        steps.download(source, working_copy, origin, config, None, ctx)  # type: ignore[arg-type]
        project_origin.save(origin, working_copy)
        log.info("Checked out %s from %s into %s", project_id, identity, working_copy)
        _pull(working_copy, source, origin, ctx, frozenset(), defer, None, None, read)
    return working_copy


def publish(
    working_copy: Path,
    source: ProjectSource,
    base: str,
    *,
    backend: str,
    identity: str,
    params: dict[str, Any],
    ctx: OpContext | None = None,
) -> SyncReport:
    """A project made here, created on the server in one request (``P11``): every file the sync
    carries and its bundle. A FIRST publish only (:func:`project_sync.publish`); the record is
    written once the server holds the project, so a publish that failed left no link. Its
    releases are landed as they are, which a web server takes from an account holding the
    importer right (contract §6.7).

    **Run again after a failure, it finishes**: the project created by the pass that failed is
    the same request again, a replay the server answers with the project (``P11``), and each
    release it already holds is answered again (``P10``, contract §6.7).

    **A project holding releases is refused before anything is written** by a web server that
    takes no import (``import`` not in its ``/info``'s ``calls``, contract §6.1): published
    without them, they would be lost without a word; and any project by a web server not offering
    P11, creating projects (:func:`publish_refused`)."""
    working_copy = Path(working_copy)
    project_sync._require_project_folder(working_copy)
    project_id = project_origin.project_id_in(working_copy)
    ctx = ctx or OpContext()
    with project_sync._lock_for(working_copy):
        held = project_origin.load(working_copy)
        if held is not None:
            _refuse_other_origin(held, identity, base, working_copy)
            raise EvidenceError(
                "denied",
                f"{working_copy} has already been published to {held.identity} {held.base}. "
                "Use push to send changes; publish is for a project never on a server.",
            )
        refused: list = []
        local, bundle_here = steps.scan(working_copy, refused)
        files = {rel: local_path(working_copy, rel).read_bytes() for rel in local}
        bundle = steps.bundle_files(working_copy, bundle_here) or None
        report = SyncReport(refused=[(r.path, f"{r.side}: {r.why}") for r in refused])
        if bundle and (why := steps.unsendable(bundle)):
            report.skipped.append((steps.BUNDLE, why))
            bundle = None
        releases = _releases_of(working_copy)
        if why := publish_refused(source, len(releases)):
            raise EvidenceError("invalid", why, code=server_offer.UNSUPPORTED)
        made = source.create_project(PurePosixPath(base).name, files, bundle, ctx)
        if made.project_id != project_id:
            raise EvidenceError("foreign", f"The server answered for project {made.project_id}.")
        _import_releases(releases, source, project_id, ctx)
        origin = Origin(
            backend=backend,
            identity=identity,
            base=base,
            params=dict(params),
            project_id=project_id,
            since=made.token,
            listing={p: steps.server_version(r) for p, r in made.files.items()},
            stores=steps.stores_of(made.files.values()),
        )
        there = {name_key(p): p for p in made.files}
        for rel, stamp in local.items():
            path = there.get(name_key(rel))
            if path is not None:
                record(origin, path, Row(steps.server_version(made.files[path]), stamp))
                report.uploaded.append(path)
        if bundle:
            origin.bundle_server = version_of(pack(bundle))
            origin.bundle_local = steps.bundle_stamp(bundle_here)
        project_origin.save(origin, working_copy)
        log.info("Published %s to %s as %s", project_id, identity, base)
        return report


class ReleasesNotKept(EvidenceError):
    """The server did not keep a release as it is: *landed* of the project's *total* releases
    it holds (the project itself is created)."""

    def __init__(self, exc: EvidenceError, message: str, landed: int, total: int) -> None:
        super().__init__(exc.kind, message, code=exc.code)
        self.landed, self.total = landed, total


def _releases_of(working_copy: Path) -> list:
    """The copy's releases (``releases/<test_id>/v<N>.json``), as records."""
    from .evidence_store_fs_shared import FsTree
    from .project_tree_releases import all_records

    return [kept for held in all_records(FsTree(working_copy), working_copy).values()
            for kept in held]  # fmt: skip


def publish_refused(source: ProjectSource, releases: int) -> str:
    """Why a project holding *releases* (0: none) is not published to *source*, before anything
    is written: its web server does not offer creating projects (P11, contract §6.1); or it
    takes no import while the project holds releases, which could not land as they are. ``""``
    when it may (a server offering both, or a storage that is no web server). Reads the
    server's ``/info``: on a worker."""
    session = getattr(source, "session", None)
    if session is None:
        return ""
    offered = session.offered()
    if not set(server_offer.CREATE) <= offered:
        return f"{server_offer.NO_CREATE} Nothing was published."
    if not releases or set(server_offer.IMPORT) <= offered:
        return ""
    which = "its release" if releases == 1 else f"its {releases} releases"
    return (
        f"{server_offer.NO_IMPORT} This project holds {which}, which would not move there: "
        "nothing was published."
    )


def _import_releases(
    kept_all: list, source: ProjectSource, project_id: str, ctx: OpContext
) -> None:
    """The copy's releases (*kept_all*, :func:`_releases_of`) landed on the server AS THEY ARE
    (``ProjectSource.import_release``): a project published from a folder keeps its release
    numbers, dates and documents. A server that will not take one (an account that may not import,
    §6.7) fails the publish (:class:`ReleasesNotKept`), and the record is never written:
    nothing is lost."""
    for landed, kept in enumerate(kept_all):
        try:
            source.import_release(project_id, kept, ctx)
        except EvidenceError as exc:
            message = (
                f"The server did not keep {kept.test}'s release v{kept.version} as it is: {exc}"
            )
            raise ReleasesNotKept(exc, message, landed, len(kept_all)) from exc


def restore(
    working_copy: Path,
    source: ProjectSource,
    paths: Iterable[str],
    *,
    ctx: OpContext | None = None,
) -> SyncReport:
    """Bring exactly *paths* back from the server (:func:`project_sync.restore`): a path the
    manifest has no row for is not restored, and nothing is written to the server. A file lands
    only where the working copy has none; one saved there since is a conflict, nothing landed.
    ``bundle`` brings the bundle back into a ``bundle/`` folder that holds no file."""
    working_copy = Path(working_copy)
    project_sync._require_project_folder(working_copy)
    ctx = ctx or OpContext()
    writer = project_sync_pass.tag()
    with project_sync._lock_for(working_copy):
        origin = project_sync._origin_of(working_copy)
        project_sync._require_same_project(working_copy, origin)
        report = SyncReport()
        read = None
        for rel in sorted(set(paths)):
            check_cancel(ctx)
            if rel == steps.BUNDLE and origin.bundle_local:
                read = read or steps.whole(source, origin, ctx)
                steps.restore_bundle(working_copy, origin, read, report, writer)
                continue
            if rel not in origin.files:
                report.skipped.append((rel, "the manifest has no row for it to come back from"))
                continue
            read = read or steps.whole(source, origin, ctx)
            if rel not in read.listing:
                report.skipped.append((rel, "the server no longer holds it"))
                continue
            try:
                conflict = steps.download(
                    source, working_copy, origin, steps.ref(read, rel), None, ctx
                )
            except (OSError, EvidenceError) as exc:
                failure = typed_failure(exc)
                if failure.kind in FATAL:
                    raise failure from exc
                report.skipped.append((rel, str(failure)))
                continue
            if isinstance(conflict, Conflict):
                report.conflicts.append(conflict)
            else:
                report.downloaded.append(rel)
        project_origin.save(origin, working_copy)
        return report


def resolve(
    working_copy: Path,
    source: ProjectSource,
    paths: Iterable[str],
    keep: str,
    *,
    ctx: OpContext | None = None,
) -> SyncReport:
    """Answer the recorded conflicts on *paths* (:func:`project_sync.resolve`'s rules): the copy
    not kept goes to its side's trash, and an answer is refused for a copy that changed since
    the question was asked."""
    if keep not in (MINE, THEIRS):
        raise ValueError(f"keep must be {MINE!r} or {THEIRS!r}, not {keep!r}")
    working_copy = Path(working_copy)
    project_sync._require_project_folder(working_copy)
    ctx = ctx or OpContext()
    writer = project_sync_pass.tag()
    with project_sync._lock_for(working_copy):
        origin = project_sync._origin_of(working_copy)
        project_sync._require_same_project(working_copy, origin)
        report = SyncReport()
        answer = _Answer(working_copy, source, origin, ctx, writer)
        for rel in sorted(set(paths)):
            check_cancel(ctx)
            asked = origin.conflicts.get(rel)
            why = (answerable(rel) if rel != steps.BUNDLE else "") or (
                "" if asked is not None else "no pass has found it in conflict"
            )
            done = ""
            if not why:
                try:
                    why, done = answer(asked, keep)  # type: ignore[arg-type]
                except (OSError, EvidenceError) as exc:
                    failure = typed_failure(exc)
                    if failure.kind in FATAL:
                        raise failure from exc
                    why = str(failure)
            if why:
                report.skipped.append((rel, why))
                continue
            origin.conflicts.pop(rel, None)
            project_origin.save(origin, working_copy)
            getattr(report, _DONE[done]).append(rel)
            if done in (DOWNLOAD, DELETE_LOCAL) and answer.grave is not None:
                report.local_trash = str(answer.grave)
        return report


_DONE = {
    UPLOAD: "uploaded",
    DOWNLOAD: "downloaded",
    DELETE_REMOTE: "deleted_remote",
    DELETE_LOCAL: "deleted_local",
}
_LOOK_AGAIN = "it changed since the question was asked; look again"


class _Answer:
    """One conflict answered: *keep* :data:`MINE` sends this PC's copy (or its delete) naming
    the server's version the question was about; :data:`THEIRS` lands the server's copy (or
    takes this one out), the replaced file into this PC's trash."""

    def __init__(
        self, working_copy: Path, source: ProjectSource, origin: Origin, ctx: OpContext, writer: str
    ) -> None:
        self.working_copy, self.source, self.origin, self.ctx = working_copy, source, origin, ctx
        self.grave = steps.trash_of(working_copy, writer)
        self._read: steps.Read | None = None

    def read(self) -> steps.Read:
        if self._read is None:
            self._read = steps.whole(self.source, self.origin, self.ctx)
        return self._read

    def __call__(self, asked: Conflict, keep: str) -> tuple[str, str]:
        if asked.path == steps.BUNDLE:
            return self._bundle(asked, keep)
        rel = asked.path
        here = local_now(self.working_copy, rel)
        if here != asked.local:
            return _LOOK_AGAIN, ""
        if (here if keep == MINE else asked.server) is None and rel == CONFIG_FILE:
            return f"an answer may not remove the project's {CONFIG_FILE}", ""
        if keep == MINE:
            return self._mine(asked, here)
        return self._theirs(asked, here)

    def _mine(self, asked: Conflict, here: Stamp | None) -> tuple[str, str]:
        rel, there = asked.path, asked.server
        replaces = there.version if there is not None else None
        if here is None:
            if replaces is None:
                self.origin.files.pop(rel, None)
                return "", DELETE_REMOTE
            item = steps.Sent(Delete(rel, replaces), None)
        else:
            item = steps.Sent(
                Write(rel, local_path(self.working_copy, rel).read_bytes(), replaces), here
            )
        return self._send(item, DELETE_REMOTE if here is None else UPLOAD)

    def _send(self, item: steps.Sent, done: str) -> tuple[str, str]:
        change = item.change
        paths = [] if isinstance(change, BundleWrite) else [change.path]
        pushed = self.source.push_changes(
            self.origin.project_id, [change], steps.drafts(self.working_copy, paths), self.ctx
        )
        outcome = steps.outcomes(pushed, 1)[0]
        if outcome.status == CHANGED:
            return "the server's copy changed since the question was asked; look again", ""
        if outcome.status != SAVED:
            return outcome.message or outcome.kind or outcome.status, ""
        steps.record_outcome(self.origin, item, outcome, self.origin.listing, SyncReport())
        return "", done

    def _theirs(self, asked: Conflict, here: Stamp | None) -> tuple[str, str]:
        rel, there = asked.path, asked.server
        if here is not None and self.grave is None:
            return "this PC has no state folder to keep its copy in, so nothing was changed", ""
        target = None if self.grave is None else self.grave.joinpath(*rel.split("/"))
        if target is not None and here is not None and target.exists():
            return "a file of that name is already in this PC's trash for this pass", ""
        if there is None:
            move_aside(local_path(self.working_copy, rel), target)  # type: ignore[arg-type]
            self.origin.files.pop(rel, None)
            return "", DELETE_LOCAL
        read = self.read()
        if read.listing.get(rel) != there:
            return "the server's copy changed since the question was asked; look again", ""
        conflict = steps.download(
            self.source,
            self.working_copy,
            self.origin,
            steps.ref(read, rel),
            here,
            self.ctx,
            aside=target if here is not None else None,
        )
        return (
            ("the working copy's file changed since the question was asked; look again", "")
            if (conflict is not None)
            else ("", DOWNLOAD)
        )

    def _bundle(self, asked: Conflict, keep: str) -> tuple[str, str]:
        _files, stamps = steps.scan(self.working_copy)
        if Stamp(0, 0.0, steps.bundle_stamp(stamps)) != asked.local:
            return _LOOK_AGAIN, ""
        if keep == MINE:
            replaces = asked.server.version if asked.server is not None else ""
            return self._send(steps.bundle_write(self.working_copy, stamps, replaces), UPLOAD)
        read = self.read()
        if read.bundle is None or read.bundle.version != asked.server.version:  # type: ignore[union-attr]
            return "the server's copy changed since the question was asked; look again", ""
        landed = steps.land_bundle(self.working_copy, read.bundle, self.grave)
        self.origin.bundle_server = read.bundle.version
        self.origin.bundle_local = steps.bundle_stamp(landed)
        return "", DOWNLOAD


class Stranded(EvidenceError):
    """A rename neither finished nor taken back: :attr:`server` and :attr:`here` name the test
    as the server and this working copy hold it now."""

    def __init__(self, message: str, server: str, here: str) -> None:
        super().__init__("invalid", message)
        self.server, self.here = server, here


def where_it_is(server: str, here: str) -> str:
    """Where a test is after a rename that could not be taken back, in one sentence."""
    if server == here:
        return f"The test is named {server} on the server and here."
    return f"On the server the test is named {server}; here it is named {here}."


def rename_test(
    working_copy: Path,
    source: ProjectSource,
    old: str,
    new: str,
    *,
    folder: Mapping[str, str] | None = None,
    ctx: OpContext | None = None,
) -> dict[str, str]:
    """``tests/<old>`` renamed to ``tests/<new>`` on the server in ONE move (``P9``), naming the
    version of every file it holds: as this copy last synced it, or *folder* when the caller
    holds it (taking back a move this answered, whatever the record on disk says). Its draft
    follows it there. Then the same here: the folder and the rows. Its releases and its
    evidence stay under its id. Returns the versions moved, what taking it back names.

    A move the server did not make (the test changed there since) raises, nothing changed on
    either side. A folder here that will not move (a file held open) takes the server's move
    back and raises; when that fails too, it raises :class:`Stranded`. Once both sides moved
    the rename stands."""
    working_copy = Path(working_copy)
    project_sync._require_project_folder(working_copy)
    ctx = ctx or OpContext()
    with project_sync._lock_for(working_copy):
        origin = project_sync._origin_of(working_copy)
        project_sync._require_same_project(working_copy, origin)
        prefix = f"{TESTS_DIR}/{old}/"
        if folder is None:
            folder = {
                rel[len(prefix) :]: row.server.version
                for rel, row in origin.files.items()
                if rel.startswith(prefix)
            }
        moved = dict(folder)
        if moved:
            outcome = source.rename_test(origin.project_id, old, new, moved, ctx)
            if outcome.status != SAVED:
                raise EvidenceError(
                    "denied" if outcome.kind == "denied" else "invalid",
                    outcome.message
                    or f"The test {old} changed on the server: refresh from the server, then "
                    "rename it.",
                    code=outcome.status,
                )
        tests = working_copy / TESTS_DIR
        try:
            (tests / old).rename(tests / new)
        except OSError as exc:
            if moved:
                _take_back(source, origin.project_id, old, new, moved, exc)
            raise
        _follow_here(working_copy, origin, old, new)
        return moved


def _take_back(
    source: ProjectSource,
    project_id: str,
    old: str,
    new: str,
    folder: dict[str, str],
    exc: OSError,
) -> None:
    """The server's move of *old* to *new* taken back, the folder here not having followed
    (*exc*). One that cannot be raises :class:`Stranded`."""
    try:  # never stopped by the cancel that may have stopped the folder
        undone = source.rename_test(project_id, new, old, folder, OpContext())
        why = "" if undone.status == SAVED else (undone.message or undone.status)
    except (OSError, EvidenceError) as undo:
        why = str(undo)
    if why:
        raise Stranded(
            f"The server renamed {old} to {new}, but the folder here could not follow ({exc}), "
            f"and the server's rename could not be taken back ({why}). "
            f"{where_it_is(new, old)} The next refresh from the server brings it here as {new}.",
            new,
            old,
        ) from exc


def _follow_here(working_copy: Path, origin: Origin, old: str, new: str) -> None:
    """Both sides moved: this copy's rows follow. Nothing here raises, the rename stands: a
    record that cannot be saved is the next pull's to settle by the bytes."""
    prefix, moved = f"{TESTS_DIR}/{old}/", f"{TESTS_DIR}/{new}/"
    for rel in [r for r in origin.files if r.startswith(prefix)]:
        origin.files[moved + rel[len(prefix) :]] = origin.files.pop(rel)
    for rel in [r for r in origin.listing if r.startswith(prefix)]:
        origin.listing[moved + rel[len(prefix) :]] = origin.listing.pop(rel)
    try:
        project_origin.save(origin, working_copy)
    except (OSError, EvidenceError):
        log.warning("project sync: the record of %s was not saved", working_copy, exc_info=True)


def compare(
    working_copy: Path, source: ProjectSource, paths: list[str], ctx: OpContext | None = None
) -> list[tuple[str, bytes | None, bytes | None]]:
    """Each of *paths* as this PC and the server hold it, for Compare. Reads only."""
    working_copy = Path(working_copy)
    origin = project_sync._origin_of(working_copy)
    ctx = ctx or OpContext()
    read = steps.whole(source, origin, ctx)
    found = []
    for rel in paths:
        try:
            mine: bytes | None = local_path(working_copy, rel).read_bytes()
        except FileNotFoundError:
            mine = None
        theirs = None
        if rel in read.listing:
            ref = steps.ref(read, rel)
            if ref.text is not None:
                theirs = ref.text.encode("utf-8")
            else:
                scratch = part_of(local_path(working_copy, rel), steps.server_version(ref))
                try:
                    theirs = source.fetch(origin.project_id, ref, scratch, ctx).read_bytes()
                finally:
                    with suppress(OSError):
                        scratch.unlink()
        found.append((rel, mine, theirs))
    return found


def remote_test(
    working_copy: Path, source: ProjectSource, test: str, scratch: Path, ctx: OpContext
) -> None:
    """*test* as the server holds it, laid out as a project in *scratch*: its files and the
    ``functions/`` (``P7``), and its releases (this copy's ``releases/<test_id>``, the id the
    server's ``test.json`` holds). What :mod:`conflict_versions` tells the server's version from."""
    origin = project_sync._origin_of(Path(working_copy))
    found = source.read_tests(origin.project_id, [test], ctx).get(test)
    for path, ref in ((found.files or {}) if found is not None else {}).items():
        target = Path(scratch).joinpath(*path.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        source.fetch(origin.project_id, ref, target, ctx)
    test_id = test_release.test_id_of(Path(scratch), test)  # the server's test.json's
    releases = test_release.folder_of(Path(working_copy), test_id) if test_id else None
    if releases is not None and releases.is_dir():
        kept = test_release.folder_of(Path(scratch), test_id)
        kept.mkdir(parents=True, exist_ok=True)
        for item in releases.glob("*.json"):
            (kept / item.name).write_bytes(item.read_bytes())
