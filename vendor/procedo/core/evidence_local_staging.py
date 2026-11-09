"""The local half of a store: new runs and artifacts staged in this PC's durable outbox.

``stage`` / ``commit_run`` / ``write_run`` live here rather than once per backend:
staging is LOCAL disk whatever the root — the runner hands ``StagedRun.path`` to a
subprocess, which cannot write into a connection — so they use the local-disk
primitives of :mod:`evidence_store_fs_shared` (listed in the seam guard's ``LOCAL``
exemptions with their reason, ``tests/test_evidence_tree_seam.py``), and nothing of a
commit touches the store. An artifact waits in the outbox the same way, and the flush
(:mod:`evidence_outbox`) is the only writer of new runs and artifacts.

:class:`LocalStaging` holds that half, the outbox's own calls, and the binding to the
project's configuration that every write and every push checks. It reads nothing from
the store, so a store with another layout can build on it as
:class:`evidence_shared_layout.SharedLayout` does.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Any

from .evidence_shared_manifest import MANIFEST, run_info
from .evidence_store import (
    CURRENT_RUN,
    RESULT_FILE,
    SWITCHED,
    ArtifactInfo,
    EvidenceError,
    EvidenceKey,
    OpContext,
    OutboxItem,
    RunInfo,
    StagedRun,
    check_test_id,
    safe_component,
)
from .evidence_store_fs import os_error
from .evidence_tree import TreeIO, read_json
from .process_alive import pid_alive
from .project_identity import read_project_id

log = logging.getLogger(__name__)

OWNER = ".owner"  # lease + routing record of a staged directory
STAGING_SUFFIX = ".new"  # suffix of a staged directory until its lease is in (``stage``)
#: A staged run whose writer is gone is swept once its newest file is older than this.
SWEEP_AGE_S = 24 * 3600


def _owner_record(store: LocalStaging, key: EvidenceKey) -> dict:
    """The staged directory's lease — who may still be writing into it, so the sweep
    never deletes a live run — plus the routing the outbox flush reads back: the run's board,
    and its test's hidden id, which the key names (:meth:`LocalStaging.stage` checked it)."""
    from .evidence_store_fs_shared import _iso, _utc_now

    return {
        "machine_id": store.machine_id,
        "pid": os.getpid(),
        "started": _iso(_utc_now()),
        **destination(store),
        "project_id": key.project_id,
        "serial": key.board,
        "test_id": key.test_id,
    }


def lease_alive(owner: dict, machine_id: str) -> bool:
    """A staged directory's lease (:func:`_owner_record`) is held by a process that still
    runs on this machine (``machine_id``) — it may still be writing into it."""
    return owner.get("machine_id") == machine_id and pid_alive(owner.get("pid", 0))


def destination(store: LocalStaging) -> dict:
    """Where an outbox entry queued through ``store`` is sent, as the entry records it:
    the store's identity (what lists it, ``evidence_outbox_route.routed_to``), the
    ``config.json`` entry the store was opened from (``None``: not opened from one),
    which opens it again when no open project uses it, and the project folder that
    queued it (``""``: none), whose configuration decides where it goes
    (:func:`evidence_outbox_route.route`). Never a secret: a server's credential is
    filed on this PC under the store's endpoint, not in the entry."""
    return {"root": store.io.identity, "store": store.spec, "project_folder": queued_from(store)}


def queued_from(store: LocalStaging) -> str:
    """The project folder ``store`` was opened for, as an outbox entry records it
    (``""``: none)."""
    return str(store.project_root) if store.project_root is not None else ""


def configured_here(store: LocalStaging) -> bool:
    """Whether the project's ``config.json`` still names ``store``. A config that
    cannot be read raises (``unreachable``): it is not a re-pointed store, so a write
    waits — a push stops with its entries still queued — instead of being refused as
    switched."""
    from .evidence_config import load_config

    if store.project_root is None:
        return False
    spec = load_config(store.project_root).get("evidence_store", {})
    return isinstance(spec, dict) and spec.get("backend") == store.ID and store._names_this(spec)


class LocalStaging:
    """Staging, the outbox and the configuration binding of a store. The concrete store
    provides ``io`` (whose ``identity`` an outbox entry is routed by and whose
    ``flavour`` a payload name is checked in); its ``__init__`` calls
    :meth:`_init_staging` for the rest (``outbox`` + ``outbox_io``, the local outbox
    folder and a tree over it, ``project_root``, ``project_id``, ``_machine_id``,
    ``on_outbox_entry``, ``_staging``, ``_flushing``, ``_flush_again``)."""

    io: TreeIO
    outbox_io: TreeIO  # the local outbox folder's tree (the sweep reaches both)
    outbox: Path
    project_id: str
    _machine_id: str | Callable[[], str]
    on_outbox_entry: Callable[[], None] | None
    _staging: set[str]
    _flushing: threading.Lock
    _flush_again: bool
    ID: str  # the backend's registry name (``config.json:evidence_store.backend``)
    project_root: Path | None
    # A server store's endpoint and its entry -> identity rule (the default ``_names_this``).
    endpoint: Any
    endpoint_identity: Callable[[dict], str]
    #: Whether this store IS the project's configured one (:meth:`bind_to_config`).
    _bound_to_config = False
    #: The ``config.json`` entry it was opened from (``evidence_store.open_store``).
    spec: dict[str, Any] | None = None
    #: How a store with no tree sends one outbox entry (``(entry, payload folder, ctx)``):
    #: by its own call. ``None``: the outbox writes the entry into the store's tree itself
    #: (:func:`evidence_outbox.push`).
    send_entry: Callable[[dict, Path, OpContext | None], None] | None = None

    def _init_staging(
        self,
        *,
        machine_id: str | Callable[[], str],
        project_root: Path | None,
        project_id: str,
        outbox: Path | None,
    ) -> None:
        """The local half's own state, set by every backend's ``__init__``."""
        from .evidence_outbox import default_outbox_dir
        from .evidence_store_fs_shared import FsTree

        self._machine_id = (
            machine_id  # a callable is resolved on first use (this PC's machine-id file)
        )
        self.project_root = Path(project_root) if project_root else None  # for run_provenance
        self.project_id = project_id or (
            read_project_id(Path(project_root)) if project_root else ""
        )
        self.outbox = Path(outbox) if outbox else default_outbox_dir()
        self.outbox_io = FsTree(self.outbox)
        self.on_outbox_entry = None
        self._staging = set()  # run ids staged by this process: never swept
        self._flushing = threading.Lock()
        self._flush_again = False  # a flush asked for while another one ran

    @property
    def machine_id(self) -> str:
        if callable(self._machine_id):
            self._machine_id = self._machine_id()
        return self._machine_id

    @property
    def tag(self) -> str:
        """Temp-file discriminator: no two writers ever share a ``.part`` name."""
        return f"{self.machine_id}.{os.getpid()}"

    # -- the binding to the project's configuration -------------------------------------

    def bind_to_config(self) -> None:
        """This store IS the project's configured one — said by its opener, which built
        it from the entry it had just read: the construction's own read may meet a
        config that flickers, and the project's store then stayed unbound for life. From
        now on a write — and every outbox entry's push — refuses once the config names
        another store."""
        self._bound_to_config = True

    def check_binding(self) -> None:
        """Refuse (``switched``) once the project's ``config.json`` names another store.

        Never ``invalid``: nothing is wrong with what was being written, so a push that
        meets it stops with its entries still queued for the store the project names
        now, where ``invalid`` quarantined every remaining upload for good. A config
        that cannot be read raises ``unreachable`` (:func:`configured_here`): the
        write waits."""
        if self._bound_to_config and not configured_here(self):
            raise EvidenceError(
                SWITCHED, "The evidence folder changed; reopen the project before writing results."
            )

    def _names_this(self, spec: dict) -> bool:
        """Whether an entry of this backend opens THIS store: a server store by its
        endpoint — the identity its credential and its outbox routing are keyed by."""
        return self.endpoint_identity(spec) == self.endpoint.identity

    # -- new generations: staged in the LOCAL outbox, whatever the root ---------------

    def stage(self, key: EvidenceKey, ctx: OpContext | None = None) -> StagedRun:
        """A fresh staging directory in the durable outbox, carrying its ``.owner``
        lease + routing; invisible to readers until its push lands. Keys are validated
        here, so a bad one never enters a store or an outbox. Nothing here touches the
        root: a run starts, and its evidence is safe, while the store is out of reach.

        The directory appears under its run id WITH its lease already inside (made
        under a temporary name, then renamed): the sweep never meets a run folder with no
        lease yet. The key's test is the test's hidden id (:func:`evidence_store.check_test_id`),
        refused before anything is written when it is not one."""
        from .evidence_shared_layout import check_new_key, is_reserved
        from .evidence_store_fs_shared import _new_run_id, _utc_now, _write_json_atomic
        from .paths import rmtree

        if key.kind != "run" or key.run_id not in (None, CURRENT_RUN):
            raise EvidenceError("invalid", "only a new generation of a run key can be staged", key)
        check_new_key(key.board)  # the board's key: each of its folders
        check_test_id(key.test_id)
        if is_reserved(key.board):
            raise EvidenceError(
                "invalid", f"{key.board!r} is a reserved evidence folder", key, bad_key=True
            )
        run_id = _new_run_id(_utc_now())
        staged = self.outbox / run_id
        making = self.outbox / f"{run_id}{STAGING_SUFFIX}"
        try:
            making.mkdir(parents=True)
            _write_json_atomic(making / OWNER, _owner_record(self, key), self.tag)
            making.rename(staged)
        except OSError as exc:
            rmtree(making, ignore_errors=True)
            raise os_error(exc, key) from exc
        self._staging.add(run_id)
        return StagedRun(run_id, staged, replace(key, run_id=run_id, name=""))

    def discard_run(self, staged: StagedRun) -> None:
        """Remove a staged generation that will never be committed — its run never started —
        so nothing of it is left in this PC's outbox. It deletes, so it proves first: a folder
        of this outbox, not a link, staged by this process and never committed (no manifest,
        no outbox entry); anything else is refused and left. What a delete could not remove
        is logged, by name."""
        from . import evidence_outbox
        from .evidence_store_fs_shared import _staged_manifest
        from .paths import is_link, rmtree

        path = staged.path
        if (
            path.parent != self.outbox
            or is_link(path)
            or staged.run_id not in self._staging
            or _staged_manifest(path) is not None
            or evidence_outbox.entry_path(self, staged.run_id).exists()
        ):
            raise EvidenceError("invalid", "not a staged run that never started", staged.key)
        self._staging.discard(staged.run_id)
        rmtree(path, ignore_errors=True)
        if path.exists():
            left = sorted(p.relative_to(path).as_posix() for p in path.rglob("*"))
            log.warning("A run that never started stays in part in %s: %s", path, left)

    def commit_run(
        self, staged: StagedRun, manifest: dict, ctx: OpContext | None = None
    ) -> RunInfo:
        """Hash the payload, write the manifest LAST, then publish the outbox entry:
        the run is safe on this PC before anything reaches the store, and its push
        (files, manifest, pointer, search row) is the flush's job — which the service
        starts at once (``on_outbox_entry``). A generation without ``results.json``
        commits as incomplete and never becomes current. A replay redoes whatever step
        is missing: a manifest without its entry is not a finished commit."""
        from . import evidence_outbox
        from .evidence_store_fs_shared import (
            _local_manifest,
            _staged_manifest,
            _write_json_atomic,
        )

        key = staged.key
        try:
            found = _staged_manifest(staged.path)
            if found is None:
                found = _local_manifest(self, staged, manifest)
                _write_json_atomic(staged.path / MANIFEST, found, self.tag)
            if not evidence_outbox.entry_path(self, staged.run_id).exists():
                evidence_outbox.publish_or_quarantine(self, staged.path)
        except OSError as exc:
            raise os_error(exc, key) from exc
        self._staging.discard(staged.run_id)
        if not found["complete"]:
            log.warning(
                "Run %s/%s/%s committed without %s", key.board, key.test_id, key.run_id, RESULT_FILE
            )
        return run_info(found)

    def write_run(
        self,
        project_id: str,
        board: str,
        test_id: str,
        manifest: dict,
        files: dict[str, bytes | str],
        ctx: OpContext | None = None,
    ) -> RunInfo:
        """Stage, write every payload (fsynced), commit — all or nothing (a failed
        write removes the staged generation and raises). A name is a ``/``-separated
        path in the run (``plots/rise.png``), each part a safe component."""
        from .evidence_store_fs_shared import _write_file

        staged = self.stage(EvidenceKey(project_id, board, "run", test_id, CURRENT_RUN), ctx)
        try:
            for name, payload in files.items():
                path = staged.path.joinpath(*(safe_component(p) for p in PurePosixPath(name).parts))
                path.parent.mkdir(parents=True, exist_ok=True)
                _write_file(path, payload, self.tag)
        except (OSError, EvidenceError) as exc:
            self.discard_run(staged)
            if isinstance(exc, EvidenceError):
                raise
            raise os_error(exc, staged.key) from exc
        return self.commit_run(staged, manifest, ctx)

    # -- artifacts (through the outbox, like runs) ---------------------------------------

    def write_artifact(
        self,
        kind: str,
        project_id: str,
        board: str,
        name: str,
        data: bytes,
        info: ArtifactInfo,
        *,
        sidecar: bytes | None = None,
        replaces: str | None = None,
        ctx: OpContext | None = None,
    ) -> None:
        """Payload then sidecar (``sidecar`` verbatim, else synthesized from ``info``),
        staged in the outbox; the push lands them and records their index line. A draft
        carries the version it replaces (:mod:`evidence_draft`)."""
        from . import evidence_outbox

        if kind not in ("report", "export", "draft"):
            raise EvidenceError("invalid", f"unknown artifact kind: {kind!r}")
        try:
            evidence_outbox.stage_artifact(
                self, kind, project_id, board, name, data, info, sidecar, replaces
            )
        except OSError as exc:
            raise os_error(exc) from exc

    # -- the sweep: what a dead writer half-wrote in this PC's outbox ----------------------

    def sweep_incomplete(self, project_id: str, ctx: OpContext | None = None) -> list[str]:
        """Delete the runs staged in this PC's outbox that never committed (no manifest, no
        outbox entry) whose ``.owner`` is not a live process on this machine and whose newest
        file is older than a day (:meth:`_sweep`). A store with a tree sweeps its own
        staged folders with them (``SharedLayout.sweep_incomplete``)."""
        return self._sweep(self._outbox_candidates())

    def _outbox_candidates(self) -> list[tuple[TreeIO, Path]]:
        """Every folder of this PC's outbox but its quarantine. A listing the disk refuses
        is reported rather than read as "nothing to sweep"."""
        try:
            outboxed = self.outbox_io.list_names(self.outbox)
        except OSError as exc:
            raise os_error(exc) from exc
        return [(self.outbox_io, self.outbox / n) for n in outboxed or [] if n != "quarantine"]

    def _sweep(self, candidates: list[tuple[TreeIO, Any]]) -> list[str]:
        """Delete each candidate folder a dead writer left: never one this process is
        staging, never a published outbox entry's payload, never one whose lease a live
        process on this machine holds, never one touched in the last day."""
        cutoff = time.time() - SWEEP_AGE_S
        removed = []
        for tree, d in candidates:
            if not tree.is_dir(d) or d.name in self._staging or tree.exists(d / MANIFEST):
                continue
            if d.parent == self.outbox and tree.exists(d.with_suffix(".json")):
                continue  # an artifact waiting for its push
            owner = read_json(tree, d / OWNER)
            try:
                if lease_alive(owner, self.machine_id):
                    continue  # a sibling instance on this machine is still writing
                if tree.newest_mtime(d) > cutoff:
                    continue
                tree.rmtree(d)
            except OSError as exc:
                raise os_error(exc) from exc
            removed.append(d.name)
        return removed

    # -- outbox ---------------------------------------------------------------------------

    def pending_items(self, project_id: str, ctx: OpContext | None = None) -> list[OutboxItem]:
        from . import evidence_outbox

        return evidence_outbox.pending(self, project_id)

    def quarantined_items(self, project_id: str, ctx: OpContext | None = None) -> list[OutboxItem]:
        from . import evidence_outbox

        return evidence_outbox.quarantined(self, project_id)

    def flush_outbox(self, ctx: OpContext | None = None) -> list[str]:
        from . import evidence_outbox

        return evidence_outbox.flush(self, ctx or OpContext())
