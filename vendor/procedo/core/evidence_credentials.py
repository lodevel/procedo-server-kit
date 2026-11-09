"""Evidence-store credentials — on THIS machine, never in a project's config.

A project's ``config/config.json`` travels to shares and into ``.tpkg`` exports, so
no backend declares a credential parameter and nothing of the sort is ever written
into an entry. The secret lives in the machine scope
(``%APPDATA%/Procedo/app_settings.json``) instead, keyed per SERVER by the key the
backend computes (:func:`evidence_store.credential_key`: its address, never a folder
on it — :attr:`evidence_ftps_conn.FtpsEndpoint.credential_key` is host and port), so
every project and every evidence folder on one server share one sign-in::

    "evidence_credentials": {
        "ftps://bench-ftp:21": {"user": "bench", "secret": "dpapi:<base64>"},
        "sftp://bench-sftp:22": {"user": "lab", "secret": "dpapi:<passphrase>",
                                 "key_file": "dpapi:<the key file's path>"}
    }

An SFTP server is signed in to with a password or a private key file, chosen per PC
(owner, 2026-11-06): the key file's path is kept beside the secret, encrypted like it, and
the secret is then the key's passphrase. :func:`lookup` answers an
:class:`evidence_store.SignIn` that also carries the key file and the server's host key this
PC confirmed (:mod:`evidence_known_hosts`), so an SSH connection gets everything it needs
from the one lookup every backend is already handed.

The key is the backend's verbatim: a key spelled anywhere else would file a secret no
store looks up.

**Storage.** On Windows the secret is encrypted with DPAPI (``CryptProtectData``
through ``ctypes``, user scope, ``CRYPTPROTECT_UI_FORBIDDEN`` so it can never block
on a prompt), passing **the endpoint identity as the optional entropy**: a blob
copied under another endpoint's entry does not decrypt, so moving a secret between
endpoints by hand fails loudly instead of silently authenticating somewhere else.
Anywhere else there is no OS key store to use, and the secret is stored in clear
behind a visible warning (:data:`PLAINTEXT_WARNING`) rather than behind an
encryption this app would have to keep the key for — which would not be encryption.
The scheme rides in the stored value (``dpapi:`` / ``plaintext:``), so a plaintext
blob is never mistaken for an encrypted one.

**No environment fallback**: the Settings field is the single source, and an empty
field means no credential (the store then logs in with an empty password and the
server's refusal surfaces as ``auth``). A stored secret this machine or user cannot
decrypt is an ``auth`` error naming what to do, never a silent empty password.

Qt-free, stdlib only, imports no backend.
"""

from __future__ import annotations

import base64
import binascii
import logging
import os
from pathlib import PureWindowsPath
from typing import Any

from . import app_settings, evidence_known_hosts
from .evidence_store import EvidenceError, SignIn

log = logging.getLogger(__name__)

SECTION = "evidence_credentials"
#: An SSH server's entry may also name the private key file this PC signs in with
#: (encrypted like the secret, which is then its passphrase).
KEY_FILE = "key_file"
DPAPI = "dpapi"
PLAINTEXT = "plaintext"
PLAINTEXT_WARNING = (
    "stored in clear in this machine's app_settings.json — Windows DPAPI is not "
    "available here and this app will not pretend to encrypt it; the file is kept "
    "readable by this account alone (0600), without which every other account on "
    "the machine could read the password"
)
_CRYPTPROTECT_UI_FORBIDDEN = 0x1


def dpapi_available() -> bool:
    """Whether a saved secret is encrypted (Windows) or kept in clear."""
    return os.name == "nt"


# ---------------------------------------------------------------------------
# DPAPI through ctypes (Windows only; no new dependency)
# ---------------------------------------------------------------------------


def _crypt(name: str, data: bytes, entropy: bytes) -> bytes:
    """``CryptProtectData`` / ``CryptUnprotectData`` with ``entropy`` bound in.

    The buffers are held in locals for the whole call: a ``DATA_BLOB`` points into
    one, and letting it go out of scope would leave the pointer dangling.
    """
    import ctypes
    from ctypes import wintypes

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    data_buf = ctypes.create_string_buffer(data, len(data))
    entropy_buf = ctypes.create_string_buffer(entropy, len(entropy))
    given = _Blob(len(data), ctypes.cast(data_buf, ctypes.POINTER(ctypes.c_char)))
    salt = _Blob(len(entropy), ctypes.cast(entropy_buf, ctypes.POINTER(ctypes.c_char)))
    out = _Blob()
    ok = getattr(ctypes.windll.crypt32, name)(
        ctypes.byref(given),
        None,  # description: never a place for anything about the secret
        ctypes.byref(salt),
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out),
    )
    if not ok:
        raise OSError(ctypes.GetLastError(), f"{name} failed")  # type: ignore[attr-defined]
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def encode_secret(secret: str, identity: str) -> str:
    """The value stored for ``identity`` — ``""`` for no credential at all."""
    if not secret:
        return ""
    raw = secret.encode("utf-8")
    if dpapi_available():
        sealed = _crypt("CryptProtectData", raw, identity.encode("utf-8"))
        return f"{DPAPI}:{base64.b64encode(sealed).decode('ascii')}"
    log.warning("Evidence credential for %s is %s", identity, PLAINTEXT_WARNING)
    return f"{PLAINTEXT}:{base64.b64encode(raw).decode('ascii')}"


def decode_secret(stored: str, identity: str) -> str:
    """The secret back, or ``EvidenceError("auth")`` when this machine cannot read it
    (a blob copied under another endpoint's entry, another Windows user's blob, a
    truncated settings file). Never a silently empty password."""
    if not stored:
        return ""
    scheme, _sep, payload = str(stored).partition(":")
    if scheme == DPAPI and not dpapi_available():
        # A settings file carried between machines, or a roaming profile: the blob is
        # real but only Windows can open it. Without this the ``ctypes.windll`` lookup
        # raises ``AttributeError`` straight out of the module, which is neither an
        # ``OSError`` nor the typed error the docstring promises.
        raise EvidenceError(
            "auth",
            f"the stored credential for {identity} was encrypted with Windows DPAPI "
            "and cannot be read on this system — enter it again in Settings",
        )
    try:
        raw = base64.b64decode(payload, validate=True)
        if scheme == PLAINTEXT:
            return raw.decode("utf-8")
        if scheme == DPAPI:
            return _crypt("CryptUnprotectData", raw, identity.encode("utf-8")).decode("utf-8")
    except (OSError, ValueError, binascii.Error, AttributeError) as exc:
        raise EvidenceError(
            "auth",
            f"the stored credential for {identity} cannot be read on this machine "
            f"({exc}) — enter it again in Settings",
        ) from exc
    raise EvidenceError(
        "auth",
        f"the stored credential for {identity} is in an unknown format {scheme!r} — "
        "enter it again in Settings",
    )


# ---------------------------------------------------------------------------
# The machine-scope store
# ---------------------------------------------------------------------------


def _entries(data: dict[str, Any] | None = None) -> dict[str, Any]:
    found = (data if data is not None else app_settings.load_app_settings()).get(SECTION)
    return found if isinstance(found, dict) else {}


def _entry(identity: str) -> dict[str, Any]:
    found = _entries().get(identity)
    return found if isinstance(found, dict) else {}


def endpoints() -> list[str]:
    """Every endpoint this machine holds a credential for, sorted."""
    return sorted(_entries())


def user_for(identity: str) -> str:
    return str(_entry(identity).get("user") or "")


def has_credential(identity: str) -> bool:
    return bool(_entry(identity))


def has_secret(identity: str) -> bool:
    """A password is stored (as opposed to a user with an empty one)."""
    return bool(_entry(identity).get("secret"))


def key_file_for(identity: str) -> str:
    """The private key file this PC signs in to the server *identity* with, ``""`` when it
    signs in with a password (an SFTP server, slice 6b: chosen per PC, never in a project)."""
    return decode_secret(str(_entry(identity).get(KEY_FILE) or ""), identity)


def lookup(identity: str) -> SignIn:
    """The ``Credentials`` callable ``open_store`` forwards to a backend:
    ``(user, password)`` for this endpoint, ``("", "")`` when none is stored — with, for an SSH
    server, the key file it signs in with and the host key this PC confirmed for it
    (:class:`evidence_store.SignIn`)."""
    entry = _entry(identity)
    return SignIn(
        str(entry.get("user") or ""),
        decode_secret(str(entry.get("secret") or ""), identity),
        key_file=key_file_for(identity),
        host_key=evidence_known_hosts.pinned(identity),
    )


def save(identity: str, user: str, secret: str | None, *, key_file: str | None = None) -> None:
    """Store (or replace) this endpoint's credential. The secret is encoded here and
    never logged.

    ``secret=None`` keeps whatever is stored (the editor never reads a password back
    into its field, so an untouched field must not wipe one); ``""`` stores "no
    password" — the store then signs in with an empty one and the server's refusal
    surfaces as ``auth``, which is the honest outcome of an empty field.

    ``key_file`` (an SSH server): the private key file to sign in with, encrypted like the
    secret, which is then the key's passphrase; ``""`` signs in with the password again and
    ``None`` keeps what is stored.
    """
    if not identity:
        raise EvidenceError("invalid", "a credential needs the endpoint it belongs to")
    blob = None if secret is None else encode_secret(secret, identity)
    key_blob = None if key_file is None else encode_secret(key_file.strip(), identity)
    wanted = user.strip()

    def mutate(data: dict[str, Any]) -> Any:
        entries = dict(_entries(data))
        held = entries.get(identity) if isinstance(entries.get(identity), dict) else {}
        entry = {
            "user": wanted,
            "secret": str(held.get("secret") or "") if blob is None else blob,
        }
        kept_key = str(held.get(KEY_FILE) or "") if key_blob is None else key_blob
        if kept_key:
            entry[KEY_FILE] = kept_key
        entries[identity] = entry
        data[SECTION] = entries

    # Merge-safe (another window's settings are re-read first) and RAISING: a
    # credential that reports success having stored nothing is the one outcome this
    # refuses — the user would then meet an ``auth`` failure with no idea why.
    try:
        app_settings.update_app_settings(mutate)
    except OSError as exc:
        raise EvidenceError(
            "denied", f"the credential for {identity} could not be stored: {exc}"
        ) from exc
    stored = _entry(identity)  # read back: the write is only real if it is there
    if (
        stored.get("user") != wanted
        or (blob is not None and stored.get("secret") != blob)
        or (key_blob is not None and str(stored.get(KEY_FILE) or "") != key_blob)
    ):
        raise EvidenceError(
            "denied",
            f"the credential for {identity} was not stored — check that "
            f"{app_settings.get_app_settings_file()} is writable",
        )
    log.info("Evidence credential saved for %s (user %r)", identity, wanted)


def remove(identity: str) -> bool:
    """Forget this endpoint's credential. True when one was there."""

    def mutate(data: dict[str, Any]) -> Any:
        entries = dict(_entries(data))
        if entries.pop(identity, None) is None:
            return False
        data[SECTION] = entries

    if not app_settings.update_app_settings(mutate):  # merge-safe, like save
        return False
    log.info("Evidence credential removed for %s", identity)
    return True


def describe(key: str, where: str, secret: str = "password") -> str:
    """One line for the GUI: which sign-in the server at *key* will use, and how it is
    held — the server named *where* (its address, never the key), its secret called
    *secret* (``token`` for the web server). Never the secret, and never its length."""
    if not key:
        return "No server yet."
    entry = _entry(key)
    if not entry:
        return f"No sign-in saved for {where}."
    user = str(entry.get("user") or "")
    who = f"Saved for {user} at {where}" if user else f"Saved for {where}"
    how = "encrypted with Windows DPAPI" if dpapi_available() else PLAINTEXT_WARNING
    if entry.get(KEY_FILE):  # named by its file name, never its path's folders or contents
        try:
            name = PureWindowsPath(key_file_for(key)).name
        except EvidenceError:
            name = "(cannot be read on this PC)"
        held = f"passphrase {how}" if entry.get("secret") else "no passphrase"
        return f"{who} — key file {name}, {held}."
    if not entry.get("secret"):
        return f"{who} — no {secret}."
    return f"{who} — {secret} {how}."
