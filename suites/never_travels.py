"""The paths that never travel as a project's files (``procedo_server_contract.md`` §5.2, rule
P11), generated from the app's own list: the sync's exclusion list
(``project_never_travels.EXCLUSIONS``) and the two folders that travel as objects of their own
(``project_never_travels.OBJECT_DIRS``: ``bundle/``, ``releases/``). Nothing is spelled here, so a
name added to the app's list is a new path here, checked with no change of this module.

Each path is otherwise a valid project file of a project holding the test ``T1``: an entry
taking a root name is a file under that folder; one taking a name at any depth, a file in a
folder of that name inside ``tests/T1/``; one taking a file's name, that file in
``tests/T1/``. A server refusing it can only be refusing the name.
"""

from __future__ import annotations

from procedo.core.project_never_travels import (
    AS_FILE,
    AT_ANY_DEPTH,
    AT_ROOT,
    EXCLUSIONS,
    OBJECT_DIRS,
    exclusion,
)

#: The test folder the paths below a test go in.
TEST = "tests/T1"


def _roots() -> list[tuple[str, tuple[str, ...]]]:
    """Every entry as ``(where, its example names)``, the object folders as root entries."""
    entries = [(entry.where, entry.examples) for entry in EXCLUSIONS]
    return [*entries, (AT_ROOT, tuple(sorted(OBJECT_DIRS)))]


def _placed(where: str, name: str) -> str:
    if where == AT_ROOT:
        return f"{name}/kit/kit.json"
    if where == AT_ANY_DEPTH:
        return f"{TEST}/{name}/kit.md"
    if where == AS_FILE:
        return f"{TEST}/{name}"
    raise AssertionError(f"an exclusion entry looks at {where!r}, which no path here places")


def paths() -> list[str]:
    """One path for every example name of every entry of the list, in the list's order."""
    return [_placed(where, name) for where, names in _roots() for name in names]


def near_misses() -> list[str]:
    """Paths that DO travel, each one step from a path of :func:`paths`: its name in upper case
    (names compare exactly), a root name below a test, a file's name as a folder's."""
    out = []
    for where, names in _roots():
        for name in names:
            if where == AT_ROOT:
                out += [f"{name.upper()}/kit/kit.json", f"{TEST}/{name}/kit.md"]
            elif where == AT_ANY_DEPTH:
                out.append(f"{TEST}/{name.upper()}/kit.md")
            else:
                out += [f"{TEST}/{name.upper()}", f"{TEST}/as_folder/{name}/kit.md"]
    return out


def refused(path: str) -> bool:
    """Whether the app says *path* never travels as a file: its sync leaves it out, or it is
    under one of the object folders (names compared exactly)."""
    return path.split("/", 1)[0] in OBJECT_DIRS or bool(exclusion(path))
