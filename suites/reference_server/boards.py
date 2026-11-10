"""Boards: list (E4), a board's results (E5), its lot and fields (E15).

A board is its part number, revision and serial (§4.11); the board with neither of the first
two is the "no part number" group, where every format-1 result lands. A board comes into being
with its first run (E1), entry (E15) or report or draft (E10), and is never removed.
"""

from __future__ import annotations

import copy

from . import offer, registration
from .call import Answer, Call
from .entries import (
    add_entries,
    board_group,
    board_group_folded,
    board_values,
    check_received,
    entries_answer,
)
from .errors import bad_request, not_found
from .multipart import Part
from .runs import run_answer, run_full
from .state import Board, Project, State, board_key
from .validate import check_board_name, entries_of, name_key, nfc, validate_board_entry


def board_answer(state: State, project: Project, board: Board, *, with_entries: bool) -> dict:
    lot, details = board_values(board.entries, last_lot=state.broken("5.11.lot-details"))
    obj: dict = {"format_version": 2, "serial": board.serial}
    if board.part_number is not None:
        obj["part_number"] = board.part_number
    if board.board_revision is not None:
        obj["board_revision"] = board.board_revision
    if lot is not None:
        obj["lot"] = lot
    obj["details"] = details
    if with_entries or state.broken("E4.list"):  # the fault: E4 lists each with its entries
        obj["entries"] = entries_answer(board.entries, board_group)
    obj["created_at"] = board.created_at
    ended = [
        run.obj["ended"]
        for run in project.runs.values()
        if run.board_key == board.key
        and (run.deleted is None or state.broken("5.11.board-times"))
        and run.obj.get("ended")
    ]
    if ended:
        # the fault: deleted runs counted, and the newest run pushed taken, not the latest ended
        newest = [r.obj["ended"] for r in project.runs.values() if r.obj.get("ended") in ended]
        broken = state.broken("5.11.board-times")
        obj["last_ended"] = newest[-1] if broken else max(ended)
    return obj


def _filter(value: str | None, empty_any: bool) -> tuple[bool, str | None]:
    """A list filter: absent, any; empty, none; else that name. (``empty_any``: empty is
    any, a fault.)"""
    if value is None or (empty_any and value == ""):
        return False, None
    return True, name_key(value) if value else None


def list_boards(call: Call) -> Answer:
    """E4: by part number, revision, serial, each ascending by case folding, "none" first."""
    state = call.state
    empty_any = state.broken("E4.filters")
    by_part, part = _filter(call.query.one("part_number"), empty_any)
    by_revision, revision = _filter(call.query.one("board_revision"), empty_any)
    project = state.projects.get(call.params["project_id"])
    if project is None and state.broken("6.1.unseen-project-empty"):
        raise not_found("This server never saw this project.")
    boards = []
    if project is not None:
        order = (
            (lambda k: (k[0] or "", k[1] or "", k[2]))
            if not state.broken("E4.order")
            else (lambda k: _spelled(project.boards[k]))
        )
        run_boards = {run.board_key for run in project.runs.values()}
        for key in sorted(project.boards, key=order):
            if not registration.listed(state, project.project_id, key):
                continue  # §3.3: on a server registering its boards, the registered ones only
            if key not in run_boards and state.broken("E4.registered-listed"):
                continue  # the fault: a board no run was pushed to is not listed
            if by_part and key[0] != part:
                continue
            if by_revision and key[1] != revision:
                continue
            boards.append(board_answer(state, project, project.boards[key], with_entries=False))
    return Answer({"boards": boards})


def _spelled(board: Board) -> tuple:
    """A board's names as spelled, case kept: the order of the fault of ``E4.order``."""
    return (board.part_number or "", board.board_revision or "", board.serial)


def board_results(call: Call) -> Answer:
    """E5: the board with its entries, then per test id its pointer and runs (§6.3)."""
    state = call.state
    if call.is_read_twin():  # the board stays in the query; only have and include in the body
        extra = sorted(set(call.json()) - {"have", "include"})
        if extra:
            raise bad_request(f"This read takes no {extra[0]!r} in its body.")
        include = call.json().get("include")
    else:
        include = call.query.one("include")
    if include not in (None, "current"):
        raise bad_request("include is current, or absent.")
    if state.broken("5.12.include-current"):
        include = None
    part_number, revision = call.board_address()
    serial = check_board_name(call.params["serial"], "serial")
    have = call.have()
    project = state.projects.get(call.params["project_id"])
    key = board_key(part_number, revision, serial)
    board = project.boards.get(key) if project else None
    answer: dict = {"format_version": 2}
    if board is None:
        unknown: dict = {"format_version": 2, "serial": nfc(serial)}
        if part_number is not None:
            unknown["part_number"] = nfc(part_number)
        if revision is not None:
            unknown["board_revision"] = nfc(revision)
        unknown.update({"details": {}, "entries": []})
        answer.update({"board": unknown, "tests": []})
        if include:
            answer["current_runs"] = []
        return Answer(answer)
    assert project is not None
    by_test: dict[str, list] = {}
    for run in project.runs.values():
        if run.board_key == key and run.deleted is None:
            by_test.setdefault(run.test_id, []).append(run)
        elif run.board_key == key and state.broken("5.12.all-deleted-not-listed"):
            by_test.setdefault(run.test_id, [])
    tests = []
    current_runs = []
    parts: list[Part] = []
    sent: set[str] = set()
    for test_id in sorted(by_test, reverse=state.broken("5.12.tests-by-test-id")):
        runs = sorted(by_test[test_id], key=lambda run: run.run_id, reverse=True)
        pointer = project.pointers.get((key, test_id))
        tests.append(
            {
                "test_id": test_id,
                "pointer": copy.deepcopy(pointer) if pointer else None,
                "runs": [run_answer(state, project, run, summary=True) for run in runs],
            }
        )
        if include and pointer and pointer.get("run_id") in project.runs:
            obj, run_parts = run_full(state, project, project.runs[pointer["run_id"]], have, sent)
            current_runs.append(obj)
            parts += run_parts
    board_obj = board_answer(state, project, board, with_entries=True)
    answer.update({"board": board_obj, "tests": tests})
    if include:
        answer["current_runs"] = current_runs
    return Answer(answer, parts)


def save_entries(call: Call) -> Answer:
    """E15: add lot or field entries to a board; it is created when new (§6.8)."""
    state, body = call.state, call.json()
    part_number, revision = call.board_address()
    serial = check_board_name(call.params["serial"], "serial")
    importing = call.importing()
    entries = entries_of(body, validate_board_entry)
    check_received(entries, importing=importing)
    project_id = call.params["project_id"]
    key = board_key(part_number, revision, serial)
    if not importing:
        offer.note(offer.BENCH_RUN)
    # §3.3: a bench's entry on a board that is not registered is refused, never an import's
    if not importing or state.broken("3.3.import-any-call"):  # the fault: an import checked
        let_through = state.broken("E15.unknown-board")
        registration.check(state, project_id, key, serial, let_through=let_through)
    project = state.ensure_project(project_id, None)
    board = state.ensure_board(project, serial, part_number, revision)
    if importing and not state.broken("3.3.import-registers"):  # its board is the server's
        registration.register(state, project_id, board.key)
    group = board_group_folded if state.broken("5.11.field-name-exact") else board_group
    results = add_entries(state, board.entries, entries, group, importing=importing)
    for entry in entries:
        state.seen_bench(entry["machine_id"], entry.get("bench"))
    return Answer({"results": results})
