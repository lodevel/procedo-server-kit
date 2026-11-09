"""Entries, never replaced: a run's annotation (E7, §6.5) and a board's lot and fields (E15, §6.8).

Each entry is added to its history, in the order received, and never changed. Its slot is its
kind and its key (an annotation's) or its name, compared exactly (a board field's). Which entry
of a slot is current is computed from that order on every read, never stored: the slot's first
entry, then each entry whose ``replaces`` names the current one. An entry is answered ``saved``
when it becomes current, ``changed`` (kept, not current, with the current entry) when another
was saved meanwhile, ``already`` when its ``entry_id`` is held.
"""

from __future__ import annotations

import copy
from collections.abc import Callable

from . import faults
from .errors import bad_request
from .state import State
from .validate import drop_nulls, is_time

ANSWER_FIELDS = ("current", "received_at", "import")
NULL_VALUES = ("text", "value", "replaces")  # null is itself a value (§2)


def annotation_group(entry: dict) -> tuple:
    return (entry["kind"], entry["key"])


def board_group(entry: dict) -> tuple:
    if entry["kind"] == "lot":
        return ("lot",)
    if faults.on("2.4.exact-exceptions"):
        return ("field", entry["name"].casefold())
    return ("field", entry["name"])


def board_group_folded(entry: dict) -> tuple:
    """A board entry's slot with the field's name case folded: the fault of
    ``5.11.field-name-exact``, never the rule."""
    group = board_group(entry)
    return group if len(group) == 1 else ("field", group[1].casefold())


def current_entries(
    history: list[dict], group_of: Callable[[dict], tuple], *, last_wins: bool = False
) -> dict[tuple, dict]:
    """Each slot's current entry, by the rule of §6.5 applied in the order received
    (``last_wins``: each slot's last entry received, a fault)."""
    current: dict[tuple, dict] = {}
    for entry in history:
        group = group_of(entry)
        held = current.get(group)
        if held is None or entry.get("replaces") == held["entry_id"] or last_wins:
            current[group] = entry
    return current


def entries_answer(
    history: list[dict], group_of: Callable[[dict], tuple], *, last_wins: bool = False
) -> list[dict]:
    """The history as a reader sees it: each entry with ``current`` computed now."""
    found = current_entries(history, group_of, last_wins=last_wins)
    current = {id(entry) for entry in found.values()}
    return [dict(copy.deepcopy(entry), current=id(entry) in current) for entry in history]


def check_received(entries: list[dict], *, importing: bool) -> None:
    """An import's ``received_at``, checked before anything is written (§6.10)."""
    if importing:
        for sent in entries:
            received = sent.get("received_at")
            if received is not None and not is_time(received):
                raise bad_request("An entry's received_at must be a UTC time.")


def add_entries(
    state: State,
    history: list[dict],
    entries: list[dict],
    group_of: Callable[[dict], tuple],
    *,
    importing: bool,
) -> list[dict]:
    """Append each entry to ``history``; answer one result per entry, in order.

    An import keeps each entry's ``received_at``, or leaves it without one (§6.10); the caller
    checked it first (``check_received``).
    """
    held = {entry["entry_id"] for entry in history}
    results = []
    for sent in entries:
        entry_id = sent["entry_id"]
        if entry_id in held:
            results.append({"entry_id": entry_id, "status": "already"})
            continue
        current = current_entries(history, group_of).get(group_of(sent))
        stored = drop_nulls(
            {key: copy.deepcopy(value) for key, value in sent.items() if key not in ANSWER_FIELDS},
            keep=NULL_VALUES,
        )
        if not importing or state.broken("import.E7-E15-received-at"):
            stored["received_at"] = state.now()
        elif sent.get("received_at") is not None:
            stored["received_at"] = sent["received_at"]
        result = {"entry_id": entry_id}
        if current is None or sent["replaces"] == current["entry_id"]:
            result["status"] = "saved"
        else:
            result["status"] = "changed"
            result["current"] = dict(copy.deepcopy(current), current=True)
        history.append(stored)
        held.add(entry_id)
        results.append(result)
    return results


def annotation_data(entries: list[dict], *, at_received: bool = False) -> dict:
    """The run file's own form of what is current (§4.9, ``data``); ``at_received``: each
    entry's ``received_at`` as its ``at``, a fault."""
    data: dict = {
        "version": "2.0",
        "comments": {"criteria": {}},
        "verdict_overrides": {"criteria": {}},
    }
    for (kind, key), entry in current_entries(entries, annotation_group).items():
        field_name = "text" if kind == "comment" else "value"
        value = entry.get(field_name)
        if value is None:
            continue
        item = {
            field_name: value,
            "by": entry["by"],
            "at": entry.get("received_at") if at_received else entry["saved_at"],
            "entry_id": entry["entry_id"],
        }
        section = data["comments"] if kind == "comment" else data["verdict_overrides"]
        if key == "test":
            section["test"] = item
        else:
            section["criteria"][key] = item
    return data


def board_values(entries: list[dict], *, last_lot: bool = False) -> tuple[str | None, dict]:
    """A board's lot and details, from its current entries (§4.11); ``last_lot``: the last
    lot entry received, a fault."""
    current = current_entries(entries, board_group)
    lot_entry = current.get(("lot",))
    if last_lot:
        lot_entry = next((e for e in reversed(entries) if e["kind"] == "lot"), None)
    lot = lot_entry.get("value") if lot_entry else None
    details = {
        group[1]: entry["value"]
        for group, entry in current.items()
        if group[0] == "field" and entry.get("value") is not None
    }
    return lot, details
