"""Files on the JSON API server's own storage, as parts of a call or alone (``data_format.md``
§3.3, §3.4b): :class:`ApiBlobs`, the :class:`project_store.BlobStore` of that storage.

* **Which way a file travels.** A text document rides inside the JSON whatever its size (the
  callers put it there); a binary file of at most 1 MiB is a part of the JSON request or
  answer; one larger travels alone: uploaded before the JSON that references it (B1, held to
  ``limits.max_file_bytes``, checked before sending) and downloaded after it (B2). A part that
  would take a request over ``limits.max_upload_bytes`` goes alone too (:meth:`ApiBlobs.parts`).
* **A broken download resumes.** B2 lands in ``<cache>/<owner>/<sha[0:2]>/<sha>.part`` as it
  arrives; the next attempt asks for the rest (``Range``), and what arrived is checked against
  its sha256 before it is used.
* **One download per file at a time.** A second request for a file being downloaded waits for
  the first and takes what it brought (single-flight, by owner and version).
* **A failed storage is remembered briefly.** A download that could not reach the server makes
  the next ones fail at once with the same reason for :data:`FAILED_FOR_S` seconds, so a pass
  with many files does not knock on a dead server for each; the next pass tries again.

Every call may wait on the server: on a worker, never on the GUI thread. Qt-free.
"""

from __future__ import annotations

import hashlib
import shutil
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from . import atomic_write
from .api_session import ApiSession
from .evidence_api_http import Part, json_bytes
from .evidence_api_run import mimetype
from .evidence_store import EvidenceError, OpContext
from .project_store import PART_MAX, version_of

#: How long a storage that could not be reached is not asked again.
FAILED_FOR_S = 30.0
#: The kinds of failure that are the storage's, not one file's.
_UNREACHABLE = frozenset({"unreachable", "timeout", "auth"})


def _hex(version: str) -> str:
    return version.removeprefix("sha256:")


class ApiBlobs:
    """The server's own storage (``api``), one file at a time; a downloaded file kept in
    *cache_dir* by its owner and sha256."""

    def __init__(
        self, session: ApiSession, cache_dir: Path, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.session = session
        self.cache_dir = Path(cache_dir)
        self._clock = clock
        self._guard = threading.Lock()
        self._flights: dict[tuple[str, str], threading.Lock] = {}
        self._failed: tuple[float, EvidenceError] | None = None

    def _segments(self, owner: str, version: str, store_id: int | None = None) -> tuple[str, ...]:
        """B1's and B2's path: the storage (the one new files go to unless the file names its
        own), the owner, the bare sha256."""
        store = self.session.store_id() if store_id is None else store_id
        return ("storages", str(store), "blobs", owner, _hex(version))

    # -- up --------------------------------------------------------------------------------

    def upload(self, owner: str, data: bytes, ctx: OpContext | None = None) -> str:
        """B1: *data* alone, for *owner*; its version."""
        version = version_of(data)
        largest = self.session.limit("max_file_bytes")
        if largest is not None and len(data) > largest:
            raise EvidenceError(
                "invalid",
                f"A file of {len(data)} bytes is larger than this server takes ({largest}).",
            )
        with self.session.operation(ctx):
            self.session.ready()
            self.session.wire.upload("uploadBlob", self._segments(owner, version), data)
        return version

    def parts(
        self, owner: str, files: Mapping[str, bytes], body: dict, ctx: OpContext | None = None
    ) -> list[Part]:
        """The parts a JSON request carries for *files* (name -> bytes), each larger one sent
        alone first (B1), and as many more as keep the request under the server's
        ``max_upload_bytes`` with *body*, its JSON."""
        distinct = {version_of(data): (name, data) for name, data in files.items()}
        alone = {v for v, (_n, data) in distinct.items() if len(data) > PART_MAX}
        room = self.session.limit("max_upload_bytes")
        if room is not None:
            riding = sorted(
                (v for v in distinct if v not in alone), key=lambda v: -len(distinct[v][1])
            )
            total = len(json_bytes(body)) + sum(len(distinct[v][1]) + 512 for v in riding)
            for v in riding:
                if total <= room:
                    break
                alone.add(v)
                total -= len(distinct[v][1]) + 512
        for v in sorted(alone):
            self.upload(owner, distinct[v][1], ctx)
        return [
            Part(v, name, mimetype(name), data)
            for v, (name, data) in sorted(distinct.items())
            if v not in alone
        ]

    # -- down ------------------------------------------------------------------------------

    def cached(self, owner: str, version: str) -> Path:
        """Where the bytes of *version* are kept once downloaded."""
        sha = _hex(version)
        return self.cache_dir / owner / sha[:2] / sha

    def download(
        self,
        owner: str,
        version: str,
        size: int,
        dest: Path,
        ctx: OpContext | None = None,
        store_id: int | None = None,
    ) -> Path:
        """B2: the bytes of *version* into *dest*, resumed from what an earlier attempt kept;
        from *store_id*, the storage the file's reference names (the active one by default)."""
        kept = self.kept(owner, version, size, ctx, store_id)
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        temp = dest.with_name(f".{dest.name}.{threading.get_ident()}.copy")
        shutil.copyfile(kept, temp)
        atomic_write.replace(temp, dest)
        return dest

    def kept(
        self,
        owner: str,
        version: str,
        size: int,
        ctx: OpContext | None = None,
        store_id: int | None = None,
    ) -> Path:
        """The bytes of *version* in this PC's cache (:meth:`cached`), downloaded first when
        they are not there whole."""
        with self._flight(owner, version):
            kept = self.cached(owner, version)
            if kept.is_file() and kept.stat().st_size == size:
                return kept
            self._refuse_while_failed()
            try:
                self._get(self._segments(owner, version, store_id), version, size, kept, ctx)
            except EvidenceError as exc:
                if exc.kind in _UNREACHABLE:
                    with self._guard:
                        self._failed = (self._clock() + FAILED_FOR_S, exc)
                raise
            with self._guard:
                self._failed = None
            return kept

    def _flight(self, owner: str, version: str) -> threading.Lock:
        """The lock one file's download holds: a second caller waits for the first."""
        with self._guard:
            return self._flights.setdefault((owner, version), threading.Lock())

    def _refuse_while_failed(self) -> None:
        with self._guard:
            failed = self._failed
        if failed is not None and self._clock() < failed[0]:
            raise EvidenceError(failed[1].kind, f"{failed[1]} (not asked again so soon)")

    def _get(
        self, segments: tuple[str, ...], version: str, size: int, kept: Path, ctx: OpContext | None
    ) -> None:
        part = kept.with_name(kept.name + ".part")
        part.parent.mkdir(parents=True, exist_ok=True)
        start = part.stat().st_size if part.is_file() else 0
        if start >= size:
            part.unlink()  # whole already, or longer than the file: not what was asked for
            start = 0
        with self.session.operation(ctx):
            self.session.ready()
            try:
                self._into(part, segments, start)
            except EvidenceError as exc:
                if exc.code != "range_not_satisfiable":
                    raise
                part.unlink(missing_ok=True)  # the copy kept is not this file's beginning
                self._into(part, segments, 0)
        digest = hashlib.sha256()
        with part.open("rb") as held:
            for block in iter(lambda: held.read(1 << 20), b""):
                digest.update(block)
        if part.stat().st_size != size or "sha256:" + digest.hexdigest() != version:
            part.unlink()
            raise EvidenceError(
                "corrupt", f"The file downloaded is not {version[:19]}…: it was dropped."
            )
        atomic_write.replace(part, kept)

    def _into(self, part: Path, segments: tuple[str, ...], start: int) -> None:
        """B2 from *start* appended to *part*; the whole file over it when the server sent it
        whole."""
        with part.open("ab" if start else "wb") as out:
            status = self.session.wire.download("downloadBlob", segments, out.write, start)
        if start and status == 200:  # the whole file came, appended to what was kept
            atomic_write.write_bytes(part, part.read_bytes()[start:])
