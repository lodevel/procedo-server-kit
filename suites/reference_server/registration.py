"""§3.3 ``boards_registered``: on a server registering its boards, each board of a project is
registered or not, and E4 lists exactly the registered ones (:func:`listed`).

A bench's write naming a board that is not registered (a run, E1; a report or a draft, E10; a
board entry, E15) is ``409 unknown_board``, kind ``invalid``, nothing written (:func:`check`);
on a registered board it is never refused so. An import (``"import": true``) is taken on any
board, whose board, when new, becomes registered once it is stored (:func:`register`): the one
way a board is registered through the contract.
"""

from __future__ import annotations

from . import faults
from .errors import conflict
from .state import State


def check(state: State, project_id: str, key: tuple, serial: str, *, let_through: bool) -> None:
    """``409 unknown_board`` for a bench's write naming a board *key* not registered in
    *project_id*; nothing on a server not registering its boards, nor when *let_through* (a
    fault of the call's rule)."""
    if not state.registered or let_through:
        return
    if key not in state.registered_boards.get(project_id, set()):
        raise conflict(
            "unknown_board",
            f"This server lists no board {serial!r} of that part number and revision: "
            "pick a board it holds.",
        )


def register(state: State, project_id: str, key: tuple) -> None:
    """An import's board becomes registered: E4 lists it from then on, and every bench's write
    on it is taken."""
    if not state.registered:
        return
    if faults.on("3.3.listed-taken"):  # the fault: listed by E4, yet not registered
        state.listed_only.setdefault(project_id, set()).add(key)
        return
    state.registered_boards.setdefault(project_id, set()).add(key)


def listed(state: State, project_id: str, key: tuple) -> bool:
    """Whether E4 lists the board *key*: every board on a server not registering its boards,
    the registered ones alone on one that does."""
    if not state.registered:
        return True
    return key in state.registered_boards.get(project_id, set()) or key in state.listed_only.get(
        project_id, set()
    )
