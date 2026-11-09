"""The listing index: a cache with detectable gaps, never a source of truth.

Listing a board's reports reads **one sidecar per report** — free on a filesystem
root, a round trip on a fresh data connection over FTPS. The index removes that
sweep without ever becoming something a reader has to trust:

* **Per-machine shards.** Each bench APPENDS to its own ``<root>/index/<machine_id>.jsonl``
  and to no other, so there is no read-modify-write and no lost update between
  benches. One JSON object per line, one line per artifact, carrying what the
  browser lists (:class:`evidence_store.ArtifactInfo`) plus the facts gap detection
  compares (both lengths and the sidecar's modification time), the moment the line
  was observed (``at``) and the machine that wrote it.
* **Records are a byproduct.** A line is written from bytes already in hand — the
  sidecar a listing had to read anyway, or the sidecar a write was already
  committing — never by a sweep of its own.
* **Reconciliation with gap detection.** A reader concatenates every shard, then
  lists the artifact folder ONCE per board and walks the listing: an entry the
  shards do not describe *exactly* is fetched from its sidecar, returned AND
  appended; a record whose file is not in the listing is dropped from the result.
  So staleness is *detectable* rather than assumed, a failed append self-corrects on
  the next pass, and the index can never hide a report.
* **The shards answer first.** :func:`known` serves what they already describe with
  no listing and no sidecar read, so a view paints rows before the reconciliation that
  corrects them has run — and a board whose own listing is refused keeps exactly those
  rows, marked stale, instead of going blank.
* **Time decides, never filename order.** Where two benches hold a record for the
  same artifact the greater ``at`` wins (ties by shard name). Sorting by shard name
  alone let a bench whose name sorts last pin a superseded verdict for ever — a
  report republished under its own name, which the report dialog supports, showed
  PASS on every bench while FAIL sat in another shard.
* **A failed append never fails a listing.** The index is a cache: a read-only
  store, a full disk or a refused ``APPE`` costs the next pass one sidecar read,
  not the listing. A CANCELLED one is not swallowed — the user stopped it.
* **A refused READ raises.** The shard is store data and obeys the store's
  rules: a denied ``index/`` listing or shard read is reported, never read as "no
  reports" — the mistake the contract's refused-listing rule exists to prevent.

**What counts as "still described"** (:func:`_matches`): both lengths *and* the
sidecar's modification time must agree, so a report republished with an identical
payload and sidecar length is still re-read; and the sidecar must be strictly OLDER
than the start of the listing that read it, because ``MLSD``'s ``modify`` fact has
one-second resolution and a sidecar rewritten within the second of the read that
recorded it is indistinguishable from the one that was read. That bounds the doubt
to artifacts touched in the same second as a listing. A record written by a WRITER
carries no observed modification time, so it is a provisional row only. The first
authoritative listing reads its sidecar; later unchanged listings reuse that verified
record. A delayed writer can therefore never make superseded bytes authoritative.

**Where there is no trustworthy listing** (an FTPS server without ``MLSD``:
``"query"`` leaves ``CAPABILITIES`` the first time a listing is refused) gap
detection is impossible, so reads come from the shards alone and the store says its
listings are unreliable (:func:`reliable`). Consequences, decided here:

* A name **no shard has ever seen is not listed, and stays unlisted**. It remains
  readable by name — ``read_artifact`` and ``local_path`` need no listing — but a
  point read fetches the payload, not the sidecar, so it has nothing to record and
  deliberately records nothing: buying a listing row with an extra round trip on
  every read would pay the cost the index exists to remove. The row appears when a
  bench that CAN list reconciles, or when the writer records it.
* Shards are DISCOVERED by listing ``index/``, which such a server cannot do
  either, so a bench reads **only its own shard** there. Such a store is therefore
  a per-machine cache, not a shared one — and, as built today, it is read-only in
  any case: creating a folder needs a listing, so nothing reaches a server without
  ``MLSD`` for the index to describe (recorded against the design, not fixed here).
* A shard line this build cannot read (a torn append, or a future ``v``) makes that
  pass **incomplete**. On a store with a listing that is harmless — gap detection
  puts the artifact back. Where the shards are the only answer it is reported as
  ``corrupt`` rather than silently shortening the list, which is the refused-listing
  hazard by another door.

**Growth and compaction — decided: shards are never compacted.** A line is written
only when an artifact is genuinely new or genuinely changed, so a shard grows with
real change, not with listings (about 200 bytes a line: 22 reports ≈ 4 KB, and a
project that rewrote every report daily for a year ≈ 1.6 MB, still one read). A
rewrite would reintroduce exactly what per-machine shards exist to avoid — a
read-modify-write, needing this module's lock plus a durable temp-and-replace, and
losing this bench's history if it were interrupted. The cost of not compacting is one
small read per shard per reconciliation pass; that is the trade, and it is stated in
``docs/plans/evidence_store_design.md`` too.

**Recorded in 5b-2, not fixed.** A tree's ``has_mlsd`` latches True->False for its whole
lifetime (5a-2), so ONE transient refusal puts :data:`UNRELIABLE` on every later listing
of that store until the app restarts. Shards are never collected, so on a store with no
listing the row of a permanently deleted report is permanent — there is nothing to notice
the deletion with. Both are bounded by the fact that such a store is read-only as built
(above). The stale concept now lives in three places: :data:`UNRELIABLE` here, the
per-board reason in ``evidence_service._stale_reason``, and the report browser's own
"never reached" sentences. This module is otherwise Qt-free protocol code carrying no
view strings at all, and ``evidence_channel.describe_error`` is where every other one lives
— so if this is ever carved, ``doubt`` / :data:`UNRELIABLE` / ``known`` / ``known_rows``
/ ``_from_shards`` are the natural seam and that is where they go.

Only ``report`` is *listed* through the index. An export listing reads no sidecar —
its rows are the directory listing itself — so there is nothing to cache and a
record would only add a way to be wrong; a draft is rewritten by name on every save,
which is the one shape a name-keyed cache must not hold. The record carries ``kind``
so a second kind can be added without a format change — and one was (below).

**Search foundations (2026-09-22): a ``run`` record, and a ``test`` field.** A
published run appends ONE line, :data:`RUN`, to this bench's shard — the same file
reports already share — carrying ``serial`` (the board's key, as in every record),
``test`` (the name its test had when it ran), ``test_id`` (the test's hidden id, which the
run is filed under), ``name`` (the run id),
``verdict``, ``ended`` and ``operator`` (:func:`run_record`,
:func:`note_run_published`). ``_parse`` checks ``v == SCHEMA`` and every reader that
LISTS filters by kind (``known`` / ``reconcile`` / ``_from_shards`` always ask for
:data:`REPORT`), so a run line sits in the same shard a report-listing reader reads and is
skipped by it. A report's record also carries ``test``: the test ids ITS OWN sidecar
covers (``report["tests"][*]["name"]``, verbatim, ``[]`` when the sidecar names none
— see :data:`evidence_store.ArtifactInfo.tests`). The two kinds' ``test`` fields are
different SHAPES on purpose — a list for a report that may cover several, one string
for a run that belongs to exactly one — because ``kind`` already tells a reader which
it is holding; a report and a run are never compared against the same ``test`` value
without knowing which kind produced it. Documented in ``docs/plans/evidence_store_design.md``
§"Index shards".

A run is recorded when it REACHES the store: when :mod:`evidence_outbox` lands its
push (``_push_run``) — every write goes through the outbox — exactly as an artifact's
line follows its own push. Never while it is still waiting in the outbox.

**A delete and a restore are lines too** (``data_format.md`` §6.2, ``deleted=exclude`` by
default): a run or a report a manager moves into the evidence trash gets a newer line under
the same key carrying ``"deleted": true`` (:func:`trash_line`, written by
:mod:`evidence_trash`), and its restore a newer line without it, so the search leaves a
deleted item out unless asked and the latest line still decides. A deleted line never
describes a listed artifact (:func:`_matches`), and the shards alone never list one
(:func:`_from_shards`).

:mod:`evidence_search`'s shard query merges the SAME shard bytes with the SAME
precedence (latest ``at`` wins, shard name breaking a tie) for a caller with no
open store at all — a bare folder, no tree, no ownership check. That reader lives
there, not here: this module's own protocol code is scanned by
``tests/test_evidence_tree_seam.py`` for I/O that bypasses the tree, and a
store-free reader belongs beside the code that has no store to bypass one with,
not inside the seam that enforces there is always one.

Qt-free, stdlib only. Every store read and write goes through the store's tree
(``store.io`` / ``io``), which ``tests/test_evidence_tree_seam.py`` enforces.
Design: ``docs/plans/evidence_store_design.md``, "Listing, index and latency".
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import PurePath
from typing import Any

from .evidence_board import key_of_path
from .evidence_owner import check_owner, mkdir_owned
from .evidence_store import ArtifactInfo, EvidenceError, OpContext, safe_component
from .evidence_store_fs import _sidecars, os_error
from .evidence_tree import match_children, name_key, read_json

log = logging.getLogger(__name__)

INDEX = "index"  # the shard folder beside the boards (a reserved sibling)
SUFFIX = ".jsonl"
SCHEMA = 1
REPORT = "report"
RUN = "run"  # a published run's own line — never listed through KINDS (see module docstring)
KINDS: tuple[str, ...] = (REPORT,)  # the kinds whose listing reads a sidecar
PATTERN = "*.html"  # what a report listing matches
ABSENT = -1  # a length or time that is not there (no sidecar, or an unreadable entry)
DELETED = "deleted"  # a line's flag: the item is in the evidence trash (:func:`trash_line`)

#: Why rows from a store with no trustworthy listing can be stale although nothing
#: failed: gap detection needs a listing, so a report DELETED on such a server keeps the
#: record that describes it and keeps being listed. A view says this rather than
#: presenting those rows as what is on the store (see :func:`doubt`).
UNRELIABLE = (
    "showing what the evidence index recorded — this store cannot list its folders, "
    "so a report deleted elsewhere can still appear here"
)

Key = tuple[str, str, str]  # (kind, resolved board key, name)
Facts = tuple[int, int, int]  # (payload size, sidecar size, sidecar mtime)

#: Appending is one seek-then-write on Windows, not an atomic operation, so two
#: threads of THIS process appending to one shard lose records (measured: 4 threads x
#: 300 records produced 1064 of 1200). Three service workers, the warm-up and an
#: inline ``write_artifact`` all reach this, so the lock is not theoretical. Two
#: PROCESSES on one machine remain a documented risk that gap detection self-corrects.
_APPEND_LOCK = threading.Lock()


@dataclass(frozen=True)
class Shards:
    """One reconciliation pass's view of the index.

    ``usable`` is False when ``<root>/index`` is not this module's folder at all (a
    board of the project folds to the reserved name), in which case the index
    disables itself for that store rather than writing into someone's evidence.
    ``complete`` is False when a line could not be read.
    """

    records: dict[Key, dict] = field(default_factory=dict)
    usable: bool = True
    complete: bool = True


# ---------------------------------------------------------------------------
# Shard location, and the store's own answer about its listings
# ---------------------------------------------------------------------------


def reliable(store: Any) -> bool:
    """Whether this store's LISTINGS can be trusted — the store's own answer.

    A backend that cannot enumerate authoritatively drops ``"query"`` from its
    capabilities (the FTPS one does so the first time a server answers "command not
    understood" to ``MLSD``), and gap detection needs a listing it can trust.
    """
    return "query" in store.CAPABILITIES


def kept(store: Any) -> bool:
    """Whether *store* keeps this index: a store over a folder tree does; one that lands each
    entry itself (``send_entry``: the database, the web server) answers its listings from its
    own records and keeps none, so there are no shards to read."""
    return getattr(store, "send_entry", None) is None


def doubt(store: Any) -> str:
    """:func:`reliable` in the words a view shows, or ``""`` when there is no doubt.

    A store whose listings are trustworthy has none to report: every row it returns was
    compared against the folder it came from. Where they are not trustworthy the rows
    are the shards alone, and :data:`UNRELIABLE` says what that costs them.
    """
    return "" if reliable(store) else UNRELIABLE


def folder(store: Any, project_id: str) -> PurePath:
    """``<evidence folder>/index`` — never renamed, never a board (reserved)."""
    return store._namespace(project_id) / INDEX


def own_shard(store: Any) -> str:
    """This bench's shard file name; the only one it ever writes to."""
    return f"{safe_component(store.machine_id)}{SUFFIX}"


def _shard_names(store: Any, where: PurePath) -> tuple[list[str], bool]:
    """``(shards to read, this folder is ours)``.

    Where the listing is trustworthy the shards are the folder's own content, so one
    bench reads every other bench's lines. Where it is not, the folder cannot be
    enumerated at all and the answer is this bench's own shard, addressed by NAME — a
    read that needs no listing. Anything in the folder that is not a shard means the
    folder is not ours (see :class:`Shards`).
    """
    io = store.io
    try:
        names = io.list_names(where)
    except OSError as exc:
        if reliable(store):
            raise os_error(exc) from exc  # a refused read is reported, never "no reports"
        names = None
    if names is None:
        return ([] if reliable(store) else [own_shard(store)]), True
    if any(not n.endswith(SUFFIX) for n in names):
        return [], False
    return sorted(names), True


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


def _report_tests(report: dict) -> tuple[str, ...]:
    """The test ids a report's OWN sidecar covers — ``report["tests"][*]["name"]``,
    verbatim, in order; ``()`` when there is no list to read (a report written
    before this field existed, or one whose sidecar names none)."""
    tests = report.get("tests")
    if not isinstance(tests, list):
        return ()
    return tuple(
        t["name"]
        for t in tests
        if isinstance(t, dict) and isinstance(t.get("name"), str) and t["name"]
    )


def info_of_report(name: str, report: dict) -> ArtifactInfo:
    """The row a report's sidecar stands for — the ONE rule, used by the listing that
    reads the sidecar (``SharedLayout._report_info``), by the record written from it
    and by the record read back.

    Values are carried VERBATIM: a sidecar holding ``null`` or a number rendered
    ``'None'`` from a shard and blank from a sidecar read while these were two
    implementations, which is a warm row disagreeing with a cold one over the same
    bytes — exactly what a cache may never do.
    """
    meta = report.get("metadata") or {}
    overrides = report.get("verdict_overrides") or {}
    if not isinstance(meta, dict):
        meta = {}
    verdict = (
        overrides["overall"]
        if isinstance(overrides, dict) and "overall" in overrides
        else meta.get("overall_verdict", "")
    )
    return ArtifactInfo(
        name=name,
        generated=meta.get("generated", ""),
        scenario=meta.get("scenario", ""),
        operator=meta.get("operator", ""),
        overall_verdict=verdict,
        machine_id="",
        tests=_report_tests(report),
    )


def _record(
    kind: str,
    board: str,
    info: ArtifactInfo,
    facts: Facts,
    machine: str,
    *,
    observed_at: int | None = None,
) -> dict:
    size, meta_size, mtime = facts
    return {
        "v": SCHEMA,
        "at": int(time.time()) if observed_at is None else observed_at,
        "kind": kind,
        "serial": board,
        "name": info.name,
        "size": size,
        "meta_size": meta_size,
        "mtime": mtime,
        "generated": info.generated,
        "scenario": info.scenario,
        "operator": info.operator,
        "overall_verdict": info.overall_verdict,
        "machine_id": machine,
        "test": list(info.tests),
    }


def run_record(
    board: str,
    test: str,
    test_id: str,
    run_id: str,
    verdict: str,
    ended: str,
    operator: str,
    machine: str,
) -> dict:
    """The line :func:`note_run_published` appends: one per committed run, keyed
    like every other record by ``(kind, board, name)`` — ``name`` is the run id
    here, exactly as it is the report's file name for :data:`REPORT`, so ranking
    and merging need no kind-specific rule (:func:`_key`, :func:`_at` unchanged).

    ``test`` is the name the ONE test this run belongs to had when it ran, as its manifest
    recorded it (a run record's ``test`` is a string; a report's is a list — see the module
    docstring); ``test_id`` that test's hidden id, the folder the run is filed under."""
    return {
        "v": SCHEMA,
        "at": int(time.time()),
        "kind": RUN,
        "serial": board,
        "name": run_id,
        "test": test,
        "test_id": test_id,
        "verdict": verdict,
        "ended": ended,
        "operator": operator,
        "machine_id": machine,
    }


def note_run_published(
    store: Any,
    project_id: str,
    board: str,
    test: str,
    test_id: str,
    run_id: str,
    verdict: str,
    ended: str,
    operator: str,
    ctx: OpContext | None = None,
) -> None:
    """Record a run that just REACHED the store — the outbox push that lands it (see
    the module docstring), through :func:`note_records`."""
    record = run_record(
        board, test, test_id, run_id, verdict, ended, operator, machine=store.machine_id
    )
    note_records(store, project_id, [record], ctx)


def note_records(
    store: Any, project_id: str, records: list[dict], ctx: OpContext | None = None
) -> None:
    """Append *records* about items already on the store (a run pushed, an item deleted or
    restored).

    Skipped, like every other write into ``index/``, on a store whose reserved
    folder is somebody's real board (:class:`Shards.usable` — the store already
    disables the whole index there rather than writing into somebody's evidence);
    the item itself is still stored, only its shard search row is the cost.

    A refused or unreachable read of the EXISTING shards is logged and swallowed,
    never raised: by the time this runs the item is already durably stored, and
    an index-only failure must not turn that success into an exception (a
    cancelled context is the one exception :func:`append_records` itself already
    re-raises, and this stays consistent with that same contract rather than a
    second one).
    """
    if not records:
        return
    try:
        usable = shards(store, project_id, ctx).usable
    except EvidenceError as exc:
        if exc.kind == "cancelled":
            raise
        log.warning("Evidence index: %d record(s) not recorded (%s)", len(records), exc)
        return
    except OSError as exc:
        log.warning("Evidence index: %d record(s) not recorded (%s)", len(records), exc)
        return
    if usable:
        append_records(store, project_id, records, ctx)


def trash_line(
    store: Any, project_id: str, what: str, place: PurePath, *, deleted: bool = False
) -> dict | None:
    """The line saying a run's folder or a report's file at *place* was moved into the
    evidence trash (``deleted``), or restored: what its own line said, read from the item
    where it is now, under its resolved board and its test's id. ``None`` for what the index holds no
    line for (an export, a draft) and for an item that cannot be read (a cache, never a
    failure of the delete or the restore)."""
    ns = store._namespace(project_id)
    try:
        if what == RUN:
            m = read_json(store.io, place / "manifest.json")
            if not m:
                return None
            record = run_record(
                key_of_path(ns, place.parents[3]),
                m.get("test", ""),
                place.parents[1].name,  # the test's id: the folder the run is filed under
                place.name,
                m.get("verdict", ""),
                m.get("ended", ""),
                m.get("operator", ""),
                machine=store.machine_id,
            )
        elif what == REPORT:
            info = store._report_info(place)
            facts = (ABSENT, ABSENT, ABSENT)  # a hint only: a listing reads its sidecar again
            record = _record(
                REPORT, key_of_path(ns, place.parent.parent), info, facts, store.machine_id
            )
        else:
            return None
    except (OSError, EvidenceError) as exc:
        log.warning("Evidence index: %s %s not recorded (%s)", what, place.name, exc)
        return None
    return {**record, DELETED: True} if deleted else record


def _info(rec: dict) -> ArtifactInfo:
    """The row a shard line stands for. ``machine_id`` stays EMPTY, exactly as a
    sidecar read produces it: the record's own ``machine_id`` is the bench that
    wrote the LINE, which is not what the browser shows. No coercion — see
    :func:`info_of_report`. Callers only ever hand this a :data:`REPORT` record
    (every kind-filtered reader asks for that kind alone), so ``test`` is read as
    the list it is there."""
    return ArtifactInfo(
        name=rec.get("name", ""),
        generated=rec.get("generated", ""),
        scenario=rec.get("scenario", ""),
        operator=rec.get("operator", ""),
        overall_verdict=rec.get("overall_verdict", ""),
        machine_id="",
        tests=tuple(t for t in (rec.get("test") or []) if isinstance(t, str) and t),
    )


def _parse(line: str) -> dict | None:
    """One shard line, or ``None`` when this build cannot read it — a torn append, or
    a record written by a newer version. The caller marks the pass incomplete."""
    try:
        rec = json.loads(line)
    except json.JSONDecodeError:
        return None
    return rec if isinstance(rec, dict) and rec.get("v") == SCHEMA else None


def _key(rec: dict) -> Key | None:
    kind, board, name = rec.get("kind"), rec.get("serial"), rec.get("name")
    if not all(isinstance(v, str) and v for v in (kind, name)) or not isinstance(board, str):
        return None
    return (kind, board, name)  # type: ignore[return-value]


def _at(rec: dict) -> int:
    value = rec.get("at")
    return value if isinstance(value, int) else 0


def _matches(rec: dict, facts: Facts) -> bool:
    """Whether ``rec`` still describes what the listing just reported.

    Both lengths must agree, and the sidecar's modification time must agree with the
    one recorded — a reader records it, so a republished report whose payload and
    sidecar happen to keep their lengths is still caught. A WRITER's record carries
    :data:`ABSENT` there and is only a hint: another writer may already have replaced
    the bytes before that record was appended. A reader's record must describe a
    sidecar strictly older than the start of its read, to reject ambiguous changes
    within the timestamp's one-second resolution.
    """
    size, meta_size, mtime = facts
    if rec.get(DELETED) or (rec.get("size"), rec.get("meta_size")) != (size, meta_size):
        return False
    held = rec.get("mtime")
    if held == ABSENT or held != mtime:
        return False
    return mtime != ABSENT and mtime < _at(rec)


def sizes_of(data: bytes, sidecar: bytes | None) -> Facts:
    """The facts a WRITER records: the lengths it commits, and no modification time
    (it would have to read back what it just wrote — see :func:`_matches`)."""
    return len(data), len(sidecar) if sidecar is not None else ABSENT, ABSENT


def from_sidecar(
    kind: str, board: str, name: str, sidecar: bytes | None, facts: Facts, machine: str
) -> dict | None:
    """The record a WRITER contributes, read from the sidecar bytes it is already
    committing — the same rule a reader applies to the same bytes, so the two can
    never disagree.

    ``None`` (no record, left to gap detection) for a kind that is not indexed, and
    for a sidecar that names no ``generated``: a reader would fall back to the
    file's modification time there, which a writer does not have, and a record that
    says something different from what the sidecar read would say is worse than no
    record at all.
    """
    if kind not in KINDS or not sidecar:
        return None
    try:
        report = json.loads(sidecar.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(report, dict):
        return None
    info = info_of_report(name, report)
    return _record(kind, board, info, facts, machine) if info.generated else None


# ---------------------------------------------------------------------------
# Reading and appending
# ---------------------------------------------------------------------------


def _memo(ctx: OpContext | None, key: tuple, build: Callable[[], Any]) -> Any:
    """``build()``, once for the operation ``ctx`` belongs to.

    The service hands ONE context to every call of a walk, so this is what makes the
    shards parse once per reconciliation pass instead of once per board — a five-board
    warm walk read every shard five times before. A caller with no context (a writer,
    a bare probe) simply does the work."""
    if ctx is None:
        return build()
    if key not in ctx.memo:
        ctx.memo[key] = build()
    return ctx.memo[key]


def _stop(ctx: OpContext | None) -> None:
    """Raise where the operation was cancelled.

    The service cancels a walk BETWEEN boards; this is what makes one board's own pass
    stop too, so a cold walk over a board holding many reports stops reading sidecars
    when the user presses Cancel instead of finishing the board first. A cancellation is
    REPORTED, never swallowed into a short listing — a listing that quietly loses rows is
    the hazard the refused-listing rule exists to prevent.
    """
    if ctx is not None and ctx.cancel.is_set():
        raise EvidenceError("cancelled", "the evidence listing was cancelled")


def shards(store: Any, project_id: str, ctx: OpContext | None = None) -> Shards:
    """Every shard concatenated, once per operation (see :func:`_memo`)."""
    return _memo(ctx, ("evidence-index", project_id), lambda: read_shards(store, project_id))


def read_shards(
    store: Any, project_id: str, fetch: Callable[[PurePath], bytes] | None = None
) -> Shards:
    """Read and merge the shards. Precedence is ``at`` — the moment a line was
    written — with the shard name breaking a tie, never the shard name alone. A
    REFUSED read raises; an absent shard is simply no lines (this bench's own does not
    exist until it writes one). *fetch* reads one shard's bytes as ``io.read_bytes`` does
    (the default): a search over a server reads them through this PC's copy
    (:mod:`evidence_shard_cache`)."""
    where = folder(store, project_id)
    names, usable = _shard_names(store, where)
    if not usable:
        return Shards({}, usable=False, complete=False)
    fetch = fetch or store.io.read_bytes

    def read(shard: str) -> bytes | None:
        try:
            return fetch(where / shard)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise os_error(exc) from exc

    return merge(names, read)


def merge(names: list[str], read: Callable[[str], bytes | None]) -> Shards:
    """The shards *names* (``read(name)``: a shard's bytes, ``None`` when it is not there) as
    one view: the format's ONE reader, whoever fetched the bytes — a store's tree here, a bare
    folder for :func:`evidence_search.read_folder_shards`. Precedence is ``at``, the shard name
    breaking a tie; a line this build cannot read marks the view incomplete."""
    ranked: dict[Key, tuple[tuple[int, str], dict]] = {}
    complete = True
    for shard in names:
        raw = read(shard)
        if raw is None:
            continue
        for text in raw.decode("utf-8", "replace").splitlines():
            text = text.strip()
            if not text:
                continue
            rec = _parse(text)
            key = _key(rec) if rec is not None else None
            if rec is None or key is None:
                complete = False  # a torn append, or a record this build cannot read
                continue
            rank = (_at(rec), shard)
            if key not in ranked or rank >= ranked[key][0]:
                ranked[key] = (rank, rec)
    return Shards({k: rec for k, (_rank, rec) in ranked.items()}, usable=True, complete=complete)


def _ensure(store: Any, project_id: str, where: PurePath) -> bool:
    """The shard folder, created like every other folder this project's evidence goes
    into: through the ownership marker. A bare ``_mkdir_safe`` here appended this
    bench's shard INTO a folder another project owns — the one write on the LISTING
    path, and the quietest of all of them."""
    mkdir_owned(store, project_id, where)
    return True


def append_records(
    store: Any, project_id: str, records: list[dict], ctx: OpContext | None = None
) -> None:
    """Add ``records`` to THIS bench's shard, in one append.

    Never a rewrite, so no other bench's lines can be lost and no reader sees a
    half-written file; under :data:`_APPEND_LOCK`, because appending is not atomic on
    Windows. A failure is logged and swallowed: the index is a cache, and a store that
    refuses the write must still list its reports — the next pass finds the same gap
    and tries again. A CANCELLED write is re-raised: the user stopped it, and that is
    not a cache miss.
    """
    if not records:
        return
    payload = b"".join(
        json.dumps(r, ensure_ascii=True, sort_keys=True).encode("utf-8") + b"\n" for r in records
    )
    where = folder(store, project_id)
    try:
        _memo(ctx, ("evidence-index-dir", project_id), lambda: _ensure(store, project_id, where))
        with _APPEND_LOCK:
            store.io.append_bytes(where / own_shard(store), payload)
    except EvidenceError as exc:
        if exc.kind == "cancelled":
            raise
        log.warning("Evidence index: %d record(s) not appended (%s)", len(records), exc)
    except OSError as exc:
        log.warning("Evidence index: %d record(s) not appended (%s)", len(records), exc)


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


def _stat(io: Any, path: PurePath) -> tuple[int, int]:
    """``(size, mtime)`` as whole numbers, or :data:`ABSENT` for an entry that cannot
    be stat-ed. Free where the tree already holds the parent's listing, which is every
    case this is used in."""
    try:
        size, mtime = io.stat(path)
        return int(size), int(mtime)
    except OSError:
        return ABSENT, ABSENT


def _facts(io: Any, kind: str, path: PurePath) -> Facts:
    """What gap detection compares a record against: the payload's length, the
    sidecar's length, and the SIDECAR's modification time — the file whose content the
    row is made of. An artifact with no sidecar falls back to the payload's own time,
    so it is still stable across passes instead of being re-read for ever."""
    size, stamp = _stat(io, path)
    side = _sidecars(kind, path)
    if side:
        meta_size, meta_stamp = _stat(io, side[0])
        if meta_size != ABSENT:
            return size, meta_size, meta_stamp
    return size, ABSENT, stamp


def _listing(store: Any, where: PurePath) -> list[PurePath] | None:
    """The artifact folder's own content, in the order the listing produced it —
    or ``None`` when this store has no trustworthy listing to compare against.

    A refusal is told from an absent ``MLSD`` by ASKING the store afterwards: the
    tree drops ``"query"`` at the moment it learns the command is not there, so a
    failure with the capability still present is a genuine refusal and raises.
    """
    io = store.io
    try:
        if not io.is_dir(where):
            return []
        return by_name(match_children(io, where, PATTERN))
    except OSError:
        if reliable(store):
            raise
        return None


def _walk(store: Any, where: PurePath, ctx: OpContext | None = None) -> list[ArtifactInfo]:
    """The listing WITHOUT the index — one sidecar read per artifact, which is what
    the report browser did before this module existed. Used where the index cannot
    apply to this store at all; cancellable per artifact like the reconciled one."""
    rows: list[ArtifactInfo] = []
    for path in _listing(store, where) or []:
        _stop(ctx)
        rows.append(store._report_info(path))
    return rows


def by_name(paths: list[PurePath]) -> list[PurePath]:
    """An artifact folder's files in the contract's order (``data_format.md`` §5.2, E8): by
    name, by case folding, on every tree — never by the path's own order, which is
    case-sensitive on a Linux folder and not on Windows (decision 69)."""
    return sorted(paths, key=lambda p: (name_key(p.name), p.name))


def _from_shards(found: Shards, kind: str, board: str) -> list[ArtifactInfo]:
    """The rows the shards describe for one board, in name order (:func:`by_name`'s), a
    deleted item's line left out.

    ONE rule, used both where there is no listing to reconcile against and where the
    rows are served before the reconciliation that will correct them — so "what we
    already know" is the same answer in both places.
    """
    rows = [r for k, r in found.records.items() if k[:2] == (kind, board) and not r.get(DELETED)]
    return [_info(r) for r in sorted(rows, key=lambda r: (name_key(r["name"]), r["name"]))]


def known(
    store: Any, project_id: str, board: str, ctx: OpContext | None = None
) -> list[ArtifactInfo]:
    """What the shards already describe for ``board`` — no folder listing, no sidecar.

    This is what a listing serves IMMEDIATELY while :func:`reconcile` runs behind it
    ("serve cached shards immediately, reconcile in background"), and what a board keeps
    when its own listing is refused. The rows come from :func:`_info`, the very helper a
    reconciled warm row comes from, so a row served early IS the row the reconciliation
    confirms rather than a paraphrase of it.

    :data:`REPORT` is the only indexed kind, so there is nothing to choose: a ``kind``
    parameter here would be generality no caller can use, and one more way for an early
    row to disagree with the reconciled one.

    ``board`` is the board's key as ``list_boards`` reported it, which is the resolved
    spelling records are keyed by. A store whose ``index`` is somebody's board knows
    nothing: ``[]``.
    """
    found = shards(store, project_id, ctx)
    return _from_shards(found, REPORT, board) if found.usable else []


def known_rows(
    store: Any, project_id: str, boards: list[str], ctx: OpContext | None = None
) -> dict[str, list[ArtifactInfo]]:
    """:func:`known` for a whole walk, in the order ``boards`` were listed; a board the
    shards say nothing about is left out.

    One shard read for the walk — ``ctx`` memoizes the parse (:func:`_memo`) — so serving
    every board's known rows costs the listing a single small read before its first row,
    and the reconciliation that follows re-reads nothing.

    That one read is where a cancel in the early phase lands, so the loop is stopped like
    any other: the user pressed Cancel before a row was painted, and the boards behind
    the read are not worth finishing. A store that keeps no index (:func:`kept`) knows nothing
    early: ``{}``, and its listings answer.
    """
    out: dict[str, list[ArtifactInfo]] = {}
    if not kept(store):
        return out
    for board in boards:
        _stop(ctx)
        rows = known(store, project_id, board, ctx)
        if rows:
            out[board] = rows
    return out


def reconcile(
    store: Any,
    kind: str,
    project_id: str,
    board: str,
    where: PurePath,
    ctx: OpContext | None = None,
) -> list[ArtifactInfo]:
    """One board's artifact listing, served from the shards and reconciled against the
    folder — the whole point of the index, in one pass.

    The board is keyed by the RESOLVED folders (``where.parent``'s, below the evidence
    folder: the bare serial, or ``parts/<pn>/<rev>/<serial>``, :mod:`evidence_board`), not
    by the spelling the caller used, or ``SN1``/``sn1``/``Sn1`` would be three boards
    holding three records for one report on a case-insensitive store.

    What the shards already describe is available WITHOUT this pass (:func:`known`), so a
    view paints rows first and lets this correct them; ``ctx`` stops the pass per
    artifact rather than only between boards.

    Cost in data connections, MEASURED against a real server for 22 reports over 5
    boards, and pinned in ``tests/test_evidence_index_ftps.py`` so it cannot drift:

    ===============================  =====  ====  ====  ====  ========
    pass                             total  MLSD  RETR  APPE  sidecars
    ===============================  =====  ====  ====  ====  ========
    HEAD (no index)                     39    16    23     0        22
    cold index                          49    20    23     5        22
    warm index                          17    16     1     0         0
    second bench, first walk            18    16     2     0         0
    ===============================  =====  ====  ====  ====  ========

    So a COLD pass costs MORE than the listing did before (+26%): it pays the whole
    walk it replaces, plus the shard reads and one append per board. The index buys
    the second pass and every pass after it, on every bench — 39 down to 17, with the
    part that grows with the number of reports gone entirely. (Re-measured 2026-11-05: a
    board's name is found by case folding on every tree, so mapping it lists the evidence
    folder once per board, five ``MLSD`` more on a warm walk.)

    Every row also carries the evidence folder's ownership proof
    (:mod:`evidence_owner`): one marker read on a store's first path mapping — the HEAD
    row's 23rd ``RETR``, the second bench's 2nd — and, on a store's first WRITE, a
    second read plus the write (the cold row's extra ``RETR`` and its one ``STOR``),
    because a read-time proof is not permission to claim a folder. All three are per
    STORE, which is why the warm row pays none of them, and why ownership is proved in
    the path mappers rather than per call.
    """
    found = shards(store, project_id, ctx)
    if not found.usable:  # <root>/index is somebody's board: leave this store alone
        return _walk(store, where, ctx)
    _stop(ctx)
    observed_at = int(time.time())
    paths = _listing(store, where)
    resolved = key_of_path(store._namespace(project_id), where.parent)
    if paths is None:  # no trustworthy listing: the shards ARE the answer
        if not found.complete:
            raise EvidenceError(
                "corrupt",
                f"the evidence index for {resolved} holds a record this version cannot read, "
                "and this store has no listing to check it against",
            )
        return _from_shards(found, kind, resolved)
    rows: list[ArtifactInfo] = []
    fresh: list[dict] = []
    for path in paths:
        _stop(ctx)  # a cancelled walk stops HERE, not at the end of the board
        facts = _facts(store.io, kind, path)
        rec = found.records.get((kind, resolved, path.name))
        if rec is not None and _matches(rec, facts):
            rows.append(_info(rec))
            continue
        info = store._report_info(path)  # the gap: the one sidecar read this pass pays for
        rows.append(info)
        fresh.append(
            _record(kind, resolved, info, facts, store.machine_id, observed_at=observed_at)
        )
    append_records(store, project_id, fresh, ctx)
    return rows


def counted(store: Any, project_id: str) -> int | None:
    """How many reports the shards know about; ``None`` when there is no index to read.

    An **upper bound**, not a count: a record is never removed, so a report removed by
    other means than a delete (a vanished board, a hand edit) still has a line; a delete's
    own line is left out. What ``check_access`` projects its measured
    per-file cost over when the store cannot be walked — an FTPS server without
    ``MLSD`` has no listing to count, and this is the only number available there —
    and :func:`evidence_ops.report_count` words it as a bound because of that.
    """
    if not kept(store):
        return None
    try:
        # The shards are the folder's data like everything else in it: a folder another
        # project owns has no count to give this one — and this is the fallback
        # ``report_count`` reaches exactly when the walk was refused.
        check_owner(store, project_id)
        found = shards(store, project_id)
    except EvidenceError:
        return None
    if not found.usable or not found.records:
        return None
    return sum(1 for key, rec in found.records.items() if key[0] == REPORT and not rec.get(DELETED))
