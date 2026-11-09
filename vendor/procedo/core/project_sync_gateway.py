"""Everything the GUI asks of the project-sync engine, behind ONE object.

:mod:`project_sync` is free functions over a :class:`evidence_tree.TreeIO`. The GUI
needs a little more than that and none of it on the GUI thread: the origin record,
the tree rebuilt from it with THIS machine's credential, a folder listing to pick a
project out of, and the one recovery that is purely local (*Forget this working
copy*). Gathering them here gives the GUI a single seam — the binding, the New
Project dialog and *Open from server* all hold a :class:`SyncGateway`, and a test
hands them a fake with the same methods, so no test thread can reach a server.

**The endpoint is the PARENT folder, never the project.** The project's folder on
the server is ``<parent>/<name>`` and travels as the pass's *base*; the reconnect
params stay rooted at the parent. That is what lets :func:`project_sync.publish`
create the project's folder rather than refuse a base path that is not there. (The
sign-in is the SERVER's whatever folder is named — :func:`evidence_store.
credential_key` — so one serves every project and evidence folder on it.)

**A server that keeps projects itself** — the web server's JSON API, the evidence database
(:func:`project_transport.reads_by_changes`) — has no folders: its projects are listed by
name, the *base* of one is ``/<name>``, and every pass is :mod:`project_sync_api`'s over its
:class:`project_store.ProjectSource`. Each method below answers for both kinds, so no caller
asks which kind a server is, except to show or hide a folder (:meth:`SyncGateway.has_folders`).

Qt-free, so the engine seam stays testable without a QApplication.
"""

from __future__ import annotations

import functools
import logging
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path, PurePath, PurePosixPath
from typing import Any

from . import (
    conflict_versions,
    evidence_config,
    evidence_credentials,
    project_conflict_view,
    project_local_state,
    project_origin,
    project_sync,
    project_sync_api,
    project_sync_pass,
    project_sync_release,
    project_sync_scan,
    project_transport,
    server_settings,
    working_copy_claims,
    working_copy_cleanup,
)
from .evidence_store import REGISTRY, EvidenceError, OpContext
from .evidence_store_fs import os_error
from .evidence_tree import TreeIO
from .paths import inside
from .project_origin import Origin
from .project_source import SyncLock, share_of
from .project_store_tree import TreeProjectSource
from .project_sync import SyncReport, SyncStatus

log = logging.getLogger(__name__)

#: The project sources one gateway call built on this thread (``None``: no call open).
_BUILT = threading.local()


def _closes_its_sources(method: Callable[..., Any]) -> Callable[..., Any]:
    """*method*, closing every project source it built (:meth:`SyncGateway.source`) when it
    returns: a source that holds connections (the database's pool, the web server's session)
    is never left to the garbage collector. A call made from inside another joins it."""

    @functools.wraps(method)
    def call(*args: Any, **kwargs: Any) -> Any:
        if getattr(_BUILT, "sources", None) is not None:
            return method(*args, **kwargs)
        _BUILT.sources = []
        try:
            return method(*args, **kwargs)
        finally:
            built, _BUILT.sources = _BUILT.sources, None
            for source in built:
                close = getattr(source, "close", None)
                if close is None:
                    continue
                try:
                    close()
                except Exception:  # noqa: BLE001 - a source that cannot close owes nothing
                    log.warning("a project source did not close", exc_info=True)

    return call


def _closing_sources(cls: type) -> type:
    """Every public method of *cls* that is not a static one closes the sources it built."""
    for name, value in list(vars(cls).items()):
        if name.startswith("_") or isinstance(value, (staticmethod, classmethod, property)):
            continue
        if callable(value):
            setattr(cls, name, _closes_its_sources(value))
    return cls


__all__ = ["SyncGateway", "folder_name", "gateway", "project_base"]


def folder_name(name: str) -> str:
    """*name*, verbatim, once it is one folder name — the ONE rule the app has.

    Exactly one path component and nothing else: not blank, not ``.`` or ``..``,
    no separator. ``.`` is the one that matters, because it joins to the endpoint's
    OWN root and a move publishes there and then removes the source.

    Verbatim, and that is the whole point of it being here: New Project composes the
    project's folder through :func:`project_base` and Move/Re-point through
    ``project_relocate.server_base``. A second, stricter rule on one of the two made
    the app refuse to re-point a project it had itself created, and a rule that
    rewrote the name (NFC) addressed a different folder from the one the record
    names.
    """
    if not name.strip() or name in (".", "..") or "/" in name or "\\" in name:
        raise EvidenceError(
            "invalid",
            f"{name!r} is not a folder name. Give the project a folder of its own on the server.",
        )
    return name


def project_base(parent: str, name: str) -> str:
    """The project's folder on the server: ``<parent>/<name>``, server-spelled.

    One place, because three callers compose it (New Project publishes to it, *Open
    from server* checks one out of it, and the credential note names the parent) and
    a second spelling would make a project that publishes fine refuse to be opened.

    POSIX, because a project SERVER is: every transport offered as a project location
    is one (``project_location.server_types`` excludes the filesystem, which is opened
    in place). A Windows-flavoured server backend would need its own join here.
    """
    base = f"{parent}/{folder_name(name)}".replace("\\", "/")
    return "/" + "/".join(part for part in base.split("/") if part)


@_closing_sources
class SyncGateway:
    """The engine, the transport and this machine's credential in one object."""

    #: What the last :meth:`forget` could not remove from the set-aside copy (``""``
    #: when it removed everything). The folder IS gone as far as the app is concerned
    #: — the record was dropped — so this is a note to the user, never a failure.
    forget_leftover = ""

    # -- the working copy's record -------------------------------------------------

    def origin(self, working_copy: Path) -> Origin | None:
        """The record for THIS folder, or ``None`` when it is an ordinary project.

        Two things have to be true, and the second one is the fix for a measured
        defect: the record has to exist, and it has to be the record OF THIS FOLDER.
        One ``project_id`` in several folders is ordinary here — an Explorer copy of
        the project off the share carries it — and every one of those folders used
        to answer "I am the working copy": opening the copy pulled the server over
        it and pushed its private files back, and *Forget* ``rmtree``d it.

        The cheap test comes first and costs no I/O at all: a working copy can only
        live under :func:`project_local_state.working_copies_root`, so a path
        outside it is not one — which also keeps this off a dead share, where
        reading a config file on the GUI thread costs twenty seconds.
        """
        folder = Path(working_copy)
        if not self.could_be_working_copy(folder):
            return None
        record = project_origin.load(folder)
        if record is None or not project_origin.describes(record, folder):
            return None
        return record

    @staticmethod
    def could_be_working_copy(folder: Path | str) -> bool:
        """Whether *folder* is even in the place working copies live.

        ``abspath``, never ``realpath``: resolving a path OPENS it, and this is
        asked about whatever the recent list holds — a share that is no longer
        there, on the GUI thread. So it is pure string work, and the price is that
        a working copy reached through a junction fails the cheap test and shows no
        server item. The app composes these paths itself, and the failure is "no
        sync shown", not a folder touched by mistake.
        """
        return inside(folder, project_local_state.working_copies_root())

    def credential_missing(self, backend: str, params: dict[str, Any]) -> str:
        """Why this endpoint has no credential to sign in with (``""`` when it has).

        Asked BEFORE a pass rather than after: a store that signs in with ``("", "")``
        comes back as a server refusal, which reads like a wrong password and sends
        the user to the server's admin instead of to the settings editor.

        Whether a credential is needed at all is the BACKEND's answer: declaring
        ``ENDPOINT_PARAMS`` — the fields a secret is filed under — is how one says it
        signs in. A transport that does not (a filesystem tree, which is what the
        tests drive as their server) is never held up for a password nobody can give.
        Where the sign-in is added is the backend's too (``SIGN_IN_HINT``: the database's
        login is Settings ▸ Database's).
        """
        declared = self._declared(backend)
        if not getattr(declared, "ENDPOINT_PARAMS", ()):
            return ""
        key = self.credential_key(backend, params)
        if not key or evidence_credentials.has_credential(key):
            return ""
        hint = getattr(declared, "SIGN_IN_HINT", "") or (
            f"Add {evidence_config.sign_in_words(backend)} in Settings ▸ Server"
        )
        return (
            f"No sign-in is saved on this PC for {server_settings.sign_in_label(backend, key)}. "
            f"{hint}: a sign-in never travels with the project."
        )

    # -- the transport ---------------------------------------------------------------

    def sync_params(self, spec: dict[str, Any]) -> dict[str, Any]:
        """A configuration ENTRY (what the form renders) as reconnect parameters.

        Asked of the backend that declared the fields, never coerced here: the form's
        values are strings, and a record built from them by hand reconnected to port
        ``"21"`` with TLS ``"off"`` read as true.
        """
        backend = spec.get("backend", "")
        convert = getattr(self._declared(backend), "sync_params", None)
        if convert is None:
            raise EvidenceError(
                "invalid", f"a project cannot live on a {backend or 'nameless'} store"
            )
        return convert(spec)

    def _declared(self, backend: str) -> type | None:
        """The class that declares *backend*'s fields: the evidence registry's for a tree, the
        project transport's for a server that keeps projects itself."""
        if self.has_folders(backend):
            return REGISTRY.get(backend)
        return project_transport.source_class(backend)

    @staticmethod
    def has_folders(backend: str) -> bool:
        """Whether a server of *backend* is a tree of folders a user can name and browse; a
        server that keeps projects itself (the web server) has none."""
        return not project_transport.reads_by_changes(backend)

    def tree(self, backend: str, params: dict[str, Any]) -> TreeIO:
        """The tree for these reconnect parameters, with the machine's credential."""
        if not self.has_folders(backend):
            raise EvidenceError("invalid", "This server has no folders: it keeps its projects.")
        origin = Origin(backend=backend, identity="", base="", params=dict(params), project_id="")
        return project_transport.tree_for(origin, evidence_credentials.lookup)

    @staticmethod
    def source(backend: str, params: dict[str, Any]) -> Any:
        """The project source for these reconnect parameters, with the machine's credential
        (a server that keeps projects itself). Building one opens no connection. Built during a
        gateway call, it is closed when that call returns (:func:`_closes_its_sources`); built
        outside one, its caller owns it."""
        made = project_transport.source_for(backend, params, evidence_credentials.lookup)
        built = getattr(_BUILT, "sources", None)
        if built is not None:
            built.append(made)
        return made

    def endpoint_root(self, backend: str, params: dict[str, Any]) -> str:
        """The folder this endpoint is rooted at, as the SERVER spells it (``/`` for a
        server with no folders: its projects are ``/<name>``).

        Read off the transport rather than off a param named ``base_path``, which is
        one backend's spelling of it. Building a tree opens no connection.
        """
        if not self.has_folders(backend):
            return "/"
        return self.tree(backend, params).root.as_posix()

    def credential_key(self, backend: str, params: dict[str, Any]) -> str:
        """The key this endpoint's sign-in is filed under — the SERVER's, so one
        sign-in serves every folder on it. ``""`` for a transport that signs in
        nowhere (a folder tree has no key)."""
        if not self.has_folders(backend):
            return str(self.source(backend, params).identity)
        return str(getattr(self.tree(backend, params), "credential_key", ""))

    def _tree_for(self, origin: Origin) -> TreeIO:
        return project_transport.tree_for(origin, evidence_credentials.lookup)

    def _source_of(self, origin: Origin) -> Any:
        """The project source an origin record reaches, ``None`` for a tree's record."""
        if self.has_folders(origin.backend):
            return None
        return self.source(origin.backend, origin.params)

    @staticmethod
    def _base(tree: TreeIO, base: str) -> PurePath:
        """A recorded base path as the tree spells its own paths."""
        return type(tree.root)(base)

    def _origin_or_raise(self, working_copy: Path) -> Origin:
        origin = self.origin(working_copy)
        if origin is None:
            raise EvidenceError(
                "foreign",  # the kind that means "this belongs to another project"
                f"{working_copy} is not this machine's working copy of a project on a "
                "server: it has no origin record of its own",
            )
        return origin

    # -- the passes ------------------------------------------------------------------

    def pull(
        self,
        working_copy: Path,
        *,
        ctx: OpContext | None = None,
        approved_deletes: frozenset[str] = frozenset(),
        hold: Any = None,
        progress: Any = None,
    ) -> SyncReport:
        origin = self._origin_or_raise(working_copy)
        source = self._source_of(origin)
        if source is not None:
            return project_sync_api.pull(
                Path(working_copy),
                source,
                ctx=ctx,
                approved_deletes=approved_deletes,
                hold=hold,
                progress=progress,
            )
        tree = self._tree_for(origin)
        return project_sync.pull(
            Path(working_copy),
            tree,
            self._base(tree, origin.base),
            ctx=ctx,
            approved_deletes=approved_deletes,
            hold=hold,
            progress=progress,
        )

    def push(
        self,
        working_copy: Path,
        *,
        ctx: OpContext | None = None,
        approved_deletes: frozenset[str] = frozenset(),
    ) -> SyncReport:
        origin = self._origin_or_raise(working_copy)
        source = self._source_of(origin)
        if source is not None:
            return project_sync_api.push(
                Path(working_copy), source, ctx=ctx, approved_deletes=approved_deletes
            )
        tree = self._tree_for(origin)
        return project_sync.push(
            Path(working_copy),
            tree,
            self._base(tree, origin.base),
            ctx=ctx,
            approved_deletes=approved_deletes,
        )

    def restore(
        self, working_copy: Path, paths: frozenset[str], *, ctx: OpContext | None = None
    ) -> SyncReport:
        """Bring exactly *paths* back from the server. Writes nothing to it."""
        origin = self._origin_or_raise(working_copy)
        source = self._source_of(origin)
        if source is not None:
            return project_sync_api.restore(Path(working_copy), source, paths, ctx=ctx)
        tree = self._tree_for(origin)
        return project_sync.restore(
            Path(working_copy), tree, self._base(tree, origin.base), paths, ctx=ctx
        )

    def source_folders(self, working_copy: Path) -> list[str]:
        """The folders of a share project ON THE SHARE, for the source watcher: the project
        folder and every folder a synced file is in, with the folders above them (a new
        subfolder is said by its parent). Read from the record, so nothing on the share is
        asked; empty for a project that is not on a share."""
        record = self.origin(working_copy)
        root = record.params.get("root") if record is not None else None
        if (
            record is None
            or record.backend != project_transport.FILESYSTEM
            or not isinstance(root, str)
            or not root
        ):
            return []
        below: set[PurePosixPath] = set()
        for rel in record.files:
            below.update(PurePosixPath(rel).parents)
        return sorted({str(Path(root).joinpath(*folder.parts)) for folder in below})

    def status(self, working_copy: Path) -> SyncStatus:
        """The OFFLINE count — what is waiting to go up, from the manifest alone.

        Nothing here touches the server: this is what a status-bar timer asks every
        few seconds, and it answers ``busy`` rather than waiting for a running pass.
        """
        return project_sync.status(Path(working_copy))

    def checkout(
        self,
        backend: str,
        params: dict[str, Any],
        base: str,
        *,
        ctx: OpContext | None = None,
        defer: bool = False,
    ) -> Path:
        """This machine's working copy of the project at *base*, made and filled
        (``project_sync.checkout``; *defer* leaves the large files for a later pass)."""
        if not self.has_folders(backend):
            source = self.source(backend, params)
            return project_sync_api.checkout(
                source,
                base,
                backend=backend,
                identity=source.identity,
                params=dict(params),
                ctx=ctx,
                defer=defer,
            )
        tree = self.tree(backend, params)
        return project_sync.checkout(
            tree,
            self._base(tree, base),
            backend=backend,
            params=dict(params),
            ctx=ctx,
            defer=defer,
        )

    def copy_on_this_pc(self, backend: str, params: dict[str, Any], base: str) -> Path | None:
        """This PC's working copy of the project at *base* on that server, if it has one —
        one working copy per project per PC, whichever app made it: *Open Project from
        Server* opens it rather than checking out over it. Local disk only (the copies'
        records); a share's copy is never one."""
        key = self.credential_key(backend, params)
        wanted = PurePosixPath(str(base).replace("\\", "/"))
        root = project_local_state.working_copies_root()
        for folder in sorted(root.iterdir()) if root.is_dir() else ():
            origin = self.origin(folder) if folder.is_dir() else None
            if (
                origin is None
                or origin.backend != backend
                or share_of(origin) is not None
                or PurePosixPath(origin.base.replace("\\", "/")) != wanted
            ):
                continue
            try:
                if self.credential_key(origin.backend, origin.params) == key:
                    return folder
            except EvidenceError:
                continue
        return None

    def publish(
        self,
        working_copy: Path,
        backend: str,
        params: dict[str, Any],
        base: str,
        *,
        ctx: OpContext | None = None,
    ) -> SyncReport:
        if not self.has_folders(backend):
            source = self.source(backend, params)
            return project_sync_api.publish(
                Path(working_copy),
                source,
                base,
                backend=backend,
                identity=source.identity,
                params=dict(params),
                ctx=ctx,
            )
        tree = self.tree(backend, params)
        return project_sync.publish(
            Path(working_copy),
            tree,
            self._base(tree, base),
            backend=backend,
            params=dict(params),
            ctx=ctx,
        )

    def repoint(
        self,
        working_copy: Path,
        backend: str,
        params: dict[str, Any],
        base: str,
        *,
        ctx: OpContext | None = None,
    ) -> Origin:
        """Point this working copy at the SAME project at a NEW address.

        **It proves the new address holds the same project before it writes a byte**:
        the project's ``config/config.json`` is read THROUGH the new tree and its
        ``project_id`` compared with the record's. Another project's folder, or one
        that cannot be read, refuses and the record is left exactly as it was — the
        alternative is a push of this project over somebody else's, which is the
        accident every identity guard in this engine exists to prevent.

        The manifest survives and a pull is owed (``project_origin.repoint``).
        """
        origin = self._origin_or_raise(working_copy)
        ctx = ctx or OpContext()
        found, identity = self._project_at(backend, params, base, ctx)
        if found != origin.project_id:
            raise EvidenceError(
                "foreign",  # another project's folder: an identity refusal, not an ACL
                f"{base} on {identity} holds project {found or 'none'}, and {working_copy} is "
                f"this machine's working copy of {origin.project_id}. Nothing was changed.",
            )
        return project_origin.repoint(
            Path(working_copy),
            origin,
            backend=backend,
            identity=identity,
            base=base,
            params=dict(params),
        )

    def _project_at(
        self, backend: str, params: dict[str, Any], base: str, ctx: OpContext
    ) -> tuple[str, str]:
        """The id of the project at *base* (``""`` when none is there), and the server's
        identity: its ``config/config.json`` read through a tree, its name looked up on a
        server that keeps projects itself."""
        if not self.has_folders(backend):
            source = self.source(backend, params)
            name = PurePosixPath(base).name.casefold()
            listed = source.list_projects(ctx)
            found = [p.project_id for p in listed if p.name.casefold() == name]
            return (found[0] if found else ""), str(source.identity)
        tree = self.tree(backend, params)
        try:
            with project_sync_pass.operation(tree, ctx):
                data = tree.read_bytes(self._base(tree, base) / project_origin.CONFIG_FILE)
        except OSError as exc:
            raise os_error(exc) from exc
        return project_origin.project_id_of(data), str(tree.identity)

    def occupant(
        self, backend: str, params: dict[str, Any], base: str, ctx: OpContext
    ) -> tuple[str, list[str]] | None:
        """Who holds the project's place at *base*: ``None`` when it is free (not there, or an
        empty folder), else the project id it declares (``""``: files and no project) and what
        it holds, first names first. What a Move asks before it copies a byte."""
        if not self.has_folders(backend):
            found, _identity = self._project_at(backend, params, base, ctx)
            return (found, [PurePosixPath(base).name]) if found else None
        tree = self.tree(backend, params)
        root = self._base(tree, base)
        try:
            with project_sync_pass.operation(tree, ctx):
                names = tree.list_names(root)
                held = sorted(name for name in names or [] if not name.endswith(".part"))
                if not held:
                    return None
                try:
                    data = tree.read_bytes(root / project_origin.CONFIG_FILE)
                    return project_origin.project_id_of(data), held
                except (OSError, EvidenceError):
                    return "", held
        except OSError as exc:
            raise os_error(exc) from exc

    def rename_test(
        self,
        working_copy: Path,
        old: str,
        new: str,
        *,
        folder: dict[str, str] | None = None,
        ctx: OpContext | None = None,
    ) -> dict[str, str]:
        """Rename a test of a project on a server that keeps projects itself, in one move there
        and then here (:func:`project_sync_api.rename_test`): its draft follows it on the
        server. Its releases, runs and current-run marks are filed under its hidden id, which
        the rename keeps: none of them moves, wherever they are kept, and a run still waiting
        in this PC's outbox goes where it would have gone.

        *folder* is what taking back a move of this caller's names (the versions that move
        answered); returns the versions moved. A move neither finished nor taken back raises
        :class:`project_sync_api.Stranded`, saying where the test is on each side."""
        origin = self._origin_or_raise(working_copy)
        source = self._source_of(origin)
        if source is None:
            raise EvidenceError(
                "invalid", "A test is renamed on the server only where the server keeps projects."
            )
        return project_sync_api.rename_test(
            Path(working_copy), source, old, new, folder=folder, ctx=ctx or OpContext()
        )

    def release(
        self, working_copy: Path, test: str, note: str = "", ctx: OpContext | None = None
    ) -> int:
        """Release *test* of a working copy of a project on a server, with *note*: on the
        server first (its ``ProjectSource.release``: the web server's own, or the project's
        folder on an FTPS server as a tree's), then the same release in the copy
        (:mod:`project_sync_release`); the number the server gave it. Raises
        ``test_release.Refused`` (the message for the user)."""
        origin = self._origin_or_raise(working_copy)
        source = self._source_of(origin)
        if source is None:
            tree = self._tree_for(origin)
            base = self._base(tree, origin.base)
            source = TreeProjectSource(tree, base.parent, {origin.project_id: base})
        return project_sync_release.release(Path(working_copy), source, test, note, ctx=ctx)

    def renames_on_server(self, working_copy: Path) -> bool:
        """Whether a test of *working_copy* is renamed on its server (:meth:`rename_test`)
        rather than here alone. Reads the local record only."""
        origin = self.origin(working_copy)
        return origin is not None and not self.has_folders(origin.backend)

    # -- browsing --------------------------------------------------------------------

    def folders(self, backend: str, params: dict[str, Any], base: str = "") -> list[str]:
        """The folder names directly under *base* (the endpoint's own root by default).

        Folders only: a project is a folder, and listing the files beside them would
        only give the user something to pick that cannot be checked out. A server that keeps
        projects itself answers its projects' names: what *Check access* lists there.
        """
        if not self.has_folders(backend):
            return sorted(p.name for p in self.source(backend, params).list_projects())
        tree = self.tree(backend, params)
        root = self._base(tree, base) if base else tree.root
        try:
            with project_sync_pass.operation(tree, OpContext()):
                names = tree.list_names(root)
                if names is None:
                    raise EvidenceError("not_found", f"{root} is not a folder on the server")
                return sorted(name for name in names if tree.is_dir(root / name))
        except OSError as exc:
            raise os_error(exc) from exc

    def projects(self, backend: str, params: dict[str, Any]) -> list[str]:
        """Every project directly under this endpoint's own folder, by its full path on
        the server: each folder there that holds ``config/config.json``.

        What *Open project from server* lists for one server: one listing, then the
        one read per folder :meth:`is_project` makes, all in ONE operation scope (one
        connection, one cached listing per folder). On a worker, never inline. A server that
        keeps projects itself lists them by name: ``/<name>`` each.
        """
        if not self.has_folders(backend):
            listed = self.source(backend, params).list_projects()
            return sorted(f"/{p.name}" for p in listed)
        tree = self.tree(backend, params)
        try:
            with project_sync_pass.operation(tree, OpContext()):
                names = tree.list_names(tree.root)
                if names is None:
                    raise EvidenceError("not_found", f"{tree.root} is not a folder on the server")
                found = [tree.root / name for name in sorted(names)]
                return [
                    folder.as_posix()
                    for folder in found
                    if tree.is_dir(folder) and tree.is_file(folder / project_origin.CONFIG_FILE)
                ]
        except OSError as exc:
            raise os_error(exc) from exc

    def is_project(self, backend: str, params: dict[str, Any], base: str) -> bool:
        """Whether the folder at *base* holds a project — ONE read, no listing.

        What the folder browser asks before it lets a folder be chosen as a project.
        The config file is the same one :func:`project_sync.checkout` fetches first,
        so a folder this answers yes for is one the checkout can start from.
        """
        if not self.has_folders(backend):
            return bool(self._project_at(backend, params, base, OpContext())[0])
        tree = self.tree(backend, params)
        try:
            with project_sync_pass.operation(tree, OpContext()):
                return tree.is_file(self._base(tree, base) / project_origin.CONFIG_FILE)
        except OSError as exc:
            raise os_error(exc) from exc

    def make_folder(self, backend: str, params: dict[str, Any], folder: str) -> None:
        """Create ONE folder on the server; its parent must already be there.

        The browser's *New folder*, and the only write the GUI makes to a server
        outside a sync pass. A tree never creates its own root, so a mistyped path
        is still reported rather than grown.
        """
        tree = self.tree(backend, params)
        try:
            with project_sync_pass.operation(tree, OpContext()):
                tree.mkdir(self._base(tree, folder))
        except OSError as exc:
            raise os_error(exc) from exc

    # -- the local-only recovery ------------------------------------------------------

    def forget(self, working_copy: Path) -> tuple[bool, bool]:
        """Drop THIS machine's working copy of a project: the record, then the folder.

        The recovery for a project renamed or moved on the server, which the engine
        refuses exactly as it refuses a foreign one — nothing can tell the two apart.
        It touches the SERVER not at all, which is the whole point: the project is
        still there and is checked out again afterwards. Unpushed changes in the
        folder are lost, so the caller confirms with the count in hand first.

        **It re-proves that this folder is the recorded working copy** right before
        the ``rmtree``, as every delete in the engine re-proves its own row: this
        removes a whole project folder, and a caller that passed the wrong path — an
        Explorer copy of the project carrying the same ``project_id`` — would
        otherwise be obeyed. Measured: it was.

        **The folder goes first, the record only if it went.** A record unlinked
        beside a folder that would not go (a file held open) leaves a project folder
        nothing knows the origin of — unforgettable and unsyncable. The caller is
        told what is left instead (``removed=False``).

        **And it is whole or nothing.** ``rmtree(ignore_errors=True)`` over a folder
        with one file open removes everything it CAN and answers False: the caller
        was then told to "try again" on a project folder that had lost its tests,
        its bundle and its ``config/config.json`` — which the next open refuses as
        "not a project". So the folder is RENAMED aside first; Windows refuses that
        rename outright while any file inside is open, which turns a partial
        deletion into a clean refusal that has touched nothing. The rename lands
        beside the folder, inside the app-data root this module owns.
        """
        working_copy = Path(working_copy)
        if self.origin(working_copy) is None:
            raise EvidenceError(
                "foreign",
                f"{working_copy} is not this machine's working copy of a project on a "
                "server — a folder holding the same project is not the same folder. "
                "Nothing was removed.",
            )
        removed = True
        leftover = None
        record = project_origin.record_path(working_copy)
        had_record = record is not None and record.exists()
        # The same steps as removing a copy nobody uses (working_copy_cleanup), with no proof
        # that nothing waits: the caller confirmed losing it, the count in hand.
        if working_copy.is_dir():
            try:
                doomed = working_copy_cleanup.set_aside(working_copy)
            except OSError:
                log.warning(
                    "%s could not be set aside; nothing removed", working_copy, exc_info=True
                )
                return False, False
            working_copy_cleanup.finish(doomed, working_copy, record)
            removed = not working_copy.exists()
            leftover = doomed if doomed.exists() else None
        else:
            working_copy_cleanup.drop_record(record, working_copy)
        dropped = had_record and not record.exists()
        self.forget_leftover = str(leftover) if leftover is not None else ""
        log.info(
            "Forgot the working copy %s (folder removed %s, record dropped %s, leftover %s)",
            working_copy,
            removed,
            dropped,
            self.forget_leftover or "none",
        )
        return dropped, removed

    # -- conflicts: what the dialog shows, and the answers -------------------------------

    def conflict_items(
        self, working_copy: Path, names: dict[str, str] | None = None
    ) -> list[project_conflict_view.Item]:
        """The recorded conflicts as the dialog's rows (*names*: a test's folder → the name the
        list shows); each test's version told on both sides from its files and its releases
        (:mod:`conflict_versions`: the server's read into a scratch folder)."""
        origin = self._origin_or_raise(working_copy)
        names = names or {}
        source = self._source_of(origin)
        if source is not None:
            with tempfile.TemporaryDirectory(prefix="procedo-conflict-") as scratch:
                return project_conflict_view.items(
                    origin.conflicts.values(),
                    name_of=lambda test: names.get(test, test),
                    version_here=lambda test: conflict_versions.here(Path(working_copy), test),
                    version_there=conflict_versions.ThereFrom(
                        lambda test, folder: project_sync_api.remote_test(
                            Path(working_copy), source, test, folder, OpContext()
                        ),
                        Path(scratch),
                    ),
                )
        tree = self._tree_for(origin)
        base = self._base(tree, origin.base)
        try:
            with (
                tempfile.TemporaryDirectory(prefix="procedo-conflict-") as scratch,
                project_sync_pass.operation(tree, OpContext()),
            ):
                return project_conflict_view.items(
                    origin.conflicts.values(),
                    name_of=lambda test: names.get(test, test),
                    version_here=lambda test: conflict_versions.here(Path(working_copy), test),
                    version_there=conflict_versions.There(tree, base, Path(scratch)),
                )
        except OSError as exc:
            raise os_error(exc) from exc

    def compare(self, working_copy: Path, paths: list[str]) -> list[project_conflict_view.Pair]:
        """Each of *paths* as this PC and the server hold it, for Compare. Reads only."""
        origin = self._origin_or_raise(working_copy)
        source = self._source_of(origin)
        if source is not None:
            found = {
                rel: (mine, theirs)
                for rel, mine, theirs in project_sync_api.compare(Path(working_copy), source, paths)
            }
            return project_conflict_view.pairs(
                paths, lambda rel: found[rel][0], lambda rel: found[rel][1]
            )
        tree = self._tree_for(origin)
        base = self._base(tree, origin.base)

        def mine(rel: str) -> bytes | None:
            path = project_sync_scan.local_path(Path(working_copy), rel)
            try:
                return path.read_bytes()
            except FileNotFoundError:
                return None

        try:
            with project_sync_pass.operation(tree, OpContext()):
                return project_conflict_view.pairs(
                    paths,
                    mine,
                    lambda rel: conflict_versions.read_or_none(
                        tree, base.joinpath(*rel.split("/"))
                    ),
                )
        except OSError as exc:
            raise os_error(exc) from exc

    def resolve(
        self, working_copy: Path, paths: list[str], keep: str, *, ctx: OpContext | None = None
    ) -> SyncReport:
        """Answer the recorded conflicts on *paths* (``project_sync.resolve``)."""
        origin = self._origin_or_raise(working_copy)
        source = self._source_of(origin)
        if source is not None:
            return project_sync_api.resolve(Path(working_copy), source, paths, keep, ctx=ctx)
        tree = self._tree_for(origin)
        return project_sync.resolve(
            Path(working_copy), tree, paths, keep, self._base(tree, origin.base), ctx=ctx
        )

    # -- only the open project's copy is kept ------------------------------------------

    def tidy(self, keep: Path | None) -> list[working_copy_cleanup.Outcome]:
        """Remove every working copy of a share project but *keep* (the open one) that nothing
        waits in and nobody uses (``working_copy_cleanup``), after finishing what an earlier
        removal left. Local disk only: nothing asks a share."""
        working_copy_cleanup.sweep_removing()
        return [
            working_copy_cleanup.remove_if_clean(copy)
            for copy in working_copy_cleanup.candidates(keep)
        ]

    def push_kept(
        self, working_copy: Path, *, ctx: OpContext | None = None
    ) -> working_copy_cleanup.Outcome:
        """A copy kept because it holds changes: a pull, then a push — with no approval, so
        only the app's own removals leave the share — under its sync lock, then removed if that
        settled it. Another window syncing it, a share that does not answer, a new conflict:
        it stays kept, as it was, and is tried again later."""
        lock = SyncLock.take(working_copy)
        if lock is None:
            return working_copy_cleanup.Outcome(
                Path(working_copy), False, working_copy_cleanup.SYNCED_ELSEWHERE
            )
        try:
            claimed = working_copy_claims.holder(working_copy)
            if claimed:  # opened, or run from, since it was kept: not an "other" copy any more
                return working_copy_cleanup.Outcome(
                    Path(working_copy), False, f"in use ({claimed})"
                )
            self.pull(
                working_copy,
                ctx=ctx,
                hold=lambda: working_copy_claims.live(working_copy, working_copy_claims.RUN),
            )
            self.push(working_copy, ctx=ctx)
        except (OSError, EvidenceError) as exc:
            log.info("kept working copy %s not pushed: %s", working_copy, exc)
            return working_copy_cleanup.Outcome(Path(working_copy), False, str(exc), waiting=True)
        finally:
            lock.release()
        return working_copy_cleanup.remove_if_clean(working_copy)

    # -- where working copies live ----------------------------------------------------

    def working_copy_dir(self, project_id: str, base: str) -> Path:
        """This machine's folder for a project that is about to be published."""
        return project_local_state.working_copy_dir(project_id, base)

    def local_trash(self, working_copy: Path) -> Path | None:
        """Where this PC keeps what the sync took out of *working_copy*, if anything.

        ``None`` when there is nothing in it. *Forget* names it: the trash is in the
        state folder, not in the project, so removing the working copy does NOT take
        it — which is the point, since it may hold the only copy of something.
        """
        trash = project_local_state.local_trash_dir(working_copy)
        if trash is None or not trash.is_dir() or not any(trash.iterdir()):
            return None
        return trash


_DEFAULT = SyncGateway()


def gateway() -> SyncGateway:
    """The gateway the GUI uses. The seam: a test monkeypatches this name."""
    return _DEFAULT
