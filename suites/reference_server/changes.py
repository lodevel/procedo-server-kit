"""P9: writes, deletes, moves, drafts and a new bundle in one request (web_api_projects.md P9).

Each change is checked against what the server holds when its turn comes, in order, and gives
one result: ``saved`` (with the new ``version``), ``changed`` (the file is not the version it
``replaces``: nothing written for it, ``current`` is what the server holds) or ``refused``
(with its ``kind`` and sentence). One conflict never blocks the rest. A change that asks for
what the server already holds (a replay) is ``saved`` and writes nothing, whatever its
``replaces`` says. A change after which a test folder holding a file would hold no
``test.json`` with a valid ``test_id``, or two tests would hold one ``test_id``, is
``refused`` (kind ``invalid``), nothing written for it (``ids``, data_format.md §4.3): so a new
test's ``test.json`` comes before its other files and goes after them. Checked before anything
is written, for the whole request: its form (``400``) and the bytes of every reference
(``409 missing_files``).

A move names every file it moves, by its path in the folder, with its version: the folder must
hold exactly those, and its destination nothing, else ``changed`` (no ``current``: a folder is
no File) and nothing moved; a move into itself, or one that would change a file's form or make
it a file that never travels, is ``refused``. A test folder's move carries its ``test.json``,
so the test keeps its ``test_id``, and its releases, runs and current-run marks, all filed
under it, stay where they are: a rename moves no evidence (§6.9). Its draft moves with it. A
test whose last file goes is deleted: its releases, runs and marks stay under its ``test_id``,
and its draft goes. ``drafts`` follow ``drafts.save_drafts``.

A project P11 never created (the server knows it only by its evidence) is ``404``, as P2–P8
and P10 answer it (data_format.md §5.1).
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import dataclass, field

from . import faults
from .call import Answer, Call
from .drafts import check_drafts, save_drafts
from .errors import bad_request
from .ids import id_problem, test_of_key, tests_of
from .project_files import (
    check_form,
    check_path,
    clashes,
    is_text_path,
    never_travels,
    ref_of,
    spell,
    text_file,
    under,
)
from .projects import held_project, log_changes, token_of
from .state import Project, State, StoredFile
from .storages import check_refs, project_refs, reclaim, referenced_again, store_parts
from .validate import SHA_HEX, is_int, name_key, opt_pattern, req_int, req_str, version_hex

CHANGE_FIELDS = ("path", "replaces", "text", "version", "size", "store_id", "delete", "bundle")
BUNDLE = "bundle/"


# ---- The changes, as sent -------------------------------------------------------------------------


def _replaces(change: dict, *, required: bool = False) -> str | None:
    value = change.get("replaces")
    if value is None:
        if required:
            raise bad_request("A delete names the version it replaces.")
        return None
    return "sha256:" + version_hex(value, "replaces")


def _stored_file(change: dict) -> dict:
    """The File a write of a stored file makes, its unknown fields kept."""
    obj = {key: copy.deepcopy(v) for key, v in change.items() if key not in CHANGE_FIELDS}
    obj["version"] = "sha256:" + version_hex(change.get("version"), "A write's version")
    obj["size"] = req_int(change, "size", "write")
    if not is_int(change.get("store_id")):
        raise bad_request("A write of a stored file names its store_id.")
    obj["store_id"] = change["store_id"]
    return {key: value for key, value in obj.items() if value is not None}


def _parse_bundle(change: dict) -> dict:
    bundle = change["bundle"]
    if not isinstance(bundle, dict):
        raise bad_request("A new bundle names bundle: {bundle_id, bundle_version}.")
    req_str(bundle, "bundle_id", "bundle", empty=False)
    req_str(bundle, "bundle_version", "bundle", empty=False)
    opt_pattern(bundle, "bundle_sha256", "bundle", SHA_HEX, "64 lower-case hex digits")
    fields = {key: copy.deepcopy(v) for key, v in bundle.items() if v is not None}
    fields.pop("archive", None)
    return {
        "op": "bundle",
        "path": BUNDLE,
        "replaces": _replaces(change),
        "bundle": fields,
        "file": _stored_file(change),
    }


def _parse_move(change: dict, names: bool) -> dict:
    path = check_path(change.get("path"), "A move's path", folder=True, names=names)
    move_to = check_path(change.get("move_to"), "A move's move_to", folder=True, names=names)
    folder = change.get("replaces_folder")
    if not isinstance(folder, dict):
        raise bad_request("A move names replaces_folder: {file: version}, {} for none.")
    named = {}
    for name, version in folder.items():
        relative = name_key(check_path(name, "A moved file's name"))
        named[relative] = "sha256:" + version_hex(version, "Each version of replaces_folder")
    return {"op": "move", "path": change["path"], "from": path, "to": move_to, "named": named}


def _parse(change: object, state: State) -> dict:
    """One change, checked: its ``op`` and what it needs, the path as sent kept for its result."""
    if not isinstance(change, dict):
        raise bad_request("Each change is a JSON object.")
    names = not (state.broken("2.10.path-names-400") or state.broken("P9.malformed-400"))
    if change.get("bundle") is not None:
        return _parse_bundle(change)
    if "move_to" in change or "replaces_folder" in change:
        return _parse_move(change, names)
    path = check_path(change.get("path"), "A change's path", names=names)
    parsed = {"op": "", "path": change["path"], "at": path, "key": name_key(path)}
    if change.get("delete") is not None:
        if change["delete"] is not True:
            raise bad_request("A delete says delete: true.")
        parsed.update(op="delete", replaces=_replaces(change, required=True))
    elif change.get("text") is not None:
        if not isinstance(change["text"], str):
            raise bad_request("A text write's text is a string.")
        text_ok = is_text_path(parsed["key"]) or state.broken("2.9.wrong-form-400")
        if not text_ok and not never_travels(path):  # else refused alone
            raise bad_request(
                f"{path!r} is not under tests/, functions/, config/ or scenarios/: it is a "
                "reference to a storage, never text (data_format.md §2)."
            )
        extra = {key: v for key, v in change.items() if key not in CHANGE_FIELDS}
        made = text_file(change["text"], f"file {path}", "", extra)
        for key in ("version", "size"):
            stated = change.get(key)
            if (
                stated is not None
                and stated != made[key]
                and not state.broken("2.9.text-size-version-400")
            ):
                raise bad_request(f"The text of {path} is not the {key} the change states.")
        made.pop("mtime")
        parsed.update(op="write", replaces=_replaces(change), file=made)
    elif change.get("store_id") is not None or change.get("version") is not None:
        parsed.update(op="write", replaces=_replaces(change), file=_stored_file(change))
    else:
        raise bad_request("A change writes text or a stored file, deletes, moves or is a bundle.")
    return parsed


# ---- Applying them ------------------------------------------------------------------------------


@dataclass
class _Work:
    """One request's changes, made on a copy of the project, kept only when all is checked."""

    project: Project
    now: str
    broken: Callable[[str], bool] = lambda _rule: False
    events: list[tuple] = field(default_factory=list)
    unsaved_tests: set[str] = field(default_factory=set)
    functions_unsaved: bool = False
    changed_tests: set[str] = field(default_factory=set)  # tests whose files' bytes changed

    def touch(self, key: str, *, content: bool = False) -> None:
        self.project.updated = self.now
        segments = key.split("/")
        if len(segments) > 2 and segments[0] == "tests":
            self.project.test_updated[segments[1]] = self.now
            if content:
                self.changed_tests.add(segments[1])

    def not_saved(self, result: dict, *keys: str) -> dict:
        """A result other than ``saved``: the drafts of the tests it concerns are not kept."""
        for key in keys:
            segments = key.split("/")
            if segments[0] == "functions":
                self.functions_unsaved = True
            elif len(segments) > 1 and segments[0] == "tests":
                self.unsaved_tests.add(segments[1])
        return result


def _saved(path: str, version: str | None = None) -> dict:
    result = {"path": path, "status": "saved"}
    if version is not None:
        result["version"] = version
    return result


def _changed(path: str, current: dict | None) -> dict:
    result: dict = {"path": path, "status": "changed"}
    if current is not None:
        result["current"] = copy.deepcopy(current)
    return result


def _refused(path: str, kind: str, message: str) -> dict:
    return {"path": path, "status": "refused", "kind": kind, "message": message}


def _write(work: _Work, change: dict) -> dict:
    files, key, path = work.project.files, change["key"], change["path"]
    if never_travels(change["at"]):
        message = f"{change['at']} is never a project file (data_format.md §4.2)."
        return work.not_saved(_refused(path, "invalid", message), key)
    current = files.get(key)
    held = current.obj["version"] if current is not None else None
    if held == change["file"]["version"]:
        return _saved(path, held)  # what the server holds already: a replay
    if change["replaces"] != held:
        result = _changed(path, current.obj if current else None)
        if current is None and work.broken("P9.changed"):
            result["current"] = None
        return work.not_saved(result, key)
    if current is None and clashes(files, key):
        message = f"{change['at']} would be a file and a folder at once."
        return work.not_saved(_refused(path, "invalid", message), key)
    spelling = current.path if current is not None else spell(files, change["at"])
    made = StoredFile(path=spelling, obj={**change["file"], "mtime": work.now})
    problem = _id_problem({**files, key: made}, [key])
    if problem is not None:
        return work.not_saved(_refused(path, "invalid", problem), key)
    files[key] = made
    work.events.append(("file", key, spelling))
    work.touch(key, content=True)
    return _saved(path, change["file"]["version"])


def _delete(work: _Work, change: dict) -> dict:
    files, key, path = work.project.files, change["key"], change["path"]
    current = files.get(key)
    if current is None:
        return _saved(path)  # gone already: a replay
    if current.obj["version"] != change["replaces"]:
        return work.not_saved(_changed(path, current.obj), key)
    problem = _id_problem({other: f for other, f in files.items() if other != key}, [key])
    if problem is not None:
        return work.not_saved(_refused(path, "invalid", problem), key)
    del files[key]
    work.events.append(("file", key, current.path))
    work.touch(key, content=True)
    return _saved(path)


def _bundle(work: _Work, change: dict) -> dict:
    project = work.project
    held = project.bundle
    held_version = held["archive"]["version"] if held is not None else None
    archive = change["file"]
    if held is not None and held_version == archive["version"]:
        if all(held.get(key) == value for key, value in change["bundle"].items()):
            return _saved(BUNDLE, held_version)  # a replay
    if change["replaces"] != held_version:
        return _changed(BUNDLE, held["archive"] if held is not None else None)
    project.bundle = {**change["bundle"], "archive": {**archive, "mtime": work.now}}
    project.updated = work.now
    work.events.append(("bundle",))
    return _saved(BUNDLE, archive["version"])


def _id_problem(files: dict[str, StoredFile], keys: list[str]) -> str | None:
    """Why the files a change would leave break a rule of a test's id, for the tests its
    paths (``keys``) lie under; None when they keep both (``ids.id_problem``)."""
    touched = {test for key in keys if (test := test_of_key(key)) is not None}
    return id_problem(files, touched) if touched else None


def _test_of(key: str) -> str | None:
    """The test a ``tests/<name>`` folder names (its key), else None."""
    segments = key.split("/")
    return segments[1] if len(segments) == 2 and segments[0] == "tests" else None


def _move_refusal(work: _Work, change: dict, moving: dict) -> dict | None:
    """Why this move cannot be made, as its result; None when it can."""
    project, path = work.project, change["path"]
    source, target = change["from"], change["to"]
    source_key, target_key = name_key(source), name_key(target)
    keys = (source_key, target_key)
    if target_key.startswith(source_key + "/") or source_key.startswith(target_key + "/"):
        return work.not_saved(_refused(path, "invalid", "A folder cannot move into itself."), *keys)
    versions = {relative: stored.obj["version"] for relative, stored in moving.items()}
    if versions != change["named"]:
        there = under(project.files, target_key)
        landed = {relative: stored.obj["version"] for relative, stored in there.items()}
        if not moving and change["named"] and landed == change["named"]:
            if not work.broken("P9.replay"):
                return _saved(path)  # moved already: a replay
        return work.not_saved(_changed(path, None), *keys)
    if (
        target_key != source_key
        and (under(project.files, target_key) or target_key in project.files)
        and not work.broken("P9.move-conflict")
    ):
        return work.not_saved(_changed(path, None), *keys)
    depth = len(source.split("/"))
    for relative, stored in moving.items():
        new_key = f"{target_key}/{relative}"
        new_path = "/".join([target, *stored.path.split("/")[depth:]])
        crosses = is_text_path(f"{source_key}/{relative}") != is_text_path(new_key)
        if (crosses or never_travels(new_path)) and not work.broken("P9.move-refused"):
            message = (
                f"Moved to {target}, {relative} would change form or become a file that never "
                "travels (data_format.md §2, §4.2)."
            )
            return work.not_saved(_refused(path, "invalid", message), *keys)
    return None


def _move(work: _Work, change: dict) -> dict:
    project = work.project
    source, target = change["from"], change["to"]
    source_key, target_key = name_key(source), name_key(target)
    moving = under(project.files, source_key)
    refusal = _move_refusal(work, change, moving)
    if refusal is not None:
        return refusal
    moved_keys = {f"{source_key}/{relative}" for relative in moving}
    files = {key: stored for key, stored in project.files.items() if key not in moved_keys}
    base = spell(files, target)  # the folders it lands in, as already spelled
    depth = len(source.split("/"))
    events = []
    for relative, stored in moving.items():
        old_key, new_key = f"{source_key}/{relative}", f"{target_key}/{relative}"
        new_path = "/".join([base, *stored.path.split("/")[depth:]])
        if source_key == target_key and faults.on("2.10.move-case-respells"):
            new_path = stored.path
        files[new_key] = StoredFile(path=new_path, obj=stored.obj)
        events += [("file", old_key, stored.path), ("file", new_key, new_path)]
    problem = _id_problem(files, [event[1] for event in events])
    if problem is not None:
        keys = (source_key, target_key)
        return work.not_saved(_refused(change["path"], "invalid", problem), *keys)
    project.files = files
    work.events += events
    for event in events:
        work.touch(event[1])
    old_test, new_test = _test_of(source_key), _test_of(target_key)
    if old_test is not None and new_test is not None and old_test != new_test:
        for kept in (project.drafts, project.test_updated):  # a rename: its draft follows
            if old_test in kept:
                kept[new_test] = kept.pop(old_test)
        if work.broken("P9.rename-carries"):
            project.drafts.pop(new_test, None)
    return _saved(change["path"])


APPLY = {"write": _write, "delete": _delete, "bundle": _bundle, "move": _move}


# ---- The call -------------------------------------------------------------------------------------


def push_changes(call: Call) -> Answer:
    """P9: one result per change, in order; checked whole first, then kept in one step."""
    state: State = call.state
    body = call.json()
    held = held_project(call)  # one known only by its evidence too: its files are elsewhere
    sent = body.get("changes")
    if not isinstance(sent, list):
        raise bad_request("The body needs changes, a list.")
    changes = [_parse(change, state) for change in sent]
    drafts = check_drafts(body.get("drafts"))
    project_id = held.project_id
    project = copy.deepcopy(held)
    work = _Work(project, state.now(), state.broken)

    for change in changes:  # every reference's bytes, before anything is written (§3.5)
        if change["op"] == "write" and "store_id" in change["file"]:
            what = f"file {change['at']}"
            check_form(state, project_id, change["key"], change["file"], call.parts, what)
    refs = [ref_of(c["file"]) for c in changes if "store_id" in c.get("file", {})]
    check_refs(state, project_id, refs, call.parts, "change")

    tests_before = tests_of(project)
    results = [APPLY[change["op"]](work, change) for change in changes]
    for gone in set(tests_before) - set(tests_of(project)):  # its evidence stays
        project.drafts.pop(gone, None)
    save_drafts(
        project,
        drafts,
        changed=work.changed_tests,
        unsaved=work.unsaved_tests,
        functions_unsaved=work.functions_unsaved,
        now=work.now,
    )
    refs = [  # the references of the changes saved: their parts are kept, no others
        ref_of(change["file"])
        for change, result in zip(changes, results, strict=True)
        if "store_id" in change.get("file", {}) and result["status"] == "saved"
    ]
    to_write = [
        sha for sha, _size, store_id in refs if state.storages[store_id]["backend"] == "api"
    ]
    if state.broken("6.2.unreferenced-part-ignored"):
        to_write += list(call.parts)
    store_parts(state, project_id, list(dict.fromkeys(to_write)), call.parts)
    before = project_refs(held)
    state.projects[project_id] = project
    reclaim(state, project, before)
    referenced_again(state, project_id, refs)
    if not work.events and state.broken("P9.replay-no-token"):  # a replay logged again
        work.events += [("file", c["key"], c["at"]) for c in changes if "key" in c]
    if drafts and not work.events and state.broken("5.3.draft-moves-no-token"):
        work.events.append(("draft",))
    log_changes(state, project, work.events)
    return Answer({"token": token_of(state, project), "results": results})
