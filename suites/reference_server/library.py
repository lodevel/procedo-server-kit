"""The library: list (L1), read one version (L2), publish one (L3). data_format.md §4.16, §5.3.

A version is ``<id>@<version>`` within its kind, compared exactly (as the app's library
compares them), created once: published again with the same ``content_sha256`` it is
``already`` (the item as first published), with another ``409 version_exists``
(``details.content_sha256`` the stored one). One that differs from a held one of its kind only
by case is ``409 exists``: a PC's library folder (``<id>/<version>/`` on a Windows disk) could
not hold both. Its archive is a reference to a storage, its ``api`` bytes kept under
``library`` in place of a project (§3.2); one the server cannot give is in ``missing``
(``archive``). ``contains_code`` is the publisher's word, never checked against the archive
(the server opens no archive), except that a ``capability``, ``wizard``, ``tool`` or
``package`` always holds code, so one sent with ``false`` is refused; an item holding code is
published only by a login with the ``publisher`` role. ``published_at`` and ``published_by``
(the login) are the server's. The list leaves the manifest out; it is ordered by kind, id (by
code point), then publication, oldest first.
"""

from __future__ import annotations

import copy

from .blobs import LIBRARY
from .call import Answer, Call
from .errors import bad_request, conflict, forbidden, not_found
from .project_files import check_file, file_parts, missing_of, ref_of
from .storages import check_refs, store_parts
from .validate import (
    MACHINE_ID,
    SHA_HEX,
    format_version,
    name_key,
    opt_int,
    opt_object,
    opt_pattern,
    opt_str,
    req_bool,
    req_pattern,
    req_str,
)

KINDS = ("bundle", "config", "report", "export", "capability", "wizard", "skill", "tool", "package")
ALWAYS_CODE = ("capability", "wizard", "tool", "package")
ITEM_FORMAT = 1
ANSWER_FIELDS = ("published_at", "published_by")


def _kind(value: str | None) -> str:
    if value not in KINDS:
        raise bad_request(f"A library kind is one of {', '.join(KINDS)}.")
    return value


def item_answer(item: dict, *, manifest: bool) -> dict:
    obj = copy.deepcopy(item)
    if not manifest:
        obj.pop("manifest", None)
    return obj


def list_library(call: Call) -> Answer:
    """L1: every kind (or one), every id with every version, without manifests."""
    state = call.state
    kind = call.query.one("kind")
    if kind is not None and not (state.broken("L1.kind-400") and kind not in KINDS):
        _kind(kind)
    order = {key: index for index, key in enumerate(state.library)}
    keys = sorted(state.library, key=lambda key: (key[0], key[1], order[key]))
    if state.broken("L1.order"):
        keys = list(state.library)
    items = [
        item_answer(state.library[key], manifest=state.broken("L1.light"))
        for key in keys
        if kind is None or key[0] == kind
    ]
    return Answer({"items": items})


def get_item(call: Call) -> Answer:
    """L2 and its ``/read`` twin: one version with its manifest + its archive's part."""
    kind = _kind(call.params["kind"])
    item = call.state.library.get((kind, call.params["id"], call.params["version"]))
    if item is None and call.state.broken("2.4.exact-exceptions"):
        asked = (kind, name_key(call.params["id"]), name_key(call.params["version"]))
        for key, held in call.state.library.items():
            if (key[0], name_key(key[1]), name_key(key[2])) == asked:
                item = held
    if item is None:
        raise not_found(
            f"The library holds no {kind} {call.params['id']}@{call.params['version']}."
        )
    obj = item_answer(item, manifest=True)
    missing = missing_of(call.state, LIBRARY, [("archive", obj["archive"])])
    if missing:
        obj["missing"] = missing
    parts = file_parts(call.state, LIBRARY, [("archive.zip", obj["archive"])], call.have(), set())
    return Answer(obj, parts)


def _checked(body: dict, kind: str, code_kinds: bool = True) -> dict:
    where = "library item"
    format_version(body, where, ITEM_FORMAT)
    if body.get("kind") != kind:
        raise bad_request("The item's kind is the one its path names.")
    req_str(body, "id", where, empty=False)
    req_str(body, "version", where, empty=False)
    req_pattern(body, "content_sha256", where, SHA_HEX, "64 lower-case hex digits")
    opt_int(body, "content_bytes", where)
    opt_object(body, "manifest", where)
    opt_str(body, "requires_app", where)
    opt_pattern(body, "machine_id", where, MACHINE_ID, "8 lower-case hex digits")
    if req_bool(body, "contains_code", where) is False and kind in ALWAYS_CODE and code_kinds:
        raise bad_request(f"A {kind} holds Python a PC would run: contains_code is true.")
    item = {
        key: copy.deepcopy(value)
        for key, value in body.items()
        if key not in ANSWER_FIELDS and key != "archive" and value is not None
    }
    item["archive"] = check_file(body.get("archive"), "item's archive", text_ok=False)
    return item


def publish(call: Call) -> Answer:
    """L3: ``saved``, or ``already`` for the same content; ``409 version_exists`` else."""
    state, body = call.state, call.json()
    kind = _kind(call.params["kind"])
    item = _checked(body, kind, not state.broken("5.16.contains-code-400"))
    by_any = state.broken("5.16.code-needs-publisher")
    if item["contains_code"] and "publisher" not in call.user.roles and not by_any:
        raise forbidden("An item holding code is published by a login with the publisher role.")
    key = (kind, item["id"], item["version"])
    held = state.library.get(key)
    folded = (kind, name_key(item["id"]), name_key(item["version"]))
    for other in state.library:
        twin = (other[0], name_key(other[1]), name_key(other[2])) == folded
        if other != key and twin and not state.broken("5.16.case-twin-409"):
            raise conflict(
                "exists",
                f"The library holds {other[1]}@{other[2]}, which differs from "
                f"{item['id']}@{item['version']} only by case: a PC could not hold both.",
            )
    if held is not None and not state.broken("5.16.created-once"):
        if held["content_sha256"] == item["content_sha256"]:
            return Answer({"status": "already", "item": item_answer(held, manifest=True)})
        raise conflict(
            "version_exists",
            f"{item['id']}@{item['version']} is on the server already with other content: "
            "publish it under a new version.",
            content_sha256=held["content_sha256"],
        )
    refs = [ref_of(item["archive"])]
    store_parts(state, LIBRARY, check_refs(state, LIBRARY, refs, call.parts, "item"), call.parts)
    item["archive"]["mtime"] = state.now()
    item["published_at"] = state.now()
    item["published_by"] = call.user.name
    if state.broken("5.16.item-roundtrip"):
        item["published_by"] = str(body.get("published_by") or "somebody")
    state.library[key] = item
    return Answer({"status": "saved", "item": item_answer(item, manifest=True)})
