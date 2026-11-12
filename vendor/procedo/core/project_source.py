"""Where a project lives, decided when it is opened (docs/plans/project_working_copy_design.md,
"Local or remote: deciding on open"): a folder on this PC's own disk is opened as it is; a folder
on a share is opened through this PC's working copy of it, which the sync keeps in step with the
share. Qt-free; every call that touches the share runs on the open's worker.

* :func:`source_kind` — ``local`` or ``remote``, from the path RESOLVED first: a mapped drive,
  a junction, a symlink and a ``subst`` letter onto a share all classify as the share they are.
* :func:`copy_of` / :func:`source_of` — the working copy of a share folder on this PC, and back;
  the recent list and the window keep the share's path and name (:func:`shown_path`).
* :func:`route` — which folder an open reads: the project itself, or its working copy (made
  first when there is none yet).
* :class:`SyncLock` — the one process that syncs a working copy, Bench or Studio, a share's copy
  or a server's. Another window on the same copy reads and writes it but does not sync it; the
  lock file names the app that holds it (:class:`Holder`), so that window can say which.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

from . import evidence_store_fs, paths, project_identity, project_local_state, project_origin
from .project_transport import FILESYSTEM

log = logging.getLogger(__name__)

LOCAL = "local"
REMOTE = "remote"

#: The lock file a working copy's syncing app holds, in the copy's state folder.
SYNC_LOCK = "sync.lock"
#: How much of the lock file its holder's record may take (the lock is on a byte past it).
_HOLDER_BYTES = 4096


def resolve(path: Path | str) -> Path:
    """*path* as the folder it really is: links, junctions and ``subst`` letters followed, a
    mapped drive turned into its ``\\\\server\\share`` path (``os.path.realpath``). The
    ``\\\\?\\`` long-path prefix is taken off first, so a local ``\\\\?\\C:\\…`` does not read as
    a UNC path; ``\\\\?\\UNC\\server\\…`` stays the share it names."""
    text = paths.short_text(str(path))
    if os.name != "nt" and text.startswith(("\\\\", "//")):
        return Path(text)  # a share path named on a machine that cannot resolve it
    return Path(os.path.realpath(text))


def source_kind(path: Path | str) -> str:
    """:data:`REMOTE` when the resolved *path* is on a share (UNC, a network drive, a drive
    Windows cannot classify), :data:`LOCAL` otherwise — a fixed or removable disk, a OneDrive
    folder (its own client syncs it), a virtual disk. No override: the resolved path decides."""
    return REMOTE if evidence_store_fs.is_remote_root(resolve(path)) else LOCAL


def _same_folder(a: str, b: str) -> bool:
    return project_origin.folder_key(a) == project_origin.folder_key(b)


def _spelled(path: str) -> str:
    """*path* as compared by spelling alone — never resolved: this is asked on the GUI thread
    (the window title, the recent list), and resolving a share that went away takes 20 s."""
    return os.path.normcase(os.path.normpath(path))


def share_of(origin: project_origin.Origin | None) -> Path | None:
    """The share folder a working copy's record says it is the copy of, ``None`` for any other
    record. A share opened through its copy is checked out with the project folder itself as the
    filesystem source's root (``root`` and ``base`` are one spelling, both written by that
    checkout); a project checked out with
    *Open Project from Server* on a filesystem server keeps the server's root and the project's
    folder apart, is opened through the copy itself, and is not a share's copy."""
    if origin is None or origin.backend != FILESYSTEM:
        return None
    root = origin.params.get("root")
    if not isinstance(root, str) or not root or _spelled(root) != _spelled(origin.base):
        return None
    return Path(root)


def copy_of(source: Path | str) -> Path | None:
    """This PC's working copy of the share folder *source*, ``None`` when it has none yet. Found
    among the working copies by the folder their record names as their source, so nothing on
    the share is read."""
    wanted = str(resolve(source))
    root = project_local_state.working_copies_root()
    for folder in sorted(root.iterdir()) if root.is_dir() else ():
        origin = project_origin.load(folder) if folder.is_dir() else None
        share = share_of(origin)
        if (
            share is not None
            and project_origin.describes(origin, folder)
            and _same_folder(str(share), wanted)
        ):
            return folder
    return None


def source_of(working_copy: Path | str) -> Path | None:
    """The share folder *working_copy* is the copy of, ``None`` for any other folder (a
    project on this PC, or a copy of a server project). A folder outside the working copies'
    own folder is not one, and nothing in it is read."""
    if not paths.inside(working_copy, project_local_state.working_copies_root()):
        return None
    origin = project_origin.load(Path(working_copy))
    if origin is None or not project_origin.describes(origin, working_copy):
        return None
    return share_of(origin)


def shown_path(path: Path | str) -> Path:
    """The path a person knows the project by: the share folder for a copy of one (the recent
    list, the window title), else *path* itself."""
    return source_of(path) or Path(path)


@dataclass(frozen=True)
class Route:
    """Which folder an open reads (:func:`route`)."""

    #: The folder read and shown: the project itself, or this PC's working copy of it.
    path: Path
    #: The share folder, resolved, for a project on a share (``None`` for one on this PC).
    share: Path | None = None
    #: No working copy yet: the share is checked out first, and :attr:`path` is the share.
    first_open: bool = False


def route(path: Path | str) -> Route:
    """Where an open of *path* reads from. A folder on this PC is read where it is, as it
    always was. A folder on a share is read from this PC's working copy of it; with no copy
    yet, the share is checked out first (*first_open*). Runs on the open's worker: it may
    resolve a path on a share."""
    if source_kind(path) == LOCAL:
        return Route(Path(path))
    share = resolve(path)
    copy = copy_of(share)
    if copy is None:
        return Route(share, share, first_open=True)
    return Route(copy, share)


@dataclass(frozen=True)
class Holder:
    """Who holds a working copy's sync lock: the app's name (``Procedo Bench``, ``Procedo
    Studio``; ``""`` when the taker did not say), its process and when it took the lock."""

    app: str
    pid: int
    since: str

    @property
    def session(self) -> str:
        """This holding, told apart from every other one of the same copy (a pass stamp names
        the holding it was made in: ``project_sync_stamp``)."""
        return f"{self.pid}@{self.since}"


class SyncLock:
    """The operating system's lock on ``<state folder>/sync.lock``, held by the one process that
    syncs a working copy (:func:`take`). Released by :meth:`release`, and by the operating
    system when the process ends, however it ends. The holder's record (:class:`Holder`) is
    written at the start of the file, before the locked byte, so another process reads it while
    the lock is held (:meth:`holder_of`)."""

    def __init__(self, fd: int | None, path: Path | None, holder: Holder | None = None) -> None:
        self._fd = fd
        self.path = path
        self.holder = holder

    @classmethod
    def take(cls, working_copy: Path | str, app: str = "") -> SyncLock | None:
        """The lock, or ``None`` when another process holds it (that one syncs the copy).
        A copy with no state folder, or a filesystem that cannot lock, is not refused: the
        caller gets a lock that holds nothing and syncs, as before there was a lock. *app* is
        the name the record gives the holder. Local disk only; a window runs it on its sync
        worker, never on the GUI thread."""
        holder = Holder(app, os.getpid(), time.strftime("%Y-%m-%dT%H:%M:%S"))
        state = project_local_state.state_dir(working_copy)
        if state is None:
            return cls(None, None, holder)
        path = state / SYNC_LOCK
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
        if not project_identity.lock_file(fd, take=True):
            os.close(fd)
            return None
        record = json.dumps({"app": app, "pid": holder.pid, "since": holder.since}).encode()
        try:
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, record[:_HOLDER_BYTES])
        except OSError:  # the lock holds without its record: the other window names no app
            log.warning("could not write the holder of %s", path, exc_info=True)
        return cls(fd, path, holder)

    @staticmethod
    def holder_of(working_copy: Path | str) -> Holder | None:
        """The record of whoever took the copy's lock last (``None``: none, or unreadable).
        Asked by a window that could not take it; never makes a state folder."""
        state = project_local_state.existing_state_dir(working_copy)
        if state is None:
            return None
        try:
            with open(state / SYNC_LOCK, "rb") as handle:
                data = json.loads(handle.read(_HOLDER_BYTES).rstrip(b"\x00") or b"{}")
            return Holder(str(data["app"]), int(data["pid"]), str(data["since"]))
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            project_identity.lock_file(self._fd, take=False)
        finally:
            os.close(self._fd)
            self._fd = None
