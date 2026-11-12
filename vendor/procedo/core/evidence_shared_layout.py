"""``layout: shared`` — immutable run generations — written once, over a :class:`TreeIO`.

::

    <root>/<board>/results/<test_id>/runs/<run_id>/…          + manifest.json (LAST)
                                      /<run_id>.deleted.json  in the trash, or purged (below)
                                 /current.json                {"run_id","ended","machine_id"}
          /<board>/entries/000001.json…                       the board's lot and fields
          /<board>/reports/<name>.html + <name>.json
                          /.generations/<name>/<n>/…          a report's earlier generations
          /exports/<name> + <name>.meta.json                  (project-level)
                  /.generations/<name>/<n>/…                  an export's earlier generations
          /.evidence-project.json                             the folder's owner (full project_id)
          /.trash/<entry>/…                                   what a Delete moved (:mod:`evidence_trash`;
                                                              a deleted item's tombstone beside it)

``<test_id>`` is the test's hidden id (a lower-case UUID, ``test_format.TEST_ID``), never its
name: every ``test_id`` a call takes is that id (:func:`evidence_store.check_test_id`), so a test
renamed keeps its runs and its current-run marks where they are, and a test made again under a
deleted one's name starts with none. Each run records the name its test had when it ran
(its manifest's ``test``).

``<board>`` is the board's KEY as folders (:mod:`evidence_board`): ``<serial>`` for the
"no part number" board — every board recorded before the board identity — and
``parts/<part number>/<revision>/<serial>`` for any other, each name escaped (``%XX``) so
that any character it holds reads back as typed. The ``board`` every call takes IS that
key. A serial named like a folder the store keeps at the root (``parts``, ``index``…) is
escaped (``%70arts``), so no bare serial can be one; the runs saved with no serial are
``<root>/_unsorted`` (``evidence_board.NO_SERIAL``).

``<root>`` is the configured evidence folder ITSELF, exactly as the user typed it
(2026-09-19): the store imposes no folder name of its own, and the ownership marker,
not the folder's name, says which project the evidence belongs to. A folder marked by
a DIFFERENT project is refused; an unmarked one is adopted and marked on first write.
The marker itself is :mod:`evidence_owner` — both path mappers here (``_board_dir``,
``_artifact_root``) prove ownership through it, so a read, a delete and a listing are
refused exactly as a write is.

A generation without ``manifest.json`` is incomplete and invisible. The pointer
advances iff the run's ``ended`` is strictly newer than the pointer's — ties and
older runs are no-ops whatever machine they come from; ``promote_run`` is the
manual escape; those primitives over a tree (manifest, pointer, annotation) are
:mod:`evidence_shared_manifest`. :class:`SharedLayout` builds paths on the store's
``root`` and does every store read and write through ``self.io``
(``tests/test_evidence_tree_seam.py``);
a concrete store (``evidence_store_fs_shared.FilesystemStore``,
``evidence_store_ftps.FtpsStore``) supplies the
tree, the creation of folders below the root (``_mkdir_safe``), ``local_path`` and
``check_access``. EVERY root — a local disk, a share, a server — is fronted by the
durable outbox (:mod:`evidence_outbox`) for runs AND artifacts: a write lands on this
PC and the flush is the only writer of new runs and artifacts. That local half
(``stage`` / ``commit_run`` / ``write_run``, ``write_artifact``, the outbox calls) is
:class:`evidence_local_staging.LocalStaging`, which :class:`SharedLayout` builds on.
"""

from __future__ import annotations

import json
import logging
import threading
import unicodedata
from collections.abc import Callable, Collection, Mapping
from dataclasses import replace
from pathlib import Path, PurePath
from typing import Any

from . import evidence_artifact_generations, evidence_index, evidence_trash
from .evidence_board import PARTS, SEPARATOR, is_canonical, is_keyed, segments
from .evidence_local_staging import LocalStaging
from .evidence_owner import MARKER, check_owner, mkdir_owned
from .evidence_search import INDEX_RUN_FILTERS, Filters, ResultRow, answer, refuse_unanswered
from .evidence_search_choices import INDEX_CHOICES, Choices, choices_of, refuse_readings
from .evidence_shared_manifest import (
    BOARD_ENTRIES,
    POINTER,
    _generation_dirs,
    _manifest,
    _manifests,
    _pointer_of,
    _read_pointer,
    add_board_entries,
    add_entries,
    annotation_of,
    read_board_entries,
    read_entries,
    run_info,
)
from .evidence_store import (
    CURRENT_RUN,
    NEWEST,
    RESULT_FILE,
    ArtifactInfo,
    EvidenceError,
    OpContext,
    RunInfo,
    check_test_id,
    guard_component,
    no_page,
    safe_component,
)
from .evidence_store_fs import _format_mtime, _sidecars, os_error
from .evidence_trash import Deletion, TrashItem
from .evidence_tree import (
    TreeIO,
    lock_wait_s,
    match_children,
    read_json,
    read_text,
    resolve_child,
    tree_lock,
)

log = logging.getLogger(__name__)

CONVERSIONS = ".conversions.json"  # the Move's pass record
_PROBE = ".evidence_probe.tmp"  # ``check_access`` writes this and its ``.tmp2`` rename
#: What the store owns beside the boards. Since the evidence folder IS the configured
#: root (2026-09-19), these names and every board share ONE directory level, so the
#: reserved set had to grow to everything the store writes there: the index's own
#: folder (:mod:`evidence_index`, named there so the reserved name and the writer
#: agree), the Move's pass record, the probe files and the evidence trash
#: (:mod:`evidence_trash`, whose runs and reports no listing may reach), and the folder
#: of the boards with a part number or a revision (:mod:`evidence_board`).
_RESERVED_SIBLINGS = frozenset(
    {
        "exports",
        evidence_index.INDEX,
        CONVERSIONS,
        MARKER,
        _PROBE,
        _PROBE + "2",
        evidence_trash.TRASH,
        PARTS,
    }
)
_RESERVED_FOLDED = frozenset(n.casefold() for n in _RESERVED_SIBLINGS)
#: ``.conversion-<id8>.lock`` — the Move's own lock FOLDER, which sits at the evidence
#: root while a Move runs and would otherwise list as a board.
_LOCK_PREFIX, _LOCK_SUFFIX = ".conversion-", ".lock"
#: ``evidence_tree._reclaim`` renames an abandoned lock ``<name>.<tag>.<hex>.abandoned``
#: and KEEPS it when the removal fails — at the evidence root that residue is a
#: directory beside the boards, so it is the store's too.
_ABANDONED = ".abandoned"


def _is_conversion_lock(name: str) -> bool:
    """The Move's lock folder, and what a failed reclaim leaves of one."""
    if not name.startswith(_LOCK_PREFIX):
        return False
    return name.endswith(_LOCK_SUFFIX) or (name.endswith(_ABANDONED) and _LOCK_SUFFIX in name)


def is_reserved(name: str) -> bool:
    """An entry the store owns at the evidence root, by EXACT name — as HEAD.

    **The index is a cache and yields to real evidence.** A board that already exists
    and holds results keeps resolving, opening and listing exactly as it did before
    (``INDEX`` and ``Index`` are ordinary board names here), because "visible but
    unopenable" would be worse than HEAD, not better. Where such a board collides with
    this folder on a case-insensitive store, :mod:`evidence_index` disables itself for
    that store rather than writing into somebody's evidence.
    """
    return name in _RESERVED_SIBLINGS or _is_conversion_lock(name)


def would_collide(name: str) -> bool:
    """A board name that may not be CREATED: it folds to one the store owns.

    The one place the case-insensitive rule belongs. A *new* board called ``INDEX``
    would be the store's own bookkeeping folder on any case-insensitive filesystem, so
    it is refused on every backend — while nothing that already exists is disturbed,
    and so the two rules never meet. Asked where a board is created
    (:func:`board_folder_to_write`: the push, a board's lot saved), and by
    :func:`evidence_board.key_of`, which escapes such a serial so that any serial is a board.
    """
    folded = name.casefold()
    return folded in _RESERVED_FOLDED or _is_conversion_lock(folded)


def check_new_key(key: str) -> None:
    """Refuse (``invalid``) a board key no NEW run or artifact may be written under: each
    of its folders must pass the rules of a component about to be created
    (:func:`evidence_store.safe_component`), and the key must be the one
    :func:`evidence_board.key_of` gives its board (its names escaped), in either Unicode form
    (a folder is made in NFC, ``evidence_tree.resolve_child``). Asked where a write is staged
    (``stage``, ``stage_artifact``, ``queue_annotation``). Whether a bare serial is a name
    the store owns stays with each caller (:func:`is_reserved`, :func:`would_collide`):
    ``parts`` and the escaped part number and revision are not boards, so that rule does not
    apply to them, and the case collisions inside ``parts/`` are refused by the push that
    makes the folder (``evidence_tree.make_dirs``)."""
    names = segments(key)
    if not is_canonical(unicodedata.normalize("NFC", key)):
        raise EvidenceError("invalid", f"{key!r} is not the key of a board", bad_key=True)
    for name in names[1:] or names:
        safe_component(name)


def board_folder(io: TreeIO, ns: PurePath, key: str) -> PurePath:
    """A board's folder below the evidence folder *ns*, each folder in the spelling that
    already exists there (:func:`evidence_tree.resolve_child`, folder by folder, so a part
    number, a revision and a serial follow the serial's case rules). ``parts`` is the
    store's own folder, spelled exactly: a key read back from a resolved folder must start
    with it. A bare serial naming a folder the store owns is refused."""
    first, *rest = segments(key)
    if rest:
        folder = ns / PARTS
        for name in rest:
            folder = resolve_child(io, folder, name)
        return folder
    folder = resolve_child(io, ns, first)
    if is_reserved(folder.name):
        raise EvidenceError(
            "invalid", f"{key!r} is a reserved folder in the shared layout", bad_key=True
        )
    return folder


def board_folder_to_write(io: TreeIO, ns: PurePath, key: str) -> PurePath:
    """:func:`board_folder` of a board about to receive something, which may create it: a
    NEW bare board whose name folds to one the store owns (``INDEX``) is refused
    (:func:`would_collide`), while an existing board of that name keeps receiving. A keyed
    board lives below ``parts/``, where the store owns no name."""
    folder = board_folder(io, ns, key)
    if not is_keyed(key) and would_collide(folder.name) and not io.is_dir(folder):
        raise EvidenceError(
            "invalid",
            f"{key!r} would collide with a reserved folder in the shared layout",
            bad_key=True,
        )
    return folder


# ---------------------------------------------------------------------------
# Helpers over the store
# ---------------------------------------------------------------------------


def pointer_of_test(store: SharedLayout, project_id: str, board: str, test_id: str) -> dict | None:
    """The test's pointer as a push will meet it, read through the store's tree."""
    try:
        return _read_pointer(store.io, store._test_dir(project_id, board, test_id))
    except OSError as exc:
        raise os_error(exc) from exc


#: ``ctx.memo`` slots of one read of a test's runs, each ``(slot, project, board, test)`` ->
#: run id -> manifest: the manifests an earlier read listed (:func:`know_runs`), and those this
#: operation's listing found (:func:`listed_runs`), which the reads after it in the same
#: operation (``current_run``, ``read_run``, ``read_run_annotation``) use instead of opening
#: the manifest again.
_KNOWN, _LISTED = "shared_layout.known", "shared_layout.listed"


def know_runs(
    ctx: OpContext, project_id: str, board: str, test_id: str, manifests: Mapping[str, dict]
) -> None:
    """Hand this operation the manifests an earlier read of the test listed: its listing opens
    none of those again (a committed generation never changes); it still lists the folder,
    so a run deleted since is gone and a new one is read."""
    ctx.memo[(_KNOWN, project_id, board, test_id)] = manifests


def listed_runs(
    ctx: OpContext | None, project_id: str, board: str, test_id: str
) -> dict[str, dict]:
    """Run id -> manifest of the committed runs this operation's listing of the test found;
    empty when it did not list them."""
    return ctx.memo.get((_LISTED, project_id, board, test_id), {}) if ctx is not None else {}


#: Files of an export folder that are not exports: a sidecar, a part, a deleted name's
#: tombstone (:func:`evidence_trash.tombstone`).
_NOT_ARTIFACTS = (".meta.json", ".part", evidence_trash.TOMBSTONE)


def _deleted(io: TreeIO, place: PurePath) -> bool:
    """Whether the run or artifact once at *place* is in the evidence trash now (deleted,
    not purged): its tombstone says so."""
    tomb = evidence_trash.tombstone_of(io, place)
    return bool(tomb) and not tomb.get(evidence_trash.PURGED_AT)


def _mtime_text(io: TreeIO, path: PurePath) -> str:
    try:
        return _format_mtime(io.stat(path)[1])
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# The layout
# ---------------------------------------------------------------------------


class SharedLayout(LocalStaging):
    """The shared layout's protocol over ``self.io``, on top of the local staging and
    outbox of :class:`evidence_local_staging.LocalStaging`. The concrete store provides
    ``root`` (the tree's root path), ``io``, and ``_mkdir_safe`` / ``local_path`` /
    ``check_access``; its ``__init__`` calls :meth:`_init_shared` for the rest (the
    local half's state, :meth:`LocalStaging._init_staging`, plus ``_owned``,
    ``_marked`` and ``_mark_lock``)."""

    root: PurePath
    _owned: set[str]  # :mod:`evidence_owner`'s per-instance proof: ids proved to own ``root``
    _marked: set[str]  # ... and whose marker it knows is already there
    # Serializes THIS store's marker write: two threads of one process share a
    # ``.part`` temp name (machine.pid), and a module-level lock made an unrelated
    # local store wait out another store's network write (measured: 2.7 s).
    _mark_lock: threading.Lock
    #: Write only into a folder whose marker names the project — never adopt an
    #: unmarked one, never write on an inconclusive read (:func:`evidence_owner.check_owner`):
    #: a store opened from an outbox entry, which no project names
    #: (``evidence_store.open_entry_store``).
    requires_marker = False

    def _init_shared(
        self,
        *,
        machine_id: str | Callable[[], str],
        project_root: Path | None,
        project_id: str,
        outbox: Path | None,
    ) -> None:
        """The layout's own state, set by every backend's ``__init__`` once its tree
        (``io``, ``root``) is built."""
        self._init_staging(
            machine_id=machine_id, project_root=project_root, project_id=project_id, outbox=outbox
        )
        self._owned = set()  # ids whose ownership of the root is proved
        self._marked = set()  # ... and whose marker is known to be there
        self._mark_lock = threading.Lock()  # serializes THIS store's marker write

    def latency_class(self) -> str:
        return self.io.latency_class()

    def evidence_form(self) -> Any:
        """This store's evidence as the moves' common form: the tree itself
        (:mod:`evidence_form_tree`)."""
        from .evidence_form_tree import TreeForm

        return TreeForm(self)

    # -- the evidence folder + path mapping -------------------------------------------

    def _namespace(self, project_id: str) -> PurePath:
        """This project's evidence folder: the configured root ITSELF, exactly as the
        user typed it. The store imposes no folder name of its own (2026-09-19) — it
        records the project's FULL id in :data:`MARKER` at the root instead. Pure path
        mapping, no I/O: :func:`evidence_owner.check_owner` reads that marker, from the
        two mappers below that every operation goes through."""
        if not project_id:
            raise EvidenceError("invalid", "the shared layout needs a project_id")
        return self.root

    def _board_dir(self, project_id: str, board: str) -> PurePath:
        """A board's folder (*board* is its key, :func:`board_folder`) — and one of the
        two doors ownership is proved at, so a read, a delete and a listing are refused
        exactly as a write is. The proof is cached per store instance
        (:func:`evidence_owner.check_owner`), so putting it here costs one round trip for
        the store's life, not one per mapping."""
        check_owner(self, project_id)
        return board_folder(self.io, self._namespace(project_id), board)

    def _test_dir(self, project_id: str, board: str, test_id: str | None) -> PurePath:
        """``<board>/results/<test_id>``: the parent of the test's ``runs/``; *test_id* is the test's
        hidden id, refused (``invalid``) before anything is read when it is not one."""
        return self._board_dir(project_id, board) / "results" / check_test_id(test_id)

    def _run_dir(
        self, project_id: str, board: str, test_id: str | None, run_id: str | None = None
    ) -> PurePath:
        test_dir = self._test_dir(project_id, board, test_id)
        if run_id in (None, CURRENT_RUN):
            m = self._current_manifest(test_dir)
            if m is None:
                raise EvidenceError("not_found", f"no current run for {board}/{test_id}")
            run_id = m["run_id"]
        return test_dir / "runs" / guard_component(run_id)

    def _artifact_root(self, kind: str, project_id: str, board: str) -> PurePath:
        """The other door ownership is proved at — ``exports/`` reaches the evidence
        folder without passing a board, so it has to prove it for itself."""
        if kind in ("report", "draft"):
            return self._board_dir(project_id, board) / "reports"
        if kind == "export":
            check_owner(self, project_id)
            return self._namespace(project_id) / "exports"
        raise EvidenceError("invalid", f"unknown artifact kind: {kind!r}")

    def _current_manifest(
        self, test_dir: PurePath, listed: Mapping[str, dict] | None = None
    ) -> dict | None:
        """The pointer's generation. ``{"run_id": null}`` is "not run"; a missing,
        unreadable or dangling pointer falls back to the newest complete run by
        ``ended`` — which is what recovers a commit interrupted between the manifest
        and the pointer, on any bench, without a second record to consult. ``listed``:
        the generations this operation's listing found (:func:`listed_runs`), so none
        is opened again."""
        ptr = _read_pointer(self.io, test_dir)
        if ptr and "run_id" in ptr:
            if ptr["run_id"] is None:
                return None
            run_dir = test_dir / "runs" / guard_component(ptr["run_id"])
            m = listed.get(run_dir.name) if listed else _manifest(self.io, run_dir)
            if m:
                return m
            log.warning("%s points at a missing generation %s", test_dir / POINTER, ptr["run_id"])
        found = list(listed.values()) if listed else _manifests(self.io, test_dir)
        complete = (m for m in found if m.get("complete"))
        return max(complete, key=lambda m: (m.get("ended", ""), m["run_id"]), default=None)

    # -- artifacts (written through the outbox, :class:`LocalStaging`) ------------------

    def read_artifact(
        self, kind: str, project_id: str, board: str, name: str, ctx: OpContext | None = None
    ) -> bytes:
        """A write still waiting in the outbox is readable by its writer."""
        from . import evidence_outbox

        staged = evidence_outbox.pending_artifact(self, kind, project_id, board, name)
        if staged is not None:
            try:
                return staged.read_bytes()  # the local outbox copy
            except FileNotFoundError:
                pass  # a flush pushed it while we looked: read the pushed copy below
            except OSError as exc:
                raise os_error(exc) from exc
        p = self._artifact_root(kind, project_id, board) / guard_component(name)
        try:
            return self._payload(kind, p)
        except FileNotFoundError:
            raise EvidenceError("not_found", f"no {kind} {name!r} for {board}") from None
        except OSError as exc:
            raise os_error(exc) from exc

    def _payload(self, kind: str, p: PurePath) -> bytes:
        """The file at *p*; a report there as its record alone (contract §5.10) is
        :func:`evidence_store.no_page`, as on every storage."""
        try:
            return self.io.read_bytes(p)
        except FileNotFoundError:
            if kind == "report" and self.io.is_file(_sidecars(kind, p)[0]):
                raise no_page(p.name) from None
            raise

    def _report_info(self, html: PurePath) -> ArtifactInfo:
        """The row a report's sidecar stands for. The rule itself lives in
        :func:`evidence_index.info_of_report`, so the listing that reads a sidecar and
        the index record written from the same bytes cannot drift apart."""
        info = evidence_index.info_of_report(
            html.name, read_json(self.io, html.with_suffix(".json"))
        )
        return info if info.generated else replace(info, generated=_mtime_text(self.io, html))

    def list_artifacts(
        self, kind: str, project_id: str, board: str, ctx: OpContext | None = None
    ) -> list[ArtifactInfo]:
        """A report listing comes through the index (:mod:`evidence_index`): the
        shards, reconciled against ONE listing of the folder, so only an artifact
        the shards do not already describe costs its sidecar read. An export or a
        draft listing reads no sidecar — the rows ARE the listing — so there is
        nothing to cache and they stay direct. Every kind by name, by case folding
        (:func:`evidence_index.by_name`)."""
        d = self._artifact_root(kind, project_id, board)
        try:
            if kind in evidence_index.KINDS:
                return evidence_index.reconcile(self, kind, project_id, board, d, ctx)
            if not self.io.is_dir(d):
                return []
            if kind == "draft":
                files = match_children(self.io, d, "*.draft.json")
            else:
                files = [d / n for n in self.io.list_names(d) or []]
            return [
                self._file_info(p)
                for p in evidence_index.by_name(files)
                if self.io.is_file(p) and not p.name.endswith(_NOT_ARTIFACTS)
            ]
        except OSError as exc:
            raise os_error(exc) from exc

    def _file_info(self, path: PurePath) -> ArtifactInfo:
        """The row of an artifact whose listing reads no sidecar (an export, a draft)."""
        return ArtifactInfo(path.name, _mtime_text(self.io, path), "", "", "", "")

    def _generations(
        self, kind: str, project_id: str, board: str, name: str
    ) -> list[tuple[int, PurePath]]:
        """``(number, payload)`` of every generation of a report or an export, oldest first
        (:func:`evidence_artifact_generations.listing`)."""
        if kind not in evidence_artifact_generations.KEPT:
            raise EvidenceError("invalid", f"a {kind} keeps no generations")
        root = self._artifact_root(kind, project_id, board)
        return evidence_artifact_generations.listing(self.io, root, guard_component(name), kind)

    def list_generations(
        self, kind: str, project_id: str, board: str, name: str, ctx: OpContext | None = None
    ) -> list[tuple[int, ArtifactInfo]]:
        """Every generation of a report or an export sent under ``name``, oldest first, each
        with its row; the last is the newest, what :meth:`read_artifact` reads. ``[]``: the
        artifact is not on the store."""
        try:
            return [
                (n, self._report_info(p) if kind == "report" else self._file_info(p))
                for n, p in self._generations(kind, project_id, board, name)
            ]
        except OSError as exc:
            raise os_error(exc) from exc

    def read_generation(
        self,
        kind: str,
        project_id: str,
        board: str,
        name: str,
        generation: int,
        ctx: OpContext | None = None,
    ) -> bytes:
        """The payload of one generation (:meth:`list_generations`' numbers)."""
        try:
            for n, p in self._generations(kind, project_id, board, name):
                if n == generation:
                    return self._payload(kind, p)
        except OSError as exc:
            raise os_error(exc) from exc
        raise EvidenceError("not_found", f"no generation {generation} of {kind} {name!r}")

    def delete_artifact(
        self,
        kind: str,
        project_id: str,
        board: str,
        name: str,
        ctx: OpContext | None = None,
        *,
        deletion: Deletion,
    ) -> None:
        """Move the payload and its sidecars into the evidence trash, with *deletion*'s
        reason (:mod:`evidence_trash`), and a report's or an export's earlier generations
        with them. An already-absent artifact is a no-op, but a REFUSED move is reported:
        answering "deleted" to a file the server kept made the browser re-list it, so the
        user saw a delete that silently did nothing."""
        evidence_trash.require_reason(deletion)
        root = self._artifact_root(kind, project_id, board)
        p = root / guard_component(name)
        try:
            present = [f for f in (p, *_sidecars(kind, p)) if self.io.is_file(f)]
            earlier = evidence_artifact_generations.folder_of(root, p.name)
            if kind in evidence_artifact_generations.KEPT and self.io.is_dir(earlier):
                present.append(earlier)
            if present:
                evidence_trash.move_in(
                    self,
                    kind,
                    present,
                    deletion,
                    project_id=project_id,
                    board=board,
                    place=p,
                    name=name,
                )
        except OSError as exc:
            raise os_error(exc) from exc

    # -- boards -----------------------------------------------------------------------

    def list_boards(self, project_id: str, ctx: OpContext | None = None) -> list[str]:
        """The project's boards, by key: the bare serials, and the boards under ``parts/``.
        The door the GUI opens a project through, so this is where a folder another
        project marked is refused: listing somebody else's boards as this project's is
        the harm the marker exists to prevent."""
        check_owner(self, project_id)
        ns = self._namespace(project_id)
        try:
            names = self.io.list_names(ns)
            if names is None:
                return []
            found = [n for n in names if self.io.is_dir(ns / n) and not is_reserved(n)]
            if PARTS in names and self.io.is_dir(ns / PARTS):
                found += self._keyed_boards(ns / PARTS)
            return sorted(found)
        except OSError as exc:
            raise os_error(exc) from exc

    def _keyed_boards(self, parts: PurePath) -> list[str]:
        """The keys of the boards under ``parts/<part number>/<revision>/<serial>``, built
        from the folders' names (already escaped). Three listings deep, walked only where
        ``parts`` exists, so a folder of bare serials costs nothing more; a refused
        listing raises, as the root's does."""
        io = self.io
        found = []
        for pn in io.list_names(parts) or []:
            if not io.is_dir(parts / pn):
                continue
            for rev in io.list_names(parts / pn) or []:
                folder = parts / pn / rev
                if not io.is_dir(folder):
                    continue
                for serial in io.list_names(folder) or []:
                    if io.is_dir(folder / serial):
                        found.append(SEPARATOR.join((PARTS, pn, rev, serial)))
        return found

    # -- runs -------------------------------------------------------------------------

    def list_runs(
        self, project_id: str, board: str, test_id: str, ctx: OpContext | None = None
    ) -> list[RunInfo]:
        """The test's runs; what they were listed with is kept for the rest of the operation
        (:func:`listed_runs`), and a manifest :func:`know_runs` handed in is not opened."""
        slot = (project_id, board, test_id)
        known = ctx.memo.get((_KNOWN, *slot)) if ctx is not None else None
        try:
            found = _manifests(self.io, self._test_dir(*slot), known)
        except OSError as exc:  # a refused listing or manifest read is reported, not empty
            raise os_error(exc) from exc
        if ctx is not None:
            ctx.memo[(_LISTED, *slot)] = {m["run_id"]: m for m in found}
        return [run_info(m) for m in found]

    def current_run(
        self, project_id: str, board: str, test_id: str, ctx: OpContext | None = None
    ) -> RunInfo | None:
        try:
            m = self._current_manifest(
                self._test_dir(project_id, board, test_id),
                listed_runs(ctx, project_id, board, test_id),
            )
        except OSError as exc:
            raise os_error(exc) from exc
        return run_info(m) if m else None

    def _run_manifest(
        self, run_dir: PurePath, ctx: OpContext | None, project_id: str, board: str, test_id: str
    ) -> dict | None:
        """The generation's manifest: the one this operation's listing found, else read."""
        return listed_runs(ctx, project_id, board, test_id).get(run_dir.name) or _manifest(
            self.io, run_dir
        )

    def read_run(
        self, project_id: str, board: str, test_id: str, run_id: str, ctx: OpContext | None = None
    ) -> dict:
        d = self._run_dir(project_id, board, test_id, run_id)
        f = d / RESULT_FILE
        try:
            m = self._run_manifest(d, ctx, project_id, board, test_id)
            missing = m is None or not self.io.is_file(f)
        except OSError as exc:
            raise os_error(exc) from exc
        if missing:
            raise EvidenceError("not_found", f"no result for {board}/{test_id}/{run_id}")
        try:
            return json.loads(read_text(self.io, f))
        except (OSError, ValueError) as exc:
            raise EvidenceError("corrupt", f"unreadable result {f}: {exc}") from exc

    # -- run history ------------------------------------------------------------------

    def delete_run(
        self,
        project_id: str,
        board: str,
        test_id: str,
        run_id: str,
        ctx: OpContext | None = None,
        *,
        deletion: Deletion,
    ) -> None:
        """Move the generation into the evidence trash, with *deletion*'s reason
        (:mod:`evidence_trash`); a moved current run hands the pointer to the newest
        remaining complete run, or removes it. A run deleted already is done and stays as
        it is, its first reason kept (a replay, ``data_format.md`` §6.7)."""
        evidence_trash.require_reason(deletion)
        test_dir = self._test_dir(project_id, board, test_id)
        try:
            if not self.io.is_dir(test_dir):
                raise EvidenceError("not_found", f"run not found: {board}/{test_id}/{run_id}")
            with tree_lock(
                self.io,
                test_dir / ".pointer.lock",
                self.tag,
                timeout_s=lock_wait_s(ctx),
                cancel=ctx.cancel if ctx else None,
                ctx=ctx,
            ):
                d = self._run_dir(project_id, board, test_id, run_id)
                if not self.io.is_dir(d):
                    if _deleted(self.io, d):
                        return
                    raise EvidenceError("not_found", f"run not found: {d}")
                was_current = (_read_pointer(self.io, test_dir) or {}).get("run_id") == d.name
                evidence_trash.move_in(
                    self,
                    "run",
                    [d],
                    deletion,
                    project_id=project_id,
                    board=board,
                    place=d,
                    test_id=test_id,
                    name=d.name,
                    was_current=was_current,
                )
                if was_current:
                    complete = [m for m in _manifests(self.io, test_dir) if m.get("complete")]
                    newest = max(
                        complete, key=lambda m: (m.get("ended", ""), m["run_id"]), default=None
                    )
                    if newest:
                        self.io.write_json(
                            test_dir / POINTER, _pointer_of(run_info(newest)), self.tag
                        )
                    else:
                        self.io.unlink(test_dir / POINTER, missing_ok=True)
        except OSError as exc:
            raise os_error(exc) from exc

    def promote_run(
        self, project_id: str, board: str, test_id: str, run_id: str, ctx: OpContext | None = None
    ) -> None:
        """Pointer := this generation (the manual escape from the advance rule). A pointer
        naming it already is left as it is: nothing is written (a replay, decision 43)."""
        test_dir = self._test_dir(project_id, board, test_id)
        try:
            if not self.io.is_dir(test_dir):
                raise EvidenceError("not_found", f"run not found: {board}/{test_id}/{run_id}")
            with tree_lock(
                self.io,
                test_dir / ".pointer.lock",
                self.tag,
                timeout_s=lock_wait_s(ctx),
                cancel=ctx.cancel if ctx else None,
                ctx=ctx,
            ):
                m = _manifest(self.io, test_dir / "runs" / guard_component(run_id))
                if m is None:
                    raise EvidenceError("not_found", f"run not found: {board}/{test_id}/{run_id}")
                if not m.get("complete"):
                    raise EvidenceError(
                        "invalid", f"{run_id} has no {RESULT_FILE} and cannot be current"
                    )
                if (_read_pointer(self.io, test_dir) or {}).get("run_id") == m["run_id"]:
                    return
                self.io.write_json(test_dir / POINTER, _pointer_of(run_info(m)), self.tag)
        except OSError as exc:
            raise os_error(exc) from exc

    def demote_run(
        self, project_id: str, board: str, test_id: str, ctx: OpContext | None = None
    ) -> str | None:
        """ "Mark as not run": pointer := explicit none, ``ended`` kept so only a
        genuinely newer run revives the test; generations untouched."""
        test_dir = self._test_dir(project_id, board, test_id)
        try:
            if not self.io.is_dir(test_dir):
                return None
            with tree_lock(
                self.io,
                test_dir / ".pointer.lock",
                self.tag,
                timeout_s=lock_wait_s(ctx),
                cancel=ctx.cancel if ctx else None,
                ctx=ctx,
            ):
                m = self._current_manifest(test_dir)
                if m is None:
                    return None
                self.io.write_json(
                    test_dir / POINTER,
                    {"run_id": None, "ended": m.get("ended", ""), "machine_id": self.machine_id},
                    self.tag,
                )
        except OSError as exc:
            raise os_error(exc) from exc
        return m["run_id"]

    # -- the evidence trash (:mod:`evidence_trash`) ------------------------------------
    # It sits at the evidence folder without a board or an artifact root to map through,
    # so each call proves ownership itself.

    def list_trash(self, project_id: str, ctx: OpContext | None = None) -> list[TrashItem]:
        """What the trash holds, newest deletion first."""
        check_owner(self, project_id)
        try:
            return evidence_trash.listing(self)
        except OSError as exc:
            raise os_error(exc) from exc

    # -- search (the index shards, read through the tree) ---------------------------------

    #: A folder's index records each run's operator and bench, never its measurements.
    SEARCH_RUN_FILTERS: frozenset[str] = INDEX_RUN_FILTERS

    def search(
        self,
        project_id: str,
        filters: Filters,
        ctx: OpContext | None = None,
        *,
        limit: int = NEWEST,
    ) -> list[ResultRow]:
        """The project's reports and runs the filters name, the newest *limit*: its index
        shards read through the tree (on a server through this PC's copy of them,
        :mod:`evidence_shard_cache`) and answered by the shards' own rules
        (:func:`evidence_search.answer`). A row's ``folder`` is the root: a ``Path`` of this
        PC for a folder or a share, the tree's identity for a server."""
        from . import evidence_shard_cache

        refuse_unanswered(self, filters)
        check_owner(self, project_id)
        found = evidence_shard_cache.shards(self, project_id, ctx)
        where = self.root if isinstance(self.root, Path) else self.io.identity
        return answer(where, found.records.values(), filters, limit)

    #: What its index proposes: every field but a test's readings.
    SEARCH_CHOICES: frozenset[str] = INDEX_CHOICES

    def search_choices(self, project_id: str, ctx: OpContext | None = None) -> Choices:
        """What the search's fields propose: the index shards, as :meth:`search` reads them
        (on a server through this PC's copy, its tail fetched), the items not deleted."""
        from . import evidence_shard_cache

        check_owner(self, project_id)
        return choices_of(evidence_shard_cache.shards(self, project_id, ctx).records.values())

    def search_readings(
        self, project_id: str, test: str, ctx: OpContext | None = None
    ) -> list[tuple[str, str]]:
        """Refused (``invalid``): a folder's index records no reading."""
        refuse_readings(self)
        return []

    def restore_trash(self, project_id: str, entry: str, ctx: OpContext | None = None) -> bool:
        """Put *entry* back where it was; whether it is the Current run again."""
        check_owner(self, project_id)
        try:
            return evidence_trash.restore(self, entry, ctx)
        except OSError as exc:
            raise os_error(exc) from exc

    def purge_trash(
        self,
        project_id: str,
        entries: Collection[str] = (),
        ctx: OpContext | None = None,
        *,
        now: float | None = None,
    ) -> list[str]:
        """Remove for good *entries* and every expired entry; the entries removed."""
        check_owner(self, project_id)
        try:
            return evidence_trash.purge(self, entries, now=now)
        except OSError as exc:
            raise os_error(exc) from exc

    # -- annotations (``annotations/`` in the generation, one file per entry; the manifest
    # owns the execution block and is merged in on read) --------------------------------

    def read_run_annotation(
        self, project_id: str, board: str, test_id: str, run_id: str, ctx: OpContext | None = None
    ) -> dict | None:
        d = self._run_dir(project_id, board, test_id, run_id)
        try:
            m = self._run_manifest(d, ctx, project_id, board, test_id)
            if m is None:
                return None
            return annotation_of(m, read_entries(self.io, d))
        except OSError as exc:
            raise os_error(exc) from exc

    def add_run_annotation_entries(
        self,
        project_id: str,
        board: str,
        test_id: str,
        run_id: str,
        entries: list[dict],
        ctx: OpContext | None = None,
    ) -> list[dict]:
        """A run a manager deleted takes no entry (``deleted``, decision 57); its entries come
        back with it on a restore."""
        d = self._run_dir(project_id, board, test_id, run_id)
        try:
            known = _manifest(self.io, d) is not None
            deleted = not known and _deleted(self.io, d)
        except OSError as exc:
            raise os_error(exc) from exc
        if deleted:
            raise evidence_trash.refused_deleted(f"the run {d.name}")
        if not known:
            raise EvidenceError("not_found", f"run not found: {board}/{test_id}/{run_id}")
        try:
            return add_entries(self.io, d, entries, self.tag, ctx)
        except OSError as exc:
            raise os_error(exc) from exc

    # -- a board's lot and fields (``entries/`` in the board's folder, one file per entry) --

    def read_board_entries(
        self, project_id: str, board: str, ctx: OpContext | None = None
    ) -> list[dict]:
        """The board's lot and fields as entries, in the order received; ``[]`` when none
        was saved (or the board is not on the store)."""
        try:
            return read_board_entries(self.io, self._board_dir(project_id, board))
        except OSError as exc:
            raise os_error(exc) from exc

    def add_board_entries(
        self, project_id: str, board: str, entries: list[dict], ctx: OpContext | None = None
    ) -> list[dict]:
        """Add entries to the board's lot and fields, by the rule of :mod:`annotation_entries`;
        a board not on the store yet is created (a lot may be saved before the first run).
        Per entry, :func:`annotation_entries.outcome`."""
        check_new_key(board)
        check_owner(self, project_id)
        try:
            folder = board_folder_to_write(self.io, self._namespace(project_id), board)
            mkdir_owned(self, project_id, folder / BOARD_ENTRIES)
            return add_board_entries(self.io, folder, entries, self.tag, ctx)
        except OSError as exc:
            raise os_error(exc) from exc

    # -- maintenance ----------------------------------------------------------------------

    def sweep_incomplete(self, project_id: str, ctx: OpContext | None = None) -> list[str]:
        """Delete manifest-less staged dirs (store and outbox) whose ``.owner`` is not
        a live process on this machine and whose newest file is older than a day
        (:meth:`LocalStaging._sweep`).

        It deletes and it reaches the store folder without a board or an artifact root
        to map through, so it proves ownership itself: sweeping another project's
        staged runs is the same harm by a quieter door."""
        check_owner(self, project_id)
        ns = self._namespace(project_id)
        # ``_generation_dirs`` skips a store folder it cannot list, by design — the sweep
        # deletes, so it must never act on a listing it does not trust.
        try:
            candidates = (
                [(self.io, d) for d in _generation_dirs(self.io, ns)] if self.io.is_dir(ns) else []
            )
        except OSError as exc:
            raise os_error(exc) from exc
        return self._sweep([*candidates, *self._outbox_candidates()])
