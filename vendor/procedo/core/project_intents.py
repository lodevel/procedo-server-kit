"""What the app itself removed from a working copy, so the share is told without a question
(docs/plans/project_working_copy_design.md, "Write path", decision 9).

A file missing from a working copy reaches the source only once somebody approves it by path:
a disappearance nobody explains may be a crashed write or an Explorer slip, and the source may
hold the last copy. Studio's *Delete test* and *Rename test* are not that: the person asked for
them. They note the folder they are taking away here, in the copy's machine-local state folder,
and a pass counts each path under a noted folder as approved. Anything else still asks.

* :func:`note_removed` — Studio, right BEFORE the folder leaves its name, so no look at the copy
  finds it gone and unexplained; a rename names where it goes (*moved_to*). :func:`withdraw` when
  the folder stayed after all (the rename failed).
* :func:`explained` — the offline count: which missing paths are the app's own (not questions).
* :func:`approved` — the pass: which of the deletes it planned the app asked for, and may go now.
* :func:`settle` — the pass, at its end: drops the notes that are done.

A note approves nothing it should not:

* it belongs to ONE folder — the working copy its record describes, by folder key — so a project
  opened elsewhere under the same ``project_id`` (the share itself, a local copy, a checkout made
  after *Forget*) never writes one here, and a note left from another copy is ignored;
* it approves only while its folder is ABSENT from the copy (case folded), whatever the note says:
  a rename that failed and could not withdraw, a crash between the note and the rename, a test
  made again under the name — a later disappearance in that folder still asks;
* a rename's old name goes only once everything under the new name is on the share (has a row),
  so a rename whose uploads failed or met a conflict never leaves the test only on this PC;
* it is dropped once nothing is left under it on the share, or once a pass that began after it
  was written finds its folder in the copy. A note written while a pass runs is not that pass's.

Two processes write the note (Studio adds, the syncing Bench drops), so every change is made
under the operating system's lock on ``intents.lock`` beside it, and the file is replaced whole.
Qt-free, stdlib only.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from . import atomic_write, project_identity, project_local_state, project_origin
from .project_never_travels import exclusion

log = logging.getLogger(__name__)

#: The note, and the lock every change to it is made under, in the copy's state folder.
INTENTS_FILE = "intents.json"
INTENTS_LOCK = "intents.lock"
#: How long a change waits for the other process to finish its own.
LOCK_WAIT_S = 2.0


@dataclass(frozen=True)
class Intent:
    """One noted removal: when it was written, and where a rename took the folder."""

    at: float
    moved_to: str = ""


def _relative(working_copy: Path, path: Path | str) -> str | None:
    """*path* as the copy's relative POSIX path; ``None`` for one outside it (never noted)."""
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = working_copy / candidate
    try:
        rel = os.path.relpath(os.path.normpath(candidate), os.path.normpath(working_copy))
    except ValueError:  # another drive
        return None
    rel = rel.replace(os.sep, "/")
    if rel in (".", "") or rel == ".." or rel.startswith("../"):
        return None
    return rel


def covers(noted: Iterable[str], path: str) -> bool:
    """Whether *path* is a noted path or lies under one (case-insensitively, as the planner
    matches the two sides)."""
    folded = path.casefold()
    return any(folded == n.casefold() or folded.startswith(n.casefold() + "/") for n in noted)


def present(working_copy: Path, rel: str) -> bool:
    """Whether *rel* names something in the copy, its case folded at every step: a case-only
    rename on a case-sensitive disk is still the same folder."""
    here = working_copy
    for part in rel.split("/"):
        try:
            names = os.listdir(here)
        except OSError:
            return False
        match = next((name for name in names if name.casefold() == part.casefold()), None)
        if match is None:
            return False
        here = here / match
    return True


def _own(working_copy: Path) -> bool:
    origin = project_origin.load(working_copy)
    return origin is not None and project_origin.describes(origin, working_copy)


def _file(working_copy: Path, *, create: bool) -> Path | None:
    state = (
        project_local_state.state_dir(working_copy)
        if create
        else project_local_state.existing_state_dir(working_copy)
    )
    return None if state is None else state / INTENTS_FILE


@contextmanager
def _locked(note: Path) -> Iterator[None]:
    """The note's lock, waited for (bounded); a filesystem that cannot lock is used unlocked,
    as everywhere else (``project_identity.lock_file``)."""
    fd = os.open(note.with_name(INTENTS_LOCK), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        deadline = time.monotonic() + LOCK_WAIT_S
        while not project_identity.lock_file(fd, take=True):
            if time.monotonic() >= deadline:
                raise TimeoutError(f"{note} is being changed by another process")
            time.sleep(0.02)
        try:
            yield
        finally:
            project_identity.lock_file(fd, take=False)
    finally:
        os.close(fd)


def _read(note: Path, working_copy: Path) -> dict[str, Intent]:
    """The notes written for THIS working copy: relative path -> its intent. A note another
    folder left under the same ``project_id`` reads as none."""
    try:
        data = json.loads(note.read_text("utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        log.warning("intents note %s is unreadable; nothing in it approves a delete", note)
        return {}
    if not isinstance(data, dict) or data.get("copy") != project_origin.folder_key(working_copy):
        return {}
    removed = data.get("removed")
    found: dict[str, Intent] = {}
    for path, entry in removed.items() if isinstance(removed, dict) else ():
        at = entry.get("at") if isinstance(entry, dict) else None
        moved_to = entry.get("moved_to", "") if isinstance(entry, dict) else ""
        if isinstance(path, str) and path and isinstance(at, int | float):
            found[path] = Intent(float(at), moved_to if isinstance(moved_to, str) else "")
    return found


def _write(note: Path, working_copy: Path, removed: dict[str, Intent]) -> None:
    if not removed:
        note.unlink(missing_ok=True)
        return
    atomic_write.write_json(
        note,
        {
            "copy": project_origin.folder_key(working_copy),
            "removed": {
                path: {"at": intent.at, "moved_to": intent.moved_to}
                for path, intent in sorted(removed.items())
            },
        },
    )


def _intents(working_copy: Path) -> dict[str, Intent]:
    note = _file(working_copy, create=False)
    return _read(note, working_copy) if note is not None else {}


def note_removed(
    working_copy: Path | str, paths: Iterable[Path | str], *, moved_to: Path | str | None = None
) -> bool:
    """Note that the app is removing *paths* (absolute, or relative to the copy; a folder covers
    everything under it) from *working_copy* — moving them to *moved_to* for a rename. ``False``
    when nothing was noted: the folder is not the working copy its record describes, or it has
    no state folder — its deletes then ask, as before."""
    working_copy = Path(working_copy)
    if not _own(working_copy):
        return False  # a project on this disk, or another folder of the same project
    wanted = [rel for rel in (_relative(working_copy, p) for p in paths) if rel]
    target = _relative(working_copy, moved_to) if moved_to is not None else ""
    note = _file(working_copy, create=True)
    if note is None or not wanted or target is None:
        return False
    with _locked(note):
        removed = _read(note, working_copy)
        for rel in wanted:
            removed[rel] = Intent(time.time(), target)
        _write(note, working_copy, removed)
    return True


def withdraw(working_copy: Path | str, paths: Iterable[Path | str]) -> None:
    """The app did not remove *paths* after all: their notes go."""
    working_copy = Path(working_copy)
    note = _file(working_copy, create=False)
    if note is None or not note.exists():
        return
    wanted = {rel for rel in (_relative(working_copy, p) for p in paths) if rel}
    with _locked(note):
        removed = _read(note, working_copy)
        _write(note, working_copy, {p: i for p, i in removed.items() if p not in wanted})


def noted(working_copy: Path | str) -> frozenset[str]:
    """Every path the app noted as removed and no pass has finished with yet."""
    return frozenset(_intents(Path(working_copy)))


def _gone(working_copy: Path, intents: dict[str, Intent]) -> list[str]:
    """The noted folders absent from the copy: only those explain anything."""
    return [rel for rel in intents if not present(working_copy, rel)]


def explained(working_copy: Path | str, deletes: Iterable[str]) -> frozenset[str]:
    """Of the server-side *deletes*, those under a folder the app removed (it is gone from the
    copy): not questions, whether or not they may go yet."""
    working_copy = Path(working_copy)
    gone = _gone(working_copy, _intents(working_copy))
    return frozenset(path for path in deletes if gone and covers(gone, path))


def approved(
    working_copy: Path | str, deletes: Iterable[str], rows: Iterable[str]
) -> frozenset[str]:
    """Of the server-side *deletes* a pass planned, those the app asked for that may go NOW: the
    folder is gone from the copy, and a rename's new name is on the share whole (every file
    under it has a manifest row, *rows*)."""
    working_copy = Path(working_copy)
    intents = _intents(working_copy)
    synced = {row.casefold() for row in rows}
    ready = [
        rel for rel in _gone(working_copy, intents) if _arrived(working_copy, intents, rel, synced)
    ]
    return frozenset(path for path in deletes if ready and covers(ready, path))


def _arrived(working_copy: Path, intents: dict[str, Intent], rel: str, synced: set[str]) -> bool:
    """Whether what *rel* was renamed to is on the share whole — following a rename that was
    renamed again; a destination gone with no note of its own was deleted after it, which
    leaves nothing to wait for."""
    seen: set[str] = set()
    target = intents[rel].moved_to
    while target and target not in seen:
        seen.add(target)
        if present(working_copy, target):
            return _all_synced(working_copy, target, synced)
        target = intents[target].moved_to if target in intents else ""
    return True


def _all_synced(working_copy: Path, folder: str, synced: set[str]) -> bool:
    root = working_copy.joinpath(*folder.split("/"))
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath).relative_to(working_copy).as_posix()
        dirnames[:] = [d for d in dirnames if not exclusion(f"{here}/{d}")]
        for name in filenames:
            rel = f"{here}/{name}"
            if not exclusion(rel) and rel.casefold() not in synced:
                return False
    return True


def settle(working_copy: Path | str, rows: Iterable[str], *, started: float) -> None:
    """Drop every note that is done (the pass began at *started*, seconds since the epoch): no
    manifest row (*rows*) is left under it — the share has nothing of it any more — or it was
    there when the pass began and its folder is in the copy (the remove did not happen, or the
    test was made anew), so a later disappearance there is nobody's request."""
    working_copy = Path(working_copy)
    note = _file(working_copy, create=False)
    if note is None or not note.exists():
        return
    rows = list(rows)
    with _locked(note):
        kept = {
            rel: intent
            for rel, intent in _read(note, working_copy).items()
            if any(covers((rel,), row) for row in rows)
            and not (intent.at < started and present(working_copy, rel))
        }
        _write(note, working_copy, kept)
