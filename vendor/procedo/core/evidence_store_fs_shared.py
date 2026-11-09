"""Filesystem evidence backend — the shared layout on a folder (local disk or share).

:class:`FilesystemStore` is the shared layout's protocol
(:class:`evidence_shared_layout.SharedLayout`, immutable run generations) over
:class:`FsTree`, plus what only a filesystem root does: ``check_access`` probes with a
real file, and :func:`create_folder` makes the evidence folder when it is CHOSEN
(project creation, settings Apply, a Move) — never on a write: a push that meets a
missing folder waits (``not_found``, "evidence folder not found"). Like every shared-layout
store, a local disk and a share alike, it stages runs AND artifacts in the durable
outbox (:mod:`evidence_outbox`); the flush is the only writer of new runs and
artifacts. Every staged directory
carries a ``.owner`` lease (machine, pid, started, routing) so the sweep never deletes
a live run.

The local-disk primitives below are this module's other half: every filesystem write
of the shared layout goes through the atomic helpers, and the staging every store shares
(``stage`` / ``commit_run`` / ``write_run``, which are LOCAL disk whatever the root)
uses them from :mod:`evidence_local_staging`.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import shutil
import threading
import time
import unicodedata
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePath, PureWindowsPath
from typing import Any

from . import app_settings, atomic_write, evidence_store_fs, run_provenance, run_record, test_format
from .evidence_folder_access import store_write, tree_write, writer_guard
from .evidence_local_staging import OWNER, configured_here
from .evidence_owner import check_owner, mark_owner
from .evidence_shared_layout import _PROBE, SharedLayout
from .evidence_shared_manifest import MANIFEST
from .evidence_store import (
    READ_WORKERS,
    RESULT_FILE,
    AccessReport,
    EvidenceError,
    EvidenceKey,
    OpContext,
    ParamSpec,
    StagedRun,
    guard_component,
    register,
)
from .evidence_store_fs import _read_json
from .evidence_tree import OperationScope, ScopeLocal, enter_scope, make_dirs, safe_relative
from .paths import is_link, rmtree, same_place, sha256_file

log = logging.getLogger(__name__)

_NOT_PAYLOAD = frozenset({MANIFEST, OWNER})
SKEW_WARN_S = 120


# ---------------------------------------------------------------------------
# Local generation primitives (the layout's staging and the outbox reuse them)
# ---------------------------------------------------------------------------


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(at: datetime) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_run_id(at: datetime, suffix: str | None = None) -> str:
    """``<UTC start>_<16 hex>``: 64 random bits, so two runs of a project never share an id
    (data_format.md §2)."""
    return f"{at.strftime('%Y-%m-%dT%H-%M-%SZ')}_{suffix or secrets.token_hex(8)}"


def _run_started(run_id: str) -> str:
    """``2026-09-10T14-22-05Z_7f3a1c9e`` -> ``2026-09-10T14:22:05Z``."""
    s = run_id[:20]
    return f"{s[:11]}{s[11:13]}:{s[14:16]}:{s[17:19]}Z"


def _is_payload(name: str) -> bool:
    return name not in _NOT_PAYLOAD and not name.endswith(".part")


def _payload_files(root: Path) -> list[Path]:
    """Regular files below ``root`` (recursive, sorted) minus the manifest, the
    owner lease and ``.part`` leftovers. A link or junction is never followed nor saved
    (a file outside the run is never published as one of its own): left out, and logged."""
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        for name in [d for d in dirnames if is_link(here / d)]:
            dirnames.remove(name)
            log.warning("%s is a link: not saved with the run", here / name)
        for name in filenames:
            if not _is_payload(name):
                continue
            if is_link(here / name):
                log.warning("%s is a link: not saved with the run", here / name)
            else:
                found.append(here / name)
    return sorted(found)


def _file_entries(files: list[tuple[Path, str]]) -> list[dict[str, Any]]:
    """Each payload file's manifest entry; the documents a draft ran with also say their role
    (``run_provenance.role``: ``definition``)."""
    return [
        {
            "name": name,
            "size": p.stat().st_size,
            "sha256": sha256_file(p),
            **({"role": role} if (role := run_provenance.role(name)) else {}),
        }
        for p, name in files
    ]


def _local_manifest(store: Any, staged: StagedRun, manifest: dict) -> dict:
    """The manifest of a generation staged on LOCAL disk: every payload file hashed,
    and what the run produced.

    One implementation for both backends — a generation is staged locally whatever the
    root, because the runner hands ``StagedRun.path`` to a subprocess. ``store`` supplies
    only what differs: its path flavour (a payload name that is anchored in it is refused
    HERE, so no such name ever reaches an outbox entry) and its machine id.

    The run is filed under its key's test id; it records the name its test had: the one read at
    its start, else the name of the folder of the project's copy whose ``test.json`` holds that
    id now (``test_format.name_of``), in NFC (``data_format.md`` §2).
    """
    test_id = staged.key.test_id or ""
    # What the run read at its start (``run_provenance.read_start``), else what produced it now.
    prov = manifest.get("provenance")
    if prov is None:
        root = getattr(store, "project_root", None)
        name = test_format.name_of(root, test_id) if root is not None else ""
        prov = run_provenance.for_run(root, name)
    # Before hashing: a run of a draft keeps the documents it ran with; the result file carries
    # the same provenance, operator, board details (``run_fields.RunFields.recorded``) and serial
    # as the manifest, this PC's station name and, when the project keeps a run in one file, the
    # run's files (what it read at the run's start).
    board = manifest.get("run_fields") or {}
    run_provenance.keep_documents(staged.path, prov)
    inline = set()
    if prov.attachments == run_record.SINGLE:
        inline = {p.relative_to(staged.path).as_posix() for p in _payload_files(staged.path)}
        inline -= run_provenance.SAVE_OWN
    run_provenance.record_in_results(
        staged.path,
        "provenance",
        lambda: {**run_provenance.results_fields(prov, manifest.get("operator") or ""), **board},
        {"dut_id": manifest.get("serial_number"), "station_id": app_settings.station_id()},
        inline,
    )
    files = [(p, p.relative_to(staged.path).as_posix()) for p in _payload_files(staged.path)]
    for _p, name in files:
        safe_relative(name, store.io.flavour)
    complete = (staged.path / RESULT_FILE).is_file()
    return {
        "schema_version": 1,
        "run_id": staged.run_id,
        "test": unicodedata.normalize("NFC", prov.test),
        "test_id": test_id,
        "started": manifest.get("started") or _run_started(staged.run_id),
        "ended": _iso(_utc_now()),
        "verdict": run_record.verdict(_read_json(staged.path / RESULT_FILE)) if complete else "",
        "machine_id": store.machine_id,
        "complete": complete,
        "operator": manifest.get("operator") or "",
        "serial_number": manifest.get("serial_number") or "",
        **board,
        "bundle": prov.bundle,
        "definition_sha256": prov.definition_sha256,
        "test_version": prov.test_version,
        "test_version_edited": prov.test_version_edited,
        "app_version": prov.app_version,
        "files": _file_entries(files),
    }


def _fsync(path: Path) -> None:
    # O_BINARY: the Windows CRT strips a trailing Ctrl-Z (0x1A) when a text-mode fd
    # opened for writing is closed — a file ending in that byte would be truncated.
    fd = os.open(path, os.O_RDWR | getattr(os, "O_BINARY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_json_atomic(path: Path, data: dict, tag: str, *, ensure_ascii: bool = True) -> None:
    """``<name>.<tag>.part`` (tag = machine.pid, so writers never share a temp) -> replace."""
    tmp = path.with_name(f"{path.name}.{tag}.part")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=ensure_ascii)
        f.flush()
        os.fsync(f.fileno())
    atomic_write.replace(tmp, path)


def _write_file(path: Path, payload: bytes | str, tag: str) -> None:
    """Payload write: temp, fsync, replace; ``str`` keeps text-mode (platform) newlines."""
    tmp = path.with_name(f"{path.name}.{tag}.part")
    if isinstance(payload, str):
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
    else:
        with open(tmp, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
    atomic_write.replace(tmp, path)


def _append_file(path: Path, data: bytes) -> None:
    """Add ``data`` at the end of ``path`` (created when absent), fsynced.

    The index shard is the only store record that grows rather than being replaced:
    appending is what lets two processes add lines without a read-modify-write that
    drops the other's. One ``write`` of a few hundred bytes to a handle opened for
    append — a reader that meets a torn tail skips the line (see
    :mod:`evidence_index`)."""
    with open(path, "ab") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def _copy_atomic(src: Path, dst: Path, tag: str, *, verify: bool = False) -> None:
    """copyfile, fsync, times, replace. Only the timestamps travel (exports keep their
    ``generated`` dates): the store owns its files, so a read-only source attribute
    must not make a stored copy undeletable or un-replaceable. ``verify``: the
    ``.part`` must be byte-identical to ``src`` before it replaces anything; a
    mismatch removes it and is ``corrupt``."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f"{dst.name}.{tag}.part")
    shutil.copyfile(src, tmp)
    _fsync(tmp)
    if verify and (
        tmp.stat().st_size != src.stat().st_size or sha256_file(tmp) != sha256_file(src)
    ):
        tmp.unlink(missing_ok=True)
        raise EvidenceError("corrupt", f"{dst.name} does not match its source after the copy")
    st = src.stat()
    os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
    atomic_write.replace(tmp, dst)


def missing_parent(root: Path) -> str:
    """Why this evidence folder cannot be created (``""`` when it can).

    The owner's rule (2026-09-19): the project's own folder is created for it, its
    PARENTS never are. ``mkdir(parents=True)`` turned one typo high in a path into a
    whole wrong tree with every result in it, and nothing said so until someone went
    looking. Worded in ONE place, so the Evidence tab refuses it at Apply, *Check
    access* reports it, the Move refuses before it copies a byte and the store raises
    it — all in the same words.

    Three shapes needed their own sentence. A drive or share ROOT (``Z:\\``,
    ``\\\\bench\\share``) has nothing above it, so "the folder … does not exist, so
    <empty> cannot be created there" named nothing at all. A parent that is a FILE is
    not a missing folder and saying so sends the user hunting for the wrong thing. And
    a parent this machine cannot REACH — a share that is down, a disconnected drive —
    is indistinguishable from one that is not there, so the sentence says both rather
    than asserting the one it cannot know.

    **Filesystem access** (up to three stats), so it is called where I/O may run: at
    Apply, on the *Check access* worker and inside the Move. Never from
    ``evidence_config.validate_spec``, which runs on every keystroke of the Evidence
    tab and stays a pure string test — one offline UNC root cost 21 s per call there.
    """
    if root.is_dir():
        return ""
    parent = root.parent
    if parent == root:  # a drive or share root: there is no folder above it to make it in
        return f"{root} does not exist or cannot be reached — check the path"
    if parent.is_dir():
        return ""
    if parent.is_file():
        return f"{parent} is a file, so the folder {root.name} cannot be created in it"
    return (
        f"the folder {parent} does not exist or cannot be reached, so {root.name} "
        "cannot be created there — check the path"
    )


def folder_not_found(root: PurePath) -> str:
    """What a write says when the evidence folder is not there: the uploads wait."""
    return f"evidence folder not found: {root}"


def create_folder(store: FilesystemStore, project_id: str) -> None:
    """Create the project's evidence folder and mark it as this project's — the ONE
    place it is made, at the moments it is chosen: a new project (and the open that
    gives a project its default entry), Apply in the project settings, a Move. A
    folder another project's marker owns is refused, a missing PARENT is a typo and is
    refused by name (:func:`missing_parent`); an existing folder of ours is left as is."""
    check_owner(store, project_id)
    root = store.root
    if not root.is_dir():
        evidence_store_fs.require_volume(root)  # a dead share is unreachable, not missing
        problem = missing_parent(root)
        if problem:
            raise EvidenceError("not_found", problem)
        try:
            root.mkdir(exist_ok=True)
        except OSError as exc:
            raise evidence_store_fs.os_error(exc) from exc
    mark_owner(store, project_id)


def _staged_manifest(run_dir: Path) -> dict | None:
    """A staged or outbox generation's manifest (local disk); ``None`` = none yet or
    unreadable."""
    return _read_json(run_dir / MANIFEST) or None


# ---------------------------------------------------------------------------
# The filesystem tree
# ---------------------------------------------------------------------------


def _enumerate(
    base: Path, here: str, prune: Callable[[str], object] | None
) -> tuple[dict[str, tuple[int, float]], list[str]]:
    """One folder of :meth:`FsTree.walk_files`: its files' ``(size, mtime)`` and its subfolders,
    by path below *base*, what *prune* names left out. A link is followed, as ``is_dir`` and
    ``stat`` follow one."""
    files: dict[str, tuple[int, float]] = {}
    folders: list[str] = []
    try:
        listing = os.scandir(base / here if here else base)
    except (FileNotFoundError, NotADirectoryError):
        if not here:
            raise
        # Reading it as empty would plan the deletion of everything below it in the copy.
        raise EvidenceError(
            "not_found",
            f"the server listed {here} as a folder and then could not list it; "
            "the scan is incomplete and nothing will be deleted from it",
        ) from None
    with listing as entries:
        for entry in entries:
            rel = f"{here}/{entry.name}" if here else entry.name
            if prune is not None and prune(rel):
                continue
            if entry.is_dir():
                folders.append(rel)
            else:
                info = entry.stat()
                files[rel] = (info.st_size, info.st_mtime)
    return files, folders


@dataclass
class _FsScope(OperationScope):
    """One operation on a filesystem tree: also whether its volume answered already, so
    an operation that meets many absent folders asks the share once."""

    volume_answered: bool = False


#: How much of a file a streamed copy holds in memory at once.
_STREAM_BLOCK = 1 << 20


class FsTree:
    """:class:`evidence_tree.TreeIO` over a filesystem folder: exactly the ``Path``
    calls the shared layout made before the seam, through the atomic helpers above
    (module globals, looked up at call time)."""

    #: A file put here keeps its source's modification time (:func:`_copy_atomic`), so the
    #: sync can tell the file it just sent from one another bench saved right after.
    keeps_mtime = True

    def __init__(self, root: Path) -> None:
        self.root = root
        self.write_guard = nullcontext
        self.identity = str(root)
        # The root's own type decides it, so nothing changes for a Windows store: a
        # ``Path`` on Windows IS a ``PureWindowsPath``, and case-insensitive matching
        # is what it did before the flavour was named.
        self.flavour = "windows" if isinstance(root, PureWindowsPath) else "posix"
        self._local = ScopeLocal()

    def latency_class(self) -> str:
        return "remote" if evidence_store_fs.is_remote_root(self.root) else "local"

    def operation(self, cancel: threading.Event | None = None) -> Any:
        """The scope of ONE store operation: one listing per folder, as the transports
        keep it; a write in the scope forgets what it touched (:meth:`written`)."""
        return enter_scope(self._local, _FsScope(cancel=cancel))

    def written(self, *args: Any) -> None:
        """A write landed: every path it was given, and that path's folder, is re-listed
        by this thread's open operation (``OperationScope.forget``, the transports'
        rule)."""
        scope = self._local.scope
        if scope is None:
            return
        for arg in args:
            if isinstance(arg, PurePath):
                scope.forget(arg)
                scope.forget(arg.parent)

    def list_names(self, folder: Path) -> list[str] | None:
        scope = self._local.scope
        if scope is not None and folder in scope.listings:
            return scope.listings[folder]
        if folder.is_dir():
            names = os.listdir(folder)
        else:  # absent — or a dead share, which ``is_dir`` cannot tell apart: ask its volume
            self._require_volume(scope)
            names = None
        if scope is not None:
            scope.listings[folder] = names
        return names

    def _require_volume(self, scope: Any) -> None:
        """The tree contract: a listing that could not be made raises, never answers
        "absent" — ``ConnectionError`` when the root's volume does not answer."""
        if getattr(scope, "volume_answered", False):
            return
        evidence_store_fs.require_volume(self.root)
        if scope is not None:
            scope.volume_answered = True

    def is_dir(self, path: Path) -> bool:
        return path.is_dir()

    def is_file(self, path: Path) -> bool:
        return path.is_file()

    def exists(self, path: Path) -> bool:
        return path.exists()

    def stat(self, path: Path) -> tuple[int, float]:
        st = path.stat()
        return st.st_size, st.st_mtime

    def walk_files(
        self, base: Path, prune: Callable[[str], object] | None = None
    ) -> dict[str, Any]:
        """The sync's whole-subtree hook (``project_sync_scan.scan_server``): every file below
        *base* as ``{relative posix path: Stamp}``, sorted, with one ``os.scandir`` per folder
        and no per-file ``stat`` (on Windows an enumeration carries each entry's size and
        mtime), :data:`file_cache.READERS` folders at a time. A path *prune* names (truthy) is
        neither entered nor listed: an evidence tree is never enumerated.

        It raises as the folder-by-folder walk does: an absent *base* is ``not_found``
        (``ConnectionError`` when its volume does not answer), and so is a folder listed as
        one that then could not be listed; any other error reading a folder is raised."""
        from .file_cache import READERS
        from .project_origin import Stamp

        base = Path(base)
        try:
            first = _enumerate(base, "", prune)
        except (FileNotFoundError, NotADirectoryError):
            self._require_volume(self._local.scope)
            raise EvidenceError(
                "not_found", f"the project folder is not on the server: {base}"
            ) from None
        found: dict[str, tuple[int, float]] = {}
        with ThreadPoolExecutor(READERS, thread_name_prefix="walk") as pool:
            pending = [first]
            running: set = set()
            while pending or running:
                for files, folders in pending:
                    found.update(files)
                    running |= {pool.submit(_enumerate, base, rel, prune) for rel in folders}
                pending = []
                if running:
                    done, running = wait(running, return_when=FIRST_COMPLETED)
                    pending = [future.result() for future in done]
        return {rel: Stamp(size, mtime, "") for rel, (size, mtime) in sorted(found.items())}

    def newest_mtime(self, folder: Path) -> float:
        files = [p.stat().st_mtime for p in folder.rglob("*") if p.is_file()]
        return max(files) if files else folder.stat().st_mtime

    def read_bytes(self, path: Path) -> bytes:
        return path.read_bytes()

    def get_file(self, path: Path, local: Path, *, start: int = 0) -> None:
        """The file's bytes from *start* on, appended to *local* (written fresh from 0) a
        block at a time: a large file is never held in memory, and a transfer cut part way
        resumes from what *local* already holds."""
        with open(path, "rb") as src, open(local, "ab" if start else "wb") as dst:
            src.seek(start)
            shutil.copyfileobj(src, dst, _STREAM_BLOCK)

    @tree_write
    def write_atomic(self, path: Path, data: bytes, tag: str) -> None:
        _write_file(path, data, tag)

    @tree_write
    def write_json(self, path: Path, data: dict, tag: str) -> None:
        _write_json_atomic(path, data, tag)

    @tree_write
    def append_bytes(self, path: Path, data: bytes) -> None:
        _append_file(path, data)

    @tree_write
    def put_file(self, src: Path, dst: Path, tag: str, *, verify: bool) -> None:
        _copy_atomic(src, dst, tag, verify=verify)

    @tree_write
    def mkdir(self, folder: Path) -> None:
        folder.mkdir(exist_ok=True)

    @tree_write
    def mkdir_exclusive(self, folder: Path) -> bool:
        try:
            folder.mkdir()
        except FileExistsError:
            if folder.is_dir() or not folder.exists():
                # The previous holder may have removed the directory between
                # the failed claim and this check; the caller can try again.
                return False
            raise
        return True

    @tree_write
    def release_lock(self, folder: Path) -> None:
        rmtree(folder)

    @tree_write
    def rename(self, src: Path, dst: Path) -> None:
        src.rename(dst)

    @tree_write
    def unlink(self, path: Path, *, missing_ok: bool = False) -> None:
        path.unlink(missing_ok=missing_ok)

    @tree_write
    def rmtree(self, folder: Path) -> None:
        rmtree(folder)

    @tree_write
    def remove_empty_folder(self, folder: Path) -> bool:
        """The sync's optional hook (``project_sync_trash.prune``): remove *folder* only when it
        is empty — ``os.rmdir`` refuses any other — and say whether it went. Never a link: removing
        a junction or a folder symlink is not removing a folder of the project."""
        if is_link(folder):
            return False
        try:
            os.rmdir(folder)
        except OSError:
            return False
        return True

    def sha256(self, path: Path) -> str:
        return sha256_file(path)

    def through_link(self, base: Path, path: Path) -> bool:
        """The sync's optional hook (``project_sync_trash.trash_remote``): whether *path* is
        reached from *base* through a link — a symlink, a junction. The listing walks through
        one, but what is under it belongs to whatever it points at, never to be trashed from here."""
        here = Path(base)
        for part in Path(path).relative_to(base).parts:
            here = here / part
            if is_link(here):
                return True
        return False


# ---------------------------------------------------------------------------
# The backend
# ---------------------------------------------------------------------------


@register
class FilesystemStore(SharedLayout):
    """The shared layout on a local or remote folder: the layout's protocol over
    :class:`FsTree`. The configured root IS the project's evidence folder."""

    ID = "filesystem"
    PARAMS = [ParamSpec("root", "Evidence folder", "path", True, "")]
    TYPE_LABEL = "Folder (this PC or a share)"
    CAPABILITIES = frozenset({"read", "write", "query", "sweep"})
    read_parallelism = READ_WORKERS  # filesystem calls hold no state between reads

    @classmethod
    def from_config(
        cls,
        spec: dict[str, Any],
        *,
        project_root: Path,
        machine_id: Any = None,
        credentials: Any = None,
    ) -> FilesystemStore:
        """``machine_id`` (a value, or a callable resolved on first use) is the layout's
        identity — injected by the caller, never read from settings here.
        ``credentials`` is accepted and ignored: a folder is reached with the rights the
        user already has."""
        raw = (spec.get("params") or {}).get("root") or ""
        if not raw or not Path(raw).is_absolute():
            raise EvidenceError("invalid", "the evidence folder must be an absolute path")
        if machine_id is None:
            raise EvidenceError("invalid", "the evidence folder needs a machine_id")
        return cls(Path(raw), machine_id=machine_id, project_root=project_root)

    def __init__(
        self,
        root: Path,
        *,
        machine_id: str | Callable[[], str],
        project_root: Path | None = None,
        project_id: str = "",
        outbox: Path | None = None,
    ) -> None:
        self.root = Path(root)
        self.io = FsTree(self.root)
        self._init_shared(
            machine_id=machine_id, project_root=project_root, project_id=project_id, outbox=outbox
        )
        try:
            self._bound_to_config = configured_here(self)
        except EvidenceError:
            # Nothing it can be bound to now (a probe for a project not created yet);
            # the project's own store is bound by its opener (:meth:`bind_to_config`).
            self._bound_to_config = False
        # Transfer cleanup intentionally writes the old root after the config switch.
        # Tree primitives hold it in place; project binding belongs to store operations.
        self.io.write_guard = lambda: writer_guard(self.root)

    def _names_this(self, spec: dict) -> bool:
        """Whether a ``filesystem`` entry opens THIS store: it names this root — by place
        (:func:`paths.same_place`), so the folder saved again as the share its drive letter
        maps is still this store."""
        params = spec.get("params", {})
        root = params.get("root") if isinstance(params, dict) else None
        return bool(
            isinstance(root, str)
            and root
            and same_place(os.path.abspath(root), os.path.abspath(self.root))
        )

    @contextmanager
    def _writing_root(self):
        """Hold the root in place for one operation. A missing folder is the OPERATION's
        to report ("evidence folder not found", ``_mkdir_safe``) — on every platform: the
        Windows guard cannot open it, and where there is no guard nothing says otherwise."""
        self.check_binding()
        guard = writer_guard(self.root)
        try:
            try:
                guard.__enter__()
            except FileNotFoundError:
                yield
                return
            try:
                self.check_binding()
                # A missing folder is the operation's to report (``_mkdir_safe`` asks the
                # volume first: a dead share is unreachable, never "denied"); only a root
                # that is a file is refused here.
                if self.root.exists() and not self.root.is_dir():
                    raise NotADirectoryError(str(self.root))
                yield
            finally:
                guard.__exit__(None, None, None)
        except OSError as exc:
            raise evidence_store_fs.os_error(exc) from exc

    def flush_outbox(self, ctx: OpContext | None = None) -> list[str]:
        from .evidence_outbox_lock import uploads_held

        # An empty offline outbox must not require reaching the shared folder, and
        # uploads a Move holds must not hold the folder open: it may be renaming it.
        if (
            not self.outbox.is_dir()
            or not any(self.outbox.iterdir())
            or (self.project_id and uploads_held(self))
        ):
            return []
        with self._writing_root():
            return super().flush_outbox(ctx)

    delete_artifact = store_write(SharedLayout.delete_artifact)
    delete_run = store_write(SharedLayout.delete_run)
    restore_trash = store_write(SharedLayout.restore_trash)
    purge_trash = store_write(SharedLayout.purge_trash)
    promote_run = store_write(SharedLayout.promote_run)
    demote_run = store_write(SharedLayout.demote_run)
    add_run_annotation_entries = store_write(SharedLayout.add_run_annotation_entries)
    add_board_entries = store_write(SharedLayout.add_board_entries)
    sweep_incomplete = store_write(SharedLayout.sweep_incomplete)

    def _mkdir_safe(self, target: Path) -> None:
        """mkdir -p below the root — never the root itself.

        The owner's rule (2026-10-23): the evidence folder is created when it is chosen
        (:func:`create_folder`), and a write NEVER creates it. A folder that was deleted,
        or a share mounted elsewhere, is said ("evidence folder not found") and the
        uploads wait for it, instead of a fresh empty folder quietly taking the runs.
        Ownership is ``evidence_owner.mkdir_owned``'s job."""
        self.check_binding()
        if not self.root.is_dir():
            evidence_store_fs.require_volume(self.root)  # a dead share is unreachable, not missing
            raise EvidenceError("not_found", folder_not_found(self.root))
        make_dirs(self.io, self.root, target)

    def local_path(self, key: EvidenceKey, ctx: OpContext | None = None) -> Path:
        """A key's file on this disk (the configured folder is one)."""
        if key.kind == "run":
            d = Path(self._run_dir(key.project_id, key.board, key.test_id, key.run_id))
        else:
            d = Path(self._artifact_root(key.kind, key.project_id, key.board))
        return d / guard_component(key.name) if key.name else d

    def close(self) -> None:
        """Nothing to release: a folder holds no connection."""

    # -- access ---------------------------------------------------------------------

    def check_access(self, ctx: OpContext | None = None) -> AccessReport:
        """Prove list + write + rename + delete IN the project's evidence folder, time
        the round trip, and compare the probe's server mtime with the local clock.

        A folder another project's marker owns is the whole answer: nothing is probed
        (so no capability reads as FAILED) and the refusal names both projects.

        A missing folder is reported, never created: it is made when it is chosen
        (Apply, a new project, a Move — :func:`create_folder`), and a missing PARENT is
        named here rather than at Apply."""
        report = AccessReport(False, False, False, False, False, None, None)
        if not self.project_id:
            report.notes.append("no project_id yet: open the project once, then check again")
            return report
        try:
            check_owner(self, self.project_id)
        except EvidenceError as exc:
            refused = AccessReport(
                False, False, False, False, False, None, None, probed=frozenset()
            )
            refused.notes.append(str(exc))
            return refused
        ns = self._namespace(self.project_id)
        probe = ns / _PROBE
        moved = probe.with_suffix(".tmp2")
        t0 = time.perf_counter()
        try:
            if not self.root.is_dir():
                evidence_store_fs.require_volume(self.root)
                raise EvidenceError(
                    "not_found",
                    missing_parent(self.root)
                    or f"{self.root} does not exist yet — Apply creates it",
                )
            with self._writing_root():
                try:
                    report.reachable = True
                    list(ns.iterdir())
                    report.can_list = True
                    probe.write_bytes(b"probe")
                    report.can_write = True
                    report.clock_skew_s = probe.stat().st_mtime - time.time()
                    probe.rename(moved)
                    report.can_rename = True
                    moved.unlink()
                    report.can_delete = True
                    report.per_file_ms = (time.perf_counter() - t0) * 1000 / 3
                finally:
                    for leftover in (probe, moved):
                        try:
                            leftover.unlink(missing_ok=True)
                        except OSError:
                            pass
        except (OSError, EvidenceError) as exc:  # a missing PARENT is typed, not an OSError
            report.notes.append(str(exc))
        for cap in ("list", "write", "rename", "delete"):
            report.notes.append(f"{cap}: {'ok' if getattr(report, 'can_' + cap) else 'FAILED'}")
        if report.per_file_ms is not None:
            report.notes.append(f"≈{report.per_file_ms:.1f} ms per file")
        if report.clock_skew_s is not None and abs(report.clock_skew_s) > SKEW_WARN_S:
            report.notes.append(f"clock skew {report.clock_skew_s:+.0f} s against the store")
        return report
