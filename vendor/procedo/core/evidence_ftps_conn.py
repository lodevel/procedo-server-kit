"""FTPS transport: the endpoint, its connections, and the error translation.

Carved out of :mod:`evidence_ftps` so the tree that speaks the
:class:`evidence_tree.TreeIO` contract is not also the place where sockets, TLS and
``ftplib`` reply codes live. Nothing here knows the evidence layout.

* **Connections are request-owned.** ``FTP_TLS`` cannot interleave commands, so each
  operation leases an exclusive connection from :class:`ConnectionPool` for its
  duration; a leased connection is out of the pool, so no two threads can hold the
  same one, and idle leases close after a timeout.
* **The data channel resumes the control connection's TLS session**
  (:class:`_SessionReusingFTP_TLS`). CPython's ``ftplib`` does not, and a server
  configured to require it (vsftpd's ``require_ssl_reuse``) refuses the data
  connection outright.
* **Security is not configurable**: a verified ``ssl.create_default_context()`` with
  ``check_hostname=True``, an optional CA bundle, and ``PROT P`` after login. Plain
  FTP needs the explicit ``insecure`` flag, which sends the password and all evidence
  in clear.
* **Every failure leaves as an ``OSError``** (:func:`translate`), because the protocol
  classifies ``OSError`` and anything else surfaces as ``internal``. ``ftplib`` raises
  a bare ``EOFError`` whenever the peer closes the control connection — not an
  ``OSError`` at all — so every primitive catches it alongside them.

Stdlib only (``ftplib``/``ssl``), so this module imports with no test dependency
present. Qt-free.
"""

from __future__ import annotations

import ftplib
import logging
import re
import ssl
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from .evidence_store import Absent, AuthRequired, EvidenceError

log = logging.getLogger(__name__)

IDLE_CLOSE_S = 60.0  # an unused leased connection is closed rather than kept open
MAX_IDLE = 4  # parked connections kept; the rest are closed rather than hoarded

_CODE = re.compile(r"^\s*(\d{3})")
_AUTH_CODES = frozenset({530, 532})  # credentials rejected / account needed
_GONE_CODES = frozenset({421, 425, 426, 434})  # service closing, data connection lost
MISSING_CODES = frozenset({550, 553, 501})  # absent -- or refused: see _absent_or_denied
_UNSUPPORTED_CODES = frozenset({500, 502})  # command not understood / not implemented


# ---------------------------------------------------------------------------
# Error translation
# ---------------------------------------------------------------------------


def clean_reply(exc: BaseException) -> Any:
    """Mark a failure as one the server REPORTED, leaving the command stream usable.

    A refusal is not a broken connection: ``550 No such file`` is a complete reply, and
    a missing pointer or annotation file is routine. Without this mark every such
    not-found would retire a logged-in connection and pay for a fresh login — on a
    transport whose whole cost is round trips.
    """
    exc.ftps_clean_reply = True  # type: ignore[attr-defined]
    return exc


def stream_is_fine(exc: BaseException) -> bool:
    return bool(getattr(exc, "ftps_clean_reply", False))


def reply_code(exc: BaseException) -> int:
    """The three-digit reply an ``ftplib`` error carries, or ``0``."""
    found = _CODE.match(str(exc))
    return int(found.group(1)) if found else 0


def unsupported(exc: BaseException) -> bool:
    """The server does not know the command (an ``MLSD``-less server answers so)."""
    return reply_code(exc) in _UNSUPPORTED_CODES


def translate(exc: BaseException, what: str = "") -> OSError:
    """An FTPS failure as the ``OSError`` the :class:`evidence_tree.TreeIO` contract
    requires, so ``evidence_store_fs.os_error`` classifies it as ``auth`` /
    ``not_found`` / ``denied`` / ``unreachable`` / ``timeout`` instead of ``internal``.

    ``550`` means both "no such file" and "not enough privileges" and is translated to
    :class:`evidence_store.Absent` here; :meth:`evidence_ftps.FtpsTree._absent_or_denied`
    upgrades it to ``PermissionError`` when the entry turns out to exist, because the
    contract forbids reporting a refusal as an absence.
    """
    where = f" ({what})" if what else ""
    if isinstance(exc, TimeoutError):  # socket.timeout IS TimeoutError since 3.10
        return TimeoutError(f"{exc}{where}")
    if isinstance(exc, ssl.SSLCertVerificationError):
        # The host is reachable and answering: what failed is TRUST. Reporting it as
        # ``unreachable`` would show "host down", send the app offline and hide a wrong
        # hostname or an expired certificate behind a network story.
        return PermissionError(f"the server's certificate was rejected: {exc}{where}")
    if isinstance(exc, ssl.SSLError):
        return ConnectionError(f"TLS failed: {exc}{where}")
    if isinstance(exc, EOFError):
        return ConnectionError(f"the server closed the connection{where}")
    if isinstance(exc, ftplib.Error):
        code = reply_code(exc)
        if code in _AUTH_CODES:
            out: OSError = AuthRequired(f"{exc}{where}")
        elif code in _GONE_CODES:
            out = ConnectionError(f"{exc}{where}")
        elif code in MISSING_CODES:
            out = Absent(f"{exc}{where}")
        else:
            out = PermissionError(f"{exc}{where}")
        return clean_reply(out)
    if isinstance(exc, OSError):  # connection refused / reset / no route: already typed
        return exc
    return OSError(f"{exc}{where}")


# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FtpsEndpoint:
    """Where the store is, with no credential in it."""

    host: str
    port: int = 21
    base_path: str = "/"
    user: str = ""
    tls: bool = True
    passive: bool = True
    insecure: bool = False
    ca_bundle: str = ""
    timeout_s: float = 30.0

    @property
    def root(self) -> PurePosixPath:
        return (
            PurePosixPath("/" + self.base_path.strip("/"))
            if self.base_path.strip("/")
            else PurePosixPath("/")
        )

    @property
    def identity(self) -> str:
        """The stable, printable key that routes outbox entries and keys the
        materialization cache: host, port and base path, NEVER the password.

        The scheme names the backend, not the transport, so turning ``insecure`` on or
        off does not re-spell the endpoint and orphan every pending upload. An IPv6
        literal is bracketed, so the port stays readable back off the identity
        (``ftps://::1:21/e`` cannot be split on its colons).
        """
        return f"{self.credential_key}{self.root.as_posix()}"

    @property
    def credential_key(self) -> str:
        """The key this SERVER's sign-in is filed under: host and port, never the
        folder, so one sign-in serves every folder on the server (a project's and
        its evidence folder alike). The user rides inside the entry it names."""
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"ftps://{host}:{self.port}"


Credentials = Callable[[str], tuple[str, str]]  # credential key -> (user, password)


# ---------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------


class _SessionReusingFTP_TLS(ftplib.FTP_TLS):  # noqa: N801  (mirrors ftplib's own name)
    """``FTP_TLS`` whose data connections resume the control connection's TLS session.

    CPython wraps the data socket with no ``session=``, so every transfer performs a
    full handshake and a server that requires session reuse refuses the data channel.
    Each wrap records whether the session was actually reused, which is what pins the
    behaviour instead of assuming it.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.reused_sessions: list[bool] = []
        super().__init__(*args, **kwargs)

    def ntransfercmd(self, cmd: str, rest: Any = None) -> tuple[Any, Any]:
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            conn = self.context.wrap_socket(
                conn, server_hostname=self.host, session=self.sock.session
            )
            self.reused_sessions.append(bool(conn.session_reused))
        return conn, size


def _context(endpoint: FtpsEndpoint) -> ssl.SSLContext:
    """A verified context: hostname checked, certificate required, optional CA bundle."""
    ctx = ssl.create_default_context(cafile=endpoint.ca_bundle or None)
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


def connect(endpoint: FtpsEndpoint, credentials: Credentials | None) -> ftplib.FTP:
    """One logged-in connection with ``PROT P`` set (or plain FTP when ``insecure``)."""
    user, password = ("", "")
    if credentials is not None:
        user, password = credentials(endpoint.credential_key)
    user = user or endpoint.user
    if endpoint.insecure and endpoint.tls:
        raise EvidenceError(
            "invalid",
            "'insecure' (plain FTP) and TLS are both on, which cannot both be true: "
            "turn TLS off to accept sending the password and all evidence in clear, or "
            "clear 'insecure' to keep the connection encrypted",
        )
    if not endpoint.tls or endpoint.insecure:
        if not endpoint.insecure:
            raise EvidenceError(
                "invalid",
                "TLS is off but 'insecure' is not set: plain FTP sends the password and "
                "all evidence in clear, and must be asked for explicitly",
            )
        client: ftplib.FTP = ftplib.FTP(timeout=endpoint.timeout_s)
        client.connect(endpoint.host, endpoint.port, timeout=endpoint.timeout_s)
        client.login(user, password)
    else:
        tls = _SessionReusingFTP_TLS(context=_context(endpoint), timeout=endpoint.timeout_s)
        tls.connect(endpoint.host, endpoint.port, timeout=endpoint.timeout_s)
        tls.login(user, password)
        tls.prot_p()  # the DATA channel is encrypted too, not just the login
        client = tls
    client.set_pasv(endpoint.passive)
    return client


class ConnectionPool:
    """Idle connections to one endpoint; each operation leases one exclusively.

    A leased connection is out of the pool, so no two threads can ever hold the same
    one — the reason the pool exists at all, since ``FTP_TLS`` cannot interleave
    commands and a shared session lets one request consume another's replies.
    """

    def __init__(
        self,
        endpoint: FtpsEndpoint,
        credentials: Credentials | None = None,
        *,
        idle_s: float = IDLE_CLOSE_S,
    ) -> None:
        self.endpoint = endpoint
        self.credentials = credentials
        self.idle_s = idle_s
        self.connects = 0  # how many real logins happened (a reuse pin reads it)
        self._idle: list[tuple[float, ftplib.FTP]] = []
        self._generation = 0  # closing retires leases started in earlier generations
        self._leased: dict[ftplib.FTP, int] = {}
        self._lock = threading.Lock()

    def acquire(self) -> ftplib.FTP:
        """A parked connection, PROVED alive, else a fresh one.

        A server closes an idle control connection on its own schedule (~300 s is
        common, well past our own idle timeout), and handing such a corpse out made the
        next command raise ``AttributeError`` on a ``None`` socket — an ``internal``
        error, one per idle gap. ``NOOP`` costs one round trip and settles it.
        """
        with self._lock:
            generation = self._generation
        cutoff = time.monotonic() - self.idle_s
        while True:
            with self._lock:
                if not self._idle:
                    break
                parked, conn = self._idle.pop()
            if parked >= cutoff and _alive(conn):
                with self._lock:
                    self._leased[conn] = generation
                return conn
            _shut(conn)
        try:
            conn = connect(self.endpoint, self.credentials)
        except EvidenceError:
            raise
        except Exception as exc:
            raise translate(exc, self.endpoint.identity) from exc
        with self._lock:
            self.connects += 1
            self._leased[conn] = generation
        return conn

    def release(self, conn: ftplib.FTP, *, broken: bool = False) -> None:
        """Park a healthy lease, unless the pool was closed while it was in use."""
        with self._lock:
            generation = self._leased.pop(conn, None)
            if not broken and generation == self._generation and len(self._idle) < MAX_IDLE:
                self._idle.append((time.monotonic(), conn))
                return
        _shut(conn)

    def close(self) -> None:
        """Close parked connections and retire outstanding leases when they return.

        A later acquisition may reconnect, but a lease started before this call must
        never repopulate the idle pool after a dialog or project has detached.
        """
        with self._lock:
            self._generation += 1
            idle, self._idle = self._idle, []
        for _parked, conn in idle:
            _shut(conn)


def _alive(conn: ftplib.FTP) -> bool:
    """One ``NOOP``: the connection is still there and its command stream is in step."""
    try:
        conn.voidcmd("NOOP")
    except Exception:  # a closed socket answers with anything from OSError to AttributeError
        return False
    return True


def _shut(conn: ftplib.FTP) -> None:
    with suppress(Exception):
        conn.quit()
    with suppress(Exception):
        conn.close()
