"""This PC's copy of what the JSON API answered: a run's folder, an artifact beside its record.

Consumers hand paths to ``open()`` and ``QUrl.fromLocalFile``, so a run or an artifact read
from the server is placed in the per-machine cache (:mod:`evidence_remote_cache`'s), keyed by
the server's identity and the project:

* **A run** is a folder of its own, ``runs/<run_id>``: a run's id is unique in its project and
  its files never change, so a rename of its test moves nothing here. Its files come as the
  parts of the read (E3); the folder keeps the run's manifest (:func:`evidence_api_run.manifest_of`)
  and names in ``.missing.json`` what the server could not give (:mod:`evidence_missing`): a
  missing picture never hides its run. A read states what the folder already holds
  (:func:`held`, the read's ``have``), so its bytes travel once, and a file the folder lacks
  comes with the next read that the server can give it in (a file put back on its storage).
* **An artifact** is its file (a draft: its content) with its record written beside it as its
  sidecar, each in the bytes the app itself writes them (:data:`RECORD_BYTES`): the API keeps
  a record and a draft as JSON objects, not as files. A report the server keeps without its
  page (:func:`without_page`) is its record alone.

Qt-free.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import atomic_write
from .evidence_api_run import manifest_of
from .evidence_missing import DAMAGED, MISSING, NOT_FOUND, UNREACHABLE
from .evidence_remote_cache import CACHE_MARK, record_missing
from .evidence_shared_manifest import MANIFEST
from .evidence_store import EvidenceError, no_page
from .evidence_store_fs import _read_json, _sidecars
from .evidence_tree import safe_relative
from .paths import rmtree
from .report_export import sidecar_bytes, text_file_bytes

#: A record or a draft in the bytes the app writes it: a report's sidecar
#: (``run_report._report_bytes``), an export's (``report_export.sidecar_bytes``) and a draft
#: (``result_annotations.save_global_draft``). Store then read back gives those bytes back.
RECORD_BYTES: dict[str, Callable[[Any], bytes]] = {
    "report": lambda data: text_file_bytes(json.dumps(data, indent=2)),
    "export": sidecar_bytes,
    "draft": lambda data: text_file_bytes(json.dumps(data, indent=2)),
}


def held(local: Path) -> list[str]:
    """The sha256 of each file a cached run folder holds: what its next read need not
    bring. Only files the folder's manifest names and that are there whole."""
    if not local.is_dir():
        return []
    found = []
    for entry in _read_json(local / MANIFEST).get("files") or []:
        try:
            path = local.joinpath(*safe_relative(str(entry.get("name", "")), "posix"))
            if path.stat().st_size == entry.get("size"):
                found.append(str(entry.get("sha256")))
        except (EvidenceError, OSError):
            continue
    return found


def place_run(local: Path, run: dict[str, Any], parts: dict[str, bytes]) -> None:
    """The run's folder at ``local``, every file there or named as missing: from the read's
    parts, or kept from the folder as it was (what the read said it held). Built beside it
    and moved into place in one step: a concurrent reader keeps the copy that won. A folder
    that lacks a file is built again only when the read brings one, or the server did not
    answer for one last time: every read asks the server anyway, so nothing more is fetched."""
    if (local / CACHE_MARK).is_file():
        return  # complete: its files never change
    if local.is_dir() and not _brings_more(local, run, parts):
        return
    staging = local.parent / f".{local.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    try:
        staging.mkdir(parents=True)
        missing = {}
        told = run.get("missing") if isinstance(run.get("missing"), dict) else {}
        for f in run.get("files") or []:
            name = str(f.get("name", ""))
            why = _place_file(staging, local, f, parts, told.get(name))
            if why:
                missing[name] = why
        atomic_write.write_json(staging / MANIFEST, manifest_of(run))
        record_missing(str(run.get("run_id", "")), staging, missing)
        if local.is_dir():
            rmtree(local, ignore_errors=True)  # an earlier, incomplete copy: this one replaces it
        try:
            staging.rename(local)
        except OSError:
            if not local.is_dir():
                raise  # not a lost race, a real failure
    finally:
        rmtree(staging, ignore_errors=True)  # only ever this call's own folder


def _brings_more(local: Path, run: dict[str, Any], parts: dict[str, bytes]) -> bool:
    """Whether a read of a folder lacking files may place one: it carries the right bytes of
    a file the folder lacks, or a file the server did not answer for is asked again."""
    recorded = _read_json(local / MISSING)
    if UNREACHABLE in recorded.values():
        return True
    for f in run.get("files") or []:
        sha256 = str(f.get("sha256", ""))
        data = parts.get(f"sha256:{sha256}")
        if str(f.get("name", "")) in recorded and data is not None and _whole(data, f):
            return True
    return False


def _whole(data: bytes, f: dict) -> bool:
    """Whether *data* are the bytes the run's file *f* records (its size and sha256)."""
    return len(data) == f.get("size") and hashlib.sha256(data).hexdigest() == f.get("sha256")


def _place_file(staging: Path, local: Path, f: dict, parts: dict[str, bytes], told: Any) -> str:
    """One file of the run into ``staging``; why it is not there, or ``""``. A part whose
    bytes are not the file's is ``damaged`` and never served; a file of a storage this app
    does not reach yet, or not answered, is ``unreachable`` and asked again next time."""
    try:
        parts_of = safe_relative(str(f.get("name", "")), "posix")
    except EvidenceError:
        return DAMAGED  # a name no file can be placed under
    target = staging.joinpath(*parts_of)
    target.parent.mkdir(parents=True, exist_ok=True)
    sha256, size = str(f.get("sha256", "")), f.get("size")
    data = parts.get(f"sha256:{sha256}")
    if data is not None:
        if not _whole(data, f):
            return DAMAGED
        target.write_bytes(data)
        return ""
    kept = local.joinpath(*parts_of)
    if kept.is_file() and kept.stat().st_size == size:
        shutil.copyfile(kept, target)  # what the read said this copy held: kept, not fetched
        return ""
    if told in (NOT_FOUND, DAMAGED):
        return str(told)
    return UNREACHABLE


def without_page(kind: str, artifact: dict[str, Any]) -> bool:
    """Whether the server keeps the report *artifact* without its page (contract §5.10): it
    answers no ``store_id``, as a draft, and the app draws the page again from its record."""
    return kind == "report" and "store_id" not in artifact


def page_bytes(local: Path, name: str) -> bytes:
    """The cached file at *local*; a report kept without its page is :func:`no_page`."""
    try:
        return local.read_bytes()
    except FileNotFoundError:
        raise no_page(name) from None


def place_artifact(local: Path, kind: str, data: bytes | None, artifact: dict[str, Any]) -> None:
    """The *artifact* E9 answered: its file, and its record as its sidecar (a report:
    ``<stem>.json``; an export: ``<name>.meta.json``); ``data`` ``None`` keeps the file there
    (the read said it held it), and none stays for a report kept without its page. A draft is
    its content, in the app's bytes."""
    local.parent.mkdir(parents=True, exist_ok=True)
    if kind == "draft":
        atomic_write.write_bytes(local, RECORD_BYTES["draft"](artifact.get("content")))
        return
    record = artifact.get("info")
    if without_page(kind, artifact):
        local.unlink(missing_ok=True)
    elif data is not None:
        atomic_write.write_bytes(local, data)
    sidecar = _sidecars(kind, local)[0]
    if isinstance(record, dict):
        atomic_write.write_bytes(sidecar, RECORD_BYTES[kind](record))
    else:
        sidecar.unlink(missing_ok=True)


def forget_artifact(local: Path, kind: str) -> None:
    """The server holds no such artifact: nothing of it is left to be opened."""
    for path in (local, *_sidecars(kind, local)[:1]):
        path.unlink(missing_ok=True)


def sha256_of(path: Path) -> str | None:
    """A cached file's sha256 (what an artifact's read need not bring again); ``None``
    when it is not there."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None
