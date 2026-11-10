"""Runs: push (E1), read (E3), annotate (E7), delete and restore (E11).

A run is written once (rule 7). Its JSON is kept as sent, less the answer fields and its
``null`` fields; every read adds the answer fields back: ``project_id``, ``pushed_at``,
``current``, ``annotation``, ``missing`` and the ``deleted_*`` fields. Its board is answered
with the first spelling met, in NFC (§2). It is filed under its ``test_id``, never its name:
its ``test`` is a record of the name it ran under, kept as sent (NFC), and no rename is
followed (§4.5, §6.9). A purged run's ``run_id`` is kept: a push of it is refused (§6.1,
§6.7).
"""

from __future__ import annotations

import copy
import hashlib

from . import faults, offer, registration
from .call import Answer, Call
from .entries import (
    add_entries,
    annotation_data,
    annotation_group,
    check_received,
    entries_answer,
)
from .errors import ApiError, bad_request, conflict, not_a_manager, not_found
from .multipart import Part
from .pointers import NOT_MOVED, apply_pointer_rule, hand_over, is_current
from .state import API_STORE_ID, Project, State, StoredRun, board_key
from .storages import check_refs, referenced_again, store_parts
from .validate import (
    MACHINE_ID,
    drop_nulls,
    entries_of,
    is_time,
    nfc,
    opt_str,
    opt_time,
    req_pattern,
    validate_annotation_entry,
    validate_run,
)

RUN_ANSWER_FIELDS = (
    "pushed_at",
    "current",
    "annotation",
    "missing",
    "deleted_at",
    "deleted_by",
    "deleted_by_name",
    "deleted_bench",
    "deleted_station",
    "deleted_reason",
    "deleted_was_current",
    "import",
)
SUMMARY_DROPS = ("phases", "files", "missing", "bench_values", "extra")
DELETED_FIELDS = ("deleted_at", "deleted_by", "deleted_bench")


# ---- Answers ------------------------------------------------------------------------------------


def _set(obj: dict, key: str, value: object) -> None:
    if value is None:
        obj.pop(key, None)
    else:
        obj[key] = value


def run_answer(state: State, project: Project, run: StoredRun, *, summary: bool) -> dict:
    broken = state.broken
    obj = copy.deepcopy(run.obj)
    board = project.boards[run.board_key]
    obj["project_id"] = project.project_id
    obj["serial"] = board.serial
    if obj["format_version"] >= 2:
        _set(obj, "part_number", board.part_number)
        _set(obj, "board_revision", board.board_revision)
    obj["pushed_at"] = run.pushed_at
    obj["current"] = is_current(project, run)
    if run.entries or broken("5.5.annotation-absent"):  # the fault: an empty one, not absent
        obj["annotation"] = {
            "format_version": 1,
            "run_id": run.run_id,
            "entries": entries_answer(
                run.entries, annotation_group, last_wins=broken("5.9.current-flag")
            ),
            "data": annotation_data(run.entries, at_received=broken("5.9.data")),
        }
    if run.deleted:
        obj.update(run.deleted)
    if broken("5.8.runfile-role-mimetype"):
        for item in obj["files"]:
            item.pop("mimetype", None)
    if summary:
        for key in SUMMARY_DROPS:
            if not (key == "extra" and broken("5.5.run-summary-light")):
                obj.pop(key, None)
        if broken("5.5.run-project-id"):
            del obj["project_id"]
        if broken("5.5.run-current"):
            obj["current"] = False
    elif broken("5.5.run-full"):
        obj.pop("bench_values", None)
    return obj


def run_full(
    state: State, project: Project, run: StoredRun, have: set[str], sent: set[str]
) -> tuple[dict, list[Part]]:
    """The Run with everything, and the parts of its ``api`` files not in ``have`` nor ``sent``."""
    obj = run_answer(state, project, run, summary=False)
    missing = {}
    parts = []
    for item in obj["files"]:
        if item["store_id"] != API_STORE_ID:
            continue
        sha = item["sha256"]
        if sha in have or sha in sent:
            status = state.blobs.check(project.project_id, sha, item["size"])
            data = None
        else:
            data, status = state.blobs.read(project.project_id, sha, item["size"])
        if status is not None:
            missing[item["name"]] = status
            continue
        if data is not None:
            parts.append(
                Part(sha, item["name"], item.get("mimetype") or "application/octet-stream", data)
            )
            sent.add(sha)
    if missing and not state.broken("5.5.run-missing"):
        obj["missing"] = missing
    return obj, parts


def find_run(call: Call) -> tuple[Project, StoredRun]:
    run_id = call.params["run_id"]
    project = call.state.projects.get(call.params["project_id"])
    run = project.runs.get(run_id) if project else None
    if run is None:
        raise not_found(f"No run {run_id} in this project.")
    return project, run


# ---- E1 -----------------------------------------------------------------------------------------


def _stored_run(run: dict) -> dict:
    """The Run as kept: less the answer fields, and less every ``null`` field (§2)."""
    obj = drop_nulls(
        {key: copy.deepcopy(value) for key, value in run.items() if key not in RUN_ANSWER_FIELDS}
    )
    phases = []
    for phase in obj["phases"]:
        phase = drop_nulls(phase)
        phase["measurements"] = [drop_nulls(item) for item in phase["measurements"]]
        phases.append(phase)
    obj["phases"] = phases
    obj["files"] = [drop_nulls(item) for item in obj["files"]]
    return obj


def push_run(call: Call) -> Answer:
    state, run = call.state, call.json()
    project_id = call.params["project_id"]
    importing = call.importing()
    broken = state.broken
    if broken("import.ignored-without"):
        importing = call.import_asked()
    version = validate_run(run, lot_ok=broken("5.5.lot-format-2-400"))
    if run.get("project_id") not in (None, project_id) and not broken("E1.project-id-match"):
        raise bad_request("The run's project_id is not the path's.")
    if not call.importing():  # a well-formed bench's run: what a server registering its
        offer.note(offer.BENCH_RUN)  # boards may refuse (§3.3)
    keeps_sent = importing or faults.on("2.2.answer-fields-ignored")
    pushed_at = (opt_time(run, "pushed_at", "run") if keeps_sent else None) or state.now()
    if importing and broken("import.role-and-flag"):
        pushed_at = state.now()
    run_id = run["run_id"]

    project = state.projects.get(project_id)
    if project is not None and run_id in project.purged:
        raise conflict(
            "deleted",
            f"A manager purged the run {run_id}; it is never stored again.",
            run_id=run_id,
        )
    raw_sha = hashlib.sha256(call.raw_json or b"").hexdigest()
    if faults.on("2.12.digest-decoded") and not call.parts and call.wire:
        raw_sha = hashlib.sha256(call.wire).hexdigest()
    held = project.runs.get(run_id) if project else None
    replayed = importing and broken("import.contradiction")
    if held is not None and held.raw_sha != raw_sha and not (broken("E1.run-conflict") or replayed):
        raise conflict(
            "run_conflict",
            f"A different run is stored under {run_id}.",
            kind="corrupt",
            run_id=run_id,
        )
    # §6.8, §3.3: a replay is already, an import a record: neither is checked
    imported = importing and not broken("U1.import-exempt")
    if (held is None and not imported) or broken("U1.operator-exempt"):
        offer.known_operator(state.calls, state.people, run.get("operator"), "operator",
                             unnamed_refused=state.unnamed_refused)  # fmt: skip
    if (held is None and not importing) or broken("E1.unknown-board-exempt"):
        _registered_board(state, project, run, version)
    refs = [(item["sha256"], item["size"], item["store_id"]) for item in run["files"]]
    try:
        to_write = check_refs(state, project_id, refs, call.parts, "run")
    except ApiError:
        if held is None and faults.on("3.7.G2-all-or-nothing"):
            _store_new(call, run, version, raw_sha, pushed_at)
        raise
    store_parts(state, project_id, to_write, call.parts)  # a part sent again heals a damaged blob
    if held is not None:  # a replay; a deleted run stays deleted
        assert project is not None
        return Answer(_push_result("already", project, held, run if broken("E1.answer") else None))

    referenced_again(state, project_id, refs)
    project, stored = _store_new(call, run, version, raw_sha, pushed_at)
    if importing:  # an imported run's board is one of the server's (§3.3)
        registration.register(state, project_id, stored.board_key)
    state.seen_bench(run["machine_id"], run.get("station_id"))
    if importing and broken("import.bench-seen-not-imported"):
        state.benches[run["machine_id"]]["first_seen"] = pushed_at
    if not importing:
        apply_pointer_rule(state, project, stored)
    elif "E6" in state.calls:  # imported, E6 offered: no pointer moves, the importer sets it
        # the 5.13 fault makes an imported incomplete run current too
        stopped = not stored.obj["complete"]
        if broken("import.E1-no-pointer") or (broken("5.13.incomplete-never-current") and stopped):
            apply_pointer_rule(state, project, stored)
    elif not broken("import.E1-rule-without-E6"):  # imported, no E6: the server picks (§6.7)
        apply_pointer_rule(state, project, stored, tie_to_greater=True)
    result = _push_result("saved", project, stored, run if broken("E1.answer") else None)
    if faults.on("2.4.case-first-spelling"):
        result.update({k: run[k] for k in ("serial", "part_number", "board_revision") if k in run})
    return Answer(result)


def _board_key_of(run: dict, version: int) -> tuple:
    if version >= 2:
        return board_key(run.get("part_number"), run.get("board_revision"), run["serial"])
    return board_key(None, None, run["serial"])


def _registered_board(state: State, project: Project | None, run: dict, version: int) -> None:
    """§3.3 ``boards_registered``: a run on a board that is not registered is ``409
    unknown_board``, kind ``invalid``, one of the server's own codes (§4.3; ``registration``)."""
    project_id = project.project_id if project is not None else ""
    key = _board_key_of(run, version)
    registration.check(
        state, project_id, key, run["serial"], let_through=faults.on("E1.unknown-board")
    )


def _store_new(
    call: Call, run: dict, version: int, raw_sha: str, pushed_at: str
) -> tuple[Project, StoredRun]:
    """A new run stored in its project (made when new), on its board (made when new)."""
    state, project_id, run_id = call.state, call.params["project_id"], run["run_id"]
    broken = state.broken
    project = state.ensure_project(project_id, run.get("project_name"))
    if version >= 2 or broken("5.5.format-1-board"):
        board = state.ensure_board(
            project, run["serial"], run.get("part_number"), run.get("board_revision")
        )
    else:
        board = state.ensure_board(project, run["serial"], None, None)
    stored_obj = _stored_run(run)
    stored_obj["test"] = nfc(run["test"])  # a record of the name it ran under
    stored = StoredRun(
        run_id=run_id,
        obj=stored_obj,
        raw_sha=raw_sha,
        pushed_at=pushed_at,
        board_key=board.key,
        test_id=run["test_id"],
    )
    project.runs[run_id] = stored
    return project, stored


def _push_result(status: str, project: Project, run: StoredRun, sent: dict | None) -> dict:
    """E1's answer; ``sent``: the board as the run spelled it, a fault."""
    board = project.boards[run.board_key]
    result = {"status": status, "run_id": run.run_id, "serial": board.serial}
    _set(result, "part_number", board.part_number)
    _set(result, "board_revision", board.board_revision)
    if sent is not None:
        for key in ("serial", "part_number", "board_revision"):
            _set(result, key, sent.get(key))
    result.update(
        {
            "test_id": run.test_id,
            "current": is_current(project, run),
            "pushed_at": run.pushed_at,
        }
    )
    return result


# ---- E3 -----------------------------------------------------------------------------------------


def read_run(call: Call) -> Answer:
    if call.is_read_twin():
        extra = sorted(set(call.json()) - {"have"})
        if extra:
            raise bad_request(f"This read takes no {extra[0]!r} in its body.")
    have = call.have()
    project, run = find_run(call)
    obj, parts = run_full(call.state, project, run, have, set())
    return Answer(obj, parts)


# ---- E7 -----------------------------------------------------------------------------------------


def save_annotation(call: Call) -> Answer:
    state, body = call.state, call.json()
    importing = call.importing()
    any_value = state.broken("E7.override-value-400")
    entries = entries_of(body, lambda e: validate_annotation_entry(e, any_value=any_value))
    check_received(entries, importing=importing)
    _project, run = find_run(call)
    if run.deleted is not None:
        raise conflict(
            "deleted", f"A manager deleted the run {run.run_id}: it takes no entry until restored."
        )
    results = add_entries(state, run.entries, entries, annotation_group, importing=importing)
    for entry in entries:
        state.seen_bench(entry["machine_id"], entry.get("bench"))
    return Answer({"results": results})


# ---- E11 ----------------------------------------------------------------------------------------


def manager_or_import(call: Call, *, allow_import: bool) -> bool:
    """A manager's login; or, for a delete, an importer replaying one (§6.10). Returns importing."""
    importing = allow_import and call.importing()
    if "manager" not in call.user.roles and not importing:
        raise not_a_manager()
    return importing


def deletion(call: Call, importing: bool) -> dict:
    """The deleted_* fields of a delete (E11, E12); refused without a reason. The bench's
    ``station`` and the name typed (``deleted_by_name``) are kept as sent, when sent (§6.7);
    ``deleted_by`` is the login."""
    body = call.json()
    machine_id = req_pattern(body, "machine_id", "request", MACHINE_ID, "8 lower-case hex digits")
    broken = call.state.broken
    reason = body.get("reason")
    if broken("E11.reason-400") and not isinstance(reason, str):
        reason = ""
    elif not isinstance(reason, str) or not reason.strip():
        if not broken("E11.reason-400"):
            raise bad_request("A delete needs its reason, as the manager wrote it.")
    fields = {
        "deleted_at": call.state.now(),
        "deleted_by": call.user.name,
        "deleted_bench": machine_id,
        "deleted_reason": reason,
    }
    for sent, kept in (("station", "deleted_station"), ("deleted_by_name", "deleted_by_name")):
        if body.get(sent) == "" and broken("E11.never-empty-strings"):
            continue  # a "" taken as "not known", the delete made: the fault
        value = opt_str(body, sent, "delete", empty=False)  # "" is never "not known" (§2.2)
        if value is not None:
            fields[kept] = value
    if broken("5.5.deleted-by-login") and isinstance(body.get("deleted_by"), str):
        fields["deleted_by"] = body["deleted_by"]
    if importing:
        for key in DELETED_FIELDS:
            value = body.get(key)
            if value is None:
                continue
            if key == "deleted_at" and not is_time(value):
                raise bad_request("deleted_at must be a UTC time.")
            if not isinstance(value, str):
                raise bad_request(f"{key} must be a string.")
            fields[key] = value
    return fields


def delete_run(call: Call) -> Answer:
    importing = manager_or_import(call, allow_import=True)
    fields = deletion(call, importing)
    sent = call.json().get("deleted_was_current")
    if importing and sent is not None and not isinstance(sent, bool):
        raise bad_request("deleted_was_current must be true or false.")
    project, run = find_run(call)
    moved = NOT_MOVED
    if run.deleted is None:  # deleting a deleted run is a replay: unchanged, no pointer
        fields["deleted_was_current"] = sent if importing and sent is not None else None
        if fields["deleted_was_current"] is None:
            fields["deleted_was_current"] = is_current(project, run)
        run.deleted = fields
        # imported: nothing handed on where E6 sets the pointers, as a bench's where it is not
        with_e6 = "E6" in call.state.calls
        if (
            not importing
            or (with_e6 and call.state.broken("import.E11-E12-delete"))
            or (not with_e6 and not call.state.broken("import.delete-hands-on-without-E6"))
        ):
            moved = hand_over(call.state, project, run, fields["deleted_bench"])
    call.state.seen_bench(fields["deleted_bench"])
    answer: dict = {"run": run_answer(call.state, project, run, summary=True)}
    if moved is not NOT_MOVED and not call.state.broken("E11.answer-pointer"):
        answer["pointer"] = moved  # the new pointer, or null: the test has none any more
    return Answer(answer)


def restore_run(call: Call) -> Answer:
    manager_or_import(call, allow_import=call.state.broken("import.never-restore-purge"))
    body = call.json()
    machine_id = req_pattern(body, "machine_id", "request", MACHINE_ID, "8 lower-case hex digits")
    project, run = find_run(call)
    answer: dict = {}
    if run.deleted is not None:  # restoring a live run is a replay: unchanged, no pointer
        was_current = run.deleted.get("deleted_was_current")
        run.deleted = None
        if was_current and call.state.broken("E11.restore-stays-history"):
            project.pointers.pop((run.board_key, run.test_id), None)
        if was_current:  # current again by the pointer rule (§6.7), no special case
            pointer = apply_pointer_rule(call.state, project, run, machine_id)
            if pointer is not None:
                answer["pointer"] = pointer
    call.state.seen_bench(machine_id)
    return Answer({"run": run_answer(call.state, project, run, summary=True), **answer})
