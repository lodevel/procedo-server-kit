"""A generation's manifest and its test's pointer, read and advanced over a :class:`TreeIO`.

The primitives of ``layout: shared`` that work on a tree, not on a store — the layout
(:mod:`evidence_shared_layout`), the outbox's push and the conversion all reuse them:

* ``manifest.json`` (written LAST) makes a generation complete; without it the
  generation is incomplete and invisible (:func:`_manifest`, :func:`_manifests`);
* ``current.json`` names the test's current run, and advances iff a complete run's
  ``ended`` is strictly newer than the pointer's (:func:`advances`,
  :func:`_advance_pointer`);
* ``annotations/`` beside the manifest holds what people wrote about the run: one file per
  comment or verdict override saved (``000001.json``…, numbered in the order received, under
  the folder's lock), never rewritten (:func:`add_entries`, the rule of
  :mod:`annotation_entries`); a run annotated before entries keeps its whole-file
  ``annotations.json``, read as its first entries;
* ``entries/`` in a board's folder holds the board's lot and fields the same way, one file
  per value saved (:func:`add_board_entries`). Each entry file is written by the store that
  takes it, which adds when (``received_at``).

Qt-free, stdlib only; every store read and write goes through the tree.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable, Mapping
from pathlib import PurePath

from .annotation_entries import (
    ALREADY,
    BOARD_KINDS,
    RECEIVED_AT,
    RUN_KINDS,
    heads,
    is_entry,
    legacy_entries,
    outcome,
    slot,
    utc_now,
    value_of,
)
from .evidence_board import PARTS
from .evidence_store import OpContext, RunInfo
from .evidence_tree import TreeIO, glob_children, lock_wait_s, read_json, tree_lock

log = logging.getLogger(__name__)

MANIFEST = "manifest.json"
POINTER = "current.json"
ANNOTATIONS = "annotations.json"  # a run's whole-file annotation, before entries
ENTRIES = "annotations"  # a run's annotation entries, one file each
BOARD_ENTRIES = "entries"  # a board's lot and fields, one file each, in the board's folder
_ENTRY_FILE = re.compile(r"(\d{6})\.json")


def _manifest(io: TreeIO, run_dir: PurePath) -> dict | None:
    """The generation's manifest; ``None`` = incomplete (no manifest) or unreadable."""
    return read_json(io, run_dir / MANIFEST) or None


def run_info(m: dict) -> RunInfo:
    return RunInfo(
        m["run_id"],
        m.get("started", ""),
        m.get("ended", ""),
        m.get("verdict", ""),
        m.get("machine_id", ""),
        bool(m.get("complete")),
        m.get("test", ""),
        m.get("test_id", ""),
    )


def _manifests(
    io: TreeIO, test_dir: PurePath, known: Mapping[str, dict] | None = None
) -> list[dict]:
    """Committed generations under ``<test>/runs``, newest first. A listed generation whose
    manifest is in ``known`` (run id -> manifest, an earlier read's) is not opened again: once
    its manifest is published, a generation never changes."""
    runs = test_dir / "runs"
    names = io.list_names(runs)
    if names is None:
        return []
    known = known or {}
    found = []
    for d in sorted((runs / n for n in names), reverse=True):
        m = known.get(d.name) or (_manifest(io, d) if io.is_dir(d) else None)
        if m:
            found.append(m)
    return found


def _read_pointer(io: TreeIO, test_dir: PurePath) -> dict | None:
    """``current.json``: ``None`` when absent, ``{}`` when unreadable (logged)."""
    p = test_dir / POINTER
    if not io.exists(p):
        return None
    data = read_json(io, p)
    if not data:
        log.warning("Unreadable pointer %s: falling back to the newest complete run", p)
    return data


def annotation_of(m: dict, entries: list[dict]) -> dict:
    """A run's annotation as read (``data_format.md`` §4.9): its entries in the order received
    + the manifest's execution block."""
    execution = {"operator": m.get("operator", ""), "completed_at": m.get("ended", "")}
    return {
        "format_version": 1,
        "run_id": m.get("run_id", ""),
        "entries": entries,
        "execution": {**execution, "serial_number": m.get("serial_number", "")},
    }


def _numbered(io: TreeIO, folder: PurePath) -> list[str]:
    """The entry files of an entries folder (``annotations/``, ``entries/``), in the order
    received."""
    return sorted(n for n in io.list_names(folder) or [] if _ENTRY_FILE.fullmatch(n))


def _read_numbered(io: TreeIO, folder: PurePath, kinds: tuple[str, ...]) -> list[dict]:
    """The entries of ``kinds`` an entries folder holds, by number: the order received."""
    found = []
    for name in _numbered(io, folder):
        e = read_json(io, folder / name)
        if is_entry(e, kinds):
            found.append(e)
    return found


def read_entries(io: TreeIO, run_dir: PurePath) -> list[dict]:
    """A generation's annotation entries in the order received: its whole-file
    ``annotations.json`` of before entries first, then each entry file by number."""
    found = legacy_entries(read_json(io, run_dir / ANNOTATIONS))
    return found + _read_numbered(io, run_dir / ENTRIES, RUN_KINDS)


def read_board_entries(io: TreeIO, board_dir: PurePath) -> list[dict]:
    """A board's lot and fields as entries, in the order received (``[]``: none saved)."""
    return _read_numbered(io, board_dir / BOARD_ENTRIES, BOARD_KINDS)


def add_entries(
    io: TreeIO,
    run_dir: PurePath,
    entries: list[dict],
    tag: str,
    ctx: OpContext | None = None,
    *,
    changes_only: bool = False,
) -> list[dict]:
    """Add ``entries`` to a generation's annotation (:func:`_add_numbered`). ``changes_only``:
    an entry whose value is already its key's current one is left out (a whole-file save
    converted to entries, which names every key it held, changed or not)."""
    return _add_numbered(
        io, run_dir / ENTRIES, lambda: read_entries(io, run_dir), entries, tag, ctx, changes_only
    )


def add_board_entries(
    io: TreeIO, board_dir: PurePath, entries: list[dict], tag: str, ctx: OpContext | None = None
) -> list[dict]:
    """Add ``entries`` to a board's lot and fields (:func:`_add_numbered`). The caller has
    made the board's ``entries/`` folder: a board may have no run yet."""
    folder = board_dir / BOARD_ENTRIES
    return _add_numbered(io, folder, lambda: read_board_entries(io, board_dir), entries, tag, ctx)


def _add_numbered(
    io: TreeIO,
    folder: PurePath,
    read: Callable[[], list[dict]],
    entries: list[dict],
    tag: str,
    ctx: OpContext | None,
    changes_only: bool = False,
) -> list[dict]:
    """Add ``entries`` to an entries folder, each a NEW file numbered after the last and
    stamped with when the store took it (``received_at``), under the folder's lock: the
    order received is the order of the numbers, whatever the benches' clocks say. Each
    answers :func:`annotation_entries.outcome` against what the folder held right before it
    (``read``): ``saved``, ``changed`` (written all the same: nothing is lost) or ``already``
    (its id is there: a replay, nothing written)."""
    io.mkdir(folder)
    with tree_lock(
        io,
        folder / ".lock",
        tag,
        timeout_s=lock_wait_s(ctx),
        cancel=ctx.cancel if ctx else None,
        ctx=ctx,
    ):
        held = read()
        names = _numbered(io, folder)
        number = int(names[-1][:6]) if names else 0
        answers = []
        for e in entries:
            head = heads(held).get(slot(e)) if changes_only else None
            if changes_only and value_of(head) == value_of(e):
                continue
            answer = outcome(held, {**e, RECEIVED_AT: utc_now()})
            if answer["status"] != ALREADY:
                number += 1
                io.write_json(folder / f"{number:06d}.json", answer["entry"], tag)
                held.append(answer["entry"])
            answers.append(answer)
    return answers


def _pointer_of(info: RunInfo) -> dict:
    return {"run_id": info.run_id, "ended": info.ended, "machine_id": info.machine_id}


def advances(ptr: dict | None, info: RunInfo) -> bool:
    """THE pointer rule: a complete run becomes current iff its ``ended`` is strictly newer
    than the pointer's (a demoted one keeps its ``ended``; none takes any complete run)."""
    return info.complete and (not ptr or info.ended > ptr.get("ended", ""))


def after_pushes(ptr: dict | None, infos: Iterable[RunInfo]) -> RunInfo | None:
    """Which of ``infos``, pushed in this order onto pointer ``ptr``, ends up current
    (:func:`advances` run after run, as the pushes apply it); ``None``: none of them."""
    winner = None
    for info in infos:
        if advances(ptr, info):
            ptr, winner = _pointer_of(info), info
    return winner


def _advance_pointer(
    io: TreeIO, test_dir: PurePath, info: RunInfo, tag: str, ctx: OpContext | None = None
) -> bool:
    """The rule: strictly newer ``ended`` wins; ties, older runs and incomplete runs
    are no-ops. A demoted pointer keeps its ``ended``, so only a genuinely newer
    run revives the test. Every pointer writer holds the same store-side lock,
    so another bench cannot replace a newer pointer using an earlier read.

    A writer that cannot TAKE that lock — timed out, refused, or cancelled by the
    user — leaves the generation committed with the pointer behind it, and the error
    it raises is the whole answer. The run is already on the store and already in the
    history; promoting it is one click. Nothing is patched behind the user's back:
    a marker beside the pointer was tried and removed, because a marker whose delete
    failed silently outranked every later promote and demote, and an older bench that
    had never heard of it had its own pointer writes ignored."""
    if not info.complete:
        return False
    with tree_lock(
        io,
        test_dir / ".pointer.lock",
        tag,
        timeout_s=lock_wait_s(ctx),
        cancel=ctx.cancel if ctx else None,
        ctx=ctx,
    ):
        if not advances(_read_pointer(io, test_dir), info):
            return False
        io.write_json(test_dir / POINTER, _pointer_of(info), tag)
    return True


def _is_dir_or_skip(io: TreeIO, path: PurePath) -> bool:
    try:
        return io.is_dir(path)
    except OSError:
        return False


def _board_dirs(io: TreeIO, ns: PurePath) -> list[PurePath]:
    """Every board's folder, in listing order: ``<serial>`` and
    ``parts/<part number>/<revision>/<serial>`` (:mod:`evidence_board`); what cannot be
    listed or stat-ed is skipped. ``parts`` itself is never read as a board: a part
    number named ``results`` would otherwise make its revisions read as tests."""
    found: list[PurePath] = []
    for child in glob_children(io, ns, "*"):
        if child.name != PARTS:
            if _is_dir_or_skip(io, child):
                found.append(child)
            continue
        for pn in glob_children(io, child, "*"):
            for rev in glob_children(io, pn, "*"):
                found += [s for s in glob_children(io, rev, "*") if _is_dir_or_skip(io, s)]
    return found


def _generation_dirs(io: TreeIO, ns: PurePath) -> list[PurePath]:
    """``ns.glob("*/results/*/runs/*")`` and the same below every board of ``parts/``, in
    the same order: every entry of a ``<board>/results/<test>/runs/`` folder; what cannot
    be listed or stat-ed is skipped, as the glob skips it. The store's own siblings
    (``exports``, ``index``, a conversion lock) hold no ``results/``, so they contribute
    nothing."""
    found: list[PurePath] = []
    for board in _board_dirs(io, ns):
        for test in glob_children(io, board / "results", "*"):
            if _is_dir_or_skip(io, test):
                found += glob_children(io, test / "runs", "*")
    return found
