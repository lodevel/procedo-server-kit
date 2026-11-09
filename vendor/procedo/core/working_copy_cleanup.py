"""Only the open project's working copy is kept (docs/plans/project_working_copy_design.md,
"Only the open project's copy is kept", the owner's decision 11).

When another project has opened, every other working copy of a project on a share is removed —
unless it still holds something the share has not got, or somebody is using it. Reopening a
removed one is a first open: the share is checked out again. Only the folder under
``working_copies/`` goes; the per-PC state folder (the venv selection, the media cache, this PC's
trash, the record of what was synced) is keyed by ``project_id`` and stays.

It removes users' folders, so every step PROVES before it acts, and the first proof that fails
keeps the copy exactly as it is (:func:`remove_if_clean`, one copy at a time, on the sync worker):

1. **The folder** is the working copy its record describes, of a project on a share
   (``project_source.share_of``). A copy of a server project — a filesystem server's too — is
   never removed: its recent entry names the copy itself, and nothing could check it out again
   from there.
2. **The sync lock** is taken without waiting: another Bench window that syncs it keeps it.
3. **Nothing is waiting**, by the offline ``status``: no upload, no conflict, no "missing here"
   question, no pull owed, no removal the app noted (Studio may be making it right now).
4. **No live claim** (``working_copy_claims``): a Bench window that has it open, Studio, a run —
   looked at under the copy's removal lock, held from here to the end. A claimer writes its claim,
   waits for that lock, and only then looks for the folder, so one of the two sees the other.
5. **Set aside**: the folder is renamed to ``<name>.removing`` beside it. Windows refuses that
   while a file inside is open or a process works in it, so a holder nobody claimed still turns
   into a clean refusal that touched nothing.
6. **The claims again**: one written meanwhile renames the folder back, untouched.
7. **Leftovers** — what is in the folder and is not the project (a ``*.bak``, a name the sync
   refused, an editor's session file) — are moved to this PC's trash, byte for byte, by the
   sync's own whole-or-nothing move. What can be made again (build output, a transfer's temp, a
   lock, this machine's state) is not kept. One that cannot be moved puts the folder back.
8. **Delete** the set-aside folder (the store's ``rmtree``), and only once it is gone drop the
   record — only if it is still the one the proofs read: a half-removed copy is never left
   without one, and a copy made again meanwhile keeps its own.

A ``<name>.removing`` a crash left behind is deleted by the next sweep (:func:`sweep_removing`),
and its record dropped once it has gone. Qt-free; every call is disk I/O on this PC.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import (
    evidence_rename,
    paths,
    project_intents,
    project_local_state,
    project_origin,
    project_sync,
    project_sync_pass,
    working_copy_claims,
)
from .evidence_store import EvidenceError
from .project_never_travels import exclusion
from .project_origin import Row, Stamp
from .project_source import SyncLock, share_of
from .project_sync_plan import unsafe
from .project_sync_scan import is_link
from .project_sync_trash import grave_name, move_aside

log = logging.getLogger(__name__)

#: The suffix of a working copy set aside for removal (``SyncGateway.forget`` uses it too).
REMOVING = project_local_state.SET_ASIDE_SUFFIX

#: Why a copy was kept, as the footer and the log say it.
NOT_A_SHARE_COPY = "not a working copy of a project on a share"
SYNCED_ELSEWHERE = "another Bench window syncs it"
WAITING = "it holds changes the share has not got"
IN_USE = "a file in it is open, or a program works in it"
UNREADABLE = "it could not be checked"
REMOVAL_UNDER_WAY = "another removal of it is under way"

#: Build output and transfers: made again by whoever needs them.
_REMAKEABLE_DIRS = frozenset({".pytest_cache", ".mypy_cache"})
_REMAKEABLE_FILES = frozenset({".project_id.lock"})


@dataclass(frozen=True)
class Outcome:
    """What :func:`remove_if_clean` did with one copy."""

    copy: Path
    #: The copy is gone from its name. A set-aside folder that would not go whole is finished
    #: by the next :func:`sweep_removing`; nothing reads or syncs it meanwhile.
    removed: bool
    #: Why it was kept (``""`` when removed).
    why: str = ""
    #: The leftovers moved to this PC's trash, by relative path.
    kept_in_trash: tuple[str, ...] = ()
    #: Where they went (``""`` when nothing did).
    trash: str = ""
    #: The copy is kept because something waits to go up: a background push may settle it.
    waiting: bool = False
    #: Among what waits, a conflict: only somebody's answer settles it (``project_sync.resolve``).
    conflicted: bool = False


@dataclass
class _Proof:
    origin: object = None
    #: The copy's state folder, found while the copy has its name: after the rename it can no
    #: longer be found from it, and the claims and the record are there.
    state: Path | None = None
    lock: SyncLock | None = None
    #: The record as the proofs read it: dropped at the end only if it is still that one.
    record: bytes | None = None
    #: The offline status found a conflict.
    conflicted: bool = False
    moved: list[str] = field(default_factory=list)
    trash: str = ""


def candidates(keep: Path | str | None = None) -> list[Path]:
    """The working copies of share projects on this PC, every one but *keep* (the open one)."""
    root = project_local_state.working_copies_root()
    kept = project_origin.folder_key(keep) if keep is not None else None
    found = []
    for folder in sorted(root.iterdir()) if root.is_dir() else ():
        if folder.name.endswith(REMOVING) or not folder.is_dir() or is_link(folder):
            continue
        if kept is not None and project_origin.folder_key(folder) == kept:
            continue
        origin = project_origin.load(folder)
        if share_of(origin) is not None and project_origin.describes(origin, folder):
            found.append(folder)
    return found


def remove_if_clean(
    copy: Path | str,
    *,
    rename: Callable[[Path, Path], None] | None = None,
) -> Outcome:
    """Remove the working copy *copy* when every proof holds; keep it, untouched but for its
    leftovers, at the first that does not. *rename* is the set-aside's rename (a test makes it
    refuse, as Windows does with a file open)."""
    copy = Path(copy)
    proof = _Proof()
    try:
        why = _prove(copy, proof)
        if why:
            return Outcome(copy, False, why, waiting=why == WAITING, conflicted=proof.conflicted)
        # From the first look at the claims to the end, under the copy's removal lock: a claimer
        # waits for it before it looks for the folder (working_copy_claims.claim).
        try:
            with working_copy_claims.removing(proof.state) as held:
                if not held:
                    return Outcome(copy, False, REMOVAL_UNDER_WAY)
                return _remove(copy, proof, rename)
        except OSError:  # the claims could not be read: in use is never guessed away
            log.warning("working copy %s: its claims could not be read; kept", copy, exc_info=True)
            return _kept(copy, UNREADABLE, proof)
    finally:
        if proof.lock is not None:
            proof.lock.release()


def _remove(copy: Path, proof: _Proof, rename: Callable[[Path, Path], None] | None) -> Outcome:
    """Steps 3 and 5-8, under the removal lock."""
    working_copy_claims.prune_in(proof.state)  # nobody holds them: they count for nothing
    claimed = working_copy_claims.holder_in(proof.state)
    if claimed:
        return Outcome(copy, False, f"in use ({claimed})")
    record = proof.state / project_origin.RECORD_NAME
    try:
        proof.record = record.read_bytes()
        doomed = set_aside(copy, rename=rename)
    except OSError:
        log.info("working copy %s is in use; kept", copy, exc_info=True)
        return Outcome(copy, False, IN_USE)
    try:
        claimed = working_copy_claims.holder_in(proof.state)
    except OSError:
        claimed = "its claims could not be read"
    if claimed:  # claimed between the check and the rename: it stays, leftovers and all
        _put_back(doomed, copy, rename)
        return Outcome(copy, False, f"in use ({claimed})")
    why = _leftovers_to_trash(doomed, proof)
    if why:
        _put_back(doomed, copy, rename)
        return _kept(copy, why, proof)
    finish(doomed, copy, record, expected=proof.record)
    return Outcome(copy, True, "", tuple(proof.moved), proof.trash)


def _put_back(doomed: Path, copy: Path, rename: Callable[[Path, Path], None] | None) -> None:
    """The set-aside folder back under its name. One that cannot go back stays set aside: a
    sweep deletes it only once nobody claims the copy and no removal holds it."""
    try:
        (rename or os.replace)(doomed, copy)
    except OSError:
        log.warning("working copy %s could not be put back; it stays at %s", copy, doomed)


def _kept(copy: Path, why: str, proof: _Proof) -> Outcome:
    return Outcome(copy, False, why, tuple(proof.moved), proof.trash)


def _prove(copy: Path, proof: _Proof) -> str:
    """Steps 1-4: why the copy must stay, ``""`` when nothing says so."""
    origin = project_origin.load(copy)
    if (
        share_of(origin) is None
        or not project_origin.describes(origin, copy)
        or not paths.inside(copy, project_local_state.working_copies_root())
        or is_link(copy)
    ):
        return NOT_A_SHARE_COPY
    proof.origin = origin
    proof.state = project_local_state.existing_state_dir(copy)
    if proof.state is None:
        return UNREADABLE
    try:
        proof.lock = SyncLock.take(copy)
    except OSError:
        return UNREADABLE
    if proof.lock is None:
        return SYNCED_ELSEWHERE
    if copy.joinpath("config", evidence_rename.JOURNAL).exists():
        return WAITING  # an evidence rename to finish: its recovery belongs to this copy
    if project_intents.noted(copy):  # Studio is removing a test in it right now, or just did
        return WAITING
    try:
        counts = project_sync.status(copy)
    except (OSError, EvidenceError) as exc:
        log.info("working copy %s could not be checked: %s", copy, exc)
        return UNREADABLE
    proof.conflicted = bool(counts.conflicts)
    if (
        counts.busy
        or counts.uploads
        or counts.conflicts
        or counts.remote_delete_paths
        or counts.deletes_remote
        or counts.needs_pull
    ):
        return WAITING
    return ""


#: The temps the sync itself writes beside a file: ``<name>.<writer>.part`` (a landing, the
#: writer ``sync.<pid>.<8 hex>``) and ``<name>.<12 hex>.sync.part`` (a resumable download).
_SYNC_TEMP = re.compile(r".+\.(sync\.\d+\.[0-9a-f]{8}|[0-9a-f]{12}\.sync)\.part")


def _remakeable(rel: str, *, is_dir: bool) -> bool:
    """Only the exact shapes the app and its tools make themselves, which nobody needs back: a
    ``__pycache__`` (or a test tool's cache) folder, the sync's own temps, the id lock and this
    machine's state at the top of the copy. Anything else that is not the project is kept."""
    parts = rel.split("/")
    name = parts[-1]
    if is_dir:
        return name in _REMAKEABLE_DIRS or (
            len(parts) == 1 and project_local_state.is_machine_state(name)
        )
    if len(parts) == 1 and (
        name in _REMAKEABLE_FILES or project_local_state.is_machine_state(name)
    ):
        return True
    if len(parts) > 1 and parts[-2] == "__pycache__" and name.endswith(".pyc"):
        return True
    return bool(_SYNC_TEMP.fullmatch(name))


def _walk_failed(exc: OSError) -> None:
    raise exc  # a folder that cannot be listed is a folder whose files would be lost unseen


def _is_synced(doomed: Path, rel: str, rows: dict[str, Row]) -> bool:
    """Whether the file *rel* is, provably, the copy the share already has: its row is one the
    sync still carries, and the file is exactly as that row recorded it when it last synced."""
    row = rows.get(rel.casefold())
    if row is None:
        return False
    try:
        here = doomed.joinpath(*rel.split("/")).stat()
    except OSError:
        return False
    return Stamp(here.st_size, here.st_mtime) == row.local


def _leftovers_to_trash(doomed: Path, proof: _Proof) -> str:
    """Step 7, on the folder set aside (nothing can write there any more): every file that is not
    provably the share's copy (:func:`_is_synced`) and is not something the app makes again goes
    to this PC's trash, whole or not at all — a file saved after the status was read, a row the
    sync no longer carries, a ``*.bak``, a name the sync refused. A link is moved as the link,
    never followed. ``""`` once they have; else why the copy goes back."""
    rows = {
        rel.casefold(): row
        for rel, row in proof.origin.files.items()
        if not (exclusion(rel) or unsafe(rel))
    }
    leftovers: list[str] = []
    try:
        for dirpath, dirnames, filenames in os.walk(doomed, onerror=_walk_failed):
            here = Path(dirpath).relative_to(doomed).as_posix()
            prefix = "" if here == "." else f"{here}/"
            walked = []
            for name in dirnames:
                rel = f"{prefix}{name}"
                if is_link(Path(dirpath) / name):
                    leftovers.append(rel)  # moved as it is: what it points at is not touched
                elif not _remakeable(rel, is_dir=True):
                    walked.append(name)
            dirnames[:] = walked
            for name in filenames:
                rel = f"{prefix}{name}"
                if is_link(Path(dirpath) / name) or not (
                    _is_synced(doomed, rel, rows) or _remakeable(rel, is_dir=False)
                ):
                    leftovers.append(rel)
    except OSError:
        log.warning("working copy %s could not be read whole; kept", doomed, exc_info=True)
        return UNREADABLE
    if not leftovers:
        return ""
    trash = project_local_state.local_trash_dir(doomed)
    if trash is None:
        return UNREADABLE
    grave = trash / grave_name(f"removed.{project_sync_pass.tag()}")  # one per attempt
    for rel in leftovers:
        target = grave.joinpath(*rel.split("/"))
        log.info("working copy %s: %s is not on the share; kept in this PC's trash", doomed, rel)
        try:
            if target.exists() or target.is_symlink():
                raise FileExistsError(str(target))  # never over what the trash already keeps
            move_aside(doomed.joinpath(*rel.split("/")), target)
        except (OSError, EvidenceError):
            log.warning("working copy %s: %s could not be kept; the copy stays", doomed, rel)
            return IN_USE
        proof.moved.append(rel)
        proof.trash = str(grave)
    return ""


def set_aside(copy: Path, *, rename: Callable[[Path, Path], None] | None = None) -> Path:
    """Step 6: rename *copy* to ``<name>.removing`` beside it (an earlier attempt's is cleared
    first), and return where it went. Raises when the rename is refused, having touched nothing:
    Windows refuses it while a file inside is open or a process works in it — a whole-or-nothing
    removal, where ``rmtree`` over a busy folder deletes what it can and leaves half a project."""
    doomed = copy.with_name(copy.name + REMOVING)
    if doomed.exists():
        paths.rmtree(doomed, ignore_errors=True)
    (rename or os.replace)(copy, doomed)
    return doomed


def finish(doomed: Path, copy: Path, record: Path | None, *, expected: bytes | None = None) -> bool:
    """Step 8: delete the set-aside *doomed* (the store's ``rmtree``, which clears a read-only
    bit and retries), then — only once it has gone — the *record* of *copy*. Whether it went; one
    that did not is finished by the next :func:`sweep_removing`."""
    paths.rmtree(doomed, ignore_errors=True)
    if doomed.exists():
        log.warning("working copy %s was not removed whole; the next sweep finishes it", copy)
        return False
    drop_record(record, copy, expected=expected)
    return True


def drop_record(record: Path | None, copy: Path, *, expected: bytes | None = None) -> None:
    """The record, when it still names *copy*, *copy* is not there (a copy made again since
    has a record of its own, under the same id), and — given *expected* — it is still the very
    record the removal proved (a first open writing a new one meanwhile is left alone)."""
    if record is None or copy.exists():
        return
    if expected is not None:
        try:
            if record.read_bytes() != expected:
                return
        except OSError:
            return
    origin = project_origin.read_record(record)
    if origin is not None and origin.working_copy == project_origin.folder_key(copy):
        record.unlink(missing_ok=True)


def sweep_removing() -> list[Path]:
    """Delete every ``<name>.removing`` a crash or an unfinished removal left (they were set aside
    after every proof held), and drop each one's record once it has gone — but never one a
    removal is still working on (its removal lock is held: it may be about to put it back), nor
    one whose project somebody claims. The ones still there afterwards are returned; the next
    sweep tries again."""
    root = project_local_state.working_copies_root()
    left = []
    for doomed in sorted(root.iterdir()) if root.is_dir() else ():
        if not doomed.name.endswith(REMOVING) or is_link(doomed) or not doomed.is_dir():
            continue
        copy = doomed.with_name(doomed.name[: -len(REMOVING)])
        state = project_local_state.existing_state_dir(doomed)  # by its own id
        record = state / project_origin.RECORD_NAME if state is not None else None
        origin = project_origin.read_record(record) if record is not None else None
        if (
            not project_local_state.marker_of(doomed)
            or origin is None
            or origin.working_copy != project_origin.folder_key(copy)
        ):
            # Not a working copy this app set aside (a folder somebody named so, or one whose
            # removal went too far to say): never deleted on a guess.
            left.append(doomed)
            continue
        try:
            with working_copy_claims.removing(state) as held:
                if not held or working_copy_claims.holder_in(state):
                    left.append(doomed)
                    continue
                # The same proof as any removal: what is not provably on the share is kept first.
                if _leftovers_to_trash(doomed, _Proof(origin=origin)) or not finish(
                    doomed, copy, record
                ):
                    left.append(doomed)
        except OSError:
            left.append(doomed)
    return left
