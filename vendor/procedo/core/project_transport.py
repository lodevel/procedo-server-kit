"""The project transports: which backend builds what an origin record describes.

Two kinds of server, each a registry rather than a branch, so the sync engines name no
transport in their own logic:

* a TREE (:data:`TREE_FACTORIES`): a folder of files the sync lists and copies
  (:mod:`project_sync`) — a share, FTPS, SFTP;
* a PROJECT SOURCE (:data:`SOURCE_FACTORIES`): a server that keeps projects itself and is read
  by what changed (:class:`project_store.ProjectSource`, :mod:`project_sync_api`) — the web
  server's JSON API, the evidence database. The class that declares such a server's fields (its
  params, the ones its sign-in is filed under, its reconnect params) is
  :func:`source_class`'s.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from .evidence_sftp_conn import paramiko_available
from .evidence_store import EvidenceError
from .evidence_tree import TreeIO
from .project_origin import Origin
from .server_certificate import without_choice

FTPS = "ftps"
SFTP = "sftp"
HTTP = "http"
POSTGRES = "postgres"
FILESYSTEM = "filesystem"

Credentials = Callable[[str], tuple[str, str]]  # identity -> (user, password)
TreeFactory = Callable[[dict[str, Any], "Credentials | None"], TreeIO]
SourceFactory = Callable[[dict[str, Any], "Credentials | None"], Any]

#: Backend name -> what builds its tree out of an origin record's parameters. A
#: registry rather than a branch, the way ``evidence_store.REGISTRY`` is one: a
#: transport added later is an entry here and nothing else, and the sync engine names
#: no transport in its own logic.
TREE_FACTORIES: dict[str, TreeFactory] = {}


def register_tree(backend: str, factory: TreeFactory) -> None:
    TREE_FACTORIES[backend] = factory


def _ftps_tree(params: dict[str, Any], credentials: Credentials | None) -> TreeIO:
    from .evidence_ftps import FtpsTree
    from .evidence_ftps_conn import FtpsEndpoint

    try:
        endpoint = FtpsEndpoint(**params)
    except TypeError as exc:
        raise EvidenceError(
            "invalid", f"the origin record's parameters are not an endpoint: {exc}"
        ) from exc
    return FtpsTree(endpoint, credentials)


def _sftp_tree(params: dict[str, Any], credentials: Credentials | None) -> TreeIO:
    """An SFTP server's folder (slice 6b). The key file and the confirmed host key come with
    the credential lookup (:class:`evidence_store.SignIn`), never with the record."""
    from .evidence_sftp import SftpTree
    from .evidence_sftp_conn import SftpEndpoint

    try:
        endpoint = SftpEndpoint(**params)
    except TypeError as exc:
        raise EvidenceError(
            "invalid", f"the origin record's parameters are not an endpoint: {exc}"
        ) from exc
    return SftpTree(endpoint, credentials)


def _filesystem_tree(params: dict[str, Any], _credentials: Credentials | None) -> TreeIO:
    """A project reached as a plain path. Not a transport the app needs — a share is
    opened in place — but it is what the tests drive as their server, and what keeps
    the registry a registry instead of a branch with one arm."""
    from .evidence_store_fs_shared import FsTree

    root = str(params.get("root") or "")
    if not root:
        raise EvidenceError("invalid", "a filesystem origin needs a root")
    return FsTree(Path(root))


register_tree(FTPS, _ftps_tree)
register_tree(FILESYSTEM, _filesystem_tree)
if paramiko_available():  # the ``sftp`` extra: without it no picker offers SFTP
    register_tree(SFTP, _sftp_tree)

#: Backend name -> what builds its :class:`project_store.ProjectSource` out of an origin
#: record's parameters.
SOURCE_FACTORIES: dict[str, SourceFactory] = {}


def _api_source(params: dict[str, Any], credentials: Credentials | None) -> Any:
    from .api_session import ApiSession
    from .evidence_api_http import ApiEndpoint
    from .project_store_api import ApiProjectSource

    try:
        endpoint = ApiEndpoint(**without_choice(params))  # the switch is this PC's alone
    except TypeError as exc:
        raise EvidenceError(
            "invalid", f"the origin record's parameters are not an endpoint: {exc}"
        ) from exc
    return ApiProjectSource(ApiSession(endpoint, credentials), cache_dir())


def _db_source(params: dict[str, Any], credentials: Credentials | None) -> Any:
    """The evidence database's projects, with this PC's machine id, as its evidence store
    connects (:meth:`evidence_store_db.DatabaseStore.from_config`)."""
    from . import app_settings
    from .evidence_store_db import DatabaseStore
    from .project_store_db import DbProjectSource

    store = DatabaseStore.from_config(
        {"backend": POSTGRES, "params": params},
        project_root=None,
        machine_id=app_settings.get_machine_id,
        credentials=credentials,
    )
    return DbProjectSource(store)


SOURCE_FACTORIES[HTTP] = _api_source
SOURCE_FACTORIES[POSTGRES] = _db_source


def _api_class() -> type:
    from .evidence_store_api import ApiStore

    return ApiStore


def _db_class() -> type:
    from .evidence_store_db import DatabaseStore

    return DatabaseStore


#: Backend name -> the store class that declares a project source's fields (``PARAMS``,
#: ``ENDPOINT_PARAMS``, ``sync_params``): asked here rather than in the evidence registry, which
#: offers only what may be picked as an evidence storage.
SOURCE_CLASSES: dict[str, Callable[[], type]] = {HTTP: _api_class, POSTGRES: _db_class}


def source_class(backend: str) -> type | None:
    """The class that declares the fields of the project source *backend* (``None``: none)."""
    found = SOURCE_CLASSES.get(backend)
    return found() if found is not None else None


def cache_dir() -> Path:
    """Where this PC keeps the large project files a server sent alone (B2), by version."""
    from . import app_identity

    return app_identity.local_data_dir() / "project_files"


def reads_by_changes(backend: str) -> bool:
    """Whether a project on *backend* is a project source's (:data:`SOURCE_FACTORIES`)."""
    return backend in SOURCE_FACTORIES


def source_for(backend: str, params: dict[str, Any], credentials: Credentials | None = None) -> Any:
    """The project source of *backend* at *params*, with the injected credential lookup."""
    factory = SOURCE_FACTORIES.get(backend)
    if factory is None:
        raise EvidenceError("invalid", f"no project server for backend {backend!r}")
    return factory(params, credentials)


def tree_for(origin: Origin, credentials: Credentials | None = None) -> TreeIO:
    """The tree an origin record describes, built by its backend's registered factory.

    *credentials* is the lookup ``identity -> (user, password)``, injected as
    ``open_store`` injects it — never read from the record, which holds no secret.
    """
    factory = TREE_FACTORIES.get(origin.backend)
    if factory is None:
        raise EvidenceError(
            "invalid",
            f"no project transport for backend {origin.backend!r}; this app has "
            f"{sorted(TREE_FACTORIES)}",
        )
    return factory(origin.params, credentials)
