"""The HTTPS transport to the web server: the endpoint, and one call on the wire.

What every call of the JSON API (:mod:`evidence_api_http`) rides on, for the evidence, the
projects and the library alike:

* **The signature is the proof.** Every answer must carry ``X-Bench-Store: v1``; without it a
  captive portal, a misrouted proxy or a web page answered, so the failure is ``unreachable``
  and never "the file is not there".
* **A status is an answer, not a failure** (``web_api_spec.md`` §5.1). :data:`STATUS_KIND`
  maps it to the app's kind, an echoed ``kind`` the app knows wins, and the kind picks the
  exception: the ``OSError`` shapes where one exists, and :class:`evidence_store.EvidenceError`
  for ``invalid`` / ``corrupt`` / ``internal``, which have none.
* **TLS is verified** unless this PC accepts any certificate from that server (which opener a
  call takes, and a refusal in words: :mod:`server_certificate`); plain HTTP is chosen
  explicitly (``insecure``), and the two flags contradicting each other is refused first.
* **Identity is one spelling of one server** (A.2, A.4 row 5): case, default port, trailing
  slashes and an IPv6 literal all collapse, the scheme is the backend name so toggling
  ``insecure`` never re-spells it, and the path prefix is part of it. It files the token and
  routes durable outbox entries, so it must never move.
* **No credential leaves in a URL, a log line or an error** (§6).

``urllib`` from the stdlib, so no new dependency, its connections kept open and reused
(:mod:`http_keepalive`). Qt-free.
"""

from __future__ import annotations

import json
import logging
import ssl
import threading
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from . import http_keepalive
from .evidence_store import Absent, AuthRequired, EvidenceError
from .evidence_tree import OperationScope, ScopeLocal, enter_scope
from .server_certificate import Openers, got_through, refused

log = logging.getLogger(__name__)

BLOCK = 1 << 16  # transfer block: the unit at which a transfer notices a cancel
API_PREFIX = "api/v1"
SIGNATURE = "x-bench-store"  # header name, lower-cased as the answers are read
SIGNATURE_VALUE = "v1"
_DEFAULT_PORTS = {"http": 80, "https": 443}

#: The kinds a server may ECHO. ``foreign`` and ``cancelled`` are deliberately not
#: here: the app concludes those itself (§5), and a server that could echo ``foreign``
#: would be able to shut a project's evidence doors from the outside.
ECHOED_KINDS = frozenset(
    {"invalid", "not_found", "auth", "denied", "unreachable", "timeout", "corrupt", "internal"}
)

#: §5.1, status → the app's kind. The reasoning that is not obvious: 409 is
#: ``invalid`` so one protected path quarantines one queue entry instead of stalling
#: every other bench's uploads behind it; 429 and 503 are ``unreachable`` so the app
#: backs off instead of hammering file by file; 507 is ``denied`` because the server
#: is up and an offline banner would send the operator to the network, not the disk.
STATUS_KIND: dict[int, str] = {
    400: "invalid",
    401: "auth",
    403: "denied",
    404: "not_found",
    405: "invalid",
    408: "timeout",
    409: "invalid",
    410: "not_found",
    411: "invalid",
    412: "invalid",
    413: "invalid",
    414: "invalid",
    416: "corrupt",
    422: "corrupt",
    429: "unreachable",
    500: "internal",
    501: "invalid",
    502: "unreachable",
    503: "unreachable",
    504: "timeout",
    507: "denied",
}


class NotOurServer(ConnectionError):
    """Something answered, and it does not speak this API — no ``X-Bench-Store``.

    A ``ConnectionError``, so every caller classifies it as ``unreachable`` exactly
    as a refused connection; its own type only so *Check access* can say "that
    address answers, but it is not a test-bench server" instead of "cannot reach it",
    which would send the user to the network over a proxy or a portal in the way.
    """


def _as_error(kind: str, message: str) -> BaseException:
    """The kind as the exception the contract requires: ``evidence_store_fs.os_error``
    reads these ``OSError`` shapes back into the same kinds, so a failure keeps its
    meaning all the way to the user, and the three kinds with no ``OSError`` shape are
    raised as the typed error directly, as the FTPS tree does for ``corrupt``."""
    if kind == "auth":
        return AuthRequired(message)
    if kind == "not_found":
        return Absent(message)
    if kind == "denied":
        return PermissionError(message)
    if kind == "unreachable":
        return ConnectionError(message)
    if kind == "timeout":
        return TimeoutError(message)
    return EvidenceError(kind, message)


# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------


def normalize_base(base_url: str) -> str:
    """One spelling of one server address: ``https://serve.com/testprocedure/``.

    Case, a default port, repeated and missing trailing slashes and an IPv6 literal
    all collapse here, so three spellings are one identity and one credential.
    Userinfo is DROPPED: a token pasted into the address would otherwise reach a log
    line and the credential key. A base that already ends in ``/api/v1`` is refused
    with that sentence rather than silently joined (§3.0).
    """
    raw = (base_url or "").strip()
    if not raw:
        raise EvidenceError("invalid", "the web backend needs a server address")
    if "://" not in raw:
        raw = "https://" + raw
    parts = urlsplit(raw)
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS:
        raise EvidenceError("invalid", f"{base_url!r} is not an http:// or https:// address")
    try:
        host, port = parts.hostname, parts.port
    except ValueError:
        raise EvidenceError("invalid", f"{base_url!r} does not name a host and port") from None
    if not host:
        raise EvidenceError("invalid", f"{base_url!r} does not name a host")
    host = f"[{host}]" if ":" in host else host
    netloc = host if port in (None, _DEFAULT_PORTS[scheme]) else f"{host}:{port}"
    prefix = "/".join(p for p in parts.path.split("/") if p)
    if prefix.lower().endswith(f"{API_PREFIX}"):
        raise EvidenceError(
            "invalid",
            f"the server address is the base, without /{API_PREFIX} — the app appends it: "
            f"{base_url!r}",
        )
    return f"{scheme}://{netloc}/" + (f"{prefix}/" if prefix else "")


@dataclass(frozen=True)
class ApiEndpoint:
    """Where the web server is: its base address and the transport's settings, no credential.
    It has no folder: the server addresses a project by its id."""

    base_url: str
    tls: bool = True
    insecure: bool = False
    ca_bundle: str = ""
    timeout_s: float = 30.0
    proxy: str = ""  # "" = the system proxy, "direct" = bypass, else a proxy URL

    @property
    def base(self) -> str:
        return normalize_base(self.base_url)

    @property
    def identity(self) -> str:
        """The stable, printable key that files this server's token and routes the outbox
        entries and keys the caches: the normalized address and its path prefix — NEVER the
        token (A.2). The scheme is the backend name, so toggling ``insecure`` cannot re-spell
        it and orphan a pending upload."""
        parts = urlsplit(self.base)
        return f"http://{parts.netloc}/{parts.path.strip('/')}"


#: credential key -> (user, token), the same injected shape the FTPS tree takes; the
#: user rides back with the secret rather than being declared in a project's config.
Credentials = Callable[[str], tuple[str, str]]


@dataclass
class _Answer:
    status: int
    headers: dict[str, str]
    body: bytes


# ---------------------------------------------------------------------------
# The wire
# ---------------------------------------------------------------------------


class _Wire:
    """The opener, the credential, the URLs, one request and the §5 translation. Nothing
    here knows what a call means: :class:`evidence_api_http.ApiWire` does."""

    def __init__(self, endpoint: ApiEndpoint, credentials: Credentials | None = None) -> None:
        self.endpoint = endpoint
        self.base = endpoint.base  # raises on a bad address before anything is tried
        self.identity = endpoint.identity
        self.credentials = credentials
        self.requests = 0  # what a cost pin counts
        self._info: dict[str, Any] | None = None
        self._local = ScopeLocal()
        self._check_flags()
        #: Which opener a call takes: this PC's choice for this server, asked before each one.
        self.openers = Openers(self.identity, endpoint.tls, endpoint.ca_bundle, self._opener)

    @property
    def scope(self) -> OperationScope | None:
        """The open operation's scope, or ``None`` outside one."""
        return self._local.scope

    @contextmanager
    def operation(self, cancel: threading.Event | None = None) -> Iterator[OperationScope]:
        """The scope for ONE store operation: one cancel flag. Re-entrant, so a store
        method may enter it without knowing whether its caller already did."""
        with enter_scope(self._local, OperationScope(cancel=cancel)) as scope:
            yield scope

    # -- transport ------------------------------------------------------------------

    def _check_flags(self) -> None:
        """``insecure`` means plain HTTP and nothing else, and the two flags contradict each
        other in BOTH directions, refused here and not at the first upload, the run over."""
        if self.endpoint.tls and self.endpoint.insecure:
            raise EvidenceError(
                "invalid",
                "TLS and plain HTTP are both on, which cannot both be true: turn TLS "
                "off to accept sending the token and all evidence in clear, or clear "
                "plain HTTP to keep the connection encrypted",
            )
        if not self.endpoint.tls and not self.endpoint.insecure:
            raise EvidenceError(
                "invalid",
                "TLS is off but plain HTTP is not asked for: plain HTTP sends the token "
                "and all evidence in clear, and must be chosen explicitly",
            )
        scheme = urlsplit(self.base).scheme
        if self.endpoint.insecure != (scheme == "http"):
            raise EvidenceError(
                "invalid",
                f"the address is {scheme}:// but plain HTTP is "
                f"{'not ' if self.endpoint.insecure else ''}asked for",
            )

    def _opener(self, context: ssl.SSLContext, pool: Any) -> urllib.request.OpenerDirector:
        """One opener: *context* (the TLS :class:`server_certificate.Openers` chose), the
        connections kept in *pool*, and the proxy from the field, never from the environment."""
        proxy = self.endpoint.proxy.strip()
        if proxy.lower() == "direct":
            handler: urllib.request.BaseHandler = urllib.request.ProxyHandler({})
        elif proxy:
            handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        else:
            handler = urllib.request.ProxyHandler()  # the system's, PAC/WPAD included
        return urllib.request.build_opener(
            http_keepalive.KeepAliveHTTPSHandler(pool, context),
            http_keepalive.KeepAliveHTTPHandler(pool),
            handler,
        )

    def _token(self) -> tuple[str, str]:
        """``(user, token)`` for this endpoint, from the injected lookup alone."""
        if self.credentials is None:
            return "", ""
        return self.credentials(self.identity)

    def request(
        self,
        method: str,
        url: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        allow: tuple[int, ...] = (),
        authenticate: bool = True,
        what: str = "",
        sink: Callable[[bytes], object] | None = None,
    ) -> _Answer:
        """One call. Answers in ``allow`` come back instead of raising, because a
        status the caller asked for is an answer (§5.2). *sink* takes a successful
        answer's body block by block as it arrives (the answer's ``body`` is then empty):
        what came before a broken connection is kept, and a download resumes from it."""
        self.check_cancel(what or url)
        sending = dict(headers or {})
        if authenticate:
            token = self._token()[1]
            if token:
                sending["Authorization"] = f"Bearer {token}"
        if body is not None:
            sending["Content-Length"] = str(len(body))
        request = urllib.request.Request(url, data=body, headers=sending, method=method)
        log.debug("%s %s", method, url)  # never the headers: one of them is the token
        self.requests += 1
        opener = self.openers.opener()
        try:
            with opener.open(request, timeout=self.endpoint.timeout_s) as response:
                answer = _Answer(
                    response.status, _lower(response.headers.items()), self._read(response, sink)
                )
        except urllib.error.HTTPError as exc:
            answer = _Answer(exc.code, _lower(exc.headers.items()), exc.read())
        except EvidenceError:
            raise
        except Exception as exc:  # every transport failure leaves as an OSError
            raise _transport_error(exc, what or url, self) from exc
        got_through(self.identity)
        self._verify_signature(answer, what or url)
        if answer.status >= 400 and answer.status not in allow:
            raise _refusal(answer, what or url)
        return answer

    def _read(self, response: Any, sink: Callable[[bytes], object] | None = None) -> bytes:
        """The body, block by block, honouring the scope's cancel between blocks; into
        *sink* when there is one."""
        out = bytearray()
        while True:
            self.check_cancel("download")
            block = response.read(BLOCK)
            if not block:
                return bytes(out)
            if sink is not None:
                sink(block)
            else:
                out += block

    def _verify_signature(self, answer: _Answer, what: str) -> None:
        """``X-Bench-Store: v1`` on EVERY answer, or nothing of ours was reached: the
        app treats a 404 as proof a file is not there, and a captive portal, a
        misrouted proxy and an expired gateway all answer 404 too."""
        if answer.headers.get(SIGNATURE, "").strip() != SIGNATURE_VALUE:
            raise NotOurServer(f"that address answers, but it is not a test-bench server ({what})")

    # -- urls -----------------------------------------------------------------------

    def api(self, tail: str) -> str:
        return f"{self.base}{API_PREFIX}/{tail}"

    # -- scope ----------------------------------------------------------------------

    def check_cancel(self, what: str) -> None:
        scope = self.scope
        if scope is not None and scope.cancelled():
            raise EvidenceError("cancelled", f"cancelled: {what}")

    # -- discovery ------------------------------------------------------------------

    def info(self) -> dict[str, Any]:
        """``/info`` (§3.1), read once per wire: what this server will and will not do."""
        if self._info is None:
            answer = self.request("GET", self.api("info"), what="/info")
            self._info = _json_object(answer.body)
        return self._info

    def read_info(self) -> dict[str, Any] | None:
        """``/info`` as this wire read it; ``None`` before it was."""
        return self._info


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _lower(items: Any) -> dict[str, str]:
    return {str(k).lower(): str(v) for k, v in items}


def error_details(body: bytes) -> dict[str, Any]:
    """The ``details`` of a §5 envelope — what the unauthenticated ``/info`` answers
    with, and the whole public surface of a server (§3.1)."""
    found = _json_object(body).get("error")
    found = found.get("details") if isinstance(found, dict) else None
    return found if isinstance(found, dict) else {}


def _json_object(body: bytes) -> dict[str, Any]:
    try:
        found = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return {}
    return found if isinstance(found, dict) else {}


def envelope(body: bytes) -> tuple[str, str, str, str]:
    """``(code, kind, message, path)`` of a §5 error body; empty strings when it is
    not one. The server's own sentence is what the user is shown."""
    found = _json_object(body).get("error")
    if not isinstance(found, dict):
        return "", "", "", ""
    return (
        str(found.get("code") or ""),
        str(found.get("kind") or ""),
        str(found.get("message") or ""),
        str(found.get("path") or ""),
    )


def _refusal(answer: _Answer, what: str) -> BaseException:
    """A non-2xx answer as the failure the contract requires (§5.1).

    An echoed ``kind`` the app knows wins, so a server may invent any ``code`` and
    still produce the right behaviour; otherwise the status decides.
    ``store_entry_missing`` is the one code that overrides its status: a base path
    that is not there is a configuration mistake, not "no evidence here".
    """
    code, echoed, message, path = envelope(answer.body)
    default = (
        "denied" if code == "store_entry_missing" else STATUS_KIND.get(answer.status, "denied")
    )
    kind = echoed if echoed in ECHOED_KINDS else default
    said = message or f"the server answered {answer.status}"
    where = path or what
    return _as_error(kind, f"{said} ({where})" if where else said)


def _transport_error(exc: BaseException, what: str, wire: _Wire) -> BaseException:
    """A failure that never became an answer. A rejected certificate is ``denied``,
    never ``unreachable``: the host answered and TRUST failed, and reporting it as a
    network problem hides a wrong hostname or an expired certificate behind an
    offline banner. It is said in words, Python's text logged (:mod:`server_certificate`)."""
    where = f" ({what})" if what else ""
    reason = getattr(exc, "reason", exc)
    if isinstance(reason, ssl.SSLCertVerificationError):
        return refused(reason, wire.identity, wire.endpoint, where, wire.scope)
    if isinstance(reason, ssl.SSLError):
        return ConnectionError(f"TLS failed: {reason}{where}")
    if isinstance(reason, TimeoutError):
        return TimeoutError(f"{reason}{where}")
    if isinstance(exc, TimeoutError):
        return TimeoutError(f"{exc}{where}")
    return ConnectionError(f"{reason}{where}")
