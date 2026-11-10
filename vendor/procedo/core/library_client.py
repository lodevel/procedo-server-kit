"""The library ON THE SERVER — list, download into this PC's library, publish.

Owner's rule 8: *"Templates aren't linked to a project in particular, I would
have expected local templates and server templates, as bundles"*, and *"Must be
able to download server bundles and templates to also have them locally."*

The server is the one picked in the Library window's *Server* list, the first of this
PC's servers until one is (:func:`library_server`), reached exactly the way a project on a server is reached: the
entry is converted to reconnect parameters by the backend that declared its fields. A
server of folders (FTPS, a share) is the :class:`ServerLibrary` here, its tree built by that
backend's factory in :data:`project_transport.TREE_FACTORIES`; the web server is
:class:`library_api.ApiLibrary` (L1–L3), over the same session as its projects
(:func:`configured`).

**Where a library sits on a server of folders**: folders under the endpoint's
base path, ending at ``<id>/<version>/``, the layout the local libraries already have, so a
download is a copy and an upload is the same files back — nothing is packed, unpacked or
re-hashed to move between them.

**Identity is ``<id>@<version>``; content is the hash.** The copy here is
compared by computing the digest of its bytes, the copy there by the hash its
manifest declares (:mod:`library_entry`). Equal is the same thing: a download
writes nothing at all. Different is a **conflict** and neither side is touched —
the app never auto-replaces a library item, and the sentence names the id, the
version and the first eight characters of both hashes.

**Publishing is create-only per version.** A version already on the server is
refused whatever it holds; a corrected build gets a new version. The files go up
under a dot-named staging folder and the whole folder is renamed into place
last, so a transfer that stops half way leaves nothing a picker can see — and
the local copy is never rewritten, not even to record the hash it declares.

Qt-free: the dialog runs these on the sync worker.
"""

from __future__ import annotations

import logging
import shutil
import uuid
from collections.abc import Callable
from pathlib import Path, PurePath
from typing import Any

from . import (
    app_settings,
    bundle_registry,
    config_manager,
    evidence_credentials,
    export_packages,
    library_entry,
    project_sync_gateway,
    project_sync_pass,
    project_transport,
    server_offer,
    server_settings,
)
from .evidence_store import EvidenceError, OpContext, credential_key
from .evidence_tree import TreeIO, make_dirs, read_json
from .library_entry import KINDS, Entry
from .project_origin import Origin

log = logging.getLogger(__name__)

#: Where each kind lives on THIS PC. Called, never bound: a test redirects a
#: library by patching the module attribute these read.
LOCAL_ROOTS: dict[str, Callable[[], Path]] = {
    library_entry.BUNDLE: lambda: bundle_registry.bundle_store_path(),
    library_entry.EXPORT: lambda: export_packages.local_exports_dir(),
    library_entry.REPORT: lambda: export_packages.local_reports_dir(),
    library_entry.CONFIG: lambda: config_manager.get_configs_dir(),
}

#: Prefix of a folder that is being written. Dot-named, so every loader in the
#: app — and this module's own listing — skips it.
STAGING = ".staging-"


class LibraryConflict(EvidenceError):
    """The same ``id@version`` on both sides holding different content, or a
    version that is already published. Nothing was written either way.

    An :class:`evidence_store.EvidenceError` so every caller that already handles
    one handles this; its own class so a dialog can say "conflict" rather than
    "the server refused".
    """

    def __init__(self, message: str) -> None:
        super().__init__("invalid", message)


# ---------------------------------------------------------------------------
# This PC's side
# ---------------------------------------------------------------------------


def local_folder(kind: str, item_id: str, version: str) -> Path:
    """Where this PC keeps ``<id>@<version>`` of *kind*.

    A configuration is the one kind with no version level: a bench keeps one
    version of a name (``company_storage_design.md`` §5) and its manifest says which.
    """
    root = LOCAL_ROOTS[kind]() / item_id
    return root / version if KINDS[kind].local_versions else root


def local_entry(kind: str, item_id: str, version: str) -> Entry | None:
    """What this PC holds under that id and version, or ``None``.

    A configuration answers only for the version it actually has: asked about
    another one it says "nothing", because the one folder it has is taken.
    """
    entry = library_entry.read(kind, local_folder(kind, item_id, version))
    return entry if entry is not None and entry.version == version else None


def local_entries(kind: str) -> list[Entry]:
    """Every version of every id this PC holds, by ``(id, version)``."""
    root = LOCAL_ROOTS[kind]()
    if not root.is_dir():
        return []
    out: list[Entry] = []
    for id_dir in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")):
        folders = (
            sorted(p for p in id_dir.iterdir() if p.is_dir() and not p.name.startswith("."))
            if KINDS[kind].local_versions
            else [id_dir]
        )
        out += [e for e in (library_entry.read(kind, f) for f in folders) if e is not None]
    return sorted(out, key=lambda e: (e.id, e.version))


def add_template(kind: str, folder: Path) -> tuple[Entry, bool]:
    """Take the template *folder* (``schema.json`` beside its template) into this PC's
    library of *kind* under the id and version its ``schema.json`` gives: ``(its entry,
    whether it was copied)``.

    The same id and version here with the same content: nothing to do, ``False``. Other
    content under that version: refused, nothing written — a library item is never replaced.
    A folder that is not a template of *kind* is refused, saying which file is missing. The
    files are staged under a dot-name and renamed into place last, as a download is, and the
    staged copy is what is compared: a ``__pycache__`` or a shell file in *folder* is not content.
    """
    folder = Path(folder)
    spec = KINDS[kind]
    package = _template_in(kind, folder)
    for value in (package.package_id, package.version):
        if not bundle_registry.is_safe_component(value):
            # Hardened at the point of use: the id and the version become folder names.
            raise EvidenceError(
                "invalid",
                f"{value!r} in {folder / spec.manifest} cannot name a folder: use letters, "
                "digits, '.', '_' and '-'.",
            )
    target = local_folder(kind, package.package_id, package.version)
    root = LOCAL_ROOTS[kind]()
    root.mkdir(parents=True, exist_ok=True)
    staging = root / f"{STAGING}{uuid.uuid4().hex[:8]}"
    try:
        shutil.copytree(
            folder,
            staging,
            ignore=shutil.ignore_patterns("__pycache__", *library_entry.OS_SHELL_BASENAMES),
        )
        added = library_entry.read(kind, staging)
        if added is None or (added.id, added.version) != (package.package_id, package.version):
            # The manifest changed under the copy.
            raise EvidenceError("invalid", f"{folder} changed while it was copied: add it again.")
        here = library_entry.read(kind, target)
        if here is not None:
            if here.version == added.version and here.content_sha256 == added.content_sha256:
                log.info("%s %s is already here, unchanged", kind, added.ref)
                return here, False
            raise LibraryConflict(other_content_sentence(added))
        if target.exists():
            raise LibraryConflict(
                f"{target} is already there and is not a library item (no {spec.manifest}). "
                "Nothing was added — remove it first."
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        staging.replace(target)
    finally:
        _drop_staging(staging)
    log.info("Added %s %s from %s into %s", kind, added.ref, folder, target)
    return library_entry.read(kind, target) or added, True


def _template_in(kind: str, folder: Path) -> export_packages.ExportPackage:
    """The template *folder* holds, of *kind*; refused, saying why, when it holds none."""
    manifest = KINDS[kind].manifest
    if not (folder / manifest).is_file():
        raise EvidenceError("invalid", f"{folder} is not a template: {manifest} is missing.")
    try:
        package = export_packages.load_export_package(folder, "library")
    except (OSError, ValueError) as exc:
        raise EvidenceError("invalid", f"{folder} is not a template: {exc}.") from exc
    takes = export_packages.RENDER_KINDS[kind]
    if package.kind not in takes:
        raise EvidenceError(
            "invalid",
            f"{KINDS[kind].label}s take kind {' or '.join(repr(k) for k in takes)}: "
            f"{folder / manifest} says kind {package.kind!r}.",
        )
    return package


def other_content_sentence(entry: Entry) -> str:
    """A template whose id and version this PC's library holds with other content."""
    field = KINDS[entry.kind].version_field
    return (
        f"{entry.id} {entry.version} is already in this PC's library with other content: "
        f"raise {field} in {KINDS[entry.kind].manifest}."
    )


def newest_per_id(entries: list[Entry]) -> list[Entry]:
    """The newest version of each id — what a picker offers unless it was built
    to show them all."""
    best: dict[str, Entry] = {}
    for entry in entries:
        current = best.get(entry.id)
        if current is None or library_entry.version_key(entry.version) > library_entry.version_key(
            current.version
        ):
            best[entry.id] = entry
    return [best[k] for k in sorted(best)]


# ---------------------------------------------------------------------------
# The server's side
# ---------------------------------------------------------------------------


class ServerLibrary:
    """The libraries on one server, over one transport.

    Built from reconnect parameters — what a backend's ``sync_params`` makes of
    the configured entry — so this module never reads a form and never spells a
    transport's name.
    """

    def __init__(
        self,
        backend: str,
        params: dict,
        credentials: Callable[[str], tuple[str, str]] | None = None,
    ) -> None:
        self.backend = backend
        self.params = dict(params)
        self._credentials = credentials or evidence_credentials.lookup

    # -- where a kind lives on this server ---------------------------------------

    def tree_for(self, kind: str) -> tuple[TreeIO, PurePath]:
        """``(tree, the folder that holds <id>/<version>)`` for *kind*: the kind's folders
        under the base path the endpoint is rooted at."""
        spec = KINDS[kind]
        origin = Origin(
            backend=self.backend, identity="", base="", params=dict(self.params), project_id=""
        )
        tree = project_transport.tree_for(origin, self._credentials)
        root = tree.root
        for part in (spec.store, spec.subpath):
            if part:
                root = root / part
        return tree, root

    # -- the three operations ------------------------------------------------------

    def list(self, kind: str, ctx: OpContext | None = None) -> list[Entry]:
        """Every ``<id>/<version>`` this server publishes of *kind*.

        A version folder with no manifest is not a library entry — the rule both
        local libraries already apply — so a publish that stopped half way is
        invisible here too.
        """
        tree, root = self.tree_for(kind)
        out: list[Entry] = []
        with project_sync_pass.operation(tree, ctx or OpContext()):
            for item_id in _child_names(tree, root):
                for version in _child_names(tree, root / item_id):
                    entry = _remote_entry(tree, kind, root, item_id, version)
                    if entry is not None:
                        out.append(entry)
        return sorted(out, key=lambda e: (e.id, e.version))

    def download(self, kind: str, item_id: str, version: str, ctx: OpContext | None = None) -> Path:
        """Copy ``<id>@<version>`` into this PC's library (:func:`install`); return its folder."""
        spec = KINDS[kind]
        tree, root = self.tree_for(kind)
        with project_sync_pass.operation(tree, ctx or OpContext()):
            remote = _remote_entry(tree, kind, root, item_id, version)
            if remote is None:
                raise EvidenceError(
                    "not_found", f"{item_id}@{version} is not in the {spec.label} library there."
                )
            return install(
                kind, remote, lambda staging: _fetch_tree(tree, root / item_id / version, staging)
            )

    def publish(self, kind: str, item_id: str, version: str, ctx: OpContext | None = None) -> Entry:
        """Send this PC's ``<id>@<version>`` up, create-only.

        A version already on the server is refused whatever it holds and nothing
        is written: a published version is never silently replaced. The files go
        up under a staged name and the folder is renamed into place last, so a
        transfer that stops half way leaves nothing a picker can see.
        """
        spec = KINDS[kind]
        entry = to_publish(kind, item_id, version)
        tree, root = self.tree_for(kind)
        with project_sync_pass.operation(tree, ctx or OpContext()):
            if _remote_entry(tree, kind, root, item_id, version) is not None:
                raise LibraryConflict(exists_sentence(spec.label, entry))
            if tree.is_dir(root / item_id / version):
                # A hand-made folder already occupies the slot, without a
                # manifest — said plainly here, or the rename either fails in
                # the transport's own words or lands inside it.
                raise LibraryConflict(
                    _debris_sentence(spec.label, spec.manifest, root / item_id / version)
                )
            staging = root / item_id / f"{STAGING}{uuid.uuid4().hex[:8]}"
            try:
                # From the tree's OWN root: a server that has never held this
                # kind has neither the store folder nor the id folder yet.
                make_dirs(tree, tree.root, staging)
                _put_tree(tree, entry.root, staging, skip=spec.manifest)
                # The manifest LAST and from memory: it carries the hash and the
                # size this copy declares, and the local library keeps its own.
                tree.write_atomic(
                    staging / spec.manifest,
                    library_entry.stamped_manifest(kind, entry.root),
                    "pub",
                )
                tree.rename(staging, root / item_id / version)
            except BaseException:
                _drop_remote_staging(tree, staging)
                raise
        log.info("Published %s %s@%s to %s", kind, item_id, version, root / item_id / version)
        return entry


def to_publish(kind: str, item_id: str, version: str) -> Entry:
    """This PC's ``<id>@<version>`` of *kind*, which a publish sends; ``not_found`` without it."""
    entry = local_entry(kind, item_id, version)
    if entry is None or entry.root is None:
        raise EvidenceError(
            "not_found", f"{item_id}@{version} is not in this PC's {KINDS[kind].label} library."
        )
    return entry


def install(kind: str, remote: Entry, fill: Callable[[Path], None]) -> Path:
    """``<id>@<version>`` the server publishes (*remote*, its declared hash) into this PC's
    library, *fill* writing its files into a staging folder; its folder.

    Content that is already here is not copied again — nothing at all is
    written. DIFFERENT content under the same name is a conflict: this
    machine's copy is left exactly as it is and the caller is told which two
    hashes disagree. What arrived must hold what its manifest declares, or
    nothing is installed (:func:`_require_declared`).
    """
    spec = KINDS[kind]
    target = local_folder(kind, remote.id, remote.version)
    local = library_entry.read(kind, target)
    # The VERSION too: a configuration keeps one folder per name, and the
    # same bytes under another version number are another library item.
    if local is not None and (local.version, local.content_sha256) == (
        remote.version,
        remote.content_sha256,
    ):
        log.info("%s %s is already here, unchanged", kind, remote.ref)
        return target
    if local is not None:
        raise LibraryConflict(_conflict_sentence(spec.label, remote, local))
    if target.exists():
        # A folder that is not a library entry — a hand-copy without its
        # manifest. Said plainly here, or the download ends in the
        # filesystem's own words about a name that already exists.
        raise LibraryConflict(
            f"{target} is already there and is not a {spec.label.lower()} "
            f"(no {spec.manifest}). Nothing was downloaded — remove it first."
        )
    staging = target.parent / f"{STAGING}{uuid.uuid4().hex[:8]}"
    staging.mkdir(parents=True, exist_ok=False)
    try:
        fill(staging)
        _require_declared(kind, staging, remote, spec.label)
        staging.replace(target)
    except BaseException:
        _drop_staging(staging)
        raise
    log.info("Downloaded %s %s into %s", kind, remote.ref, target)
    return target


#: The app_settings key naming the server the Library window was last pointed at.
PICKED = "library_server"


#: The web server the libraries were last found on (its identity; ``""``: none, or another
#: kind of server), as :func:`library_server` last read it: what :func:`not_offered` asks,
#: from memory.
_last_web_server = [""]


def library_server() -> server_settings.Server | None:
    """The server the libraries are on: the one picked last in the Library window,
    else the first of this PC's servers (the picked one renamed or removed too);
    ``None`` when it has none. Reads this PC's settings."""
    found = server_settings.servers()
    picked = str(app_settings.load_app_settings().get(PICKED) or "").strip().casefold()
    chosen = (s for s in found if s.name.strip().casefold() == picked)
    server = next(chosen, found[0] if found else None)
    web = server is not None and server.backend == server_offer.WEB_SERVER
    _last_web_server[0] = credential_key(server.spec) if web else ""
    return server


def pick_library_server(name: str) -> None:
    """Point every library at this PC's server called *name*, from now on."""
    app_settings.update_app_settings(lambda data: data.update({PICKED: name}))


def not_offered() -> str:
    """Why every entry point of the server's library is greyed: its server is a web server known
    not to offer the library (L1-L3, contract §6.1); ``""`` otherwise (another server, none, or
    one whose offer is not known yet). From memory alone (what :func:`library_server` last read,
    what the server last offered): a menu or a selection asks it, never a file or the server."""
    identity = _last_web_server[0]
    lacks = bool(identity) and server_offer.lacks_on(identity, server_offer.LIBRARY)
    return server_offer.NO_LIBRARY if lacks else ""


def refresh_offer() -> None:
    """Which server the libraries are on now, and what it offers when it is a web server (its
    ``/info``): read again after this PC's servers changed (Settings ▸ Server's Save), so
    :func:`not_offered` follows at once. A server that does not answer leaves its offer not
    known (logged). Reads this PC's settings and the server: on a worker."""
    try:
        client = configured()
    except EvidenceError as exc:
        log.info("The libraries' server cannot be opened: %s", exc)
        return
    session = getattr(client, "session", None)
    if session is None:
        return
    try:
        session.ready()  # /info: what it offers is noted (server_offer)
    except EvidenceError as exc:
        log.info("What the libraries' server offers could not be read: %s", exc)
    finally:
        session.close()


def configured() -> Any:
    """The library on :func:`library_server` (a :class:`project_store.LibrarySource`), or
    ``None`` when this PC has no server."""
    server = library_server()
    if server is None:
        return None
    gate = project_sync_gateway.gateway()
    params = gate.sync_params(server.spec)
    if not gate.has_folders(server.backend):
        from .library_api import ApiLibrary  # it builds on this module's rules

        session = gate.source(server.backend, params).session
        return ApiLibrary(session, project_transport.cache_dir())
    return ServerLibrary(server.backend, params)


# ---------------------------------------------------------------------------
# Moving the bytes
# ---------------------------------------------------------------------------


def _remote_entry(
    tree: TreeIO, kind: str, root: PurePath, item_id: str, version: str
) -> Entry | None:
    """The entry the server publishes there, read off its manifest — the one
    side that is not computed, because hashing it would mean downloading it."""
    spec = KINDS[kind]
    manifest = read_json(tree, root / item_id / version / spec.manifest)
    section = manifest.get(spec.section) if spec.section else manifest
    if not isinstance(section, dict) or not section:
        return None
    return Entry(
        kind,
        item_id,
        version,
        library_entry.declared_sha256(kind, section),
        library_entry.declared_size(kind, section),
    )


def _require_declared(kind: str, staging: Path, remote: Entry, label: str) -> None:
    """The downloaded folder holds exactly what its manifest declares, or nothing is installed:
    an item may hold code this PC will run, so a file changed, added or left out on the
    server, or damaged on the way, is refused before it lands
    (docs/plans/company_storage_design.md §8, L1)."""
    name = f"{remote.id}@{remote.version}"
    if not remote.content_sha256:
        raise EvidenceError(
            "corrupt",
            f"{label} {name} on the server declares no content hash, so what arrived cannot be "
            "checked. Nothing was installed: publish it again from a PC that has it.",
        )
    got = library_entry.read(kind, staging)
    if got is None or (got.id, got.version) != (remote.id, remote.version):
        raise EvidenceError(
            "corrupt", f"{label} {name} arrived without its own manifest. Nothing was installed."
        )
    if got.content_sha256 != remote.content_sha256:
        raise EvidenceError(
            "corrupt",
            f"{label} {name} on the server does not hold what its manifest declares "
            f"({got.short_sha} arrived, {remote.content_sha256[:8]} declared): changed there "
            "or damaged on the way. Nothing was installed.",
        )


def _child_names(tree: TreeIO, folder: PurePath) -> list[str]:
    """The sub-folders of *folder*, staged ones left out; none when it is absent."""
    try:
        names = tree.list_names(folder)
    except OSError:
        return []
    if names is None:
        return []
    return sorted(n for n in names if not n.startswith(".") and tree.is_dir(folder / n))


def _fetch_tree(tree: TreeIO, remote: PurePath, local: Path) -> None:
    """Every file below *remote*, into *local*, folder by folder."""
    names = tree.list_names(remote)
    if names is None:
        raise EvidenceError("not_found", f"{remote} is not a folder on the server")
    for name in sorted(names):
        source = remote / name
        if tree.is_dir(source):
            (local / name).mkdir(exist_ok=True)
            _fetch_tree(tree, source, local / name)
        else:
            (local / name).write_bytes(tree.read_bytes(source))


def _put_tree(tree: TreeIO, local: Path, remote: PurePath, *, skip: str = "") -> None:
    """Every file below *local*, up to *remote*; *skip* names one file at the
    ROOT that the caller sends itself, last."""
    for child in sorted(local.iterdir()):
        if child.name in library_entry.OS_SHELL_BASENAMES:
            continue
        if child.is_dir():
            tree.mkdir(remote / child.name)
            _put_tree(tree, child, remote / child.name)
        elif child.name != skip:
            tree.put_file(child, remote / child.name, "pub", verify=True)


def _drop_staging(staging: Path) -> None:
    """Remove a staging folder of OURS. Re-proved at the call: it is the name
    this module composed, under the library root — never a published folder."""
    if staging.name.startswith(STAGING) and staging.is_dir():
        shutil.rmtree(staging, ignore_errors=True)


def _drop_remote_staging(tree: TreeIO, staging: PurePath) -> None:
    """The same, on the server, and never a reason to lose the real failure."""
    if not staging.name.startswith(STAGING):
        return
    try:
        tree.rmtree(staging)
    except (OSError, EvidenceError):
        log.warning("Could not remove the staged upload at %s", staging, exc_info=True)


def _conflict_sentence(label: str, remote: Entry, local: Entry) -> str:
    return (
        f"{label} {remote.ref} is already on this PC as {local.ref}, holding different "
        f"content (here {local.short_sha or 'unknown'}…, there {remote.short_sha or 'unknown'}…). "
        f"Nothing was downloaded — rename or delete the copy here first."
    )


def exists_sentence(label: str, entry: Entry) -> str:
    return (
        f"{label} {entry.ref} is already on the server (here {entry.short_sha}…). Nothing "
        f"was uploaded — a published version is never replaced; publish a new version."
    )


def _debris_sentence(label: str, manifest: str, path: PurePath) -> str:
    return (
        f"{path} is already there and is not a published {label.lower()} "
        f"(no {manifest}). Nothing was uploaded — remove it there first."
    )
