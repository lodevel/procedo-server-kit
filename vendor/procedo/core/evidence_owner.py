"""The evidence folder's ownership marker — whose evidence a folder holds.

The path the user configures IS the project's evidence folder (2026-09-19), so the
folder's NAME says nothing about whose it is: two projects pointed at one folder
would interleave their boards. ``.evidence-project.json`` at the root of the folder
records the FULL ``project_id`` instead, and:

* a folder whose marker names a DIFFERENT project is **refused**, everywhere
  (:func:`check_owner`, which every path mapping and every write goes through), with
  its own error kind — ``foreign``, never the ``denied`` an ACL also raises;
* an **unmarked** folder is **adopted** and marked by the first write — a store
  written before there were markers has no other way to keep working, and nothing
  in it says otherwise — except by a store opened from an outbox entry
  (``requires_marker``), which writes only into a folder whose marker conclusively
  names the project: one an upload was queued for that says nothing now may have been
  left by a Move;
* only a marker read that CONCLUDES decides anything (:func:`marker_owner`), and a
  read-time proof never authorises a write: :func:`mark_owner` re-reads.

Carved out of :mod:`evidence_shared_layout` rather than living there: it is one
concern (who owns the folder), it is the thing every operation now proves, and the
layout had no room left. Functions over the store rather than methods on it, so the
outbox, the index and the conversion prove ownership through the same three calls
the layout does.

Qt-free, stdlib only; every store read and write goes through the store's tree.
"""

from __future__ import annotations

import json
import logging
from pathlib import PurePath

from .evidence_store import EvidenceError
from .evidence_store_fs import os_error
from .evidence_tree import TreeIO

log = logging.getLogger(__name__)

MARKER = ".evidence-project.json"  # the evidence folder's owner: the project's FULL id

#: The typed refusal this module raises, and the ONE thing it means: the folder belongs
#: to another project. Deliberately not ``denied``, which an ACL, an anti-virus lock or
#: a share that went away also raise — the GUI shuts a project's doors on this one and
#: keeps the non-fatal offline path for those.
FOREIGN = "foreign"


def foreign_folder(root: PurePath, owner: str, project_id: str) -> str:
    """Why this project may not use that evidence folder — both ids and the folder.

    The refusal names the project that got there first rather than merging behind
    somebody's back."""
    return (
        f"{root} is the evidence folder of project {owner}, not of project "
        f"{project_id}. Give this project a folder of its own, or — if that folder "
        f"really is this project's — remove its {MARKER} and open the project again."
    )


def marker_owner(io: TreeIO, root: PurePath) -> str | None:
    """Tri-state: the FULL ``project_id`` the folder's marker names, ``""`` only when
    the marker is conclusively ABSENT, and ``None`` when the read could not CONCLUDE.

    The three answers are three different decisions, and collapsing any two of them is
    a way to lose a folder. A single transient ``PermissionError`` read as "unmarked"
    made the store adopt the folder AND write its own id over the rightful owner's,
    which locked that owner out of its own evidence for good.

    **A marker that is THERE but says nothing usable is inconclusive, not absent** —
    a truncated write, an empty file, a JSON array, a ``project_id`` that is ``null``
    or a number. The file exists, so somebody put it there; what it should say is
    exactly what cannot be read, and a folder full of another project's evidence is
    the likeliest thing behind it. Claiming it would overwrite the only record of who
    that was. So the store neither claims nor refuses such a folder: it keeps reading
    and writing the evidence, and never marks. (The earlier rule — "unparseable reads
    as none and is rewritten" — was wrong for the same reason and is gone.) That is
    also why this reads the bytes itself rather than through
    :func:`evidence_tree.read_json`, which answers ``{}`` for an absent file and for a
    damaged one alike.

    An FTPS server without ``MLSD`` cannot tell an absent file from a refused one and
    answers ``None`` here as it does for every other read it cannot settle: such a
    store stays readable and writable, it simply never claims a folder it could not
    read — so two projects sharing one base path there are not caught, which is as far
    as the owner's "if someone sets the same result folder for two projects, he's an
    idiot; I don't mind a warning" goes.
    """
    path = root / MARKER
    try:
        raw = io.read_bytes(path)
    except FileNotFoundError:
        return ""  # proved ABSENT: the folder carries no marker, and may be adopted
    except (OSError, EvidenceError) as exc:
        log.info(
            "%s could not be read (%s): this folder's owner stays unknown, and the "
            "store neither claims nor refuses it",
            path,
            exc,
        )
        return None
    try:
        owner = json.loads(raw.decode("utf-8")).get("project_id")
    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
        owner = None
    if isinstance(owner, str) and owner:
        return owner
    log.warning(
        "%s is there but names no project: the folder's owner stays unknown, and "
        "the store neither claims nor refuses it",
        path,
    )
    return None


def check_owner(store, project_id: str) -> None:
    """Refuse a folder whose marker names ANOTHER project, naming both.

    Every path mapping goes through this (``_board_dir`` / ``_artifact_root``), so a
    read, a delete and a listing are refused exactly like a write — the marker is
    proved ONCE per store instance, so that costs one round trip for the life of the
    store, not one per mapping.

    What it proves is a READ: that this folder is not somebody else's right now. It is
    not permission to write the marker — the folder can be claimed a millisecond later
    — which is :func:`mark_owner`'s own re-read to establish.
    """
    if not project_id:
        raise EvidenceError("invalid", "the shared layout needs a project_id")
    if project_id in store._owned:
        return
    owner = marker_owner(store.io, store.root)
    if owner and owner != project_id:
        raise EvidenceError(FOREIGN, foreign_folder(store.root, owner, project_id))
    if owner != project_id and store.requires_marker:
        raise EvidenceError(
            "not_found", f"{store.root} does not say it holds project {project_id}'s evidence"
        )
    if owner is None:
        return  # inconclusive: claim nothing, refuse nothing, ask again next time
    store._owned.add(project_id)
    if owner:
        # It already names US: there is nothing to write, ever. Recording that here is
        # what keeps a write from re-reading a marker this store has already read.
        store._marked.add(project_id)


def mark_owner(store, project_id: str) -> None:
    """On the first write that puts evidence in the folder: record this project's FULL
    id there — and only over a marker that is STILL conclusively absent.

    The re-read inside the lock is the whole point. ``_owned`` is a READ-time proof and
    a cheap one: a store that listed an unmarked folder caches it and may not write for
    hours. If the rightful owner marks the folder in between — another window, another
    bench, the project's own first run — writing on the strength of that cached proof
    stamps this project's id over theirs, and the owner is refused from its own evidence
    from then on (reproduced from a bare ``list_boards``, and from a ``check_access``
    of a folder that did not exist yet). So the marker is read again here, under the
    store's own lock, and the answer decides:

    * still absent — write it, we are the first;
    * ours already — nothing to do, and never rewrite a file that already says this;
    * somebody else's — drop the stale ``_owned`` proof and refuse, so this store stops
      believing a folder is free the moment it learns it is not;
    * inconclusive — write nothing, claim nothing (:func:`marker_owner`).
    """
    check_owner(store, project_id)
    if project_id in store._marked or project_id not in store._owned:
        return  # already marked, or the read could not conclude: never write over it
    with store._mark_lock:
        if project_id in store._marked:
            return  # another thread of this process settled it while we waited
        owner = marker_owner(store.io, store.root)
        if owner is None:
            return  # no longer conclusive: claim nothing
        if owner:
            if owner != project_id:
                store._owned.discard(project_id)  # the read-time proof is stale: drop it
                raise EvidenceError(FOREIGN, foreign_folder(store.root, owner, project_id))
            store._marked.add(project_id)
            return  # already ours: the file says exactly this, so nothing is written
        try:
            store.io.write_json(
                store.root / MARKER,
                {"schema_version": 1, "project_id": project_id, "machine_id": store.machine_id},
                store.tag,
            )
        except OSError as exc:
            raise os_error(exc) from exc
        store._marked.add(project_id)


def mkdir_owned(store, project_id: str, target: PurePath) -> None:
    """``_mkdir_safe`` for a path about to hold this project's evidence: refuse a
    folder another project owns, create the folders, then mark the root once."""
    check_owner(store, project_id)
    store._mkdir_safe(target)
    mark_owner(store, project_id)
