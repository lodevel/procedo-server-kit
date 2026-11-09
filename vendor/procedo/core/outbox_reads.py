"""What this PC's outbox still holds, read as the store will hold it once pushed — the
one seam every run reader goes through: the list badges, Test details and the detail
panel's runs, the report, the serial list.

Without it a run waiting for its upload was invisible: the store's pointer still named
the previous run, so a new FAIL showed as the previous PASS beside "pending upload".
Now this PC's newest pending run is the test's run when it is newer than the store's,
shown with its own verdict, and everything about it — result, console log, pictures,
annotation — is read from its payload on this PC: never from the store, so it shows
with the share down and costs no I/O on it.

Qt-free and layout-free: a store with a local ``outbox`` has pending runs (one without
has none), and everything else is the store's own answer.

The outbox is read ONCE per operation (``ctx.memo``): a badge snapshot of a hundred
tests parses it once, not once per call. A context from :func:`offline_context` answers
from this PC alone — what a view shows when the store could not answer — and never
falls through to the store.

* :func:`current_run` — the test's newest COMPLETE pending run when its push would make
  it current (:func:`evidence_shared_manifest.after_pushes`, the pointer's own rule), so
  what is shown is what the pushes will make current.
* :func:`list_runs`, :func:`is_pending`, :func:`local_path`, :func:`read_run` — the
  history (newest first, pending ones flagged) and the payload of a pending run.
* :func:`read_annotation` / :func:`add_annotation_entries` — a comment or verdict override
  on a pending run (or on a run an earlier save of which still waits) is queued with the
  entries and added to the run's at the push (:func:`evidence_outbox.queue_annotation`).
* :func:`read_board_entries` / :func:`add_board_entries` — a board's lot and fields: every
  save is queued, and added to the board's at the push
  (:func:`evidence_outbox.queue_board_entries`); what the board holds is the store's entries
  then those still queued here.
* :func:`read_file` — one file of a run (result, log, picture) where it is now: this
  PC's copy, or the store's once the run was uploaded mid-read. Every report read uses it.
** :func:`boards`, :func:`queued_artifacts` — a board only a queued entry names yet, and
  this PC's copies of artifacts still waiting.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import evidence_outbox
from .annotation_entries import outcome
from .evidence_shared_layout import pointer_of_test
from .evidence_shared_manifest import after_pushes, annotation_of, run_info
from .evidence_store import (
    CURRENT_RUN,
    RESULT_FILE,
    EvidenceError,
    EvidenceKey,
    EvidenceStore,
    OpContext,
    OutboxItem,
    RunInfo,
    guard_component,
)
from .evidence_tree import name_key

_OFFLINE = "outbox_reads.offline"


def offline_context() -> OpContext:
    """A context whose reads answer from this PC's outbox alone: the store is never asked."""
    ctx = OpContext()
    ctx.memo[_OFFLINE] = True
    return ctx


def is_offline(ctx: OpContext | None) -> bool:
    return ctx is not None and bool(ctx.memo.get(_OFFLINE))


@dataclass(frozen=True)
class _Waiting:
    """One project's outbox as one operation reads it (:func:`evidence_outbox.waiting`),
    indexed once: by board and test id, by run id and by board."""

    runs: list[dict] = field(default_factory=list)  # run entries, newest first
    notes: dict[str, list[dict]] = field(default_factory=dict)  # run_id -> its queued entries
    boards: dict[str, list[dict]] = field(default_factory=dict)  # board -> its queued entries
    items: list[OutboxItem] = field(default_factory=list)
    names: dict[str, str] = field(default_factory=dict)  # run_id -> the name it recorded
    by_test: dict[tuple[str, str], list[dict]] = field(default_factory=dict, init=False)
    by_id: dict[str, dict] = field(default_factory=dict, init=False)
    by_board: dict[str, list[dict]] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        for e in self.runs:
            key = (name_key(e["serial"]), e["test_id"])
            self.by_test.setdefault(key, []).append(e["manifest"])
            self.by_id[e["manifest"]["run_id"]] = e["manifest"]
        for board, entries in self.boards.items():
            self.by_board.setdefault(name_key(board), []).extend(entries)

    def queued(self, board: str, test_id: str) -> list[dict]:
        """The manifests of the pending runs of the test whose hidden id is *test_id*, newest first —
        matched the way the push resolves the folders they land in: the board by
        :func:`evidence_tree.name_key`, the test by its id."""
        return self.by_test.get((name_key(board), test_id), [])

    def manifest(self, run_id: str | None) -> dict | None:
        """A run's manifest while it waits here; ``None`` once it is on the store."""
        return self.by_id.get(run_id or "")

    def board_entries(self, board: str) -> list[dict]:
        """The entries queued for a board, in the order saved — matched as the push
        resolves its folder."""
        return self.by_board.get(name_key(board), [])


def _waiting(store: EvidenceStore | None, project_id: str, ctx: OpContext | None) -> _Waiting:
    """This project's outbox, read once per operation (no ``ctx``: read for this call)."""
    if getattr(store, "outbox", None) is None:
        return _Waiting()
    memo = ctx.memo if ctx is not None else {}
    key = ("outbox_reads", project_id, id(store))
    if key not in memo:
        memo[key] = _Waiting(*evidence_outbox.waiting(store, project_id))
    return memo[key]


def current_run(
    store: EvidenceStore, project_id: str, board: str, test_id: str, ctx: OpContext | None = None
) -> RunInfo | None:
    """The run ``test_id`` shows: the pending run of this PC the pushes will leave current
    (:func:`evidence_shared_manifest.after_pushes`, in push order onto the pointer they will
    meet — offline, onto none), else the store's current run."""
    queued = _waiting(store, project_id, ctx).queued(board, test_id)  # newest first
    if not queued:
        return None if is_offline(ctx) else store.current_run(project_id, board, test_id, ctx=ctx)
    ptr = None if is_offline(ctx) else _pointer(store, project_id, board, test_id, ctx)
    winner = after_pushes(ptr, [run_info(m) for m in reversed(queued)])
    if winner is not None or is_offline(ctx):
        return winner
    return store.current_run(project_id, board, test_id, ctx=ctx)


def _pointer(
    store: EvidenceStore, project_id: str, board: str, test_id: str, ctx: OpContext | None
) -> dict | None:
    """The test's pointer as the pushes will meet it: a store that keeps it as a record of its
    own reads it (``read_pointer``: the evidence database's row), a folder layout from its
    ``current.json``."""
    read = getattr(store, "read_pointer", None)
    if read is not None:
        return read(project_id, board, test_id, ctx=ctx)
    return pointer_of_test(store, project_id, board, test_id)


def list_runs(
    store: EvidenceStore, project_id: str, board: str, test_id: str, ctx: OpContext | None = None
) -> list[tuple[RunInfo, bool]]:
    """``(run, pending)`` for the store's runs of ``test_id`` in the store's order — with
    this PC's pending runs among them, all newest first by ``run_id`` (its start), as every
    store orders them (``data_format.md`` §6.2: an incomplete run sits where its start puts
    it)."""
    pending = [(run_info(m), True) for m in _waiting(store, project_id, ctx).queued(board, test_id)]
    if is_offline(ctx):
        return pending
    ids = {r.run_id for r, _ in pending}  # pushed, its entry not yet dropped: one row
    stored = [
        (r, False)
        for r in store.list_runs(project_id, board, test_id, ctx=ctx)
        if r.run_id not in ids
    ]
    if not pending:
        return stored
    return sorted(pending + stored, key=lambda rp: rp[0].run_id, reverse=True)


def is_pending(store: EvidenceStore, key: EvidenceKey, ctx: OpContext | None = None) -> bool:
    return _waiting(store, key.project_id, ctx).manifest(key.run_id) is not None


def local_path(store: EvidenceStore, key: EvidenceKey, ctx: OpContext | None = None) -> Path:
    """``store.local_path``, except that a pending run is its payload on this PC."""
    if key.kind == "run" and is_pending(store, key, ctx):
        d = store.outbox / key.run_id
        return d / guard_component(key.name) if key.name else d
    if is_offline(ctx):
        raise EvidenceError("not_found", f"{key.run_id} is not waiting on this PC")
    return store.local_path(key, ctx=ctx)


def read_file(
    store: EvidenceStore | None,
    path: Path,
    keys: Iterable[EvidenceKey | None] = (),
    ctx: OpContext | None = None,
) -> bytes:
    """One file of a run — a result, a log, a picture — read where it is now: ``path`` as
    a snapshot handed it out (this PC's copy of a pending run, or where the store keeps
    the run), else, when this PC's copy vanished because the run was just uploaded, the
    store's copy of the same run at the same relative path (a generation never changes,
    so its run id still finds it). ``keys``: the runs ``path`` may belong to. Never waits
    for an upload: a report reads a pending run from this PC, share down or not."""
    try:
        return path.read_bytes()
    except FileNotFoundError:
        moved = None if is_offline(ctx) else _uploaded_copy(store, path, keys, ctx)
        if moved is None:
            raise
    return moved.read_bytes()


def _uploaded_copy(
    store: EvidenceStore | None,
    path: Path,
    keys: Iterable[EvidenceKey | None],
    ctx: OpContext | None,
) -> Path | None:
    """Where the store keeps ``path`` now, for a file of a pending run's payload whose run
    has been uploaded since (its entry is gone); ``None`` for any other path."""
    if getattr(store, "outbox", None) is None:
        return None
    for key in keys:
        if key is None or key.run_id in (None, CURRENT_RUN):
            continue
        payload = store.outbox / key.run_id
        if path.is_relative_to(payload):
            if is_pending(store, key, ctx):
                return None  # still waiting here: the file is really missing
            return store.local_path(replace(key, name="")) / path.relative_to(payload)
    return None


def read_run(
    store: EvidenceStore,
    project_id: str,
    board: str,
    test_id: str,
    run_id: str,
    ctx: OpContext | None = None,
) -> dict:
    """``store.read_run``, except that a pending run's result is read from this PC."""
    if _waiting(store, project_id, ctx).manifest(run_id) is not None:
        path = store.outbox / run_id / RESULT_FILE
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            pass  # pushed while we looked — or it never had one: the store says which
        except (OSError, ValueError) as exc:
            raise EvidenceError("corrupt", f"unreadable result {path}: {exc}") from exc
    if is_offline(ctx):
        raise EvidenceError("not_found", f"no result of {run_id} on this PC")
    return store.read_run(project_id, board, test_id, run_id, ctx=ctx)


def read_annotation(
    store: EvidenceStore, key: EvidenceKey, ctx: OpContext | None = None
) -> dict | None:
    """The run's annotation (:func:`evidence_shared_manifest.annotation_of`): a pending run's
    from this PC (the entries queued for it, the execution block from its manifest); a run
    on the store with saves still queued shows its entries, then those, as the push will
    add them."""
    run_id = key.run_id or CURRENT_RUN
    waiting = _waiting(store, key.project_id, ctx)
    queued, m = waiting.notes.get(run_id, []), waiting.manifest(run_id)
    if m is not None:
        return annotation_of(m, list(queued))
    if is_offline(ctx):
        return None
    stored = store.read_run_annotation(
        key.project_id, key.board, key.test_id or "", run_id, ctx=ctx
    )
    if not queued or stored is None:
        return stored
    return {**stored, "entries": [*stored["entries"], *queued]}


def add_annotation_entries(store: EvidenceStore, key: EvidenceKey, added: list[dict]) -> list[dict]:
    """Add one save's annotation entries to the run (:mod:`annotation_entries`) — queued with
    the outbox entries while the run waits here, or while an earlier save of it does (so a
    later save never lands before it). Per entry, what the store answered
    (:func:`annotation_entries.outcome`); a queued one answers against what this PC
    holds, the store applying the same rule when it arrives."""
    run_id = key.run_id or CURRENT_RUN
    waiting = _waiting(store, key.project_id, None)
    if waiting.manifest(run_id) is not None or run_id in waiting.notes:
        name = waiting.names.get(run_id, "")
        evidence_outbox.queue_annotation(
            store, key.project_id, key.board, key.test_id or "", run_id, added, name=name
        )
        held = list(waiting.notes.get(run_id, []))
        answers = []
        for e in added:
            answers.append(outcome(held, e))
            held.append(e)
        return answers
    return store.add_run_annotation_entries(
        key.project_id, key.board, key.test_id or "", run_id, added
    )


def read_board_entries(
    store: EvidenceStore, project_id: str, board: str, ctx: OpContext | None = None
) -> list[dict]:
    """A board's lot and fields as entries (:func:`annotation_entries.board_data` reads them):
    the store's in the order received, then those still queued here in the order saved, as
    the push will add them; offline, the queued ones alone."""
    queued = _waiting(store, project_id, ctx).board_entries(board)
    if is_offline(ctx):
        return list(queued)
    return [*store.read_board_entries(project_id, board, ctx=ctx), *queued]


def add_board_entries(
    store: EvidenceStore, project_id: str, board: str, added: list[dict], seen: list[dict]
) -> list[dict]:
    """Queue one save's board entries for their push (:func:`evidence_outbox.queue_board_entries`):
    always queued, as a run is, so a bench saves a lot with the store out of reach — before
    the board's first run too — and the push creates the board when it is new. Per entry,
    the answer against ``seen``, the board's entries as this person read them
    (:func:`read_board_entries`); the store applies the same rule when they arrive, keeping
    one that arrives after someone else's in the history, not current."""
    evidence_outbox.queue_board_entries(store, project_id, board, added)
    held = list(seen)
    answers = []
    for e in added:
        answers.append(outcome(held, e))
        held.append(e)
    return answers


def boards(store: EvidenceStore, project_id: str, ctx: OpContext | None = None) -> list[str]:
    """The project's boards on the store, and a board only an entry waiting here names."""
    found = set(store.list_boards(project_id, ctx=ctx))
    found.update(i.board for i in _waiting(store, project_id, ctx).items if i.board)
    return sorted(found)


def queued_artifacts(
    store: EvidenceStore | None, kind: str, project_id: str, board: str
) -> dict[str, Path]:
    """``name -> this PC's copy`` of the ``kind`` artifacts of ``board`` still waiting for
    their upload — what the Report Browser lists as pending and opens, and the report
    a generation queued."""
    if getattr(store, "outbox", None) is None:
        return {}
    return evidence_outbox.pending_artifacts(store, kind, project_id, board)
