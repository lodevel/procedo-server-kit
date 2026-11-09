"""The SFTP host keys this machine trusts — in the machine scope, never in a project's config.

``docs/plans/evidence_db_design.md`` ("SFTP: a full storage type", Host key): an SFTP server's
host key is pinned per server, keyed by the key its sign-in is filed under
(:attr:`evidence_sftp_conn.SftpEndpoint.credential_key`, host and port), in
``%APPDATA%/Procedo/app_settings.json``::

    "evidence_known_hosts": {
        "sftp://bench-sftp:22": {"key": "ssh-ed25519 SHA256:…", "confirmed": "2026-11-06"}
    }

A pin is the type and OpenSSH SHA256 fingerprint :func:`evidence_sftp_conn.fingerprint`
writes. It is written only when the user accepts the fingerprint the server offered
(:func:`pin`), never by a connection on its own: there is no trust on first use.
:func:`pinned` is the lookup injected into an :class:`evidence_sftp.SftpTree` (a tree reads no
settings itself). A host key is public: nothing here is a secret.

Qt-free, stdlib only, imports no backend.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from . import app_settings
from .evidence_store import EvidenceError

log = logging.getLogger(__name__)

SECTION = "evidence_known_hosts"


def _pins(data: dict[str, Any] | None = None) -> dict[str, Any]:
    found = (data if data is not None else app_settings.load_app_settings()).get(SECTION)
    return found if isinstance(found, dict) else {}


def _pin(server: str) -> dict[str, Any]:
    found = _pins().get(server)
    return found if isinstance(found, dict) else {}


def pinned(server: str) -> str:
    """The host key pinned for *server* (``sftp://host:port``), ``""`` when none is."""
    return str(_pin(server).get("key") or "").strip()


def confirmed_on(server: str) -> date | None:
    """The day the user confirmed *server*'s key on this PC (what the Host key row says)."""
    try:
        return date.fromisoformat(str(_pin(server).get("confirmed") or ""))
    except ValueError:
        return None


def pin(server: str, fingerprint: str) -> None:
    """Trust *fingerprint* as *server*'s host key from now on (replacing any earlier pin): what
    accepting the fingerprint shown on first contact, or a deliberately changed key, does. It
    raises when the pin is not stored, since a trust that silently did not happen would meet the
    same refusal again with no idea why."""
    wanted = fingerprint.strip()
    if not server or not wanted:
        raise EvidenceError("invalid", "a host key pin needs its server and its fingerprint")

    def mutate(data: dict[str, Any]) -> Any:
        data[SECTION] = {
            **_pins(data),
            server: {"key": wanted, "confirmed": date.today().isoformat()},
        }

    try:
        app_settings.update_app_settings(mutate)
    except OSError as exc:
        raise EvidenceError(
            "denied", f"the host key of {server} could not be stored: {exc}"
        ) from exc
    if pinned(server) != wanted:
        raise EvidenceError(
            "denied",
            f"the host key of {server} was not stored — check that "
            f"{app_settings.get_app_settings_file()} is writable",
        )
    log.info("Host key pinned for %s: %s", server, wanted)


def forget(server: str) -> bool:
    """Trust no host key for *server* any more. True when one was pinned."""

    def mutate(data: dict[str, Any]) -> Any:
        pins = dict(_pins(data))
        if pins.pop(server, None) is None:
            return False
        data[SECTION] = pins

    if not app_settings.update_app_settings(mutate):
        return False
    log.info("Host key pin removed for %s", server)
    return True
