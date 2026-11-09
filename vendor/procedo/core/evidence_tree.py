"""The byte-store seam under the shared evidence layout.

The shared layout's protocol (immutable generations, the forward-only pointer, the
evidence folder's boards, artifacts with sidecars, the outbox push) is written once,
over a :class:`TreeIO`: ``evidence_store_fs_shared.FsTree`` over a filesystem root
today, an FTPS connection in slice 5a-2. Protocol code builds paths with ``/``,
``.name``, ``.parent`` and ``.relative_to`` on the store's root object and does every
read and write through the tree — never a filesystem method on a path
(``tests/test_evidence_tree_seam.py``). The helpers below reproduce, over any tree,
what the filesystem code did with ``Path``: same results, same order, same errors.
Qt-free, stdlib only.

**The contract** (``docs/plans/evidence_store_design.md``, slice 5) — what every tree
must honour, and what the protocol may assume:

* **Every failure is an ``OSError``** — ``FileNotFoundError`` for an absent entry,
  ``PermissionError`` for a refused one, ``ConnectionError``/``TimeoutError`` for the
  transport. A tree never lets a transport-specific exception escape
  (``ftplib.error_perm`` and friends are translated), because the protocol catches
  ``OSError`` and classifies it with :func:`evidence_store_fs.os_error`; anything else
  surfaces as ``internal``. **The one carve-out is cancellation**, which raises
  ``EvidenceError("cancelled")``: a cancelled transfer is not a transport failure, the
  app already has that kind, and classifying it as one would offer the user a Retry for
  something they just stopped. Nothing else a tree raises may leave as anything but an
  ``OSError``.
* **A refused listing raises; it never answers "empty".** ``list_names`` returns
  ``None`` only when the path is absent or is not a folder, and callers read ``None``
  as empty — a denied listing answered with ``None`` would show a populated store as
  empty instead of reporting the refusal.
* **A tree declares its path flavour** in :attr:`TreeIO.flavour`: ordering follows from
  the path objects the tree hands out (``PureWindowsPath`` sorts case-insensitively), and
  :func:`safe_relative` reads it, since what counts as an absolute name is flavour-specific
  (``C:\\x`` is anchored to a Windows tree and a stray file name to a POSIX one). A NAME
  (a board's part number, revision and serial, a test) is matched the same way on every
  flavour (``data_format.md`` §2, decision 21): by NFC then case folding, the first
  spelling stored winning (:func:`resolve_child`), so ``sn1`` pushed onto a Linux folder
  holding ``SN1/`` lands in ``SN1/`` as it does on Windows.
* **``write_atomic`` is all-or-nothing to a reader.** Where the transport cannot replace
  an existing name atomically, a short delete-then-rename gap is permitted for
  ``current.json`` only (readers fall back to the newest complete generation while
  its writer holds the pointer lock), never for ``manifest.json``, which is the
  visibility switch.
* **``write_json`` writes the tree's own newline convention** (the filesystem tree keeps
  text mode, so CRLF on Windows, matching what is already on disk); the layout never
  compares a JSON record byte-for-byte across trees.
* **``append_bytes`` adds; it never rewrites.** The listing index
  (:mod:`evidence_index`) is the one store record that GROWS, and appending is what
  lets two writers add to a file without a read-modify-write that loses the other's
  lines. A tree offers no atomicity beyond the single call: a transfer the transport
  cuts short may leave a partial tail, which is why a shard is one self-describing
  JSON object per line — the reader skips what does not parse and the missing entry
  is found again by gap detection. **A tree need not create missing parents** (the
  filesystem one does not; the FTPS one does, because it creates them for every
  upload anyway), so a caller creates the folder first — ``evidence_index`` calls
  ``_mkdir_safe`` once per operation before it appends.
* **``stat`` raises where ``exists`` answers cheaply**; a tree may serve
  ``exists``/``is_dir``/``is_file``/``stat`` from one cached parent listing within a
  single store operation, never across operations.
* **Times are seconds since the Unix epoch, UTC**, as ``time.time()`` returns them;
  one-second resolution is enough (only the 24 h sweep and the clock-skew probe read
  them).
* **``identity`` is a stable, printable key** for the store's location that survives a
  restart and never contains a secret (host, port and base path — never the password):
  it routes durable outbox entries and keys the materialization cache.
* **``mkdir`` creates exactly one level and never the tree's own root**, so a mistyped
  base path is reported rather than created.
* **Cancellation is scoped, not per call.** No primitive takes a context; the store
  enters a cancellation scope for the operation and a tree honours it between transfer
  blocks, so a slow FTPS transfer stops without adding a parameter to every primitive
  (documented here; slice 5a-2 builds it).
"""

from __future__ import annotations

import json
import logging
import threading
import time
import unicodedata
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from io import BytesIO, TextIOWrapper
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import Any, Protocol

from .evidence_lock_holder import EvidenceLocked, holder_record
from .evidence_store import EvidenceError, OpContext, guard_component, safe_component
from .evidence_store_fs import os_error
from .process_alive import pid_alive

log = logging.getLogger(__name__)

OWNER_RECORD = "owner.json"  # a lock's holder (``evidence_lock_holder.holder_record``)
#: A healthy hand-off of a lock takes milliseconds, so a writer waits this long before
#: it suspects the holder of being gone at all: inside that grace it never reads the
#: owner record, which keeps the normal contended path free of extra store reads AND
#: out of the holder's way (on Windows, a reader's open handle on ``owner.json`` makes
#: the holder's delete of it fail with a sharing violation).
_RECLAIM_GRACE_S = 0.5
#: How often it re-reads the record after that. The probe is a store read, so it is
#: not made on every poll of the wait loop.
_RECLAIM_PROBE_S = 5.0
_RELEASE_ATTEMPTS = 5
_RELEASE_PAUSE_S = 0.02
#: The share of an operation's budget a lock wait may use. The service's stall watchdog
#: (``evidence_service._on_stall``) fails a request silent for ``ctx.timeout_s`` with a
#: generic "did not answer" and drops its late answer, so a lock wait as long as the whole
#: budget lost that race and the user never learnt WHO holds the lock.
_LOCK_WAIT_SHARE = 0.8
#: After this long on another writer's lock, the operation's context says who holds it
#: (:attr:`OpContext.lock_held`): a healthy hand-off is over well before.
HELD_NOTICE_S = 2.0


def lock_wait_s(ctx: OpContext | None) -> float:
    """How long an operation run with ``ctx`` waits for a store-side lock: long enough for
    any healthy hand-off, short enough that its timeout naming the holder
    (:class:`evidence_lock_holder.EvidenceLocked`) reaches the user before the stall budget."""
    return ctx.timeout_s * _LOCK_WAIT_SHARE if ctx is not None else 30.0


@dataclass
class OperationScope:
    """One store operation on a tree: its cancel flag and every folder listing it read.

    A listing is served from here for the rest of the operation and never across
    operations; a folder the operation writes to is forgotten, so it is re-listed and
    never served stale. The transports extend it with what else they hold for the
    operation (FTPS its connection, a share whether its volume answered)."""

    cancel: threading.Event | None = None
    listings: dict[PurePath, Any] = field(default_factory=dict)

    def forget(self, folder: PurePath) -> None:
        """A folder this operation just wrote to is re-listed, never served stale."""
        self.listings.pop(folder, None)

    def cancelled(self) -> bool:
        return self.cancel is not None and self.cancel.is_set()


class ScopeLocal(threading.local):
    """The operation scope open on THIS thread, if any."""

    scope: Any = None


@contextmanager
def enter_scope(
    local: ScopeLocal, scope: OperationScope, closed: Callable[[Any], None] | None = None
) -> Iterator[Any]:
    """Open ``scope`` on this thread for one operation — or join the one already open,
    so a store method may enter it without knowing whether its caller already did.
    ``closed(scope)`` runs when the operation that OPENED it ends (FTPS: release its
    connection); a joining one closes nothing."""
    if local.scope is not None:
        yield local.scope
        return
    local.scope = scope
    try:
        yield scope
    finally:
        local.scope = None
        if closed is not None:
            closed(scope)


def forget_listing(io: Any, folder: PurePath) -> None:
    """The open operation of *io* on this thread re-lists *folder* at its next look: what a
    caller needs before a check that must see the server as it is NOW, not as the listing
    that planned the operation saw it. Nothing without an open operation."""
    scope = getattr(getattr(io, "_local", None), "scope", None)
    if scope is None:  # the web tree keeps its operation on its wire
        scope = getattr(getattr(io, "wire", None), "scope", None)
    if scope is not None:
        scope.forget(folder)


def tree_operation(io: Any, cancel: threading.Event | None = None) -> Any:
    """The tree's operation scope (:meth:`TreeIO.operation`) when it has one, else
    nothing: what every caller enters to make a run of calls ONE operation."""
    operation = getattr(io, "operation", None)
    return operation(cancel) if operation is not None else nullcontext()


class ListedTree:
    """The entry predicates of a server tree, answered from ONE parent listing.

    A backend provides ``_entries(folder)`` — ``{name: (kind, size, mtime, ...)}`` of
    one folder, cached for the operation, ``None`` when it is absent or not a folder —
    and ``_fact(path)``, one entry's tuple from its parent's listing (``None`` when it
    is not there)."""

    _entries: Callable[[PurePath], dict[str, tuple] | None]
    _fact: Callable[[PurePath], tuple | None]

    def list_names(self, folder: PurePath) -> list[str] | None:
        found = self._entries(folder)
        return None if found is None else list(found)

    def is_dir(self, path: PurePath) -> bool:
        fact = self._fact(path)
        return fact is not None and fact[0] == "dir"

    def is_file(self, path: PurePath) -> bool:
        fact = self._fact(path)
        return fact is not None and fact[0] == "file"

    def exists(self, path: PurePath) -> bool:
        return self._fact(path) is not None


class TreeIO(Protocol):
    """Byte access below one store root; paths are the root's own type."""

    identity: str  # routes outbox entries to their root (a filesystem tree: ``str(root)``)
    flavour: str  # "windows" | "posix": how names are matched and what counts as absolute

    def latency_class(self) -> str:
        """``"local"`` (a read answers in a blink) or ``"remote"``; neither runs on the GUI
        thread (``evidence_service``), this says how soon a view shows it is loading."""

    def operation(self, cancel: threading.Event | None = None) -> Any:
        """Context manager: the :class:`OperationScope` of ONE store operation on this
        thread (re-entrant — a nested one joins the open scope)."""

    def list_names(self, folder: PurePath) -> list[str] | None:
        """Entry names in listing order; ``None`` when ``folder`` is not a folder."""

    def is_dir(self, path: PurePath) -> bool: ...

    def is_file(self, path: PurePath) -> bool: ...

    def exists(self, path: PurePath) -> bool: ...

    def stat(self, path: PurePath) -> tuple[int, float]:
        """``(size, mtime)``, the time in epoch seconds; raises when the entry cannot
        be stat-ed (use :meth:`exists` to ask cheaply whether it is there)."""

    def newest_mtime(self, folder: PurePath) -> float:
        """Activity signal of a staged folder: the newest file's mtime below it in
        epoch seconds (a folder's own mtime never moves on a rewrite), else the
        folder's."""

    def read_bytes(self, path: PurePath) -> bytes: ...

    def get_file(self, path: PurePath, local: Path, *, start: int = 0) -> None:
        """The file's bytes from *start* on, appended to the LOCAL file *local* (written fresh
        from 0) block by block: a large file is never held in memory, and a cut transfer
        resumes from what *local* already holds (the attachment storage streams through it)."""

    def write_atomic(self, path: PurePath, data: bytes, tag: str) -> None:
        """Temp ``<name>.<tag>.part``, durable, then replace ``path``."""

    def append_bytes(self, path: PurePath, data: bytes) -> None:
        """Add ``data`` at the END of ``path``, creating it when absent; the bytes
        already there are never rewritten (see the append rule above)."""

    def write_json(self, path: PurePath, data: dict, tag: str) -> None:
        """:meth:`write_atomic` of ``data`` as the store's JSON records are written."""

    def put_file(self, src: Path, dst: PurePath, tag: str, *, verify: bool) -> None:
        """Upload the LOCAL file ``src`` atomically; ``verify``: the temp copy must be
        byte-identical to ``src`` before it replaces anything (else ``corrupt``)."""

    def mkdir(self, folder: PurePath) -> None:
        """One level, below the root (never the root itself); an existing folder is fine."""

    def mkdir_exclusive(self, folder: PurePath) -> bool:
        """Atomically create ``folder``; False only when it is already a directory.

        A successful claim invalidates the parent's cached listing, so protected
        reads see changes made by the previous lock holder.
        """

    def release_lock(self, folder: PurePath) -> None:
        """Remove a claimed lock and its owner record, even after cancellation."""

    def rename(self, src: PurePath, dst: PurePath) -> None: ...

    def unlink(self, path: PurePath, *, missing_ok: bool = False) -> None: ...

    def rmtree(self, folder: PurePath) -> None: ...

    def sha256(self, path: PurePath) -> str: ...


def _owner_of(io: TreeIO, folder: PurePath) -> dict:
    """A lock's owner record, or ``{}`` when there is none this reader can get at."""
    try:
        return read_json(io, folder / OWNER_RECORD)
    except (OSError, EvidenceError):
        return {}


def _taken_over(io: TreeIO, folder: PurePath, nonce: str) -> str:
    """Why the lock in *folder* is no longer the one this holder claimed with *nonce* (``""``:
    it still is, or nobody can tell). Its owner record names another holder: a release or an
    "abandoned" mark from this one would remove or betray THEIR lock (a release whose request
    reached the store but whose answer was lost, then a new holder, then this one's retry). A
    record that cannot be read, or names no nonce, is released as before rather than leaked."""
    try:
        record = read_json(io, folder / OWNER_RECORD)
    except (OSError, EvidenceError):
        return ""
    held = record.get("nonce")
    if held is not None and held != nonce:
        return f"it is held by {record.get('owner', 'another writer')} now"
    return ""


def _abandoned(record: dict, tag: str) -> bool:
    """Whether a lock is held by NOBODY — never whether it is merely old.

    ``owner`` is ``<machine id>.<pid>``. A lock THIS machine took whose process no
    longer exists has no holder, and the operating system is the authority on that;
    a holder whose release failed says so itself (``abandoned``). Another machine's
    lock is never touched: we cannot ask that machine about its processes, and a
    paused writer there could still resume — which is exactly what "never steal by
    age" exists to protect.
    """
    if record.get("abandoned"):
        return True
    machine, _, pid = str(record.get("owner", "")).rpartition(".")
    if not machine or machine != tag.rpartition(".")[0] or not pid.isdigit():
        return False
    return not pid_alive(int(pid))


def _reclaim(io: TreeIO, folder: PurePath, tag: str) -> bool:
    """Move an abandoned lock out of the way so the next claim can succeed.

    Reclaiming is NOT claiming. The rename only clears the way; the lock is open to
    every waiter the moment it lands, and exclusion comes from :meth:`TreeIO.mkdir_exclusive`
    alone — the reclaimer re-enters the same wait loop and may well lose the next claim
    to somebody else. Two writers reclaiming at the same moment are harmless for the
    same reason: the loser's rename fails, it retries, and whoever creates the folder
    first holds it.
    """
    record = _owner_of(io, folder)
    if not _abandoned(record, tag):
        return False
    aside = folder.with_name(f"{folder.name}.{tag}.{time.monotonic_ns():x}.abandoned")
    try:
        io.rename(folder, aside)
    except OSError:
        return False  # another reclaimer won, or the holder released: the loop retries
    log.warning("Reclaimed abandoned evidence lock %s (owner %r)", folder, record.get("owner"))
    try:
        io.rmtree(aside)
    except OSError:
        log.warning("Could not remove the reclaimed lock %s", aside, exc_info=True)
    return True


def _release(io: TreeIO, folder: PurePath) -> None:
    """Release a lock, retrying a transient sharing violation.

    A release that fails is how a lock leaks, and on Windows one can fail for a reason
    that is already over: a waiting writer reading ``owner.json`` holds a handle that
    makes the holder's delete of that file fail with ``WinError 32``. The outbox
    retries its entry files for the same reason.
    """
    for attempt in range(_RELEASE_ATTEMPTS):
        try:
            io.release_lock(folder)
            return
        except PermissionError:
            if attempt == _RELEASE_ATTEMPTS - 1:
                raise
            time.sleep(_RELEASE_PAUSE_S * (attempt + 1))


def _mark_abandoned(
    io: TreeIO, folder: PurePath, record: dict, tag: str, exc: BaseException
) -> None:
    """Record that a lock whose release failed has no holder any more.

    A release that failed while the body had already failed used to be one warning
    line and a lock nobody could ever take again — one transient share or FTPS error
    during a pointer write, and every later write of that test times out forever. Say
    so loudly, and write the fact into the lock, so the next writer reclaims it.
    """
    why = _taken_over(io, folder, record["nonce"])
    if why:
        log.warning("Evidence lock %s was not marked abandoned: %s", folder, why)
        return
    log.error("Could not release evidence lock %s (%s) — marking it abandoned", folder, exc)
    try:
        io.write_json(folder / OWNER_RECORD, {**record, "abandoned": True}, tag)
    except (OSError, EvidenceError):
        log.error(
            "Could not mark evidence lock %s abandoned; it must be removed by hand",
            folder,
            exc_info=True,
        )


def _timed_out(folder: PurePath, record: dict) -> EvidenceError:
    """The timeout of a lock wait: :class:`EvidenceLocked` when the owner record names a
    holder (the user is told who, and may break it), else the plain ``timeout``."""
    message = (
        f"Timed out waiting for evidence lock {folder}. Its owner.json says "
        f"{record or 'nothing'}. Another writer may still be "
        "active. If it has stopped, inspect owner.json and remove this lock folder "
        "before retrying. Do not remove a live writer's lock."
    )
    if record.get("owner"):
        return EvidenceLocked(message, folder, record)
    return EvidenceError("timeout", message)


@contextmanager
def tree_lock(
    io: TreeIO,
    folder: PurePath,
    tag: str,
    *,
    timeout_s: float = 30.0,
    cancel: threading.Event | None = None,
    ctx: OpContext | None = None,
) -> Iterator[None]:
    """Serialize writers through an exclusive directory on the store itself.

    The parent must exist. A lock is never expired by AGE — a paused client could
    still resume writing — but a lock with no holder is not a lock: one this machine
    took whose process has gone, and one whose holder could not release it, are
    reclaimed (:func:`_abandoned`). Its owner record carries the writer tag (machine
    id and process id for store operations), the station, the user and the acquisition
    time, and the timeout quotes it (:class:`EvidenceLocked`), so a human knows which
    bench to go and check — and may break a lock whose PC is gone
    (:mod:`evidence_lock_break`).

    With *ctx*, a wait that lasts :data:`HELD_NOTICE_S` puts the holder's record in
    ``ctx.lock_held`` (the busy dialog then says who it waits for), and
    ``ctx.stop_lock_wait`` ends the wait at once as its timeout would.
    """
    stopped = cancel if cancel is not None else threading.Event()
    deadline = time.monotonic() + timeout_s
    notice = time.monotonic() + HELD_NOTICE_S if ctx is not None else None
    # The grace never outlives the wait: a caller that allows less than it (a fast
    # GUI-side timeout) would otherwise put the first probe PAST its own deadline and
    # never reclaim an abandoned lock at all — not even one marked ``abandoned``.
    next_probe = time.monotonic() + min(_RECLAIM_GRACE_S, timeout_s / 2)
    while True:
        if stopped.is_set():
            raise EvidenceError("cancelled", "the evidence operation was cancelled")
        if io.mkdir_exclusive(folder):
            break
        now = time.monotonic()
        if now >= next_probe:  # reading the owner record is a store read: not every poll
            next_probe = now + _RECLAIM_PROBE_S
            if _reclaim(io, folder, tag):
                continue
        if ctx is not None and ctx.stop_lock_wait.is_set():
            raise _timed_out(folder, _owner_of(io, folder))
        if notice is not None and now >= notice:
            notice = None
            record = _owner_of(io, folder)
            if record.get("owner") and not _abandoned(record, tag):
                ctx.lock_held.update(record)  # type: ignore[union-attr]
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _timed_out(folder, _owner_of(io, folder))
        stopped.wait(min(0.05, remaining))

    # The nonce tells THIS claim from any later one of the same writer tag: a release and an
    # "abandoned" mark act only on the lock it still names (:func:`_taken_over`). The rest
    # says who and where, for a person deciding whether to break it.
    record = holder_record(tag)
    failed = True
    try:
        io.write_json(folder / OWNER_RECORD, record, tag)
        yield
        failed = False
    finally:
        why = _taken_over(io, folder, record["nonce"])
        if why:
            log.error("Evidence lock %s was not released by %s: %s", folder, tag, why)
        else:
            try:
                _release(io, folder)
            except (OSError, EvidenceError) as exc:
                if not failed:
                    raise
                _mark_abandoned(io, folder, record, tag, exc)


def read_text(io: TreeIO, path: PurePath) -> str:
    """The file as ``open(path, encoding="utf-8").read()`` returns it: universal
    newlines, a BOM kept, invalid UTF-8 raising ``UnicodeDecodeError``."""
    return TextIOWrapper(BytesIO(io.read_bytes(path)), encoding="utf-8").read()


def read_json(io: TreeIO, path: PurePath) -> dict:
    """A JSON object; ``{}`` when ABSENT, unparseable or not an object — a REFUSED read
    raises.

    Swallowing every ``OSError`` here hid committed evidence: a server answering ``550``
    to ``RETR manifest.json`` made :func:`evidence_shared_manifest._manifest` read ``{}``,
    so a test with two results listed as never run and ``promote_run`` said "run not
    found". Only an absent file means "no record" (``FileNotFoundError``, of which
    :class:`evidence_store.Absent` is the tree's proven form); denied, unreachable and
    timed-out reads go to the caller, which classifies them.
    """
    try:
        data = json.loads(read_text(io, path))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def glob_children(io: TreeIO, folder: PurePath, pattern: str) -> list[PurePath]:
    """``folder.glob(pattern)`` for a one-component pattern: the children whose name
    matches (case rules of the path flavour), in listing order; a folder that cannot
    be listed has none."""
    try:
        names = io.list_names(folder) or []
    except OSError:
        return []
    return [folder / n for n in names if (folder / n).match(pattern)]


def match_children(io: TreeIO, folder: PurePath, pattern: str) -> list[PurePath]:
    """:func:`glob_children` for a listing whose REFUSAL must be reported.

    ``glob_children`` answers "no matches" for a folder it cannot list, because that is
    what ``Path.glob`` does. For an artifact listing that is the failure the contract's
    refused-listing rule exists to prevent: a reports folder the server refuses would
    read as "no reports" instead of saying so. This one lets the ``OSError`` out, and
    the caller classifies it."""
    names = io.list_names(folder) or []
    return [folder / n for n in names if (folder / n).match(pattern)]


def name_key(name: str) -> str:
    """What a name is compared by, on every tree (``data_format.md`` §2): its NFC form, case
    folded (``str.casefold``)."""
    return unicodedata.normalize("NFC", name).casefold()


def same_name(a: str, b: str) -> bool:
    """Whether two names are one name (:func:`name_key`)."""
    return name_key(a) == name_key(b)


def _cannot_list(io: TreeIO) -> bool:
    """A tree that has no listing at all: an FTPS server without ``MLSD`` says so in
    ``has_mlsd`` once it has been asked."""
    return getattr(io, "has_mlsd", True) is False


def resolve_child(io: TreeIO, parent: PurePath, name: str) -> PurePath:
    """``parent/name`` in the spelling stored there: the sibling of that exact spelling, else
    the one sibling that is the same name by NFC and case folding (:func:`name_key`), on every
    flavour — a case-sensitive tree (a Linux folder, FTPS) reuses ``SN1/``
    for ``sn1`` exactly as NTFS does, so a name read back may differ in case only, never
    become a second folder (decision 21). Two pre-existing variants without the exact one are
    ambiguous. A name nothing matches is answered in NFC, the form a folder is created in.

    A tree that cannot list at all (an FTPS server without ``MLSD``) keeps the name as given:
    there is nothing to match it against, and a point read by name must still work there."""
    name = guard_component(name)
    try:
        names = io.list_names(parent) or []
    except OSError as exc:
        if _cannot_list(io):
            return parent / name
        raise os_error(exc) from exc
    if name in names:
        return parent / name
    hits = [n for n in names if same_name(n, name)]
    if len(hits) > 1:
        raise EvidenceError("invalid", f"ambiguous case variants of {name!r}: {sorted(hits)}")
    return parent / (hits[0] if hits else unicodedata.normalize("NFC", name))


_FLAVOURS: dict[str, type[PurePath]] = {"windows": PureWindowsPath, "posix": PurePosixPath}


def safe_relative(name: str, flavour: str) -> tuple[str, ...]:
    """The components of a payload file name that may be joined below a generation.

    A manifest names its own files, and a hand-edited one may name anything: the name
    must be RELATIVE and unanchored *in the tree's flavour* and free of ``..``.
    ``C:\\x`` and ``\\\\server\\s`` are anchored to a Windows tree; to a POSIX one they are
    a single odd name, which :func:`guard_component` then refuses for its separators —
    so both flavours reject both spellings, by different rules. Raises
    ``EvidenceError("corrupt")``, which quarantines the entry rather than pushing it.
    """
    pure = _FLAVOURS.get(flavour, PurePosixPath)(name)
    try:
        if pure.anchor or not pure.parts:
            raise EvidenceError("corrupt", f"{name!r} is not a relative file name")
        return tuple(guard_component(part) for part in pure.parts)
    except EvidenceError as exc:
        raise EvidenceError("corrupt", f"{name!r} is not a relative file name") from exc


def check_new_dirs(io: TreeIO, root: PurePath, target: PurePath) -> list[PurePath]:
    """The folders from an existing ``root`` down to ``target`` that are not there yet, in
    order, each NEW one passing the create-time rules (:func:`evidence_store.safe_component`:
    never a case variant of a sibling). Lists only down to the first absent folder: what is
    below it has no sibling yet. Creates nothing."""
    cur, new = root, []
    for part in target.relative_to(root).parts:
        if not new:
            names = io.list_names(cur) or []
            if part not in names:
                safe_component(part, siblings=names)
                new.append(cur / part)
        else:
            safe_component(part)
            new.append(cur / part)
        cur = cur / part
    return new


def make_dirs(io: TreeIO, root: PurePath, target: PurePath) -> None:
    """mkdir -p from an existing ``root`` down to ``target``; each NEW component
    passes the create-time rules (:func:`check_new_dirs`)."""
    for folder in check_new_dirs(io, root, target):
        io.mkdir(folder)
