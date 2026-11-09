"""The pure decision of a project-sync pass: what must move, and which way.

One function, :func:`plan`, no I/O. It takes the local scan, the server listing and
the manifest of the last sync (:mod:`project_origin`) and answers with the actions
that reconcile them — the table in ``docs/plans/project_sync_design.md``, row by
row:

============== ================== =========================================
local vs row   server vs row      action
============== ================== =========================================
same           same               nothing
same           changed            download
changed        same               upload
changed        changed            **conflict**: edited / edited
same           gone               delete locally
gone           same               delete on the server
changed        gone               **conflict**: edited / deleted
gone           changed            **conflict**: deleted / edited
gone           gone               nothing; the row is dropped
============== ================== =========================================

and, where there is no row at all, ``new``/``absent``: one side alone uploads or
downloads, and two unknown copies of one name are a conflict too (new / new). A row
covers BOTH sides, so "new" on one side and "same" on the other cannot occur.

**A test and the bundle are one unit** (:func:`unit_of`). Their files only make sense
together: one PC's ``procedure.json`` beside another's ``test.py`` is a test nobody
wrote, and two bundles' wheels beside one ``bundle.json`` a bundle nobody installed. So
when a unit has a change on this side AND one on the server, in any of its files, every
file of it that changed on either side is a conflict, answered together; a unit changed
on one side only moves as the table says.

**A release never changes** (``test_release``: ``releases/<test_id>/v<N>.json``, written once on
the share). One edited in the working copy is never uploaded over the share's: it is a
conflict whose only answer is the share's copy (``project_sync_conflicts.resolve_one``),
the edited one going to this PC's trash.

**Nothing is decided by age** (docs/plans/project_working_copy_design.md,
"Conflicts"): a file changed on both sides, or changed on one and deleted on the
other, is a :class:`Conflict`. Nothing acts on it: both copies stay as they are, the
row stays, and the next pass meets the same question until somebody answers it
(``project_sync.resolve``). Two copies holding the same bytes are no conflict; that
takes reading them, so the pass settles it, not this function. No clock is read,
so none has to be compared with another machine's.

**A push does not list the server.** It hands this function the manifest AS the
server listing: every server side then reads "same", and the only actions that can
come out are the uploads and the server-side deletes the local folder asks for.
Each of them looks its own file up on the server before it acts
(:mod:`project_sync_transfer`), so a push is not blind. One planner, two callers, no
second mode.

**Exclusions** (:func:`project_never_travels.exclusion`) — what is in a project folder but
is not the project — are the list of :mod:`project_never_travels`; the planner never plans a
path it names.

**Path rules** (:func:`unsafe`) — server paths are POSIX and local ones are
Windows, so the two are matched on a case-folded relative key, and a server name
that cannot exist on Windows, escapes the root, or differs from another only in
case is REFUSED and reported. It is never silently repaired and two names are
never merged into one file.

Qt-free, stdlib only, no I/O.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from .evidence_store import EvidenceError, safe_component
from .evidence_tree import name_key
from .project_never_travels import (  # noqa: F401  (bench_state: Contract C, Studio reads it)
    bench_state,
    exclusion,
)
from .project_origin import Conflict, Row, Stamp

DOWNLOAD = "download"
UPLOAD = "upload"
DELETE_LOCAL = "delete_local"
DELETE_REMOTE = "delete_remote"


#: What Windows refuses in a path component on top of what ``safe_component``
#: already rules out (separators, control characters, reserved device names, a
#: trailing dot or space). A POSIX server is free to serve all of them.
_WINDOWS_ILLEGAL = re.compile(r'[<>:"|?*]')
#: Windows' classic limits. Long-path support is opt-in per machine, so a server
#: name that would produce a path at or over ``MAX_LOCAL_PATH`` characters is
#: refused ONCE and reported — the alternative is a pass that dies at ``open()``
#: on the same file every time, which is how one bad name wedges a project for good.
MAX_LOCAL_PATH = 259
MAX_COMPONENT = 255


@dataclass(frozen=True)
class Action:
    """One file to move or remove. ``path`` is relative and POSIX-spelled."""

    kind: str
    path: str
    reason: str


@dataclass(frozen=True)
class Refusal:
    """A name that is not synced, and why — reported, never silently dropped."""

    path: str
    side: str  # "local" | "server"
    why: str


#: The kinds of a :class:`project_origin.Conflict`: what happened here / on the server.
EDITED_EDITED = "edited/edited"
NEW_NEW = "new/new"
EDITED_DELETED = "edited/deleted"
DELETED_EDITED = "deleted/edited"


@dataclass(frozen=True)
class Plan:
    """What a pass will do, what it refuses, and what it leaves for somebody to decide."""

    actions: tuple[Action, ...] = ()
    refused: tuple[Refusal, ...] = ()
    conflicts: tuple[Conflict, ...] = ()


#: A file this large is downloaded after the project has opened, not before
#: (``docs/plans/project_working_copy_design.md``, "ODB++ archives", owner's Q4).
DEFERRED_SIZE = 8 << 20


def deferred(path: str, size: int, odb_archive: str = "") -> bool:
    """Whether *path* (``size`` bytes on the server) waits for the background download a
    first open makes after the project is shown: an archive at the project root
    (``*.tgz``), the board archive ``config.json`` names (*odb_archive*, relative), anything
    under ``documents/``, and any file over :data:`DEFERRED_SIZE`."""
    parts = path.split("/")
    return (
        (len(parts) == 1 and path.lower().endswith(".tgz"))
        or (bool(odb_archive) and path.casefold() == odb_archive.casefold())
        or parts[0].casefold() == "documents"
        or size > DEFERRED_SIZE
    )


def unsafe(path: str, budget: int | None = None) -> str:
    """Why this relative path may not be synced at all (``""`` when it may).

    *budget* is how many characters are left for it below the working copy
    (``MAX_LOCAL_PATH`` minus the root and its separator); ``None`` does not check,
    which is what a caller with no root in hand — a plain planner test — passes.

    Refused rather than repaired: a server name this machine cannot create is a
    name the two sides would never agree on again, and renaming it here would make
    the working copy quietly disagree with the server for good.

    The components are the POSIX ones — the path was built by joining a listing's
    names with ``/`` — so they are split HERE and not by a Windows path object: a
    server name holding a backslash is ONE name that Windows refuses, and letting
    ``PureWindowsPath`` split it into two would turn it into a folder. That split
    is also what refuses an ESCAPE, by the component rules rather than by a path
    object's anchor: ``..`` and ``.`` are not names ``guard_component`` allows,
    ``/absolute`` and ``//server/share`` put an empty component first, and a drive
    letter carries the colon Windows refuses. (An ``evidence_tree.safe_relative``
    call on top of this was measurably dead: no name reaches it that the loop has
    not already refused.)
    """
    if not path or path != path.strip():
        return f"{path!r} is not a usable file name"
    try:
        for part in path.split("/"):
            safe_component(part)
            if _WINDOWS_ILLEGAL.search(part):
                return f"{part!r} holds a character Windows refuses in a file name"
            if len(part) > MAX_COMPONENT:
                return f"{part[:40]!r}… is longer than a file name may be"
    except EvidenceError as exc:
        return str(exc)
    if budget is not None and len(path) > budget:
        return (
            f"the path is {len(path)} characters and only {budget} are left below the "
            f"working copy before the {MAX_LOCAL_PATH}-character limit"
        )
    return ""


# ---------------------------------------------------------------------------
# The planner
# ---------------------------------------------------------------------------


def _index(
    entries: Mapping[str, Stamp], side: str, refused: list[Refusal], budget: int | None = None
) -> dict[str, tuple[str, Stamp]]:
    """Name key -> (the spelling, the stamp), minus what is not synced. The key is the path's
    NFC form, case folded (:func:`evidence_tree.name_key`): one name typed two ways, or
    spelled by two systems in two Unicode forms, is one file (``data_format.md`` §2).

    Two server names with one key are a collision NTFS cannot hold: both are dropped and
    reported. Merging them would give one local file whose content flips between two server
    files at every pass.
    """
    out: dict[str, tuple[str, Stamp]] = {}
    collided: set[str] = set()
    for path in sorted(entries):
        why = exclusion(path) or unsafe(path, budget)
        if why:
            refused.append(Refusal(path, side, why))
            continue
        key = name_key(path)
        if key in out:
            collided.add(key)
            refused.append(
                Refusal(
                    path,
                    side,
                    f"{path!r} and {out[key][0]!r} differ only in case or in their Unicode "
                    "form, which one Windows folder cannot hold; neither is synced",
                )
            )
            continue
        out[key] = (path, entries[path])
    for key in collided:
        out.pop(key, None)
    return out


def _rows(manifest: Mapping[str, Row]) -> dict[str, tuple[str, Row]]:
    """The manifest under the same key. Rows for paths no longer synced are dropped
    silently — the exclusion list changing is not a refusal to report at the user."""
    return {
        name_key(path): (path, row)
        for path, row in sorted(manifest.items())
        if not (exclusion(path) or unsafe(path))
    }


def _state(now: Stamp | None, was: Stamp | None) -> str:
    """How one side compares with what the manifest recorded for it.

    The WHOLE stamp decides it, never a chosen pair of fields: the web server's stamps
    carry a version (the ``sha256:`` of the bytes) and no time, and a moved version is
    "changed" whatever the size says.
    """
    if now is None:
        return "gone" if was is not None else "absent"
    if was is None:
        return "new"
    return "same" if now == was else "changed"


#: (local state, server state) -> (action kind, why). ``None`` = nothing to do.
#: The rows of :data:`_CONFLICTS` are not here, and ``("absent", "absent")`` cannot
#: occur — a key is in the plan because one of the three inputs holds it.
_TABLE: dict[tuple[str, str], tuple[str | None, str]] = {
    ("new", "absent"): (UPLOAD, "new in the working copy"),
    ("absent", "new"): (DOWNLOAD, "new on the server"),
    ("same", "same"): (None, ""),
    ("same", "changed"): (DOWNLOAD, "changed on the server"),
    ("same", "gone"): (DELETE_LOCAL, "deleted on the server"),
    ("changed", "same"): (UPLOAD, "changed in the working copy"),
    ("gone", "same"): (DELETE_REMOTE, "deleted in the working copy"),
    ("gone", "gone"): (None, "deleted on both sides"),
}
#: The kinds of the other files of a unit changed on both sides (:func:`unit_of`): what happened
#: to THIS file here / on the server, e.g. ``edited/unchanged``.
_WORDS = {
    "new": "new",
    "changed": "edited",
    "gone": "deleted",
    "same": "unchanged",
    "absent": "absent",
}

#: A state that is a change on its side.
_MOVED = frozenset({"new", "changed", "gone"})


def is_release(path: str) -> bool:
    """A test's release file (``test_release``): written once, never replaced."""
    parts = path.split("/")
    return len(parts) == 3 and parts[0] == "releases" and parts[2].endswith(".json")


def unit_of(path: str) -> str:
    """The unit a file is synced as: ``tests/<id>`` for a test's file, ``bundle`` for the
    project's bundle, ``""`` for a file on its own."""
    parts = path.split("/")
    if len(parts) > 2 and parts[0] == "tests":
        return "/".join(parts[:2])
    if len(parts) > 1 and parts[0] == "bundle":
        return "bundle"
    return ""


#: (local state, server state) -> the :class:`Conflict` kind it is.
_CONFLICTS: dict[tuple[str, str], str] = {
    ("changed", "changed"): EDITED_EDITED,
    ("new", "new"): NEW_NEW,
    ("changed", "gone"): EDITED_DELETED,
    ("gone", "changed"): DELETED_EDITED,
}


def plan(
    local_scan: Mapping[str, Stamp],
    server_listing: Mapping[str, Stamp],
    manifest: Mapping[str, Row],
    *,
    path_budget: int | None = None,
) -> Plan:
    """What reconciles the working copy and the server. Pure: no I/O, no clock.

    All three inputs are keyed by a relative POSIX path as their own side spells
    it; matching is by NFC and folded case (:func:`_index`), because one Windows folder
    cannot hold two names that differ only in case. ``path_budget`` is how long a relative path may be below
    this working copy (:func:`unsafe`).
    """
    refused: list[Refusal] = []
    conflicts: list[Conflict] = []
    local = _index(local_scan, "local", refused, path_budget)
    server = _index(server_listing, "server", refused, path_budget)
    rows = _rows(manifest)

    seen = []
    for key in sorted(set(local) | set(server) | set(rows)):
        here = local.get(key)
        there = server.get(key)
        row = rows.get(key)
        # The server's spelling wins where it has one: an upload under the local
        # spelling would make a SECOND file on a case-sensitive server.
        path = (there or row or here)[0]
        states = (
            _state(here[1] if here else None, row[1].local if row else None),
            _state(there[1] if there else None, row[1].server if row else None),
        )
        seen.append((path, states, here[1] if here else None, there[1] if there else None))

    changed_here = {unit_of(path) for path, states, *_ in seen if states[0] in _MOVED}
    changed_there = {unit_of(path) for path, states, *_ in seen if states[1] in _MOVED}
    contested = (changed_here & changed_there) - {""}

    actions: list[Action] = []
    for path, states, here_stamp, there_stamp in seen:
        if states in _CONFLICTS:
            conflicts.append(Conflict(path, _CONFLICTS[states], here_stamp, there_stamp))
            continue
        kind, why = _TABLE[states]
        if kind is None:
            continue
        if unit_of(path) in contested or (
            kind == UPLOAD and states[1] == "same" and is_release(path)
        ):
            conflicts.append(
                Conflict(path, f"{_WORDS[states[0]]}/{_WORDS[states[1]]}", here_stamp, there_stamp)
            )
            continue
        actions.append(Action(kind, path, why))
    return Plan(tuple(actions), tuple(refused), tuple(conflicts))
