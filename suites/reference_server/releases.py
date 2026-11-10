"""Releases: list (P8), read one (P8b), release (P10), all by the test's ``test_id``.

A release is filed under its test's ``test_id``, never its name (data_format.md §4.4): each id
numbers its own releases from 1, a rename moves none, a deleted test's stay under its id, and
nothing is ever retired. P10 releases the test whose ``test.json`` holds the path's id (none:
``404``) and names the version of each file it releases (only those are kept: the app chooses
them); any that is not the server's now is ``409 changed`` with the test's documents now and
the paths that changed, and a ``content`` a release under the same id has is ``409
same_as_release``. Its documents are kept by their names in the test: a path under
``tests/<test>/`` less that folder, a ``functions/`` path whole; two files taking one name are
``400``. A release is never changed afterwards: its ``test`` stays the name it was released
under.

An importer with ``"import": true`` keeps the ``version``, ``released_at`` and ``test`` sent
(§6.10), filed under the path's id whether or not a test holds it now; a version taken by
another release is ``409 changed`` (the same release again answers it). Its old documents are
no longer the project's files, so it sends them whole, as ``documents`` (the Release's own map:
text, or references whose bytes are parts of the same ``multipart/form-data`` request or held
already), in place of ``files``.
"""

from __future__ import annotations

import copy

from . import offer
from .call import Answer, Call
from .errors import bad_request, conflict, not_found
from .ids import test_holding, tests_of
from .project_files import check_file, check_path, file_map, file_parts, ref_of
from .project_tests import documents_of
from .projects import held_project, log_changes, release_answer, releases_changed
from .state import Project
from .storages import check_refs, referenced_again, store_parts
from .validate import (
    MACHINE_ID,
    SHA_HEX,
    TEST_ID,
    format_version,
    name_key,
    nfc,
    opt_bool,
    opt_int,
    opt_pattern,
    opt_str,
    opt_time,
    req_pattern,
    version_hex,
)

RELEASE_FORMAT = 1
TEST_FORMATS = ("text", "code")
REQUEST_FIELDS = (  # read from a P10 body; every other field is kept as sent
    "format_version",
    "test_id",
    "test",
    "version",
    "format",
    "fingerprint",
    "content",
    "note",
    "released_at",
    "released_by",
    "machine_id",
    "numbered_before_releases",
    "pc",
    "files",
    "documents",
    "import",
)


def _test_id_param(call: Call) -> str:
    test_id = call.params["test_id"]
    if TEST_ID.fullmatch(test_id) is None:
        raise bad_request("A release call names its test by its test_id, a lower-case UUID.")
    return test_id


def _project_and_id(call: Call) -> tuple[Project, str]:
    """The project and the path's test id; the id's form checked first, before the project's
    existence is looked at (§2.5)."""
    if call.state.broken("2.5.malformed-test-id-400-first"):
        project = held_project(call)
        return project, _test_id_param(call)
    test_id = _test_id_param(call)
    return held_project(call), test_id


def _version_param(call: Call) -> int:
    text = call.params["version"]
    if not text.isdigit() or int(text) < 1:
        raise bad_request("A release's version is an integer of at least 1.")
    return int(text)


def list_releases(call: Call) -> Answer:
    """P8: the releases under the test id, oldest first, without documents; ``[]`` for none."""
    project, test_id = _project_and_id(call)
    releases = project.releases.get(test_id, [])
    listed = [release_answer(call.state, project.project_id, r, documents=False) for r in releases]
    return Answer({"releases": listed})


def get_release(call: Call) -> Answer:
    """P8b and its ``/read`` twin: one release with its documents + parts."""
    project, test_id = _project_and_id(call)
    version = _version_param(call)
    for release in project.releases.get(test_id, []):
        if release["version"] == version:
            obj = release_answer(call.state, project.project_id, release, documents=True)
            items = list(obj.get("documents", {}).items())
            return Answer(
                obj, file_parts(call.state, project.project_id, items, call.have(), set())
            )
    raise not_found(f"The test {test_id} has no release v{version}.")


# ---- P10 ------------------------------------------------------------------------------------------


def _named_files(
    project: Project, test_key: str, files: object, *, overwrite: bool = False
) -> tuple[dict[str, dict], list[str]]:
    """The documents P10's ``files`` name, by their names in the test, and the paths whose
    version is not the server's now."""
    if not isinstance(files, dict):
        raise bad_request("The release needs files, a map of path to version.")
    documents: dict[str, dict] = {}
    changed: list[str] = []
    for raw_path, version in files.items():
        path = check_path(raw_path, "A released file's path")
        key = name_key(path)
        version_hex(version, f"The version of {path}")
        segments = key.split("/")
        if not (segments[:2] == ["tests", test_key] and len(segments) > 2) and not (
            segments[0] == "functions" and len(segments) > 1
        ):
            raise bad_request(
                f"{path!r} is not the test's: a release names files under its folder and "
                "the functions/ files they import."
            )
        stored = project.files.get(key)
        if stored is None or stored.obj["version"] != version:
            changed.append(path)
            continue
        spelled = stored.path.split("/")
        name = "/".join(spelled[2:]) if segments[0] == "tests" else stored.path
        if name_key(name) in {name_key(held) for held in documents} and not overwrite:
            raise bad_request(f"Two of the files released take the document name {name!r}.")
        documents[name] = copy.deepcopy(stored.obj)
    return documents, changed


def _imported_documents(call: Call, documents: object) -> dict[str, dict]:
    """An imported release's own documents, whole: text, or references to held bytes."""
    if not isinstance(documents, dict):
        raise bad_request("documents is a map of name to File.")
    checked = {}
    for raw_name, obj in documents.items():
        name = check_path(raw_name, "A document's name")
        checked[name] = check_file(obj, f"document {name}", text_ok=True)
        checked[name]["mtime"] = call.state.now()
    refs = [ref_of(obj) for obj in checked.values() if "store_id" in obj]
    to_keep = check_refs(call.state, call.params["project_id"], refs, call.parts, "release")
    store_parts(call.state, call.params["project_id"], to_keep, call.parts)
    referenced_again(call.state, call.params["project_id"], refs)
    return checked


def release(call: Call) -> Answer:
    """P10: check the files named, refuse a content already released, give the next number."""
    state, body = call.state, call.json()
    project, test_id = _project_and_id(call)
    if state.broken("P10.body-test-id-ignored") and isinstance(body.get("test_id"), str):
        test_id = body["test_id"]
    importing = call.importing()
    where = "release"
    if body.get("format_version") is not None:
        format_version(body, where, RELEASE_FORMAT)
    if body.get("format") not in TEST_FORMATS:
        raise bad_request("The release's format is text or code.")
    fingerprint = req_pattern(body, "fingerprint", where, SHA_HEX, "64 lower-case hex digits")
    content = req_pattern(body, "content", where, SHA_HEX, "64 lower-case hex digits")
    for key in ("note", "released_by", "pc"):
        opt_str(body, key, where)
    opt_pattern(body, "machine_id", where, MACHINE_ID, "8 lower-case hex digits")
    opt_bool(body, "numbered_before_releases", where)
    version = opt_int(body, "version", where, minimum=1) if importing else None
    released_at = opt_time(body, "released_at", where) if importing else None
    released_as = opt_str(body, "test", where, empty=False) if importing else None

    test_key = test_holding(project, test_id)
    if test_key is None and (not importing or state.broken("import.P10")):
        raise not_found(f"No test of this project holds the test_id {test_id}.")
    if not importing or state.broken("U1.import-exempt"):  # §6.8: with U1, one of its users
        offer.known_operator(state.calls, state.people, body.get("released_by"), "released_by",
                             unnamed_refused=state.unnamed_refused)  # fmt: skip
    releases = project.releases.get(test_id, [])
    # The test's name now, or the name an import says it was released under.
    spelling = nfc(released_as) if released_as else tests_of(project).get(test_key or "")
    if spelling is None:
        raise bad_request("An imported release of an id no test holds names its test.")
    if importing and body.get("documents") is not None:
        documents = _imported_documents(call, body["documents"])
    elif test_key is None:
        raise bad_request("An imported release of an id no test holds sends its documents.")
    else:
        overwrite = state.broken("P10.two-files-one-name-400")
        documents, changed = _named_files(project, test_key, body.get("files"), overwrite=overwrite)
        if changed:
            raise conflict(
                "changed",
                f"{len(changed)} of the files released changed on the server meanwhile.",
                current=file_map(documents_of(project, test_key)),
                changed=changed,
            )
    if version is not None:
        for held in releases:
            if held["version"] == version:
                if (held["content"], held["fingerprint"]) == (content, fingerprint):
                    replay = release_answer(state, project.project_id, held, documents=False)
                    return Answer(replay)
                raise conflict(
                    "changed",
                    f"{spelling} has another release v{version}.",
                    current=release_answer(state, project.project_id, held, documents=False),
                )
    else:
        for held in releases:
            if held["content"] == content:
                raise conflict(
                    "same_as_release",
                    f"Nothing to release: the files are exactly v{held['version']}.",
                    version=held["version"],
                )
        version = max((held["version"] for held in releases), default=0) + 1

    made: dict = {
        "format_version": RELEASE_FORMAT,
        "test_id": test_id,
        "test": spelling,
        "version": version,
        "format": body["format"],
        "fingerprint": fingerprint,
        "content": content,
    }
    for key in ("note", "released_by", "pc", "machine_id", "numbered_before_releases"):
        if body.get(key) is not None:
            made[key] = body[key]
    if state.broken("5.4.numbered-before-releases"):
        made.pop("numbered_before_releases", None)
    made["released_at"] = released_at or state.now()
    for key, value in body.items():
        if key not in REQUEST_FIELDS and value is not None:
            made[key] = copy.deepcopy(value)
    made["documents"] = documents
    project.releases[test_id] = sorted([*releases, made], key=lambda held: held["version"])
    if not state.broken("P3.token-moves"):
        log_changes(state, project, [releases_changed(test_id)])
    light = not state.broken("P10.answer-light")
    return Answer(release_answer(state, project.project_id, made, documents=not light))
