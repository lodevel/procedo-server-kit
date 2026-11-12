"""Who is using a working copy right now, proved by a live process rather than guessed
(docs/plans/project_working_copy_design.md, "Only the open project's copy is kept": claims).

Each holder writes its own ``in_use.<pid>.<why>`` into the copy's machine-local state folder, with
the time its process started, and removes that very file when done (:func:`drop`). A claim whose
process is not alive on this PC — or whose pid now belongs to a process that started at another
time (Windows hands pids out again quickly) — is stale and counts for nothing, so a crash never
leaves a copy claimed for ever; the remover deletes stale claims it meets. Every Bench process on
the PC sees every claim: the window that syncs a copy is not always the one running a test from it.

* :data:`OPEN` — a Bench window has the project open (that Bench's pid), from before it is
  read until it closes.
* :data:`STUDIO` — Procedo Studio works on it (the Studio process's pid; Bench writes it under
  its own pid just before the launch, then under Studio's once it runs).
* :data:`RUN` — a test runs from the copy (an automated queue or a guided run), from before it
  reads its first file until it ends: a sync pass lands nothing in the copy meanwhile.
* :data:`EXPORT` — the project is exported to a zip from the copy (``project_zip_export``):
  nothing lands meanwhile either, so the zip is one version of it (:func:`holds_landing`).

**Claim, then look** (:func:`claim`). A removal holds the copy's ``removal.lock`` from its first
look at the claims until it is done (:func:`removing`); a claimer writes its claim, then waits for
any removal under way to finish, and only then looks for the folder. Either the removal saw the
claim and kept the copy (or put it back), or it had finished and the folder is gone — never a
folder read while it is being set aside.

Only a working copy is ever claimed: a project on this PC's own disk is never removed, so nothing
is written for it. Qt-free, stdlib only.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from . import paths, project_identity, project_local_state
from .process_alive import pid_alive, start_time

log = logging.getLogger(__name__)

#: A Bench window has the project open.
OPEN = "open"
#: Procedo Studio works on it.
STUDIO = "studio"
#: A test runs from the copy.
RUN = "run"
#: The project is exported to a zip from the copy.
EXPORT = "export"
#: Every claim, in the order a refusal names them.
ALL = (OPEN, STUDIO, RUN, EXPORT)
#: The claims a sync pass lands nothing in the copy for (:func:`holds_landing`).
HOLD_LANDING = (RUN, EXPORT)

_PREFIX = "in_use."
#: Held by a removal, from its first look at the claims until the copy is gone or kept.
REMOVAL_LOCK = "removal.lock"
#: How long a claimer waits for a removal under way to finish before it looks for the folder.
SETTLE_S = 60.0


def _name(pid: int, why: str) -> str:
    return f"{_PREFIX}{pid}.{why}"


def claim(
    working_copy: Path | str, why: str, pid: int | None = None, *, settle_s: float = SETTLE_S
) -> Path | None:
    """Claim *working_copy* for *why* under *pid* (this process by default), then wait for any
    removal under way to finish (*settle_s* at most): the caller looks for the folder only after
    this returns. The claim file — what :func:`drop` takes — or ``None`` when nothing was
    claimed: the folder is not a working copy (it is never removed), or it has no state folder."""
    if not paths.inside(working_copy, project_local_state.working_copies_root()):
        return None
    state = project_local_state.state_dir(working_copy)
    if state is None:
        return None
    pid = os.getpid() if pid is None else pid
    path = state / _name(pid, why)
    path.write_text(str(start_time(pid) or ""), "utf-8")
    _settle(state, settle_s)
    return path


def drop(path: Path | None) -> None:
    """Remove the claim *path* (:func:`claim`'s answer); one already gone is already released."""
    if path is not None:
        path.unlink(missing_ok=True)


def release(working_copy: Path | str, why: str, pid: int | None = None) -> None:
    """Drop this process's (or *pid*'s) claim found by the copy's path (prefer :func:`drop` with
    the claim file: a copy set aside meanwhile can no longer be found from its path)."""
    state = project_local_state.existing_state_dir(working_copy)
    if state is not None:
        (state / _name(os.getpid() if pid is None else pid, why)).unlink(missing_ok=True)


def _claims(state: Path) -> Iterator[tuple[Path, str, bool]]:
    """Every claim file in *state*: (file, why, whether its process holds it now). Raises when
    an existing state folder cannot be listed: "nobody claims it" is never a guess."""
    try:
        names = os.listdir(state)
    except FileNotFoundError:
        return
    for name in names:
        if not name.startswith(_PREFIX):
            continue
        pid, _, why = name[len(_PREFIX) :].partition(".")
        if not pid.isdigit() or why not in ALL:
            continue
        yield state / name, why, _held(state / name, int(pid))


def _held(path: Path, pid: int) -> bool:
    """Whether *pid* is alive and is the process that wrote the claim (same start time)."""
    if not pid_alive(pid):
        return False
    try:
        written = path.read_text("utf-8").strip()
    except FileNotFoundError:
        return False  # released meanwhile
    except OSError:
        return True  # unreadable: held, the safe way
    if not written:
        return True  # its start time could not be read when it claimed: the pid alone says
    now = start_time(pid)
    return not now or str(now) == written


def live(working_copy: Path | str, why: str) -> bool:
    """Whether a living process on this PC claims *working_copy* for *why*."""
    state = project_local_state.existing_state_dir(working_copy)
    return state is not None and live_in(state, why)


def holds_landing(working_copy: Path | str) -> bool:
    """Whether a living process on this PC reads *working_copy* as one version (a test run,
    an export: :data:`HOLD_LANDING`): a sync pass lands nothing in it meanwhile. One look at
    the claims; claims that cannot be read count as held."""
    state = project_local_state.existing_state_dir(working_copy)
    if state is None:
        return False
    try:
        return any(held and kind in HOLD_LANDING for _path, kind, held in _claims(state))
    except OSError:
        return True


def landing_holder(working_copy: Path | str) -> str | None:
    """Which kind holds the landings in *working_copy* (:data:`HOLD_LANDING`): :data:`RUN`
    when a run holds it (with an export or not: the run is what the operator watches),
    :data:`EXPORT` when only an export does, ``None`` when nothing does. One look at the
    claims, as :func:`holds_landing`; claims that cannot be read count as held by a run."""
    state = project_local_state.existing_state_dir(working_copy)
    if state is None:
        return None
    try:
        kinds = {kind for _path, kind, held in _claims(state) if held and kind in HOLD_LANDING}
    except OSError:
        return RUN
    return next((kind for kind in HOLD_LANDING if kind in kinds), None)


def live_in(state: Path, why: str) -> bool:
    """:func:`live`, for the copy whose state folder is *state* (found before the copy moved).
    Claims that cannot be read count as held: a sync holds its landings, a removal keeps."""
    try:
        return any(held and kind == why for _path, kind, held in _claims(state))
    except OSError:
        return True


def holder(working_copy: Path | str) -> str:
    """Which claim keeps *working_copy* in use (:data:`ALL`'s order), ``""`` when none is live."""
    state = project_local_state.existing_state_dir(working_copy)
    return holder_in(state) if state is not None else ""


def holder_in(state: Path) -> str:
    """:func:`holder`, for the copy whose state folder is *state*. Raises when the claims
    cannot be read (the caller keeps the copy)."""
    held = {kind for _path, kind, alive in _claims(state) if alive}
    return next((why for why in ALL if why in held), "")


def prune_in(state: Path) -> None:
    """Delete the stale claims in *state*: nobody holds them. Called under :func:`removing`."""
    for path, _why, held in list(_claims(state)):
        if not held:
            path.unlink(missing_ok=True)


@contextmanager
def removing(state: Path) -> Iterator[bool]:
    """The copy's removal lock, taken WITHOUT waiting: ``True`` while held (the removal goes on),
    ``False`` when another removal holds it (this one keeps the copy). A filesystem that cannot
    lock answers ``True``, as every lock here degrades."""
    fd = os.open(state / REMOVAL_LOCK, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        if not project_identity.lock_file(fd, take=True):
            yield False
            return
        try:
            yield True
        finally:
            project_identity.lock_file(fd, take=False)
    finally:
        os.close(fd)


def _settle(state: Path, wait_s: float) -> None:
    """Wait until no removal holds *state*'s removal lock (bounded)."""
    deadline = time.monotonic() + wait_s
    while True:
        with removing(state) as free:
            if free:
                return
        if time.monotonic() >= deadline:
            log.warning("a removal of the copy in %s is still running; going on", state)
            return
        time.sleep(0.05)
