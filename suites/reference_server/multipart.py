"""Multipart bodies (data_format.md §3.3): ``multipart/form-data`` up, ``multipart/mixed`` down.

Hand-written on purpose: the stdlib ``cgi`` module is gone, and the contract's form is small:
a first part named ``json``, then one part per content named ``sha256:<hex>`` of its bytes.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import secrets
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from email.message import Message
from urllib.parse import quote

from .errors import ApiError, bad_request
from .validate import VERSION

# §3.4b: a file of at most 1 MB rides as a part; a larger one travels alone, by the blob calls.
MAX_PART_BYTES = 1024 * 1024


@dataclass
class Part:
    """One file part of an answer: its bytes, named by their sha256."""

    sha: str
    filename: str
    mimetype: str
    data: bytes


def header_param(header_name: str, value: str, param: str) -> str | None:
    """A parameter of a header (``boundary`` of a Content-Type, ``name`` of a disposition)."""
    message = Message()
    message[header_name] = value
    found = message.get_param(param, header=header_name)
    if isinstance(found, tuple):  # RFC 2231 form
        found = found[2]
    return found


def gunzip(data: bytes, what: str) -> bytes:
    try:
        return gzip.decompress(data)
    except (OSError, EOFError, zlib.error) as exc:
        raise bad_request(f"The {what} says gzip but is not a gzip stream.") from exc


def parse_form_data(
    body: bytes, content_type: str, broken: Callable[[str], bool] = lambda _rule: False
) -> tuple[bytes, dict[str, bytes]]:
    """Split a ``multipart/form-data`` request into its JSON bytes and its file parts.

    Returns the ``json`` part's bytes (un-gzipped) and ``{sha256 hex: bytes}``. Each file
    part is hashed: a part whose bytes do not match its name is ``422 digest_mismatch``.
    """
    boundary = header_param("content-type", content_type, "boundary")
    if not boundary:
        raise bad_request("A multipart/form-data body needs its boundary.")
    raw_parts = _split(body, boundary.encode("latin-1"))
    if not raw_parts:
        raise bad_request("The multipart body holds no part.")
    json_bytes: bytes | None = None
    files: dict[str, bytes] = {}
    for index, (headers, data) in enumerate(raw_parts):
        disposition = headers.get("content-disposition", "")
        name = header_param("content-disposition", disposition, "name") if disposition else None
        if name is None:
            raise bad_request("Each part needs a Content-Disposition with its name.")
        if index == 0:
            if name != "json":
                raise bad_request('The first part must be named "json".')
            if headers.get("content-encoding", "").strip().lower() == "gzip":
                data = gunzip(data, "json part")
            json_bytes = data
            continue
        if name == "json":
            raise bad_request('Only the first part is named "json".')
        match = VERSION.fullmatch(name)
        if match is None:
            raise bad_request(f"A file part is named sha256:<64 hex>, not {name!r}.")
        sha = match.group(1)
        if sha in files and not broken("6.2.two-parts-one-name-400"):
            raise bad_request(f"Two parts are named {name}; one content travels as one part.")
        if headers.get("content-encoding"):
            raise bad_request("A file part never carries a Content-Encoding.")
        if hashlib.sha256(data).hexdigest() != sha and not broken("6.2.digest-mismatch"):
            if broken("4.3.422"):
                raise bad_request(f"The bytes of part {name} do not match its name.")
            raise ApiError(
                422,
                "digest_mismatch",
                "corrupt",
                f"The bytes of part {name} do not match its name.",
                details={"part": name},
            )
        files[sha] = data
    assert json_bytes is not None
    return json_bytes, files


def _split(body: bytes, boundary: bytes) -> list[tuple[dict[str, str], bytes]]:
    delimiter = b"--" + boundary
    if body.startswith(delimiter):
        position = len(delimiter)
    else:
        start = body.find(b"\r\n" + delimiter)
        if start < 0:
            raise bad_request("The multipart body does not hold its boundary.")
        position = start + 2 + len(delimiter)
    parts = []
    while True:
        if body[position : position + 2] == b"--":
            return parts
        line_end = body.find(b"\r\n", position)
        if line_end < 0 or body[position:line_end].strip(b" \t"):
            raise bad_request("The multipart body is malformed after a boundary.")
        start = line_end + 2
        end = body.find(b"\r\n" + delimiter, start)
        if end < 0:
            raise bad_request("The multipart body has no closing boundary.")
        parts.append(_part(body[start:end]))
        position = end + 2 + len(delimiter)


def _part(raw: bytes) -> tuple[dict[str, str], bytes]:
    if raw.startswith(b"\r\n"):
        head, data = b"", raw[2:]
    else:
        split = raw.find(b"\r\n\r\n")
        if split < 0:
            raise bad_request("A multipart part has no blank line after its headers.")
        head, data = raw[:split], raw[split + 4 :]
    headers: dict[str, str] = {}
    for line in head.split(b"\r\n"):
        if not line:
            continue
        name, sep, value = line.decode("utf-8", "replace").partition(":")
        if not sep:
            raise bad_request("A multipart part has a malformed header line.")
        headers[name.strip().lower()] = value.strip()
    return headers, data


def _disposition(name: str, filename: str | None) -> str:
    text = f'attachment; name="{name}"'
    if filename is None:
        return text
    if filename.isascii() and filename.isprintable():
        escaped = filename.replace("\\", "\\\\").replace('"', '\\"')
        return f'{text}; filename="{escaped}"'
    return f"{text}; filename*=UTF-8''{quote(filename, safe='')}"


def build_mixed(obj: dict, parts: list[Part]) -> tuple[bytes, str]:
    """An answer with parts: ``multipart/mixed``, the JSON first, then one part per content."""
    payload = json.dumps(obj, ensure_ascii=False, allow_nan=False).encode("utf-8")
    while True:
        boundary = "procedo-ref-" + secrets.token_hex(12)
        token = boundary.encode("ascii")
        if token not in payload and all(token not in part.data for part in parts):
            break
    chunks = [
        f"--{boundary}\r\n".encode(),
        f"Content-Disposition: {_disposition('json', None)}\r\n".encode(),
        b"Content-Type: application/json\r\n\r\n",
        payload,
        b"\r\n",
    ]
    for part in parts:
        chunks += [
            f"--{boundary}\r\n".encode(),
            f"Content-Disposition: {_disposition('sha256:' + part.sha, part.filename)}\r\n".encode(),
            f"Content-Type: {part.mimetype}\r\n\r\n".encode(),
            part.data,
            b"\r\n",
        ]
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/mixed; boundary={boundary}"
