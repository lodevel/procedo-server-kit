"""SFTP transport: the endpoint, the pinned host key, the connections and the error translation.

Carved out of :mod:`evidence_sftp` the way :mod:`evidence_ftps_conn` is carved out of the FTPS
tree: the tree speaks the :class:`evidence_tree.TreeIO` contract, and the sockets, the SSH
handshake and paramiko's exceptions live here (``docs/plans/evidence_db_design.md``, "SFTP: a
full storage type"). Nothing here knows the evidence layout.

* **paramiko is optional** (the ``sftp`` extra). It is imported on the first connection, never
  at module load, so the app imports and runs without it; choosing SFTP without it is refused
  with :data:`MISSING_PARAMIKO`, and :func:`paramiko_available` is what a registration asks.
* **The host key is pinned, never trusted on first use.** The pins live in this machine's
  settings (:mod:`evidence_known_hosts`), keyed by the server (:attr:`SftpEndpoint.
  credential_key`), and reach a connection with the credential itself
  (:class:`evidence_store.SignIn`, beside the key file), so no backend reads a settings module.
  A server with no pin is refused as :class:`HostKeyUnknown` (an ``auth`` error carrying the
  offered fingerprint, which only Test sign-in in Settings ▸ Server shows for the user to
  confirm); a server whose key differs from its pin is refused as :class:`HostKeyChanged`
  (``denied``, naming both fingerprints). The check runs after the key exchange and BEFORE any
  credential is sent.
* **Every refusal is a sentence saying what to do** (the slice 6b mock-up's frame 7): nothing
  on the port, no answer, the password or the key refused, the key file's passphrase, a PuTTY
  key, a key file not on this PC.
* **One SSH transport per endpoint, one SFTP channel per operation** (:class:`ChannelPool`):
  an SFTP session cannot interleave requests from two operations safely, so each operation
  leases a channel exclusively, as FTPS leases a connection. The transport is shared (paramiko's
  is thread-safe), so an operation pays for a channel, not for a login. Idle channels, and a
  transport nobody used for :data:`IDLE_CLOSE_S`, are closed rather than reused.
* **Every wait is bounded** by the endpoint's ``timeout_s``: the TCP connect, the banner, the
  key exchange, the authentication, the channel's subsystem request (paramiko waits on it with
  no timeout, so a watchdog closes the transport) and every SFTP request (the channel's own
  timeout). No caller runs on the GUI thread (``evidence_service``); this is what keeps a dead
  server from holding a worker forever.
* **Every failure leaves as an ``OSError``** (:func:`translate`): paramiko's
  ``AuthenticationException`` is :class:`evidence_store.AuthRequired`, its ``SSHException`` and a
  bare ``EOFError`` a ``ConnectionError``, a socket timeout a ``TimeoutError``. A status reply
  keeps its meaning: ``ENOENT`` is :class:`evidence_store.Absent` (the tree proves it against the
  parent's listing), ``EACCES`` and a generic failure a ``PermissionError``.

Qt-free. Imports no settings module: the credential, the key file and the pin are injected.
"""

from __future__ import annotations

import base64
import errno
import hashlib
import importlib.util
import logging
import socket
import struct
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from .evidence_store import Absent, AuthRequired, EvidenceError

log = logging.getLogger(__name__)

IDLE_CLOSE_S = 60.0  # an unused channel, or a transport nobody leased, is closed after this
MAX_IDLE = 4  # parked channels kept; the rest are closed rather than hoarded
POSIX_RENAME = "posix-rename@openssh.com"  # the atomic replace, where the server offers it

MISSING_PARAMIKO = (
    "SFTP needs paramiko, which is not installed with this Procedo: install the 'sftp' extra "
    '(pip install "procedo-bench[sftp]") or choose another storage type.'
)

Credentials = Callable[[str], tuple[str, str]]  # credential key -> (user, password or passphrase)
#: credential key -> the pinned host key (:func:`fingerprint`), ``""`` when none is pinned.
HostKeys = Callable[[str], str]
#: credential key -> the private key file to sign in with, ``""`` to sign in with the password.
KeyFiles = Callable[[str], str]


# ---------------------------------------------------------------------------
# paramiko, optional
# ---------------------------------------------------------------------------


def paramiko_available() -> bool:
    """Whether the ``sftp`` extra is installed, without importing it (nothing registers
    without it, and asking must not cost the import)."""
    return importlib.util.find_spec("paramiko") is not None


def load_paramiko() -> Any:
    """paramiko, imported on first use; its absence is refused in a sentence the user can act
    on, never an ``ImportError``."""
    try:
        import paramiko
    except ImportError as exc:
        raise EvidenceError("invalid", MISSING_PARAMIKO) from exc
    return paramiko


def wire_errors() -> tuple[type[BaseException], ...]:
    """What a request on the wire can raise: socket errors and status replies (``OSError``),
    a closed stream (``EOFError``) and paramiko's own protocol failures."""
    return (OSError, EOFError, load_paramiko().SSHException)


# ---------------------------------------------------------------------------
# The endpoint and its host key
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SftpEndpoint:
    """Where the store is, with no credential, key file or host key in it."""

    host: str
    port: int = 22
    base_path: str = "/"
    user: str = ""
    timeout_s: float = 30.0

    @property
    def root(self) -> PurePosixPath:
        """The base path as the absolute server path every request names."""
        inner = self.base_path.strip("/")
        return PurePosixPath("/" + inner) if inner else PurePosixPath("/")

    @property
    def identity(self) -> str:
        """Host, port and base path, never a secret: what routes outbox entries and keys the
        materialization cache (``sftp://host:22/evidence``)."""
        return f"{self.credential_key}{self.root.as_posix()}"

    @property
    def credential_key(self) -> str:
        """The key this SERVER's sign-in, key file and host key are filed under: host and port,
        never the folder. An IPv6 literal is bracketed, so the port reads back off it."""
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"sftp://{host}:{self.port}"


def fingerprint(key: Any) -> str:
    """A host key as it is pinned and shown: its type and OpenSSH's SHA256 fingerprint
    (``ecdsa-sha2-nistp256 SHA256:…``), the form ``ssh-keygen -lf`` prints."""
    digest = base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode("ascii")
    return f"{key.get_name()} SHA256:{digest.rstrip('=')}"


class HostKeyUnknown(AuthRequired):
    """The server's host key is pinned nowhere on this machine: nothing is trusted on first
    use, so the user must confirm :attr:`offered` with Test sign-in in Settings ▸ Server
    first (``auth``)."""

    host_key_state = "unknown"

    def __init__(self, server: str, offered: str) -> None:
        super().__init__(
            "The server's key is not confirmed on this PC yet. Click Test sign-in in "
            "Settings ▸ Server to see it and confirm it."
        )
        self.server = server
        self.offered = offered


class HostKeyChanged(PermissionError):
    """The server offered another key than the one pinned for it (``denied``): never accepted
    silently, since that is exactly what a server in the middle looks like."""

    host_key_state = "changed"

    def __init__(self, server: str, pinned: str, offered: str) -> None:
        super().__init__(
            "The server's key has changed. Nothing was sent.\n"
            f"Confirmed on this PC: {pinned}\n"
            f"Shown now: {offered}\n"
            "Another machine may be answering in the server's place. If the administrator "
            "replaced the key, check the new fingerprint with them, then Forget host key… and "
            "Test sign-in again."
        )
        self.server = server
        self.pinned = pinned
        self.offered = offered


def check_host_key(endpoint: SftpEndpoint, key: Any, pinned: str) -> None:
    """Refuse a server whose key is not the one pinned for it (see the two exceptions)."""
    offered = fingerprint(key)
    pinned = pinned.strip()
    if not pinned:
        raise HostKeyUnknown(endpoint.credential_key, offered)
    if pinned != offered:
        raise HostKeyChanged(endpoint.credential_key, pinned, offered)


# ---------------------------------------------------------------------------
# Error translation
# ---------------------------------------------------------------------------


#: What paramiko raises a status reply as: ``IOError(errno, text)``, which Python turns into
#: these by its errno (``ENOENT``, ``EACCES``, none). A socket failure is always another class
#: (``TimeoutError``, ``ConnectionResetError``, ``BrokenPipeError``…).
_STATUS_TYPES = (OSError, FileNotFoundError, PermissionError)


def status_reply(exc: BaseException) -> bool:
    """A failure the SERVER reported in a status reply, which leaves the channel usable."""
    return type(exc) in _STATUS_TYPES


def translate(exc: BaseException, what: str = "") -> OSError:
    """An SFTP failure as the ``OSError`` the :class:`evidence_tree.TreeIO` contract requires,
    so :func:`evidence_store_fs.os_error` classifies it as ``auth`` / ``not_found`` / ``denied``
    / ``unreachable`` / ``timeout`` instead of ``internal``. Already typed failures (a host key
    refusal, :class:`Absent`, :class:`AuthRequired`) pass through."""
    where = f" ({what})" if what else ""
    if isinstance(exc, (Absent, AuthRequired, HostKeyChanged)) or getattr(exc, "said", False):
        return exc
    if isinstance(exc, TimeoutError):  # socket.timeout IS TimeoutError since 3.10
        return TimeoutError(f"the server did not answer in time{where}")
    if isinstance(exc, EOFError):
        return ConnectionError(f"the server closed the connection{where}")
    if isinstance(exc, socket.gaierror):
        return ConnectionError(f"the server's name could not be resolved: {exc}{where}")
    if status_reply(exc):
        code = getattr(exc, "errno", None)
        if code == errno.ENOENT:
            return Absent(f"{exc.strerror or exc}{where}")  # type: ignore[attr-defined]
        return PermissionError(f"{getattr(exc, 'strerror', None) or exc}{where}")
    if isinstance(exc, OSError):  # refused / reset / no route: already typed by the socket
        return exc
    paramiko = _loaded()
    if paramiko is not None:
        if isinstance(exc, paramiko.AuthenticationException):
            return AuthRequired(f"the server refused the sign-in: {exc}{where}")
        if isinstance(exc, paramiko.SSHException):
            return ConnectionError(f"SSH failed: {exc}{where}")
    return OSError(f"{exc}{where}")


def _loaded() -> Any:
    """paramiko if it was already imported (an exception of it can only come from there)."""
    import sys

    return sys.modules.get("paramiko")


# ---------------------------------------------------------------------------
# One transport: connect, check the host key, sign in
# ---------------------------------------------------------------------------


#: The steps of a connection, as a sign-in test names them while it runs (``step(n)``).
STEPS = ("connecting", "checking the server's key", "signing in", "listing the folder")
Step = Callable[[int], None]  # called with the index in STEPS of the step starting


def _where(endpoint: SftpEndpoint) -> str:
    return f"{endpoint.host}:{endpoint.port}"


def _open_socket(endpoint: SftpEndpoint) -> socket.socket:
    """The TCP connection, its failures in a sentence saying what to check."""
    timeout = endpoint.timeout_s
    try:
        return socket.create_connection((endpoint.host, endpoint.port), timeout=timeout)
    except TimeoutError as exc:
        failure = TimeoutError(
            f"Cannot reach {_where(endpoint)}: no answer in {timeout:g} s. Check the host and "
            "the port, and that this PC's network reaches the server."
        )
        failure.said = True  # type: ignore[attr-defined]  (a sentence: never re-worded)
        raise failure from exc
    except ConnectionRefusedError as exc:
        raise ConnectionRefusedError(
            f"{endpoint.host} answers, but nothing listens on port {endpoint.port}. Check the port."
        ) from exc
    except socket.gaierror as exc:
        raise ConnectionError(
            f"Cannot find {endpoint.host}: this PC does not know that name. Check the host."
        ) from exc


def _handshake(endpoint: SftpEndpoint) -> Any:
    """A transport with its key exchange done and NOTHING sent about who we are."""
    paramiko = load_paramiko()
    timeout = endpoint.timeout_s
    transport = paramiko.Transport(_open_socket(endpoint))
    transport.banner_timeout = timeout
    transport.handshake_timeout = timeout
    transport.auth_timeout = timeout
    try:
        transport.start_client(timeout=timeout)
    except BaseException:
        transport.close()
        raise
    return transport


def offered_host_key(endpoint: SftpEndpoint) -> str:
    """The server's host key (:func:`fingerprint`), read after the key exchange and before any
    sign-in."""
    try:
        transport = _handshake(endpoint)
    except EvidenceError:
        raise
    except Exception as exc:
        failure = translate(exc, endpoint.credential_key)
        if failure is exc:
            raise
        raise failure from exc
    try:
        return fingerprint(transport.get_remote_server_key())
    finally:
        transport.close()


PUTTY_KEY = (
    "This key file is a PuTTY key (.ppk), which Procedo cannot read. Open it in PuTTYgen, use "
    "Conversions ▸ Export OpenSSH key, then pick that file."
)


def _private_key(path: str, passphrase: str) -> Any:
    """The key file, opened with its passphrase; one that cannot be used is ``auth``, in a
    sentence saying what to do (read on the worker that connects, never on the GUI thread)."""
    paramiko = load_paramiko()
    try:
        with open(path, "rb") as handle:
            head = handle.read(64)
    except FileNotFoundError as exc:
        raise AuthRequired(
            f"The key file {path} is not on this PC. Pick it again with Browse…."
        ) from exc
    except OSError as exc:
        raise AuthRequired(f"The key file {path} cannot be read: {exc}") from exc
    if head.startswith(b"PuTTY-User-Key-File"):
        raise AuthRequired(PUTTY_KEY)
    try:
        return paramiko.PKey.from_path(path, passphrase.encode("utf-8") if passphrase else None)
    except (paramiko.PasswordRequiredException, TypeError) as exc:  # encrypted, none given
        raise AuthRequired(
            "The key file needs its passphrase. Type it, then Save sign-in."
        ) from exc
    except ValueError as exc:
        if passphrase:
            raise AuthRequired(
                "The key file's passphrase is wrong. Type it again, then Save sign-in."
            ) from exc
        raise AuthRequired(f"The key file {path} cannot be used: {exc}") from exc
    except (paramiko.SSHException, OSError) as exc:
        raise AuthRequired(f"The key file {path} cannot be used: {exc}") from exc


class SignInRefused(AuthRequired):
    """The server refused this user's password or key (``auth``), in a sentence saying what
    to do: retype the password, or have the public key added for the user."""

    def __init__(self, endpoint: SftpEndpoint, user: str, key_file: str) -> None:
        if key_file:
            public = f"{PureWindowsPath(key_file).name}.pub"
            text = (
                f"The server refused this key for {user}. Ask the server's administrator to "
                f"add the public key ({public}) for {user}."
            )
        else:
            text = (
                f"User or password refused for {user} on {_where(endpoint)}. Type the "
                "password again, Save sign-in, then Test sign-in."
            )
        super().__init__(text)


def connect(
    endpoint: SftpEndpoint,
    credentials: Credentials | None,
    host_keys: HostKeys | None = None,
    key_files: KeyFiles | None = None,
    step: Step | None = None,
) -> Any:
    """One signed-in transport: the key exchange, the pinned host key, then the password or the
    private key file (the stored secret is then its passphrase).

    The key file and the pin come with the credential (:class:`evidence_store.SignIn`, what
    this PC keeps for the server); *key_files* and *host_keys* replace them when given."""
    say = step or (lambda _index: None)
    say(0)
    transport = _handshake(endpoint)
    try:
        server = endpoint.credential_key
        found = credentials(server) if credentials else ("", "")
        user, secret = found
        user = user or endpoint.user
        key_file = key_files(server) if key_files is not None else getattr(found, "key_file", "")
        pinned = host_keys(server) if host_keys is not None else getattr(found, "host_key", "")
        say(1)
        check_host_key(endpoint, transport.get_remote_server_key(), pinned or "")
        say(2)
        paramiko = load_paramiko()
        try:
            if key_file:
                transport.auth_publickey(user, _private_key(key_file, secret))
            else:
                transport.auth_password(user, secret)
        except paramiko.AuthenticationException as exc:
            raise SignInRefused(endpoint, user, key_file) from exc
        if not transport.is_authenticated():
            raise SignInRefused(endpoint, user, key_file)
        say(3)  # signed in: what follows is the folder
    except BaseException:
        transport.close()
        raise
    return transport


def _extensions(data: bytes) -> dict[str, bytes]:
    """The extension pairs of an SFTP ``VERSION`` packet's body (after the version number)."""
    found: dict[str, bytes] = {}
    pos = 0
    try:
        while pos < len(data):
            values = []
            for _ in range(2):
                (size,) = struct.unpack(">I", data[pos : pos + 4])
                values.append(data[pos + 4 : pos + 4 + size])
                pos += 4 + size
            found[values[0].decode("utf-8", "replace")] = values[1]
    except struct.error:
        pass  # a truncated list offers what was read
    return found


_CLIENT: Any = None


def _client_class() -> Any:
    """paramiko's ``SFTPClient`` that keeps the extensions the server advertises: paramiko reads
    the ``VERSION`` packet and drops them, and ``posix-rename`` must be used only where it is
    offered."""
    global _CLIENT
    if _CLIENT is None:
        paramiko = load_paramiko()
        from paramiko import sftp as wire

        class Client(paramiko.SFTPClient):
            extensions: dict[str, bytes] = {}

            def _send_version(self) -> int:
                msg = paramiko.Message()
                msg.add_int(wire._VERSION)
                self._send_packet(wire.CMD_INIT, msg)
                kind, data = self._read_packet()
                if kind != wire.CMD_VERSION:
                    raise paramiko.SFTPError("Incompatible sftp protocol")
                self.extensions = _extensions(data[4:])
                return int(struct.unpack(">I", data[:4])[0])

        _CLIENT = Client
    return _CLIENT


def open_channel(transport: Any, timeout_s: float) -> Any:
    """One SFTP session on *transport*, every request on it bounded by *timeout_s*. paramiko
    waits for the subsystem's answer with no timeout, so a watchdog closes the transport if the
    server never gives one (its channels then fail at once instead of hanging)."""
    channel = transport.open_session(timeout=timeout_s)
    channel.settimeout(timeout_s)
    try:
        watchdog = threading.Timer(timeout_s, transport.close)
        watchdog.daemon = True
        watchdog.start()
        try:
            channel.invoke_subsystem("sftp")
        finally:
            watchdog.cancel()
        return _client_class()(channel)  # the version exchange: bounded by the channel's timeout
    except BaseException:
        channel.close()
        raise


# ---------------------------------------------------------------------------
# The pool: one transport, channels leased per operation
# ---------------------------------------------------------------------------


class ChannelPool:
    """The endpoint's transport and its idle SFTP channels; each operation leases one channel
    exclusively, so no two threads ever send on the same one."""

    def __init__(
        self,
        endpoint: SftpEndpoint,
        credentials: Credentials | None = None,
        *,
        host_keys: HostKeys | None = None,
        key_files: KeyFiles | None = None,
        idle_s: float = IDLE_CLOSE_S,
    ) -> None:
        self.endpoint = endpoint
        self.credentials = credentials
        self.host_keys = host_keys
        self.key_files = key_files
        self.idle_s = idle_s
        #: Told each connection step as it starts (:data:`STEPS`): a sign-in test names them.
        self.step: Step | None = None
        self.connects = 0  # transports signed in (a reuse pin reads it)
        self.channels = 0  # channels opened
        self._transport: Any = None
        self._last_use = 0.0
        self._idle: list[tuple[float, Any]] = []
        self._leased: dict[Any, tuple[int, Any]] = {}  # channel -> (generation, its transport)
        self._generation = 0  # closing retires the leases of earlier generations
        self._lock = threading.Lock()
        self._connecting = threading.Lock()  # one sign-in at a time; the others reuse it

    def acquire(self) -> Any:
        """A parked channel PROVED alive, else a new channel on the (re)connected transport."""
        with self._lock:
            generation = self._generation
        cutoff = time.monotonic() - self.idle_s
        while True:
            with self._lock:
                if not self._idle:
                    break
                parked, client = self._idle.pop()
            if parked >= cutoff and _alive(client):
                return self._lease(client, generation)
            _shut(client)
        transport = self._live_transport()
        try:
            client = open_channel(transport, self.endpoint.timeout_s)
        except EvidenceError:
            raise
        except Exception as exc:
            if not transport.is_active():
                self._drop(transport)
            failure = translate(exc, self.endpoint.identity)
            if failure is exc:
                raise
            raise failure from exc
        with self._lock:
            self.channels += 1
        return self._lease(client, generation)

    def _lease(self, client: Any, generation: int) -> Any:
        with self._lock:
            self._leased[client] = (generation, client.get_channel().get_transport())
            self._last_use = time.monotonic()
        return client

    def _live_transport(self) -> Any:
        """The signed-in transport, reconnected when it died or sat unused past the idle time."""
        with self._connecting:
            with self._lock:
                transport = self._transport
                stale = time.monotonic() - self._last_use > self.idle_s and not any(
                    t is transport for _g, t in self._leased.values()
                )
            if transport is not None and transport.is_active() and not stale:
                return transport
            if transport is not None:
                self._drop(transport)
            with self._lock:
                generation = self._generation
            try:
                transport = connect(
                    self.endpoint, self.credentials, self.host_keys, self.key_files, self.step
                )
            except EvidenceError:
                raise
            except Exception as exc:
                failure = translate(exc, self.endpoint.identity)
                if failure is exc:
                    raise
                raise failure from exc
            with self._lock:
                closed = generation != self._generation
                if not closed:
                    self._transport = transport
                    self.connects += 1
            if closed:  # the pool was closed while this signed in (a cancelled test)
                _close_transport(transport)
                raise EvidenceError("cancelled", "the connection was closed while it opened")
            return transport

    def release(self, client: Any, *, broken: bool = False) -> None:
        """Park a healthy channel, unless the pool was closed while it was leased; close a
        retired transport once its last channel is back."""
        with self._lock:
            generation, transport = self._leased.pop(client, (None, None))
            self._last_use = time.monotonic()
            healthy = not broken and _open(client)
            if healthy and generation == self._generation and len(self._idle) < MAX_IDLE:
                self._idle.append((time.monotonic(), client))
                return
            orphan = transport is not None and transport is not self._transport
            orphan = orphan and not any(t is transport for _g, t in self._leased.values())
        _shut(client)
        if orphan:
            _close_transport(transport)
        elif transport is not None and not transport.is_active():
            self._drop(transport)

    def _drop(self, transport: Any) -> None:
        with self._lock:
            if self._transport is transport:
                self._transport = None
        _close_transport(transport)

    def close(self) -> None:
        """Close the idle channels and the transport (now, or when its last lease returns). A
        lease started before this call never repopulates the pool."""
        with self._lock:
            self._generation += 1
            idle, self._idle = self._idle, []
            transport, self._transport = self._transport, None
            busy = transport is not None and any(t is transport for _g, t in self._leased.values())
        for _parked, client in idle:
            _shut(client)
        if transport is not None and not busy:
            _close_transport(transport)


def _open(client: Any) -> bool:
    channel = client.get_channel()
    return not channel.closed and channel.get_transport().is_active()


def _alive(client: Any) -> bool:
    """One round trip: the channel is still there and the server still answers on it."""
    if not _open(client):
        return False
    try:
        client.normalize(".")
    except Exception:  # a dead channel answers with anything from OSError to SSHException
        return False
    return True


def _shut(client: Any) -> None:
    with suppress(Exception):
        client.close()


def _close_transport(transport: Any) -> None:
    with suppress(Exception):
        transport.close()
