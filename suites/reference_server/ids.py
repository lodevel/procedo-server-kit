"""A test's id: read from its ``test.json`` alone, and the two rules every project keeps.

Every test has a hidden ``test_id``, a lower-case UUID, kept in ``tests/<test>/test.json``
beside ``"active"`` (data_format.md §4.3). The server reads it there and nowhere else, as it
reads the test's ``format`` from the same file. Two rules hold of every project it keeps:

- a test folder holding any file holds a ``test.json`` that is a JSON object (a byte-order
  mark before it allowed) whose ``test_id`` is a lower-case UUID;
- no two tests of one project hold the same ``test_id``.

P9 checks them after each change, in the order sent, and refuses alone a change that breaks
one; P11 checks the project whole.
"""

from __future__ import annotations

import json
from collections.abc import Iterable

from .state import Project, StoredFile
from .validate import TEST_ID

MARKER = "test.json"


def tests_in(files: dict[str, StoredFile]) -> dict[str, str]:
    """Every test of these files: its key and its folder's spelling, in name order (folded).
    A test is a folder ``tests/<test>/`` holding at least one file."""
    found: dict[str, str] = {}
    for key in sorted(files):
        segments = key.split("/")
        if len(segments) >= 3 and segments[0] == "tests":
            found.setdefault(segments[1], files[key].path.split("/")[1])
    return found


def tests_of(project: Project) -> dict[str, str]:
    return tests_in(project.files)


def test_of_key(key: str) -> str | None:
    """The test a file's key lies under (``tests/<test>/…``), else None."""
    segments = key.split("/")
    return segments[1] if len(segments) >= 3 and segments[0] == "tests" else None


def marker_in(files: dict[str, StoredFile], test_key: str) -> dict | None:
    """The test's ``test.json`` as a JSON object; None when it is absent, not text, not JSON
    or not an object."""
    stored = files.get(f"tests/{test_key}/{MARKER}")
    text = stored.obj.get("text") if stored is not None else None
    if not isinstance(text, str):
        return None
    try:
        data = json.loads(text.lstrip("﻿"))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def test_id_in(files: dict[str, StoredFile], test_key: str) -> str | None:
    """The ``test_id`` the test's ``test.json`` holds, when it is a lower-case UUID."""
    marker = marker_in(files, test_key)
    test_id = marker.get("test_id") if marker is not None else None
    return test_id if isinstance(test_id, str) and TEST_ID.fullmatch(test_id) else None


def test_id_of(project: Project, test_key: str) -> str | None:
    return test_id_in(project.files, test_key)


def test_holding(project: Project, test_id: str) -> str | None:
    """The key of the test whose ``test.json`` holds this id, else None."""
    for key in tests_of(project):
        if test_id_of(project, key) == test_id:
            return key
    return None


def id_problem(files: dict[str, StoredFile], touched: Iterable[str] | None = None) -> str | None:
    """Why a test among ``touched`` (every test when None) breaks one of the two rules, as one
    sentence naming it; None when both hold."""
    tests = tests_in(files)
    ids = {key: test_id_in(files, key) for key in tests}
    chosen = tests if touched is None else [key for key in tests if key in set(touched)]
    for key in chosen:
        test_id = ids[key]
        if test_id is None:
            return (
                f"tests/{tests[key]}/ has no test.json holding its test_id, a lower-case "
                "UUID: a test's test.json comes before its other files and goes after them "
                "(data_format.md §4.3)."
            )
        for other, other_id in ids.items():
            if other != key and other_id == test_id:
                return (
                    f"tests/{tests[key]}/ and tests/{tests[other]}/ hold one test_id, "
                    f"{test_id}: each test has its own (data_format.md §4.3)."
                )
    return None
