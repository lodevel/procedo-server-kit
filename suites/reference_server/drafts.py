"""A test's draft, as P9 and P11 send them (data_format.md §4.3).

A test's draft is what the client that last changed its files sent (the server never computes
a fingerprint), with the test's ``test_id``. It is kept when the test exists after the request,
its ``test_id`` is the one that test's ``test.json`` then holds, and every change of the
request under its folder or under ``functions/`` was saved. A test whose files a
request changed without a draft kept for it loses the draft it held, so a draft never
describes files it was not made from: the test then has none until a client sends one. A
change under ``functions/`` drops no draft (the server cannot tell which tests it reaches; the
app sends the drafts of those it reaches). A draft has no result of its own and moves no
change token: P3 does not report drafts.
"""

from __future__ import annotations

import copy

from .errors import bad_request
from .ids import test_id_of, tests_of
from .state import Project
from .validate import (
    MACHINE_ID,
    SHA_HEX,
    TEST_ID,
    drop_nulls,
    name_key,
    opt_int,
    opt_pattern,
    opt_str,
    req_pattern,
    req_str,
)


def check_drafts(drafts: object) -> list[dict]:
    """``drafts`` (DraftWrite): the test, its id, its fingerprints and what it is based on."""
    if drafts is None:
        return []
    if not isinstance(drafts, list):
        raise bad_request("drafts is a list of drafts.")
    checked = []
    for draft in drafts:
        if not isinstance(draft, dict):
            raise bad_request("Each draft is a JSON object.")
        where = "draft"
        req_str(draft, "test", where, empty=False)
        req_pattern(draft, "test_id", where, TEST_ID, "a lower-case UUID")
        for key in ("fingerprint", "content"):
            req_pattern(draft, key, where, SHA_HEX, "64 lower-case hex digits")
        opt_int(draft, "based_on", where, minimum=1)
        opt_str(draft, "updated_by", where)
        opt_pattern(draft, "machine_id", where, MACHINE_ID, "8 lower-case hex digits")
        checked.append(drop_nulls(copy.deepcopy(draft)))
    return checked


def save_drafts(
    project: Project,
    drafts: list[dict],
    *,
    changed: set[str],
    unsaved: set[str],
    functions_unsaved: bool,
    now: str,
) -> None:
    """Keep each draft of a test that exists and holds the draft's ``test_id``, unless a change
    of the request to its folder (``unsaved``: those tests' keys) or to ``functions/`` was not
    saved; then drop the draft held by every test whose files changed (``changed``) and that
    kept no new one."""
    tests = tests_of(project)
    kept: set[str] = set()
    for draft in drafts:
        key = name_key(draft["test"])
        if key not in tests or key in unsaved or functions_unsaved:
            continue
        if test_id_of(project, key) != draft["test_id"]:
            continue
        stored = {
            name: value for name, value in draft.items() if name not in ("test", "updated_at")
        }
        stored["updated_at"] = now
        project.drafts[key] = stored
        kept.add(key)
    for key in changed - kept:
        project.drafts.pop(key, None)
