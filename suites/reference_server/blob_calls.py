"""Large files on the ``api`` storage, one per request (data_format.md §3.4b, B1 and B2).

``POST /storages/{store_id}/blobs/{owner}/{sha256}`` (B1) takes a file's bytes as the whole
body, ``application/octet-stream``: ``saved``, or ``already`` when the owner holds them; a body
whose hash is not the path's is ``422 digest_mismatch``. ``GET`` on the same path (B2) answers
the bytes, honouring one ``Range`` so a broken download resumes where it stopped (``206``;
``416`` past the end; several ranges, or one this server cannot read, answer the whole file).

The owner is a ``project_id`` or ``library``, as in a storage's layout (§3.2): a blob is its
owner's, so B2 never reads another project's bytes, and B1 keeps the bytes where the write
that references them looks. Only an ``api`` storage: another is the app's to reach (``400``),
an unknown one ``404``. B1 is limited by ``limits.max_file_bytes``, never by
``max_upload_bytes`` (the JSON requests'). A file over 1 MiB never rides as a part: the HTTP
layer refuses such a part in a request and leaves it out of an answer, whose JSON still lists
the file.
"""

from __future__ import annotations

import hashlib

from . import faults
from .blobs import LIBRARY
from .call import Answer, Call
from .errors import ApiError, bad_request, forbidden, not_found
from .multipart import MAX_PART_BYTES
from .validate import PROJECT_ID, SHA_HEX

# Who may upload, by owner: whoever writes what references the file (data_format.md §5.2).
UPLOADERS = {
    "project": frozenset({"bench", "importer"}),
    LIBRARY: frozenset({"bench", "publisher"}),
}


def _blob(call: Call) -> tuple[int, str, str]:
    """The path's storage, owner and sha256 (bare hex), by their form alone: ``(store_id, owner,
    sha256)``. Their form is checked before the login's roles and the storage (§2.13)."""
    text = call.params["store_id"]
    if not text.isdigit():
        raise bad_request("store_id is an integer.")
    owner = call.params["owner"]
    if owner != LIBRARY and PROJECT_ID.fullmatch(owner) is None:
        raise bad_request("A blob's owner is a project_id (a lower-case UUID) or library.")
    sha = call.params["sha256"]
    if call.state.broken("B1.bare-sha-400"):
        sha = sha.removeprefix("sha256:")
    if SHA_HEX.fullmatch(sha) is None:
        raise bad_request("A blob is named by its sha256, 64 lower-case hex digits, bare.")
    return int(text), owner, sha


def _api_storage(call: Call, store_id: int) -> None:
    """The path's storage is held (else ``404``) and is the server's own (``api``, else
    ``400``: the app reaches another directly, §3.2)."""
    storage = call.state.storage_of(store_id)
    if storage is None:
        raise not_found(f"No storage {store_id}.")
    if storage["backend"] != "api" and not call.state.broken("B1.api-storage-only"):
        raise bad_request(
            f"Storage {store_id} is not the server's own (api): the app reaches it directly (§3.2)."
        )


def upload(call: Call) -> Answer:
    """B1: the bytes of one file, kept with their owner, held or not yet (a new project's file
    goes before its P11, a first run's before its E1). Its ``application/octet-stream`` is
    checked with the request's form (``server._parse_body``)."""
    store_id, owner, sha = _blob(call)
    allowed = UPLOADERS[LIBRARY if owner == LIBRARY else "project"]
    if not call.user.roles & allowed and not call.state.broken("B1.roles-403"):
        needs = " or ".join(sorted(allowed))
        raise forbidden(f"An upload for this owner needs the {needs} role.")
    _api_storage(call, store_id)
    if owner != LIBRARY and call.state.broken("B1.owner-before-project"):
        if owner not in call.state.projects:
            raise not_found("This server holds nothing for this project yet.")
    data = call.raw_body
    if hashlib.sha256(data).hexdigest() != sha and not call.state.broken("B1.digest-422"):
        raise ApiError(
            422,
            "digest_mismatch",
            "corrupt",
            "The bytes sent do not match the sha256 the path names.",
            details={"part": f"sha256:{sha}"},
        )
    held = call.state.blobs.check(owner, sha, len(data)) is None
    if not held:  # absent, or damaged: the bytes sent replace it (§3.5)
        call.state.blobs.put(owner, sha, data)
    status = "already" if held and not call.state.broken("B1.already") else "saved"
    return Answer({"status": status, "version": f"sha256:{sha}", "size": len(data)})


def _range(header: str | None, size: int, broken: bool = False) -> tuple[int, int] | None:
    """One byte range ``(first, last)`` of ``Range: bytes=…``; None to send it all (several
    ranges, or a header this server does not read). Past the end: ``416``."""
    if header is None:
        return None
    unit, _, spec = header.partition("=")
    first, dash, last = spec.strip().partition("-")
    if unit.strip().lower() != "bytes" or "," in spec or not dash:
        return None
    if not (first.isdigit() or first == "") or not (last.isdigit() or last == ""):
        return None
    if first == "" and last == "":
        return None
    if first == "":
        start, end = max(size - int(last), 0), size - 1
        satisfiable = int(last) > 0 and size > 0
    else:
        start = int(first)
        end = min(int(last), size - 1) if last else size - 1
        satisfiable = start < size and (last == "" or int(last) >= start)
    if not satisfiable and faults.on("4.3.416"):
        return None
    if not satisfiable:
        raise ApiError(
            416,
            "range_not_satisfiable",
            "corrupt",
            f"The file holds {size} bytes: that range is past its end.",
            headers={} if broken else {"Content-Range": f"bytes */{size}"},
        )
    return start, end


def download(call: Call) -> Answer:
    """B2: the bytes of one file its owner holds, whole or from a byte on."""
    store_id, owner, sha = _blob(call)
    _api_storage(call, store_id)
    state = call.state
    data, _status = state.blobs.read(owner, sha)
    if data is None and state.broken("B2.owner-404") and state.blobs.owner_holding(sha):
        data, _status = state.blobs.read(state.blobs.owner_holding(sha) or owner, sha)
    if data is not None and len(data) <= MAX_PART_BYTES and state.broken("B2.any-size"):
        data = None
    if data is None:
        raise not_found(f"This storage holds no sha256:{sha} for {owner}.")
    headers = {"Accept-Ranges": "bytes"}
    asked = None if state.broken("B2.range") else call.headers.get("range")
    span = _range(asked, len(data), state.broken("4.3.416"))
    if span is None:
        return Answer({}, raw=data, headers=headers)
    start, end = span
    headers["Content-Range"] = f"bytes {start}-{end}/{len(data)}"
    return Answer({}, raw=data[start : end + 1], status=206, headers=headers)
