"""Where the bytes are (data_format.md §3): S1–S4, and the check every write makes (§3.5).

The server has one storage of its own, ``api`` (store 1), whose bytes travel as parts, and,
when its administrator set one, an own ``filesystem`` storage (store 2, §7.1) the app writes to
itself: references into it are checked, and what nothing references there any more is
reclaimed. A storage the app declares (S2) is reached only when its folder lies in a folder
this server mounts (``State.reaches``): then references into it are checked; else they are
assumed to hold. Either way the server never deletes in it: what nothing in a project
references any more there is listed, by project, for the app to delete (S3), in the set-aside
steps of §6.7 (S4) that never lose a blob a push referenced.
"""

from __future__ import annotations

import copy
from pathlib import Path

from .blobs import BlobStore
from .call import Answer, Call
from .errors import ApiError, bad_request, not_found
from .state import API_STORE_ID, Project, State
from .validate import PROJECT_ID, name_key, req_str, version_hex

DECLARABLE = ("filesystem", "ftps", "sftp")
SECRET_PARAMS = ("password", "passwd", "passphrase", "secret", "private_key", "token")


# ---- The check every write makes ------------------------------------------------------------


def check_refs(
    state: State,
    project_id: str,
    refs: list[tuple[str, int, int]],
    parts: dict[str, bytes],
    noun: str,
) -> list[str]:
    """Every reference ``(sha256 hex, size, store_id)`` must point at bytes that are there.

    For the ``api`` storage, a part of this request, or a blob this project already holds whole
    (one uploaded alone for it, §3.4b, included); for another storage this server reaches, a
    blob there whole; a storage it does not reach is assumed to hold.
    Returns the parts to keep with the project (``store_parts``). Raises ``409
    missing_files`` (nothing written) or ``400 bad_request`` for an unknown ``store_id``.
    """
    sizes: dict[str, int] = {}
    missing: list[str] = []
    to_write: list[str] = []
    for sha, size, store_id in refs:
        if sizes.setdefault(sha, size) != size:
            raise bad_request(f"The {noun} names sha256 {sha[:12]}… with two different sizes.")
        storage = state.storage_of(store_id)
        if storage is None and state.broken("6.2.unknown-store-id-400"):
            continue
        if storage is None:
            raise bad_request(
                f"store_id {store_id} names no storage this server lists.", store_id=store_id
            )
        if storage["backend"] != "api":
            if state.broken("6.2.unreachable-assumed"):
                missing.append(sha)
            elif _absent_in_reached(state, storage, project_id, sha, size):
                missing.append(sha)
            continue
        if sha in parts:
            if len(parts[sha]) != size and not state.broken("6.2.part-size-400"):
                raise bad_request(
                    f"The part sha256:{sha} holds {len(parts[sha])} bytes, the {noun} says {size}."
                )
            if sha not in to_write:
                to_write.append(sha)
        elif state.blobs.check(project_id, sha, size) is None:
            continue  # this project holds it (sent before, or alone first: §3.4b)
        elif _held_by_fault(state, project_id, sha, noun):
            continue
        else:
            missing.append(sha)
    if missing:
        versions = sorted({f"sha256:{sha}" for sha in missing})
        raise ApiError(
            409,
            "missing_files",
            "denied" if state.broken("4.3.409-invalid") else "invalid",
            f"{len(versions)} files of this {noun} are not in the storage yet; send them, then the {noun} again.",
            details={"missing": versions},
        )
    return to_write


def _absent_in_reached(state: State, storage: dict, project_id: str, sha: str, size: int) -> bool:
    """A blob a storage this server reaches lacks (or holds damaged), §7.1 and §6.2."""
    reached = state.reached.get(storage["store_id"])
    if reached is None:
        return False  # not reached: assumed to hold
    if not storage["declared"] and state.broken("7.1.references-checked"):
        return False
    return reached.check(project_id, sha, size) is not None


def _held_by_fault(state: State, project_id: str, sha: str, noun: str) -> bool:
    """A blob the server takes as held only because it breaks a rule on purpose (``faults``)."""
    if state.broken("6.2.missing-files-409"):
        return True
    if noun == "change" and state.broken("P9.missing-files-409"):
        return True
    if state.broken("6.2.blobs-per-project") and state.blobs.owner_holding(sha) is not None:
        return True
    return state.broken("6.2.damaged-blob") and state.blobs.path(project_id, sha).exists()


def store_parts(state: State, project_id: str, shas: list[str], parts: dict[str, bytes]) -> None:
    """Keep each part with its project (or ``library``)."""
    for sha in shas:
        if sha in parts:
            state.blobs.put(project_id, sha, parts[sha])


def referenced_again(state: State, project_id: str, refs: list[tuple[str, int, int]]) -> None:
    """A blob a write references is no longer one to delete (§6.7): S4 then answers ``keep``."""
    for sha, _size, store_id in refs:
        state.to_delete.get(store_id, {}).get(project_id, set()).discard(sha)


def _file_ref(found: set[tuple[int, str]], obj: dict | None) -> None:
    if obj is not None and obj.get("store_id") is not None:
        found.add((obj["store_id"], obj["version"].removeprefix("sha256:")))


def project_refs(project: Project) -> set[tuple[int, str]]:
    """Every ``(store_id, sha256)`` the project references: its runs, its artifacts, its files,
    its bundle and its releases' documents (§3.2: a blob counts only for its own project)."""
    found = set()
    for run in project.runs.values():
        for item in run.obj["files"]:
            found.add((item["store_id"], item["sha256"]))
    for slot in project.artifacts.values():
        for item in slot.items:
            if item.obj.get("store_id") is not None:
                found.add((item.obj["store_id"], item.obj["sha256"]))
    for stored in project.files.values():
        _file_ref(found, stored.obj)
    if project.bundle is not None:
        _file_ref(found, project.bundle["archive"])
    for releases in project.releases.values():
        for release in releases:
            for document in release.get("documents", {}).values():
                _file_ref(found, document)
    return found


def reclaim(state: State, project: Project, before: set[tuple[int, str]]) -> dict[str, list[str]]:
    """What the project referenced ``before`` and no longer does: the server's own blobs are
    removed (``api`` and an own storage, §7.1), a declared storage's are listed for the app to
    delete (S3) and never deleted there (§7.3). Returns those, by ``store_id`` as a decimal
    string."""
    freed: dict[str, list[str]] = {}
    for store_id, sha in sorted(before - project_refs(project)):
        if store_id == API_STORE_ID:
            state.blobs.remove(project.project_id, sha)
            continue
        storage, reached = state.storage_of(store_id), state.reached.get(store_id)
        if storage is not None and not storage.get("declared"):
            if reached is not None:
                reached.remove(project.project_id, sha)
            continue
        if reached is not None and state.broken("7.3.declared-never-cleaned"):
            reached.remove(project.project_id, sha)
        listed = state.to_delete.setdefault(store_id, {}).setdefault(project.project_id, set())
        listed.add(sha)
        freed.setdefault(str(store_id), []).append(f"sha256:{sha}")
    return freed


# ---- S1–S4 -------------------------------------------------------------------------------------


def list_storages(call: Call) -> Answer:
    state = call.state
    storages = [copy.deepcopy(state.storages[key]) for key in sorted(state.storages)]
    for storage in storages:
        if state.broken("5.15.storage-fields"):
            storage.pop("reachable", None)
        if state.broken("7.1.params-no-secret") and storage["backend"] == "api":
            storage["params"] = {"password": "kit"}
    if state.broken("S1.storages"):
        return Answer({"storages": storages})
    return Answer({"instance_id": state.instance_id, "storages": storages})


def declare_storage(call: Call) -> Answer:
    state, body = call.state, call.json()
    backend = body.get("backend")
    if backend not in DECLARABLE:
        raise bad_request(f"A declared storage's backend is one of {', '.join(DECLARABLE)}.")
    params = body.get("params")
    strings = state.broken("S2.params-strings-400") or all(
        isinstance(value, str) for value in (params or {}).values()
    )
    if not isinstance(params, dict) or not strings:
        raise bad_request("params must be an object of strings.")
    for name in params:
        folded = name if state.broken("S2.secret-key-400") else name.casefold()
        if folded in SECRET_PARAMS:
            raise bad_request(f"params never holds a secret ({name!r}); it stays on each PC.")
    identity = req_str(body, "identity", "storage", empty=False)
    exact = not state.broken("2.4.exact-exceptions")
    for storage in state.storages.values():
        same = (
            storage["identity"] == identity
            if exact
            else name_key(storage["identity"]) == name_key(identity)
        )
        if storage.get("declared") and same and not state.broken("S2.declare"):
            return Answer({"status": "already", "storage": copy.deepcopy(storage)})
    store_id = max(state.storages) + 1
    reached = backend == "filesystem" and state.reaches_folder(params.get("root"))
    storage = {key: copy.deepcopy(value) for key, value in body.items() if key != "store_id"}
    storage.update(
        {
            "format_version": 1,
            "store_id": store_id,
            "backend": backend,
            "params": dict(params),
            "identity": identity,
            "active": False,
            "declared": True,
            "reachable": reached,
        }
    )
    state.storages[store_id] = storage
    if reached:
        state.reached[store_id] = BlobStore(Path(params["root"]))
    return Answer({"status": "saved", "storage": copy.deepcopy(storage)})


def _declared(call: Call) -> int:
    text = call.params["store_id"]
    if not text.isdigit():
        raise bad_request("store_id is an integer.")
    store_id = int(text)
    storage = call.state.storage_of(store_id)
    if storage is None:
        raise not_found(f"No storage {store_id}.")
    if not storage.get("declared") and not call.state.broken("S3S4.declared-only"):
        raise bad_request(
            f"Storage {store_id} is the server's own: the server reclaims its blobs itself."
        )
    return store_id


def _by_project(listed: dict[str, set[str]]) -> dict[str, list[str]]:
    """``{"<project_id>": ["sha256:<hex>"]}``, projects with nothing listed left out."""
    return {
        project_id: [f"sha256:{sha}" for sha in sorted(shas)]
        for project_id, shas in sorted(listed.items())
        if shas
    }


def to_delete(call: Call) -> Answer:
    """S3: the blobs nothing in their project references any more, by project."""
    store_id = _declared(call)
    if call.state.broken("S3.to-delete"):
        return Answer({"to_delete": {}})
    return Answer({"to_delete": _by_project(call.state.to_delete.get(store_id, {}))})


def deleted(call: Call) -> Answer:
    """S4: the app set these aside; ``delete`` those still unreferenced, ``keep`` the others."""
    store_id = _declared(call)
    sent = call.json().get("set_aside")
    if not isinstance(sent, dict):
        raise bad_request('The body needs set_aside, {"<project_id>": ["sha256:<hex>", ...]}.')
    set_aside: dict[str, list[str]] = {}
    for project_id, versions in sent.items():
        if PROJECT_ID.fullmatch(project_id) is None:
            raise bad_request("set_aside is keyed by project_id, a lower-case UUID.")
        if not isinstance(versions, list):
            raise bad_request("Each project's set_aside is a list of versions.")
        set_aside[project_id] = [version_hex(value, "Each set-aside blob") for value in versions]
    listed = call.state.to_delete.setdefault(store_id, {})
    delete: dict[str, set[str]] = {}
    keep: dict[str, set[str]] = {}
    for project_id, shas in set_aside.items():
        held = listed.get(project_id, set())
        for sha in shas:
            if sha in held or call.state.broken("S4.deleted"):
                held.discard(sha)
                delete.setdefault(project_id, set()).add(sha)
            else:  # referenced since S3 listed it (or never listed): never lost
                keep.setdefault(project_id, set()).add(sha)
    return Answer(
        {"delete": _by_project(delete), "keep": _by_project(keep), "to_delete": _by_project(listed)}
    )


# ---- E14 ----------------------------------------------------------------------------------------


def list_benches(call: Call) -> Answer:
    state = call.state
    benches = [copy.deepcopy(state.benches[key]) for key in sorted(state.benches)]
    if state.broken("5.14.bench"):
        for bench in benches:
            bench.pop("label", None)
    if state.broken("E14.benches"):
        benches = []
    return Answer({"benches": benches})
