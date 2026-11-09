"""SFTP evidence backend — the shared layout on an SFTP server (slice 6b).

:class:`SftpStore` is :class:`evidence_server_store.ServerTreeStore` over
:class:`evidence_sftp.SftpTree`, the shape of :class:`evidence_store_ftps.FtpsStore`:
staging, the outbox, generations, the pointer lock, the index and *Move evidence…* work
unchanged, because they are written over the tree.

* **Its params are where the server is**: host, port (22), the base path and the user. No
  TLS switch, no passive mode, no CA bundle: the server's key is checked instead, pinned on
  this PC (:mod:`evidence_known_hosts`). How this PC signs in — a password or a key file — is
  chosen in the sign-in box per PC and kept with the sign-in (owner, 2026-11-06), so it never
  travels with a project.
* **The credential, the key file and the pin are never params**: they reach the store as the
  injected lookup every backend is handed (an :class:`evidence_store.SignIn`), keyed by the
  server's address (:meth:`credential_key`, host and port).
* **Optional.** paramiko is the ``sftp`` extra: without it this backend does not register,
  so no picker offers SFTP, and an entry naming it reads "sftp (not installed)"
  (:func:`evidence_config.unknown_type`), untouched.

Registered by import side effect, exactly as the FTPS backend is.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from .evidence_server_store import ServerTreeStore
from .evidence_sftp import SftpTree
from .evidence_sftp_conn import SftpEndpoint, paramiko_available
from .evidence_store import AccessReport, EvidenceError, OpContext, ParamSpec, register

ID = "sftp"


def _endpoint(params: dict[str, Any]) -> SftpEndpoint:
    """An endpoint from a config entry's declared params (never the credential)."""
    host = str(params.get("host") or "").strip()
    if not host:
        raise EvidenceError("invalid", "the SFTP backend needs a host")
    base_path = str(params.get("base_path") or "").strip()
    if not base_path:
        raise EvidenceError("invalid", "the SFTP backend needs a base path on the server")
    try:
        port = int(params.get("port") or 22)
    except (TypeError, ValueError):
        raise EvidenceError("invalid", f"not a port number: {params.get('port')!r}") from None
    return SftpEndpoint(
        host=host,
        port=port,
        base_path=base_path,
        user=str(params.get("user") or ""),
        timeout_s=float(params.get("timeout_s") or 30.0),
    )


def sftp_params(endpoint: SftpEndpoint) -> dict[str, Any]:
    """An :class:`evidence_sftp_conn.SftpEndpoint` as reconnect parameters (an origin record,
    a library): field by field, and no credential among them."""
    return {
        "host": endpoint.host,
        "port": endpoint.port,
        "base_path": endpoint.base_path,
        "user": endpoint.user,
        "timeout_s": endpoint.timeout_s,
    }


class SftpStore(ServerTreeStore):
    """``backend: sftp``."""

    ID = ID
    PARAMS = [
        ParamSpec("host", "Host", "str", True, None),
        ParamSpec("port", "Port", "str", False, "22"),
        ParamSpec("base_path", "Base path on the server", "str", True, ""),
        ParamSpec("user", "User", "str", True, None),
    ]
    TYPE_LABEL = "SFTP server"
    ENDPOINT_PARAMS = ("host", "port", "base_path")
    #: The sign-in box offers "Sign in with: Password / Key file" and the server's key
    #: (``bench/sftp_sign_in.py``) for a backend that says so.
    SIGNS_IN_WITH_KEY = True
    read_parallelism = 2
    #: SFTP listings are structured and never localized: ``query`` is always there.
    CAPABILITIES = frozenset({"read", "write", "sweep", "query"})

    _TREE = SftpTree
    _parse_endpoint = staticmethod(_endpoint)

    @classmethod
    def sync_params(cls, spec: dict[str, Any]) -> dict[str, Any]:
        """This entry's endpoint as project-sync reconnect parameters (never a credential)."""
        return sftp_params(_endpoint(spec.get("params") or {}))

    @classmethod
    def credential_params(cls, key: str) -> dict[str, str]:
        """The inverse of :meth:`credential_key` (``sftp://host:port``); ``{}`` for any other."""
        try:
            parts = urlsplit(key)
            host, port = parts.hostname, parts.port
        except ValueError:
            return {}
        if parts.scheme != ID or not host or not port:
            return {}
        return {"host": host, "port": str(port)}

    def _missing_folder_note(self) -> str:
        return f"Signed in, but {self.root} is not on the server. Check Base path on the server."

    def sign_in(self, ctx: OpContext | None = None) -> AccessReport:
        """The shared sign-in test, its "may not read the folder" refusal in a sentence."""
        try:
            return super().sign_in(ctx)
        except EvidenceError as exc:
            if exc.kind == "denied" and not getattr(exc.__cause__, "host_key_state", ""):
                user = self.endpoint.user or "this user"
                raise EvidenceError(
                    "denied",
                    f"Signed in, but {user} may not read {self.root}. Ask the server's "
                    "administrator.",
                ) from exc
            raise


if paramiko_available():  # without the extra nothing registers, so nothing offers SFTP
    register(SftpStore)
