"""Projects: list (P1), open (P2, P2b), what changed since a token (P3), create (P11).

A project P11 created is *held*: the server keeps its files, bundle and releases (§4.2), the
releases by their test's ``test_id`` (§4.4). P11 checks the project whole against the two
rules of a test's id (``ids``): one that breaks either is ``400``, nothing kept. A project the
server knows only by its evidence (a run, a report pushed to its id) is not listed
and does not open (``404``): its files live elsewhere, and an empty project would read as every
file deleted.

The change token (rule 9) is opaque to the client; here ``<this server>.<n>``, ``n`` growing by
one with each request that changes the project's files, bundle or releases, each change logged
under it. ``since`` answers what the log holds after its token; any token this server cannot
answer from (older than the log, another server's, from the future, unreadable) is ``410
gone``, and the app opens the project in full. Text documents travel in the JSON; the parts
are those of ``api``-stored files (the bundle's archive and the releases' documents included)
not in ``have``, and an ``api`` file the server cannot give is listed in ``missing``.
"""

from __future__ import annotations

import copy
import json

from . import offer
from .call import Answer, Call
from .drafts import check_drafts, save_drafts
from .errors import ApiError, bad_request, conflict, not_found
from .ids import id_problem
from .project_files import (
    check_file,
    check_form,
    check_path,
    clashes,
    file_map,
    file_parts,
    is_text_path,
    missing_of,
    never_travels,
    ref_of,
    safe_name,
)
from .state import Project, State, StoredFile
from .storages import check_refs, referenced_again, store_parts
from .validate import (
    PROJECT_ID,
    SHA_HEX,
    format_version,
    name_key,
    nfc,
    opt_pattern,
    req_str,
)

PROJECT_FORMAT = 1
PROJECT_FIELDS = (  # what P11 reads, and the answer fields it ignores
    "format_version",
    "project_id",
    "name",
    "files",
    "bundle",
    "path",
    "token",
    "updated",
    "created_at",
    "created_by",
    "store_id",
)
BUNDLE_FIELDS = ("bundle_id", "bundle_version", "bundle_sha256", "archive")


# ---- The change token ---------------------------------------------------------------------------


def token_of(state: State, project: Project) -> str:
    """The project's change token; ``""`` on a server that does not offer P3 (§6.3, P2)."""
    if "P3" not in state.calls and not state.broken("P2.token-empty-without-P3"):
        return ""
    return f"{state.token_prefix}.{project.token}"


def log_changes(state: State, project: Project, events: list[tuple]) -> None:
    """One more token for this request's changes; the oldest changes past the log's size go,
    and ``since`` before them is then ``410 gone``."""
    if not events:
        return
    project.token += 1
    project.log += [(project.token, event) for event in events]
    overflow = len(project.log) - state.max_changes
    if overflow > 0:
        project.log_floor = project.log[overflow - 1][0]
        del project.log[:overflow]


def _gone(message: str) -> ApiError:
    return ApiError(410, "gone", "not_found", message)


def parse_since(state: State, project: Project, text: object) -> int:
    """The token's number; ``410 gone`` for any token this server cannot answer from."""
    if not isinstance(text, str) or text == "":
        raise bad_request("since is a change token, as a project answer gave it.")
    prefix, dot, number = text.rpartition(".")
    if not dot or not number.isdigit() or prefix != state.token_prefix:
        raise _gone("This server cannot answer from that change token: open the project in full.")
    since = int(number)
    if since > project.token or since < project.log_floor:
        raise _gone("This change token is too old: open the project in full.")
    return since


# ---- Answers ------------------------------------------------------------------------------------


def held_project(call: Call) -> Project:
    """The project the path names, when the server keeps its files; else ``404``."""
    project = call.state.projects.get(call.params["project_id"])
    evidence_only = project is not None and not project.held
    if project is None or (evidence_only and not call.state.broken("6.1.evidence-only-project")):
        raise not_found("This server keeps no files for this project.")
    return project


def release_answer(state: State, project_id: str, release: dict, *, documents: bool) -> dict:
    """A Release, with its documents (and ``missing``, the ``api`` ones the server cannot give)
    or without them."""
    obj = copy.deepcopy(release)
    if not documents:
        obj.pop("documents", None)
        return obj
    missing = missing_of(state, project_id, list(obj.get("documents", {}).items()))
    if missing and not state.broken("5.4.release-missing"):
        obj["missing"] = missing
    return obj


def releases_map(
    state: State, project: Project, test_ids: set[str] | None = None
) -> tuple[dict[str, list], list[tuple[str, dict]]]:
    """``{test_id: [Release with documents]}``, oldest first: every test id that has releases,
    or those ``test_ids`` name. With the documents whose parts may travel."""
    chosen = list(project.releases) if test_ids is None else test_ids
    found: dict[str, list] = {}
    items: list[tuple[str, dict]] = []
    for test_id in sorted(chosen):
        held = project.releases.get(test_id, [])
        found[test_id] = [
            release_answer(state, project.project_id, release, documents=True) for release in held
        ]
        for release in held:
            items += list(release.get("documents", {}).items())
    return found, items


def releases_changed(test_id: str) -> tuple:
    """The event ``since`` lists: the releases under ``test_id`` changed (one made)."""
    return ("releases", test_id)


def project_missing(state: State, project: Project, files: list[StoredFile], bundle: bool) -> dict:
    """``missing`` of a Project or a ProjectChanges: its files by path, the bundle as ``bundle/``."""
    items = [(stored.path, stored.obj) for stored in files]
    if bundle and project.bundle is not None:
        items.append(("bundle/", project.bundle["archive"]))
    return missing_of(state, project.project_id, items)


def project_answer(state: State, project: Project, *, listing: bool) -> dict:
    """The Project: in the list (P1) with its ``path`` and without files, bundle and releases."""
    obj = copy.deepcopy(project.extra)
    obj.update({"format_version": PROJECT_FORMAT, "project_id": project.project_id})
    if project.name is not None:
        obj["name"] = project.name
    if listing and not state.broken("P1.light-with-path"):
        obj["path"] = project.path
    obj["token"] = token_of(state, project)
    for key in ("updated", "created_at", "created_by"):
        if getattr(project, key) is not None:
            obj[key] = getattr(project, key)
    if not listing or state.broken("P1.light-with-path"):
        obj["files"] = file_map(list(project.files.values()))
        if project.bundle is not None:
            obj["bundle"] = copy.deepcopy(project.bundle)
        obj["releases"] = releases_map(state, project)[0]
        missing = project_missing(state, project, list(project.files.values()), bundle=True)
        if missing:
            obj["missing"] = missing
    return obj


def bundle_items(project: Project) -> list[tuple[str, dict]]:
    if project.bundle is None:
        return []
    return [("bundle.zip", project.bundle["archive"])]


def list_projects(call: Call) -> Answer:
    """P1: every project whose files this server keeps, by name (folded), without files."""
    held = [project for project in call.state.projects.values() if project.held]
    if call.state.broken("P1.order"):
        held.sort(key=lambda project: (project.path or "", project.project_id))
    else:
        held.sort(key=lambda project: (name_key(project.path or ""), project.project_id))
    return Answer(
        {"projects": [project_answer(call.state, project, listing=True) for project in held]}
    )


def _changes_since(state: State, project: Project, since: int) -> tuple[dict, list]:
    """P3: the files changed since the token (each as it is now), those deleted, the bundle
    when it changed, each test id whose releases changed with its whole list now; and the
    files whose parts may travel."""
    touched: dict[str, str] = {}
    bundle = False
    release_ids: set[str] = set()
    for token, event in project.log:
        if token <= since:
            continue
        if event[0] == "file":
            touched[event[1]] = event[2]
        elif event[0] == "bundle":
            bundle = True
        elif event[0] == "releases":
            release_ids.add(event[1])
    changed = [project.files[key] for key in touched if key in project.files]
    obj: dict = {
        "token": token_of(state, project),
        "files": file_map(changed),
        "deleted": sorted(
            (path for key, path in touched.items() if key not in project.files), key=name_key
        ),
    }
    items = [(stored.path, stored.obj) for stored in changed]
    if bundle and project.bundle is not None:
        obj["bundle"] = copy.deepcopy(project.bundle)
        items += bundle_items(project)
    if state.broken("P3.releases-changed"):
        release_ids = set()
    obj["releases"], release_items = releases_map(state, project, release_ids)
    missing = project_missing(state, project, changed, bundle=bundle)
    if missing:
        obj["missing"] = missing
    return obj, items + release_items


def read_project(call: Call) -> Answer:
    """P2 (the whole project), P3 (``since``) and their twin P2b, + the parts not in ``have``."""
    state = call.state
    if call.is_read_twin() and call.json().get("since") is not None:
        offer.refuse_unoffered(state.calls, "P3")  # P2b with since is P3's twin (§6.1)
        offer.note("P3")
    project = held_project(call)
    if call.is_read_twin():
        since = call.json().get("since")
    else:
        since = call.query.one("since")
    have = call.have()
    if since == "" and state.broken("P3.empty-since-400"):
        since = None
    if since is None:
        obj = project_answer(state, project, listing=False)
        if obj.get("missing") and state.broken("5.2.project-missing"):
            raise not_found("A file of this project is missing.")
        items = [(stored.path, stored.obj) for stored in project.files.values()]
        items += bundle_items(project)
        items += releases_map(state, project)[1]
    else:
        obj, items = _changes_since(state, project, parse_since(state, project, since))
    items.sort(key=lambda item: name_key(item[0]))
    return Answer(obj, file_parts(state, project.project_id, items, have, set()))


# ---- P11 ------------------------------------------------------------------------------------------


def check_bundle(bundle: object, where: str) -> dict:
    """A Bundle as sent (§4.2): its id and version, and its archive, a reference."""
    if not isinstance(bundle, dict):
        raise bad_request(f"The {where} must be a Bundle object.")
    req_str(bundle, "bundle_id", where, empty=False)
    req_str(bundle, "bundle_version", where, empty=False)
    opt_pattern(bundle, "bundle_sha256", where, SHA_HEX, "64 lower-case hex digits")
    stored = {
        key: copy.deepcopy(value)
        for key, value in bundle.items()
        if value is not None and key != "archive"
    }
    stored["archive"] = check_file(bundle.get("archive"), f"{where}'s archive", text_ok=False)
    return stored


def _config_project_id(files: dict[str, StoredFile]) -> str:
    stored = files.get("config/config.json")
    text = stored.obj.get("text") if stored is not None else None
    if text is None:
        raise bad_request("A new project's files hold config/config.json, as text.")
    try:
        config = json.loads(text)
    except ValueError as exc:
        raise bad_request("config/config.json is not valid JSON.") from exc
    project_id = config.get("project_id") if isinstance(config, dict) else None
    if not isinstance(project_id, str) or PROJECT_ID.fullmatch(project_id) is None:
        raise bad_request("config/config.json holds the project's project_id, a lower-case UUID.")
    return project_id


def _same_as_held(project: Project, files: dict[str, StoredFile], bundle: dict | None) -> bool:
    """A P11 replayed: the project holds exactly these files and this bundle still."""
    held = {key: stored.obj["version"] for key, stored in project.files.items()}
    sent = {key: stored.obj["version"] for key, stored in files.items()}
    held_bundle = project.bundle["archive"]["version"] if project.bundle else None
    sent_bundle = bundle["archive"]["version"] if bundle else None
    return held == sent and held_bundle == sent_bundle


def create_project(call: Call) -> Answer:
    """P11: a new project, its files and its bundle (+ parts); ``409 exists`` when a project
    with that id or folder is here already (a replay of the same files answers it again)."""
    state, body = call.state, call.json()
    format_version(body, "project", PROJECT_FORMAT)
    name = nfc(req_str(body, "name", "project", empty=False))
    unsafe = name != name.strip() or not safe_name(name)
    if unsafe and not state.broken("5.2.project-name-400"):
        raise bad_request(
            f"The project's name {name!r} cannot name its folder on every PC (no control "
            'character, <>:"/\\|?*, trailing dot or blank, nor a device name such as CON).'
        )
    sent = body.get("files")
    if not isinstance(sent, dict):
        raise bad_request("The project needs files, a map of path to File.")
    now = state.now()
    files: dict[str, StoredFile] = {}
    for raw_path, raw in sent.items():
        path = check_path(raw_path, "A file's path", names=not state.broken("2.10.path-names-400"))
        key = name_key(path)
        if key in files:
            raise bad_request(f"The files name {path!r} twice (paths compare by case folding).")
        if never_travels(path):
            raise bad_request(f"{path!r} is never a project file (data_format.md §4.2).")
        obj = check_file(raw, f"file {path}", text_ok=is_text_path(key))
        obj["mtime"] = now
        files[key] = StoredFile(path=path, obj=obj)
    for key, stored in files.items():
        if clashes({other: f for other, f in files.items() if other != key}, key):
            raise bad_request(f"{stored.path!r} is a file and a folder at once.")
    problem = id_problem(files)
    if problem is not None and state.broken("5.3.test-id-rules-p11"):
        raise conflict("exists", problem)
    if problem is not None:
        raise bad_request(problem)
    project_id = _config_project_id(files)
    if body.get("project_id") is not None and body["project_id"] != project_id:
        raise bad_request("The project's project_id is not the one config/config.json holds.")
    bundle = None
    if body.get("bundle") is not None:
        bundle = check_bundle(body["bundle"], "bundle")
        bundle["archive"]["mtime"] = now
    drafts = check_drafts(body.get("drafts"))
    if state.broken("P11.drafts"):
        drafts = []
    if state.broken("5.3.draft-fields"):
        for draft in drafts:
            for key in ("based_on", "updated_by", "machine_id"):
                draft.pop(key, None)

    held = state.projects.get(project_id)
    if held is not None and held.held:
        if held.name == name and _same_as_held(held, files, bundle):
            return Answer(project_answer(state, held, listing=False))
        raise conflict("exists", "A project with this project_id is on the server already.")
    for other in state.projects.values():
        if other.held and name_key(other.path or "") == name_key(name):
            raise conflict("exists", f"A project's folder is named {name} on the server already.")

    stored_files = [stored.obj for stored in files.values() if "store_id" in stored.obj]
    if bundle is not None:
        stored_files.append(bundle["archive"])
    for key, stored in files.items():
        check_form(state, project_id, key, stored.obj, call.parts, f"file {stored.path}")
    refs = [ref_of(obj) for obj in stored_files]
    to_write = check_refs(state, project_id, refs, call.parts, "project")
    store_parts(state, project_id, to_write, call.parts)
    referenced_again(state, project_id, refs)

    project = state.ensure_project(project_id, name)
    project.held = True
    project.name = name
    project.path = name
    project.created_at = now
    project.created_by = call.user.name
    project.updated = now
    project.extra = {
        key: copy.deepcopy(value)
        for key, value in body.items()
        if key not in PROJECT_FIELDS and key not in ("import", "drafts") and value is not None
    }
    project.files = files
    project.bundle = bundle
    save_drafts(project, drafts, changed=set(), unsaved=set(), functions_unsaved=False, now=now)
    for key in files:
        if key.startswith("tests/") and key.count("/") >= 2:
            project.test_updated[key.split("/")[1]] = now
    project.token += 1  # older tokens (an evidence-only project's) describe no files: 410
    project.log = []
    project.log_floor = project.token
    return Answer(project_answer(state, project, listing=False))
