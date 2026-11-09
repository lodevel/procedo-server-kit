"""Conflicts of a project-sync pass: settled when the two copies hold the same bytes, otherwise
answered by somebody — *keep mine* or *take theirs* — with the copy not kept put in its side's
trash (docs/plans/project_working_copy_design.md, "Conflicts"). :mod:`project_sync_transfer` moves the
bytes; this module decides, per conflict, which way they move.
"""

from __future__ import annotations

import logging
from contextlib import suppress
from pathlib import Path, PurePath

from . import project_origin
from .evidence_store import EvidenceError, OpContext
from .evidence_tree import TreeIO, forget_listing
from .paths import sha256_file
from .project_never_travels import exclusion
from .project_origin import CONFIG_FILE, Origin, Row
from .project_sync_pass import FATAL, check_cancel, typed_failure
from .project_sync_plan import (
    DELETE_LOCAL,
    DELETE_REMOTE,
    DOWNLOAD,
    UPLOAD,
    Conflict,
    Plan,
    is_release,
    unsafe,
)
from .project_sync_report import SyncReport
from .project_sync_scan import local_now, local_path, server_now, server_stamp
from .project_sync_transfer import fetch_part, land_part, record, send
from .project_sync_trash import into_server_trash, move_aside

log = logging.getLogger(__name__)


def settle(
    tree: TreeIO,
    base: PurePath,
    working_copy: Path,
    origin: Origin,
    made: Plan,
    report: SyncReport,
    ctx: OpContext,
) -> None:
    """Each of the plan's conflicts whose two copies hold the same bytes is no conflict:
    its row records both sides as the plan saw them and nothing moves. Every other one
    goes into ``report.conflicts``, both copies as they are.

    The sizes are compared first, so a real conflict costs no read; equal sizes are read
    and hashed on both sides (SHA-256), each side first proved still the copy the plan
    saw (the server's looked up again, not served from the listing that planned it). A
    copy that moved since the plan, or that could not be read, stays a conflict: the
    next pass looks again. This is also what makes an interrupted first checkout resume:
    a file that landed before its row was saved reads ``new``/``new`` with the same
    bytes."""
    for conflict in made.conflicts:
        check_cancel(ctx)
        try:
            same = _same_bytes(tree, base, working_copy, conflict)
        except (OSError, EvidenceError) as exc:
            failure = typed_failure(exc)
            if failure.kind in FATAL:
                raise failure from exc
            same = False
        if same:
            record(origin, conflict.path, Row(conflict.server, conflict.local))  # type: ignore[arg-type]
            project_origin.save(origin, working_copy)
            continue
        log.warning(
            "project sync: %s is in conflict (%s); neither copy was touched",
            conflict.path,
            conflict.kind,
        )
        report.conflicts.append(conflict)


def _same_bytes(tree: TreeIO, base: PurePath, working_copy: Path, conflict: Conflict) -> bool:
    if conflict.local is None or conflict.server is None:
        return False  # a deletion against an edit: there are no two copies to compare
    if conflict.local.size != conflict.server.size:
        return False
    if local_now(working_copy, conflict.path) != conflict.local:
        return False
    remote = base / conflict.path
    forget_listing(tree, remote.parent)
    if server_now(tree, remote) != conflict.server:
        return False
    return tree.sha256(remote) == sha256_file(local_path(working_copy, conflict.path))


#: The two answers to a conflict: the working copy's file is kept, or the server's.
MINE = "mine"
THEIRS = "theirs"


def answerable(rel: str) -> str:
    """Why *rel* may not be answered at all (``""`` when it may): a path the sync never
    carries — evidence, the trash, one bench's files — or one it refuses."""
    return exclusion(rel) or unsafe(rel)


def resolve_one(
    tree: TreeIO,
    base: PurePath,
    working_copy: Path,
    origin: Origin,
    asked: Conflict,
    keep: str,
    writer: str,
    local_grave: Path | None,
    grave_of: str,
) -> tuple[str, str]:
    """Answer one conflict, *asked* as a pass recorded it: *keep* :data:`MINE` or
    :data:`THEIRS`. ``(why not, "")`` — both copies as they were — or ``("", what was
    done)``: ``UPLOAD``, ``DOWNLOAD``, ``DELETE_REMOTE`` or ``DELETE_LOCAL``.

    **It answers the question that was asked.** Both copies are looked up again first;
    one that is no longer the copy the conflict recorded is refused ("look again"): the
    answer was given about other bytes.

    **Neither answer loses bytes, and no failure empties the path.** The copy that is not
    kept goes to its side's trash — the server's ``.trash`` (:func:`into_server_trash`)
    or this PC's (*local_grave*) — and the kept one is in place on the other side BEFORE
    that, under a ``.part`` name, so what is left between the two is two renames:

    * mine, here a file: sent to ``<path>.<writer>.part`` on the server, then the
      server's copy (if any) to its trash, then the ``.part`` renamed into place;
    * mine, here none (deleted here): the server's copy to the server's trash;
    * theirs, there a file: streamed to a local temp first, then the working copy's file
      (if any) to this PC's trash and the temp into place (:func:`land_part`, *aside*);
    * theirs, there none (deleted on the server): the working copy's file to this PC's
      trash.

    An answer that would leave the project without ``config/config.json`` is refused."""
    if keep not in (MINE, THEIRS):
        raise ValueError(f"keep must be {MINE!r} or {THEIRS!r}, not {keep!r}")
    rel = asked.path
    remote = base / rel
    forget_listing(tree, remote.parent)
    here, there = local_now(working_copy, rel), server_now(tree, remote)
    if (here, there) != (asked.local, asked.server):
        return "it changed since the question was asked; look again", ""
    if (here if keep == MINE else there) is None and rel == CONFIG_FILE:
        return f"an answer may not remove the project's {CONFIG_FILE}", ""
    if keep == MINE and there is not None and is_release(rel):
        return "a release never changes: only the share's copy can be kept", ""
    if keep == MINE:
        if here is None:
            why = into_server_trash(tree, base, rel, grave_of)
            if not why:
                origin.files.pop(rel, None)
            return why, "" if why else DELETE_REMOTE
        src = local_path(working_copy, rel)
        if there is None:
            send(tree, src, remote, rel, origin, writer)
            return "", UPLOAD
        part = remote.with_name(f"{remote.name}.{writer}.part")
        tree.put_file(src, part, writer, verify=True)  # the kept copy is on the server first
        why = into_server_trash(tree, base, rel, grave_of)
        if why:
            with suppress(OSError, EvidenceError):
                tree.unlink(part, missing_ok=True)
            return why, ""
        tree.rename(part, remote)
        record(origin, rel, Row(server_stamp(tree, remote), here))
        return "", UPLOAD
    grave = None if local_grave is None else local_grave.joinpath(*rel.split("/"))
    if here is not None:
        if grave is None:
            return "this PC has no state folder to keep its copy in, so nothing was changed", ""
        if grave.exists():
            return "a file of that name is already in this PC's trash for this pass", ""
    if there is None:
        if local_now(working_copy, rel) != here:
            return "it changed since the question was asked; look again", ""
        move_aside(local_path(working_copy, rel), grave)  # type: ignore[arg-type]
        origin.files.pop(rel, None)
        return "", DELETE_LOCAL
    server = server_stamp(tree, remote)
    if server != there:
        return "the server's copy changed since the question was asked; look again", ""
    part, md5 = fetch_part(tree, remote, local_path(working_copy, rel), server, rel)
    if not land_part(working_copy, rel, part, server, origin, expect=here, aside=grave, md5=md5):
        return "the working copy's file changed since the question was asked; look again", ""
    return "", DOWNLOAD
