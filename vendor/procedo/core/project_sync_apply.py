"""A project-sync plan carried out: every transfer, then every delete, the record saved as the
pass goes. Each file is moved by :mod:`project_sync_transfer` or removed by
:mod:`project_sync_trash`; one that fails is reported and the pass goes on, unless the failure is
about the pass (:data:`project_sync_pass.FATAL`).

Qt-free, stdlib only.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from pathlib import Path, PurePath
from typing import Any

from . import project_local_state, project_origin, project_sync_trash
from .evidence_store import EvidenceError, OpContext
from .evidence_tree import TreeIO, name_key
from .project_origin import Origin, Stamp
from .project_sync_pass import FATAL, check_cancel, operation, tag, typed_failure
from .project_sync_plan import DELETE_LOCAL, DELETE_REMOTE, DOWNLOAD, UPLOAD, Plan
from .project_sync_report import SyncReport
from .project_sync_transfer import HELD, SENT, download, upload

log = logging.getLogger(__name__)


#: A manifest is saved after a file this large lands, and otherwise at most this often.
SAVE_AFTER_BYTES = 1 << 20
SAVE_EVERY_S = 1.0


class Saver:
    """When the record is written during a pass: after a large file, otherwise at most
    once a second, and always at the end. Writing the whole record after every file made
    a first checkout of a large project quadratic; a pass that dies between two saves
    still resumes, because a file that landed with no row reads new / new with the same
    bytes and settles without a question (:func:`settle`)."""

    def __init__(self, origin: Origin, working_copy: Path) -> None:
        self.origin, self.working_copy = origin, working_copy
        self.last = time.monotonic()
        self.dirty = False

    def moved(self, size: int) -> None:
        self.dirty = True
        if size > SAVE_AFTER_BYTES or time.monotonic() - self.last >= SAVE_EVERY_S:
            self.flush()

    def flush(self) -> None:
        if self.dirty:
            project_origin.save(self.origin, self.working_copy)
            self.dirty = False
        self.last = time.monotonic()


def moves(
    tree: TreeIO,
    base: PurePath,
    working_copy: Path,
    origin: Origin,
    made: Plan,
    ctx: OpContext,
    writer: str,
    report: SyncReport,
    *,
    listed: Mapping[str, Stamp],
    scanned: Mapping[str, Stamp] | None = None,
    deferred: Callable[[str, int], bool] | None = None,
    defer: bool = False,
    hold: Callable[[], bool] | None = None,
    progress: Callable[[str, Stamp | None], None] | None = None,
) -> None:
    """Every download and upload, each followed by its manifest row. *progress* is told each
    download as it starts (its path and the listing's stamp: a window shows how far it is from
    its resumable temp).

    *listed* is the server listing the plan was made from — for a push, the
    manifest standing in for one — and every upload is checked against it first
    (:func:`upload`); *scanned* is the working copy's scan, and every download is
    checked against it right before it lands (:func:`download`). One whose other
    side moved on is reported in ``conflicts``.

    *deferred* names the downloads of the deferred class (``project_sync_plan.deferred``,
    given the path and its size on the server): with *defer* they are left for later and
    reported in ``deferred``; without it they come after every other file, the largest
    last, so the project is whole as early as it can be.

    *hold* is asked right before each download and again right before it lands: ``True`` (a
    test runs from the working copy) leaves it for a later pass, reported in ``held``.

    The record is written as the pass goes (:class:`Saver`), not only at the end: a pass
    that stops halfway — a dropped link, a cancel, a bench switched off — must leave a
    state the next pass completes rather than one it has to ask about. A file that fails
    is reported and the pass goes on (:data:`FATAL` is the exception).
    """
    here = {name_key(rel): stamp for rel, stamp in (scanned or {}).items()}

    def late(action: Any) -> bool:
        size = listed[action.path].size if action.path in listed else 0
        return action.kind == DOWNLOAD and deferred is not None and deferred(action.path, size)

    first = [a for a in made.actions if a.kind in (DOWNLOAD, UPLOAD) and not late(a)]
    last = sorted(
        (a for a in made.actions if a.kind == DOWNLOAD and late(a)),
        key=lambda a: (listed[a.path].size if a.path in listed else 0, a.path),
    )
    if defer:
        report.deferred.extend(a.path for a in last)
        last = []
    saver = Saver(origin, working_copy)
    try:
        for action in first + last:
            check_cancel(ctx)
            if action.kind == DOWNLOAD and hold is not None and hold():
                report.held.append(action.path)
                continue
            if action.kind == DOWNLOAD and progress is not None:
                progress(action.path, listed.get(action.path))
            try:
                if action.kind == DOWNLOAD:
                    done, conflict = download(
                        tree,
                        base,
                        working_copy,
                        origin,
                        action.path,
                        writer,
                        here.get(name_key(action.path)),
                        hold,
                    )
                    done_list = report.downloaded
                else:
                    seen = listed.get(action.path)
                    done, conflict = upload(
                        tree, base, working_copy, origin, action.path, writer, seen
                    )
                    done_list = report.uploaded
                if conflict is not None:
                    report.conflicts.append(conflict)
                    continue
                if done == HELD:
                    report.held.append(action.path)
                    continue
                if done == SENT:
                    done_list.append(action.path)
            except (OSError, EvidenceError) as exc:
                failure = typed_failure(exc)
                if failure.kind in FATAL:
                    raise failure from exc
                log.info("project sync: %s did not %s — %s", action.path, action.kind, failure)
                report.skipped.append((action.path, str(failure)))
                continue
            row = origin.files.get(action.path)
            saver.moved(row.local.size if row is not None else 0)
    finally:
        saver.flush()


def deletes(
    tree: TreeIO,
    base: PurePath,
    working_copy: Path,
    origin: Origin,
    made: Plan,
    ctx: OpContext,
    report: SyncReport,
    approved: frozenset[str] = frozenset(),
    writer: str = "",
    *,
    hold: Callable[[], bool] | None = None,
) -> None:
    """Every delete, LAST and one operation scope each — see :func:`trash_remote`.

    Last, so a pass that dies among the transfers has destroyed nothing; one scope
    each, so the proof cannot be served from the listing that planned it.

    **A server-side delete happens only for a path in *approved*.** The owner's
    rule, and it is enforced PER PATH rather than per pass: approving ``{a, b}``
    cannot remove ``c``, whatever the plan grew into between the question and the
    answer. An unapproved one is left exactly as it is — file, row and all — and
    reported in ``pending_deletes``, so the next pass asks the same question.

    A LOCAL delete needs no approval — but it is not a delete either: the file is
    moved to this PC's trash (:func:`trash_local`), because a file can leave a
    server in ways this app never sees and the working copy may hold its last copy.
    While *hold* answers ``True`` (a test runs from the working copy) none is taken out:
    it is reported in ``held`` for a later pass.

    A test whose last file this pass removed from the server is deleted there: its releases
    stay under its id, and a later test of that name has its own.
    """
    grave_of = project_sync_trash.grave_name(writer or tag())
    local_grave = project_local_state.local_trash_dir(working_copy)
    local_grave = None if local_grave is None else local_grave / grave_of
    for action in made.actions:
        if action.kind not in (DELETE_LOCAL, DELETE_REMOTE):
            continue
        check_cancel(ctx)
        if action.kind == DELETE_LOCAL and hold is not None and hold():
            report.held.append(action.path)
            continue
        if action.kind == DELETE_REMOTE and action.path not in approved:
            report.pending_deletes.append(action.path)
            continue
        row = origin.files.get(action.path)
        if row is None:  # the row that justified it is gone: prove it again next pass
            report.skipped.append((action.path, "the manifest row that justified it is gone"))
            continue
        try:
            with operation(tree, ctx):
                if action.kind == DELETE_LOCAL:
                    why = project_sync_trash.trash_local(
                        tree, base, working_copy, origin, action.path, row, local_grave
                    )
                else:
                    why = project_sync_trash.trash_remote(
                        tree, base, working_copy, origin, action.path, row, grave_of
                    )
        except (OSError, EvidenceError) as exc:
            failure = typed_failure(exc)
            if failure.kind in FATAL:
                raise failure from exc
            log.info("project sync: %s was not removed — %s", action.path, failure)
            report.skipped.append((action.path, str(failure)))
            continue
        if why:
            log.info("project sync: not deleting %s — %s", action.path, why)
            report.skipped.append((action.path, why))
            continue
        if action.kind == DELETE_LOCAL:
            report.deleted_local.append(action.path)
            if local_grave is not None:
                report.local_trash = str(local_grave)
        else:
            report.deleted_remote.append(action.path)
        project_origin.save(origin, working_copy)
