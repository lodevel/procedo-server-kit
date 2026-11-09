"""Move or copy a project's SHARED evidence from any storage to any storage: the hub.

The owner (2026-11-06): *"everyone to a common format, then that common format to everyone"*.
The move never converts one storage into another: each storage reads its evidence into the
common form and writes it from it (:class:`evidence_form.EvidenceForm`, which a store gives with
``evidence_form()``), and this module only ever talks to two forms. A folder, a share, FTPS and
SFTP are the folder layout already (:mod:`evidence_form_tree`); the database builds it from its
rows and lands it as rows (:mod:`evidence_db.form`). A storage without a form is refused before
anything moves; the web server reads through its working copy and lands with the importer right of
this PC's own account (:mod:`evidence_form_api`).

Shared filesystem folders first use :mod:`evidence_rename` when a checked, non-overwriting
directory rename is possible. That path verifies directory identity and journals the
configuration switch; it does not copy or hash payload files. Everything else is the copy:

0. **One pass at a time.** The whole pass is held under the SOURCE's own move lock (a folder's
   ``.conversion-<id8>.lock``, the database's lock of the project) — and the project's entry is
   re-read under it. Two benches moving one project's evidence at the same moment used to both
   "succeed", each emptying the source into its own destination. The second pass now refuses:
   while the first holds the lock, naming the bench that holds it; afterwards, because the
   project no longer points at the store this pass was about to empty.
1. **Refuse before a byte moves** (:func:`transfer_problem`) — a destination that is the source
   or nested with it, one another project owns, a folder that already holds somebody's files and
   owns nobody, one whose PARENT is not there, one that is a file, a source that cannot be
   reached and a local source holding a link. No run is ever written into the source in place —
   every run is staged in this PC's outbox — and runs and reports still waiting there do not
   refuse the move: the caller holds the project's uploads
   (:class:`evidence_outbox_lock.PushHold`) until the switch is done and the window has re-opened
   the store, and the entries are uploaded to the new store afterwards.
2. **Copy, unit by unit, file by file** (:func:`evidence_form.by_unit`): a folder destination
   writes each file as it comes, a database lands one item (a run with its files, a mark, a
   report with its generations) in one transaction once its files are fetched. A file the
   destination keeps no place for (a folder's listing index or trash, for the database) is not
   carried: it stays in the source, is never removed, and the report names it.
3. **Verify at the destination**: every file carried, read back BY THE DESTINATION and compared
   with what the source held, in the common form's terms (a payload byte for byte, a document as
   its object); then the source re-walked, so a file written while the copy ran — a run another
   bench committed — keeps the whole source and is named, and nothing is switched.
4. **Switch** ``config.json:evidence_store`` to the target entry (through :mod:`evidence_config`,
   the project's own write path), and only if nothing re-pointed the project meanwhile.
5. **The source.** A local folder is emptied file by file, each unlink re-proving at that moment
   that the file is the one that was verified, that it is still inside the evidence folder with
   no link on the way, and that the destination still holds it. The removal always answers a
   report, never an exception. A source that is not a folder on this PC (a server, the database)
   is left in place and the summary says so: deleting evidence there is its own operation.

Re-runnable: a pass that stopped resumes, because what the destination already holds exactly is
not written again. ``cancel`` is honoured between files until the switch and takes back nothing
already verified. A kill between the switch and the removal leaves a complete second copy in the
source folder, and the folder says so itself (``.conversions.json``, :mod:`evidence_form_tree`).
Running the same move again then copies nothing, re-verifies, and finishes the removal.

Qt-free, stdlib only.
"""

from __future__ import annotations

import contextlib
import copy
import logging
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from typing import Any

from . import (
    evidence_config,
    evidence_outbox,
    evidence_outbox_lock,
    evidence_outbox_route,
    evidence_rename,
)
from .evidence_board import split_path
from .evidence_form import (
    Answer,
    EvidenceForm,
    Held,
    Rel,
    Unit,
    by_unit,
    held_of_file,
)
from .evidence_owner import MARKER
from .evidence_shared_layout import CONVERSIONS
from .evidence_shared_manifest import MANIFEST
from .evidence_store import EvidenceError
from .evidence_store_fs import os_error
from .evidence_tree import TreeIO
from .project_identity import read_project_id

log = logging.getLogger(__name__)


class _Progress:
    """Ticks per file through copy, verify and removal; the total is re-based at
    each phase, so the last tick equals it."""

    def __init__(self, total: int, report: Callable[[int, int], None] | None) -> None:
        self.total, self.done, self._report = total, 0, report

    def phase(self, remaining: int) -> None:
        self.total = self.done + remaining

    def tick(self, n: int = 1) -> None:
        self.done += n
        if self._report is not None:
            self._report(self.done, self.total)


@dataclass(frozen=True)
class TransferReport:
    source: str  # the store the evidence came from
    destination: str  # the store it is on now
    copied: int = 0  # files copied by this pass
    present: int = 0  # files an earlier, interrupted pass had already put there
    removed: int = 0  # files removed from the source (a local folder only)
    left_behind: list[str] = field(default_factory=list)  # source files kept, and why
    source_kept: bool = False  # the source is not a folder on this PC: nothing was deleted there
    renamed: bool = False  # the directory itself moved; no payload files were copied
    copied_only: bool = False  # duplicate evidence; the project still uses the source
    late_runs: list[str] = field(
        default_factory=list
    )  # runs that reached the source after the copy
    #: Source files the destination keeps no place for (a part file, for the
    #: database), with why: never carried, never removed.
    not_carried: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# The two forms
# ---------------------------------------------------------------------------


def form_of(store: Any) -> EvidenceForm | None:
    """The store's side of the hub, or ``None`` for a store that has none yet."""
    make = getattr(store, "evidence_form", None)
    return make() if make is not None else None


def store_label(store: Any) -> str:
    """A store in the user's words, for the question asked before the move and for the
    answer after it: a folder is its path; a store on a server is its address and folder
    together — ``/bench-evidence/ACME`` is a folder name on any number of servers."""
    form = form_of(store)
    if form is not None:
        return form.label
    return store.io.identity


def _shown(rel: Rel) -> str:
    return PurePath(*rel).as_posix()


# ---------------------------------------------------------------------------
# Refusals — all of them before a byte moves
# ---------------------------------------------------------------------------


def transfer_problem(
    project_root: Path, source: Any, destination: Any, *, project_id: str, scan: bool = True
) -> str | None:
    """Why this project's evidence cannot move from ``source`` to ``destination`` (``None``:
    it can), in the user's words. I/O on both stores, so it runs on a worker before the user is
    asked to confirm — and again inside the move.

    ``scan=False`` checks the selected locations without walking the source. Content checks
    belong to the confirmed operation, whose rename or copy path performs the checks appropriate
    to it before moving anything."""
    if not project_id:
        return f"{Path(project_root).name} has no project_id"
    src, dst = form_of(source), form_of(destination)
    if src is None or dst is None:
        kind = getattr(source if src is None else destination, "TYPE_LABEL", "this storage")
        return f"Evidence is not moved to or from {kind} yet. Nothing was moved."
    problem = dst.overlap(src) or src.reachable() or dst.destination_problem(scan=scan)
    if problem is not None:
        return problem
    if scan:
        links = src.links()
        if links:
            return (
                f"replace the link {src.where(links[0])} with a real folder, then move the "
                "evidence again"
            )
    return dst.target_problem(project_id)


# ---------------------------------------------------------------------------
# The copy and the verification
# ---------------------------------------------------------------------------


def _copy_unit(
    destination: EvidenceForm, unit: Unit, rels: list[Rel], fetch: Callable[[Rel], Path | None]
) -> dict[Rel, Answer]:
    """One unit landed at the destination (:meth:`EvidenceForm.put_unit`)."""
    return destination.put_unit(unit, rels, fetch)


def _copy(
    source: EvidenceForm,
    destination: EvidenceForm,
    planned: list[Rel],
    scratch: Path,
    steps: _Progress,
    check_cancel: Callable[[], None],
) -> tuple[dict[Rel, Held], int, int, dict[Rel, str], list[Rel]]:
    """Every planned file into the destination, unit by unit: ``(held, copied, present,
    not carried, derived)``, ``held`` what each carried file holds as the SOURCE had it."""
    held: dict[Rel, Held] = {}
    kept: dict[Rel, str] = {}
    derived: list[Rel] = []
    copied = present = 0
    count = 0
    for unit, rels in by_unit(planned):
        check_cancel()
        fetched: dict[Rel, Held] = {}

        def fetch(rel: Rel, _fetched: dict[Rel, Held] = fetched) -> Path | None:
            nonlocal count
            check_cancel()
            count += 1
            local = source.fetch(rel, scratch / str(count))
            if local is not None:
                _fetched[rel] = held_of_file(rel, local)
            return local

        answers = _copy_unit(destination, unit, rels, fetch)
        for rel in rels:
            answer = answers.get(rel, "not written by the destination")
            if answer is None:
                derived.append(rel)  # a cache the destination rebuilds: nothing to carry
            elif isinstance(answer, str):
                kept[rel] = answer
            else:
                held[rel] = fetched[rel]
                copied += answer
                present += not answer
            steps.tick()
        for staged in scratch.iterdir():
            staged.unlink()
    return held, copied, present, kept, derived


def _verify(
    destination: EvidenceForm,
    held: dict[Rel, Held],
    steps: _Progress,
    check_cancel: Callable[[], None],
) -> None:
    """Every carried file, proved from the DESTINATION's own reading: it lists the file, and
    what it holds is what the source held.

    Read from the destination rather than from the plan, so a file the store cannot enumerate
    is caught here — a stat of a name we already know would have found it and a later reader
    would not."""
    steps.phase(len(held))
    found = destination.held(sorted(held), check_cancel)
    for rel, was in sorted(held.items()):
        check_cancel()
        if rel not in found:
            raise EvidenceError(
                "corrupt",
                f"{destination.where(rel)} is not in the destination's own listing — nothing "
                "was switched and nothing was removed",
            )
        if found[rel] != was:
            raise EvidenceError(
                "corrupt",
                f"{destination.where(rel)} does not match the file it was copied from — "
                "nothing was switched and nothing was removed",
            )
        steps.tick()


def _first_change(before: dict[Rel, Any], now: dict[Rel, Any]) -> Rel | None:
    """The first file added, removed or rewritten in the source since the copy started."""
    for rel in sorted(set(before) | set(now)):
        if before.get(rel) != now.get(rel):
            return rel
    return None


# ---------------------------------------------------------------------------
# The removal — a local source only
# ---------------------------------------------------------------------------


def _still_held(source: Any, destination: EvidenceForm, rel: Rel, held: Held, stamp: Any) -> bool:
    """Re-proved at the moment of the unlink, nothing trusted from an earlier check: the
    destination still holds it, then this file is still the one copied (:meth:`TreeForm.
    still_holds`)."""
    if destination.held_file(rel) != held:
        return False
    return source.still_holds(rel, held, stamp)


def _drop_derived(source: Any, derived: list[Rel]) -> None:
    """A folder source's caches the destination rebuilds itself (its listing index): gone
    with the evidence they described, never listed as kept."""
    for rel in derived:
        try:
            source.local_root.joinpath(*rel).unlink(missing_ok=True)
        except OSError as exc:
            log.warning("%s was kept in %s: %s", "/".join(rel), source.local_root, exc)


def _drop_empty_folders(io: TreeIO, root: PurePath) -> None:
    """The folders below the root, deepest first, each removed only once it is empty.
    The root itself stays: the user configured that folder, and an empty folder is not
    damage."""
    from .evidence_form import travels
    from .evidence_form_tree import walk

    try:
        folders = walk(io, root)[0]
    except (OSError, EvidenceError) as exc:
        log.warning("%s could not be listed for its empty folders: %s", root, exc)
        return
    for rel in sorted(folders, key=len, reverse=True):
        if not travels(rel):
            continue  # this pass's own lock: the lock releases itself
        try:
            Path(root.joinpath(*rel)).rmdir()
        except OSError:
            pass  # not empty, or held open: something stays, and the listing says what


def _left_in(source: Any, kept: list[Rel]) -> list[str]:
    """What is still in the source folder after the removal, from a walk OF IT — and, when
    that walk itself cannot be made (the share went away mid-pass), the files this pass knows
    it did not remove. The report is owed either way."""
    from .evidence_form import travels
    from .evidence_form_tree import walk

    try:
        _folders, files, links = walk(source.io, source.root)
    except (OSError, EvidenceError) as exc:
        log.warning("%s could not be listed after the move: %s", source.root, exc)
        return sorted(_shown(rel) for rel in kept)
    return sorted(_shown(rel) for rel in files + links if travels(rel))


def _late_runs(left: list[str], held: dict[Rel, Held]) -> list[str]:
    """The runs among what stayed in the source that the copy never saw: another
    writer's push (another PC reaching the folder, or another user of this one) landed
    between the last walk and the switch. They are in the folder the project left and
    not in its new store, so the report names them — never silently.

    A run is late when a file of it stayed and the copy never held its manifest. A
    push another PC started before the switch goes on into this folder (its binding is
    checked once per entry, before the payload, and a Move holds only this PC's
    uploads), so even a run with no manifest yet is named: its writer finishes it
    here. A comment saved on a run the copy took (its manifest held) is not a run; it
    stays listed among the files kept."""

    def run_of(parts: tuple[str, ...] | list[str]) -> str | None:
        board, rest = split_path(parts)  # a bare serial, or parts/<pn>/<rev>/<serial>
        if len(rest) > 4 and rest[0] == "results" and rest[2] == "runs":
            return "/".join((board, *rest[:4]))
        return None

    moved = {run_of(rel) for rel in held if rel[-1] == MANIFEST and len(split_path(rel)[1]) == 5}
    runs = {run_of(name.split("/")) for name in left} - moved - {None}
    return sorted(runs)


def _remove_source(
    source: Any,
    destination: EvidenceForm,
    held: dict[Rel, Held],
    before: dict[Rel, Any],
    steps: _Progress,
) -> tuple[int, list[str]]:
    """Empty the source folder by the rule of :func:`_still_held`, then its folders.
    Everything else — written, changed, no longer held, undeletable, or a link — stays
    and is listed. ``(removed, left)``.

    This answers a report whatever happens and never raises: the switch is behind us,
    so an exception here would throw away the one thing the user needs, which is the
    list of files that stayed. A destination that cannot be asked (a server that
    refused one read mid-pass) means "keep this file" — already the rule when the
    answer is no.

    The ownership marker goes LAST and only when nothing is left: a folder that still
    holds any of this project's evidence must keep saying whose it is. The pass record
    goes with it, because the move it describes is over.
    """
    root = source.local_root
    steps.phase(len(held) + 1)
    removed = 0
    kept: list[Rel] = []
    for rel in sorted(held):
        src = root.joinpath(*rel)
        try:
            if _still_held(source, destination, rel, held[rel], before[rel]):
                src.unlink()
                removed += 1
            else:
                kept.append(rel)
        except (OSError, EvidenceError) as exc:
            log.warning("evidence file %s kept: %s", src, exc)
            kept.append(rel)
        steps.tick()
    left = _left_in(source, kept)
    if not left:
        for name in (MARKER, CONVERSIONS):
            try:
                (root / name).unlink(missing_ok=True)
            except OSError as exc:
                log.warning("%s was kept in %s: %s", name, root, exc)
    _drop_empty_folders(source.io, root)
    steps.tick()
    if left:
        log.warning("%d evidence file(s) kept in %s after the move", len(left), root)
    return removed, left


# ---------------------------------------------------------------------------
# Move
# ---------------------------------------------------------------------------


def _still_ours(project_root: Path, live: dict[str, Any]) -> None:
    """The project must still name the store this pass is moving out of — read under
    the source's lock when the pass starts, and again right before the switch.

    Another bench that moved this project's evidence while this pass waited for the
    lock has already re-pointed it; switching it back would name a store that bench
    has just emptied.
    """
    if evidence_config.read_spec(project_root) != live:
        raise EvidenceError(
            "invalid",
            f"{project_root.name}'s evidence store was changed elsewhere while its "
            "evidence was moving — nothing was switched and nothing was removed",
        )


def _let_go(source: EvidenceForm) -> None:
    """What the source's form holds for the pass, let go (a web server's copy on this PC)."""
    release = getattr(source, "release", None)
    if release is not None:
        release()


def _record_switch(source: EvidenceForm, project_root: Path, destination: EvidenceForm) -> None:
    """Right before the configuration is written: the source notes that its copy is still to
    be removed (:meth:`EvidenceForm.record_switch`)."""
    source.record_switch(project_root, destination)


def _uploads_follow(source: Any, destination: Any, project_id: str) -> None:
    """Right after the switch, still under the caller's hold on the uploads: what waits
    in this PC's outbox for the store being left is addressed to the new one, so it is
    uploaded there and never into the store being emptied."""
    evidence_outbox_route.redirect(
        source.outbox, project_id, source.io.identity, destination, turn_held=True
    )


def _not_carried(kept: dict[Rel, str]) -> list[str]:
    return [f"{_shown(rel)} ({why})" for rel, why in sorted(kept.items())]


def transfer_evidence(
    project_root: Path,
    source: Any,
    destination: Any,
    target_spec: dict[str, Any],
    *,
    progress: Callable[[int, int], None] | None = None,
    cancel: threading.Event | None = None,
    copy_only: bool = False,
    expected_source_spec: dict[str, Any] | None = None,
) -> TransferReport:
    """Move this project's shared evidence from ``source`` to ``destination`` and
    switch ``config.json`` to ``target_spec``, or copy without switching/deleting
    when ``copy_only`` is set.

    ``progress(done, total)`` reports the rename phase, or ticks per file while
    copying, verifying and removing when a folder rename is unavailable.
    ``cancel`` is honoured between files until the switch: what already reached the
    destination stays there (the next pass skips it), the configuration and the source
    are untouched. After the switch the move completes — a failure there keeps files in
    the source and lists them.
    """
    project_root = Path(project_root)
    live = (
        copy.deepcopy(expected_source_spec)
        if expected_source_spec is not None
        else evidence_config.read_spec(project_root)
    )
    project_id = read_project_id(project_root) or ""
    problem = transfer_problem(project_root, source, destination, project_id=project_id, scan=False)
    if problem is not None:
        raise EvidenceError("invalid", problem)
    if evidence_outbox.pending(source, project_id) and not evidence_outbox_lock.uploads_held(
        source
    ):
        # The caller owes the hold (:class:`evidence_outbox_lock.PushHold`): without it a
        # push could land in the store being left, where nothing would show it again.
        raise EvidenceError(
            "invalid",
            "runs of this project are waiting on this PC to be uploaded and nothing holds "
            "them during the move — nothing was moved",
        )
    src, dst = form_of(source), form_of(destination)
    places = (src.label, dst.label)
    _still_ours(project_root, live)
    if not copy_only and evidence_rename.try_rename(
        project_root, source, destination, target_spec, progress=progress, cancel=cancel
    ):
        _uploads_follow(source, destination, project_id)
        return TransferReport(*places, renamed=True)
    problem = transfer_problem(project_root, source, destination, project_id=project_id)
    if problem is not None:
        raise EvidenceError("invalid", problem)
    cancelled = (
        f"copying {project_root.name}'s evidence was cancelled"
        if copy_only
        else f"moving {project_root.name}'s evidence was cancelled before the switch"
    )

    def check_cancel() -> None:
        if cancel is not None and cancel.is_set():
            raise EvidenceError(
                "cancelled",
                f"{cancelled}: "
                f"the configuration and {places[0]} are unchanged, and what already "
                f"reached {places[1]} is kept for the next pass",
            )

    # The whole pass is held under the SOURCE's own move lock, and the project's entry is
    # re-read under it: "nothing re-pointed the project" is a check-then-write that two
    # benches passed together, each then emptying the source into its own store.
    with contextlib.ExitStack() as held_open, src.move_lock(project_id, cancel):
        held_open.callback(_let_go, src)
        _still_ours(project_root, live)
        try:
            planned = src.walk()
            before = src.stamps(planned)
            dst.prepare(project_id, src.label)
            steps = _Progress(2 * len(planned), progress)
            with tempfile.TemporaryDirectory(prefix="evidence-transfer-") as scratch:
                held, copied, present, kept, derived = _copy(
                    src, dst, planned, Path(scratch), steps, check_cancel
                )
            _verify(dst, held, steps, check_cancel)
            changed = _first_change(before, src.stamps(src.walk()))
            if changed is not None:
                activity = "being copied" if copy_only else "moving"
                retry = "copy" if copy_only else "move"
                raise EvidenceError(
                    "cancelled",
                    f"{src.where(changed)} changed while the evidence was {activity} "
                    f"(a run finished?) — nothing was switched and nothing was removed; {retry} "
                    "it again",
                )
            check_cancel()  # the last point of return: after the switch the move completes
            _still_ours(project_root, live)
            counts = {"copied": copied, "present": present, "not_carried": len(kept)}
            not_carried = _not_carried(kept)
            if copy_only:
                dst.finish(counts)
                return TransferReport(
                    *places,
                    copied,
                    present,
                    source_kept=True,
                    copied_only=True,
                    not_carried=not_carried,
                )
            _record_switch(src, project_root, dst)
            evidence_config.write_spec(project_root, target_spec)
            _uploads_follow(source, destination, project_id)
            dst.finish(counts)
        except OSError as exc:
            raise os_error(exc) from exc

        if src.local_root is None:
            # A source that is not a folder on this PC is left alone: deleting evidence on a
            # server or in the database is an approved delete of its own.
            return TransferReport(*places, copied, present, 0, [], True, not_carried=not_carried)
        _drop_derived(src, derived)
        removed, left = _remove_source(src, dst, held, before, steps)
        stays = {_shown(rel) for rel in kept}  # never carried: listed as such, not as kept
        left = [path for path in left if path not in stays]
        late = _late_runs(left, held)
        if late:
            log.warning(
                "%d run(s) reached %s after the copy and were not moved: %s",
                len(late),
                src.label,
                late,
            )
        return TransferReport(
            *places,
            copied,
            present,
            removed,
            left,
            False,
            late_runs=late,
            not_carried=not_carried,
        )


def _waiting_note(items: list, *, copy_only: bool) -> str:
    """What the question says about this project's runs, artifacts and comments still waiting
    in this PC's outbox (``""``: none)."""
    if not items:
        return ""
    runs, artifacts, comments = evidence_outbox.waiting_counts(items)
    waiting = (
        f"{runs} run(s), {artifacts} artifact(s) and {comments} comment(s) of this project "
        "are still waiting on this PC to be uploaded"
    )
    if copy_only:
        return (
            f"\n\n{waiting}. They are not part of this copy: they will be uploaded to the "
            "store the project uses now."
        )
    return (
        f"\n\n{waiting}: they will be uploaded to the new location after the move. Runs "
        "waiting on other PCs follow once those PCs read the new location in the project "
        "configuration."
    )


def render_question(
    source: str, destination: str, *, copy_only: bool, from_folder: bool, waiting: list
) -> str:
    """The question asked before the move or the copy, the last word before it: from where to
    where (each named as the move's answer names it), what happens, and what waits on this
    PC (*waiting*: its outbox items)."""
    note = _waiting_note(waiting, copy_only=copy_only)
    if copy_only:
        return (
            f"Copy this project's evidence?\n\nFrom:  {source}\nTo:    {destination}\n\n"
            "Files are copied and verified. Existing destination files with the same names are "
            "replaced if different. The original evidence is kept, and the project continues "
            f"using its current store.{note}"
        )
    after = (
        "After copying, the folder it came from is emptied, file by file, of files the new "
        "store provably holds."
        if from_folder
        else "Nothing is deleted on the server it came from."
    )
    return (
        f"Move this project's evidence to another store?\n\nFrom:  {source}\nTo:    "
        f"{destination}\n\nWhere supported, the evidence folder is renamed without copying "
        "files. Otherwise every file is copied and verified (sha256). The project "
        f"configuration is switched to the new store. {after}{note}"
    )


def _not_carried_text(report: TransferReport) -> str:
    if not report.not_carried:
        return ""
    shown = "\n".join(f"  {p}" for p in report.not_carried[:20])
    more = f"\n  … and {len(report.not_carried) - 20} more" if len(report.not_carried) > 20 else ""
    return (
        f"\n\nNot carried — the new store keeps no place for them, so they stay where they "
        f"were ({len(report.not_carried)}):\n{shown}{more}"
    )


def render_transfer(report: TransferReport) -> str:
    """The move's answer in the user's words: what was copied, what was removed and
    what was left."""
    if report.copied_only:
        return (
            f"Evidence copied to:\n{report.destination}\n\n"
            f"Files copied: {report.copied}\n"
            f"Already there: {report.present}\n\n"
            f"The original evidence is unchanged. The project still uses:\n{report.source}"
            + _not_carried_text(report)
        )
    if report.renamed:
        return (
            f"Evidence folder moved to:\n{report.destination}\n\n"
            f"Renamed from:\n{report.source}\n\n"
            "The folder was moved without copying its files."
        )
    text = (
        f"Evidence moved to:\n{report.destination}\n\n"
        f"Files copied: {report.copied}\n"
        f"Already there: {report.present}\n\n"
    )
    if report.source_kept:
        return (
            text
            + (
                f"Left in place, on the server it came from:\n{report.source}\n\n"
                "Nothing was deleted there — removing evidence from a server is its own "
                "operation."
            )
            + _not_carried_text(report)
        )
    if not report.left_behind:
        return (
            text
            + f"Removed from the folder it came from ({report.removed} files):\n{report.source}"
            + _not_carried_text(report)
        )
    if report.late_runs:
        text += (
            f"{len(report.late_runs)} run(s) landed in {report.source} after the copy and were "
            "NOT moved — they are not in the new store:\n"
            + "\n".join(f"  {r}" for r in report.late_runs)
            + "\n\n"
        )
    shown = "\n".join(f"  {p}" for p in report.left_behind[:20])
    more = f"\n  … and {len(report.left_behind) - 20} more" if len(report.left_behind) > 20 else ""
    return (
        text
        + (
            f"Removed from {report.source}: {report.removed}\n"
            f"Kept there — changed or not identical to the copy, never deleted "
            f"({len(report.left_behind)}):\n{shown}{more}"
        )
        + _not_carried_text(report)
    )
