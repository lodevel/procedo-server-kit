"""FTPS evidence backend — the shared layout on an FTPS server.

:class:`FtpsStore` is the shared layout's protocol
(:class:`evidence_shared_layout.SharedLayout`, immutable run generations) over
:class:`evidence_ftps.FtpsTree`, plus what only a server root does — the mirror of
what :class:`evidence_store_fs_shared.FilesystemStore` owns for a filesystem
root, shared with SFTP in :class:`evidence_server_store.ServerTreeStore` (Check access,
the sign-in test, the artifact validator):

* **Everything is staged on local disk**, as for every shared-layout store: ``stage``
  hands out a directory in the durable outbox, because the runner gives ``StagedRun.path`` to a subprocess
  that writes its own payload files; the shared layout's ``commit_run`` hashes that
  directory, writes the manifest and publishes the outbox entry. The push to the
  server is the flush, so the end of a test never waits on the network and a run is
  safe once the local entry is fsynced.
* **A missing base path is reported, never created** (``_mkdir_safe``), so a mistyped
  ``base_path`` cannot quietly grow a second, empty store. Since 2026-09-19 the base
  path IS the project's evidence folder — boards sit directly in it — and the folder's
  ownership marker, not its name, says whose evidence it holds.
* **``local_path`` materializes** into a per-machine cache, because consumers hand
  paths to ``open()`` and ``QUrl.fromLocalFile``. Generations are immutable, so a run's
  cache entry is always correct once complete; artifacts are overwritten by name
  (a draft on every save), so theirs is revalidated against the server's size and
  modification time before it is reused.
* **The store's own default folder is still evidence.** ``local_path`` answers with the
  materialization cache, and keeping that folder in a save dialog means "evidence", so
  the write routes through ``write_artifact`` and the outbox to the server; only a
  folder the user picks somewhere else is a plain file export.
* **The credential is never a param.** ``PARAMS`` declares where the store is, never
  who reaches it: a project's ``config.json`` travels to shares and packages. The
  secret arrives as an injected lookup keyed by :meth:`credential_key` (host and port:
  one sign-in per server), which :mod:`evidence_credentials` files it under in the
  machine scope.

Registered by import side effect, exactly as the filesystem backend is.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlsplit

from .evidence_ftps import FtpsTree
from .evidence_ftps_conn import FtpsEndpoint
from .evidence_remote_cache import flag
from .evidence_server_store import ServerTreeStore
from .evidence_store import EvidenceError, ParamSpec, register

log = logging.getLogger(__name__)


def _endpoint(params: dict[str, Any]) -> FtpsEndpoint:
    """An endpoint from a config entry's declared params (never the credential)."""
    host = str(params.get("host") or "").strip()
    if not host:
        raise EvidenceError("invalid", "the FTPS backend needs a host")
    # Required here as well as in ``validate_spec``: coercing an empty base path to
    # "/" made an entry the tab refuses build fine in code, silently pointing the
    # store at the whole server rather than at the folder the user meant to name.
    base_path = str(params.get("base_path") or "").strip()
    if not base_path:
        raise EvidenceError("invalid", "the FTPS backend needs a base path on the server")
    try:
        port = int(params.get("port") or 21)
    except (TypeError, ValueError):
        raise EvidenceError("invalid", f"not a port number: {params.get('port')!r}") from None
    return FtpsEndpoint(
        host=host,
        port=port,
        base_path=base_path,
        user=str(params.get("user") or ""),
        tls=flag(params, "tls", True),
        passive=flag(params, "passive", True),
        insecure=flag(params, "insecure", False),
        ca_bundle=str(params.get("ca_bundle") or ""),
    )


def ftps_params(endpoint: Any) -> dict[str, Any]:
    """An :class:`evidence_ftps_conn.FtpsEndpoint` as reconnect parameters.

    Field by field, and there is no credential among them because the endpoint has
    no password field at all: a secret reaches a backend through the injected
    lookup, never through a record.
    """
    return {
        "host": endpoint.host,
        "port": endpoint.port,
        "base_path": endpoint.base_path,
        "user": endpoint.user,
        "tls": endpoint.tls,
        "passive": endpoint.passive,
        "insecure": endpoint.insecure,
        "ca_bundle": endpoint.ca_bundle,
        "timeout_s": endpoint.timeout_s,
    }


@register
class FtpsStore(ServerTreeStore):
    """``backend: ftps``."""

    ID = "ftps"
    # The credential is NOT declared: secrets never reach a project's config.json,
    # which travels to shares and packages. It is looked up by endpoint instead.
    PARAMS = [
        ParamSpec("host", "Host", "str", True, None),
        ParamSpec("port", "Port", "str", False, "21"),
        ParamSpec("base_path", "Base path on the server", "str", True, ""),
        ParamSpec("user", "User", "str", True, None),
        ParamSpec("tls", "TLS", "choice:on|off", True, "on"),
        ParamSpec("passive", "Passive mode", "choice:on|off", True, "on"),
        ParamSpec(
            "insecure",
            "Plain FTP (sends the password and all evidence in clear)",
            "choice:off|on",
            True,
            "off",
        ),
        ParamSpec("ca_bundle", "CA bundle (optional)", "str", False, ""),
    ]
    #: The name a picker offers this backend under.
    TYPE_LABEL = "FTPS server"

    #: The declared params that form :meth:`endpoint_identity` — what the machine
    #: settings' credentials editor asks for to compute the key a secret is filed
    #: under. Declaring them is also how a backend says "I need a credential".
    ENDPOINT_PARAMS = ("host", "port", "base_path")
    #: Reads of one operation at once (:func:`evidence_store.read_each`), each read on
    #: ONE leased connection, pooled between reads — so also how many connections a
    #: snapshot holds at once. Kept low (owner, 2026-10-07): a server's per-address
    #: connection cap must never refuse a snapshot.
    read_parallelism = 2

    _TREE = FtpsTree
    _parse_endpoint = staticmethod(_endpoint)

    @classmethod
    def sync_params(cls, spec: dict[str, Any]) -> dict[str, Any]:
        """This entry's endpoint as PROJECT-SYNC reconnect parameters.

        A project that lives on this server is opened through a working copy whose
        origin record has to be able to rebuild the connection (``project_transport.
        tree_for``). Declaring the conversion here, beside :meth:`endpoint_identity`,
        is what keeps the GUI from coercing a port and a pair of on/off flags itself —
        the entry's params are strings out of a form, and a record built from them
        by hand reconnected to port ``"21"`` and TLS ``"off"`` meaning ``True``.
        Never a credential: the endpoint has no password field at all.
        """
        return ftps_params(_endpoint(spec.get("params") or {}))

    @classmethod
    def credential_params(cls, key: str) -> dict[str, str]:
        """The inverse of :meth:`credential_key` — what the credentials editor fills
        its fields with when the user picks a saved sign-in. ``{}`` for anything that
        is not one of this backend's keys.

        Parsed as the URL it is, not split on the first colon: an IPv6 literal carries
        its own (``ftps://[::1]:21``), and splitting there filled the editor with
        nothing at all while claiming the endpoint was fine.
        """
        try:
            parts = urlsplit(key)
            host, port = parts.hostname, parts.port
        except ValueError:
            return {}
        if parts.scheme != "ftps" or not host or not port:
            return {}
        return {"host": host, "port": str(port)}

    @property
    def CAPABILITIES(self) -> frozenset[str]:  # noqa: N802  (the protocol's spelling)
        """``query`` is advertised only while the server is known to have ``MLSD``:
        without it listings cannot be parsed authoritatively (localized ``LIST``
        output is not a protocol) and the index path covers the gap."""
        found = {"read", "write", "sweep"}
        return frozenset(found | {"query"}) if self.io.has_mlsd else frozenset(found)

    def _capability_notes(self) -> list[str]:
        if "query" not in self.CAPABILITIES:
            return ["the server has no MLSD: listings are unreliable"]
        return []
