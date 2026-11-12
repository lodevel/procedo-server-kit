"""Artifacts: list (E8), read (E9), push (E10), delete and restore (E12). §4.10, §6.6.

``kind`` + the board + ``name`` identify one. A report or an export is never replaced: a push
that is not its name's newest generation (the same ``sha256`` and ``info``) is the next
``generation``. A draft is replaced only when ``replaces`` is the held draft's ``sha256``, which
the app computes and the server stores and answers, never computes. The first report or draft
of a board creates the board. A report is its record (``info``, required); started with
``report_pages`` False, the server keeps that record alone, as a server may (§5.10): the report
is answered as a draft is, its page's ``sha256`` and no file.
"""

from __future__ import annotations

import base64
import copy
import json
from collections.abc import Callable
from functools import cmp_to_key

from . import offer, registration
from .call import Answer, Call
from .errors import bad_request, conflict, not_found, unsupported_filter
from .multipart import Part
from .runs import deletion, manager_or_import
from .state import API_STORE_ID, ArtifactItem, ArtifactSlot, Project, State, board_key
from .storages import check_refs, reclaim, referenced_again, store_parts
from .validate import (
    ARTIFACT_KINDS,
    MACHINE_ID,
    VERDICTS,
    check_board_name,
    drop_nulls,
    is_int,
    is_time,
    name_key,
    nfc,
    req_pattern,
    validate_artifact,
)

ARTIFACT_ANSWER_FIELDS = (
    "generation",
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
LIST_DROPS = ("info", "content")
LIST_FILTERS = {
    "kind",
    "serial",
    "part_number",
    "board_revision",
    "work_order",
    "test",
    "verdict",
    "from",
    "to",
    "deleted",
    "generations",
    "include",
    "limit",
    "cursor",
}
DEFAULT_LIMIT = 100
MAX_LIMIT = 500
# A part's Content-Type follows the name's extension: an Artifact has no mimetype (§3.3).
EXTENSION_TYPES = {
    ".html": "text/html",
    ".json": "application/json",
    ".md": "text/markdown",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


def part_type(name: str) -> str:
    dot = name.rfind(".")
    extension = name[dot:].lower() if dot >= 0 else ""
    return EXTENSION_TYPES.get(extension, "application/octet-stream")


def artifact_board(art: dict) -> tuple:
    """The board an artifact belongs to; a format-1 artifact's is the "no part number" one."""
    if art["serial"] == "":
        return (None, None, "")
    if art["format_version"] < 2:
        return board_key(None, None, art["serial"])
    return board_key(art.get("part_number"), art.get("board_revision"), art["serial"])


def artifact_answer(
    state: State, project: Project, slot: ArtifactSlot, item: ArtifactItem, *, full: bool
) -> dict:
    obj = copy.deepcopy(item.obj)
    obj["name"] = slot.name
    board = project.boards.get(slot.board_key)
    if board is not None:
        obj["serial"] = board.serial
        if obj["format_version"] >= 2:
            for key in ("part_number", "board_revision"):
                value = getattr(board, key)
                if value is None:
                    obj.pop(key, None)
                else:
                    obj[key] = value
    if obj.get("store_id") == API_STORE_ID and not state.broken("5.10.artifact-missing"):
        status = state.blobs.check(project.project_id, obj["sha256"], obj["size"])
        if status is not None:
            obj["missing"] = {slot.name: status}
    if slot.deleted:
        obj.update(slot.deleted)
    if state.broken("5.10.artifact-fields"):
        obj.pop("work_order", None)
        obj.pop("fields", None)
    if state.broken("5.10.report-page-optional") and slot.kind == "report":
        obj.pop("info", None)  # the fault: the record dropped
    if not full and not state.broken("E8.light"):
        for key in LIST_DROPS:
            obj.pop(key, None)
    return obj


def _slot_key(kind: str, board: tuple, name: str) -> tuple:
    return (kind, board, name_key(name))


# ---- E10 ----------------------------------------------------------------------------------------


def _same(held: dict, art: dict, file_only: bool = False) -> bool:
    """The same file and the same record: what the folder storages compare (§6.6).
    (``file_only``: the file alone, a fault.)"""
    same_info = file_only or held.get("info") == art.get("info")
    return held["sha256"] == art["sha256"] and same_info


def _sorted_keys(value: object) -> object:
    """A JSON value with its keys sorted at every level, as a store reordering keys keeps it (a
    fault)."""
    if isinstance(value, dict):
        return {k: _sorted_keys(value[k]) for k in sorted(value)}
    if isinstance(value, list):
        return [_sorted_keys(v) for v in value]
    return value


def push_artifact(call: Call) -> Answer:
    state, art = call.state, call.json()
    project_id = call.params["project_id"]
    importing = call.importing()
    broken = state.broken
    validate_artifact(
        art,
        blank_serial_ok=broken("5.10.serial-by-kind-400"),
        info_optional=broken("5.10.report-info-required"),
    )
    key = _slot_key(art["kind"], artifact_board(art), art["name"])
    if art["serial"] != "" and not importing:  # a bench's write on a board (§3.3)
        offer.note(offer.BENCH_RUN)
    project = state.projects.get(project_id)
    slot = project.artifacts.get(key) if project else None
    keep_null = ("replaces",) if art["kind"] == "draft" else ()
    stored_obj = drop_nulls(
        {k: copy.deepcopy(v) for k, v in art.items() if k not in ARTIFACT_ANSWER_FIELDS},
        keep=keep_null,
    )
    if broken("5.10.info-key-order"):
        for record in ("info", "content"):
            if record in stored_obj:
                stored_obj[record] = _sorted_keys(stored_obj[record])
    if art["kind"] == "draft":
        return _push_draft(call, project, slot, key, stored_obj)

    generation = art.get("generation") if importing else None
    if state.broken("import.E10-generation"):
        generation = None
    if generation is not None and (not is_int(generation) or generation < 1):
        raise bad_request("generation must be an integer of at least 1.")
    items = slot.items if slot else []
    if generation is not None:
        same = next((item for item in items if item.obj["generation"] == generation), None)
        if same is not None and not _same(same.obj, art, broken("E10.info-only-new-generation")):
            assert project is not None and slot is not None
            current = artifact_answer(state, project, slot, same, full=True)
            if broken("import.E10-changed-current"):
                current.pop("info", None)  # the fault: the generation held without its record
            raise conflict(
                "changed",
                f"Generation {generation} of {slot.name} holds another file or record.",
                current=current,
            )
    else:
        file_only = broken("E10.info-only-new-generation")
        same = items[-1] if items and _same(items[-1].obj, art, file_only) else None
    if slot is not None and slot.deleted is not None and same is None:
        raise conflict(
            "deleted", f"A manager deleted {slot.name}; it cannot be sent again under that name."
        )
    if same is None:  # a replay is never refused for its board
        _check_board(call, art, key)
    refs = [(art["sha256"], art["size"], art["store_id"])]
    # §5.10: a server keeping a report's record alone never refuses it for its page. (The
    # fault: kept without its page, yet refused for it.)
    refused = broken("5.10.report-page-never-refused") and art["kind"] == "report"
    page = art["kind"] != "report" or (state.report_pages and not refused)
    if page or refused:
        to_write = check_refs(state, project_id, refs, call.parts, "artifact")
        if page:
            store_parts(state, project_id, to_write, call.parts)  # a part sent again heals one
    if same is not None:  # a replay, also of a push made before a delete: it stays deleted
        assert project is not None and slot is not None
        answer = artifact_answer(state, project, slot, same, full=True)
        return Answer({"status": "already", "artifact": answer})

    if page:
        referenced_again(state, project_id, refs)
    else:  # kept as a draft is: its sha256, no file
        stored_obj.pop("size", None)
        stored_obj.pop("store_id", None)
    project = state.ensure_project(project_id, None)
    slot = _ensure_slot(state, project, key, art)
    _register_board(call, art, key)
    if generation is None:
        generation = max((item.obj["generation"] for item in items), default=0) + 1
    stored_obj["generation"] = generation
    item = ArtifactItem(obj=stored_obj)
    slot.items.append(item)
    slot.items.sort(key=lambda i: i.obj["generation"])
    if not page and state.storage_of(art["store_id"]) is not None:
        reclaim(state, project, {(art["store_id"], art["sha256"])})  # the page: no one's
    state.seen_bench(art["machine_id"])
    answer = artifact_answer(state, project, slot, item, full=True)
    return Answer({"status": "saved", "artifact": answer})


def _push_draft(
    call: Call, project: Project | None, slot: ArtifactSlot | None, key: tuple, art: dict
) -> Answer:
    """A draft: replaced only when ``replaces`` is the held draft's ``sha256`` (§6.6)."""
    state = call.state
    held = slot.latest() if slot else None
    if held is not None and held.obj["sha256"] == art["sha256"]:
        assert project is not None and slot is not None
        answer = artifact_answer(state, project, slot, held, full=True)
        return Answer({"status": "already", "artifact": answer})
    if slot is not None and slot.deleted is not None:
        raise conflict(
            "deleted", f"A manager deleted {slot.name}; it cannot be sent again under that name."
        )
    if held is not None and art.get("replaces") != held.obj["sha256"]:
        assert project is not None and slot is not None
        answer = artifact_answer(state, project, slot, held, full=True)
        return Answer({"status": "changed", "artifact": answer})
    _check_board(call, art, key)
    project = state.ensure_project(call.params["project_id"], None)
    slot = _ensure_slot(state, project, key, art)
    _register_board(call, art, key)
    slot.items = [ArtifactItem(obj=art)]
    state.seen_bench(art["machine_id"])
    answer = artifact_answer(state, project, slot, slot.latest(), full=True)
    return Answer({"status": "saved", "artifact": answer})


def _check_board(call: Call, art: dict, key: tuple) -> None:
    """§3.3: a bench's report or draft on a board that is not registered, ``409
    unknown_board``, nothing written (an export names no board; an import is taken)."""
    state = call.state
    checked = not call.importing() or state.broken("3.3.import-any-call")  # the fault: checked
    if art["serial"] != "" and checked:
        project_id = call.params["project_id"]
        let_through = state.broken("E10.unknown-board")
        registration.check(state, project_id, key[1], art["serial"], let_through=let_through)


def _register_board(call: Call, art: dict, key: tuple) -> None:
    """§3.3: an imported report's or draft's board becomes registered."""
    if art["serial"] != "" and call.importing() and not call.state.broken("3.3.import-registers"):
        registration.register(call.state, call.params["project_id"], key[1])


def _ensure_slot(state: State, project: Project, key: tuple, art: dict) -> ArtifactSlot:
    """The name's slot; a report's or a draft's first creates its board (§4.11)."""
    if art["kind"] != "export" and not state.broken("5.11.board-created"):
        two = art["format_version"] >= 2
        state.ensure_board(
            project,
            art["serial"],
            art.get("part_number") if two else None,
            art.get("board_revision") if two else None,
        )
    slot = project.artifacts.get(key)
    if slot is None:
        slot = ArtifactSlot(kind=art["kind"], board_key=key[1], name=nfc(art["name"]))
        project.artifacts[key] = slot
    return slot


# ---- E8 -----------------------------------------------------------------------------------------


def _row_key(slot: ArtifactSlot, item: ArtifactItem) -> list:
    """``[generated, kind, serial, part number, revision, name, generation]``: an artifact's
    place in E8's order (:func:`_order`); ``""`` for no ``generated`` and for "none"."""
    part, revision, serial = slot.board_key
    return [
        item.obj.get("generated") or "",
        slot.kind,
        serial,
        part or "",
        revision or "",
        name_key(slot.name),
        item.obj.get("generation") or 0,
    ]


def _order(a: list, b: list, broken: Callable[[str], bool]) -> int:
    """E8's order: newest first by ``generated`` (``""``, no ``generated``, sorts after every
    time), then kind, serial, part number, revision and name ascending ("none", ``""``,
    first), then the generation newest first. (The faults: ``E8.order``, the times left out:
    the names' order alone; ``E8.order-ties``, the board left out of the ties.)"""
    if not broken("E8.order") and a[0] != b[0]:
        return -1 if a[0] > b[0] else 1
    ties = (lambda k: [k[1], k[5]]) if broken("E8.order-ties") else (lambda k: k[1:6])
    if ties(a) != ties(b):
        return -1 if ties(a) < ties(b) else 1
    return (a[6] < b[6]) - (a[6] > b[6])


def _cursor_in(text: str | None) -> list | None:
    if text is None:
        return None
    try:
        value = json.loads(base64.urlsafe_b64decode(text.encode("ascii")))
    except (ValueError, UnicodeError) as exc:
        raise bad_request("This cursor is not one this server gave.") from exc
    if not (
        isinstance(value, list)
        and len(value) == 7
        and all(isinstance(v, str) for v in value[:6])
        and is_int(value[6])
    ):
        raise bad_request("This cursor is not one this server gave.")
    return value


def _cursor_out(key: list) -> str:
    return base64.urlsafe_b64encode(json.dumps(key).encode("utf-8")).decode("ascii")


def _limit(text: str | None, ceiling: int = MAX_LIMIT) -> int:
    if text is None:
        return DEFAULT_LIMIT
    if not text.isdigit() or int(text) < 1:
        raise bad_request("limit is a positive integer.")
    return min(int(text), ceiling)


def _within(generated: str | None, start: str | None, end: str | None, off: bool) -> bool:
    """``generated`` within ``[start, end)``; with no bound, any (no ``generated`` too); with
    a bound, an artifact with no ``generated`` never. (``off``, the fault of
    ``E8.from-to-bounds``: ``(start, end]``.)"""
    if start is None and end is None:
        return True
    if generated is None:
        return False
    if start is not None and (generated <= start if off else generated < start):
        return False
    return end is None or (generated <= end if off else generated < end)


def list_artifacts(call: Call) -> Answer:
    query = call.query
    for name in sorted(query.names() - LIST_FILTERS):
        raise unsupported_filter(name)
    kind = query.one("kind")
    if kind is not None and kind not in ARTIFACT_KINDS:
        raise bad_request(f"kind is one of {', '.join(ARTIFACT_KINDS)}.")
    serial = query.one("serial")
    part = query.one("part_number")
    revision = query.one("board_revision")
    work_order = query.one("work_order")
    test = query.one("test")
    deleted = query.one("deleted") or "exclude"
    if deleted not in ("exclude", "include", "only"):
        raise bad_request("deleted is exclude, include or only.")
    generations = query.one("generations") or "latest"
    if generations not in ("latest", "all"):
        raise bad_request("generations is latest or all.")
    include = query.one("include")
    if include not in (None, "info"):
        raise bad_request("include is info, or absent.")
    verdict = query.one("verdict")
    verdicts = None
    if verdict is not None:
        verdicts = set(verdict.split(","))
        if not verdicts <= set(VERDICTS):
            raise bad_request(f"verdict is one or more of {', '.join(VERDICTS)}, comma-separated.")
    start, end = query.one("from"), query.one("to")
    for name, value in (("from", start), ("to", end)):
        if value is not None and not is_time(value):
            raise bad_request(f"{name} is a UTC time, YYYY-MM-DDTHH:MM:SSZ.")
    broken = call.state.broken
    if verdicts is not None and broken("E8.verdict"):  # the fault: only the first one
        verdicts = {verdict.split(",")[0]}
    if broken("E8.from-to"):
        start = end = None
    limit = _limit(query.one("limit"), 400 if broken("E8.limit-cursor") else MAX_LIMIT)
    cursor = _cursor_in(query.one("cursor"))
    project = call.state.projects.get(call.params["project_id"])
    rows = []
    for slot in project.artifacts.values() if project else []:
        board = slot.board_key
        if kind is not None and slot.kind != kind:
            continue
        if serial is not None and board[2] != name_key(serial):
            continue
        if part is not None and board[0] != (name_key(part) if part else None):
            continue
        if revision is not None and board[1] != (name_key(revision) if revision else None):
            continue
        if deleted == "exclude" and slot.deleted is not None:
            continue
        if deleted == "only" and slot.deleted is None:
            continue
        every = generations == "all" and not broken("E8.generations")  # the fault: latest only
        for item in slot.items if every else [slot.latest()]:
            held = item.obj.get("work_order") or ""
            if broken("E8.filters") and work_order is not None and held != work_order:
                continue
            if work_order is not None and name_key(held) != name_key(work_order):
                continue
            if test is not None and name_key(test) not in {
                name_key(t) for t in item.obj.get("tests") or []
            }:
                continue
            if verdicts is not None and item.obj.get("verdict") not in verdicts:
                continue
            if not _within(item.obj.get("generated"), start, end, broken("E8.from-to-bounds")):
                continue
            rows.append((_row_key(slot, item), slot, item))
    order = cmp_to_key(lambda a, b: _order(a, b, broken))
    rows.sort(key=lambda row: order(row[0]))
    if cursor is not None and not broken("3.1.opaque-continuation"):
        rows = [row for row in rows if _order(row[0], cursor, broken) > 0]
    page = rows[:limit]
    artifacts = []
    for _key, slot, item in page:
        assert project is not None
        obj = artifact_answer(call.state, project, slot, item, full=False)
        if include == "info" and "info" in item.obj:
            obj["info"] = copy.deepcopy(item.obj["info"])
        artifacts.append(obj)
    next_cursor = _cursor_out(page[-1][0]) if len(rows) > limit else None
    return Answer({"artifacts": artifacts, "cursor": next_cursor})


# ---- E9 -----------------------------------------------------------------------------------------


def get_artifact(call: Call) -> Answer:
    kind, name = call.params["kind"], call.params["name"]
    if kind not in ARTIFACT_KINDS:
        raise bad_request(f"kind is one of {', '.join(ARTIFACT_KINDS)}.")
    part_number, revision = call.board_address()
    serial = call.query.one("serial")
    if serial is None:
        raise bad_request(
            "E9 needs serial, as it needs part_number: empty for a project-level export.",
            parameter="serial",
        )
    generation = call.query.one("generation")
    if generation is not None and (not generation.isdigit() or int(generation) < 1):
        raise bad_request("generation is a positive integer.")
    have = call.have()
    if serial == "":
        board = (None, None, "") if part_number is None and revision is None else None
    else:
        board = board_key(part_number, revision, check_board_name(serial, "serial"))
    project = call.state.projects.get(call.params["project_id"])
    slot = project.artifacts.get((kind, board, name_key(name))) if project and board else None
    if slot is None:
        raise not_found(f"No {kind} named {name} for this board.")
    item = slot.latest()
    if generation is not None and not (
        slot.kind == "draft" and call.state.broken("E9.draft-generation-404")
    ):
        found = [i for i in slot.items if i.obj.get("generation") == int(generation)]
        if not found:  # a draft has no generation
            raise not_found(f"{slot.name} has no generation {generation}.")
        item = found[0]
    obj = artifact_answer(call.state, project, slot, item, full=True)
    parts = []
    sha = item.obj.get("sha256")
    if item.obj.get("store_id") == API_STORE_ID and sha not in have and "missing" not in obj:
        data, _status = call.state.blobs.read(project.project_id, sha, item.obj["size"])
        if data is not None:
            parts.append(Part(sha, slot.name, part_type(slot.name), data))
    return Answer(obj, parts)


# ---- E12 ----------------------------------------------------------------------------------------


def named_slot_key(body: dict) -> tuple:
    """The slot an E12 body or an E13 item names: kind + board + name."""
    kind = body.get("kind")
    if kind not in ARTIFACT_KINDS:
        raise bad_request(f"kind is one of {', '.join(ARTIFACT_KINDS)}.")
    serial, name = body.get("serial"), body.get("name")
    if not isinstance(serial, str) or not isinstance(name, str) or not name:
        raise bad_request("The body names the artifact: kind, serial, name (and its board).")
    parts = []
    for key in ("part_number", "board_revision"):
        value = body.get(key)
        if value is not None and not isinstance(value, str):
            raise bad_request(f"{key} must be a string.")
        parts.append(value or None)
    board = (None, None, "") if serial == "" else board_key(parts[0], parts[1], serial)
    return (kind, board, name_key(name))


def _find_slot(call: Call) -> tuple[Project, ArtifactSlot]:
    key = named_slot_key(call.json())
    project = call.state.projects.get(call.params["project_id"])
    slot = project.artifacts.get(key) if project else None
    if slot is None:
        raise not_found(f"No {key[0]} named {call.json()['name']} for this board.")
    return project, slot


def delete_artifact(call: Call) -> Answer:
    """E12: covers every generation of the name; a replay leaves it as it is."""
    importing = manager_or_import(call, allow_import=True)
    fields = deletion(call, importing)
    project, slot = _find_slot(call)
    replaced = call.state.broken("E12.replays") or (
        importing and call.state.broken("import.delete-replay")
    )
    if slot.deleted is None or replaced:
        slot.deleted = fields
    call.state.seen_bench(fields["deleted_bench"])
    return Answer(artifact_answer(call.state, project, slot, slot.latest(), full=True))


def restore_artifact(call: Call) -> Answer:
    manager_or_import(call, allow_import=False)
    machine_id = req_pattern(
        call.json(), "machine_id", "request", MACHINE_ID, "8 lower-case hex digits"
    )
    project, slot = _find_slot(call)
    slot.deleted = None
    call.state.seen_bench(machine_id)
    return Answer(artifact_answer(call.state, project, slot, slot.latest(), full=True))
