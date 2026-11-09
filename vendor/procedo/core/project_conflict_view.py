"""What the conflict dialog and Compare show (docs/plans/project_working_copy_design.md,
"Conflicts", decision 2; the approved mock-up's *Changed on this PC and on the share* and
*Compare*). Qt-free: built on the sync's worker, shown on the GUI thread.

Conflicts are ASKED per unit (``project_sync_plan.unit_of``): every file under ``tests/<id>/`` is
one row, the project's ``bundle/`` is one row, answered together; a file elsewhere is a row of its
own. A unit changed on both sides holds every file that changed on either side, so a file changed
on one side only reads ``edited / unchanged``. Each side says when it last changed and,
for a test, its version as that side's files are against its releases (``v4``, ``Draft (from v4)``,
``Draft``: :mod:`conflict_versions`); a side that deleted it says so.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from .project_origin import Conflict, Stamp
from .project_sync_plan import DELETED_EDITED, EDITED_DELETED, EDITED_EDITED, NEW_NEW, unit_of

#: What happened, as the dialog's column says it.
HAPPENED = {
    EDITED_EDITED: "edited / edited",
    NEW_NEW: "new / new",
    EDITED_DELETED: "edited / deleted",
    DELETED_EDITED: "deleted / edited",
}

#: Files Compare shows as text; anything else is compared by its time and size.
TEXT_SUFFIXES = frozenset(
    {".md", ".json", ".py", ".txt", ".csv", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".xml"}
)


@dataclass(frozen=True)
class Item:
    """One row: a test (all its files in conflict), or one file outside ``tests/``."""

    title: str
    detail: str
    paths: tuple[str, ...]
    here: str
    there: str
    happened: str
    #: Every file of the row can be read as text: Compare shows it.
    text: bool
    #: The test's folder (``""`` for a file outside ``tests/``).
    test: str = ""


@dataclass(frozen=True)
class Pair:
    """One file as Compare shows it: this PC's text and the share's (``None``: not there)."""

    path: str
    mine: str | None
    theirs: str | None


def group_of(path: str) -> str:
    """The row a conflicted file belongs to: its unit (``tests/<id>``, ``bundle``), else itself."""
    return unit_of(path) or path


def when(mtime: float, now: float | None = None) -> str:
    """``today 14:02``, ``yesterday 09:15``, ``3 Nov 14:02``."""
    now = time.time() if now is None else now
    then, today = time.localtime(mtime), time.localtime(now)
    clock = time.strftime("%H:%M", then)
    days = (
        time.mktime(today[:3] + (0, 0, 0, 0, 0, -1)) - time.mktime(then[:3] + (0, 0, 0, 0, 0, -1))
    ) // 86400
    if days == 0:
        return f"today {clock}"
    if days == 1:
        return f"yesterday {clock}"
    return f"{then.tm_mday} {time.strftime('%b', then)} {clock}"


def _size(size: int) -> str:
    return f"{size} B" if size < 1024 else f"{round(size / 1024)} KB"


def _side(stamps: list[Stamp | None], version: str | None, *, file: bool, now: float) -> str:
    present = [s for s in stamps if s is not None]
    if not present:
        return "deleted"
    newest = max(present, key=lambda s: s.mtime)
    label = when(newest.mtime, now)
    if version:
        return f"{label} · {version}"
    if file:
        return f"{label} · {_size(newest.size)}"
    return label


def items(
    conflicts: Iterable[Conflict],
    *,
    name_of: Callable[[str], str] = lambda test: test,
    version_here: Callable[[str], str | None] = lambda test: None,
    version_there: Callable[[str], str | None] = lambda test: None,
    now: float | None = None,
) -> list[Item]:
    """The rows for *conflicts*, tests first then files, each in path order. *name_of* a test's
    folder → the name the list shows; *version_here* / *version_there* its version on each side
    (``v4``, ``Draft (from v4)``; None: not told)."""
    now = time.time() if now is None else now
    groups: dict[str, list[Conflict]] = {}
    for conflict in sorted(conflicts, key=lambda c: c.path):
        groups.setdefault(group_of(conflict.path), []).append(conflict)
    rows = []
    for key, members in sorted(
        groups.items(), key=lambda kv: (not kv[0].startswith("tests/"), kv[0])
    ):
        is_test = key.startswith("tests/") and key != members[0].path
        is_bundle = key == "bundle" and key != members[0].path
        test = key.split("/", 1)[1] if is_test else ""
        happened = ", ".join(
            dict.fromkeys(HAPPENED.get(c.kind, c.kind.replace("/", " / ")) for c in members)
        )
        count = len(members)
        files = f"{count} file{'' if count == 1 else 's'}"
        rows.append(
            Item(
                title=name_of(test) if is_test else "Bundle" if is_bundle else key,
                detail=(
                    f"{files} of the test"
                    if is_test
                    else f"{files} of the project's bundle"
                    if is_bundle
                    else "a file outside tests"
                ),
                paths=tuple(c.path for c in members),
                here=_side(
                    [c.local for c in members],
                    version_here(test) if is_test else None,
                    file=not (is_test or is_bundle),
                    now=now,
                ),
                there=_side(
                    [c.server for c in members],
                    version_there(test) if is_test else None,
                    file=not (is_test or is_bundle),
                    now=now,
                ),
                happened=happened,
                text=all(_is_text(c.path) for c in members),
                test=test,
            )
        )
    return rows


def _is_text(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return "." in name and "." + name.rsplit(".", 1)[-1].lower() in TEXT_SUFFIXES


def pairs(
    paths: Iterable[str],
    read_mine: Callable[[str], bytes | None],
    read_theirs: Callable[[str], bytes | None],
) -> list[Pair]:
    """Compare's files: each side's text (``None`` where that side has no such file)."""

    def text(data: bytes | None) -> str | None:
        return None if data is None else data.decode("utf-8", errors="replace")

    return [Pair(path, text(read_mine(path)), text(read_theirs(path))) for path in paths]
