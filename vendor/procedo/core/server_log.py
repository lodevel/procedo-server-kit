"""The calls the app makes to a server, in its log (area ``server``), one line each.

* **A web server call** (:func:`http`, around :meth:`evidence_http._Wire.request`, the one seam
  every JSON API call goes through — projects, evidence, the library): the method, the path with
  its ids and names as ``…`` (``/api/v1/projects/…/boards``), the answer, the time it took and the
  server's ``X-Request-Id``, so the same call is found in the server's own log.
* **A connection** to an FTPS or SFTP server or a database (:func:`connecting`): where, and how
  long the sign-in took.

The levels: an answer the app expects is INFO — 2xx, and 304, 404, 409, 412, which callers ask
for and decide on; any other refusal (4xx), a server failure (5xx) and no answer at all a WARN:
the operation that failed says it as an ERROR, once per outage (``failure_log``), so an outage
counts once in the footer. Nothing else of a call is logged: not a header (one carries the token), not the
query, not a body.
"""

from __future__ import annotations

import functools
import logging
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

#: The fixed words of the API's paths (``docs/plans/procedo_api.openapi.yaml``, pinned by
#: ``tests/test_app_log.py``); any other segment is an id or a name, shown as ``…``.
WORDS = frozenset(
    {
        "api",
        "v1",
        "info",
        "storages",
        "to_delete",
        "deleted",
        "blobs",
        "benches",
        "users",
        "projects",
        "read",
        "changes",
        "tests",
        "releases",
        "runs",
        "annotation",
        "delete",
        "restore",
        "boards",
        "operators",
        "readings",
        "current",
        "entries",
        "artifacts",
        "purge",
        "library",
    }
)
#: Refusals a caller asks for and decides on: an answer, not a problem.
_EXPECTED = frozenset({304, 404, 409, 412})
#: The longest request id kept: the server's header, read as it came.
_REQUEST_ID_MAX = 64


def path_of(url: str, base: str) -> str:
    """The call's path below the server's address, every id and name as ``…``, no query."""
    tail = url[len(base) :] if url.startswith(base) else urlsplit(url).path
    tail = tail.split("?", 1)[0]
    return "/" + "/".join(s if s in WORDS else "…" for s in tail.strip("/").split("/") if s)


def took(started: float) -> str:
    """The time since *started* (``time.monotonic``): ``84 ms``, ``12.3 s`` from ten seconds."""
    ms = (time.monotonic() - started) * 1000
    return f"{ms:.0f} ms" if ms < 10_000 else f"{ms / 1000:.1f} s"


def request_id(headers: Mapping[str, str]) -> str:
    """The server's ``X-Request-Id`` (*headers* lower-cased), printable characters only."""
    found = "".join(ch for ch in headers.get("x-request-id", "") if ch.isprintable()).strip()
    return found[:_REQUEST_ID_MAX]


class HttpCall:
    """One call being made: :meth:`answered` says its answer; no answer is said by :func:`http`."""

    def __init__(self, method: str, url: str, base: str) -> None:
        self.method, self.path, self.base = method, path_of(url, base), base
        self.started = time.monotonic()
        self.done = False

    def answered(self, status: int, headers: Mapping[str, str]) -> None:
        self.done = True
        ok = status < 400 or status in _EXPECTED
        level = logging.INFO if ok else logging.WARNING
        found = request_id(headers)
        said = f" · request {found}" if found else ""
        log.log(
            level, "%s %s → %s in %s%s", self.method, self.path, status, took(self.started), said
        )

    def failed(self, error: BaseException) -> None:
        self.done = True
        level = logging.INFO if getattr(error, "kind", "") == "cancelled" else logging.WARNING
        log.log(
            level,
            "%s %s → no answer after %s: %s",
            self.method,
            self.path,
            took(self.started),
            error,
        )


@contextmanager
def http(method: str, url: str, base: str) -> Iterator[HttpCall]:
    """The log line of one web server call: its answer once :meth:`HttpCall.answered`, else
    the failure that left the block."""
    call = HttpCall(method, url, base)
    try:
        yield call
    except BaseException as exc:
        if not call.done:
            call.failed(exc)
        raise


def connecting(
    kind: str, where: Callable[..., str]
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """The log line of a connection made by the wrapped function: *kind* (``FTPS``…) and the
    place *where* names from the function's arguments (an address, never a secret)."""

    def wrap(connect: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(connect)
        def call(*args: Any, **kwargs: Any) -> Any:
            place, started = where(*args, **kwargs), time.monotonic()
            try:
                made = connect(*args, **kwargs)
            except BaseException as exc:
                log.warning("%s %s → not connected after %s: %s", kind, place, took(started), exc)
                raise
            log.info("%s %s → connected in %s", kind, place, took(started))
            return made

        return call

    return wrap
