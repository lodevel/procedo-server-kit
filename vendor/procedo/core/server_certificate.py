"""A web server's certificate: this PC's *Accept any certificate*, and a refusal in words.

What the owner approved on 2026-11-09 (``server_users_mockup.html`` §3):

* **Accept any certificate** is one more advanced row of a web server in Settings ▸ Server
  (:data:`PARAM`), off by default, greyed while TLS is off, its warning under it while on.
  With it on, TLS is still used, but no certificate and no name is checked.
* **It is this PC's setting for that server, and nothing else's.** It is read from this PC's
  saved servers alone (:func:`this_pc`), when a connection is made: never from a project's
  entry, an origin record, or any reconnect parameter, so nothing a project or a record holds
  can turn it on, and a Save in Settings reaches every open connection at its next call, both
  ways. A server entry counts it only while its own TLS is on and its value is exactly ``on``
  (:func:`entry_on`); two saved web servers on one address are refused by Settings, and would
  read as off. The one exception is Settings ▸ Server's own Check access and Test sign-in,
  which try the value typed there, before Save, for that call on that thread alone
  (:func:`trying`). The server's identity never includes it: its token is never filed again.
* **A refused certificate is said in plain words** (:class:`CertificateRejected`), as the
  database's certificate messages are (``evidence_db_check._certificate``): what happened, then
  what to do, the CA bundle first and the switch second, never the switch for an expired
  certificate. Python's text goes to the log once per change (``evidence_http``). Studio keeps
  no server settings and says where they are instead (:func:`in_settings_app`).

Qt-free. :func:`this_pc` looks at the settings file and :func:`certificate_names` makes a
handshake: both on a worker, as every call of the transport is. The window's thread reads the
copy kept in memory (:func:`kept`, :func:`kept_name`), never the file.
"""

from __future__ import annotations

import logging
import os
import socket
import ssl
import threading
import urllib.request
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlsplit

from .evidence_store import REGISTRY, ParamSpec, credential_key

log = logging.getLogger(__name__)
#: The declared param of the switch (the web server's ``PARAMS``).
ANY_CERTIFICATE = "any_certificate"
WARNING = (
    "The connection stays encrypted, but this PC no longer checks that it talks to your server: "
    "someone on the network could pose as it and read the token and the results. Use it to try "
    "a server with a self-signed certificate; for daily use give the server's CA bundle above "
    "instead."
)
PARAM = ParamSpec(
    ANY_CERTIFICATE,
    "Accept any certificate",
    "choice:off|on",
    False,
    "off",
    this_pc=True,
    needs="tls",
    warning=WARNING,
)
#: What a success says while the server's certificate is not checked (in amber).
UNCHECKED_NOTE = "its certificate is not checked (Accept any certificate is on)"

#: OpenSSL's verify codes, as what a person is told (the database's table).
_CERT_CODES = {
    10: "expired",  # certificate has expired
    18: "self_signed",  # self-signed certificate
    19: "self_signed",  # self-signed certificate in the chain
    20: "untrusted",  # unable to get the local issuer certificate
    21: "untrusted",  # unable to verify the first certificate
    62: "name",  # hostname mismatch
    64: "name",  # IP address mismatch
}
#: What to do about a certificate not trusted, or made out to another name, as Bench says it.
UNTRUSTED_FIX = (
    "Give the server's CA bundle under Advanced connection settings, or, only to try this "
    "server, turn on Accept any certificate there."
)
NAME_FIX = (
    "Type the name the certificate carries in Server address, or, only to try this server, "
    "turn on Accept any certificate under Advanced connection settings."
)
_STUDIO_FIX = {
    UNTRUSTED_FIX: "Its settings are in Procedo Bench: File › Settings… › Server › Advanced "
    "connection settings (CA bundle, or Accept any certificate).",
    NAME_FIX: "Its settings are in Procedo Bench: File › Settings… › Server (Server address, "
    "or Accept any certificate under Advanced connection settings).",
}
#: Seconds the second handshake that reads a certificate's names may take.
NAMES_TIMEOUT_S = 5.0


# ---------------------------------------------------------------------------
# This PC's choice
# ---------------------------------------------------------------------------


def is_on(value: Any) -> bool:
    """The one reading of the switch's value: exactly ``on`` (what the form writes); anything
    else, ``On``, ``true`` or ``True`` included, is off."""
    return value == "on"


def declares(backend: str) -> bool:
    """Whether *backend* has the switch (the web server)."""
    return any(p.key == ANY_CERTIFICATE for p in getattr(REGISTRY.get(backend), "PARAMS", ()))


def entry_on(spec: dict[str, Any]) -> bool:
    """Whether a server entry (Settings ▸ Server's) has the switch on and in force: a backend
    that declares it, the entry's own TLS on and plain HTTP off, the value exactly ``on``. A
    value left ``on`` while TLS is off (greyed) counts for nothing."""
    from .evidence_remote_cache import flag  # the reading of TLS every store uses

    params = spec.get("params") or {}
    if not declares(str(spec.get("backend", ""))):
        return False
    if not flag(params, "tls", True) or flag(params, "insecure", False):
        return False
    return is_on(params.get(ANY_CERTIFICATE))


class _Kept:
    """This PC's choices as last read: ``{identity: on}`` and the servers' names, with the
    settings file's stamp they were read at. ``lock`` guards these fields (in memory only:
    the window's thread takes it); ``reading`` is held by the one worker re-reading the file,
    from the stamp it saw to the stamp it stores, so two re-reads across a Save never leave
    the old choices under the new stamp. ``generation`` counts what this process saved."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.reading = threading.Lock()
        self.stamp: tuple[int, int] | None = None
        self.generation = 0
        self.choices: dict[str, bool] = {}
        self.names: dict[str, str] = {}


_KEPT = _Kept()
_TRIAL = threading.local()


def _stamp() -> tuple[int, int] | None:
    from . import app_settings

    try:
        found = os.stat(app_settings.get_app_settings_file())
    except OSError:
        return None
    return (found.st_mtime_ns, found.st_size)


def _choices_of(servers: Iterable[Any]) -> tuple[dict[str, bool], dict[str, str]]:
    choices: dict[str, bool] = {}
    names: dict[str, str] = {}
    for server in servers:
        key = credential_key(server.spec)
        if not key:
            continue
        names.setdefault(key, server.name)
        if declares(server.backend):  # two entries on one address: on only if both say so
            choices[key] = choices.get(key, True) and entry_on(server.spec)
    return choices, names


def remember(servers: Iterable[Any]) -> None:
    """Keep *servers* (``server_settings.Server``) as this PC's choices: called once they are
    saved, so the next connection, in this process, follows at once. In memory only (the
    window's thread calls it): the next worker reads the file again to take its stamp."""
    choices, names = _choices_of(servers)
    with _KEPT.lock:
        _KEPT.choices, _KEPT.names = choices, names
        _KEPT.stamp, _KEPT.generation = None, _KEPT.generation + 1


def this_pc(identity: str) -> bool:
    """Whether this PC accepts any certificate from the server *identity* names, as its saved
    servers say NOW (the settings file read again when it changed): what a connection asks
    before each call. On a worker. Settings' own check tries its typed value (:func:`trying`).

    One worker re-reads at a time (``reading``), and what it read is kept only when the file
    did not change while it read and nothing was saved here meanwhile; otherwise the next call
    reads again, so an old choice is never kept under a newer stamp."""
    trial = getattr(_TRIAL, "choices", {})
    if identity in trial:
        return trial[identity]
    if _stamp() != _KEPT.stamp:
        from .server_settings import servers  # this PC's servers: above this module

        with _KEPT.reading:
            before = _stamp()
            with _KEPT.lock:
                current, generation = before == _KEPT.stamp, _KEPT.generation
            if not current:
                choices, names = _choices_of(servers())
                unmoved = _stamp() == before  # (no file is touched under ``lock``)
                with _KEPT.lock:
                    if unmoved and _KEPT.generation == generation:
                        _KEPT.choices, _KEPT.names, _KEPT.stamp = choices, names, before
    return kept(identity)


def kept(identity: str) -> bool:
    """:func:`this_pc` from the copy in memory, never the file: what the window's thread asks."""
    return bool(identity) and _KEPT.choices.get(identity, False)


def kept_name(identity: str) -> str:
    """The name of this PC's server *identity* names, from the copy in memory (``""``)."""
    return _KEPT.names.get(identity, "")


@contextmanager
def trying(identity: str, choice: bool) -> Iterator[None]:
    """Within this block, on this thread alone, connections to *identity* take *choice*: the
    value typed in Settings ▸ Server, tried by its Check access or Test sign-in before Save."""
    trial = dict(getattr(_TRIAL, "choices", {}))
    _TRIAL.choices = {**trial, identity: choice}
    try:
        yield
    finally:
        _TRIAL.choices = trial


def tried(spec: dict[str, Any], fn: Callable[..., Any]) -> Callable[..., Any]:
    """*fn*, run with *spec*'s typed switch (Settings ▸ Server's entry, before Save) tried for
    its server (:func:`trying`); *fn* as it is for an entry that does not hold the switch (a
    project's: this PC's saved choice then applies) or a backend without it."""
    holds = ANY_CERTIFICATE in (spec.get("params") or {})
    if not holds or not declares(str(spec.get("backend", ""))):
        return fn
    key, choice = credential_key(spec), entry_on(spec)

    def run(*args: Any, **kwargs: Any) -> Any:
        with trying(key, choice):
            return fn(*args, **kwargs)

    return run


def unchecked(spec: dict[str, Any]) -> bool:
    """Whether a connection to *spec*'s server checks no certificate, as the window shows it
    (amber): the entry's own switch when it is Settings ▸ Server's (it has one), else this PC's
    choice for that server from the copy in memory. No file is read."""
    params = spec.get("params") or {}
    if ANY_CERTIFICATE in params:
        return entry_on(spec)
    from .evidence_remote_cache import flag

    if not declares(str(spec.get("backend", ""))) or not flag(params, "tls", True):
        return False
    return kept(credential_key(spec))


def without_choice(params: dict[str, Any]) -> dict[str, Any]:
    """*params* (a project's entry, an origin record's reconnect params) without the switch:
    nothing but this PC's servers ever holds it."""
    return {k: v for k, v in params.items() if k != ANY_CERTIFICATE}


def _direct(endpoint: Any, host: str) -> bool:
    """Whether a connection to *host* goes straight to it: no proxy in the settings, and none of
    the system's (``HTTPS_PROXY``, Windows' settings) for that host."""
    proxy = str(endpoint.proxy).strip().lower()
    if proxy:
        return proxy == "direct"
    system = urllib.request.getproxies()
    if not (system.get("https") or system.get("all")):
        return True
    return bool(urllib.request.proxy_bypass(host))


# ---------------------------------------------------------------------------
# A refusal in words
# ---------------------------------------------------------------------------


class CertificateRejected(PermissionError):
    """TLS trust failed. A ``PermissionError``, so it classifies as ``denied`` and
    never as ``unreachable``: the host answered, and hiding a wrong hostname or an
    expired certificate behind an offline banner is what that rule prevents.

    Its text is the plain sentence Settings ▸ Server shows; :attr:`short` is the footer's half
    sentence. *reason*: ``self_signed``, ``untrusted``, ``name``, ``expired`` or ``refused``
    (any other verify failure, *why* in OpenSSL's short words)."""

    def __init__(
        self, reason: str, *, host: str, names: list[str] | None = None, why: str = ""
    ) -> None:
        self.reason, self.host, self.names, self.why = reason, host, list(names or []), why
        super().__init__(self._sentence())

    def _sentence(self) -> str:
        if self.reason in ("self_signed", "untrusted"):
            return f"The server's certificate is not trusted by this PC ({self._which}). " + (
                UNTRUSTED_FIX
            )
        if self.reason == "name":
            return f"The server's certificate is for {self._names}, not {self.host}. {NAME_FIX}"
        if self.reason == "expired":
            return (
                "The server's certificate has expired. Ask the server's administrator to renew it."
            )
        return f"The server's certificate is refused ({self._why}). Ask the server's administrator."

    @property
    def _which(self) -> str:
        return "self-signed" if self.reason == "self_signed" else "unknown authority"

    @property
    def _names(self) -> str:
        return ", ".join(self.names) if self.names else "another name"

    @property
    def _why(self) -> str:
        return self.why or "not valid"

    @property
    def short(self) -> str:
        """``its certificate is not trusted (self-signed)``: what a footer says after the
        server's name."""
        if self.reason in ("self_signed", "untrusted"):
            return f"its certificate is not trusted ({self._which})"
        if self.reason == "name":
            return f"its certificate is for {self._names}, not {self.host}"
        if self.reason == "expired":
            return "its certificate has expired"
        return f"its certificate is refused ({self._why})"


def certificate_refusal(err: object) -> CertificateRejected | None:
    """The :class:`CertificateRejected` *err* is, or was raised from; ``None`` for any other
    failure."""
    seen: set[int] = set()
    found = err if isinstance(err, BaseException) else None
    while found is not None and id(found) not in seen:
        if isinstance(found, CertificateRejected):
            return found
        seen.add(id(found))
        found = found.__cause__ or found.__context__
    return None


def in_settings_app(text: str) -> str:
    """*text* as an app without server settings of its own (Studio) says it: a certificate's
    "what to do" names where the settings are, in Procedo Bench."""
    for bench, studio in _STUDIO_FIX.items():
        text = text.replace(bench, studio)
    return text


def rejected(
    error: ssl.SSLCertVerificationError,
    endpoint: Any = None,
    names: Callable[[str, int], list[str]] | None = None,
) -> CertificateRejected:
    """*error* as the sentence a person reads. *endpoint*: the web server's
    (:class:`evidence_http.ApiEndpoint`); *names*: how the names a certificate made out to
    another name carries are found (``None``: they are not, and it says "another name")."""
    reason = _CERT_CODES.get(getattr(error, "verify_code", 0) or 0, "refused")
    parts = urlsplit(endpoint.base) if endpoint is not None else None
    host = (parts.hostname or "") if parts is not None else ""
    found: list[str] = []
    if reason == "name" and parts is not None and host and names is not None:
        found = names(host, parts.port or 443)
    why = str(getattr(error, "verify_message", "") or "")
    return CertificateRejected(reason, host=host, names=found, why=why)


def verified_context(ca_bundle: str = "") -> ssl.SSLContext:
    """The server's chain checked against *ca_bundle* (or the system's store when empty, which
    on Windows includes the Windows certificate store) and its name against the host."""
    context = ssl.create_default_context(cafile=ca_bundle or None)
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return context


def unchecked_context() -> ssl.SSLContext:
    """TLS that checks no certificate and no name: Accept any certificate."""
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def certificate_names(endpoint: Any, host: str, port: int) -> list[str]:
    """The names the server's certificate is made out to (its DNS and IP subject alternative
    names, else its common name), read through a handshake that checks the chain but not the
    name; ``[]`` when they cannot be read, or when the server is not reached directly (a proxy
    in the settings or the system's: this handshake would go round it, and might read another
    machine's certificate)."""
    if not _direct(endpoint, host):
        return []
    try:
        context = verified_context(endpoint.ca_bundle)
        context.check_hostname = False
        timeout = min(endpoint.timeout_s, NAMES_TIMEOUT_S)
        with socket.create_connection((host, port), timeout=timeout) as raw:
            with context.wrap_socket(raw, server_hostname=host) as tls:
                cert = tls.getpeercert() or {}
    except (OSError, ValueError):
        return []
    names = [str(v) for k, v in cert.get("subjectAltName", ()) if k in ("DNS", "IP Address")]
    if names:
        return names
    for rdn in cert.get("subject", ()):
        names += [str(v) for k, v in rdn if k == "commonName"]
    return names


# ---------------------------------------------------------------------------
# A server's refusals, once per change; a connection's opener per choice
# ---------------------------------------------------------------------------

#: identity -> (the refusal last met, the names its certificate carries). Kept for the
#: process, not per connection: every sync pass builds a connection of its own, and the
#: same refusal met by each of them is logged, and its names read, once.
_REFUSED: dict[str, tuple[str, list[str]]] = {}
_REFUSED_LOCK = threading.Lock()


def refused(
    error: ssl.SSLCertVerificationError, identity: str, endpoint: Any, where: str, scope: Any
) -> CertificateRejected:
    """*error*, met calling the server *identity* names, in words (:func:`rejected`). A new
    refusal is logged with Python's text, and the names a certificate made out to another
    name carries are read (a second handshake: direct connections only, never once *scope*,
    the call's, is cancelled); the same one met again costs nothing more, until a call gets
    through (:func:`got_through`)."""
    seen = f"{getattr(error, 'verify_code', 0)}:{getattr(error, 'verify_message', '')}"
    with _REFUSED_LOCK:
        known = _REFUSED.get(identity)
    if known is None or known[0] != seen:
        log.warning("the server's certificate was rejected: %s%s", error, where)
        names: list[str] = []
        if scope is None or not scope.cancelled():
            names = rejected(error, endpoint, lambda h, p: certificate_names(endpoint, h, p)).names
        known = (seen, names)
        with _REFUSED_LOCK:
            _REFUSED[identity] = known
    found = list(known[1])
    return rejected(error, endpoint, lambda _host, _port: found)


def got_through(identity: str) -> None:
    """A call to *identity*'s server got an answer: a refusal met after this is new again."""
    if identity in _REFUSED:
        with _REFUSED_LOCK:
            _REFUSED.pop(identity, None)


class Openers:
    """Which opener a call to one server goes out with: the one for this PC's choice NOW
    (:func:`this_pc`), asked before each call, so a Save reaches the next call both ways.

    One opener, with its own pool of kept connections, per choice: a call that wants its
    certificate checked is never handed one built, or a connection opened, without the check,
    whatever another thread asks at the same moment. The choice and its opener are taken
    together under one lock, and the other choice's opener is dropped with its connections
    when the choice changes. *build* makes an opener from a TLS context and a pool (the
    transport's own, ``evidence_http``)."""

    def __init__(
        self, identity: str, tls: bool, ca_bundle: str, build: Callable[[ssl.SSLContext, Any], Any]
    ) -> None:
        self.identity, self.tls, self.ca_bundle, self._build = identity, tls, ca_bundle, build
        self._lock = threading.Lock()
        self._built: dict[bool, tuple[Any, Any]] = {}
        self._last = threading.local()
        self._pair(False)  # the checked one at once: a CA bundle that cannot be read says so now

    def _pair(self, unchecked: bool) -> tuple[Any, Any]:
        from .http_keepalive import Pool

        pair = self._built.get(unchecked)
        if pair is None:
            context = unchecked_context() if unchecked else verified_context(self.ca_bundle)
            pool = Pool()
            pair = self._built[unchecked] = (self._build(context, pool), pool)
        return pair

    def opener(self) -> Any:
        """The opener for this call, on its worker; this thread's :attr:`unchecked` says which."""
        want = self.tls and this_pc(self.identity)
        with self._lock:
            opener = self._pair(want)[0]
            dropped = self._built.pop(not want, None)
        if dropped is not None:
            dropped[1].close()  # no connection opened under the other choice is reused
        self._last.unchecked = want
        return opener

    @property
    def unchecked(self) -> bool:
        """Whether this thread's last call went out checking no certificate."""
        return bool(getattr(self._last, "unchecked", False))

    @property
    def pool(self) -> Any:
        """The kept connections of the checked opener (else the unchecked one's)."""
        with self._lock:
            return (self._built.get(False) or self._built[True])[1]

    def close(self) -> None:
        with self._lock:
            pools = [pool for _opener, pool in self._built.values()]
        for pool in pools:
            pool.close()
