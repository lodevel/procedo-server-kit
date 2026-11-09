"""The push lock: one per project on this PC, held by the operating system.

A push takes this project's turn for each entry (:func:`_take_turn`); an evidence Move
holds the uploads (:class:`PushHold`) from its first check until the configuration
names the new store and the window has re-opened it.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

from .evidence_store import EvidenceError, safe_component
from .evidence_store_fs import os_error
from .project_identity import (
    ConfigLockBusy,
    _acquire_lock,
    _lock_owner,
    _record_owner,
    _release_lock,
)

if TYPE_CHECKING:
    from .evidence_outbox import OutboxStore

#: How long a Move waits for an upload already running to finish its entry.
MOVE_WAIT_S = 60.0


def _lock_path(outbox: Path, project_id: str, kind: str) -> Path:
    """One of this project's two locks on this PC: ``push`` is the turn a push holds
    for one entry, ``move`` is held by a Move from before it waits for that turn until
    it lets uploads go on. Beside the outbox and not in it: an outbox that holds
    nothing must read as empty, and the sweep owns every folder in it."""
    folder = outbox.with_name(f"{outbox.name}.locks")
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{safe_component(project_id)}.{kind}.lock"


def _try_lock(
    outbox: Path, project_id: str, kind: str, wait_s: float, *, record: bool = False
) -> int | None:
    """The lock, waited for at most ``wait_s``; ``None`` while somebody else holds it.
    ``record``: say who holds it (a Move's hold, which a busy message names) — never for
    a push's turn or a probe, which would rewrite the file for every entry."""
    fd = None
    try:
        fd = _acquire_lock(_lock_path(outbox, project_id, kind), wait_s=wait_s)
        if record:
            _record_owner(fd)
    except ConfigLockBusy:
        return None
    except OSError as exc:
        if fd is not None:
            _release_lock(fd)
        raise os_error(exc) from exc
    return fd


def uploads_held(store: OutboxStore) -> bool:
    """Whether a Move holds this project's uploads from this PC, or waits for them."""
    moving = _try_lock(store.outbox, store.project_id, "move", 0)
    if moving is None:
        return True
    _release_lock(moving)
    return False


def _take_turn(store: OutboxStore) -> int | None:
    """The push turn for one entry, without waiting; ``None`` when this push must stop
    — a Move holds the turn or waits for it, or another window on this PC is pushing.

    The turn is taken BEFORE the Move's lock is asked: a Move that holds its lock and
    then wins the turn knows no push is running, because every push holds the turn
    while it asks. Asking the other way round, a push could pass the question and then
    take the turn after the Move had checked it."""
    turn = _try_lock(store.outbox, store.project_id, "push", 0)
    if turn is None:
        return None
    try:
        held = uploads_held(store)
    except BaseException:
        _release_lock(turn)  # a turn that leaked would stop every later push for good
        raise
    if held:
        _release_lock(turn)  # a Move is waiting: give way between entries
        return None
    return turn


class PushHold:
    """A Move's hold on this project's uploads from this PC.

    Taken before the Move's first check and released only once the configuration names
    the new store and the window has re-opened it: an upload never lands in the store
    being left — the accepted gap of a switch that came mid-entry is closed — and the
    entries still waiting go to the new store afterwards. A push already running
    finishes its entry first and the next one gives way (:func:`_take_turn`); a holder
    that does not let go within :data:`MOVE_WAIT_S` makes the Move say so, with
    nothing changed.

    The locks are the operating system's, so a process that dies releases them. Taken
    on a worker, released on the GUI thread: :meth:`release` may run on any thread,
    twice, and before :meth:`take` has finished — the hold then ends at once and the
    take refuses (``cancelled``), so a Move never runs on a hold already let go.
    """

    def __init__(self, outbox: Path, project_id: str) -> None:
        self._outbox, self._project_id = outbox, project_id
        self._held: list[int] = []
        self._released = False
        self._guard = threading.Lock()

    def take(self) -> None:
        if not self._project_id:
            return  # nothing can be queued for it; the Move refuses it in its own words
        deadline = time.monotonic() + MOVE_WAIT_S
        held: list[int] = []
        try:
            for kind in ("move", "push"):  # the order :func:`_take_turn` relies on
                fd = _try_lock(
                    self._outbox,
                    self._project_id,
                    kind,
                    max(0.0, deadline - time.monotonic()),
                    record=True,
                )
                if fd is None:
                    # Only a Move records itself, so only the move lock can name its holder.
                    holder = (
                        _lock_owner(_lock_path(self._outbox, self._project_id, kind))
                        if kind == "move"
                        else ""
                    )
                    raise EvidenceError(
                        "invalid",  # a refusal like the Move's others, shown with its text
                        "An upload or another evidence move of this project is still running "
                        f"on this PC{holder}. Nothing was changed; try again once it has "
                        "finished.",
                    )
                held.append(fd)
            with self._guard:
                if self._released:
                    raise EvidenceError(
                        "cancelled", "the evidence move was stopped before it began"
                    )
                self._held = held
        except BaseException:
            for fd in reversed(held):
                _release_lock(fd)
            raise

    def release(self) -> None:
        with self._guard:
            held, self._held, self._released = self._held, [], True
        for fd in reversed(held):
            _release_lock(fd)
