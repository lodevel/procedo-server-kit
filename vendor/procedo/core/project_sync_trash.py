"""What a project-sync pass takes out of the project, on either side: moved into a trash, never
unlinked.

The owner's rule: *"if someone deletes the folder by mistake everyone loses it?"* A file removed on
the server goes to ``<base>/.trash/<pass>/``, a file removed from the working copy to this PC's
trash, and a move that fails leaves the file exactly where it is. Each removal first proves the
file is still the one the plan was made from.

A folder a removal left EMPTY goes too (:func:`prune`), on the side it was emptied on: a deleted
test is a folder, and an empty one left behind still reads as a test to everyone who lists the
project's folders. Only ever by ``rmdir``, which refuses a folder holding anything, and never a
top-level folder of the project (``tests/``, ``config/``…).

Qt-free, stdlib only.
"""

from __future__ import annotations

import os
import shutil
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path, PurePath, PurePosixPath

from . import atomic_write
from .evidence_store import EvidenceError
from .evidence_tree import TreeIO, make_dirs
from .project_never_travels import TRASH_DIR
from .project_origin import Origin, Row, Stamp
from .project_sync_pass import why_failed
from .project_sync_scan import local_path, server_stamp


def move_aside(src: Path, target: Path) -> None:
    """Put *src* at *target*, whole or not at all — never a half-written copy.

    ``os.replace`` when the two are on one volume (the state folder and the working
    copy both hang off the app-data root, so they usually are), and otherwise a copy
    whose size is checked before the original is unlinked. Either way the bytes
    exist somewhere at every moment.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        atomic_write.replace(src, target)
        return
    except OSError:
        pass  # another volume, or a rename the filesystem refuses: copy it instead
    before = src.stat()
    shutil.copy2(src, target)
    after = src.stat()
    if target.stat().st_size != after.st_size or (before.st_size, before.st_mtime) != (
        after.st_size,
        after.st_mtime,
    ):
        # Not whole, or saved while it was copied: the copy may not hold the newest bytes.
        with suppress(OSError):
            target.unlink()
        raise EvidenceError("corrupt", f"{src.name} was not copied whole into the trash")
    src.unlink()


def trash_local(
    tree: TreeIO,
    base: PurePath,
    working_copy: Path,
    origin: Origin,
    rel: str,
    row: Row,
    grave: Path | None,
) -> str:
    """Take the working copy's file OUT of the project, having proved it is the one
    planned — into this PC's trash, never to nothing.

    The rule the server side has, on this side too, and for a reason measured on a
    real project: *"a local delete needs no approval, the server's copy is in
    ``.trash``"* is only true when THIS APP did the deleting. An administrator's
    cleanup, a restored backup, a folder dragged aside on the server — the file is
    simply gone from the listing, the plan says "delete locally", and the last copy
    of somebody's work was unlinked. Now it is moved to
    :func:`project_local_state.local_trash_dir`, and a move that FAILS leaves the
    file exactly where it is.

    Why the file is re-read rather than trusted: between the scan and here, the
    runner may have rewritten it and the server may have put it back. Either makes
    this row stale, and a stale row is not licence to remove it — the next pass will
    look again. The WHOLE stamp decides it, so an edit of the same length in the
    same second is still an edit.
    """
    return take_out(working_copy, origin, rel, row, grave, back=lambda: tree.exists(base / rel))


def take_out(
    working_copy: Path,
    origin: Origin,
    rel: str,
    row: Row,
    grave: Path | None,
    *,
    back: Callable[[], bool],
) -> str:
    """:func:`trash_local` for any storage: *back* says whether the server holds the file
    again (``""`` when it went into *grave*, else why it stayed)."""
    dst = local_path(working_copy, rel)
    try:
        here = dst.stat()
    except FileNotFoundError:
        origin.files.pop(rel, None)  # somebody was quicker; the row is all that is left
        return ""
    if Stamp(here.st_size, here.st_mtime) != row.local:
        return "the working copy's file changed after the scan"
    if back():
        return "the file is on the server again"
    if grave is None:
        return "this PC has no state folder to keep it in, so it was left alone"
    target = grave.joinpath(*rel.split("/"))
    if target.exists():
        return "a file of that name is already in this PC's trash for this pass"
    try:
        move_aside(dst, target)
    except OSError as exc:
        return why_failed(exc)
    origin.files.pop(rel, None)
    prune(rel, lambda folder: _rmdir(local_path(working_copy, folder)))
    return ""


def _rmdir(folder: Path) -> bool:
    try:
        os.rmdir(folder)
    except OSError:
        return False
    return True


def prune(rel: str, remove_empty: Callable[[str], bool]) -> None:
    """Remove the folders above *rel* that are now empty, deepest first, up to and NOT
    including the project's top-level folder; ``remove_empty(folder)`` removes one only when
    it is empty and says whether it did. The first one that stays ends it: a folder above a
    folder that holds something is not empty either."""
    for folder in PurePosixPath(rel).parents:
        if len(folder.parts) < 2 or not remove_empty(folder.as_posix()):
            return


def grave_name(writer: str) -> str:
    """The folder ONE pass keeps what it removed under, on either side.

    The stamp alone was not enough: it has one-second granularity, and two passes
    inside one second aimed at the same name — where ``rename`` (FTP's RNFR/RNTO on
    many servers) OVERWRITES without a word, destroying the copy the whole rule
    exists to keep. The writer tag makes it unique per pass, as it does for every
    other temp name the sync writes.
    """
    return f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{writer}"


def trash_remote(
    tree: TreeIO,
    base: PurePath,
    working_copy: Path,
    origin: Origin,
    rel: str,
    row: Row,
    grave_of: str,
) -> str:
    """Move the server's file into ``.trash``, having proved it is the one planned.

    **Never an unlink.** The owner's rule: *"if someone deletes the folder by
    mistake everyone loses it?"* — so a confirmed delete is a rename into
    ``<base>/.trash/<UTC stamp>/<the path it had>``, which every other bench can
    move back. ``.trash`` is outside the sync in both directions, so it neither
    comes down nor goes up nor is ever itself trashed. Nothing purges it: that is
    the server owner's business, as it is for any other folder on their disk.

    The listing that planned this is already old, so the entry is looked up again —
    in an operation scope of its own (:func:`project_sync.pull` closes the scan's
    before the deletes run), because a tree may serve a stat from the listing it
    cached for the operation that planned the delete, which would make the proof
    read its own evidence.

    A rename that fails removes NOTHING: the file stays where it is, the row stays
    with it, and the caller reports it. "It could not be kept" is never a reason to
    destroy it instead.
    """
    if local_path(working_copy, rel).exists():
        return "the file is back in the working copy"
    remote = base / rel
    try:
        server = server_stamp(tree, remote)
    except FileNotFoundError:
        origin.files.pop(rel, None)  # already gone: this pass finishes the last one
        return ""
    if server != row.server:
        return "the server's copy changed after the listing"
    through = getattr(tree, "through_link", None)  # a transport that can have links
    if through is not None and through(base, remote):
        return "it is reached through a link on the share, so it is not this project's to remove"
    why = into_server_trash(tree, base, rel, grave_of)
    if not why:
        origin.files.pop(rel, None)
        hook = getattr(tree, "remove_empty_folder", None)  # a transport that has one
        if hook is not None:
            prune(rel, lambda folder: hook(base.joinpath(*folder.split("/"))))
    return why


def into_server_trash(tree: TreeIO, base: PurePath, rel: str, grave_of: str) -> str:
    """Rename the server's *rel* into ``<base>/.trash/<grave_of>/<rel>``; why not (the file
    is left where it is), or ``""``."""
    remote = base / rel
    grave = base / TRASH_DIR / grave_of
    for part in rel.split("/")[:-1]:
        grave = grave / part
    make_dirs(tree, base, grave)
    kept = grave / rel.split("/")[-1]
    if tree.exists(kept):
        # Looked up in THIS operation's own scope, so it is not the listing that planned
        # the pass answering about itself. A rename over it would destroy the copy
        # somebody may be about to put back — the one thing the trash exists for.
        return "a file of that name is already in the server's trash; it was kept where it is"
    tree.rename(remote, kept)
    return ""
