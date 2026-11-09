"""What a store of the shared layout on a server tree has — the materialization cache and
the operation scope.

:class:`RemoteCachedStore` is the shared layout (:class:`evidence_shared_layout.SharedLayout`)
on a server tree (:class:`evidence_store_ftps.FtpsStore`):

* **``local_path`` materializes** into a per-machine cache, because consumers hand
  paths to ``open()`` and ``QUrl.fromLocalFile``. A run is fetched as a WHOLE
  generation, each file on its own: one the server cannot give is left out and named in
  the entry's ``.missing.json`` (:mod:`evidence_missing`), so a missing picture never
  hides its run, and looked for again (:func:`look_again`). Generations are immutable and
  addressed by a concrete run id, so a complete cache entry is always correct. An artifact
  is overwritten by name (a draft on every save), so its entry is revalidated against the
  server before it is reused — the one thing each backend decides for itself
  (``_fetch_if_stale``: FTPS compares size, second and a "fetched at" stamp).
* **Every inherited protocol call runs inside ONE operation scope** (:data:`_SCOPED`).

A backend subclass names its tree (``_TREE``), its endpoint parser
(``_parse_endpoint``) and its validator.
"""

from __future__ import annotations

import functools
import hashlib
import logging
import os
import secrets
import threading
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

from . import app_identity, atomic_write
from .evidence_board import segments
from .evidence_missing import DAMAGED, MISSING, NOT_FOUND, UNREACHABLE
from .evidence_shared_layout import SharedLayout
from .evidence_shared_manifest import MANIFEST
from .evidence_store import (
    CURRENT_RUN,
    RESULT_FILE,
    Absent,
    EvidenceError,
    EvidenceKey,
    OpContext,
    guard_component,
)
from .evidence_store_fs import _read_json, os_error
from .evidence_tree import safe_relative
from .paths import rmtree

log = logging.getLogger(__name__)

#: A generation with every file of its manifest in the entry: no later read changes it. An
#: entry WITHOUT it is served, its ``.missing.json`` naming what it lacks, and a later read
#: looks for those files again (:func:`look_again`).
CACHE_MARK = ".complete"

#: The cache entries this process looked at already (fetched, or looked again): a file of
#: theirs the server proved absent or gave damaged is looked for again once per session.
_LOOKED: set[str] = set()
_LOOKED_LOCK = threading.Lock()


def default_cache_dir() -> Path:
    """The per-machine materialization cache, beside the outbox."""
    return app_identity.local_data_dir() / "evidence_cache"


def record_missing(run_id: str, local: Path, missing: dict[str, str]) -> None:
    """Name what a materialized run's entry lacks, else mark it complete — in that order, so
    a reader never finds an entry to look again into without its record. Every server
    store's materialization ends with it (this module's, the database's, the API's)."""
    if missing:
        log.warning("Run %s is shown without %s", run_id, missing)
        atomic_write.write_json(local / MISSING, missing)
        return
    (local / CACHE_MARK).write_bytes(b"")
    (local / MISSING).unlink(missing_ok=True)


def look_again(local: Path, why: str) -> bool:
    """Whether a read of the cache entry ``local`` fetches again a file it lacks for *why*.
    ``unreachable``: always, the server did not answer. ``not_found`` and ``damaged`` (a file
    put back on the storage from a backup): once per session, by the session's first read of
    the entry — a fetch counts as one (:func:`looked`). A store's read asks it for each file
    it lacks, so it is asked on the read's worker, never the screen's thread."""
    if why == UNREACHABLE:
        return True
    with _LOOKED_LOCK:
        return str(local) not in _LOOKED


def looked(local: Path) -> None:
    """The cache entry ``local`` was looked at in full this session: fetched, or looked again
    for every file it lacked."""
    with _LOOKED_LOCK:
        _LOOKED.add(str(local))


def flag(params: dict[str, Any], key: str, default: bool) -> bool:
    """A declared ``choice:on|off`` param as a bool — read the same way when the store
    is built and when the entry is validated, so the two can never disagree."""
    raw = params.get(key)
    if raw is None or raw == "":
        return default
    return str(raw).strip().lower() in ("1", "on", "true", "yes")


#: The contract's "one cached parent listing for the duration of a single store
#: operation, never across" needs an operation boundary, and cancellation is scoped
#: rather than passed to every primitive. The methods below come from
#: :class:`SharedLayout`, which knows nothing about transports, so each is wrapped
#: once, at the end of this module: it enters the tree's scope (re-entrant, so a store
#: method that already opened one is unaffected), which leases one connection for the
#: call and drops the listing cache at the end. ``stage`` / ``commit_run`` /
#: ``write_run`` are NOT here: they touch local disk only (the outbox), and a scope
#: for them would pay for a login the end of a test must never wait on.
_SCOPED = (
    "list_boards",
    "list_runs",
    "current_run",
    "read_run",
    "delete_run",
    "promote_run",
    "demote_run",
    "list_artifacts",
    "read_artifact",
    "write_artifact",
    "delete_artifact",
    "list_generations",
    "read_generation",
    "list_trash",
    "search",
    "restore_trash",
    "purge_trash",
    "read_run_annotation",
    "add_run_annotation_entries",
    "read_board_entries",
    "add_board_entries",
    "sweep_incomplete",
    "flush_outbox",
)


class RemoteCachedStore(SharedLayout):
    """The shared layout on a server: a backend subclass adds its transport."""

    #: The backend's tree, built as ``_TREE(endpoint, credentials)``.
    _TREE: Callable[..., Any]
    #: The backend's entry params -> endpoint (a ``staticmethod``; never a credential).
    _parse_endpoint: Callable[[dict[str, Any]], Any]
    #: Refetch ``local`` from ``remote`` unless the server proves the copy on disk is
    #: still current. Only a proven absence is ignored: an unreachable or refused
    #: server must not quietly serve a stale artifact as current.
    _fetch_if_stale: Callable[[PurePosixPath, Path], None]

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """A scoped name defined in a backend's body would replace the wrapper with an
        unscoped call — silently, so it is refused when the class is built."""
        super().__init_subclass__(**kwargs)
        shadowed = sorted(set(_SCOPED) & set(vars(cls)))
        if shadowed:
            raise TypeError(f"{cls.__name__} would bypass the operation scope of {shadowed}")

    def __init__(
        self,
        endpoint: Any,
        *,
        machine_id: str | Callable[[], str],
        project_root: Path | None = None,
        project_id: str = "",
        outbox: Path | None = None,
        credentials: Callable[[str], tuple[str, str]] | None = None,
        cache_dir: Path | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.io = self._TREE(endpoint, credentials)
        self.root = self.io.root
        self.cache_dir = Path(cache_dir) if cache_dir else default_cache_dir()
        self._init_shared(
            machine_id=machine_id, project_root=project_root, project_id=project_id, outbox=outbox
        )

    @classmethod
    def from_config(
        cls,
        spec: dict[str, Any],
        *,
        project_root: Path,
        machine_id: Any = None,
        credentials: Callable[[str], tuple[str, str]] | None = None,
    ) -> RemoteCachedStore:
        """``credentials`` is an injected lookup by endpoint identity — the store never
        reads settings itself, so no backend imports the settings module."""
        if machine_id is None:
            raise EvidenceError("invalid", "the shared layout needs a machine_id")
        return cls(
            cls._parse_endpoint(spec.get("params") or {}),
            machine_id=machine_id,
            project_root=project_root,
            credentials=credentials,
        )

    @classmethod
    def endpoint_identity(cls, spec: dict[str, Any]) -> str:
        """The identity a store built from this entry would use (it routes the
        outbox), computed WITHOUT building one. The GUI asks the backend rather than
        spelling it itself, so a pending upload is never routed to a string no store
        answers to. ``""`` when the entry does not name an endpoint yet."""
        try:
            return cls._parse_endpoint(spec.get("params") or {}).identity
        except EvidenceError:
            return ""

    @classmethod
    def credential_key(cls, spec: dict[str, Any]) -> str:
        """The key this entry's sign-in is filed under — the SERVER's, never the
        folder's, so one sign-in serves every folder on it — asked of the endpoint
        the store would use, so the GUI never spells it. Being no folder's, it needs
        none: an entry naming the server alone (a sign-in picked in the editor) has
        one too. ``""`` when the entry does not name a server yet."""
        params = dict(spec.get("params") or {})
        params["base_path"] = params.get("base_path") or "/"  # a server tree's folder key
        try:
            return cls._parse_endpoint(params).credential_key
        except EvidenceError:
            return ""

    # -- materialization ------------------------------------------------------------

    def _cache_root(self, project_id: str) -> Path:
        """Keyed by store identity, so two stores never share an entry, and hashed
        because an endpoint is not a folder name."""
        endpoint = hashlib.sha256(self.io.identity.encode("utf-8")).hexdigest()[:16]
        return self.cache_dir / endpoint / guard_component(project_id[:8])

    def local_path(self, key: EvidenceKey, ctx: OpContext | None = None) -> Path:
        """A real local path for a consumer that must ``open()`` it.

        A run materializes as a WHOLE generation: the result, its embedded images and
        ``console.log`` are read side by side, so a single file would leave a directory
        the consumer cannot use. Generations are immutable and addressed by a CONCRETE
        run id (never ``current``), so a complete cache entry is always correct. A file
        the server could not give is absent from the entry and named in its
        ``.missing.json`` (:func:`evidence_missing.missing_files` reads it).
        """
        try:
            with self.io.operation((ctx or OpContext()).cancel):
                if key.kind == "run":
                    return self._run_dir_cached(key)
                return self._artifact_cached(key)
        except OSError as exc:  # the whole materialization path, typed like every other
            raise os_error(exc, key) from exc

    def _run_dir_cached(self, key: EvidenceKey) -> Path:
        test_dir = self._test_dir(key.project_id, key.board, key.test_id)
        run_id = key.run_id
        if run_id in (None, CURRENT_RUN):
            current = self._current_manifest(test_dir)
            if current is None:
                raise EvidenceError("not_found", f"no current run for {key.board}/{key.test_id}")
            run_id = current["run_id"]
        remote = test_dir / "runs" / guard_component(run_id)
        local = (
            self._cache_root(key.project_id).joinpath(*_board_folders(key.board))
            / guard_component(key.test_id or "")
            / guard_component(run_id)
        )
        if not (local / CACHE_MARK).is_file():
            if local.is_dir():
                self._fetch_again(remote, local)
            else:
                self._materialize(remote, local)
        return local / guard_component(key.name) if key.name else local

    def _materialize(self, remote: PurePosixPath, local: Path) -> None:
        """Fetch into a folder THIS call owns, then move it into place.

        A cache entry is shared by (identity, project, board, test id, run id), so
        fetching straight into it forced a failure to delete it — which deleted a
        concurrent reader's generation out from under it. A private staging folder makes
        a cancel cost nothing but itself, and the rename publishes the entry in one
        step; a reader that lost the race keeps the winner's copy (an entry still missing
        a file included: a later read looks for that file again, :func:`look_again`).
        """
        staging = local.parent / f".{local.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
        try:
            self._fetch_generation(remote, staging)
            try:
                staging.rename(local)
            except OSError:
                if not local.is_dir():
                    raise  # not a lost race, a real failure
            looked(local)
        finally:
            rmtree(staging, ignore_errors=True)  # only ever this call's own folder

    def _fetch_generation(self, remote: PurePosixPath, local: Path) -> None:
        """Download each file the generation's manifest lists on its own, the manifest
        last: a file the server cannot give is named in ``.missing.json`` instead of
        failing the run. Only a fetch that left out nothing is marked complete."""
        from .evidence_tree import read_json

        found = read_json(self.io, remote / MANIFEST)
        if not found:
            raise EvidenceError("not_found", f"no manifest for {remote.name}")
        local.mkdir(parents=True, exist_ok=True)
        missing = {}
        for entry in found.get("files", []):
            why = self._fetch_file(remote, local, entry, Path.write_bytes)
            if why:
                missing[str(entry.get("name", ""))] = why
        (local / MANIFEST).write_bytes(self.io.read_bytes(remote / MANIFEST))
        self._record_missing(remote, local, missing)

    def _fetch_again(self, remote: PurePosixPath, local: Path) -> None:
        """Fetch into a served entry the files it lacks that :func:`look_again` says to: the
        ones the server did not answer for last time, and once per session the ones it proved
        absent or gave damaged.

        Into the entry itself, never a fresh copy: a reader may hold it, and a file in it
        is never rewritten, only added (another reader's retry may place the same verified
        bytes first)."""
        recorded = _read_json(local / MISSING)
        missing, again = {}, False
        for entry in _read_json(local / MANIFEST).get("files", []):
            name = str(entry.get("name", ""))
            why = recorded.get(name, "")
            if why and look_again(local, why):
                again = True
                why = self._fetch_file(remote, local, entry, atomic_write.create_bytes)
            if why:
                missing[name] = why
        if again:
            self._record_missing(remote, local, missing)
        looked(local)

    def _fetch_file(
        self,
        remote: PurePosixPath,
        local: Path,
        entry: dict[str, Any],
        place: Callable[[Path, bytes], object],
    ) -> str:
        """Put one manifest file in ``local`` with ``place``; why it is not there, or ``""``.

        Only a proven absence is ``not_found``: any other failure to read it is
        ``unreachable``, retried by the next read. Bytes whose size or sha256 differ from
        the manifest's are ``damaged`` and never placed. The run's own record is no
        attachment: a failure to read it fails the read, as the error it is."""
        parts = safe_relative(str(entry.get("name", "")), self.io.flavour)
        try:
            data = self.io.read_bytes(remote.joinpath(*parts))
        except OSError as exc:
            if parts == (RESULT_FILE,):
                raise  # without its record there is no run to show
            if isinstance(exc, Absent):
                return NOT_FOUND
            log.info("%s of run %s not fetched: %s", entry.get("name"), remote.name, exc)
            return UNREACHABLE
        if len(data) != entry.get("size") or hashlib.sha256(data).hexdigest() != entry.get(
            "sha256"
        ):
            return DAMAGED
        target = local.joinpath(*parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            place(target, data)
        except FileExistsError:
            pass  # another reader's retry placed the same verified bytes
        return ""

    @staticmethod
    def _record_missing(remote: PurePosixPath, local: Path, missing: dict[str, str]) -> None:
        record_missing(remote.name, local, missing)

    def _artifact_cached(self, key: EvidenceKey) -> Path:
        """An artifact is overwritten by name (a draft on every save), so its cache
        entry — and each sidecar's — goes through the backend's ``_fetch_if_stale``."""
        from .evidence_store_fs import _sidecars

        folder = self._cache_root(key.project_id).joinpath(*_board_folders(key.board or "_project"))
        folder = folder / key.kind
        if not key.name:
            folder.mkdir(parents=True, exist_ok=True)
            return folder
        remote = self._artifact_root(key.kind, key.project_id, key.board) / guard_component(
            key.name
        )
        local = folder / guard_component(key.name)
        folder.mkdir(parents=True, exist_ok=True)
        for source, target in [
            (remote, local),
            *zip(_sidecars(key.kind, remote), _sidecars(key.kind, local)),
        ]:
            self._fetch_if_stale(source, target)
        return local

    def close(self) -> None:
        """Drop what the tree holds open (a project switch, a re-pointed store,
        shutdown): FTPS's logged-in connections, nothing over HTTP. Reopening is
        automatic."""
        self.io.close()


def _board_folders(key: str) -> tuple[str, ...]:
    """The cache folders of a board: its key's folders, as on the store
    (:func:`evidence_board.segments`), so two boards sharing a serial never share one."""
    return tuple(guard_component(name) for name in segments(key))


def _scoped(name: str) -> Any:
    inherited = getattr(SharedLayout, name)

    @functools.wraps(inherited)  # the wrapper keeps the signature callers introspect
    def call(self: RemoteCachedStore, *args: Any, **kwargs: Any) -> Any:
        ctx = kwargs.get("ctx")
        if ctx is None:
            ctx = next((a for a in reversed(args) if isinstance(a, OpContext)), None)
        with self.io.operation(ctx.cancel if ctx is not None else None):
            return inherited(self, *args, **kwargs)

    call.scoped = True  # the pin reads this
    return call


assert not (set(_SCOPED) & set(vars(RemoteCachedStore))), (
    "these would be replaced by their inherited versions rather than wrapped: "
    f"{sorted(set(_SCOPED) & set(vars(RemoteCachedStore)))}"
)
for _name in _SCOPED:
    setattr(RemoteCachedStore, _name, _scoped(_name))
