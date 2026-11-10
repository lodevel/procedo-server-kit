"""Which run is current, per board and test id (data_format.md §4.13, §6.4, §6.7).

A board has one mark per ``test_id``, never per name: an old and a new test of one name
each keep theirs. The pointer rule: a complete run becomes current when its ``test_id`` has
no pointer on that board,
or when its ``ended`` is strictly later than the pointer's; a tie, an older run and an
incomplete one leave it. A demoted pointer keeps its ``ended``. Deleting (or purging) the
current run hands the pointer to the newest remaining complete run not deleted, else leaves no
pointer. E6 is the manual escape; an importer sets the pointer as the source had it. A server
not offering E6 picks the current run of an import itself: the rule, a tie to the greater
``run_id`` (contract §6.7, ``import.E1-rule-without-E6``).
"""

from __future__ import annotations

import copy

from . import faults
from .call import Answer, Call
from .errors import bad_request, conflict, not_found
from .state import Project, State, StoredRun, board_key
from .validate import (
    MACHINE_ID,
    RUN_ID,
    TEST_ID,
    check_board_name,
    is_time,
    opt_time,
    req_pattern,
)


def is_current(project: Project, run: StoredRun) -> bool:
    pointer = project.pointers.get((run.board_key, run.test_id))
    return pointer is not None and pointer.get("run_id") == run.run_id


def apply_pointer_rule(
    state: State,
    project: Project,
    run: StoredRun,
    machine_id: str | None = None,
    *,
    tie_to_greater: bool = False,
) -> dict | None:
    """The run becomes current by the rule (a push, a restore). Returns the new pointer, or None.

    The pointer's ``machine_id`` is the bench that moved it: the run's own on a push, the
    manager's on a restore. *tie_to_greater*: an import on a server without E6, where runs come
    in any order: a tie goes to the greater ``run_id`` (§6.7).
    """
    if not run.obj["complete"]:
        return None
    key = (run.board_key, run.test_id)
    ended = run.obj["ended"]
    pointer = project.pointers.get(key)
    held = pointer.get("ended") if pointer is not None else None
    tie_wins = state.broken("5.13.tie-or-older-leaves") or (
        tie_to_greater and pointer is not None and run.run_id > str(pointer.get("run_id") or "")
    )
    if held is not None and (ended < held if tie_wins else ended <= held):
        return None
    new = {
        "test_id": run.test_id,
        "run_id": run.run_id,
        "ended": ended,
        "machine_id": machine_id or run.obj["machine_id"],
        "updated_at": state.now(),
    }
    project.pointers[key] = new
    return copy.deepcopy(new)


NOT_MOVED = object()  # hand_over's answer when the run was not current: no `pointer` answered


def hand_over(state: State, project: Project, run: StoredRun, machine_id: str) -> object:
    """Deleting or purging the current run hands the pointer on (§4.13, §6.7).

    To the newest remaining complete run not deleted (the latest ``ended``, then the greatest
    ``run_id``); none remaining, the test has no pointer on that board any more. Returns the new
    pointer, ``None`` when none remains, or ``NOT_MOVED`` when the run was not current.
    """
    key = (run.board_key, run.test_id)
    pointer = project.pointers.get(key)
    if pointer is None or pointer.get("run_id") != run.run_id:
        return NOT_MOVED
    remaining = [
        other
        for other in project.runs.values()
        if (other.board_key, other.test_id) == key
        and other.run_id != run.run_id
        and other.deleted is None
        and (other.obj["complete"] or state.broken("5.13.handoff-complete-only"))
    ]
    if state.broken("5.13.handoff-tiebreak"):
        best = max(remaining, key=lambda other: other.run_id, default=None)
    else:
        # .get: an incomplete run has no ended, and only a server broken on purpose
        # (5.13.handoff-complete-only) counts it among the heirs
        best = max(
            remaining, key=lambda other: (other.obj.get("ended") or "", other.run_id), default=None
        )
    if best is None:
        if state.broken("5.13.handoff-none"):
            pointer.update(run_id=None, machine_id=machine_id, updated_at=state.now())
        else:
            del project.pointers[key]
        return None
    new = {
        "test_id": run.test_id,
        "run_id": best.run_id,
        "ended": best.obj.get("ended"),  # none: the incomplete heir of the fault above
        "machine_id": machine_id,
        "updated_at": state.now(),
    }
    if new["ended"] is None:
        del new["ended"]
    project.pointers[key] = new
    return copy.deepcopy(new)


def _as_read(pointer: dict | None, replaces: dict | None) -> bool:
    """Whether ``replaces`` is the pointer as it is now."""
    if pointer is None:
        return replaces is None
    if replaces is None:
        return False
    return replaces.get("run_id") == pointer.get("run_id") and replaces.get("ended") == pointer.get(
        "ended"
    )


def set_current(call: Call) -> Answer:
    """E6: set (promote) or clear (demote) the current run of a test id on a board."""
    state, body = call.state, call.json()
    part_number, revision = call.board_address()
    serial = check_board_name(call.params["serial"], "serial")
    test_id = call.params["test_id"]
    if TEST_ID.fullmatch(test_id) is None:
        raise bad_request("E6 names the test by its test_id, a lower-case UUID.")
    importing = call.importing()
    if "run_id" not in body:
        raise bad_request("The body needs run_id: the run to make current, or null to clear.")
    target = body["run_id"]
    if target is not None and (not isinstance(target, str) or RUN_ID.fullmatch(target) is None):
        raise bad_request("run_id must be a run id, or null.")
    replaces = body.get("replaces")
    if "replaces" not in body and not importing:
        raise bad_request("The body needs replaces: the pointer as last read, or null.")
    if replaces is not None and not importing:
        if not isinstance(replaces, dict) or "run_id" not in replaces or "ended" not in replaces:
            raise bad_request("replaces is null or {run_id, ended}.")
        if replaces["ended"] is not None and not is_time(replaces["ended"]):
            raise bad_request("replaces.ended must be a UTC time.")
    machine_id = req_pattern(body, "machine_id", "request", MACHINE_ID, "8 lower-case hex digits")
    ended = opt_time(body, "ended", "request") if importing else None
    updated_at = opt_time(body, "updated_at", "request") if importing else None

    project = state.projects.get(call.params["project_id"])
    key = (board_key(part_number, revision, serial), test_id)
    run = None
    if target is not None:
        run = project.runs.get(target) if project else None
        if run is None or (run.board_key, run.test_id) != key:
            raise not_found(f"{target} is not a run of the test {test_id} on this board.")
        if run.deleted is not None and not state.broken("E6.not-its-run-404"):
            # an import's too: a pointer never names a deleted run
            raise not_found(f"{target} was deleted by a manager: it cannot be current.")
        if not run.obj["complete"] and not state.broken("E6.incomplete-400"):
            raise bad_request("Only a complete run can be current.")
    state.seen_bench(machine_id)

    if importing:  # §6.4, §6.10: exactly as the source had it, replaces not checked
        project = state.ensure_project(call.params["project_id"], None)
        new = {"test_id": test_id, "run_id": target}
        if run is not None and (ended is None or state.broken("import.E6-as-sent")):
            ended = run.obj["ended"]
        if ended is not None:
            new["ended"] = ended
        new.update({"machine_id": machine_id, "updated_at": updated_at or state.now()})
        project.pointers[key] = new
        return Answer({"status": "saved", "pointer": copy.deepcopy(new)})

    pointer = project.pointers.get(key) if project else None
    stale_clear = replaces is not None and state.broken("E6.clear-no-pointer")
    if pointer is None and target is None and not stale_clear:  # satisfied: a replay (§6.4)
        return Answer({"status": "saved", "pointer": {"test_id": test_id, "run_id": None}})
    # A request the pointer already satisfies is a replay: saved, nothing written (§6.4).
    replay = pointer is not None and pointer.get("run_id") == target
    if replay and not state.broken("E6.replay"):
        return Answer({"status": "saved", "pointer": copy.deepcopy(pointer)})
    matches = _as_read(pointer, replaces) or (pointer is None and state.broken("E6.no-pointer-yet"))
    if not matches and not state.broken("E6.changed"):
        current = copy.deepcopy(pointer) if pointer else {"test_id": test_id, "run_id": None}
        if faults.on("4.5.refusal-inside-200"):
            raise conflict("changed", "The pointer moved meanwhile.", current=current)
        return Answer({"status": "changed", "pointer": current})
    assert project is not None
    new = {"test_id": test_id, "run_id": target}
    kept = run.obj.get("ended") if run else (pointer or {}).get("ended")
    if run is not None and pointer is not None and state.broken("5.13.set-by-hand-ended"):
        kept = pointer.get("ended", kept)
    if run is None and state.broken("5.13.demoted-keeps-ended"):
        kept = None
    if kept is not None:
        new["ended"] = kept
    new.update({"machine_id": machine_id, "updated_at": state.now()})
    project.pointers[key] = new
    return Answer({"status": "saved", "pointer": copy.deepcopy(new)})
