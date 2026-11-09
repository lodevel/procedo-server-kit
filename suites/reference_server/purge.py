"""E13: remove for good what was deleted strictly longer ago than the retention period (§6.7).

With ``items`` (runs by ``run_id``, artifacts by kind + board + name), those deleted items too,
now, whatever their age: an item the server no longer holds is skipped (already purged); an
item that is not deleted refuses the whole request. The answer counts what went (one per run,
one per artifact name) and lists it, what expired included (``purged``): each run by its
``run_id``, each artifact by its kind, its board's names as stored and its name.

A purged run's ``run_id`` is kept, so a push of it is refused (``409 deleted``). A purge of a
run a pointer still names (only after an imported delete) hands the pointer on, as a delete
does. The server reclaims the ``api`` blobs nothing in the project references any more; a blob
in a declared storage it cannot reach goes on that storage's list (S3), by project, for the app
to delete. The project's files, bundle and releases count as references (§3.2).
"""

from __future__ import annotations

from datetime import timedelta

from .artifacts import named_slot_key
from .call import Answer, Call
from .errors import bad_request, not_a_manager
from .pointers import hand_over
from .state import ArtifactSlot, Project
from .storages import project_refs, reclaim
from .validate import MACHINE_ID, RUN_ID, RUN_ID_SHAPE, parse_time, req_pattern

ARTIFACT_ITEM_KEYS = {"kind", "serial", "part_number", "board_revision", "name"}


def _named(body: dict) -> tuple[list[str], list[tuple]] | None:
    """The ``items`` of a purge request: run ids and artifact slot keys; None when absent."""
    items = body.get("items")
    if items is None:
        return None
    if not isinstance(items, list):
        raise bad_request(
            "items is a list of runs ({run_id}) and artifacts ({kind, serial, name})."
        )
    run_ids, slot_keys = [], []
    for item in items:
        if not isinstance(item, dict):
            raise bad_request("Each item is a JSON object.")
        if "run_id" in item:
            if set(item) != {"run_id"} or not isinstance(item["run_id"], str):
                raise bad_request("A run item is {run_id} alone.")
            if RUN_ID.fullmatch(item["run_id"]) is None:
                raise bad_request(f"A run_id is {RUN_ID_SHAPE}.")
            run_ids.append(item["run_id"])
        else:
            extra = sorted(set(item) - ARTIFACT_ITEM_KEYS)
            if extra:
                raise bad_request(f"An artifact item takes no {extra[0]!r}.")
            slot_keys.append(named_slot_key(item))
    return run_ids, slot_keys


def _artifact_ref(project: Project, slot: ArtifactSlot) -> dict:
    """An artifact name as E13's answer lists it: kind, its board's names, name (§6.7)."""
    ref = {"kind": slot.kind, "serial": ""}
    board = project.boards.get(slot.board_key)
    if board is not None:
        ref["serial"] = board.serial
        for key in ("part_number", "board_revision"):
            if getattr(board, key) is not None:
                ref[key] = getattr(board, key)
    ref["name"] = slot.name
    return ref


def purge(call: Call) -> Answer:
    state = call.state
    if "manager" not in call.user.roles:
        raise not_a_manager()
    machine_id = req_pattern(
        call.json(), "machine_id", "request", MACHINE_ID, "8 lower-case hex digits"
    )
    named = _named(call.json())
    project = state.projects.get(call.params["project_id"])
    if project is None:
        state.seen_bench(machine_id)
        return Answer({"runs": 0, "artifacts": 0, "purged": {"runs": [], "artifacts": []}})
    cutoff = state.clock() - timedelta(days=state.retention_days)

    def expired(deleted: dict | None) -> bool:
        if deleted is None:
            return False
        if state.broken("E13.expired-purged"):
            return parse_time(deleted["deleted_at"]) <= cutoff
        return parse_time(deleted["deleted_at"]) < cutoff

    runs = [run_id for run_id, run in project.runs.items() if expired(run.deleted)]
    slots = [key for key, slot in project.artifacts.items() if expired(slot.deleted)]
    if named is not None:  # the named items as well as the retention sweep (§6.7)
        named_runs = [run_id for run_id in dict.fromkeys(named[0]) if run_id in project.runs]
        named_slots = [key for key in dict.fromkeys(named[1]) if key in project.artifacts]
        live = [r for r in named_runs if project.runs[r].deleted is None]
        live += [project.artifacts[k].name for k in named_slots if not project.artifacts[k].deleted]
        if live:
            raise bad_request(
                f"Only a deleted item is purged; {live[0]} is not deleted.", live=live
            )
        runs += [run_id for run_id in named_runs if run_id not in runs]
        slots += [key for key in named_slots if key not in slots]
    state.seen_bench(machine_id)

    before = project_refs(project)
    pid = project.project_id
    listed_before = {sid: set(by.get(pid, ())) for sid, by in state.to_delete.items()}
    went = {
        "runs": runs,
        "artifacts": [_artifact_ref(project, project.artifacts[k]) for k in slots],
    }
    for run_id in runs:
        if not state.broken("E13.pointer-of-purged"):
            hand_over(state, project, project.runs[run_id], machine_id)
        del project.runs[run_id]
        project.purged.add(run_id)
    generations = sum(len(project.artifacts[key].items) for key in slots)
    for key in slots:
        del project.artifacts[key]
    if state.broken("5.11.board-never-removed"):
        held = {run.board_key for run in project.runs.values()}
        for key in [key for key in project.boards if key not in held]:
            del project.boards[key]
    reclaim(state, project, before)
    counted = generations if state.broken("E13.counts-to-delete") else len(slots)
    answer: dict = {"runs": len(runs), "artifacts": counted, "purged": went}
    # §6.4 E13, §7.3: every blob of this project nothing references now in its declared
    # storages, not only those this purge freed.
    unreferenced = {
        str(store_id): [f"sha256:{sha}" for sha in sorted(listed[project.project_id])]
        for store_id, listed in sorted(state.to_delete.items())
        if listed.get(project.project_id)
    }
    if state.broken("E13.to-delete-scope"):  # only what this purge freed
        unreferenced = {
            sid: kept
            for sid, shas in unreferenced.items()
            if (kept := [v for v in shas if v[7:] not in listed_before.get(int(sid), set())])
        }
    if unreferenced and not state.broken("7.3.to-delete-in-purge"):
        answer["to_delete"] = unreferenced  # absent when none: never {} (§6.4 E13)
    elif state.broken("E13.to-delete-absent"):
        answer["to_delete"] = {}
    return Answer(answer)
