"""The search's rows and rules, on every storage (``data_format.md`` §6.2): "all results of
SN0042", "every FAIL of dcdc_efficiency last month".

Search is an action of the evidence store (``EvidenceStore.search``), answered the same on every
storage: a store over a folder tree (a folder or a share, FTPS, SFTP) reads its index shards
(``docs/plans/evidence_store_design.md`` §"Index shards") through its tree and answers them here
(:func:`answer`), with nothing beyond the shard files: no listing, no sidecar read, no
reconciliation; the database runs SQL (:mod:`evidence_db.search`) and the web server asks E2
and E8 (:mod:`evidence_api_search`), each then deciding by the same predicate (:func:`matches`)
and answering in the same order (:func:`in_order`). :func:`search_reports_and_runs` reads bare
folders' shards with the same reader, without a store.

A report's shard line carries the LIST of tests it covers (``[]`` when its sidecar names
none); a run's carries the one test it belongs to: the name it had when the run ran (``test``)
and its hidden id, which the run is filed under (``test_id``; a run line without one is left
out). Both fan out to one :class:`ResultRow` per test, so ``test=`` filters the same
way over either kind, by the name recorded, and a report naming no test still surfaces under a
board-only or date-only search rather than silently dropping out. The one
cost of fanning out: a report covering several tests appears once per test it
names, so a board-only query may see it more than once — group the rows by
``path`` to collapse that back to one entry per artifact.

The contract's search rules (``data_format.md`` §6.2, decisions 32 and 33), here as on every
storage: a text filter compares by NFC then case folding (``SN0042`` finds ``sn0042``); a run
or a report a manager deleted is left out unless asked (``deleted``: ``exclude``, ``include``,
``only``; the index's delete line, :func:`evidence_index.trash_line`); the rows come newest
first by when the item started — a run's start, which its ``run_id`` carries (so an incomplete
run sits where its start puts it), a report's ``generated`` — then the run id or name
descending, then the folder (the project) ascending.

The run filters (:data:`RUN_FILTERS`) find runs only, never a report. A folder's index records
each run's operator and bench (:func:`evidence_index.run_record`), so every storage answers
those two (:data:`INDEX_RUN_FILTERS`), compared as the other text filters; a measurement only
where readings are recorded (the database, the web server). A reading is named by its number
in its test (``{1}``, ``{2}``…): its name means something inside one test only, so a search by
a measurement always names its test, or is refused (:func:`refuse_unanswered`; the owner,
2026-11-07).

No GUI here — this module hands back pointer rows, never a rendered list; a
caller opens the report or run behind a row through the ordinary evidence
store (the row's ``path``), which this module never touches.

Qt-free, stdlib only.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from . import utc_time
from .evidence_board import NO_SERIAL, NO_SERIAL_LABEL, board_of, label_of, segments
from .evidence_index import DELETED, INDEX, REPORT, RUN, SUFFIX, Key, merge
from .evidence_store import EvidenceError
from .evidence_tree import name_key
from .test_format import is_test_id

#: What the ``deleted`` filter takes (``data_format.md`` §6.2): leave deleted items out
#: (the default), take them with the others, or only them (the manager's "Show deleted").
EXCLUDE, INCLUDE, ONLY = "exclude", "include", "only"
#: The run filters (``EvidenceStore.SEARCH_RUN_FILTERS``): what a storage records of a run beyond
#: its row. Given one, no report is answered (a report records none of them).
OPERATOR, MACHINE, MEASURED = "operator", "machine_id", "measured"
RUN_FILTERS = frozenset({OPERATOR, MACHINE, MEASURED})
#: The run filters a folder's index answers too: each run's line records its operator and bench.
INDEX_RUN_FILTERS = frozenset({OPERATOR, MACHINE})
#: Why a measurement with no test is refused.
MEASURED_NEEDS_TEST = "a reading's name only means something inside one test: name the test"


@dataclass(frozen=True)
class ResultRow:
    """One pointer a caller can open through the ordinary evidence store —
    never a payload."""

    folder: str
    board: str  # the board's key (:mod:`evidence_board`); a person reads :attr:`board_label`
    test: str  # the test's name as recorded (a run's: the name it had when the run ran)
    kind: str  # "report" | "run"
    name: str  # a report's file name, or a run's run_id
    verdict: str
    ended: str  # report: its "generated"; run: its "ended" — both UTC, "YYYY-MM-DDTHH:MM:SSZ"
    machine_id: str
    path: str
    deleted: bool = False  # in the evidence trash (a manager deleted it)
    test_id: str = ""  # a run's: its test's hidden id, which it is filed under; "" for a report

    @property
    def board_label(self) -> str:
        """The board as a person reads it: ``AX-2210 rev B · SN0042``, the serial alone for a
        board with no part number (:func:`evidence_board.label_of`), never its key."""
        return label_of(self.board)


def item_path(folder: Path | str, kind: str, board: str, test_id: str, name: str) -> str:
    """The immutable layout's own path (``evidence_shared_layout``'s module
    docstring), rebuilt from the pointer alone — no I/O, and never assumed to
    exist: a caller opens it through the store, which is where a moved or
    deleted artifact is actually noticed, not here. ``board`` is the board's key, one
    folder or ``parts/<pn>/<rev>/<serial>`` (:mod:`evidence_board`); a run is filed under its
    test's hidden id. *folder* a local ``Path`` (a folder store's root): a path of this PC;
    a ``str`` (a server's ``<identity>`` or ``<identity>/<project_id>``): joined with ``/``."""
    below = (*segments(board), *(("results", test_id, "runs") if kind == RUN else ("reports",)))
    if isinstance(folder, Path):
        return str(folder.joinpath(*below, name))
    return "/".join((folder, *below, name))


def rows_of(folder: Path | str, rec: dict) -> list[ResultRow]:
    """Every :class:`ResultRow` one shard record stands for — [] for a record
    this function does not recognise (a future kind, or a line too damaged to
    carry a board and a name), never raised: one bad line costs this record,
    not the search."""
    kind, board, name = rec.get("kind"), rec.get("serial"), rec.get("name")
    if (
        kind not in (REPORT, RUN)
        or not isinstance(board, str)
        or not isinstance(name, str)
        or not name
    ):
        return []
    machine = str(rec.get("machine_id") or "")
    deleted = bool(rec.get(DELETED))
    if kind == RUN:
        test, test_id = rec.get("test"), rec.get("test_id")
        if not isinstance(test, str) or not is_test_id(test_id):
            return []  # a run line names the test it is filed under; without it nothing points
        return [
            ResultRow(
                str(folder),
                board,
                test,
                RUN,
                name,
                str(rec.get("verdict") or ""),
                str(rec.get("ended") or ""),
                machine,
                item_path(folder, RUN, board, test_id, name),
                deleted,
                test_id,
            )
        ]
    verdict, ended = str(rec.get("overall_verdict") or ""), str(rec.get("generated") or "")
    tests_field = rec.get("test")
    test_ids = (
        [t for t in tests_field if isinstance(t, str) and t]
        if isinstance(tests_field, list)
        else []
    )
    return [
        ResultRow(
            str(folder),
            board,
            t,
            REPORT,
            name,
            verdict,
            ended,
            machine,
            item_path(folder, REPORT, board, "", name),
            deleted,
        )
        for t in (test_ids or [""])
    ]


def read_folder_shards(evidence_folder: Path) -> dict[Key, dict]:
    """Every kind's records under ``<evidence_folder>/index``, merged by the shard format's one
    reader (:func:`evidence_index.merge`) — but read directly off a bare folder, never through
    a store: no ownership check, no listing beyond ``index/`` itself, and no error on an absent
    folder or an unreadable shard — both simply contribute nothing, because this reader has no
    store to raise a refusal against and no authority to raise one anyway.

    Deliberately NOT in :mod:`evidence_index`: that module's protocol code is scanned by
    ``tests/test_evidence_tree_seam.py`` for filesystem access outside the store's tree, and a
    store-free reader is exactly that, on purpose — it belongs beside the query that has no
    store, not inside the seam that enforces there is always one. It fetches the bytes only:
    what they say is :func:`evidence_index.merge`'s, so it can never disagree with what a store
    reads of the same shards.
    """
    where = evidence_folder / INDEX
    try:
        names = sorted(p.name for p in where.iterdir() if p.name.endswith(SUFFIX))
    except OSError:
        return {}

    def read(shard: str) -> bytes | None:
        try:
            return (where / shard).read_bytes()
        except OSError:
            return None

    return merge(names, read).records


def _day(value: str | date | datetime | None) -> date | None:
    """The day of this PC's calendar *value* names (a ``date``, a ``datetime``'s date, or a
    ``YYYY-MM-DD`` text); ``None`` for none."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value:
        return date.fromisoformat(value[:10])
    return None


def _start(value: str | date | datetime | None) -> str:
    """The instant a *From* bound starts at (UTC, :mod:`utc_time`): the local midnight its day
    starts with; an instant given as recorded, as it is; ``""``: no bound."""
    if utc_time.is_recorded(value):
        return value  # type: ignore[return-value]
    day = _day(value)
    return utc_time.day_start(day) if day is not None else ""


def _end(value: str | date | datetime | None) -> str:
    """The instant a *To* bound ends before (UTC): the local midnight after its day, so the
    whole day is in; an instant given as recorded, as it is; ``""``: no bound."""
    if utc_time.is_recorded(value):
        return value  # type: ignore[return-value]
    day = _day(value)
    return utc_time.day_start(day + timedelta(days=1)) if day is not None else ""


def records_in(folders: Iterable[str | Path]) -> list[tuple[Path, dict]]:
    """Every shard record of the folders, with its folder; a folder given twice read once."""
    seen_folders: dict[Path, None] = {}
    for raw in folders:
        seen_folders.setdefault(Path(raw), None)
    return [(folder, rec) for folder in seen_folders
            for rec in read_folder_shards(folder).values()]  # fmt: skip


def _rows_in(folders: Iterable[str | Path]) -> list[ResultRow]:
    """Every row the folders' shards describe, a folder given twice read once."""
    return [row for folder, rec in records_in(folders) for row in rows_of(folder, rec)]


def _started(row: ResultRow) -> str:
    """When the row's item started, as ``YYYY-MM-DDTHH-MM-SS`` (UTC): a run's from its
    ``run_id``, a report's from its ``generated`` (ISO with ``Z``; a time written without its
    zone, as written)."""
    if row.kind == RUN:
        return row.name[:19]
    day, _, clock = row.ended.replace("T", " ").partition(" ")
    return f"{day}T{clock[:8].replace(':', '-')}"


def _folded(value: str | None) -> str | None:
    return None if value is None else name_key(value)


def board_names(board: str) -> tuple[str, ...]:
    """What the Board filter finds the board of key *board* by, folded: its whole label, or its
    serial alone (the runs saved with no serial have none: their label, *No serial*)."""
    if board == NO_SERIAL:
        return (name_key(label_of(board)),)
    return name_key(label_of(board)), name_key(board_of(board).serial)


def serials_named(board: str) -> list[str]:
    """The serials the Board filter *board* may name, as typed: the whole text, or what follows
    a label's ``·`` (``AX-2210 rev B · SN0042``), and the runs with no serial for their label.
    What a storage that finds a board by its serial asks for (the database's index, E2's and
    E8's ``serial``); :func:`matches` then keeps the boards the filter names."""
    named = [board.strip(), board.rsplit("\u00b7", 1)[-1].strip()]
    if name_key(board) == name_key(NO_SERIAL_LABEL):
        named.append(NO_SERIAL)
    return list(dict.fromkeys(s for s in named if s))


@dataclass(frozen=True)
class Measured:
    """A measurement filter: runs with a measurement of *name*, its value within the bounds
    (both inclusive; ``None``: no bound). A measurement with no number matches no bound."""

    name: str
    low: float | None = None
    high: float | None = None


@dataclass(frozen=True)
class Filters:
    """A search's filters, each folded as it is compared (``None``: no filter), the days a person
    picked as instants in UTC (``""``: no bound): *since* the local midnight the first day
    starts with, *until* the local midnight after the last day (excluded), converted here, in
    the app, so every storage is asked the same instants and compares an item's own recorded
    time with them (E2's and E8's ``from``/``to``). What :func:`matches` decides a row by, on
    every storage (the database's search, :mod:`evidence_db.search`, narrows by SQL first). The run
    filters (:data:`RUN_FILTERS`: *operator*, *machine_id*, *measured*) are answered by the
    storages that record them (``EvidenceStore.SEARCH_RUN_FILTERS``), never by a row."""

    part_number: str | None
    board: str | None
    test: str | None
    verdict: str | None
    since: str
    until: str
    deleted: str
    operator: str | None = None
    machine_id: str | None = None
    measured: Measured | None = None

    @classmethod
    def of(
        cls,
        part_number: str | None = None,
        board: str | None = None,
        test: str | None = None,
        verdict: str | None = None,
        since: str | date | datetime | None = None,
        until: str | date | datetime | None = None,
        deleted: str = EXCLUDE,
        *,
        operator: str | None = None,
        machine_id: str | None = None,
        measured: Measured | None = None,
    ) -> Filters:
        if deleted not in (EXCLUDE, INCLUDE, ONLY):
            raise ValueError(f"deleted must be {EXCLUDE}, {INCLUDE} or {ONLY}: {deleted!r}")
        return cls(part_number, board, test, verdict, _start(since), _end(until), deleted,
                   operator, machine_id, measured)  # fmt: skip

    def run_filters(self) -> frozenset[str]:
        """The run filters this search names (:data:`RUN_FILTERS`): given one, no report."""
        return frozenset(name for name in RUN_FILTERS if getattr(self, name) is not None)


def refuse_unanswered(store: Any, flt: Filters) -> None:
    """Refuse (``invalid``) a search naming a run filter *store* does not answer
    (``SEARCH_RUN_FILTERS``): never an answer that quietly ignores one. A measurement is
    always searched inside its test (:data:`MEASURED_NEEDS_TEST`), on every storage."""
    if flt.measured is not None and flt.test is None:
        raise EvidenceError("invalid", MEASURED_NEEDS_TEST)
    missing = sorted(flt.run_filters() - frozenset(getattr(store, "SEARCH_RUN_FILTERS", ())))
    if missing:
        raise EvidenceError("invalid", f"this storage records no {', '.join(missing)} to search by")


def answer(
    folder: Path | str, records: Iterable[dict], flt: Filters, limit: int
) -> list[ResultRow]:
    """The newest *limit* rows the index records *records* describe that *flt* names, in the
    contract's order: a store over a folder tree's answer (``SharedLayout.search``), its rows'
    ``folder`` *folder* (:func:`item_path`). Its run filters (:data:`INDEX_RUN_FILTERS`: what
    a run's line records) keep the runs whose line names them, and no report."""
    rows = (row for rec in records if _run_named(rec, flt) for row in rows_of(folder, rec))
    return in_order(row for row in rows if matches(row, flt))[:limit]


def _run_named(rec: dict, flt: Filters) -> bool:
    """Whether the index record *rec* is one the run filters of *flt* name (every record, given
    none): a run whose operator and bench compare as the other text filters do."""
    if not flt.run_filters():
        return True
    want_operator, want_machine = _folded(flt.operator), _folded(flt.machine_id)
    return (
        rec.get("kind") == RUN
        and (want_operator is None or name_key(str(rec.get("operator") or "")) == want_operator)
        and (want_machine is None or name_key(str(rec.get("machine_id") or "")) == want_machine)
    )


def matches(row: ResultRow, flt: Filters) -> bool:
    """Whether *row* is one the filters name (:func:`search_reports_and_runs`)."""
    want_pn, want_board = _folded(flt.part_number), _folded(flt.board)
    want_test, want_verdict = _folded(flt.test), _folded(flt.verdict)
    return (
        (want_pn is None or name_key(board_of(row.board).part_number) == want_pn)
        and (want_board is None or want_board in board_names(row.board))
        and (want_test is None or name_key(row.test) == want_test)
        and (want_verdict is None or name_key(row.verdict) == want_verdict)
        and (not flt.since or (bool(row.ended) and row.ended >= flt.since))
        and (not flt.until or (bool(row.ended) and row.ended < flt.until))
        and (flt.deleted == INCLUDE or row.deleted == (flt.deleted == ONLY))
    )


def in_order(rows: Iterable[ResultRow]) -> list[ResultRow]:
    """*rows* in the contract's order: newest start first, then the run id or name
    descending, then the folder (the project) ascending."""
    found = sorted(rows, key=lambda row: row.folder)
    found.sort(key=lambda row: (_started(row), row.name), reverse=True)
    return found


def merged(*answers: Iterable[ResultRow]) -> list[ResultRow]:
    """The rows of several searches (the shards' of folder projects, a database's), as one
    answer in the contract's order."""
    return in_order(row for answer in answers for row in answer)


def search_reports_and_runs(
    folders: Iterable[str | Path],
    *,
    part_number: str | None = None,
    board: str | None = None,
    test: str | None = None,
    verdict: str | None = None,
    since: str | date | datetime | None = None,
    until: str | date | datetime | None = None,
    deleted: str = EXCLUDE,
) -> list[ResultRow]:
    """Every report and run the given evidence folders' shards already
    describe, filtered — pointer rows, in the contract's order (the module docstring).

    *part_number* / *board* / *test* / *verdict* compare by NFC then case folding with the
    spelling the shards carry (a board's part number; a board as a person reads it,
    :attr:`ResultRow.board_label`: ``AX-2210 rev B · SN0042`` finds that board, or its serial
    alone, ``SN0042``, which finds every board with that serial, whatever its part number; a
    test's name, as a run recorded it when it ran, or a report names it; an
    ``overall``/results verdict): this is a straight filter over what a
    bench already recorded (:func:`matches`). *deleted*: :data:`EXCLUDE` (default),
    :data:`INCLUDE` or :data:`ONLY`.

    A folder given more than once contributes its rows once; one with no
    ``index/`` at all — no shard has ever been written there — contributes
    nothing. Neither is an error: the caller hands this every folder it
    knows about (:func:`evidence_folders.folders`), and a folder with
    nothing to say is not a search failure. A project in an evidence database is searched by
    its store (``DatabaseStore.search``) and merged in (:func:`merged`).
    """
    flt = Filters.of(part_number, board, test, verdict, since, until, deleted)
    return in_order(row for row in _rows_in(folders) if matches(row, flt))
