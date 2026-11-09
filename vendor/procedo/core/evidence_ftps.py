"""FTPS transport for the shared evidence layout: the tree, and one operation scope.

:class:`FtpsTree` is a :class:`evidence_tree.TreeIO` over an FTPS connection, so the
shared layout's protocol (immutable generations, the forward-only pointer, the
evidence folder's boards, artifacts with sidecars, the outbox push) runs over a
server without knowing one exists. What the transport needs and the protocol must not
see is carved into two modules beside this one: :mod:`evidence_ftps_conn` (the
endpoint, the connection lease pool, the TLS contract and the error translation) and
:mod:`evidence_ftps_facts` (what one connection can be asked about a path). What is
left here is the tree itself — the decisions the contract is about:

* **Absent is not refused.** ``550`` answers both, so the tree asks the server which
  one it meant (:meth:`FtpsTree._absent_or_denied`) and a refusal RAISES: a denied
  listing answered with "empty" would show a populated store as an empty one.
* **One operation scope** (:meth:`FtpsTree.operation`) carries the cancel flag, the
  leased connection and one cached parent listing per folder — never across
  operations, and dropped for a folder the same operation writes to.
* **The replace gap is the pointer's alone.** Where the server refuses ``RNTO`` onto
  an existing name, replacing becomes delete-then-rename; the contract permits that
  gap for ``current.json`` and an artifact, never for ``manifest.json``.
* **Cancellation is the one failure that is not an ``OSError``**: it raises
  ``EvidenceError("cancelled")``, which the app already has a kind for, because a
  transfer the user stopped is not a transport failure.

``PurePosixPath`` throughout, ``flavour = "posix"``; stdlib only, so this module
imports with no test dependency present. Qt-free.
"""

from __future__ import annotations

import ftplib
import hashlib
import json
import logging
import socket
import ssl
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import IO, Any

from . import evidence_ftps_facts as facts
from .evidence_ftps_conn import (
    ConnectionPool,
    Credentials,
    FtpsEndpoint,
    clean_reply,
    reply_code,
    stream_is_fine,
    translate,
    unsupported,
)
from .evidence_shared_manifest import MANIFEST
from .evidence_store import Absent, EvidenceError
from .evidence_tree import ListedTree, OperationScope, ScopeLocal, enter_scope

log = logging.getLogger(__name__)

BLOCK = 1 << 16  # transfer block: the unit at which a transfer notices a cancel
_UNLINK_ATTEMPTS = 5  # a .part the server still holds open for a moment
_UNLINK_PAUSE_S = 0.02


class _Cancelled(Exception):
    """Internal: a transfer stopped between blocks because the scope was cancelled."""


def _finish_data_transfer(sock: socket.socket) -> None:
    """Finish TLS before closing an encrypted data socket; plain FTP only closes."""
    if isinstance(sock, ssl.SSLSocket):
        with suppress(OSError, ValueError):
            sock.unwrap()


# ---------------------------------------------------------------------------
# The operation scope
# ---------------------------------------------------------------------------


@dataclass
class _Scope(OperationScope):
    """One store operation: its cancel flag and cached listings, plus its connection."""

    conn: ftplib.FTP | None = None
    broken: bool = False
    uploaded: dict[PurePosixPath, tuple[int, str]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# The tree
# ---------------------------------------------------------------------------


class FtpsTree(ListedTree):
    """:class:`evidence_tree.TreeIO` over an FTPS connection."""

    flavour = "posix"

    def __init__(
        self,
        endpoint: FtpsEndpoint,
        credentials: Credentials | None = None,
        *,
        pool: ConnectionPool | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.root = endpoint.root
        self.identity = endpoint.identity
        self.credential_key = endpoint.credential_key
        self.pool = pool or ConnectionPool(endpoint, credentials)
        self.has_mlsd = True  # until a server answers "command not understood"
        self._local = ScopeLocal()

    # -- scope + connection ---------------------------------------------------------

    def latency_class(self) -> str:
        return "remote"

    @contextmanager
    def operation(self, cancel: threading.Event | None = None) -> Iterator[_Scope]:
        """Enter the scope for ONE store operation: one leased connection, one cached
        listing per folder, one cancel flag. Re-entrant — a nested operation joins the
        open one instead of leasing a second connection — so a store method may enter
        it without knowing whether its caller already did."""
        with enter_scope(self._local, _Scope(cancel=cancel), self._release) as scope:
            yield scope

    def _release(self, scope: _Scope) -> None:
        """The operation that leased a connection is over: back to the pool, unless
        it is in doubt."""
        if scope.conn is not None:
            self.pool.release(scope.conn, broken=scope.broken)

    @contextmanager
    def _conn(self) -> Iterator[ftplib.FTP]:
        scope = self._local.scope
        if scope is None:  # outside an operation: lease for this call alone
            conn = self.pool.acquire()
            broken = False
            try:
                yield conn
            except BaseException as exc:
                broken = not stream_is_fine(exc)
                raise
            finally:
                self.pool.release(conn, broken=broken)
            return
        if scope.conn is None:
            scope.conn = self.pool.acquire()
        try:
            yield scope.conn
        except BaseException as exc:
            # A reply the server completed leaves the connection usable; only a
            # transport failure or an unfinished transfer puts it in doubt.
            if not stream_is_fine(exc):
                scope.broken = True
            raise

    def _is_cancelled(self) -> bool:
        scope = self._local.scope
        return scope is not None and scope.cancelled()

    def _check_cancel(self) -> None:
        if self._is_cancelled():
            raise _Cancelled()

    def _cancelled(self, what: str, *, clean: bool) -> EvidenceError:
        """``clean``: the transfer ended tidily, so the connection may be reused. A
        cancelled DOWNLOAD is never clean — the server is still sending, and its reply
        cannot be drained (the drain blocks until the socket budget runs out)."""
        failure = EvidenceError("cancelled", f"cancelled: {what}")
        return clean_reply(failure) if clean else failure

    def _path(self, path: PurePosixPath) -> str:
        return PurePosixPath(path).as_posix()

    # -- listing --------------------------------------------------------------------

    def _entries(self, folder: PurePosixPath) -> dict[str, tuple[str, int, float]] | None:
        """One ``MLSD`` of ``folder`` as ``{name: (type, size, mtime)}``, cached for the
        operation. ``None`` when the path is absent or is not a folder; a REFUSED
        listing raises, so a denied share can never read as an empty store."""
        scope = self._local.scope
        if scope is not None and folder in scope.listings:
            return scope.listings[folder]
        found = self._mlsd(folder)
        if scope is not None:
            scope.listings[folder] = found
        return found

    def _mlsd(self, folder: PurePosixPath) -> dict[str, tuple[str, int, float]] | None:
        with self._conn() as conn:
            try:
                return facts.listing(conn, self._path(folder))
            except ftplib.Error as exc:
                if unsupported(exc):
                    self.has_mlsd = False
                    raise PermissionError(
                        f"the server has no MLSD, so listings are unreliable: {exc}"
                    ) from exc
                failure = translate(exc, self._path(folder))
                if not isinstance(failure, FileNotFoundError):
                    raise failure from exc
                # Absent, or not a folder: callers read both as empty. Only a FOLDER the
                # server will not list is a refusal (the contract's refused-listing rule).
                if self._probe(folder) != "dir":
                    return None
                raise clean_reply(PermissionError(f"{exc} ({self._path(folder)})")) from exc

    def _absent_or_denied(self, exc: ftplib.Error, path: PurePosixPath) -> OSError:
        """``550`` answers both "no such folder" and "not enough privileges". Ask the
        server whether the entry exists at all and report a refusal as one."""
        failure = translate(exc, self._path(path))
        if not isinstance(failure, FileNotFoundError):
            return failure
        try:
            if self._probe(path) != "absent":
                return clean_reply(PermissionError(f"{exc} ({self._path(path)})"))
        except OSError as probe_failure:
            return probe_failure  # a refused probe does not prove the path absent
        return failure

    def _probe(self, path: PurePosixPath) -> str:
        """One entry's kind. An ambiguous MLST refusal requires its parent's listing."""
        with self._conn() as conn:
            return facts.probe(conn, self._path(path))

    def _fact(self, path: PurePosixPath) -> tuple[str, int, float] | None:
        if PurePosixPath(path) == self.root:
            kind = self._probe(path)
            return None if kind == "absent" else (kind, 0, 0.0)
        found = self._entries(PurePosixPath(path).parent)
        return None if found is None else found.get(PurePosixPath(path).name)

    def stat(self, path: PurePosixPath) -> tuple[int, float]:
        fact = self._fact(path)
        if fact is None:
            raise clean_reply(Absent(f"no such entry: {self._path(path)}"))
        return fact[1], fact[2]

    def newest_mtime(self, folder: PurePosixPath) -> float:
        """The newest file's mtime below ``folder`` (a folder's own time does not move
        when a file inside it is rewritten), else the folder's own."""
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

    # -- reads ----------------------------------------------------------------------

    def read_bytes(self, path: PurePosixPath) -> bytes:
        buf = BytesIO()
        self._retr(path, buf.write)
        return buf.getvalue()

    def get_file(self, path: PurePosixPath, local: Path, *, start: int = 0) -> None:
        """The file's bytes from *start* on (``REST``), appended to *local* (written fresh
        from 0) block by block: never held in memory, and a cut transfer resumes."""
        with open(local, "ab" if start else "wb") as dst:
            self._retr(path, dst.write, start=start)

    def _retr(self, path: PurePosixPath, write: Callable[[bytes], Any], *, start: int = 0) -> None:
        """``RETR`` block by block, from *start* (``REST``) when it is not 0, honouring the
        scope's cancel between blocks.

        A cancelled download cannot drain its own reply — the server is still sending —
        so the connection is marked broken and closed rather than returned to the pool
        with an unread reply on it, which would derail the next operation.
        """
        with self._conn() as conn:
            try:
                conn.voidcmd("TYPE I")
                with conn.transfercmd(f"RETR {self._path(path)}", rest=start or None) as sock:
                    while True:
                        self._check_cancel()
                        block = sock.recv(BLOCK)
                        if not block:
                            break
                        write(block)
                    _finish_data_transfer(sock)
                conn.voidresp()
            except _Cancelled:
                raise self._cancelled(f"download of {self._path(path)}", clean=False) from None
            except ftplib.Error as exc:
                raise self._absent_or_denied(exc, path) from exc
            except (OSError, EOFError) as exc:
                raise translate(exc, self._path(path)) from exc

    def sha256(self, path: PurePosixPath) -> str:
        """The hash of the file, confirmed against the server's own size.

        The push verifies every file it just sent, and re-downloading each one to
        re-hash it would double the cost of every upload on a link whose cost is per
        file. So an upload records the digest of the bytes it streamed, and this reads
        the server's size back before trusting it: the destination is proved to hold
        exactly as many bytes as were sent, and the digest is of those bytes. A file
        this operation did not write is downloaded and hashed.
        """
        scope = self._local.scope
        recorded = scope.uploaded.get(PurePosixPath(path)) if scope is not None else None
        if recorded is not None:
            size, digest = recorded
            if self.stat(path)[0] == size:
                return digest
        found = hashlib.sha256()
        self._retr(path, found.update)
        return found.hexdigest()

    # -- writes ---------------------------------------------------------------------

    def write_atomic(self, path: PurePosixPath, data: bytes, tag: str) -> None:
        self._put(path, BytesIO(data), tag, expect=len(data))

    def write_json(self, path: PurePosixPath, data: dict, tag: str) -> None:
        """The store's JSON records in this tree's own newline convention: LF, the
        bytes ``json.dumps`` produces (the layout never compares a record across
        trees)."""
        payload = json.dumps(data, indent=2, ensure_ascii=True).encode("utf-8")
        self.write_atomic(path, payload, tag)

    def append_bytes(self, path: PurePosixPath, data: bytes) -> None:
        """``APPE``: add ``data`` at the end of ``path``, creating it when absent.

        No ``.part`` and no rename, because the point is to ADD to what other
        benches wrote rather than replace it — the index shard is the only record
        the store grows (:mod:`evidence_index`). No cancel check either: the payload
        is a handful of short lines, one block, and stopping between blocks has
        nothing to stop. A transfer the server cuts short leaves a partial line,
        which the reader skips.
        """
        path = PurePosixPath(path)
        self._ensure_parent(path)
        with self._conn() as conn:
            try:
                conn.voidcmd("TYPE I")
                with conn.transfercmd(f"APPE {self._path(path)}") as sock:
                    sock.sendall(data)
                    _finish_data_transfer(sock)
                conn.voidresp()
            except ftplib.Error as exc:
                raise self._absent_or_denied(exc, path) from exc
            except (OSError, EOFError) as exc:
                raise translate(exc, self._path(path)) from exc
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
        """Upload to ``<name>.<tag>.part``, then put it in place.

        The temp name carries the writer's tag, so two writers never share one. A
        cancelled upload CAN drain its reply (the server stops when the data connection
        closes), so the connection survives and the ``.part`` is removed on it.
        """
        dst = PurePosixPath(dst)
        temp = dst.with_name(f"{dst.name}.{tag}.part")
        self._ensure_parent(dst)
        digest = hashlib.sha256()
        sent = 0
        cancelled = False
        with self._conn() as conn:
            try:
                conn.voidcmd("TYPE I")
                with conn.transfercmd(f"STOR {self._path(temp)}") as sock:
                    while True:
                        # Stop feeding rather than dropping the socket: the transfer
                        # then ENDS cleanly, the server answers it, and the connection
                        # stays usable to delete the .part on.
                        if self._is_cancelled():
                            cancelled = True
                            break
                        block = stream.read(BLOCK)
                        if not block:
                            break
                        sock.sendall(block)
                        digest.update(block)
                        sent += len(block)
                    _finish_data_transfer(sock)
                conn.voidresp()
            except ftplib.Error as exc:
                failure = self._absent_or_denied(exc, temp)
                self._remove_leftover(temp)  # a FAILED upload leaves no .part either
                raise failure from exc
            except (OSError, EOFError) as exc:
                failure = translate(exc, self._path(dst))
                if stream_is_fine(failure):  # a dead connection cannot clean up
                    self._remove_leftover(temp)
                raise failure from exc
        self._forget(temp.parent)
        if cancelled:
            self._remove_leftover(temp)
            raise self._cancelled(f"upload of {self._path(dst)}", clean=True)
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
        """Remove the ``.part`` a cancelled or refused upload left behind.

        The server can still hold the file open for a moment after the transfer ends
        (Windows refuses to delete an open file, answering ``550``), so the removal is
        retried briefly rather than given up on at the first refusal — a cancelled
        transfer that leaves its ``.part`` is exactly what the contract forbids.
        """
        for attempt in range(_UNLINK_ATTEMPTS):
            try:
                self.unlink(temp, missing_ok=True)
                return
            except (OSError, EvidenceError):
                if attempt == _UNLINK_ATTEMPTS - 1:
                    log.warning("Could not remove %s after a stopped transfer", temp)
                    return
                time.sleep(_UNLINK_PAUSE_S * (attempt + 1))

    def _replace(self, temp: PurePosixPath, dst: PurePosixPath) -> None:
        """``RNTO`` onto an existing name is refused by many servers (pyftpdlib answers
        ``550 File exists``), so replacing becomes delete-then-rename — a gap in which
        the name does not exist.

        The contract permits that gap for ``current.json`` (a reader falls back to the
        newest complete generation while its writer holds the pointer lock) and
        for an artifact overwritten by name, but NEVER for ``manifest.json``, which is
        the switch that makes a generation visible: a manifest that cannot be put in
        place atomically fails instead of opening a window in which the generation
        exists and is invisible.
        """
        if self.exists(dst):
            if dst.name == MANIFEST:
                self._remove_leftover(temp)  # nothing of a refused write is left behind
                raise EvidenceError(
                    "denied",
                    f"{self._path(dst)} already exists and this server cannot replace a "
                    "manifest atomically; the generation is left untouched",
                )
            self.unlink(dst, missing_ok=True)
        self.rename(temp, dst)

    def _ensure_parent(self, path: PurePosixPath) -> None:
        """Create the missing folders between the root and ``path`` (a payload may name
        a subfolder). Never the root itself."""
        parent = PurePosixPath(path).parent
        missing: list[PurePosixPath] = []
        while parent != self.root and parent != parent.parent and not self.is_dir(parent):
            missing.append(parent)
            parent = parent.parent
        for folder in reversed(missing):
            self.mkdir(folder)

    def mkdir(self, folder: PurePosixPath) -> None:
        """One level, below the root and never the root itself, so a mistyped base path
        is reported rather than created. An existing folder is fine."""
        folder = PurePosixPath(folder)
        if folder == self.root:
            raise EvidenceError("invalid", f"the tree never creates its own root: {self.root}")
        with self._conn() as conn:
            try:
                conn.mkd(self._path(folder))
            except ftplib.Error as exc:
                self._forget(folder.parent)
                if self.is_dir(folder):
                    return  # someone else created it, or it was already there
                raise self._absent_or_denied(exc, folder) from exc
            except (OSError, EOFError) as exc:
                raise translate(exc, self._path(folder)) from exc
        self._forget(folder.parent)

    def mkdir_exclusive(self, folder: PurePosixPath) -> bool:
        """Atomically claim a directory. An existing directory belongs to another caller."""
        folder = PurePosixPath(folder)
        if folder == self.root:
            raise EvidenceError("invalid", f"the tree never creates its own root: {self.root}")
        if self._is_cancelled():
            raise self._cancelled(f"creating {self._path(folder)}", clean=True)
        with self._conn() as conn:
            for attempt in range(2):
                try:
                    conn.mkd(self._path(folder))
                    break
                except ftplib.Error as exc:
                    if reply_code(exc) not in (550, 521):
                        raise translate(exc, self._path(folder)) from exc
                    self._forget(folder.parent)
                    kind = self._probe(folder)
                    if kind == "dir":
                        return False
                    if kind == "absent" and attempt == 0:
                        continue  # the previous holder may have released between MKD and MLST
                    raise clean_reply(PermissionError(f"{exc} ({self._path(folder)})")) from exc
                except (OSError, EOFError) as exc:
                    raise translate(exc, self._path(folder)) from exc
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
        # An interrupted download leaves unread control replies. Cleanup needs its
        # own connection instead of continuing that broken command stream.
        self._local.scope = None
        try:
            with self.operation() as cleanup:
                try:
                    self.rmtree(folder)
                finally:
                    if scope is not None:
                        # Cleanup may start after the store was detached. Its new
                        # connection must not repopulate that closed pool either.
                        cleanup.broken = True
        finally:
            self._local.scope = scope

    def rename(self, src: PurePosixPath, dst: PurePosixPath) -> None:
        with self._conn() as conn:
            try:
                conn.rename(self._path(src), self._path(dst))
            except ftplib.Error as exc:
                raise self._absent_or_denied(exc, src) from exc
            except (OSError, EOFError) as exc:
                raise translate(exc, self._path(src)) from exc
        self._forget(PurePosixPath(src).parent)
        self._forget(PurePosixPath(dst).parent)

    def unlink(self, path: PurePosixPath, *, missing_ok: bool = False) -> None:
        with self._conn() as conn:
            try:
                conn.delete(self._path(path))
            except ftplib.Error as exc:
                failure = self._absent_or_denied(exc, path)
                if not (missing_ok and isinstance(failure, FileNotFoundError)):
                    raise failure from exc
            except (OSError, EOFError) as exc:
                raise translate(exc, self._path(path)) from exc
        self._forget(PurePosixPath(path).parent)

    def rmtree(self, folder: PurePosixPath) -> None:
        folder = PurePosixPath(folder)
        for name, (kind, _size, _mtime) in sorted((self._entries(folder) or {}).items()):
            if kind == "dir":
                self.rmtree(folder / name)
            else:
                self.unlink(folder / name, missing_ok=True)
        with self._conn() as conn:
            try:
                conn.rmd(self._path(folder))
            except ftplib.Error as exc:
                raise self._absent_or_denied(exc, folder) from exc
            except (OSError, EOFError) as exc:
                raise translate(exc, self._path(folder)) from exc
        self._forget(folder.parent)

    def _forget(self, folder: PurePosixPath) -> None:
        scope = self._local.scope
        if scope is not None:
            scope.forget(PurePosixPath(folder))

    def close(self) -> None:
        self.pool.close()
