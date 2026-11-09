"""The JSON API on the wire: one call by its operationId, answered as JSON and parts.

The contract is ``docs/plans/data_format.md`` (§1–§3, §7) and ``procedo_api.openapi.yaml``;
``web_api_spec.md`` holds the address, the headers, discovery, the refusals. The transport is
:class:`evidence_http._Wire`: the verified TLS context, the proxy, the
connections kept open (:mod:`http_keepalive`), the bearer token from the injected lookup, the
``X-Bench-Store`` signature and the cancel flag of the open operation. What this module adds is
what the JSON API asks of a call:

* **GET reads, POST writes**, a path built from quoted segments and a query whose ``+`` is a
  plus (a part number may hold ``/``, ``#`` or ``+``).
* **gzip** on JSON both ways (§2, Compression): a request's JSON (the whole body, or the
  ``json`` part of a multipart one) is compressed above :data:`GZIP_OVER`; an answer, or its
  ``json`` part, says whether it is.
* **Parts** (§3.3): a write that carries files is ``multipart/form-data``, its ``json`` part
  first, then one part per distinct content named ``sha256:<hex>``; an answer with parts is
  ``multipart/mixed``. Bytes never travel as base64.
* **A refusal is the envelope** (``web_api_spec.md`` §5): :class:`ApiError` keeps the kind the
  app acts on (an echoed kind it knows wins, else the status), and the contract's ``code`` and
  ``details`` a caller decides on (``missing_files``, ``deleted``, ``changed``…). A failure that
  never became an answer is the kind :func:`evidence_store_fs.os_error` gives it.

Qt-free; ``urllib`` from the stdlib, so no new dependency.
"""

from __future__ import annotations

import gzip
import json
import re
import secrets
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode

from .evidence_http import (
    ECHOED_KINDS,
    STATUS_KIND,
    ApiEndpoint,
    _Wire,
    envelope,
    error_details,
)
from .evidence_store import EvidenceError
from .evidence_store_fs import os_error

API_VERSION = "1"  # the major version this app speaks; /info must list it
#: A JSON body larger than this is sent gzipped (§2): small ones gain nothing.
GZIP_OVER = 1024
#: A ``have`` list longer than this goes in the body of the read's POST twin (§3.4).
HAVE_IN_QUERY = 6000
#: Every refusal comes back as an answer, so its envelope is read here, never lost.
_REFUSALS = tuple(range(400, 600))
_NAME = re.compile(r'(?:^|;)\s*name="([^"]*)"')
_BOUNDARY = re.compile(r'boundary="?([^";]+)"?')


class ApiError(EvidenceError):
    """A refusal the server answered: the app's kind, and the contract's code and details."""

    def __init__(
        self, kind: str, message: str, *, status: int, code: str, details: dict[str, Any]
    ) -> None:
        super().__init__(kind, message, code=code)
        self.status = status
        self.details = details


@dataclass
class Answer:
    """One call's answer: its JSON object and the parts it carried, by name."""

    data: dict[str, Any]
    parts: dict[str, bytes]


@dataclass(frozen=True)
class Part:
    """One file sent as a part: named by its version, its file name a hint for logs."""

    version: str  # sha256:<hex>
    filename: str
    content_type: str
    data: bytes


class ApiWire:
    """The JSON API's calls over one endpoint. It is the store's ``io``: its ``identity``
    routes the outbox entries, its ``flavour`` is the one a run's file names are checked in,
    and its ``operation`` is the scope one store call runs in (one cancel flag)."""

    flavour = "posix"

    def __init__(self, endpoint: ApiEndpoint, credentials: Callable[[str], tuple[str, str]] | None):
        self.wire = _Wire(endpoint, credentials)
        self.identity = endpoint.identity
        self._info_lock = threading.Lock()
        self._instance_id: str | None = None

    @contextmanager
    def operation(self, cancel: threading.Event | None = None) -> Iterator[Any]:
        with self.wire.operation(cancel) as scope:
            yield scope

    def close(self) -> None:
        self.wire.pool.close()

    def info(self) -> dict[str, Any]:
        """``/info`` (getInfo), read once: which API this server speaks, and this login."""
        with self._info_lock:
            try:
                return self.wire.info()
            except OSError as exc:
                raise os_error(exc) from exc

    def instance_id(self, cancel: threading.Event | None = None) -> str:
        """The server's own name for itself (S1's ``instance_id``, ``data_format.md`` §3.1),
        read once: what tells one server from another however its address is spelled (the
        evidence client and the projects' share this wire's class, so both ask it alike)."""
        if self._instance_id is None:
            require_json_api(self.info())
            with self.operation(cancel):
                data = self.call("listStorages", "GET", ("storages",)).data
            self._instance_id = str(data.get("instance_id") or "")
        return self._instance_id

    def call(
        self,
        op: str,
        method: str,
        segments: tuple[str, ...],
        *,
        query: Mapping[str, str | int] | None = None,
        body: dict[str, Any] | bytes | None = None,
        parts: list[Part] | None = None,
        token: str | None = None,
    ) -> Answer:
        """One call of the contract (``op`` is its operationId: what a log line and a refusal
        name). ``segments`` are the path below ``/api/v1``, each quoted on its own; ``parts``
        makes the body multipart; ``token`` replaces the store's login for this call alone
        (a manager's, asked for at the moment)."""
        url = self.wire.api("/".join(quote(str(s), safe="") for s in segments))
        if query:
            url += "?" + urlencode({k: str(v) for k, v in query.items()}, quote_via=quote, safe="")
        headers = {"Accept": "multipart/mixed, application/json", "Accept-Encoding": "gzip"}
        data = None
        json_part = body if isinstance(body, bytes) else json_bytes(body or {})
        if parts:
            data, headers["Content-Type"] = _form(json_part, parts)
        elif body is not None:
            data = json_part
            headers["Content-Type"] = "application/json"
            if len(data) > GZIP_OVER:
                data, headers["Content-Encoding"] = gzip.compress(data), "gzip"
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        try:
            answer = self.wire.request(
                method,
                url,
                body=data,
                headers=headers,
                allow=_REFUSALS,
                authenticate=token is None,
                what=op,
            )
        except OSError as exc:
            raise os_error(exc) from exc
        raw = answer.body
        if answer.headers.get("content-encoding", "").strip().lower() == "gzip":
            raw = _gunzip(raw, op)
        if answer.status >= 400:
            raise _refusal(answer.status, raw, op)
        kind = answer.headers.get("content-type", "")
        if kind.lower().startswith("multipart/"):
            return _mixed(raw, kind, op)
        return Answer(_json_object(raw, op), {})

    def upload(self, op: str, segments: tuple[str, ...], data: bytes) -> Answer:
        """One file's bytes as the whole body (B1, ``application/octet-stream``)."""
        url = self.wire.api("/".join(quote(str(s), safe="") for s in segments))
        headers = {"Content-Type": "application/octet-stream", "Accept": "application/json"}
        try:
            answer = self.wire.request(
                "POST", url, body=data, headers=headers, allow=_REFUSALS, what=op
            )
        except OSError as exc:
            raise os_error(exc) from exc
        if answer.status >= 400:
            raise _refusal(answer.status, answer.body, op)
        return Answer(_json_object(answer.body, op), {})

    def download(
        self, op: str, segments: tuple[str, ...], sink: Callable[[bytes], object], start: int = 0
    ) -> int:
        """One file's bytes (B2), from byte *start* on, block by block into *sink* as they
        arrive. The status: ``206`` when the server sent from *start*, ``200`` when it sent the
        whole file (the caller then starts over)."""
        url = self.wire.api("/".join(quote(str(s), safe="") for s in segments))
        headers = {"Accept": "application/octet-stream"}
        if start:
            headers["Range"] = f"bytes={start}-"
        try:
            answer = self.wire.request(
                "GET", url, headers=headers, allow=_REFUSALS, what=op, sink=sink
            )
        except OSError as exc:
            raise os_error(exc) from exc
        if answer.status >= 400:
            raise _refusal(answer.status, answer.body, op)
        return answer.status


def require_json_api(info: dict[str, Any]) -> dict[str, Any]:
    """*info* (``/info``) when the server serves the major version of the API this app speaks;
    refused otherwise, both named (``web_api_spec.md`` §3.1, ``api_versions``)."""
    served = [str(v) for v in info.get("api_versions") or []]
    if API_VERSION not in served:
        theirs = ", ".join(f"v{v}" for v in served) or "no version"
        raise EvidenceError(
            "denied", f"This server speaks API {theirs}; this app speaks v{API_VERSION}."
        )
    return info


def active_store(storages: list[Any]) -> int:
    """The ``store_id`` new files go to (S1, §3.1). Only the server's own storage (``api``) is
    written yet: another backend is a project's own storage, which this app does not reach yet,
    so the write stops and nothing is sent."""
    active = [s for s in storages if isinstance(s, dict) and s.get("active")]
    if not active:
        raise EvidenceError("denied", "This server names no storage for new files.")
    if active[0].get("backend") != "api":
        raise EvidenceError(
            "denied",
            f"This server keeps new files in a {active[0].get('backend')} storage, which this "
            "app cannot write to yet: nothing was sent.",
        )
    return int(active[0]["store_id"])


def version(sha256: str) -> str:
    """A bare sha256 as a version (§2, Hashes)."""
    return f"sha256:{sha256}"


# ---------------------------------------------------------------------------
# Bodies
# ---------------------------------------------------------------------------


def json_bytes(body: dict[str, Any]) -> bytes:
    """An object as the JSON a call sends."""
    return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _form(json_part: bytes, parts: list[Part]) -> tuple[bytes, str]:
    """``multipart/form-data``: the ``json`` part first, then one part per distinct content."""
    boundary = secrets.token_hex(16).encode("ascii")
    out = bytearray()

    def add(headers: list[str], data: bytes) -> None:
        out.extend(b"--" + boundary + b"\r\n")
        out.extend("".join(f"{h}\r\n" for h in headers).encode("utf-8") + b"\r\n")
        out.extend(data + b"\r\n")

    head = ['Content-Disposition: form-data; name="json"', "Content-Type: application/json"]
    if len(json_part) > GZIP_OVER:
        json_part = gzip.compress(json_part)
        head.append("Content-Encoding: gzip")
    add(head, json_part)
    sent: set[str] = set()
    for p in parts:
        if p.version in sent:
            continue  # two files with the same bytes travel as one part (§3.3)
        sent.add(p.version)
        hint = re.sub(r"[^A-Za-z0-9._-]", "_", p.filename.rsplit("/", 1)[-1]) or "file"
        add(
            [
                f'Content-Disposition: form-data; name="{p.version}"; filename="{hint}"',
                f"Content-Type: {p.content_type}",
            ],
            p.data,
        )
    out.extend(b"--" + boundary + b"--\r\n")
    return bytes(out), f"multipart/form-data; boundary={boundary.decode('ascii')}"


def _mixed(body: bytes, content_type: str, op: str) -> Answer:
    """A ``multipart/mixed`` answer: the ``json`` part and the file parts by name."""
    found = _BOUNDARY.search(content_type)
    if found is None:
        raise EvidenceError("corrupt", f"a multipart answer without its boundary ({op})")
    marker = b"--" + found.group(1).encode("ascii")
    pos = body.find(marker)
    if pos < 0:
        raise EvidenceError("corrupt", f"a multipart answer without its parts ({op})")
    pos += len(marker)
    data: dict[str, Any] | None = None
    parts: dict[str, bytes] = {}
    while not body.startswith(b"--", pos):
        head_end = body.find(b"\r\n\r\n", pos)
        end = body.find(b"\r\n" + marker, head_end + 4)
        if not body.startswith(b"\r\n", pos) or head_end < 0 or end < 0:
            raise EvidenceError("corrupt", f"a multipart answer cut short ({op})")
        headers = _headers(body[pos + 2 : head_end])
        content = body[head_end + 4 : end]
        name = _NAME.search(headers.get("content-disposition", ""))
        if name is not None and name.group(1) == "json":
            if headers.get("content-encoding", "").strip().lower() == "gzip":
                content = _gunzip(content, op)
            data = _json_object(content, op)
        elif name is not None:
            parts[name.group(1)] = content
        pos = end + 2 + len(marker)
    if data is None:
        raise EvidenceError("corrupt", f"a multipart answer without its json part ({op})")
    return Answer(data, parts)


def _headers(block: bytes) -> dict[str, str]:
    out = {}
    for line in block.decode("utf-8", "replace").split("\r\n"):
        name, sep, value = line.partition(":")
        if sep:
            out[name.strip().lower()] = value.strip()
    return out


def _gunzip(data: bytes, op: str) -> bytes:
    try:
        return gzip.decompress(data)
    except (OSError, EOFError) as exc:
        raise EvidenceError("corrupt", f"a gzip answer that does not decompress ({op})") from exc


def _json_object(data: bytes, op: str) -> dict[str, Any]:
    try:
        found = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise EvidenceError("corrupt", f"the server's answer is not JSON ({op})") from exc
    if not isinstance(found, dict):
        raise EvidenceError("corrupt", f"the server's answer is not a JSON object ({op})")
    return found


def _refusal(status: int, body: bytes, op: str) -> ApiError:
    """The envelope (``web_api_spec.md`` §5): the server's sentence is what the user reads; an
    echoed kind the app knows wins, else the status decides (§5.1)."""
    code, echoed, message, path = envelope(body)
    kind = echoed if echoed in ECHOED_KINDS else STATUS_KIND.get(status, "denied")
    said = message or f"the server answered {status}"
    where = f"{path}, {op}" if path else op
    return ApiError(
        kind, f"{said} ({where})", status=status, code=code, details=error_details(body)
    )
