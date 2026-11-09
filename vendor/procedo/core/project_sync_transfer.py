"""One file of a project-sync pass moved between the working copy and the server, and its
manifest row recorded.

* **A download lands through a temp file and a retried ``os.replace``**, so a reader sees the old
  bytes or the new bytes and never half of either; it is streamed into a resumable temp, never
  held in memory whole.
* **A manifest row is written only after the transfer is confirmed** against the size the other
  side reports — and only for the file that succeeded.
* **A download never replaces a file saved since the scan, and an upload never overwrites a server
  file the plan did not see.** Right before the bytes land or are sent, the other copy is looked
  up again and must still be the one the plan was made from; one that moved is a conflict,
  reported, and neither side is touched (:func:`download`, :func:`upload`).

Qt-free, stdlib only.
"""

from __future__ import annotations

import hashlib
import logging
import os
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path, PurePath
from typing import Any

from . import atomic_write
from .evidence_store import EvidenceError
from .evidence_tree import TreeIO, forget_listing
from .paths import sha256_file
from .project_origin import Origin, Row, Stamp
from .project_sync_pass import disk_full
from .project_sync_plan import (
    DELETED_EDITED,
    EDITED_DELETED,
    EDITED_EDITED,
    NEW_NEW,
    Conflict,
)
from .project_sync_scan import local_now, local_path, server_now, server_stamp
from .project_sync_trash import move_aside

log = logging.getLogger(__name__)


def record(origin: Origin, rel: str, row: Row) -> None:
    """The manifest's row for *rel*, under that spelling alone: a row the file had under
    another case (the server spelled it otherwise before) is dropped, because the planner
    matches the two sides case-insensitively and a stale twin would be read in its place."""
    for other in [k for k in origin.files if k != rel and k.casefold() == rel.casefold()]:
        del origin.files[other]
    origin.files[rel] = row


#: :func:`land`'s *expect* when the working copy's file is not checked: a checkout's first file.
ANY: Any = object()


def land(
    working_copy: Path,
    rel: str,
    data: bytes,
    server: Stamp,
    origin: Origin,
    writer: str,
    *,
    expect: Any = ANY,
    aside: Path | None = None,
) -> bool:
    """Make the working copy's file hold *data*, in one step, and record the row; ``False``
    (nothing recorded) when the working copy's file was saved meanwhile. The bytes go to a
    temp beside the target, flushed to disk, then :func:`land_part` lands it."""
    dst = local_path(working_copy, rel)
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f"{dst.name}.{writer}.part")
    try:
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        with suppress(OSError):
            tmp.unlink()
        raise
    return land_part(working_copy, rel, tmp, server, origin, expect=expect, aside=aside)


def land_part(
    working_copy: Path,
    rel: str,
    tmp: Path,
    server: Stamp,
    origin: Origin,
    *,
    expect: Any = ANY,
    aside: Path | None = None,
    md5: str = "",
) -> bool:
    """Land the finished temp *tmp* (on disk, flushed) as the working copy's *rel* and
    record the row; ``False`` (nothing recorded, *tmp* removed) when the working copy's
    file was saved meanwhile.

    ``atomic_write.replace`` (``os.replace`` with the measured Windows rename retry),
    because the runner may be reading that file right now: it sees the old bytes or the
    new bytes, never half of either — the board archive stays in use until the new one
    is in place. The temp is removed when the landing does not happen; its ``.part``
    name keeps it out of every scan in the meantime.

    Only now is the working copy's file looked at again: *expect* is the stamp it must
    still have (``None``: still absent; :data:`ANY`: not looked at). A save Studio or the
    runner made while the bytes were written is never replaced: ``False``, the caller's
    conflict. *aside* is where the file being replaced goes first (this PC's trash), so an
    answered conflict never destroys it.

    The row records the stamp the temp had once flushed (a rename on one volume keeps it)
    and *md5*. A file that no longer has that stamp after the replace was saved over in
    between: its row is not recorded (``False``), and the next pass meets it changed on
    both sides.
    """
    dst = local_path(working_copy, rel)
    landed = False
    try:
        written = tmp.stat()
        if written.st_size != server.size:
            raise EvidenceError(
                "corrupt", f"{rel} landed as {written.st_size} bytes of {server.size}"
            )
        if expect is not ANY and local_now(working_copy, rel) != expect:
            return False
        if aside is not None and dst.exists():
            move_aside(dst, aside)
        atomic_write.replace(tmp, dst)
        landed = True
    finally:
        if not landed:
            with suppress(OSError):
                tmp.unlink()
    here = dst.stat()
    if (here.st_size, here.st_mtime) != (written.st_size, written.st_mtime):
        log.warning("project sync: %s was saved over right after it landed; not recorded", rel)
        return False
    record(origin, rel, Row(server, Stamp(written.st_size, written.st_mtime), md5))
    return True


#: How much of a file a checksum or a copy holds in memory at once.
_BLOCK = 1 << 20


def part_of(dst: Path, server: Stamp) -> Path:
    """The temp a streamed download of *server*'s version of *dst* fills: one name per
    version, so a cut transfer of it resumes on the next pass and another version of the
    file never appends to it."""
    key = hashlib.sha1(f"{server.size}:{server.mtime!r}:{server.version}".encode()).hexdigest()
    return dst.with_name(f"{dst.name}.{key[:12]}.sync.part")


def fetch_part(
    tree: TreeIO, remote: PurePath, dst: Path, server: Stamp, rel: str
) -> tuple[Path, str]:
    """The server's *server* version of *rel* in its resumable temp next to *dst*, flushed
    to disk, and the MD5 of its bytes: ``(temp, md5)``.

    Streamed (``tree.get_file``), never held in memory whole, from what a cut transfer of
    the same version left (:func:`part_of`); a tree that cannot stream is read whole. A
    temp of another version of the file is removed first. A disk that fills up removes
    the temp again — what it holds would only keep the disk full — and raises (the pass
    ends: ``disk_full``)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    part = part_of(dst, server)
    for stale in dst.parent.glob(f"{dst.name}.*.sync.part"):
        if stale != part:
            with suppress(OSError):
                stale.unlink()
    have = part.stat().st_size if part.exists() else 0
    if have > server.size:
        part.unlink()
        have = 0
    try:
        if have < server.size:
            get = getattr(tree, "get_file", None)
            if get is not None:
                get(remote, part, start=have)
            else:
                part.write_bytes(tree.read_bytes(remote))
        size = part.stat().st_size
        if size != server.size:
            part.unlink()
            raise EvidenceError("corrupt", f"{rel}: the server sent {size} bytes of {server.size}")
        digest = hashlib.md5()
        with open(part, "r+b") as handle:
            for block in iter(lambda: handle.read(_BLOCK), b""):
                digest.update(block)
            os.fsync(handle.fileno())
    except OSError as exc:
        if disk_full(exc):
            with suppress(OSError):
                part.unlink()
        raise
    return part, digest.hexdigest()


#: What :func:`upload` and :func:`download` did with one file.
SENT = "sent"
ALREADY_THERE = "already there"
CONFLICT = "conflict"
#: A download that arrived while a test started running from the copy: not landed, its temp
#: kept for the next pass to land (``project_sync.pull``'s *hold*).
HELD = "held"


def download(
    tree: TreeIO,
    base: PurePath,
    working_copy: Path,
    origin: Origin,
    rel: str,
    writer: str,
    scanned: Stamp | None,
    hold: Callable[[], bool] | None = None,
) -> tuple[str, Conflict | None]:
    """Land one file, unless the working copy's file is no longer the one the plan saw, or a
    test started running from the copy while its bytes came (*hold*): :data:`HELD`, the
    resumable temp kept, nothing landed.

    The bytes are streamed into a resumable temp first (:func:`fetch_part`); *scanned* is
    the working copy's file's stamp in the scan the plan was made from (``None``: it was
    not there), and :func:`land_part` looks again right before the replace. A file Studio
    or the runner saved meanwhile is a CONFLICT (nothing lands, the row is left as it
    was), never overwritten. ``(SENT, None)``, or ``(CONFLICT, the conflict)``."""
    remote = base / rel
    server = server_stamp(tree, remote)
    dst = local_path(working_copy, rel)
    part, md5 = fetch_part(tree, remote, dst, server, rel)
    if hold is not None and hold():
        return HELD, None
    if land_part(working_copy, rel, part, server, origin, expect=scanned, md5=md5):
        return SENT, None
    now = local_now(working_copy, rel)
    kind = NEW_NEW if scanned is None else (DELETED_EDITED if now is None else EDITED_EDITED)
    log.warning(
        "project sync: %s changed in the working copy while the pass ran; nothing landed", rel
    )
    return CONFLICT, Conflict(rel, kind, now, server)


def upload(
    tree: TreeIO,
    base: PurePath,
    working_copy: Path,
    origin: Origin,
    rel: str,
    writer: str,
    seen: Stamp | None,
) -> tuple[str, Conflict | None]:
    """Send one file, unless the server's copy is no longer the one the plan saw.

    *seen* is that copy's stamp in the listing the plan was made from (``None``: it
    was not there). A push makes its plan from the manifest, so for a push *seen* is
    the version this working copy last synced. The copy is looked up again here —
    the parent's listing is asked again (the operation's cached one is what planned the
    pass, and a save another bench made since would not be in it) — and:

    * still *seen*: the file is sent (:data:`SENT`);
    * changed, holding these very bytes: nothing is sent and the row records both
      sides as they are (:data:`ALREADY_THERE`) — two benches saving the same edit
      is not a conflict;
    * changed to other bytes, or gone: a CONFLICT, with its kind. Nothing is sent and
      the row is left as it was, so the next pass meets the same question. Sending
      would overwrite another bench's save, or bring back a file somebody deleted,
      unseen.

    The sizes are compared first, so a conflict costs no download unless the two
    copies are the same length. The window between this look and the server's
    rename stays open; a transport with a conditional write could close it.
    """
    src = local_path(working_copy, rel)
    here = src.stat()
    remote = base / rel
    forget_listing(tree, remote.parent)
    now = server_now(tree, remote)
    if now != seen:
        if now is not None and now.size == here.st_size and tree.sha256(remote) == sha256_file(src):
            record(origin, rel, Row(now, Stamp(here.st_size, here.st_mtime)))
            return ALREADY_THERE, None
        log.warning(
            "project sync: %s changed here and %s on the server since the last sync; "
            "neither copy was touched",
            rel,
            "was deleted" if now is None else "changed",
        )
        if now is None:
            kind = EDITED_DELETED
        else:
            kind = NEW_NEW if seen is None else EDITED_EDITED
        return CONFLICT, Conflict(rel, kind, Stamp(here.st_size, here.st_mtime), now)
    send(tree, src, remote, rel, origin, writer)
    return SENT, None


def send(tree: TreeIO, src: Path, remote: PurePath, rel: str, origin: Origin, writer: str) -> None:
    """Put the working copy's file on the server and record the row both sides now give.

    **Only what this PC wrote is recorded as synced.** The server's stamp is read after the
    put, so another bench's save between the two would be recorded as ours, and both sides
    would then read "same" for good, each holding other bytes
    (docs/plans/company_storage_design.md §8, F2). Where the stamp can prove the bytes are
    ours (a share, which keeps the source's time) and it does not, the row is left as it was:
    the next pass meets the file changed on both sides and asks, or settles it when the bytes
    are the same."""
    here = src.stat()
    tree.put_file(src, remote, writer, verify=True)
    server = server_stamp(tree, remote)
    if server.size != here.st_size:
        raise EvidenceError("corrupt", f"{rel} arrived as {server.size} bytes of {here.st_size}")
    if not _ours(tree, server, here):
        log.warning("project sync: %s was saved over on the server right after it was sent", rel)
        return
    record(origin, rel, Row(server, Stamp(here.st_size, here.st_mtime)))


def _ours(tree: TreeIO, server: Stamp, here: os.stat_result) -> bool:
    """Whether the server's copy, as *server* says it, is the file just sent (*here*); True
    when the transport cannot tell (FTPS sets its own time)."""
    if getattr(tree, "keeps_mtime", False):
        return abs(server.mtime - here.st_mtime) < 0.001
    return True
