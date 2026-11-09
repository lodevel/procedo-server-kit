"""Persistent connections under ``urllib``: one open connection per server, per thread, reused.

``urllib`` opens a connection for every request and closes it after (it sends ``Connection:
close``), so each call to a web store paid a TCP and a TLS handshake: a cold project scan of
about 45 requests opened 45 TLS sessions (``web_api_spec.md`` §7 asks for persistent
connections; docs/plans/company_storage_design.md §9 step 5).

These handlers replace ``urllib``'s own HTTP and HTTPS ones in an opener, so everything else
``urllib`` does stays as it was: the proxy (from the field or the system's, a CONNECT tunnel for
HTTPS), the verified TLS context, the error statuses as ``HTTPError``.

* **One connection per (server, tunnel) per thread**: an ``http.client`` connection is not
  thread-safe, and the store's operations run on several workers.
* **A connection the server closed while idle** is found when it is reused: the request is
  sent once more on a new connection. Only then: a request that fails on a NEW connection
  fails as before.
* An answer the server marks as the last (``Connection: close``, HTTP/1.0) takes its
  connection out of the pool; the body still reads to its end.
* :meth:`Pool.close` closes every connection the pool opened, whichever thread holds it.

Stdlib only; Qt-free.
"""

from __future__ import annotations

import http.client
import threading
import urllib.error
import urllib.request
from typing import Any


class Pool:
    """The open connections, by ``(scheme, host, tunnel)``, one set per thread."""

    def __init__(self) -> None:
        self._local = threading.local()
        self._lock = threading.Lock()
        self._opened: list[http.client.HTTPConnection] = []
        #: Connections opened, for a cost pin: a reused one is not counted again.
        self.opened = 0

    def _mine(self) -> dict[tuple, http.client.HTTPConnection]:
        conns = getattr(self._local, "conns", None)
        if conns is None:
            conns = self._local.conns = {}
        return conns

    def get(self, key: tuple) -> http.client.HTTPConnection | None:
        return self._mine().get(key)

    def put(self, key: tuple, conn: http.client.HTTPConnection) -> None:
        self._mine()[key] = conn
        with self._lock:
            self._opened.append(conn)
            self.opened += 1

    def forget(self, key: tuple) -> None:
        """Out of the pool, left open: an answer still being read holds its socket."""
        self._mine().pop(key, None)

    def drop(self, key: tuple) -> None:
        conn = self._mine().pop(key, None)
        if conn is not None:
            conn.close()

    def close(self) -> None:
        with self._lock:
            opened, self._opened = self._opened, []
        for conn in opened:
            conn.close()  # another thread reusing it later reconnects (http.client auto_open)


#: Failures of a REUSED connection that mean the server closed it while it was idle.
_STALE = (ConnectionError, http.client.RemoteDisconnected, http.client.CannotSendRequest)


def _send(
    pool: Pool, http_class: type, req: urllib.request.Request, **conn_args: Any
) -> http.client.HTTPResponse:
    """``AbstractHTTPHandler.do_open`` with the connection kept: the same headers and tunnel,
    ``Connection: keep-alive`` instead of ``close``."""
    host = req.host
    if not host:
        raise urllib.error.URLError("no host given")
    tunnel = getattr(req, "_tunnel_host", None)
    key = (http_class.__name__, host, tunnel)
    headers = dict(req.unredirected_hdrs)
    headers.update({k: v for k, v in req.headers.items() if k not in headers})
    headers["Connection"] = "keep-alive"
    headers = {name.title(): value for name, value in headers.items()}
    tunnel_headers = {}
    if tunnel and "Proxy-Authorization" in headers:
        tunnel_headers["Proxy-Authorization"] = headers.pop("Proxy-Authorization")
    for attempt in (1, 2):
        conn = pool.get(key)
        reused = conn is not None
        if conn is None:
            conn = http_class(host, timeout=req.timeout, **conn_args)
            if tunnel:
                conn.set_tunnel(tunnel, headers=tunnel_headers)
            pool.put(key, conn)
        else:
            conn.timeout = req.timeout
            if conn.sock is not None:
                conn.sock.settimeout(req.timeout)
        try:
            conn.request(
                req.get_method(),
                req.selector,
                req.data,
                headers,
                encode_chunked=req.has_header("Transfer-encoding"),
            )
            response = conn.getresponse()
        except _STALE as exc:
            pool.drop(key)
            if reused and attempt == 1:
                continue  # closed by the server while idle: once more, on a new connection
            raise urllib.error.URLError(exc) from exc
        except OSError as exc:
            pool.drop(key)
            raise urllib.error.URLError(exc) from exc
        except BaseException:
            pool.drop(key)  # a half-sent request leaves the connection unusable
            raise
        if response.will_close:
            pool.forget(key)
        response.url = req.get_full_url()  # type: ignore[attr-defined]
        response.msg = response.reason  # type: ignore[assignment]
        return response
    raise AssertionError("unreachable")  # pragma: no cover


class KeepAliveHTTPHandler(urllib.request.HTTPHandler):
    """``urllib``'s plain-HTTP handler, connections kept in *pool*."""

    def __init__(self, pool: Pool) -> None:
        super().__init__()
        self.pool = pool

    def http_open(self, req: urllib.request.Request) -> http.client.HTTPResponse:
        return _send(self.pool, http.client.HTTPConnection, req)


class KeepAliveHTTPSHandler(urllib.request.HTTPSHandler):
    """``urllib``'s HTTPS handler with *context* (verification as the caller set it),
    connections kept in *pool*."""

    def __init__(self, pool: Pool, context: Any) -> None:
        super().__init__(context=context)
        self.pool = pool
        self.context = context

    def https_open(self, req: urllib.request.Request) -> http.client.HTTPResponse:
        return _send(self.pool, http.client.HTTPSConnection, req, context=self.context)
