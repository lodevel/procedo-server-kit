"""A project's releases on a tree: the release files as :class:`project_store.ReleaseRecord`.

A folder storage keeps a test's releases as files, ``releases/<test_id>/vN.json``, written once,
filed under the test's hidden id (``test_format.TEST_ID``), never under its name
(``data_format.md`` §4.4): a rename moves none, a deleted test's stay where they are, and a new
test, a new id, starts at v1. The JSON API keeps them as objects. This module is the one bridge:
it reads the files of a project tree as records, writes records as those files (a working copy
of a project on the API writes its ``releases/`` from the records it is given, contract question
P22) as ``test_release`` writes and reads them, and numbers a release on a tree as the API's
server numbers its own.

The tree is any :class:`evidence_tree.TreeIO` (a share, FTPS; this PC's own folder
through ``FsTree``); *base* is the project's folder on it. Qt-free.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from pathlib import PurePath

from . import test_format, test_release
from .evidence_store import EvidenceError
from .evidence_tree import TreeIO, make_dirs
from .project_store import RELEASES_DIR, ReleaseRecord

log = logging.getLogger(__name__)

#: The writer tag of the files written here.
TAG = "release"


def _folder(base: PurePath, test_id: str) -> PurePath:
    """``releases/<test_id>`` (the folder may not be there). A *test_id* that is not a test's
    hidden id (a name) is refused: releases are never filed by name."""
    if not test_format.is_test_id(test_id):
        raise EvidenceError("invalid", f"{test_id!r} is not a test id")
    return base / RELEASES_DIR / test_id


def _names(tree: TreeIO, folder: PurePath) -> list[str]:
    if not tree.is_dir(folder):
        return []
    return tree.list_names(folder) or []


def _record(data: dict, test_id: str, documents: bool) -> ReleaseRecord:
    if data["test_id"] != test_id:
        raise ValueError(f"it is filed under {test_id} but records {data['test_id']!r}")
    docs = data["documents"]
    return ReleaseRecord(
        test_id=test_id,
        test=str(data["test"]),
        version=int(data["version"]),
        format=str(data["format"]),
        fingerprint=str(data["fingerprint"]),
        content=str(data["content"]),
        documents={name: test_release.decoded(value) for name, value in docs.items()}
        if documents
        else None,
        note=str(data.get("note", "")),
        released_at=str(data.get("released_at", "")),
        released_by=str(data.get("released_by", "")),
        pc=str(data.get("pc", "")),
        machine_id=str(data.get("machine_id", "")),
        numbered_before_releases=bool(data.get("numbered_before_releases", False)),
    )


def records(
    tree: TreeIO, base: PurePath, test_id: str, *, documents: bool = True
) -> list[ReleaseRecord]:
    """The releases filed under *test_id*, oldest first. A file that is not a release (changed
    by hand, not JSON, another id than its folder's) is logged and left out: it is never
    trusted."""
    folder = _folder(base, test_id)
    out = []
    for number in sorted(
        n for n in map(test_release.release_number, _names(tree, folder)) if n is not None
    ):
        try:
            data = json.loads(tree.read_bytes(folder / f"v{number}.json").decode("utf-8"))
            out.append(_record(data, test_id, documents))
        except (UnicodeDecodeError, ValueError, KeyError, TypeError, AttributeError) as exc:
            log.warning("%s/v%s.json is not a release: %s", folder, number, exc)
    return out


def all_records(tree: TreeIO, base: PurePath) -> dict[str, list[ReleaseRecord]]:
    """Every test id's releases with their documents, by the id (a deleted test's included).
    A folder under ``releases/`` whose name is not a test id holds no release."""
    found = {}
    for name in sorted(_names(tree, base / RELEASES_DIR)):
        if test_format.is_test_id(name):
            held = records(tree, base, name)
            if held:
                found[name] = held
    return found


def file_bytes(record: ReleaseRecord) -> bytes:
    """The release file of *record*, as ``test_release`` writes one."""
    data = {
        "test": record.test,
        "test_id": record.test_id,
        "version": record.version,
        "format": record.format,
        "released_at": record.released_at,
        "released_by": record.released_by,
        "pc": record.pc,
        "machine_id": record.machine_id,
        "note": record.note,
        "fingerprint": record.fingerprint,
        "content": record.content,
        "documents": {
            name: test_release.encoded(record.documents[name])
            for name in sorted(record.documents or {})
        },
    }
    if record.numbered_before_releases:
        data["numbered_before_releases"] = True
    return test_release.record_bytes(data)


def next_number(tree: TreeIO, base: PurePath, test_id: str) -> int:
    """One more than every number filed under *test_id*."""
    names = _names(tree, _folder(base, test_id))
    return max((test_release.release_number(n) or 0 for n in names), default=0) + 1


def create(tree: TreeIO, base: PurePath, record: ReleaseRecord) -> None:
    """Write *record*'s file under its ``test_id``, never over one there: a number taken
    meanwhile refuses. A tree has no create-only write, so the look and the write are two steps
    apart; the API's server numbers its releases itself."""
    folder = _folder(base, record.test_id)
    make_dirs(tree, base, folder)
    path = folder / f"v{record.version}.json"
    if tree.exists(path):
        raise EvidenceError(
            "invalid", f"{record.test} has a release v{record.version} already", code="changed"
        )
    tree.write_atomic(path, file_bytes(record), TAG)


def write(
    tree: TreeIO, base: PurePath, releases: Mapping[str, Sequence[ReleaseRecord]]
) -> list[str]:
    """Each test id's *releases* as its files under ``releases/<test_id>/`` (P22): a release file
    not there yet is written, one there is never replaced. A record filed under another id than
    its own is refused (``corrupt``), that id's others with it. The paths written, relative to
    *base*."""
    written = []
    for test_id, held in releases.items():
        folder = _folder(base, test_id)
        stray = [r.version for r in held if r.test_id != test_id]
        if stray:
            raise EvidenceError(
                "corrupt", f"The releases listed under {test_id} hold others' too: {stray}"
            )
        there = set(_names(tree, folder))
        if held:
            make_dirs(tree, base, folder)
        for record in held:
            name = f"v{record.version}.json"
            if name not in there:
                tree.write_atomic(folder / name, file_bytes(record), TAG)
                written.append(f"{RELEASES_DIR}/{test_id}/{name}")
    return written
