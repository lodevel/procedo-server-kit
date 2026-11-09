"""The evidence store's conformance suite: every action of the interface, stored then read back
through the interface, on every storage (``docs/plans/company_storage_design.md`` §6c).

The owner: *"You cannot miss anything. ... the abstraction layer should be tested against all
storages for all possible actions."* Each case below is registered with the actions it stores
and reads back (:func:`conformance`), and runs on the local folder and the database here, on a
real FTPS server in
``test_evidence_conformance_ftps.py``, on a real SFTP server in ``test_evidence_conformance_sftp.py``,
and through the JSON API's client against the reference
server in ``test_evidence_conformance_api.py``. A second bench reads back what the first stored, and
everything is compared: the bytes, the entries in the order received with which is current and
when the store took each, the generations, the trash with its reasons.

* **Coverage by introspection**: every action of ``EvidenceStore`` that is not LOCAL
  (:data:`evidence_actions.ACTIONS`) has a case, so a new action fails here until a case
  stores and reads it back; and a case names exactly the actions it calls (checked as it
  runs), so the known gaps below reach every case they touch.
* **A storage that cannot do an action fails**, never skips: :data:`KNOWN_GAPS` is the one
  table of what a storage cannot do yet, each a strict xfail that turns into a failure the
  day it starts working.
* **Every storage but the local folder starts a server** (marker ``servers``): it runs with
  ``--servers``, and the database's besides only when a PostgreSQL server is named
  (``pg_harness``); each skip says why. The folder and the introspection tests always run.
* **A test is its hidden id** (``data_format.md`` §2, §4.5, §4.13, §6.9): every action that
  names a test takes its ``test_id``, a lower-case UUID, never its name. Each bench's project
  copy holds ``tests/<name>/test.json`` with that id (§4.3), as a project does; a run records
  the name its test had when it ran, beside the id it is filed under, and a rename moves
  nothing. The cases about it are written from the contract alone, as the reference server was.
"""

from __future__ import annotations

import itertools
import json
import os
import re
import shutil
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import local_zone
import pytest
from test_evidence_board_layout import AX_B, AX_C, BARE, PID, T12, WHY, folder_backend

from procedo.core import evidence_draft, evidence_local_staging, evidence_store_fs_shared, utc_time
from procedo.core.annotation_entries import (
    ALREADY,
    CHANGED,
    COMMENT,
    CURRENT,
    DETAIL,
    LOT,
    NOT_CURRENT,
    OVERRIDE,
    RECEIVED_AT,
    REPLACED,
    SAVED,
    TEST,
    board_data,
    histories,
    make_entry,
)
from procedo.core.evidence_actions import ACTIONS, protocol_actions
from procedo.core.evidence_board import board_of, key_of, label_of, segments
from procedo.core.evidence_search import INCLUDE, ONLY, Filters, Measured
from procedo.core.evidence_store import (
    CURRENT_RUN,
    DELETED,
    NEWEST,
    ArtifactInfo,
    EvidenceError,
    EvidenceKey,
)
from procedo.core.evidence_trash import RETENTION_DAYS, Deletion
from procedo.core.report_export import sidecar_bytes, text_file_bytes
from procedo.core.testing import saved_record

BACKENDS = ("folder", "ftps", "sftp", "db", "api")
BOARDS = (AX_B, BARE)  # a keyed board and a bare serial
#: Three benches, each named as a PC names itself: 8 lower-case hex digits (data_format.md §2).
M1, M2, M3 = "0000a001", "0000a002", "0000a003"
STAMP = r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ"
#: Tests' hidden ids (data_format.md §2): lower-case UUIDs, one per test of the project.
TID1 = "3f9c2a1e-7b4d-4e8a-9c61-2d5b8e0f4a17"
TID2 = "8d02e6b1-5c3a-4f19-b7e2-90a4c6d1e358"
TID3 = "c1a2b3c4-d5e6-4f70-8a91-b2c3d4e5f607"
NEW_ID = "4b7e9d21-0c3f-4a56-8e1b-d7f2c6a9b304"  # a test made again under a deleted one's name
#: The tests every bench's project copy holds when it opens: name -> test_id (§4.3).
TESTS = {"T1": TID1, "T2": TID2, "T3": TID3}

#: What a storage cannot do yet, by (backend, action): each case naming the action is a strict
#: xfail on that backend, with this reason. Strict: the day it works, the case fails until the
#: line is removed. A gap that only one case's check meets (what that case reads back of an
#: action, the other cases calling it passing) is keyed by (backend, case) instead.
KNOWN_GAPS: dict[tuple[str, str], str] = {}


# ---------------------------------------------------------------------------
# The registry: each case and the actions it stores and reads back
# ---------------------------------------------------------------------------

PROTOCOL = frozenset(protocol_actions())
LOCAL = frozenset(name for name, action in ACTIONS.items() if action.local)


@dataclass
class Storage:
    """One storage under test. :meth:`open` opens this project's store on it as a bench does,
    its project copy holding :data:`TESTS` the first time; every action a case calls on a store
    it opened is noted in ``called`` (an action the store calls on itself is not: the case did
    not call it)."""

    name: str
    opener: Callable[..., Any]
    called: set[str] = field(default_factory=set)
    _depth: int = 0

    def open(self, machine: str = M1) -> Any:
        store = self.opener(machine=machine)
        if not (_project(store) / "tests").exists():  # benches may share one copy (FTPS)
            for name, test_id in TESTS.items():
                _make_test(store, name, test_id)
        for action in PROTOCOL & set(dir(store)):  # ``sign_in`` is a server's only
            setattr(store, action, self._noting(action, getattr(store, action)))
        return store

    def _noting(self, action: str, method: Callable[..., Any]) -> Callable[..., Any]:
        def call(*args: Any, **kwargs: Any) -> Any:
            if not self._depth:
                self.called.add(action)
            self._depth += 1
            try:
                return method(*args, **kwargs)
            finally:
                self._depth -= 1

        return call


@dataclass(frozen=True)
class Case:
    check: Callable[[Storage, Any], None]
    actions: frozenset[str]
    #: The contract rules it catches (``procedo_server_contract.md``'s ``rule-<id>`` anchors).
    rules: tuple[str, ...] = ()

    @property
    def name(self) -> str:
        return self.check.__name__


CASES: list[Case] = []


def conformance(*actions: str, rules: tuple[str, ...] = ()) -> Callable:
    """Register a case with every action it calls that reaches a server (and any LOCAL one), and
    the server-contract rules it catches over the JSON API (``rules``: ids of the contract's
    ``rule-<id>`` anchors; a case claims a rule only when it fails against a server breaking it,
    ``docs/server_contract/coverage.md``)."""

    def register(check: Callable[[Storage, Any], None]) -> Callable[[Storage, Any], None]:
        CASES.append(Case(check, frozenset(actions), tuple(rules)))
        return check

    return register


def params(backend: str, *marks: Any) -> list:
    """``(case, backend)`` for every case; a known gap of the backend makes it a strict xfail."""
    out = []
    for case in CASES:
        gaps = sorted(a for a in (*case.actions, case.name) if (backend, a) in KNOWN_GAPS)
        why = "; ".join(f"{backend} cannot {a} yet: {KNOWN_GAPS[backend, a]}" for a in gaps)
        xfail = [pytest.mark.xfail(strict=True, reason=why)] if gaps else []
        out.append(pytest.param(case, backend, marks=[*marks, *xfail], id=f"{case.name}-{backend}"))
    return out


def run_case(case: Case, storage: Storage, monkeypatch) -> None:
    """The case on the storage, every commit a minute after the one before (runs listed in the
    order written); then that it named exactly the actions it called, so the known gaps reach
    every case they touch."""
    ticks = itertools.count()
    monkeypatch.setattr(
        evidence_store_fs_shared, "_utc_now", lambda: T12 + timedelta(minutes=next(ticks))
    )
    case.check(storage, monkeypatch)
    reaching = storage.called - LOCAL
    assert sorted(reaching - case.actions) == [], f"{case.name} calls these without naming them"
    assert sorted(case.actions - storage.called) == [], f"{case.name} names these, never calls"


# ---------------------------------------------------------------------------
# What is stored, and how it reads back
# ---------------------------------------------------------------------------

PNG = b"\x89PNG\r\n\x1a\n made-up picture "
ABSENT = "2026-09-11T00-00-00Z_0000000000000000"  # a well-formed run id never stored


def _record(key: str, test_id: str = TID1) -> dict:
    """A run's record of the test whose id is *test_id*, as a commit leaves it: with that id
    (``metadata.procedo.test_id``), so what is read back is exactly what went in."""
    return {**saved_record("PASS", serial=board_of(key).serial, test_id=test_id), "board": key}


def _files(key: str, i: int, test_id: str = TID1) -> dict[str, bytes | str]:
    """A complete run's payload: its record, two pictures (one in a folder) and a text log."""
    return {
        "results.json": json.dumps(_record(key, test_id)),
        "rise.png": PNG + f"{key} {i}".encode(),
        "plots/fall.png": PNG + f"fall {key} {i}".encode(),
        "console.log": f"run {i}\nline two\n",
    }


def _manifest(key: str) -> dict:
    return {"operator": "alice", "serial_number": board_of(key).serial}


def _run(store, key: str, test_id: str = TID1, *, i: int = 0, complete: bool = True):
    """A run of the test whose id is *test_id*, written as a bench saves one."""
    files = _files(key, i, test_id) if complete else {"console.log": f"stopped {i}\n"}
    return store.write_run(PID, key, test_id, _manifest(key), files)


def _project(store) -> Path:
    """The project copy the bench that opened *store* runs its tests from."""
    return Path(store.project_root)


def _make_test(store, name: str, test_id: str) -> None:
    """The test *name* made in this bench's project copy: its folder and its ``test.json``
    holding *test_id* (data_format.md §4.3), as Studio's *New test* makes it."""
    folder = _project(store) / "tests" / name
    folder.mkdir(parents=True)
    data = json.dumps({"active": "text", "test_id": test_id}) + "\n"
    (folder / "test.json").write_text(data, encoding="utf-8")


def _rename_test(store, old: str, new: str) -> None:
    """The test renamed in this bench's project copy: its folder moves, its ``test.json``, so
    its id, with it (data_format.md §6.9)."""
    tests = _project(store) / "tests"
    (tests / old).rename(tests / new)


def _delete_test(store, name: str) -> None:
    """The test deleted from this bench's project copy: its folder goes (§4.4, §6.9)."""
    shutil.rmtree(_project(store) / "tests" / name)


def _named(runs: list) -> list[tuple[str, str]]:
    """Each run's test as it reads back: the name it ran under, and the id it is filed under."""
    return [(r.test, r.test_id) for r in runs]


def _pushed(store, count: int) -> None:
    assert len(store.flush_outbox()) == count


def _same_files(store, key: str, test_id: str, run_id: str, sent: dict[str, bytes | str]) -> None:
    """Every file of the run reads back as sent (a text file in its platform's newlines); its
    record through ``read_run``."""
    folder = Path(store.local_path(EvidenceKey(PID, key, "run", test_id, run_id)))
    for name, data in sent.items():
        path = folder.joinpath(*name.split("/"))
        if name == "results.json":
            assert store.read_run(PID, key, test_id, run_id) == json.loads(data)
        elif isinstance(data, str):
            assert path.read_text(encoding="utf-8") == data, name
        else:
            assert path.read_bytes() == data, name


def _kind(call: Callable[..., Any], *args: Any, **kwargs: Any) -> str:
    """The kind of the error ``call(*args, **kwargs)`` must raise."""
    with pytest.raises(EvidenceError) as ei:
        call(*args, **kwargs)
    return ei.value.kind


def _code(call: Callable[..., Any], *args: Any, **kwargs: Any) -> tuple[str, str]:
    """The kind and the contract's code of the error ``call(*args, **kwargs)`` must raise."""
    with pytest.raises(EvidenceError) as ei:
        call(*args, **kwargs)
    return ei.value.kind, ei.value.code


def _queued(store) -> dict[Path, bytes]:
    """What this PC's outbox holds now (entries and payloads, not what it parked): kept to
    put back, as an outbox replay finds it (a push the store took, its entry not dropped)."""
    root = Path(store.outbox)
    return {
        p: p.read_bytes()
        for p in root.rglob("*")
        if p.is_file() and root / "quarantine" not in p.parents
    }


def _replay(kept: dict[Path, bytes]) -> None:
    for path, data in kept.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def _parked(store, name: str) -> list[str]:
    """Why this PC's outbox parked each item *name* names (a run id, an artifact's name)."""
    return [i.reason for i in store.quarantined_items(PID) if name in (i.id, i.name)]


def _as_sent(read: list[dict]) -> list[dict]:
    """Entries read back without what the store added: when it took each one (UTC, seconds)."""
    assert all(re.fullmatch(STAMP, e[RECEIVED_AT]) for e in read), read
    return [{k: v for k, v in e.items() if k != RECEIVED_AT} for e in read]


def _statuses(entries: list[dict]) -> list[str]:
    """Each entry's place in its slot's history, in the order received."""
    found = {
        line.entry["entry_id"]: line.status for rows in histories(entries).values() for line in rows
    }
    return [found[e["entry_id"]] for e in entries]


def _trashed(item) -> tuple:
    """A trash item as compared: what it was, why, who, from which PC, whether it was Current.
    A run's test (its name and id) is :func:`a_run_reads_back_its_test_name_and_test_id`'s."""
    return (
        item.what,
        item.board,
        item.name,
        item.reason,
        item.deleted_by,
        item.station,
        item.machine_id,
        item.was_current,
    )


def _why(machine: str) -> tuple:
    """:data:`WHY` as a trash item keeps it, deleted from *machine*."""
    return WHY.reason, WHY.deleted_by, WHY.station, machine


def _recent(item) -> bool:
    return abs(item.deleted_at - time.time()) < 600


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


@conformance(
    "list_boards", "list_runs", "current_run", "read_run", "read_run_annotation", "list_artifacts",
    "read_artifact",
    rules=("6.1.item-not-held-404", "E5.board"),
)  # fmt: skip
def an_empty_storage_reads_back_nothing(s: Storage, monkeypatch) -> None:
    store = s.open()
    assert store.list_boards(PID) == []
    for key in BOARDS:
        assert store.list_runs(PID, key, TID1) == []
        assert store.current_run(PID, key, TID1) is None
        assert _kind(store.read_run, PID, key, TID1, CURRENT_RUN) == "not_found"
        assert _kind(store.read_run_annotation, PID, key, TID1, CURRENT_RUN) == "not_found"
        for kind in ("report", "draft"):
            assert store.list_artifacts(kind, PID, key) == []
            assert _kind(store.read_artifact, kind, PID, key, "SN1.draft.json") == "not_found"
    assert store.list_artifacts("export", PID, "") == []
    assert _kind(store.read_artifact, "export", PID, "", "e.docx") == "not_found"


@conformance(
    "write_run", "stage", "commit_run", "list_boards", "list_runs", "current_run", "read_run",
    "read_run_annotation",
    rules=(
        "2.6.board-three-parts", "2.6.query-empty-is-none", "5.5.run-summary", "5.8.runfile-bytes",
        "5.12.runs-newest-first", "5.13.incomplete-never-current", "6.1.item-not-held-404",
        "6.2.multipart-form", "E1.push",
        "E1.creates-project-board", "E3.run",
    ),
)  # fmt: skip
def runs_complete_and_incomplete_read_back_with_their_files(s: Storage, monkeypatch) -> None:
    """A complete run with its pictures and log, a run that stopped without its record, and a
    run a test script wrote itself (stage, then commit), on a keyed board and a bare serial;
    nothing else appears."""
    store = s.open()
    sent = {}
    for key in BOARDS:
        complete = _run(store, key)
        stopped = _run(store, key, complete=False)
        staged = store.stage(EvidenceKey(PID, key, "run", TID2, CURRENT_RUN))
        external = {"results.json": json.dumps(_record(key, TID2)), "scope.png": PNG + b"scope"}
        for name, data in external.items():
            (staged.path / name).write_bytes(data.encode() if isinstance(data, str) else data)
        script = store.commit_run(staged, _manifest(key))
        sent[key] = (complete, stopped, script, external)
    _pushed(store, 6)

    reader = s.open(M2)
    assert reader.list_boards(PID) == sorted(BOARDS)
    for key, (complete, stopped, script, external) in sent.items():
        assert (complete.complete, stopped.complete, script.complete) == (True, False, True)
        assert reader.list_runs(PID, key, TID1) == [stopped, complete]  # newest first
        assert reader.current_run(PID, key, TID1) == complete  # a stopped run is never current
        assert reader.read_run(PID, key, TID1, CURRENT_RUN) == _record(key)
        _same_files(reader, key, TID1, complete.run_id, _files(key, 0))
        assert _kind(reader.read_run, PID, key, TID1, stopped.run_id) == "not_found"
        _same_files(reader, key, TID1, stopped.run_id, {"console.log": "stopped 0\n"})
        assert reader.list_runs(PID, key, TID2) == [script]
        assert reader.current_run(PID, key, TID2) == script
        _same_files(reader, key, TID2, script.run_id, external)

        assert reader.list_runs(PID, key, TID3) == [] and reader.current_run(PID, key, TID3) is None
        assert _kind(reader.read_run, PID, key, TID1, ABSENT) == "not_found"
        assert reader.read_run_annotation(PID, key, TID1, ABSENT) is None
    assert reader.list_runs(PID, AX_C, TID1) == []  # the other board of the same serial


@conformance(
    "write_run", "promote_run", "demote_run", "current_run", "read_run", "list_runs",
    rules=("5.13.newer-complete-current", "E6.set-clear"),
)  # fmt: skip
def the_current_run_is_set_and_cleared(s: Storage, monkeypatch) -> None:
    """Promote makes an older run current (an incomplete one is refused); demote leaves none,
    keeps every run, and only a newer run revives the test."""
    store, reader = s.open(), s.open(M2)
    for key in BOARDS:
        older, newer = _run(store, key, i=1), _run(store, key, i=2)
        stopped = _run(store, key, complete=False)
        _pushed(store, 3)
        assert reader.current_run(PID, key, TID1) == newer
        store.promote_run(PID, key, TID1, older.run_id)
        assert reader.current_run(PID, key, TID1) == older
        reader.promote_run(PID, key, TID1, older.run_id)  # current already: a replay, done
        assert reader.current_run(PID, key, TID1) == older
        assert reader.read_run(PID, key, TID1, CURRENT_RUN) == _record(key)
        assert _kind(store.promote_run, PID, key, TID1, stopped.run_id) == "invalid"
        assert reader.current_run(PID, key, TID1) == older

        assert store.demote_run(PID, key, TID1) == older.run_id
        assert reader.current_run(PID, key, TID1) is None
        assert _kind(reader.read_run, PID, key, TID1, CURRENT_RUN) == "not_found"
        assert reader.list_runs(PID, key, TID1) == [stopped, newer, older]
        assert store.demote_run(PID, key, TID1) is None  # nothing is current: nothing to clear

        later = _run(store, key, i=3)
        _pushed(store, 1)
        assert reader.current_run(PID, key, TID1) == later


@conformance(
    "write_run", "delete_run", "list_runs", "current_run", "read_run", "list_trash",
    "restore_trash", "purge_trash",
    rules=(
        "5.5.deleted-fields", "5.12.deleted-left-out", "5.13.delete-hands-on", "E2.deleted-only",
        "E3.deleted-answers", "E11.delete", "E11.restore", "E11.replays", "E13.not-expired-kept",
        "E13.items",
    ),
)  # fmt: skip
def a_deleted_run_is_kept_with_its_reason_until_purged(s: Storage, monkeypatch) -> None:
    """A delete without a reason is refused; a delete hands the pointer on and keeps the run
    with who, why, from where, for the store's retention period; deleting it again is done and
    keeps the first reason; a restore brings it back Current, every file as it was, and
    restoring it again is done, nothing changed; a purge removes a chosen entry for good and
    nothing that has not expired — one no longer held is skipped, one not deleted refuses the
    whole purge (decisions 48, 71a). Expiry needs another moment, which only a folder's
    purge can name (a server's own clock decides, S8): the folder trash's tests cover it."""
    store, reader = s.open(), s.open(M2)
    runs = {key: (_run(store, key, i=1), _run(store, key, i=2)) for key in BOARDS}
    _pushed(store, 4)
    kept, gone = runs[AX_B]
    blank = Deletion(" ", "alice", "BENCH-03")
    assert _kind(store.delete_run, PID, AX_B, TID1, gone.run_id, deletion=blank) == "invalid"
    assert reader.list_runs(PID, AX_B, TID1) == [gone, kept] and reader.list_trash(PID) == []

    store.delete_run(PID, AX_B, TID1, gone.run_id, deletion=WHY)
    reader.delete_run(PID, AX_B, TID1, gone.run_id, deletion=Deletion("again", "bob", "B-9"))
    assert reader.list_runs(PID, AX_B, TID1) == [kept]
    assert reader.current_run(PID, AX_B, TID1) == kept  # handed on
    [item] = reader.list_trash(PID)
    assert _trashed(item) == ("run", AX_B, gone.run_id, *_why(M1), True)
    assert _recent(item) and item.expires_at - item.deleted_at == RETENTION_DAYS * 24 * 3600
    assert reader.restore_trash(PID, item.entry) is True  # Current again: nothing newer came
    assert reader.restore_trash(PID, item.entry) is False  # live already: done, nothing changed
    assert reader.list_trash(PID) == []
    assert reader.list_runs(PID, AX_B, TID1) == [gone, kept]
    assert reader.current_run(PID, AX_B, TID1) == gone
    _same_files(reader, AX_B, TID1, gone.run_id, _files(AX_B, 2))
    restored = item.entry

    kept, gone = runs[BARE]
    store.delete_run(PID, BARE, TID1, gone.run_id, deletion=WHY)
    [item] = reader.list_trash(PID)
    assert _kind(reader.purge_trash, PID, [item.entry, restored]) == "invalid"
    assert reader.list_trash(PID) == [item]  # nothing purged
    assert reader.purge_trash(PID, [item.entry]) == [item.entry]
    assert reader.purge_trash(PID, [item.entry]) == []  # a retried purge: skipped
    assert reader.list_trash(PID) == [] and reader.list_runs(PID, BARE, TID1) == [kept]
    assert _kind(reader.restore_trash, PID, item.entry) == "not_found"

    store.delete_run(PID, BARE, TID1, kept.run_id, deletion=WHY)
    [last] = reader.list_trash(PID)
    assert reader.purge_trash(PID) == []  # not expired yet
    assert reader.purge_trash(PID, [last.entry]) == [last.entry]  # the test's last run
    assert reader.list_trash(PID) == [] and reader.list_runs(PID, BARE, TID1) == []
    assert reader.current_run(PID, BARE, TID1) is None


@conformance(
    "write_run", "delete_run", "list_runs", "current_run", "add_run_annotation_entries",
    "read_run_annotation", "list_trash", "restore_trash", "purge_trash",
    rules=("E1.replay", "E1.purged-409", "E7.deleted-run-409", "E13.run-id-kept"),
)  # fmt: skip
def a_deleted_run_stays_deleted_and_a_purged_one_never_comes_back(s: Storage, monkeypatch) -> None:
    """An outbox replay of a run deleted since its push is done and the run stays deleted; a
    comment on it is refused (``deleted``) and its own come back with it on a restore; once
    purged, a replay of it is refused (``deleted``) and parked: a purged run never comes back
    (data_format.md §6.1, §6.5, §6.7; the owner's answer to 30)."""
    store, reader = s.open(), s.open(M2)
    for key in BOARDS:
        run = _run(store, key)
        kept = _queued(store)
        _pushed(store, 1)
        note = make_entry(COMMENT, TEST, "seen", None, "alice", M1)
        store.add_run_annotation_entries(PID, key, TID1, run.run_id, [note])
        store.delete_run(PID, key, TID1, run.run_id, deletion=WHY)

        _replay(kept)
        _pushed(store, 1)  # the store holds it: done
        assert reader.list_runs(PID, key, TID1) == [] and reader.current_run(PID, key, TID1) is None
        later = make_entry(COMMENT, TEST, "late", note["entry_id"], "bob", M2)
        add = reader.add_run_annotation_entries
        assert _code(add, PID, key, TID1, run.run_id, [later]) == ("invalid", DELETED)
        [item] = reader.list_trash(PID)
        assert reader.restore_trash(PID, item.entry) is True
        assert reader.list_runs(PID, key, TID1) == [run]
        assert _as_sent(reader.read_run_annotation(PID, key, TID1, run.run_id)["entries"]) == [note]

        store.delete_run(PID, key, TID1, run.run_id, deletion=WHY)
        [item] = reader.list_trash(PID)
        assert reader.purge_trash(PID, [item.entry]) == [item.entry]
        _replay(kept)
        _pushed(store, 0)
        assert any("A manager deleted the run" in why for why in _parked(store, run.run_id))
        assert reader.list_runs(PID, key, TID1) == [] and reader.list_trash(PID) == []
        assert reader.current_run(PID, key, TID1) is None


@conformance("write_run", "list_boards", "list_runs", "current_run", "read_run")
def a_name_differing_only_by_case_is_the_stored_one(s: Storage, monkeypatch) -> None:
    """A board sent in another case is the one stored first, on a case-sensitive storage too:
    its runs land there, the lists give the first spelling, and either spelling reads them back
    (data_format.md §2, decision 21). A test is named by its id, never by a name: the name it
    ran under is a record, kept as written (:func:`a_run_reads_back_its_test_name_and_test_id`)."""
    store, reader = s.open(), s.open(M2)
    for key, other in ((AX_B, key_of("ax", "b", "sn1")), (BARE, "sn1")):
        first = _run(store, key, i=1)
        second = _run(store, other, i=2)
        _pushed(store, 2)
        for board in (key, other):
            assert reader.list_runs(PID, board, TID1) == [second, first]
            assert reader.current_run(PID, board, TID1) == second
            assert reader.read_run(PID, board, TID1, first.run_id) == _record(key)
            assert reader.read_run(PID, board, TID1, second.run_id) == _record(other)
    assert reader.list_boards(PID) == sorted(BOARDS)


# ---------------------------------------------------------------------------
# A test's hidden id: what runs and marks are filed under (data_format.md §2, §4.5, §4.13)
# ---------------------------------------------------------------------------

#: "Café" as a PC may spell a folder's name (NFD), and as a record keeps it (NFC, §2).
CAFE_NFD = unicodedata.normalize("NFD", "Café")
CAFE = unicodedata.normalize("NFC", "Café")
assert CAFE_NFD != CAFE


@conformance(
    "write_run", "stage", "commit_run", "list_runs", "current_run", "delete_run", "list_trash",
    rules=("2.4.record-kept-as-sent", "5.12.runs-newest-first"),
)  # fmt: skip
def a_run_reads_back_its_test_name_and_test_id(s: Storage, monkeypatch) -> None:
    """A run is filed under its test's id and records the name the test had when it ran, as
    written, in NFC, never respelled: a test renamed by case, then to a name a PC spells in
    NFD, keeps each earlier run with the name it ran under, all under its one id; a run a test
    script wrote likewise; the writer's answer, the lists, the current run and a deleted run's
    trash item each give both (data_format.md §2 "Names and case", §4.5 ``test``, ``test_id``;
    contract_questions T4, T5, doubt 6)."""
    store, reader = s.open(), s.open(M2)
    first = _run(store, BARE, i=1)  # the project's test T1
    _rename_test(store, "T1", "t1")  # by case only
    second = _run(store, BARE, i=2)
    _rename_test(store, "t1", CAFE_NFD)
    staged = store.stage(EvidenceKey(PID, BARE, "run", TID1, CURRENT_RUN))
    (staged.path / "results.json").write_text(json.dumps(_record(BARE)), encoding="utf-8")
    third = store.commit_run(staged, _manifest(BARE))
    _pushed(store, 3)

    named = [("T1", TID1), ("t1", TID1), (CAFE, TID1)]
    assert _named([first, second, third]) == named
    assert reader.list_runs(PID, BARE, TID1) == [third, second, first]
    assert _named(reader.list_runs(PID, BARE, TID1)) == named[::-1]
    assert reader.current_run(PID, BARE, TID1) == third
    assert _named([reader.current_run(PID, BARE, TID1)]) == [(CAFE, TID1)]

    reader.delete_run(PID, BARE, TID1, second.run_id, deletion=WHY)
    [item] = reader.list_trash(PID)
    assert (item.what, item.name, item.test, item.test_id) == ("run", second.run_id, "t1", TID1)


@conformance(
    "write_run", "list_runs", "current_run", "read_run", "demote_run", "delete_run",
    rules=("2.5.filed-under-id", "5.13.delete-hands-on"),
)  # fmt: skip
def two_ids_with_one_recorded_name_keep_their_own_runs_and_mark(s: Storage, monkeypatch) -> None:
    """A test deleted and made again under its name is another test, with its own id: the old
    test's runs and current runs stay under its id, the new one starts with none, and on a
    board each keeps its own mark whatever the other's does, though every run of both
    recorded one name (data_format.md §2, §4.5, §4.13, §6.9; contract_questions T5, doubt 4)."""
    store, reader = s.open(), s.open(M2)
    old = {key: (_run(store, key, i=1), _run(store, key, i=2)) for key in BOARDS}
    _pushed(store, 4)
    _delete_test(store, "T1")
    _make_test(store, "T1", NEW_ID)
    for key in BOARDS:
        assert reader.list_runs(PID, key, NEW_ID) == []
        assert reader.current_run(PID, key, NEW_ID) is None
    new = _run(store, AX_B, NEW_ID, i=3)
    _pushed(store, 1)

    assert _named([*old[AX_B], new]) == [("T1", TID1), ("T1", TID1), ("T1", NEW_ID)]
    for key, (older, newer) in old.items():
        assert reader.list_runs(PID, key, TID1) == [newer, older]
        assert reader.current_run(PID, key, TID1) == newer
    assert reader.list_runs(PID, AX_B, NEW_ID) == [new]
    assert reader.current_run(PID, AX_B, NEW_ID) == new
    assert reader.read_run(PID, AX_B, NEW_ID, CURRENT_RUN) == _record(AX_B, NEW_ID)
    assert reader.list_runs(PID, BARE, NEW_ID) == []
    assert _kind(reader.read_run, PID, AX_B, NEW_ID, old[AX_B][1].run_id) == "not_found"

    older, newer = old[AX_B]
    assert store.demote_run(PID, AX_B, NEW_ID) == new.run_id
    assert reader.current_run(PID, AX_B, NEW_ID) is None
    assert reader.current_run(PID, AX_B, TID1) == newer
    store.delete_run(PID, AX_B, TID1, newer.run_id, deletion=WHY)
    assert reader.current_run(PID, AX_B, TID1) == older  # handed on within its own id
    assert reader.current_run(PID, AX_B, NEW_ID) is None
    assert reader.list_runs(PID, AX_B, NEW_ID) == [new]


#: What is not a test's id (procedo_api.openapi.yaml ``TestId``: a lower-case UUID): a test's
#: name (one the project holds), its id in upper case, its id without the hyphens.
NOT_IDS = ("T1", TID1.upper(), TID1.replace("-", ""))


@conformance(
    "write_run", "stage", "list_boards", "list_runs", "current_run", "read_run",
    "read_run_annotation", "add_run_annotation_entries", "promote_run", "demote_run", "delete_run",
)  # fmt: skip
def a_name_is_not_a_test_id(s: Storage, monkeypatch) -> None:
    """Every action that names a test takes its id and refuses anything else (``invalid``),
    the test's own name included: nothing is read under it, nothing is written, nothing waits
    in this PC's outbox, the run filed under the id is untouched (data_format.md §2
    Identifiers, §4.5; procedo_api.openapi.yaml ``TestId``)."""
    store = s.open()
    run = _run(store, BARE)
    _pushed(store, 1)
    note = make_entry(COMMENT, TEST, "seen", None, "alice", M1)
    add = store.add_run_annotation_entries
    for test in NOT_IDS:
        for key in BOARDS:
            assert _kind(store.list_runs, PID, key, test) == "invalid", test
            assert _kind(store.current_run, PID, key, test) == "invalid", test
            assert _kind(store.read_run, PID, key, test, CURRENT_RUN) == "invalid", test
            assert _kind(store.read_run_annotation, PID, key, test, run.run_id) == "invalid"
            assert _kind(add, PID, key, test, run.run_id, [note]) == "invalid", test
            assert _kind(store.promote_run, PID, key, test, run.run_id) == "invalid", test
            assert _kind(store.demote_run, PID, key, test) == "invalid", test
            deleting = (PID, key, test, run.run_id)
            assert _kind(store.delete_run, *deleting, deletion=WHY) == "invalid", test
            run_of = (PID, key, test, _manifest(key), _files(key, 1))
            assert _kind(store.write_run, *run_of) == "invalid", test
            staging = EvidenceKey(PID, key, "run", test, CURRENT_RUN)
            assert _kind(store.stage, staging) == "invalid", test
    assert store.pending_items(PID) == [] and store.flush_outbox() == []

    reader = s.open(M2)
    assert reader.list_boards(PID) == [BARE]
    assert reader.list_runs(PID, BARE, TID1) == [run]
    assert reader.current_run(PID, BARE, TID1) == run


@conformance(
    "write_run", "promote_run", "demote_run", "delete_run", "current_run", "read_run", "list_runs",
    rules=("5.13.delete-hands-on", "5.13.one-mark-per-test-id", "E6.set-clear"),
)  # fmt: skip
def the_mark_is_per_board_and_test_id(s: Storage, monkeypatch) -> None:
    """A board holds one current-run mark per test id, naming that id's run: setting,
    clearing or handing one on moves no other, of another test on the same board or of the
    same test on another board; a run is set current only under its own board and id, else
    ``not_found`` (data_format.md §4.12, §4.13, §6.4; procedo_api.openapi.yaml ``setCurrent``)."""
    store, reader = s.open(), s.open(M2)
    a1, a2 = _run(store, AX_B, i=1), _run(store, AX_B, i=2)
    b1 = _run(store, AX_B, TID2, i=3)
    c1, c2 = _run(store, BARE, i=4), _run(store, BARE, i=5)
    _pushed(store, 5)
    slots = ((AX_B, TID1), (AX_B, TID2), (BARE, TID1), (BARE, TID2))

    def marks() -> list:
        return [reader.current_run(PID, key, test) for key, test in slots]

    assert marks() == [a2, b1, c2, None]
    assert [run.test_id for run in marks()[:3]] == [TID1, TID2, TID1]

    store.promote_run(PID, AX_B, TID1, a1.run_id)
    assert marks() == [a1, b1, c2, None]
    assert _kind(store.promote_run, PID, AX_B, TID2, a2.run_id) == "not_found"  # another id's
    assert _kind(store.promote_run, PID, BARE, TID1, a2.run_id) == "not_found"  # another board's
    assert _kind(store.promote_run, PID, BARE, TID2, c1.run_id) == "not_found"  # none of TID2
    assert marks() == [a1, b1, c2, None]

    assert store.demote_run(PID, AX_B, TID2) == b1.run_id
    assert marks() == [a1, None, c2, None]
    assert _kind(reader.read_run, PID, AX_B, TID2, CURRENT_RUN) == "not_found"
    assert reader.read_run(PID, AX_B, TID1, CURRENT_RUN) == _record(AX_B)

    store.delete_run(PID, BARE, TID1, c2.run_id, deletion=WHY)
    assert marks() == [a1, None, c1, None]
    assert reader.list_runs(PID, AX_B, TID2) == [b1]


@conformance(
    "write_run", "add_run_annotation_entries", "demote_run", "list_runs", "current_run", "read_run",
    "read_run_annotation",
)  # fmt: skip
def a_rename_moves_no_run_and_no_mark(s: Storage, monkeypatch) -> None:
    """A test renamed keeps its id (its ``test.json`` moves with its folder): every board's
    runs, their files and annotations, and its marks, a cleared one included, stay where they
    are, each run keeping the name it ran under; the next run records the new name under the
    same id and becomes current; a new test made under the old name has none of them
    (data_format.md §4.5, §4.13, §6.9; contract_questions T5)."""
    store, reader = s.open(), s.open(M2)
    runs = {key: (_run(store, key, i=1), _run(store, key, complete=False)) for key in BOARDS}
    other = _run(store, BARE, TID3)
    _pushed(store, 5)
    note = make_entry(COMMENT, TEST, "seen", None, "alice", M1)
    for key, (complete, _stopped) in runs.items():
        store.add_run_annotation_entries(PID, key, TID1, complete.run_id, [note])
    assert store.demote_run(PID, AX_B, TID1) == runs[AX_B][0].run_id

    _rename_test(store, "T1", "Ripple")
    _make_test(store, "T1", NEW_ID)
    later = _run(store, BARE, i=2)
    _pushed(store, 1)

    for key, (complete, stopped) in runs.items():
        held = [later, stopped, complete] if key == BARE else [stopped, complete]
        assert reader.list_runs(PID, key, TID1) == held
        _same_files(reader, key, TID1, complete.run_id, _files(key, 1))
        notes = reader.read_run_annotation(PID, key, TID1, complete.run_id)["entries"]
        assert _as_sent(notes) == [note]
        assert reader.list_runs(PID, key, NEW_ID) == []
        assert reader.current_run(PID, key, NEW_ID) is None
    assert reader.current_run(PID, AX_B, TID1) is None  # cleared before the rename: still
    assert reader.current_run(PID, BARE, TID1) == later
    ran = [("Ripple", TID1), ("T1", TID1), ("T1", TID1)]
    assert _named(reader.list_runs(PID, BARE, TID1)) == ran
    assert reader.list_runs(PID, BARE, TID3) == [other]


# ---------------------------------------------------------------------------
# Artifacts
# ---------------------------------------------------------------------------


def _report(key: str, i: int) -> tuple[bytes, bytes, ArtifactInfo]:
    """The i-th report sent under one name: its bytes, its record (the sidecar, as a report
    writes it), and the row the record lists as."""
    meta = {
        "generated": f"2026-09-2{i}T09:14:41Z",
        "scenario": f"scenario {i}",
        "operator": ("alice", "bob")[i % 2],
        "overall_verdict": ("PASS", "FAIL")[i % 2],
    }
    tests = ["t1", f"t{i + 2}"]
    sidecar = text_file_bytes(
        json.dumps({"metadata": meta, "tests": [{"name": t} for t in tests]}, indent=2)
    )
    row = ArtifactInfo(
        "r.html",
        meta["generated"],
        meta["scenario"],
        meta["operator"],
        meta["overall_verdict"],
        "",  # a report's row names no machine
        tuple(tests),
    )
    return f"<html>{key} #{i}</html>".encode(), sidecar, row


def _draft(data: dict) -> bytes:
    """A report draft as the report preview saves it (``result_annotations.save_global_draft``)."""
    return text_file_bytes(json.dumps(data, indent=2))


def _export(i: int) -> tuple[bytes, bytes]:
    return f"PK export #{i}".encode(), sidecar_bytes({"template": f"word {i}"})


PLAIN = ArtifactInfo("x", "", "", "", "", "")


def _send(
    store,
    kind: str,
    key: str,
    name: str,
    data: bytes,
    sidecar: bytes | None = None,
    replaces: str | None = None,
):
    store.write_artifact(kind, PID, key, name, data, PLAIN, sidecar=sidecar, replaces=replaces)
    _pushed(store, 1)


def _sidecar(store, kind: str, key: str, name: str) -> bytes:
    """The record kept beside an artifact, as this PC opens it."""
    path = Path(store.local_path(EvidenceKey(PID, key, kind, name=name)))
    side = path.with_suffix(".json") if kind == "report" else path.with_name(f"{name}.meta.json")
    return side.read_bytes()


@conformance(
    "write_artifact", "read_artifact", "list_artifacts", "list_generations", "read_generation",
    rules=(
        "5.10.generations", "E8.generations", "E8.include-info", "E9.generation",
        "E10.report-already", "E10.report-new-generation",
    ),
)  # fmt: skip
def reports_and_exports_keep_every_generation(s: Storage, monkeypatch) -> None:
    """A report sent three times is three generations, the newest read and listed; the same
    bytes again add none. A project's export likewise. Each generation's bytes and record read
    back; the newest's record byte for byte."""
    store, reader = s.open(), s.open(M2)
    for key in BOARDS:
        assert reader.list_generations("report", PID, key, "r.html") == []
        for i in range(3):
            data, sidecar, _row = _report(key, i)
            _send(store, "report", key, "r.html", data, sidecar)
    # The same again, saved on the bench a second later: its ``updated_at`` differs, and a
    # store compares only the file and its record (contract §5.10, E10).
    time.sleep(1.1)
    for key in BOARDS:
        _send(store, "report", key, "r.html", *_report(key, 2)[:2])
    exports = [_export(i) for i in range(2)]
    for data, meta in exports:
        _send(store, "export", "", "e.docx", data, meta)

    for key in BOARDS:
        reports = [_report(key, i) for i in range(3)]
        assert reader.list_artifacts("report", PID, key) == [reports[-1][2]]
        assert reader.read_artifact("report", PID, key, "r.html") == reports[-1][0]
        assert _sidecar(reader, "report", key, "r.html") == reports[-1][1]
        generations = reader.list_generations("report", PID, key, "r.html")
        assert generations == [(n, row) for n, (_, _, row) in enumerate(reports, start=1)]
        read = [reader.read_generation("report", PID, key, "r.html", n) for n in (1, 2, 3)]
        assert read == [data for data, _, _ in reports]
        assert _kind(reader.read_generation, "report", PID, key, "r.html", 4) == "not_found"
    assert [i.name for i in reader.list_artifacts("export", PID, "")] == ["e.docx"]
    assert reader.read_artifact("export", PID, "", "e.docx") == exports[-1][0]
    assert _sidecar(reader, "export", "", "e.docx") == exports[-1][1]
    generations = reader.list_generations("export", PID, "", "e.docx")
    assert [(n, info.name) for n, info in generations] == [(1, "e.docx"), (2, "e.docx")]
    read = [reader.read_generation("export", PID, "", "e.docx", n) for n in (1, 2)]
    assert read == [data for data, _ in exports]


DRAFT = "SN1.draft.json"


@conformance(
    "write_artifact", "read_artifact", "list_artifacts", "list_generations",
    rules=("5.10.draft-version", "E10.draft"),
)  # fmt: skip
def a_draft_is_replaced_only_when_unchanged_since_it_was_read(s: Storage, monkeypatch) -> None:
    """With none held, any draft is saved; a draft saved over the version read replaces it and
    keeps no generation; one saved over a version changed since is refused (``changed``) and
    parked, the draft held untouched; the same draft again is done (data_format.md §6.6)."""
    store, reader = s.open(), s.open(M2)
    for key in BOARDS:
        first, second, stale = (_draft({"report_comment": f"{key}, draft {i}"}) for i in range(3))
        _send(store, "draft", key, DRAFT, first, replaces="0" * 64)  # none held: saved
        _send(store, "draft", key, DRAFT, second, replaces=evidence_draft.version_of(first))
        assert reader.read_artifact("draft", PID, key, DRAFT) == second
        store.write_artifact(
            "draft", PID, key, DRAFT, stale, PLAIN, replaces=evidence_draft.version_of(first)
        )
        _pushed(store, 0)
        assert any("was changed" in why for why in _parked(store, DRAFT))
        _send(store, "draft", key, DRAFT, second)  # the one held: done, nothing written
        assert [i.name for i in reader.list_artifacts("draft", PID, key)] == [DRAFT]
        assert reader.read_artifact("draft", PID, key, DRAFT) == second
        assert _kind(reader.list_generations, "draft", PID, key, DRAFT) == "invalid"
        assert reader.list_artifacts("report", PID, key) == []
    assert len(_parked(store, DRAFT)) == len(BOARDS)


@conformance("write_artifact", "list_artifacts")
def artifacts_list_by_name_whatever_its_case(s: Storage, monkeypatch) -> None:
    """Reports, exports and drafts list by name, by case folding, on every storage
    (decision 69): never by the bytes of the name, which a Linux folder would sort ``B``
    before ``a``, nor newest first, as a web server's E8 answers (the newest here is
    ``c.html``)."""
    store, reader = s.open(), s.open(M2)
    for i, name in enumerate(("a.html", "B.html", "c.html")):
        sidecar = _report(BARE, i)[1]
        store.write_artifact("report", PID, BARE, name, b"<html/>", PLAIN, sidecar=sidecar)
    for name in ("B.docx", "a.docx"):
        store.write_artifact("export", PID, "", name, b"PK", PLAIN)
    for name in ("B.draft.json", "a.draft.json"):
        store.write_artifact("draft", PID, BARE, name, b"{}", PLAIN)
    _pushed(store, 7)
    listed = {
        kind: [i.name for i in reader.list_artifacts(kind, PID, "" if kind == "export" else BARE)]
        for kind in ("report", "export", "draft")
    }
    assert listed == {
        "report": ["a.html", "B.html", "c.html"],
        "export": ["a.docx", "B.docx"],
        "draft": ["a.draft.json", "B.draft.json"],
    }


@conformance(
    "write_artifact", "delete_artifact", "list_artifacts", "read_artifact", "list_generations",
    "read_generation", "list_trash", "restore_trash", "purge_trash",
    rules=("E12.every-generation",),
)  # fmt: skip
def a_deleted_artifact_goes_with_its_generations_and_comes_back(s: Storage, monkeypatch) -> None:
    """A report, an export and a draft deleted with a reason: gone from the lists, kept in the
    trash with who, why, from where; restored with every generation and record; purged for
    good."""
    store, reader = s.open(), s.open(M2)
    reports = [_report(AX_B, i) for i in range(2)]
    for data, sidecar, _row in reports:
        _send(store, "report", AX_B, "r.html", data, sidecar)
    exports = [_export(i) for i in range(2)]
    for data, meta in exports:
        _send(store, "export", "", "e.docx", data, meta)
    draft = _draft({"report_comment": "to do"})
    _send(store, "draft", BARE, "SN1.draft.json", draft)
    before = reader.list_generations("report", PID, AX_B, "r.html")
    blank = Deletion("", "alice", "BENCH-03")
    assert _kind(store.delete_artifact, "report", PID, AX_B, "r.html", deletion=blank) == "invalid"

    store.delete_artifact("report", PID, AX_B, "r.html", deletion=WHY)
    store.delete_artifact("export", PID, "", "e.docx", deletion=WHY)
    assert reader.list_artifacts("report", PID, AX_B) == []
    assert reader.list_generations("report", PID, AX_B, "r.html") == []
    assert _kind(reader.read_artifact, "report", PID, AX_B, "r.html") == "not_found"
    assert reader.list_artifacts("export", PID, "") == []
    trash = reader.list_trash(PID)
    assert sorted(_trashed(i) for i in trash) == [
        ("export", "", "e.docx", *_why(M1), False),
        ("report", AX_B, "r.html", *_why(M1), False),
    ]
    assert all(_recent(i) for i in trash)
    for item in trash:
        assert reader.restore_trash(PID, item.entry) is False
    assert reader.list_trash(PID) == []
    assert reader.list_generations("report", PID, AX_B, "r.html") == before
    read = [reader.read_generation("report", PID, AX_B, "r.html", n) for n in (1, 2)]
    assert read == [data for data, _, _ in reports]
    assert reader.list_artifacts("report", PID, AX_B) == [reports[-1][2]]
    assert _sidecar(reader, "report", AX_B, "r.html") == reports[-1][1]
    assert reader.read_artifact("export", PID, "", "e.docx") == exports[-1][0]
    assert _sidecar(reader, "export", "", "e.docx") == exports[-1][1]

    store.delete_artifact("draft", PID, BARE, "SN1.draft.json", deletion=WHY)
    [item] = reader.list_trash(PID)
    assert _trashed(item) == ("draft", BARE, "SN1.draft.json", *_why(M1), False)
    assert reader.purge_trash(PID, [item.entry]) == [item.entry]
    assert reader.list_trash(PID) == [] and reader.list_artifacts("draft", PID, BARE) == []


@conformance(
    "write_artifact", "delete_artifact", "list_artifacts", "read_artifact", "list_generations",
    "list_trash", "restore_trash", "purge_trash",
    rules=("E10.deleted-name", "E13.purged-list"),
)  # fmt: skip
def a_deleted_artifacts_name_takes_only_its_own_replay(s: Storage, monkeypatch) -> None:
    """A report, an export or a draft pushed under a name a manager deleted is refused
    (``deleted``) and parked, nothing written; a replay of the very push made before the
    delete is done, and it stays deleted; restored, each is as it was; purged, its name is free
    again (data_format.md §6.6, decision 63)."""
    store, reader = s.open(), s.open(M2)
    report, side = _report(AX_B, 0)[:2]
    sent = {
        ("report", AX_B, "r.html"): (report, side),
        ("export", "", "e.docx"): _export(0),
        ("draft", BARE, DRAFT): (_draft({"report_comment": "to do"}), None),
    }
    for (kind, key, name), (data, sidecar) in sent.items():
        store.write_artifact(kind, PID, key, name, data, PLAIN, sidecar=sidecar)
        kept = _queued(store)
        _pushed(store, 1)
        store.delete_artifact(kind, PID, key, name, deletion=WHY)
        _replay(kept)
        _pushed(store, 1)  # the push made before the delete: done
        assert reader.list_artifacts(kind, PID, key) == []
        other = _draft({"report_comment": "mine"}) if kind == "draft" else data + b" again"
        store.write_artifact(kind, PID, key, name, other, PLAIN, sidecar=sidecar)
        _pushed(store, 0)
        assert any(f"A manager deleted the {kind}" in why for why in _parked(store, name))
        assert reader.list_artifacts(kind, PID, key) == []
        assert _kind(reader.read_artifact, kind, PID, key, name) == "not_found"

    for item in reader.list_trash(PID):
        assert reader.restore_trash(PID, item.entry) is False
    for (kind, key, name), (data, _sidecar) in sent.items():
        assert reader.read_artifact(kind, PID, key, name) == data
    assert [n for n, _ in reader.list_generations("report", PID, AX_B, "r.html")] == [1]

    store.delete_artifact("report", PID, AX_B, "r.html", deletion=WHY)
    [item] = reader.list_trash(PID)
    assert reader.purge_trash(PID, [item.entry]) == [item.entry]
    _send(store, "report", AX_B, "r.html", b"<html>new</html>", side)  # free again
    assert reader.read_artifact("report", PID, AX_B, "r.html") == b"<html>new</html>"


# ---------------------------------------------------------------------------
# Entries: a run's comments and overrides, a board's lot and fields
# ---------------------------------------------------------------------------


def _annotations() -> list[dict]:
    """Two people correct one comment from what they both saw: the second arrives after."""
    first = make_entry(COMMENT, TEST, "VREF probed on the wrong pad", None, "alice", M1, "B-1")
    verdict = make_entry(OVERRIDE, "criterion range 9", "PASS", None, "alice", M1)
    bob = make_entry(COMMENT, TEST, "re-measured: 3.31 V", first["entry_id"], "bob", M2)
    carol = make_entry(COMMENT, TEST, "fine", first["entry_id"], "carol", M3)
    return [first, verdict, bob, carol]


@conformance(
    "write_run", "add_run_annotation_entries", "read_run_annotation",
    rules=("5.5.run-annotation", "5.9.entries-order", "5.9.received-at", "E7.statuses"),
)  # fmt: skip
def annotation_entries_read_back_in_the_order_received(s: Storage, monkeypatch) -> None:
    """Saved, changed (kept, not current), already (a replay adds nothing); read back by the
    run's id and as the current run, each with when the store took it."""
    store, reader = s.open(), s.open(M2)
    runs = {key: _run(store, key) for key in BOARDS}
    _pushed(store, 2)
    for key, run in runs.items():
        notes = _annotations()
        answers = store.add_run_annotation_entries(PID, key, TID1, run.run_id, notes[:2])
        answers += store.add_run_annotation_entries(PID, key, TID1, run.run_id, notes[2:])
        assert [a["status"] for a in answers] == [SAVED, SAVED, SAVED, CHANGED]
        assert answers[3]["current"]["entry_id"] == notes[2]["entry_id"]
        replay = store.add_run_annotation_entries(PID, key, TID1, run.run_id, notes[:1])
        assert [a["status"] for a in replay] == [ALREADY]

        execution = {
            "operator": "alice",
            "completed_at": run.ended,
            "serial_number": board_of(key).serial,
        }
        for run_id in (run.run_id, CURRENT_RUN):
            read = reader.read_run_annotation(PID, key, TID1, run_id)
            assert _as_sent(read["entries"]) == notes
            assert _statuses(read["entries"]) == [REPLACED, CURRENT, CURRENT, NOT_CURRENT]
            assert (read["run_id"], read["execution"]) == (run.run_id, execution)


def _board_entries() -> list[dict]:
    """A lot corrected twice from what was seen (the later arrives after), a field cleared."""
    lot = make_entry(LOT, "", "L2611-04", None, "alice", M1, "B-1")
    customer = make_entry(DETAIL, "Customer", "ACME", None, "alice", M1)
    fixture = make_entry(DETAIL, "Fixture", "F-7", None, "alice", M1)
    fixed = make_entry(LOT, "", "L2611-05", lot["entry_id"], "bob", M2)
    late = make_entry(LOT, "", "L2611-09", lot["entry_id"], "carol", M3)
    cleared = make_entry(DETAIL, "Customer", None, customer["entry_id"], "bob", M2)
    return [lot, customer, fixture, fixed, late, cleared]


@conformance(
    "write_run", "add_board_entries", "read_board_entries", "list_boards",
    rules=("2.6.board-three-parts", "E4.list", "E5.board", "E15.statuses", "E15.creates-board"),
)  # fmt: skip
def board_entries_read_back_in_the_order_received(s: Storage, monkeypatch) -> None:
    """A board's lot and fields: saved, changed, already; a board with no run yet is created
    by its first entry and listed."""
    store, reader = s.open(), s.open(M2)
    _run(store, BARE)
    _pushed(store, 1)
    for key in (BARE, AX_C):  # a board with a run, and one with none
        assert reader.read_board_entries(PID, key) == []
        lots = _board_entries()
        answers = store.add_board_entries(PID, key, lots[:3])
        answers += store.add_board_entries(PID, key, lots[3:])
        assert [a["status"] for a in answers] == [SAVED, SAVED, SAVED, SAVED, CHANGED, SAVED]
        assert answers[4]["current"]["entry_id"] == lots[3]["entry_id"]
        assert [a["status"] for a in store.add_board_entries(PID, key, lots[:1])] == [ALREADY]

        board = reader.read_board_entries(PID, key)
        assert _as_sent(board) == lots
        assert _statuses(board) == [REPLACED, REPLACED, CURRENT, CURRENT, NOT_CURRENT, CURRENT]
        current = {"lot": "L2611-05", "details": {"Fixture": "F-7"}, "entries": board}
        assert board_data(board) == current
    assert reader.list_boards(PID) == sorted([BARE, AX_C])


# ---------------------------------------------------------------------------
# Maintenance
# ---------------------------------------------------------------------------


@conformance("write_run", "stage", "sweep_incomplete", "list_runs", "current_run", "read_run")
def a_sweep_removes_only_what_a_dead_writer_left(s: Storage, monkeypatch) -> None:
    """A run a test script started and never committed, its writer dead for over a day, is
    swept from this PC; every stored run, complete or not, reads back as it was."""
    store = s.open()
    runs = {key: (_run(store, key), _run(store, key, complete=False)) for key in BOARDS}
    _pushed(store, 4)
    abandoned = store.stage(EvidenceKey(PID, BARE, "run", TID1, CURRENT_RUN))
    (abandoned.path / "console.log").write_text("half\n", encoding="utf-8")
    days_ago = time.time() - 2 * 24 * 3600
    for path in (abandoned.path, *abandoned.path.iterdir()):
        os.utime(path, (days_ago, days_ago))
    monkeypatch.setattr(evidence_local_staging, "lease_alive", lambda *_args: False)

    later = s.open()  # this PC again, after its writer died: the stage is not its own
    assert later.sweep_incomplete(PID) == [abandoned.run_id]
    assert not abandoned.path.exists()
    assert later.sweep_incomplete(PID) == []
    reader = s.open(M2)
    for key, (complete, stopped) in runs.items():
        assert reader.list_runs(PID, key, TID1) == [stopped, complete]
        assert reader.current_run(PID, key, TID1) == complete
        assert reader.read_run(PID, key, TID1, complete.run_id) == _record(key)


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


def _row(kind: str, board: str, test: str, name: str, verdict: str, ended: str, machine: str,
         test_id: str = "", deleted: bool = False) -> tuple:  # fmt: skip
    """A search's row as compared on every storage: what it says, and where it lies below the
    storage's root (laid out as a folder's), never the root itself."""
    below = (*segments(board), *(("results", test_id, "runs") if kind == "run" else ("reports",)))
    return (kind, board, test, name, verdict, ended, machine, deleted, test_id,
            "/" + "/".join((*below, name)))  # fmt: skip


def _found(rows: list) -> list[tuple]:
    return [
        _row(r.kind, r.board, r.test, r.name, r.verdict, r.ended, r.machine_id, r.test_id,
             r.deleted)[:-1] + (r.path[len(r.folder):].replace("\\", "/"),)
        for r in rows
    ]  # fmt: skip


def _newest_first(rows: list[tuple]) -> list[tuple]:
    """The contract's order (data_format.md §6.2): newest start first (a run's from its id, a
    report's when it was made), then the name descending."""
    def start(row: tuple) -> str:
        return row[3][:19] if row[0] == "run" else row[5][:19].replace(" ", "T").replace(":", "-")

    return sorted(rows, key=lambda row: (start(row), row[3]), reverse=True)


def _measured_run(store, key: str, test_id: str, value: float, operator: str):
    """A run whose record measured ``VOut`` = *value* (a reading's name in mixed case), as
    *operator* ran it."""
    phase = {"name": "Step 1", "outcome": "PASS",
             "measurements": {"VOut": {"name": "VOut", "outcome": "PASS",
                                       "measured_value": value}}}  # fmt: skip
    record = {**saved_record("PASS", serial=board_of(key).serial, operator=operator,
                             test_id=test_id), "board": key, "phases": [phase]}  # fmt: skip
    manifest = {"operator": operator, "serial_number": board_of(key).serial}
    return store.write_run(PID, key, test_id, manifest, {"results.json": json.dumps(record)})


def _local_time_report() -> tuple[str, bytes]:
    """A report's record as a bench writes it, made at 01:30 on 21 Sep in this PC's local time:
    its time in UTC (``utc_time``; in Paris, 23:30 the day before), and the record."""
    generated = utc_time.recorded(datetime(2026, 9, 21, 1, 30))
    return generated, text_file_bytes(json.dumps({
        "metadata": {"generated": generated, "overall_verdict": "PASS"},
        "tests": [{"name": "t1"}],
    }, indent=2))  # fmt: skip


def _local_day(row: tuple) -> str:
    """The day of this PC's calendar a search row's item ended on."""
    return utc_time.shown(row[5])[:10]


@conformance("write_run", "write_artifact", "delete_run", "search")
def a_search_answers_the_same_rows_in_the_same_order(s: Storage, monkeypatch) -> None:
    """Runs and reports on a keyed board and a bare serial, a FAIL among them, a run deleted, a
    run another bench measured: each filter answers the same rows in the contract's order on
    every storage (the decided rules: a text compares by case folding, a serial alone names
    every board with it, a report is one row per test it covers, a deleted item only when
    asked, the newest first and only the newest *limit*). The operator and the bench are
    answered by every storage, upper or lower case alike (a folder's index records them); a
    measurement inside its test only, its name upper or lower case alike, where the storage
    records readings, and refused where it does not; a measurement with no test is refused on
    every storage, so none is ever asked without one (owner, 2026-11-07). A report is never
    found by a run filter. A search after more was stored finds it too (a server folder's
    search reads only what its shards gained since). The days searched are days of this PC's
    calendar (Paris here, where it can be set): a report made at 01:30 on the 21st is found on
    the 21st, on every storage, each asked the same instants (owner, 2026-11-07)."""
    with local_zone.zone("Europe/Paris"):
        _search_rows(s)


def _search_rows(s: Storage) -> None:
    """:func:`a_search_answers_the_same_rows_in_the_same_order`, in the zone it set."""
    store, other = s.open(), s.open(M2)
    keyed = _run(store, AX_B, TID1, i=0)
    bare = _run(store, BARE, TID2, i=1)
    failed = store.write_run(PID, BARE, TID1, _manifest(BARE), {"results.json": json.dumps(
        {**saved_record("FAIL", serial="SN1", test_id=TID1), "board": BARE})})  # fmt: skip
    _pushed(store, 3)
    measured = _measured_run(other, AX_B, TID2, 5.0, "Bob")
    _pushed(other, 1)
    for key, name, i in ((AX_B, "r.html", 0), (BARE, "s.html", 1)):
        data, sidecar, _info = _report(key, i)
        _send(store, "report", key, name, data, sidecar)
    late, late_record = _local_time_report()
    _send(store, "report", BARE, "u.html", b"<html>u</html>", late_record)
    store.delete_run(PID, BARE, TID2, bare.run_id, deletion=WHY)
    assert failed.verdict == "FAIL" and keyed.verdict == "PASS"

    def run_row(info, key: str, test_id: str, machine: str = M1, deleted: bool = False):
        return _row("run", key, info.test, info.run_id, info.verdict, info.ended, machine,
                    test_id, deleted)  # fmt: skip

    def report_rows(key: str, name: str, i: int) -> list[tuple]:
        _data, _sidecar, info = _report(key, i)
        return [_row("report", key, t, name, info.overall_verdict, info.generated, M1)
                for t in info.tests]  # fmt: skip

    live = _newest_first([
        run_row(keyed, AX_B, TID1), run_row(failed, BARE, TID1),
        run_row(measured, AX_B, TID2, M2),
        *report_rows(AX_B, "r.html", 0), *report_rows(BARE, "s.html", 1),
        _row("report", BARE, "t1", "u.html", "PASS", late, M1),
    ])  # fmt: skip
    gone = run_row(bare, BARE, TID2, deleted=True)
    assert {row[2] for row in live} >= {"T1", "T2", "t1", "t2", "t3"}  # the case is not vacuous

    reader = s.open(M3)

    def search(**filters: Any) -> list[tuple]:
        limit = filters.pop("limit", NEWEST)
        return _found(reader.search(PID, Filters.of(**filters), limit=limit))

    def where(keep: Callable[[tuple], bool]) -> list[tuple]:
        return [row for row in live if keep(row)]

    assert search() == live
    assert search(part_number="ax") == where(lambda r: r[1] == AX_B)
    assert search(board=label_of(AX_B).upper()) == where(lambda r: r[1] == AX_B)
    assert search(board="sn1") == live  # a serial alone: every board with it
    assert search(test="t2") == where(lambda r: r[2].casefold() == "t2")
    assert search(test="T1", part_number="AX") == where(lambda r: r[2] in ("T1", "t1")
                                                         and r[1] == AX_B)  # fmt: skip
    assert search(verdict="fail") == where(lambda r: r[4] == "FAIL")
    assert where(lambda r: r[4] == "FAIL") and where(lambda r: r[0] == "report" and r[4] == "PASS")
    day = "2026-09-21"  # the local day the 01:30 report was made on
    assert search(since=day, until=day) == where(lambda r: _local_day(r) == day)
    assert "u.html" in [r[3] for r in search(since=day, until=day)]
    assert len(where(lambda r: _local_day(r) == day)) > 2  # a day's newest, the rest left:
    assert search(since=day, until=day, limit=2) == where(lambda r: _local_day(r) == day)[:2]
    assert search(until="2026-09-20") == where(lambda r: _local_day(r) <= "2026-09-20")
    assert "u.html" not in [r[3] for r in search(until="2026-09-20")]
    assert search(since=date(2026, 9, 21)) == where(lambda r: _local_day(r) >= day)
    assert search(deleted=ONLY) == [gone]
    assert search(deleted=INCLUDE) == _newest_first([*live, gone])
    assert search(limit=2) == live[:2]
    assert search(board="SN404") == []

    runs_measured = [run_row(measured, AX_B, TID2, M2)]
    assert {"operator", "machine_id"} <= reader.SEARCH_RUN_FILTERS  # every storage
    assert search(operator="BOB") == runs_measured  # a report never
    assert search(machine_id=M2.upper()) == runs_measured
    assert search(operator="Alice") == where(lambda r: r[0] == "run" and r[6] == M1)
    no_test = Filters.of(measured=Measured("vout"))
    assert _kind(reader.search, PID, no_test) == "invalid"  # a reading's name needs its test
    vout = Measured("vOUT", 4.9, 5.0)  # recorded as VOut
    if "measured" in reader.SEARCH_RUN_FILTERS:
        assert search(measured=vout, test="t2") == runs_measured
        assert search(measured=Measured("VOUT", 5.01), test="T2") == []  # bounds inclusive: out
        assert search(measured=Measured("vout"), test="T2") == runs_measured  # any value
        assert search(measured=Measured("vout"), test="T1") == []  # inside its test only
    else:
        assert _kind(reader.search, PID, Filters.of(measured=vout, test="T2")) == "invalid"

    later = _run(store, AX_B, TID1, i=5)  # stored after the first searches
    _pushed(store, 1)
    assert search() == _newest_first([*live, run_row(later, AX_B, TID1)])


@conformance("write_run", "write_artifact", "search_choices", "search_readings")
def a_search_proposes_every_value_stored(s: Storage, monkeypatch) -> None:
    """What the search's fields propose is read from the storage itself, complete, the same on
    every storage for what it declares (``SEARCH_CHOICES``); a field it does not declare is
    empty, never invented (owner, 2026-11-07). The part numbers and boards of every item, the
    tests (the names runs recorded and reports name; a web server's: its project's test list),
    the operators (one per name whatever its case) and the benches of the runs; a test's
    readings, that test's only (its name upper or lower case alike), where readings are
    recorded, refused where they are not. Read again, they hold what was stored since."""
    store, other = s.open(), s.open(M2)
    keyed = _run(store, AX_B, TID1, i=0)  # alice, on M1
    bare = _run(store, BARE, TID1, i=1)
    _pushed(store, 2)
    measured = _measured_run(other, AX_B, TID2, 5.0, "Bob")  # on M2, its reading VOut
    _pushed(other, 1)
    data, sidecar, info = _report(AX_B, 0)
    _send(store, "report", AX_B, "r.html", data, sidecar)
    reader = s.open(M3)
    declared = reader.SEARCH_CHOICES
    found = reader.search_choices(PID)
    assert found.part_numbers == [board_of(AX_B).part_number]
    assert found.boards == sorted({label_of(AX_B), label_of(BARE)})
    if "tests" in declared:
        assert found.tests == sorted({keyed.test, bare.test, measured.test, *info.tests})
    else:
        assert "project_tests" in declared and set(found.tests) <= set(TESTS)
    assert found.operators == (["alice", "Bob"] if "operators" in declared else [])
    assert {M1, M2} <= {machine for machine, _label in found.benches}
    if "readings" in declared:
        assert reader.search_readings(PID, "t2") == [("VOut", "")]
        assert reader.search_readings(PID, "T1") == []  # another test's readings: none
    else:
        assert _kind(reader.search_readings, PID, "t2") == "invalid"

    cx = key_of("CX", "A", "SN7")
    store.write_run(PID, cx, TID1, {"operator": "ALICE", "serial_number": "SN7"}, _files(cx, 7))
    _pushed(store, 1)
    again = reader.search_choices(PID)
    assert again.part_numbers == sorted([board_of(AX_B).part_number, "CX"])
    assert label_of(cx) in again.boards
    if "operators" in declared:  # ALICE is alice: one name
        assert [o.casefold() for o in again.operators] == ["alice", "bob"]


# ---------------------------------------------------------------------------
# Every action on every storage
# ---------------------------------------------------------------------------


def test_every_action_that_reaches_a_server_has_a_case():
    covered = frozenset().union(*(case.actions for case in CASES))
    assert sorted(covered - PROTOCOL) == []  # a case names only the interface's actions
    assert sorted(PROTOCOL - LOCAL - covered) == []


def test_every_known_gap_is_a_storage_and_an_action_a_case_runs():
    covered = frozenset().union(*(case.actions for case in CASES), (case.name for case in CASES))
    for backend, action in KNOWN_GAPS:
        assert backend in BACKENDS and action in covered, (backend, action)


def test_every_storage_runs_the_suite():
    """The folder and the database run here; every other storage in its own file."""
    here = Path(__file__).resolve().parent
    for backend in set(BACKENDS) - {"folder", "db"}:
        assert (here / f"test_evidence_conformance_{backend}.py").is_file(), backend


#: The storage type a user picks (its backend) -> the storage this suite runs it as.
SUITE_OF = {"filesystem": "folder", "ftps": "ftps", "sftp": "sftp", "http": "api", "postgres": "db"}


def test_every_storage_a_user_can_pick_runs_the_suite():
    """Coverage by introspection over what users are offered: every registered storage type
    that is not hidden is one this suite runs (the database since it was un-hidden, slice 10),
    so a type offered tomorrow fails here until it runs every action."""
    from procedo.core.evidence_config import store_types

    offered = {t.backend for t in store_types()}
    assert "postgres" in offered
    assert sorted(offered - set(SUITE_OF)) == []
    assert {SUITE_OF[b] for b in offered} <= set(BACKENDS)


def _db_storage(request, tmp_path: Path) -> Storage:
    """The evidence database (PostgreSQL and its attachment storage), signed in as its admin."""
    import evidence_db_seed as seed

    from procedo.core.evidence_store_db import DatabaseStore, DbEndpoint

    pg = request.getfixturevalue("pg_database")
    admin = pg.connect()
    request.addfinalizer(admin.close)
    seed.set_up(admin, tmp_path / "attachments")

    def opener(*, machine: str) -> DatabaseStore:
        project = tmp_path / f"project-{machine}"
        (project / "config").mkdir(parents=True, exist_ok=True)
        (project / "config" / "config.json").write_text(json.dumps({"project_id": PID}), "utf-8")
        return DatabaseStore(
            DbEndpoint(pg.host, pg.port, pg.database),
            pg.connect,
            machine_id=machine,
            project_root=project,
            project_id=PID,
            outbox=tmp_path / f"outbox-{machine}",
            cache_dir=tmp_path / f"cache-{machine}",
        )

    return Storage("db", opener)


@pytest.fixture
def storage(request, backend, tmp_path) -> Storage:
    if backend == "folder":
        return Storage("folder", folder_backend(tmp_path).build)
    assert backend == "db"
    return _db_storage(request, tmp_path)


@pytest.mark.parametrize(
    ("case", "backend"),
    params("folder") + params("db", pytest.mark.servers, pytest.mark.postgres),
)
def test_every_action_reads_back_as_it_went_in(case, backend, storage, monkeypatch):
    run_case(case, storage, monkeypatch)
