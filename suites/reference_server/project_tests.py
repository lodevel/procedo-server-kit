"""Tests: the list (P4), several with their documents (P5, P6) and one (P7), with ``/read`` twins.

A test is a folder ``tests/<test>/`` holding at least one file (``ids.tests_of``). Its
documents are every file under its folder plus every file of ``functions/``: the app's release
keeps the ``functions/`` files its code imports, which a server that reads no code cannot tell,
so a test read alone gets them all and still runs as in the whole project. Its ``test_id`` and
its ``format`` are read from its ``test.json`` (``ids``): the format is the app's rule
(``test_format.read_format``), ``"active"``, and a ``test.json`` that names neither is
``code``. Its ``latest_release`` is the highest release under its ``test_id``. Its ``draft`` is
``drafts.save_drafts``'s.
"""

from __future__ import annotations

import copy

from .call import Answer, Call
from .errors import bad_request, not_found
from .ids import marker_in, test_id_of, tests_of
from .multipart import Part
from .project_files import file_map, file_parts, missing_of
from .projects import held_project, token_of
from .state import Project, State, StoredFile
from .validate import name_key

TEST_FORMAT = 1
TEST_FORMATS = ("text", "code")


def documents_of(project: Project, test_key: str) -> list[StoredFile]:
    prefixes = (f"tests/{test_key}/", "functions/")
    return [stored for key, stored in project.files.items() if key.startswith(prefixes)]


def latest_release(project: Project, test_id: str | None) -> int | None:
    versions = [release["version"] for release in project.releases.get(test_id or "", [])]
    return max(versions) if versions else None


def test_format(project: Project, test_key: str) -> str:
    """The app's rule (``test_format.read_format``): ``test.json``'s ``active`` decides; one
    that names neither is ``code``, so its text version never runs by mistake."""
    marker = marker_in(project.files, test_key)
    active = marker.get("active") if marker is not None else None
    return active if active in TEST_FORMATS else "code"


def test_answer(project: Project, test_key: str, spelling: str, *, documents: bool) -> dict:
    test_id = test_id_of(project, test_key)
    obj: dict = {
        "format_version": TEST_FORMAT,
        "test": spelling,
        "test_id": test_id,  # every test holds one: the rules ``ids`` keeps
        "format": test_format(project, test_key),
    }
    latest = latest_release(project, test_id)
    if latest is not None:
        obj["latest_release"] = latest
    if test_key in project.test_updated:
        obj["updated"] = project.test_updated[test_key]
    if test_key in project.drafts:
        obj["draft"] = copy.deepcopy(project.drafts[test_key])
    if documents:
        obj["files"] = file_map(documents_of(project, test_key))
    return obj


def _with_documents(
    state: State, project: Project, test_key: str, spelling: str, have: set[str], sent: set[str]
) -> tuple[dict, list[Part]]:
    obj = test_answer(project, test_key, spelling, documents=True)
    items = list(obj["files"].items())
    missing = missing_of(state, project.project_id, items)
    if missing:
        obj["missing"] = missing
    return obj, file_parts(state, project.project_id, items, have, sent)


# ---- P4-P7 ----------------------------------------------------------------------------------------


def _by_name(call: Call, project: Project, names: list[str]) -> Answer:
    """P5, P6: ``{token, tests: {name asked: Test}, missing}`` + parts, one per content."""
    tests = tests_of(project)
    have, sent = call.have(), set()
    found: dict = {}
    missing: list[str] = []
    parts: list[Part] = []
    for name in dict.fromkeys(names):
        key = name_key(name)
        if key not in tests:
            missing.append(name)
            continue
        found[name], more = _with_documents(call.state, project, key, tests[key], have, sent)
        parts += more
    if call.state.broken("P5.missing"):
        missing = []
    obj = {"token": token_of(call.state, project), "tests": found, "missing": missing}
    return Answer(obj, parts)


def list_tests(call: Call) -> Answer:
    """P4 (no ``names``): every test without its documents. P5: those named, with them."""
    project = held_project(call)
    names = call.query.one("names")
    if names is None:
        tests = tests_of(project)
        listed = [test_answer(project, key, tests[key], documents=False) for key in tests]
        return Answer({"token": token_of(call.state, project), "tests": listed})
    asked = [] if names == "" else names.split(",")
    if any(name == "" for name in asked):
        raise bad_request("names is test ids separated by commas, none empty.")
    return _by_name(call, project, asked)


def read_tests(call: Call) -> Answer:
    """P6: P5 with the names and ``have`` in the body."""
    project = held_project(call)
    names = call.json().get("names")
    if not isinstance(names, list) or not all(isinstance(n, str) and n for n in names):
        raise bad_request("The body needs names, a list of test ids.")
    return _by_name(call, project, names)


def get_test(call: Call) -> Answer:
    """P7 and its ``/read`` twin: one test with its documents + parts."""
    project = held_project(call)
    key = name_key(call.params["test"])
    tests = tests_of(project)
    if key not in tests:
        raise not_found(f"No test {call.params['test']} in this project.")
    obj, parts = _with_documents(call.state, project, key, tests[key], call.have(), set())
    return Answer(obj, parts)
