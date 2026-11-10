"""Run the reference server on its own, as an external server the kit can be pointed at.

    python serve_reference.py [--port 8765] [--prefix /procedo/] [--data DIR] [--body-timeout 60]
                              [--calls all|none|P3,E2,...] [--boards]

It serves the JSON API on ``http://127.0.0.1:<port><prefix>`` until Ctrl+C (or until its
standard input closes, with ``--until-stdin-closes``), behind a small front that strips the path
prefix as a reverse proxy would. It prints the environment the kit needs to run against it:

    PROCEDO_KIT_SERVER=http://127.0.0.1:8765/procedo/
    PROCEDO_KIT_BENCH_TOKEN=kit-bench
    PROCEDO_KIT_MANAGER_TOKEN=kit-manager
    PROCEDO_KIT_IMPORTER_TOKEN=kit-importer
    PROCEDO_KIT_MOVER_TOKEN=kit-mover
    PROCEDO_KIT_BENCH_ONLY_TOKEN=kit-bench-only

The reference server keeps everything in memory and under ``--data`` (a new temporary folder by
default): stopping it forgets what it held. A request body that stops arriving for
``--body-timeout`` seconds is ``408 request_timeout``: the front passes what came on, and the
server gives up on the rest.

It offers every optional call of the contract (§6.1), U1 listing the users the kit's runs name
(``server_target.KIT_USERS``). ``--calls`` names those it offers instead (``none``: the core
calls only): the others answer ``501 unsupported``, so the kit can be tried against a server
offering some of them only. ``import`` is named the same way: without it, no login holds the
importer role and an ``"import": true`` answers ``501`` (§6.1).
"""

from __future__ import annotations

import argparse
import http.client
import shutil
import sys
import tempfile
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

#: The reference server, and the one module of the app it imports (the sync's exclusion list).
_KIT = Path(__file__).resolve().parent
sys.path[:0] = [str(_KIT / "vendor"), str(_KIT / "suites")]

from reference_server import start, stop  # noqa: E402
from server_target import KIT_USERS  # noqa: E402

BENCH_TOKEN, MANAGER_TOKEN = "kit-bench", "kit-manager"
IMPORTER_TOKEN, BENCH_ONLY_TOKEN = "kit-importer", "kit-bench-only"
MOVER_TOKEN = "kit-mover"
TOKENS = {
    BENCH_TOKEN: {"name": "bench", "roles": ["bench", "publisher"]},
    MANAGER_TOKEN: {"name": "manager-login", "roles": ["manager"]},
    IMPORTER_TOKEN: {"name": "importer-login", "roles": ["importer"]},
    MOVER_TOKEN: {"name": "mover-login", "roles": ["bench", "importer"]},
    BENCH_ONLY_TOKEN: {"name": "bench-only-login", "roles": ["bench"]},
}
#: The variables the kit reads, in the order they are printed.
VARIABLES = (
    ("PROCEDO_KIT_BENCH_TOKEN", BENCH_TOKEN),
    ("PROCEDO_KIT_MANAGER_TOKEN", MANAGER_TOKEN),
    ("PROCEDO_KIT_IMPORTER_TOKEN", IMPORTER_TOKEN),
    ("PROCEDO_KIT_MOVER_TOKEN", MOVER_TOKEN),
    ("PROCEDO_KIT_BENCH_ONLY_TOKEN", BENCH_ONLY_TOKEN),
)
#: Headers that belong to one hop, never forwarded; the front sets its own Date and Server.
HOP = {
    "connection",
    "keep-alive",
    "transfer-encoding",
    "content-length",
    "date",
    "server",
    "host",
    "expect",
}


class _Front(BaseHTTPRequestHandler):
    """Forward a request under the prefix to the reference server, the prefix taken off; any
    other path is a plain 404, as a proxy that routes nothing there answers. A body over the
    server's limit is never read: the server answers ``413`` from the head alone (§3.6)."""

    protocol_version = "HTTP/1.1"
    server: _FrontServer

    def log_message(self, format: str, *args: object) -> None:
        pass

    def _over_limit(self) -> bool:
        """The body is over the server's limit for this call: it is never read here, and the
        server answers ``413`` from the head alone, as a proxy that buffers no upload lets it."""
        length = self.headers.get("Content-Length", "")
        path = urlsplit(self.path).path
        return length.isdigit() and int(length) > self.server.limit(self.command, path)

    def handle_expect_100(self) -> bool:
        if self._over_limit():
            return True  # no "100 Continue": the body is refused unread
        return super().handle_expect_100()

    def _forward(self) -> None:
        prefix = self.server.prefix
        if not self.path.startswith(prefix):
            self._plain_404()
            return
        length = self.headers.get("Content-Length")
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP | {"expect"}}
        if self.headers.get("Transfer-Encoding"):
            body = None  # no length declared: passed on as sent, for the server to refuse (411)
            headers["Transfer-Encoding"] = self.headers["Transfer-Encoding"]
            self.close_connection = True
        elif self._over_limit():
            body = None
            headers["Content-Length"] = length  # the head alone: the server refuses it unread
            self.close_connection = True
        elif length and length.isdigit():
            body = self._read_body(int(length))
            if len(body) < int(length):  # stalled: the server answers 408 when it gives up too
                headers["Content-Length"] = length
                self.close_connection = True
        else:
            body = None
        backend = self.server.backend()
        for attempt in (1, 2):  # a kept connection the server closed meanwhile: once more
            try:
                backend.request(self.command, "/" + self.path[len(prefix) :], body, headers)
                answer = backend.getresponse()
                data = answer.read()
                break
            except (OSError, http.client.HTTPException):
                backend.close()
                if attempt == 2:
                    raise
        self.send_response(answer.status)
        for name, value in answer.getheaders():
            if name.lower() not in HOP:
                self.send_header(name, value)
        self.send_header("Content-Length", str(len(data)))
        if self.close_connection or answer.getheader("Connection", "").lower() == "close":
            self.close_connection = True
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self, size: int) -> bytes:
        """The body's bytes as they arrive, until *size* or until none came for the server's
        ``body_timeout``: then what arrived."""
        chunks, remaining = [], size
        self.connection.settimeout(self.server.body_timeout)
        try:
            while remaining > 0:
                chunk = self.rfile.read1(min(remaining, 65536))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
        except TimeoutError:
            pass
        finally:
            self.connection.settimeout(None)
        return b"".join(chunks)

    def _plain_404(self) -> None:
        data = b"Not found\n"
        self.send_response(404)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _forward


class _FrontServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port: int, prefix: str, backend_url: str, state) -> None:
        super().__init__(("127.0.0.1", port), _Front)
        self.prefix = prefix
        self.body_timeout = state.body_timeout
        self._netloc = urlsplit(backend_url).netloc
        self._local = threading.local()
        self._state = state

    def limit(self, method: str, path: str) -> int:
        """The server's limit on a request's body (``/info``'s ``limits``): ``max_file_bytes``
        for a file sent alone (B1), ``max_upload_bytes`` for every other request."""
        segments = path[len(self.prefix) :].split("/")
        upload = method == "POST" and segments[:3] == ["api", "v1", "storages"]
        if upload and len(segments) == 7 and segments[4] == "blobs":
            return self._state.max_file_bytes
        return self._state.max_upload_bytes

    def backend(self) -> http.client.HTTPConnection:
        """This thread's connection to the reference server, kept open."""
        connection = getattr(self._local, "connection", None)
        if connection is None:
            connection = http.client.HTTPConnection(self._netloc, timeout=120)
            self._local.connection = connection
        return connection


def _calls(text: str) -> list[str] | None:
    """``--calls``: None for every optional call, else those named (none for ``none``)."""
    text = text.strip()
    if text.lower() == "all":
        return None
    if text.lower() == "none":
        return []
    return [call.strip() for call in text.split(",") if call.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8765, help="0 picks a free port")
    parser.add_argument("--prefix", default="/procedo/", help="the path prefix, / for none")
    parser.add_argument("--data", type=Path, help="where the server keeps its files")
    parser.add_argument(
        "--body-timeout", type=float, default=60.0, help="seconds a stalled body waits: then 408"
    )
    parser.add_argument(
        "--until-stdin-closes", action="store_true", help="stop when standard input closes"
    )
    parser.add_argument(
        "--calls",
        default="all",
        help="the optional calls it offers, comma-separated (P3,E2,U1), 'all' or 'none': "
        "the others answer 501 unsupported",
    )
    parser.add_argument(
        "--boards",
        action="store_true",
        help="register its boards itself (boards_registered, §3.3): one board, PN-1/A/SN-REG, in "
        "a new project it prints as PROCEDO_KIT_BOARDS_PROJECT",
    )
    args = parser.parse_args(argv)
    prefix = "/" + args.prefix.strip("/") + "/" if args.prefix.strip("/") else "/"
    data = args.data or Path(tempfile.mkdtemp(prefix="procedo_reference_"))
    calls = _calls(args.calls)
    users = list(KIT_USERS)
    boards_project = str(uuid.uuid4())
    board = {"serial": "SN-REG", "part_number": "PN-1", "board_revision": "A"}
    boards = {boards_project: [board]} if args.boards else None
    backend_url, running = start(
        data, TOKENS, body_timeout=args.body_timeout, calls=calls, users=users, boards=boards
    )
    front = _FrontServer(args.port, prefix, backend_url, running.state)
    port = front.server_address[1]
    print(f"PROCEDO_KIT_SERVER=http://127.0.0.1:{port}{prefix}")
    for variable, value in VARIABLES:
        print(f"{variable}={value}")
    if args.boards:
        print(f"PROCEDO_KIT_BOARDS_PROJECT={boards_project}")
    sys.stdout.flush()
    thread = threading.Thread(target=front.serve_forever, daemon=True)
    thread.start()
    try:
        if args.until_stdin_closes:
            sys.stdin.read()
        else:
            thread.join()
    except KeyboardInterrupt:
        pass
    finally:
        front.shutdown()
        front.server_close()
        stop(running)
        if args.data is None:
            shutil.rmtree(data, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
