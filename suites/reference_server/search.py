"""E2: search runs, across projects when ``project_id`` is absent (data_format.md §6.2); E16:
the operators a project's runs recorded; E17: the readings the runs of one test recorded.

Every filter the contract names is served; any other parameter is refused with ``400
unsupported_filter``, never ignored. Every text filter compares by case folding, except
``field``, a field's name, compared exactly. ``test_id`` finds a test's runs, whatever names
it had; ``test`` matches the name a run recorded, following no rename. Answer: RunSummary
rows, newest first by ``run_id`` (descending), then ``project_id`` (ascending), paged with an
opaque cursor.
"""

from __future__ import annotations

import base64
import json
import math
from functools import cmp_to_key

from .call import Answer, Call
from .entries import board_values
from .errors import bad_request, unsupported_filter
from .runs import run_answer
from .state import Project, StoredRun
from .validate import PROJECT_ID, SHA_HEX, TEST_ID, VERDICTS, is_time, name_key

FILTERS = {
    "project_id",
    "serial",
    "test_id",
    "test",
    "operator",
    "machine_id",
    "firmware_version",
    "part_number",
    "board_revision",
    "lot",
    "field",
    "field_value",
    "verdict",
    "test_version",
    "definition_sha256",
    "draft",
    "from",
    "to",
    "measurement",
    "value_min",
    "value_max",
    "deleted",
    "limit",
    "cursor",
}
DEFAULT_LIMIT = 100
MAX_LIMIT = 500
NONE = object()  # an empty part_number or board_revision: the "no part number" board


def _number(text: str | None, name: str) -> float | None:
    if text is None:
        return None
    try:
        value = float(text)
    except ValueError:
        value = math.nan
    if not math.isfinite(value):
        raise bad_request(f"{name} is a finite number.")
    return value


def _order(a: list, b: list, projects_down: bool = False) -> int:
    """``[run_id, project_id]``: ``run_id`` descending, then ``project_id`` ascending (§6.2);
    descending with ``projects_down``, a fault."""
    if a[0] != b[0]:
        return -1 if a[0] > b[0] else 1
    step = (a[1] > b[1]) - (a[1] < b[1])
    return -step if projects_down else step


def _folded(value: str | None) -> str | None:
    return None if value is None else name_key(value)


class Filters:
    """The parsed query of one search."""

    def __init__(self, call: Call) -> None:
        query = call.query
        self.broken = call.state.broken
        for name in sorted(query.names() - FILTERS):
            raise unsupported_filter(name)
        one = query.one
        self.project_id = one("project_id")
        if self.project_id is not None and PROJECT_ID.fullmatch(self.project_id) is None:
            raise bad_request("project_id is a lower-case UUID.")
        self.test_id = one("test_id")
        if self.test_id is not None and TEST_ID.fullmatch(self.test_id) is None:
            raise bad_request("test_id is a lower-case UUID.")
        self.names = {
            key: name_key(value)
            for key in ("serial", "test", "operator", "lot", "machine_id", "firmware_version")
            if (value := one(key)) is not None
        }
        self.board = {}
        for index, key in enumerate(("part_number", "board_revision")):
            value = one(key)
            if value is not None:
                self.board[index] = name_key(value) if value else None
        self.field, field_value = one("field"), one("field_value")
        self.field_value = _folded(field_value)
        if (self.field is None) != (self.field_value is None):
            raise bad_request("field and field_value go together.")
        verdict = one("verdict")
        self.verdicts = None
        if verdict is not None:
            self.verdicts = set(verdict.split(","))
            if not self.verdicts <= set(VERDICTS):
                raise bad_request(
                    f"verdict is one or more of {', '.join(VERDICTS)}, comma-separated."
                )
        test_version = one("test_version")
        self.test_version = None
        if test_version is not None:
            if not test_version.isdigit() or int(test_version) < 1:
                raise bad_request("test_version is a positive integer.")
            self.test_version = int(test_version)
        self.definition = one("definition_sha256")
        if self.definition is not None and SHA_HEX.fullmatch(self.definition) is None:
            raise bad_request("definition_sha256 is 64 lower-case hex digits.")
        draft = one("draft")
        if draft not in (None, "true", "false"):
            raise bad_request("draft is true or false.")
        self.draft = None if draft is None else draft == "true"
        self.start, self.end = one("from"), one("to")
        for name, value in (("from", self.start), ("to", self.end)):
            if value is not None and not is_time(value):
                raise bad_request(f"{name} is a UTC time, YYYY-MM-DDTHH:MM:SSZ.")
        self.measurement = _folded(one("measurement"))
        self.value_min = _number(one("value_min"), "value_min")
        self.value_max = _number(one("value_max"), "value_max")
        bounded = self.value_min is not None or self.value_max is not None
        if (
            self.measurement is None
            and bounded
            and not self.broken("E2.bound-without-measurement-400")
        ):
            raise bad_request("value_min and value_max bound a measurement: name it.")
        self.deleted = one("deleted") or "exclude"
        if self.deleted not in ("exclude", "include", "only"):
            raise bad_request("deleted is exclude, include or only.")
        limit = one("limit")
        if limit is not None and (not limit.isdigit() or int(limit) < 1):
            raise bad_request("limit is a positive integer.")
        ceiling = 400 if self.broken("E2.limit-cursor") else MAX_LIMIT
        self.limit = min(int(limit), ceiling) if limit else DEFAULT_LIMIT
        self.cursor = _cursor_in(one("cursor"))

    def match(self, project: Project, run: StoredRun) -> bool:
        obj = run.obj
        if self.deleted == "exclude" and run.deleted is not None:
            return False
        if self.deleted == "only" and run.deleted is None:
            return False
        names = self.names
        if "serial" in names and run.board_key[2] != names["serial"]:
            return False
        if self.test_id is not None and run.test_id != self.test_id:
            return False
        if "test" in names and name_key(obj["test"]) != names["test"]:  # as recorded
            return False
        for key in ("operator", "machine_id", "firmware_version"):
            if key in names and _folded(obj.get(key)) != names[key]:
                return False
        for index, wanted in self.board.items():
            if run.board_key[index] != wanted:
                return False
        board = project.boards[run.board_key]
        lot, details = board_values(board.entries)
        if "lot" in names:
            on_board = lot is not None and name_key(lot) == names["lot"]
            recorded = obj["format_version"] < 2 and name_key(obj.get("lot") or "") == names["lot"]
            recorded = recorded and not self.broken("E2.filters")
            if not (on_board or recorded):
                return False
        if self.field is not None:  # the name exactly, the value (a string) by case folding
            values = list((obj.get("extra") or {}).items()) + list(details.items())
            if not any(k == self.field and name_key(v) == self.field_value for k, v in values):
                return False
        if self.verdicts is not None and obj.get("verdict") not in self.verdicts:
            return False
        if self.test_version is not None and obj.get("test_version") != self.test_version:
            return False
        if self.definition is not None and obj.get("definition_sha256") != self.definition:
            return False
        if self.draft is not None and obj["draft"] is not self.draft:
            return False
        if self.start is not None or self.end is not None:
            ended = obj.get("ended")
            if ended is None:
                return False
            if self.start is not None and ended < self.start:
                return False
            if self.end is not None and ended >= self.end:
                return False
        if self.measurement is not None and not self._measured(obj):
            return False
        return True

    def _measured(self, obj: dict) -> bool:
        bounded = self.value_min is not None or self.value_max is not None
        for phase in obj.get("phases") or []:
            for measurement in phase.get("measurements") or []:
                if name_key(measurement.get("name", "")) != self.measurement:
                    continue
                if not bounded:
                    return True
                value = measurement.get("value_num")
                if value is None:
                    continue
                if self.value_min is not None and value < self.value_min:
                    continue
                if self.value_max is not None and value > self.value_max:
                    continue
                return True
        return False


def _cursor_in(text: str | None) -> list | None:
    if text is None:
        return None
    try:
        value = json.loads(base64.urlsafe_b64decode(text.encode("ascii")))
    except (ValueError, UnicodeError) as exc:
        raise bad_request("This cursor is not one this server gave.") from exc
    if not (isinstance(value, list) and len(value) == 2 and all(isinstance(v, str) for v in value)):
        raise bad_request("This cursor is not one this server gave.")
    return value


def _cursor_out(key: list) -> str:
    return base64.urlsafe_b64encode(json.dumps(key).encode("utf-8")).decode("ascii")


def search_runs(call: Call) -> Answer:
    filters = Filters(call)
    state = call.state
    if filters.project_id is not None:
        project = state.projects.get(filters.project_id)
        projects = [project] if project else []
    elif state.broken("E2.across-projects"):
        projects = []
    else:
        projects = list(state.projects.values())
    rows = [
        ([run.run_id, project.project_id], project, run)
        for project in projects
        for run in project.runs.values()
        if filters.match(project, run)
    ]
    down = state.broken("E2.order")
    rows.sort(key=cmp_to_key(lambda a, b: _order(a[0], b[0], down)))
    if filters.cursor is not None and not state.broken("3.1.opaque-continuation"):
        rows = [row for row in rows if _order(row[0], filters.cursor, down) > 0]
    page = rows[: filters.limit]
    cursor = _cursor_out(page[-1][0]) if len(rows) > filters.limit else None
    return Answer(
        {
            "runs": [run_answer(state, project, run, summary=True) for _key, project, run in page],
            "cursor": cursor,
        }
    )


def list_operators(call: Call) -> Answer:
    """E16: every operator the project's runs recorded, deleted runs included, once per case
    folding in the spelling first received, ascending by case folding. (The faults:
    ``E16.operators``, each spelling listed; ``E16.deleted-included``, deleted runs left out;
    ``E16.order``, code point order.)"""
    state = call.state
    project = state.projects.get(call.params["project_id"])
    first: dict[str, str] = {}  # case folded -> the spelling first received
    for run in project.runs.values() if project else []:
        operator = run.obj.get("operator")
        if not isinstance(operator, str) or not operator:
            continue
        if run.deleted is not None and state.broken("E16.deleted-included"):
            continue
        key = operator if state.broken("E16.operators") else name_key(operator)
        first.setdefault(key, operator)
    order = (lambda k: first[k]) if state.broken("E16.order") else (lambda k: (name_key(k), k))
    return Answer({"operators": [first[key] for key in sorted(first, key=order)]})


def list_readings(call: Call) -> Answer:
    """E17: every measurement name the runs that recorded the test name ``test`` (by case
    folding, as E2's ``test``) recorded, in any phase and attempt, deleted runs included, once
    per case folding in the spelling first received, each with the first ``units`` received
    under it, ascending by case folding. ``test`` missing or empty: ``400 bad_request``. (The
    faults: ``E17.readings``, each spelling listed; ``E17.units``, no units;
    ``E17.deleted-included``, deleted runs left out; ``E17.order``, code point order;
    ``E17.test-required``, every test's readings for a missing or empty ``test``.)"""
    state = call.state
    test = call.query.one("test")
    every_test = not test and state.broken("E17.test-required")
    if not test and not every_test:
        raise bad_request("E17 lists the readings of one test: name it (test=<name>).",
                          parameter="test")  # fmt: skip
    want = name_key(test or "")
    project = state.projects.get(call.params["project_id"])
    first: dict[str, dict] = {}  # case folded -> {"name": first spelling, "units": first one}
    for run in project.runs.values() if project else []:
        if not every_test and name_key(str(run.obj.get("test") or "")) != want:
            continue
        if run.deleted is not None and state.broken("E17.deleted-included"):
            continue
        for phase in run.obj.get("phases") or []:
            for measurement in phase.get("measurements") or []:
                name = measurement.get("name")
                if not isinstance(name, str) or not name:
                    continue
                key = name if state.broken("E17.readings") else name_key(name)
                held = first.setdefault(key, {"name": name})
                units = measurement.get("units")
                if "units" not in held and isinstance(units, str) and units:
                    if not state.broken("E17.units"):
                        held["units"] = units
    order = (lambda k: first[k]["name"]) if state.broken("E17.order") else (
        lambda k: (name_key(first[k]["name"]), first[k]["name"]))  # fmt: skip
    return Answer({"readings": [first[key] for key in sorted(first, key=order)]})
