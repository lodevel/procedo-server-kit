"""Slice 2a of the board identity (docs/plans/board_identity.md): every store places and
finds a board by its KEY (:mod:`procedo.core.evidence_board`).

The "no part number" board keys as its bare serial and lives where every board always
lived (``<root>/<serial>/``); any other board lives in ``<root>/parts/<pn>/<rev>/<serial>/``.
Two boards sharing a serial must keep apart everything a board holds: runs, the pointer,
reports, the draft and the run annotations, on every backend.

The scenarios are plain functions over a :class:`Backend`, run here on the folder store, and
on a real FTPS server by
``test_evidence_board_layout_ftps.py``. What only the folder walkers do (the sweep, the
Move's late runs, the in-project check) is pinned on the folder store alone.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from procedo.core import (
    evidence_board,
    evidence_config,
    evidence_index,
    evidence_outbox,
    evidence_rename,
    evidence_search,
    evidence_store_fs_shared,
    evidence_transfer,
    outbox_reads,
)
from procedo.core.annotation_entries import FIELD, make_entry
from procedo.core.evidence_board import NO_SERIAL, PARTS, Board, board_of, key_of
from procedo.core.evidence_search_choices import known_choices
from procedo.core.evidence_shared_layout import check_new_key, is_reserved, would_collide
from procedo.core.evidence_shared_manifest import MANIFEST, POINTER
from procedo.core.evidence_store import (
    CURRENT_RUN,
    ArtifactInfo,
    EvidenceError,
    EvidenceKey,
)
from procedo.core.evidence_store_fs_shared import FilesystemStore, create_folder
from procedo.core.evidence_trash import Deletion
from procedo.core.result_annotations import global_draft_name, load_global_draft, save_global_draft
from procedo.core.testing import id_for, saved_record

PID = "a3f19c2b-0000-4000-8000-000000000000"
WHY = Deletion("wrong board fitted", "alice", "BENCH-03")
T12 = datetime(2026, 9, 11, 12, 0, 0, tzinfo=timezone.utc)

AX_B = key_of("AX", "B", "SN1")
AX_C = key_of("AX", "C", "SN1")
BARE = "SN1"  # the "no part number" board: today's layout
BOARDS = (AX_B, AX_C, BARE)
T1 = id_for("t1")  # the test's hidden id, which every store call names it by


@dataclass
class Backend:
    """A store kind under test: ``build(machine)`` opens a store of this project on it, and
    ``root`` is its evidence folder as the server keeps it on disk (to see where things
    landed)."""

    name: str
    build: Callable[..., Any]
    root: Path


def _project(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "config" / "config.json").write_text(json.dumps({"project_id": PID}), "utf-8")
    return root


def folder_backend(tmp_path: Path) -> Backend:
    root = tmp_path / "evidence"
    root.mkdir()

    def build(*, machine: str = "m1") -> FilesystemStore:
        return FilesystemStore(
            root,
            machine_id=machine,
            project_root=_project(tmp_path, f"project-{machine}"),
            outbox=tmp_path / f"outbox-{machine}",
        )

    return Backend("folder", build, root)


@pytest.fixture
def backend(tmp_path) -> Backend:
    return folder_backend(tmp_path)


def _clock(monkeypatch, hour: int) -> None:
    monkeypatch.setattr(evidence_store_fs_shared, "_utc_now", lambda: T12.replace(hour=hour))


def _result(key: str) -> dict:
    """A run's record as a commit leaves it, so what is read back is exactly what went in."""
    return {**saved_record("PASS", serial=board_of(key).serial), "board": key}


def _run(store, key: str, test_id: str = T1, *, plot: bytes = b""):
    files: dict[str, bytes | str] = {"results.json": json.dumps(_result(key))}
    if plot:
        files["rise.png"] = plot
    return store.write_run(
        PID, key, test_id, {"operator": "alice", "serial_number": board_of(key).serial}, files
    )


def _info(key: str, name: str = "r.html") -> ArtifactInfo:
    return ArtifactInfo(name, "2026-09-20T09:14:41Z", f"scenario {key}", "alice", "PASS", "m1")


def _row(info: ArtifactInfo) -> tuple:
    return info.name, info.generated, info.scenario, info.operator, info.overall_verdict


def _folder(b: Backend, key: str) -> Path:
    return b.root.joinpath(*key.split("/"))


# ---------------------------------------------------------------------------
# Scenarios, every backend
# ---------------------------------------------------------------------------


def two_boards_sharing_a_serial_keep_everything_apart(b: Backend, monkeypatch) -> None:
    """Runs, pointers, reports, drafts and annotations: each board's own, store then read
    back giving exactly what went in; and the runs land in each board's folder."""
    store = b.build()
    runs = {}
    for hour, key in enumerate(BOARDS, start=10):  # the bare board's run is the newest
        _clock(monkeypatch, hour)
        runs[key] = _run(store, key, plot=key.encode())
        store.write_artifact(
            "report", PID, key, "r.html", f"<html>{key}</html>".encode(), _info(key)
        )
        save_global_draft(store, PID, key, {"board": key})
    assert len(store.flush_outbox()) == 3 * len(BOARDS)

    for key in BOARDS:
        run = runs[key]
        published = _folder(b, key) / "results" / T1 / "runs" / run.run_id
        assert (published / MANIFEST).is_file(), key
        assert json.loads((_folder(b, key) / "results" / T1 / POINTER).read_text())["run_id"] == (
            run.run_id
        )
        assert store.current_run(PID, key, T1) == run  # its own, not the newest of the serial
        assert store.list_runs(PID, key, T1) == [run]
        assert store.read_run(PID, key, T1, CURRENT_RUN) == _result(key)
        plot = store.local_path(EvidenceKey(PID, key, "run", T1, run.run_id, "rise.png"))
        assert Path(plot).read_bytes() == key.encode()

        assert store.read_artifact("report", PID, key, "r.html") == f"<html>{key}</html>".encode()
        assert [_row(i) for i in store.list_artifacts("report", PID, key)] == [_row(_info(key))]
        report = store.local_path(EvidenceKey(PID, key, "report", name="r.html"))
        assert Path(report).read_bytes() == f"<html>{key}</html>".encode()

        assert [i.name for i in store.list_artifacts("draft", PID, key)] == ["SN1.draft.json"]
        assert load_global_draft(store, PID, key)["board"] == key

    for key in BOARDS:
        entry = make_entry("comment", "t1", f"seen on {key}", None, "alice", "m1")
        store.add_run_annotation_entries(PID, key, T1, runs[key].run_id, [entry])
    for key in BOARDS:
        entries = store.read_run_annotation(PID, key, T1, runs[key].run_id)["entries"]
        assert [e[FIELD["comment"]] for e in entries] == [f"seen on {key}"]

    assert (b.root / "parts" / "AX" / "B" / "SN1" / "reports" / "r.html").is_file()
    assert (b.root / "SN1" / "reports" / "SN1.draft.json").is_file()


def list_serials_names_every_board_by_its_key(b: Backend, monkeypatch) -> None:
    store = b.build()
    for key in BOARDS:
        _run(store, key)
    store.flush_outbox()
    assert store.list_boards(PID) == sorted(BOARDS)
    assert PARTS not in store.list_boards(PID)  # the folder of the keyed boards is no board


def odd_part_numbers_round_trip(b: Backend, monkeypatch) -> None:
    """A part number holding ``/`` and one named ``CON`` are one folder each, and the key
    read back from the folders is the board that was written."""
    store = b.build()
    boards = [Board("AX/2210", "B", "SN7"), Board("CON", "rev.", "SN8"), Board("", "B", "SN9")]
    runs = {}
    for board in boards:
        runs[board.key()] = _run(store, board.key())
        store.write_artifact(
            "report",
            PID,
            board.key(),
            "q.html",
            board.label().encode(),
            _info(board.key(), "q.html"),
        )
    store.flush_outbox()
    assert (b.root / "parts" / "AX%2F2210" / "B" / "SN7").is_dir()
    assert (b.root / "parts" / "%43ON" / "rev%2E" / "SN8").is_dir()
    assert (b.root / "parts" / "_none" / "B" / "SN9").is_dir()
    listed = store.list_boards(PID)
    assert {board_of(k) for k in listed} == set(boards)
    for key in listed:
        assert store.current_run(PID, key, T1) == runs[key]
        assert store.read_run(PID, key, T1, CURRENT_RUN) == _result(key)
        assert store.read_artifact("report", PID, key, "q.html") == board_of(key).label().encode()


#: Serials holding what a folder name cannot (doubt 3 of the plan: a serial accepts any
#: character, GS1 and company serials hold ``/ - . :``), and the folder each is written as.
ODD_SERIALS = {
    "21:0042/7": "21%3A0042%2F7",
    "A\\B*?<>|": "A%5CB%2A%3F%3C%3E%7C",
    '50% "x"': "50%25 %22x%22",
    "SN.": "SN%2E",
    "CON": "%43ON",
    "_none": "%5Fnone",
    "parts/AX/B/SN1": "parts%2FAX%2FB%2FSN1",  # one folder: never read as a keyed board
    # The names the store keeps for itself at the root, and the no-serial folder (owner,
    # 2026-11-06: any serial at all): their first character escaped.
    "parts": "%70arts",
    "exports": "%65xports",
    "INDEX": "%49NDEX",
    ".trash": "%2Etrash",
    "_unsorted": "%5Funsorted",
}


def any_serial_round_trips(b: Backend, monkeypatch) -> None:
    """Every serial, with or without a part number, is one folder: its run, report, draft,
    run comment and board lot stored, then read back from the store's own listing, give
    exactly what went in, and the board read back is the one typed."""
    store, reader = b.build(), b.build(machine="m2")
    boards = [Board(pn, "B" if pn else "", sn) for sn in ODD_SERIALS for pn in ("AX/1", "")]
    runs = {}
    for hour, board in enumerate(boards):
        _clock(monkeypatch, hour % 24)
        key = board.key()
        runs[key] = _run(store, key, plot=board.serial.encode())
        body = board.label().encode()
        store.write_artifact("report", PID, key, "r.html", body, _info(key))
        save_global_draft(store, PID, key, {"board": board.serial})
    store.flush_outbox()
    for board in boards:
        lot = make_entry("lot", "", f"lot of {board.serial}", None, "alice", "m1")
        store.add_board_entries(PID, board.key(), [lot])

    for serial, folder in ODD_SERIALS.items():
        assert (b.root / folder / "results" / T1).is_dir(), serial
        assert (b.root / "parts" / "AX%2F1" / "B" / folder / "reports" / "r.html").is_file()
    listed = reader.list_boards(PID)
    assert {board_of(key) for key in listed} == set(boards)
    for key in listed:
        board = board_of(key)
        assert evidence_board.label_of(key) == board.label()  # as typed, never escaped
        assert reader.current_run(PID, key, T1) == runs[key]
        assert reader.read_run(PID, key, T1, CURRENT_RUN) == _result(key)
        plot = reader.local_path(EvidenceKey(PID, key, "run", T1, runs[key].run_id, "rise.png"))
        assert Path(plot).read_bytes() == board.serial.encode()
        assert reader.read_artifact("report", PID, key, "r.html") == board.label().encode()
        assert [_row(i) for i in reader.list_artifacts("report", PID, key)] == [_row(_info(key))]
        assert load_global_draft(reader, PID, key)["board"] == board.serial
        entry = make_entry("comment", "t1", f"seen on {board.serial}", None, "alice", "m2")
        reader.add_run_annotation_entries(PID, key, T1, runs[key].run_id, [entry])
        notes = store.read_run_annotation(PID, key, T1, runs[key].run_id)["entries"]
        assert [e[FIELD["comment"]] for e in notes] == [f"seen on {board.serial}"]
        lots = reader.read_board_entries(PID, key)
        assert [e[FIELD["lot"]] for e in lots] == [f"lot of {board.serial}"]


def no_serial_keeps_apart_from_a_serial_named_unsorted(b: Backend, monkeypatch) -> None:
    """The runs saved with no serial (``NO_SERIAL``) and the board whose serial is typed
    ``_unsorted`` are two boards: each one's run, report and draft read back as stored."""
    store = b.build()
    typed = key_of("", "", "_unsorted")
    runs = {}
    for hour, key in enumerate((NO_SERIAL, typed)):
        _clock(monkeypatch, hour)
        runs[key] = _run(store, key)
        store.write_artifact("report", PID, key, "r.html", key.encode(), _info(key))
        save_global_draft(store, PID, key, {"board": key})
    store.flush_outbox()
    assert (b.root / "_unsorted" / "results" / T1).is_dir()
    assert (b.root / "%5Funsorted" / "results" / T1).is_dir()
    assert sorted(store.list_boards(PID)) == sorted((NO_SERIAL, typed))
    for key in (NO_SERIAL, typed):
        assert store.current_run(PID, key, T1) == runs[key]
        assert store.read_artifact("report", PID, key, "r.html") == key.encode()
        assert load_global_draft(store, PID, key)["board"] == key


def queued_runs_flush_to_their_boards_folders(b: Backend, monkeypatch) -> None:
    """The outbox carries the key: a keyed board's run lands under ``parts/``, and an
    entry queued the way every entry was before (a bare serial) where it always did. A
    comment saved while its run still waits follows it to the same board."""
    store = b.build()
    _clock(monkeypatch, 10)
    keyed = _run(store, AX_B)
    _clock(monkeypatch, 11)
    bare = _run(store, BARE)
    queued = {e["serial"] for _, e in evidence_outbox.entries(store)}
    assert queued == {AX_B, BARE}
    entry = make_entry("comment", "t1", "queued note", None, "alice", "m1")
    outbox_reads.add_annotation_entries(
        store, EvidenceKey(PID, AX_B, "run", T1, keyed.run_id), [entry]
    )
    assert len(store.flush_outbox()) == 3
    assert (b.root / "parts" / "AX" / "B" / "SN1" / "results" / T1 / "runs" / keyed.run_id).is_dir()
    assert (b.root / "SN1" / "results" / T1 / "runs" / bare.run_id).is_dir()
    assert not (b.root / "SN1" / "results" / T1 / "runs" / keyed.run_id).exists()
    notes = store.read_run_annotation(PID, AX_B, T1, keyed.run_id)["entries"]
    assert [e["entry_id"] for e in notes] == [entry["entry_id"]]
    assert store.current_run(PID, AX_B, T1) == keyed
    assert store.current_run(PID, BARE, T1) == bare


def trash_and_restore_a_keyed_boards_run_and_report(b: Backend, monkeypatch) -> None:
    store = b.build()
    runs = {key: _run(store, key) for key in (AX_B, AX_C)}
    store.write_artifact("report", PID, AX_B, "r.html", b"<html/>", _info(AX_B))
    store.flush_outbox()

    store.delete_run(PID, AX_B, T1, CURRENT_RUN, deletion=WHY)
    store.delete_artifact("report", PID, AX_B, "r.html", deletion=WHY)
    assert store.current_run(PID, AX_B, T1) is None
    assert store.current_run(PID, AX_C, T1) == runs[AX_C]  # the other board kept its run
    assert store.list_artifacts("report", PID, AX_B) == []
    items = store.list_trash(PID)
    assert {i.board for i in items} == {AX_B}
    assert all(i.files[0].startswith("parts/AX/B/SN1/") for i in items)

    run_entry = next(i.entry for i in items if i.what == "run")
    report_entry = next(i.entry for i in items if i.what != "run")
    assert store.restore_trash(PID, run_entry) is True  # Current again
    store.restore_trash(PID, report_entry)
    assert store.current_run(PID, AX_B, T1) == runs[AX_B]
    assert [i.name for i in store.list_artifacts("report", PID, AX_B)] == ["r.html"]
    assert store.list_trash(PID) == []


def the_index_keys_records_by_the_board(b: Backend, monkeypatch) -> None:
    """The writer's record, the run's search row and a second bench's reconciliation all
    file a keyed board's report under its key, so two boards' reports of one name are
    two records and each board lists only its own."""
    store = b.build()
    runs = {}
    for key in (AX_B, AX_C):
        runs[key] = _run(store, key)
        store.write_artifact("report", PID, key, "r.html", b"<html/>", _info(key))
    store.flush_outbox()
    records = evidence_index.shards(store, PID).records
    assert {k for k in records if k[0] == "report"} == {
        ("report", AX_B, "r.html"),
        ("report", AX_C, "r.html"),
    }
    assert {k for k in records if k[0] == "run"} == {
        ("run", AX_B, runs[AX_B].run_id),
        ("run", AX_C, runs[AX_C].run_id),
    }
    assert [_row(i) for i in evidence_index.known(store, PID, AX_B)] == [_row(_info(AX_B))]
    assert [_row(i) for i in evidence_index.known(store, PID, AX_C)] == [_row(_info(AX_C))]
    assert evidence_index.known(store, PID, BARE) == []

    other = b.build(machine="m2")  # a bench with no record of its own reconciles
    assert [_row(i) for i in other.list_artifacts("report", PID, AX_C)] == [_row(_info(AX_C))]
    own = [json.loads(line) for line in (b.root / "index" / "m2.jsonl").read_text().splitlines()]
    assert {(r["kind"], r["serial"], r["name"]) for r in own} == {("report", AX_C, "r.html")}


SCENARIOS = [
    two_boards_sharing_a_serial_keep_everything_apart,
    list_serials_names_every_board_by_its_key,
    odd_part_numbers_round_trip,
    any_serial_round_trips,
    no_serial_keeps_apart_from_a_serial_named_unsorted,
    queued_runs_flush_to_their_boards_folders,
    trash_and_restore_a_keyed_boards_run_and_report,
    the_index_keys_records_by_the_board,
]


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.__name__ for s in SCENARIOS])
def test_every_store_places_a_board_by_its_key(scenario, backend, monkeypatch):
    scenario(backend, monkeypatch)


# ---------------------------------------------------------------------------
# The key's rules
# ---------------------------------------------------------------------------


def test_a_bare_serial_named_parts_is_refused_and_never_listed(tmp_path):
    b = folder_backend(tmp_path)
    store = b.build()
    assert is_reserved(PARTS) and would_collide("Parts")
    with pytest.raises(EvidenceError) as ei:
        store.stage(EvidenceKey(PID, PARTS, "run", T1, CURRENT_RUN))
    assert ei.value.kind == "invalid"
    with pytest.raises(EvidenceError) as ei:
        store.current_run(PID, PARTS, T1)
    assert ei.value.kind == "invalid"
    _run(store, AX_B)
    store.flush_outbox()
    assert store.list_boards(PID) == [AX_B]


@pytest.mark.parametrize(
    "key",
    [
        "parts/_none/_none/SN1",  # that board's key is SN1
        "parts/a:b/B/SN1",  # a part number written unescaped
        "parts/%41X/B/SN1",  # escaped where nothing needed it
        "parts/AX/B",  # no serial
        "parts/AX/B/SN1/x",
        "parts/AX//SN1",
        "parts/AX/B/CON",  # a serial is escaped like a part number: %43ON
        "parts/AX/B/..",
        "parts/AX/B/a:b",  # a serial written unescaped
        "SN:1",  # the same, with no part number
        "%53N1",  # escaped where nothing needed it
        "SN1 ",  # blanks around it
    ],
)
def test_a_new_run_is_only_written_under_a_boards_own_key(tmp_path, key):
    store = folder_backend(tmp_path).build()
    with pytest.raises(EvidenceError) as ei:
        store.stage(EvidenceKey(PID, key, "run", T1, CURRENT_RUN))
    assert ei.value.kind == "invalid"
    with pytest.raises(EvidenceError):
        check_new_key(key)


def test_the_keys_of_the_two_forms_are_accepted_as_written():
    for key in (BARE, AX_B, key_of("AX/2210", "", "SN1"), key_of("", "B", "SN1")):
        check_new_key(key)


@pytest.mark.parametrize("flavour", ["posix", "windows"])
def test_a_case_variant_of_parts_never_receives_keyed_boards(tmp_path, flavour):
    """``Parts`` is a board an older run may have made (it was no reserved name): the keyed
    boards' folder is spelled ``parts`` exactly, so on a tree that matches names without
    case (``windows``) the keyed boards never go inside that board, and it is never made
    beside a case variant."""
    b = folder_backend(tmp_path)
    store = b.build()
    store.io.flavour = flavour
    old = _run(store, "SN5")
    store.flush_outbox()
    (b.root / "SN5").rename(b.root / "Parts")  # as an older build could have named a board
    run = _run(store, AX_B)
    store.flush_outbox()
    assert store.list_boards(PID) == ["Parts"]  # an existing board keeps reading
    assert store.current_run(PID, "Parts", T1) == old
    assert not (b.root / PARTS).exists()
    parked = store.quarantined_items(PID)
    assert [i.id for i in parked] == [run.run_id]
    assert "collides case-insensitively" in parked[0].reason


# ---------------------------------------------------------------------------
# What walks the evidence folder (folder store)
# ---------------------------------------------------------------------------


def test_the_sweep_reaches_keyed_boards_and_never_reads_parts_as_a_board(tmp_path):
    b = folder_backend(tmp_path)
    store = b.build()
    odd = key_of("results", "B", "runs")  # under the old walk, parts/results/B/runs/* were runs
    kept = _run(store, odd)
    _run(store, AX_B)
    store.flush_outbox()
    stale = _folder(b, AX_B) / "results" / T1 / "runs" / "20260911T000000Z-dead"
    stale.mkdir()
    (stale / "results.json").write_text("{}")
    old = time.time() - 3 * 24 * 3600
    for path in (stale / "results.json", stale, *(_folder(b, odd).rglob("*")), _folder(b, odd)):
        os.utime(path, (old, old))
    assert store.sweep_incomplete(PID) == [stale.name]
    assert store.current_run(PID, odd, T1) == kept


def test_a_move_names_a_keyed_boards_late_run():
    held = {("parts", "AX", "B", "SN1", "results", "t1", "runs", "r1", MANIFEST): ("x", 1)}
    left = [
        "parts/AX/B/SN1/results/t1/runs/r1/annotations/000001.json",  # a comment on a moved run
        "parts/AX/B/SN1/results/t1/runs/r2/results.json",
        "SN1/results/t1/runs/r3/results.json",
    ]
    assert evidence_transfer._late_runs(left, held) == [
        "SN1/results/t1/runs/r3",
        "parts/AX/B/SN1/results/t1/runs/r2",
    ]


def test_keyed_boards_count_as_evidence_in_the_project_folder(tmp_path):
    folder = tmp_path / "evidence"
    (folder / PARTS / "AX" / "B" / "SN1" / "results").mkdir(parents=True)
    assert not evidence_config._holds_evidence(folder)
    (folder / PARTS / "AX" / "B" / "SN1" / "results" / "results.json").write_text("{}")
    assert evidence_config._holds_evidence(folder)


def test_the_folder_move_refuses_an_unfinished_keyed_generation(tmp_path):
    root = tmp_path / "evidence"
    generation = root / PARTS / "AX" / "B" / "SN1" / "results" / T1 / "runs" / "r1"
    generation.mkdir(parents=True)
    (generation / "results.json").write_text("{}")
    with pytest.raises(EvidenceError, match="unfinished"):
        evidence_rename._scan(root, ".conversion-x.lock", None)
    (generation / MANIFEST).write_text("{}")
    evidence_rename._scan(root, ".conversion-x.lock", None)


def test_a_search_row_points_into_its_boards_folder():
    folder = Path("/evidence")
    run = evidence_search.item_path(folder, evidence_index.RUN, AX_B, T1, "r1")
    assert run == str(folder / "parts" / "AX" / "B" / "SN1" / "results" / T1 / "runs" / "r1")
    report = evidence_search.item_path(folder, evidence_index.REPORT, BARE, "", "r.html")
    assert report == str(folder / "SN1" / "reports" / "r.html")
    server = "ftps://host:21/ev"  # a server's root: its identity, joined with ``/``
    assert evidence_search.item_path(server, evidence_index.REPORT, AX_B, "", "r.html") == (
        "ftps://host:21/ev/parts/AX/B/SN1/reports/r.html"
    )


def test_the_draft_is_named_by_the_serial_in_its_boards_folder():
    assert global_draft_name(AX_B) == global_draft_name(BARE) == "SN1.draft.json"
    odd = key_of("", "", "21:0042/7")
    assert global_draft_name(odd) == global_draft_name(key_of("AX", "B", "21:0042/7"))
    assert global_draft_name(odd) == "21%3A0042%2F7.draft.json"


def test_a_search_finds_a_board_by_its_label_and_shows_it_as_typed(tmp_path):
    """The index carries the key; Search proposes, filters and shows the board as a person
    reads it."""
    b = folder_backend(tmp_path)
    store = b.build()
    odd = key_of("AX/1", "B", "21:0042/7")
    for key in (odd, BARE, AX_B):
        _run(store, key)
    store.flush_outbox()
    boards = known_choices([b.root]).boards
    assert boards == ["AX rev B · SN1", "AX/1 rev B · 21:0042/7", "SN1"]
    rows = evidence_search.search_reports_and_runs([b.root], board="ax/1 REV b · 21:0042/7")
    assert [(r.board, r.board_label) for r in rows] == [(odd, "AX/1 rev B · 21:0042/7")]


def test_a_serial_alone_finds_every_board_with_it_and_the_part_number_narrows(tmp_path):
    """Search's Board filter: the serial alone finds every board with that serial, whatever
    its part number; the full label finds just that board; the Part number box keeps only
    that part number's boards (owner, 2026-11-06)."""
    b = folder_backend(tmp_path)
    store = b.build()
    other = key_of("CX", "A", "SN1")
    for key in (BARE, AX_B, AX_C, other, NO_SERIAL):
        _run(store, key)
    store.flush_outbox()

    def boards(**filters) -> set[str]:
        return {r.board for r in evidence_search.search_reports_and_runs([b.root], **filters)}

    assert boards(board="sn1") == {BARE, AX_B, AX_C, other}
    assert boards(board="AX rev B · SN1") == {AX_B}
    assert boards(part_number="ax") == {AX_B, AX_C}
    assert boards(part_number="AX", board="SN1") == {AX_B, AX_C}
    assert boards(board="No serial") == {NO_SERIAL}
    assert boards(board="_unsorted") == set()  # the no-serial runs have no serial
    known = known_choices([b.root])
    part_numbers, labels = known.part_numbers, known.boards
    assert part_numbers == ["AX", "CX"]
    assert "No serial" in labels


def test_a_store_whose_folder_was_made_by_create_folder_takes_keyed_boards(tmp_path):
    """The folder is made when it is chosen; a keyed board's first run then makes
    ``parts/`` and its folders below it, and nothing else at the root."""
    store = FilesystemStore(
        tmp_path / "chosen",
        machine_id="m1",
        project_root=_project(tmp_path, "project"),
        outbox=tmp_path / "outbox",
    )
    create_folder(store, PID)
    _run(store, AX_B)
    store.flush_outbox()
    assert sorted(p.name for p in store.root.iterdir()) == [
        ".evidence-project.json",
        "index",
        "parts",
    ]
