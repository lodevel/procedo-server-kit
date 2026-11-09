"""SFTP transport for the shared evidence layout: the tree, and one operation scope.

:class:`SftpTree` is a :class:`evidence_tree.TreeIO` over paramiko's ``SFTPClient``
(``docs/plans/evidence_db_design.md``, "SFTP: a full storage type"), the twin of
:class:`evidence_ftps.FtpsTree`: one tree for the evidence store, the database's attachments
and a project on a server alike. The endpoint, the pinned host key, the connections and the
error translation live in :mod:`evidence_sftp_conn`; what is left here is the tree itself:

* **Absent is not refused.** ``ENOENT`` is proved against the parent's listing
  (:meth:`SftpTree._absent_or_denied`): an entry the parent lists but the server will not
  serve is a refusal, and a refusal RAISES — a denied listing answered with "empty" would show
  a populated store as an empty one.
* **One operation scope** (:meth:`SftpTree.operation`) carries the cancel flag, the leased
  channel and one cached parent listing per folder — never across operations, and dropped for
  a folder the same operation writes to. Listings come from ``listdir_attr``: structured (name,
  size, mtime, type), never localized, so a listing is always authoritative.
* **The replace is atomic where the server offers it** (``posix-rename@openssh.com``). Where it
  does not, replacing is delete-then-rename, the gap the contract permits for ``current.json``
  and an artifact, never for ``manifest.json``, whose replace is then refused.
* **Cancellation is the one failure that is not an ``OSError``**: it raises
  ``EvidenceError("cancelled")``, checked between transfer blocks. Stopping an SFTP transfer
  leaves the channel in step (each block is its own request), so the channel is kept and the
  ``.part`` of a stopped upload is removed on it.

``PurePosixPath`` throughout, ``flavour = "posix"``. paramiko is reached only through
:mod:`evidence_sftp_conn`, on the first connection, so this module imports without it. Qt-free.
"""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import stat as stat_mode
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import IO, Any

from .evidence_sftp_conn import (
    POSIX_RENAME,
    ChannelPool,
    Credentials,
    HostKeys,
    KeyFiles,
    SftpEndpoint,
    load_paramiko,
    status_reply,
    translate,
    wire_errors,
)
from .evidence_shared_manifest import MANIFEST
from .evidence_store import Absent, EvidenceError
from .evidence_tree import ListedTree, OperationScope, ScopeLocal, enter_scope

log = logging.getLogger(__name__)

BLOCK = 1 << 16  # transfer block: the unit at which a transfer notices a cancel
_UNLINK_ATTEMPTS = 5  # a .part the server still holds open for a moment
_UNLINK_PAUSE_S = 0.02

Fact = tuple[str, int, float]  # (kind, size, mtime): "dir" | "file" | "other"


class _Cancelled(Exception):
    """Internal: a transfer stopped between blocks because the scope was cancelled."""


def _clean(exc: BaseException) -> Any:
    """Mark a failure as one that leaves the channel in step (a status reply, a decision of
    this tree), so the operation keeps its channel instead of retiring it."""
    exc.sftp_clean = True  # type: ignore[attr-defined]
    return exc


def _fact_of(attr: Any) -> Fact:
    mode = attr.st_mode or 0
    kind = "dir" if stat_mode.S_ISDIR(mode) else "file" if stat_mode.S_ISREG(mode) else "other"
    return kind, int(attr.st_size or 0), float(attr.st_mtime or 0)


@dataclass
class _Scope(OperationScope):
    """One store operation: its cancel flag and cached listings, plus its channel."""

    client: Any = None
    broken: bool = False
    uploaded: dict[PurePosixPath, tuple[int, str]] = field(default_factory=dict)


class SftpTree(ListedTree):
    """:class:`evidence_tree.TreeIO` over an SFTP server.

    ``credentials`` answers ``(user, secret)`` by :attr:`SftpEndpoint.credential_key` — a
    :class:`evidence_store.SignIn` also carrying the private key file to sign in with (the
    secret is then its passphrase) and the confirmed host key. ``key_files`` and ``host_keys``
    replace those two when given. All are injected, as FTPS's credential is: a tree reads no
    settings. Building one without paramiko is refused
    (:data:`evidence_sftp_conn.MISSING_PARAMIKO`)."""

    flavour = "posix"

    def __init__(
        self,
        endpoint: SftpEndpoint,
        credentials: Credentials | None = None,
        *,
        host_keys: HostKeys | None = None,
        key_files: KeyFiles | None = None,
        pool: ChannelPool | None = None,
    ) -> None:
        load_paramiko()  # refused here, in a sentence, rather than at the first read
        self.endpoint = endpoint
        self.root = endpoint.root
        self.identity = endpoint.identity
        self.credential_key = endpoint.credential_key
        self.pool = pool or ChannelPool(
            endpoint, credentials, host_keys=host_keys, key_files=key_files
        )
        self._local = ScopeLocal()

    # -- scope + channel ----------------------------------------------------------------

    def latency_class(self) -> str:
        return "remote"

    @contextmanager
    def operation(self, cancel: threading.Event | None = None) -> Iterator[_Scope]:
        """Enter the scope for ONE store operation: one leased channel, one cached listing per
        folder, one cancel flag. Re-entrant — a nested operation joins the open one."""
        with enter_scope(self._local, _Scope(cancel=cancel), self._release) as scope:
            yield scope

    def _release(self, scope: _Scope) -> None:
        if scope.client is not None:
            self.pool.release(scope.client, broken=scope.broken)

    @contextmanager
    def _client(self) -> Iterator[Any]:
        """The operation's channel (leased on first use), or one leased for this call alone
        outside an operation. A failure that is not a status reply puts it in doubt."""
        scope = self._local.scope
        if scope is None:
            client = self.pool.acquire()
            broken = False
            try:
                yield client
            except BaseException as exc:
                broken = not getattr(exc, "sftp_clean", False)
                raise
            finally:
                self.pool.release(client, broken=broken)
            return
        if scope.client is None:
            scope.client = self.pool.acquire()
        try:
            yield scope.client
        except BaseException as exc:
            if not getattr(exc, "sftp_clean", False):
                scope.broken = True
            raise

    def _fail(self, exc: BaseException, path: PurePosixPath) -> OSError:
        """A wire failure as the contract's ``OSError``; a status reply keeps the channel."""
        failure = translate(exc, self._path(path))
        return _clean(failure) if status_reply(exc) else failure

    def _is_cancelled(self) -> bool:
        scope = self._local.scope
        return scope is not None and scope.cancelled()

    def _check_cancel(self) -> None:
        if self._is_cancelled():
            raise _Cancelled()

    def _cancelled(self, what: str) -> EvidenceError:
        return _clean(EvidenceError("cancelled", f"cancelled: {what}"))

    def _path(self, path: PurePosixPath) -> str:
        return PurePosixPath(path).as_posix()

    # -- listing ------------------------------------------------------------------------

    def _entries(self, folder: PurePosixPath) -> dict[str, Fact] | None:
        """One ``listdir_attr`` of ``folder`` as ``{name: (kind, size, mtime)}``, cached for
        the operation. ``None`` when the path is absent or is not a folder; a REFUSED listing
        raises, so a denied folder can never read as an empty store."""
        folder = PurePosixPath(folder)
        scope = self._local.scope
        if scope is not None and folder in scope.listings:
            return scope.listings[folder]
        found = self._list(folder)
        if scope is not None:
            scope.listings[folder] = found
        return found

    def _list(self, folder: PurePosixPath) -> dict[str, Fact] | None:
        with self._client() as sftp:
            try:
                return {a.filename: _fact_of(a) for a in sftp.listdir_attr(self._path(folder))}
            except wire_errors() as exc:
                if not status_reply(exc) or getattr(exc, "errno", None) == errno.EACCES:
                    raise self._fail(exc, folder) from exc
                refusal = exc
        # Absent and "not a folder" both answer here (``ENOENT``, or a generic failure for a
        # file): the parent says which, and a folder it lists is one the server REFUSED.
        fact = self._fact(folder)
        if fact is None or fact[0] != "dir":
            return None
        raise _clean(PermissionError(f"the server refused to list {self._path(folder)}: {refusal}"))

    def _stat_kind(self, path: PurePosixPath) -> Fact | None:
        """One entry's fact by ``stat`` (the root has no parent listing to come from)."""
        with self._client() as sftp:
            try:
                return _fact_of(sftp.stat(self._path(path)))
            except wire_errors() as exc:
                failure = self._fail(exc, path)
                if isinstance(failure, FileNotFoundError):
                    return None
                raise failure from exc

    def _fact(self, path: PurePosixPath) -> Fact | None:
        path = PurePosixPath(path)
        if path == self.root or path == path.parent:
            return self._stat_kind(path)
        found = self._entries(path.parent)
        return None if found is None else found.get(path.name)

    def _absent_or_denied(self, exc: BaseException, path: PurePosixPath) -> OSError:
        """``ENOENT`` is proved against a FRESH listing of the parent: an entry it lists is one
        the server refused, and a parent that cannot be listed proves nothing."""
        failure = self._fail(exc, path)
        if not isinstance(failure, FileNotFoundError):
            return failure
        path = PurePosixPath(path)
        self._forget(path.parent)
        try:
            if self._fact(path) is not None:
                return _clean(PermissionError(f"{failure} (the server lists it)"))
        except OSError as probe_failure:
            return probe_failure
        return failure

    def stat(self, path: PurePosixPath) -> tuple[int, float]:
        fact = self._fact(path)
        if fact is None:
            raise _clean(Absent(f"no such entry: {self._path(path)}"))
        return fact[1], fact[2]

    def newest_mtime(self, folder: PurePosixPath) -> float:
        """The newest file's mtime below ``folder`` (a folder's own time does not move when a
        file inside it is rewritten), else the folder's own."""
        newest = 0.0
        stack = [PurePosixPath(folder)]
        while stack:
            current = stack.pop()
            for name, (kind, _size, mtime) in (self._entries(current) or {}).items():
                if kind == "dir":
                    stack.append(current / name)
                else:
                    newest = max(newest, mtime)
        if newest:
            return newest
        fact = self._fact(folder)
        return fact[2] if fact is not None else 0.0

    # -- reads --------------------------------------------------------------------------

    def read_bytes(self, path: PurePosixPath) -> bytes:
        buf = BytesIO()
        self._retr(path, buf.write)
        return buf.getvalue()

    def get_file(self, path: PurePosixPath, local: Path, *, start: int = 0) -> None:
        """The file's bytes from *start* on, appended to *local* (written fresh from 0) block
        by block: never held in memory, and a cut transfer resumes."""
        with open(local, "ab" if start else "wb") as dst:
            self._retr(path, dst.write, start=start)

    def _retr(self, path: PurePosixPath, write: Callable[[bytes], Any], *, start: int = 0) -> None:
        """Read block by block from *start*, honouring the scope's cancel between blocks."""
        with self._client() as sftp:
            try:
                with sftp.open(self._path(path), "rb") as handle:
                    if start:
                        handle.seek(start)
                    while True:
                        self._check_cancel()
                        block = handle.read(BLOCK)
                        if not block:
                            break
                        write(block)
            except _Cancelled:
                raise self._cancelled(f"download of {self._path(path)}") from None
            except wire_errors() as exc:
                raise self._absent_or_denied(exc, path) from exc

    def sha256(self, path: PurePosixPath) -> str:
        """The hash of the file, confirmed against the server's own size. SFTP has no standard
        server-side digest, so an upload records the digest of the bytes it streamed and this
        trusts it once the server holds exactly as many bytes; any other file is read back."""
        scope = self._local.scope
        recorded = scope.uploaded.get(PurePosixPath(path)) if scope is not None else None
        if recorded is not None:
            size, digest = recorded
            self._forget(PurePosixPath(path).parent)
            if self.stat(path)[0] == size:
                return digest
        found = hashlib.sha256()
        self._retr(path, found.update)
        return found.hexdigest()

    # -- writes -------------------------------------------------------------------------

    def write_atomic(self, path: PurePosixPath, data: bytes, tag: str) -> None:
        self._put(path, BytesIO(data), tag, expect=len(data))

    def write_json(self, path: PurePosixPath, data: dict, tag: str) -> None:
        """The store's JSON records in this tree's newline convention: LF, the bytes
        ``json.dumps`` produces (the layout never compares a record across trees)."""
        payload = json.dumps(data, indent=2, ensure_ascii=True).encode("utf-8")
        self.write_atomic(path, payload, tag)

    def append_bytes(self, path: PurePosixPath, data: bytes) -> None:
        """Open with ``SSH_FXF_APPEND`` and add ``data`` at the end, creating the file when
        absent: the server writes at ITS end of file, so two benches appending never overwrite
        each other's lines (the index shard, :mod:`evidence_index`). No ``.part``, no rename and
        no cancel check: a handful of lines, one request."""
        path = PurePosixPath(path)
        self._ensure_parent(path)
        with self._client() as sftp:
            try:
                with sftp.open(self._path(path), "ab") as handle:
                    handle.write(data)
            except wire_errors() as exc:
                raise self._absent_or_denied(exc, path) from exc
        self._forget(path.parent)

    def put_file(self, src: Path, dst: PurePosixPath, tag: str, *, verify: bool) -> None:
        with open(src, "rb") as handle:
            self._put(dst, handle, tag, expect=Path(src).stat().st_size, verify=verify)

    def _put(
        self,
        dst: PurePosixPath,
        stream: IO[bytes],
        tag: str,
        *,
        expect: int,
        verify: bool = False,
    ) -> None:
        """Upload to ``<name>.<tag>.part``, then put it in place. A stopped or failed upload
        leaves no ``.part``."""
        dst = PurePosixPath(dst)
        temp = dst.with_name(f"{dst.name}.{tag}.part")
        self._ensure_parent(dst)
        digest = hashlib.sha256()
        sent = 0
        cancelled = False
        with self._client() as sftp:
            try:
                with sftp.open(self._path(temp), "wb") as handle:
                    handle.set_pipelined(True)
                    while True:
                        if self._is_cancelled():
                            cancelled = True
                            break
                        block = stream.read(BLOCK)
                        if not block:
                            break
                        handle.write(block)
                        digest.update(block)
                        sent += len(block)
            except wire_errors() as exc:
                failure = self._absent_or_denied(exc, temp)
                if getattr(failure, "sftp_clean", False):  # a dead channel cannot clean up
                    self._remove_leftover(temp)
                raise failure from exc
        self._forget(temp.parent)
        if cancelled:
            self._remove_leftover(temp)
            raise self._cancelled(f"upload of {self._path(dst)}")
        if verify and not self._matches(temp, sent, expect):
            self.unlink(temp, missing_ok=True)
            raise EvidenceError("corrupt", f"{dst.name} does not match its source after the upload")
        self._replace(temp, dst)
        scope = self._local.scope
        if scope is not None:
            scope.uploaded[dst] = (sent, digest.hexdigest())

    def _matches(self, temp: PurePosixPath, sent: int, expect: int) -> bool:
        """The uploaded copy is the source's length, by the server's own count."""
        return sent == expect and self.stat(temp)[0] == expect

    def _remove_leftover(self, temp: PurePosixPath) -> None:
        """Remove the ``.part`` a stopped or refused upload left, retried briefly (a server on
        Windows refuses to delete a file it still holds open)."""
        for attempt in range(_UNLINK_ATTEMPTS):
            try:
                self.unlink(temp, missing_ok=True)
                return
            except (OSError, EvidenceError):
                if attempt == _UNLINK_ATTEMPTS - 1:
                    log.warning("Could not remove %s after a stopped transfer", temp)
                    return
                time.sleep(_UNLINK_PAUSE_S * (attempt + 1))

    def _offers_posix_rename(self) -> bool:
        with self._client() as sftp:
            return POSIX_RENAME in getattr(sftp, "extensions", {})

    def _replace(self, temp: PurePosixPath, dst: PurePosixPath) -> None:
        """Put ``temp`` in place of ``dst``. ``posix-rename`` replaces atomically where the
        server offers it. Without it, SFTP's own rename never overwrites, so replacing is
        delete-then-rename — a gap the contract permits for ``current.json`` and an artifact,
        NEVER for ``manifest.json``, the switch that makes a generation visible: that replace
        fails instead, leaving nothing of the refused write behind."""
        if self._offers_posix_rename():
            with self._client() as sftp:
                try:
                    sftp.posix_rename(self._path(temp), self._path(dst))
                except wire_errors() as exc:
                    failure = self._absent_or_denied(exc, temp)
                    if getattr(failure, "sftp_clean", False):
                        self._remove_leftover(temp)
                    raise failure from exc
            self._forget(temp.parent)
            self._forget(dst.parent)
            return
        if self.exists(dst):
            if dst.name == MANIFEST:
                self._remove_leftover(temp)
                raise _clean(
                    EvidenceError(
                        "denied",
                        f"{self._path(dst)} already exists and this server cannot replace a "
                        "manifest atomically (it does not offer posix-rename); the generation "
                        "is left untouched",
                    )
                )
            self.unlink(dst, missing_ok=True)
        self.rename(temp, dst)

    def _ensure_parent(self, path: PurePosixPath) -> None:
        """Create the missing folders between the root and ``path`` (a payload may name a
        subfolder). Never the root itself."""
        parent = PurePosixPath(path).parent
        missing: list[PurePosixPath] = []
        while parent != self.root and parent != parent.parent and not self.is_dir(parent):
            missing.append(parent)
            parent = parent.parent
        for folder in reversed(missing):
            self.mkdir(folder)

    def _refuse_root(self, folder: PurePosixPath) -> None:
        if folder == self.root:
            raise EvidenceError("invalid", f"the tree never creates its own root: {self.root}")

    def mkdir(self, folder: PurePosixPath) -> None:
        """One level, below the root and never the root itself, so a mistyped base path is
        reported rather than created. An existing folder is fine."""
        folder = PurePosixPath(folder)
        self._refuse_root(folder)
        with self._client() as sftp:
            try:
                sftp.mkdir(self._path(folder))
            except wire_errors() as exc:
                if not status_reply(exc):
                    raise self._fail(exc, folder) from exc
                self._forget(folder.parent)
                if self.is_dir(folder):
                    return  # someone else created it, or it was already there
                raise self._absent_or_denied(exc, folder) from exc
        self._forget(folder.parent)

    def mkdir_exclusive(self, folder: PurePosixPath) -> bool:
        """Atomically claim a directory: SFTP's ``mkdir`` fails on an existing one, which
        belongs to another caller."""
        folder = PurePosixPath(folder)
        self._refuse_root(folder)
        if self._is_cancelled():
            raise self._cancelled(f"creating {self._path(folder)}")
        for attempt in range(2):
            with self._client() as sftp:
                try:
                    sftp.mkdir(self._path(folder))
                    break
                except wire_errors() as exc:
                    if not status_reply(exc) or getattr(exc, "errno", None) == errno.EACCES:
                        raise self._fail(exc, folder) from exc
                    refusal = exc
            self._forget(folder.parent)
            fact = self._stat_kind(folder)
            if fact is not None and fact[0] == "dir":
                return False
            if fact is None and attempt == 0:
                continue  # the previous holder may have released between mkdir and stat
            raise _clean(PermissionError(f"{refusal} ({self._path(folder)})")) from refusal
        self._forget(folder.parent)
        return True

    def release_lock(self, folder: PurePosixPath) -> None:
        """Remove an acquired lock even when its operation has been cancelled."""
        scope = self._local.scope
        if scope is not None and not scope.broken:
            cancel, scope.cancel = scope.cancel, None
            try:
                self.rmtree(folder)
            finally:
                scope.cancel = cancel
            return
        # A channel in doubt is not continued: cleanup runs on its own lease.
        self._local.scope = None
        try:
            with self.operation():
                self.rmtree(folder)
        finally:
            self._local.scope = scope

    def rename(self, src: PurePosixPath, dst: PurePosixPath) -> None:
        """SFTP's rename, which never replaces an existing ``dst`` (it is refused instead)."""
        with self._client() as sftp:
            try:
                sftp.rename(self._path(src), self._path(dst))
            except wire_errors() as exc:
                raise self._absent_or_denied(exc, src) from exc
        self._forget(PurePosixPath(src).parent)
        self._forget(PurePosixPath(dst).parent)

    def unlink(self, path: PurePosixPath, *, missing_ok: bool = False) -> None:
        with self._client() as sftp:
            try:
                sftp.remove(self._path(path))
            except wire_errors() as exc:
                failure = self._absent_or_denied(exc, path)
                if not (missing_ok and isinstance(failure, FileNotFoundError)):
                    raise failure from exc
        self._forget(PurePosixPath(path).parent)

    def rmtree(self, folder: PurePosixPath) -> None:
        folder = PurePosixPath(folder)
        for name, (kind, _size, _mtime) in sorted((self._entries(folder) or {}).items()):
            if kind == "dir":
                self.rmtree(folder / name)
            else:
                self.unlink(folder / name, missing_ok=True)
        with self._client() as sftp:
            try:
                sftp.rmdir(self._path(folder))
            except wire_errors() as exc:
                raise self._absent_or_denied(exc, folder) from exc
        self._forget(folder.parent)
        self._forget(folder)

    def _forget(self, folder: PurePosixPath) -> None:
        scope = self._local.scope
        if scope is not None:
            scope.forget(PurePosixPath(folder))

    def close(self) -> None:
        self.pool.close()
