"""What people write about a run or a board as ENTRIES, never replaced whole.

Each comment or verdict override a person saves on a run is an entry
(``docs/plans/data_format.md`` §4.9), and so is each lot or field saved with a board (§4.11):
added to the run's or the board's list, never changed nor removed, so one person's save can
never erase another's. An entry belongs to a SLOT, its ``kind`` and what it is about: a run
annotation's ``key`` (``test`` or a check's key), a board entry's ``name`` (the field's; none
for the lot). It names the entry it ``replaces`` — the one that person saw as current for that
slot when they edited. THE rule, applied in the order the store received the entries (never two
PCs' clocks compared): an entry whose ``replaces`` is the slot's current entry becomes current;
one that arrived after someone else's is kept in the history, not current ("changed", §6.5 E7,
§6.8 E15). An entry id already in the list is the same save replayed. The store that takes an
entry writes when it did (:data:`RECEIVED_AT`).

What a reader sees is each slot's current entry: a run's in the run file's own shape
(:func:`current_data`), a board's as its lot and details (:func:`board_data`);
:func:`histories` is the list as the history shows it. A run written before entries (the
whole-file ``annotations.json``) reads as its first entries (:func:`legacy_entries`).

Qt-free, I/O-free: the stores keep the entries, this module reads them.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

COMMENT = "comment"
OVERRIDE = "override"
TEST = "test"  # the key of the whole run; a check's stable key otherwise
LOT = "lot"  # the lot a board was built in
DETAIL = "field"  # one of the project's fields kept with a board, by its name

RUN_KINDS = (COMMENT, OVERRIDE)  # what a run's annotation holds
BOARD_KINDS = (LOT, DETAIL)  # what a board's entries hold

SAVED = "saved"  # it is the slot's current entry now
CHANGED = "changed"  # someone else's arrived first: kept in the history, not current
ALREADY = "already"  # the store holds that entry id already (an outbox replay)

#: Where a value sits in an entry, and the section the run file's shape keeps it in.
FIELD = {COMMENT: "text", OVERRIDE: "value", LOT: "value", DETAIL: "value"}
SECTION = {COMMENT: "comments", OVERRIDE: "verdict_overrides"}
#: What an entry is about, beside its kind: a run annotation's key, a board field's name. A
#: board has one lot, so a lot entry names nothing (its slot is ``("lot", "")``).
ABOUT = {COMMENT: "key", OVERRIDE: "key", DETAIL: "name"}
#: When the store took an entry, on its clock: written by the store, never by the bench.
RECEIVED_AT = "received_at"

Slot = tuple[str, str]  # (kind, key) of a run's annotation, (kind, name) of a board's entry


def utc_now() -> str:
    """Now, UTC, to the second: an entry's ``saved_at`` and ``received_at``."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_entry(
    kind: str,
    key: str,
    value: str | None,
    replaces: str | None,
    by: str,
    machine_id: str,
    bench: str = "",
) -> dict[str, Any]:
    """A new entry, its id made now so a replay of it is the same entry; ``value`` None or
    empty clears the slot. ``key``: what it is about (:data:`ABOUT`; ``""`` for a lot).
    ``bench``: the station's name, shown in the history; left out when not known (``""`` is
    never "not known", ``data_format.md`` §2)."""
    about = {ABOUT[kind]: key} if kind in ABOUT else {}
    return {
        "entry_id": uuid.uuid4().hex,
        "kind": kind,
        **about,
        FIELD[kind]: value or None,
        "replaces": replaces,
        "by": by,
        "machine_id": machine_id,
        **({"bench": bench} if bench else {}),
        "saved_at": utc_now(),
    }


def is_entry(e: object, kinds: Iterable[str] = tuple(FIELD)) -> bool:
    """An entry of one of ``kinds``, shaped as the rule needs it."""
    if not isinstance(e, dict) or e.get("kind") not in kinds:
        return False
    about = ABOUT.get(e["kind"])
    return isinstance(e.get("entry_id"), str) and (about is None or isinstance(e.get(about), str))


def slot(entry: Mapping[str, Any]) -> Slot:
    about = ABOUT.get(entry["kind"])
    return entry["kind"], entry[about] if about else ""


def value_of(entry: Mapping[str, Any] | None) -> str | None:
    """The entry's text or verdict; None for a clear (and for no entry)."""
    if entry is None:
        return None
    value = entry.get(FIELD[entry["kind"]])
    return value if isinstance(value, str) and value else None


def legacy_entries(
    data: Mapping[str, Any] | None, source: str = "annotations.json", machine_id: str = ""
) -> list[dict[str, Any]]:
    """A whole-file annotation (``annotations.json`` before entries: ``{"text"|"value", "by"}``
    per key, or a plain string in the oldest files) as entries, one per value it holds, none
    replacing anything. Their ids are fixed — ``<source>:<kind>:<key>`` — so a later entry can
    name the one it replaces and a replay of the same whole file is the same entries.
    ``source``: what held the file (the run's ``annotations.json``, or the id of an outbox
    entry queued before entries); ``machine_id``: the bench that saved it, when known."""
    if not isinstance(data, Mapping):
        return []
    at = str(data.get("modified_at") or "")
    found = []
    for kind, section in SECTION.items():
        block = data.get(section)
        if not isinstance(block, Mapping):
            continue
        criteria = block.get("criteria")
        items = [(TEST, block.get(TEST))]
        items += list(criteria.items()) if isinstance(criteria, Mapping) else []
        for key, raw in items:
            if isinstance(raw, Mapping):
                value, by = raw.get(FIELD[kind]), raw.get("by")
            else:
                value, by = raw, ""
            if isinstance(value, str) and value:
                found.append(
                    {
                        "entry_id": f"{source}:{kind}:{key}",
                        "kind": kind,
                        "key": str(key),
                        FIELD[kind]: value,
                        "replaces": None,
                        "by": by if isinstance(by, str) else "",
                        "machine_id": machine_id,
                        "saved_at": at,
                    }
                )
    return found


# ---------------------------------------------------------------------------
# The rule: entries in the order received -> each slot's current one and its history
# ---------------------------------------------------------------------------

CURRENT = "current"
REPLACED = "replaced"  # was current, then replaced by ``other``
NOT_CURRENT = "not_current"  # arrived after ``other`` (current then) without having seen it


@dataclass(frozen=True)
class Line:
    """One entry as its slot's history shows it."""

    entry: dict[str, Any]
    status: str  # CURRENT | REPLACED | NOT_CURRENT
    other: dict[str, Any] | None = None  # who replaced it, or whose it arrived after


def _takes_over(entry: Mapping[str, Any], head: Mapping[str, Any] | None) -> bool:
    """THE rule: an entry becomes current iff it replaces the slot's current entry. A slot
    with none yet takes any entry: there is nothing to keep."""
    return head is None or entry.get("replaces") == head["entry_id"]


def histories(entries: Iterable[Mapping[str, Any]]) -> dict[Slot, list[Line]]:
    """Each slot's history, oldest first, by the rule; an entry id seen before is skipped."""
    heads: dict[Slot, int] = {}
    lines: dict[Slot, list[list[Any]]] = {}
    seen: set[str] = set()
    for e in entries:
        if not is_entry(e) or e["entry_id"] in seen:
            continue
        seen.add(e["entry_id"])
        s = slot(e)
        rows = lines.setdefault(s, [])
        head = rows[heads[s]] if s in heads else None
        if _takes_over(e, head[0] if head else None):
            if head is not None:
                head[1], head[2] = REPLACED, e
            heads[s] = len(rows)
            rows.append([dict(e), CURRENT, None])
        else:
            rows.append([dict(e), NOT_CURRENT, head[0] if head else None])
    return {s: [Line(*row) for row in rows] for s, rows in lines.items()}


def heads(entries: Iterable[Mapping[str, Any]]) -> dict[Slot, dict[str, Any]]:
    """Each slot's current entry (a clear included)."""
    return {
        s: next(line.entry for line in rows if line.status == CURRENT)
        for s, rows in histories(entries).items()
    }


def outcome(before: list[Mapping[str, Any]], entry: Mapping[str, Any]) -> dict[str, Any]:
    """What adding ``entry`` after ``before`` (the run's or the board's entries as received)
    answers: ``{"entry", "status", "current"}`` — ``current`` the slot's current entry once
    it is in."""
    if any(e.get("entry_id") == entry["entry_id"] for e in before):
        status = ALREADY
    else:
        head = heads(before).get(slot(entry))
        status = SAVED if _takes_over(entry, head) else CHANGED
    return {"entry": dict(entry), "status": status, "current": heads([*before, entry])[slot(entry)]}


def current_data(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """What is current, in the run file's own shape (§4.9 ``data``): ``{"version": "2.0",
    "comments": {"test": {"text", "by", "at", "entry_id"}, "criteria": {key: …}},
    "verdict_overrides": {… "value" …}}``, plus every ``entries`` for the history. A cleared
    key is absent."""
    out: dict[str, Any] = {
        "version": "2.0",
        "comments": {"criteria": {}},
        "verdict_overrides": {"criteria": {}},
        "entries": list(entries),
    }
    for (kind, key), e in heads(entries).items():
        value = value_of(e)
        if value is None:
            continue
        shown = {
            FIELD[kind]: value,
            "by": e.get("by", ""),
            "at": e.get("saved_at", ""),
            "entry_id": e["entry_id"],
        }
        section = out[SECTION[kind]]
        if key == TEST:
            section[TEST] = shown
        else:
            section["criteria"][key] = shown
    return out


def board_data(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """What is current of a board (§4.11): ``{"lot": the lot or None, "details": {name:
    value}}``, plus every ``entries`` for the history. A cleared field is absent."""
    out: dict[str, Any] = {"lot": None, "details": {}, "entries": list(entries)}
    for (kind, name), e in heads(entries).items():
        value = value_of(e)
        if value is None:
            continue
        if kind == LOT:
            out["lot"] = value
        else:
            out["details"][name] = value
    return out


def wanted_values(
    comments: Mapping[str, Any], verdict_overrides: Mapping[str, Any]
) -> dict[Slot, str]:
    """The values a save asks for, from the run file's shape (a value a plain string or a
    ``{"text"|"value", …}`` dict), by key; an empty one is no value."""
    found: dict[Slot, str] = {}
    for kind, block in ((COMMENT, comments), (OVERRIDE, verdict_overrides)):
        criteria = block.get("criteria")
        items = [(TEST, block.get(TEST))]
        items += list(criteria.items()) if isinstance(criteria, Mapping) else []
        for key, raw in items:
            value = raw.get(FIELD[kind]) if isinstance(raw, Mapping) else raw
            if isinstance(value, str) and value:
                found[(kind, str(key))] = value
    return found


def changes(
    seen: list[Mapping[str, Any]], wanted: Mapping[Slot, str], by: str, machine_id: str, bench=""
) -> list[dict[str, Any]]:
    """The entries a save adds: one per key whose value differs from what the person saw
    (``seen``: the run's entries when they opened it), each replacing what they saw."""
    before = heads(seen)
    shown = {s: value_of(e) for s, e in before.items() if value_of(e) is not None}
    added = []
    for s in sorted(set(wanted) | set(shown)):
        new = wanted.get(s)
        if new != shown.get(s):
            replaces = before[s]["entry_id"] if s in before else None
            added.append(make_entry(*s, new, replaces, by, machine_id, bench))
    return added


# ---------------------------------------------------------------------------
# What a person reads: times in this PC's local time, who and when, the save's answer
# ---------------------------------------------------------------------------


def _local(saved_at: str) -> datetime | None:
    """*saved_at* in this PC's local time; None for a time that cannot be read or placed (the
    OS cannot convert a year-1 instant: it reads as no time, never stops a save or a push)."""
    try:
        at = datetime.fromisoformat(str(saved_at).replace("Z", "+00:00"))
        return at.astimezone() if at.tzinfo else at  # an older file's time is already local
    except (ValueError, OverflowError, OSError):
        return None


def when_text(saved_at: str, *, short: bool = False, absolute: bool = False, today=None) -> str:
    """``today 09:12`` (``09:12`` when ``short``), ``26 Oct 09:12`` on another day, in this
    PC's local time; ``absolute``: ``26 Oct 2026 09:12`` (a report read on any later day)."""
    at = _local(saved_at)
    if at is None:
        return ""
    clock = at.strftime("%H:%M")
    if absolute:
        return f"{at.day} {at.strftime('%b %Y')} {clock}"
    if at.date() == (today or date.today()):
        return clock if short else f"today {clock}"
    return f"{at.day} {at.strftime('%b')} {clock}"


#: How who saved an entry shows: as recorded, unless the caller names its server's users
#: (``server_lists.namer``: a web server's user by display name).
Shown = Callable[[str], str]


def as_recorded(name: str) -> str:
    """A name as it was recorded."""
    return name


def who_when(
    entry: Mapping[str, Any],
    *,
    short: bool = False,
    absolute: bool = False,
    shown: Shown = as_recorded,
) -> str:
    """``bob · today 09:12``: who saved the entry (*shown*) and when."""
    when = when_text(entry.get("saved_at", ""), short=short, absolute=absolute)
    by = entry.get("by") or ""
    return " · ".join(x for x in (shown(by) if by else "", when) if x)


def who(entry: Mapping[str, Any] | None, shown: Shown = as_recorded) -> str:
    by = (entry or {}).get("by") or ""
    return shown(by) if by else "someone"


def saved_text(outcomes: list[Mapping[str, Any]]) -> str:
    """``Saved (1 comment, 1 verdict).``: what this save made current."""
    counts = {COMMENT: 0, OVERRIDE: 0}
    for o in outcomes:
        if o["status"] == SAVED:
            counts[o["entry"]["kind"]] += 1
    parts = [
        f"{n} {word}{'' if n == 1 else 's'}"
        for n, word in ((counts[COMMENT], "comment"), (counts[OVERRIDE], "verdict"))
        if n
    ]
    return f"Saved ({', '.join(parts)})." if parts else "Saved (nothing changed)."


def changed_text(o: Mapping[str, Any], label: str, shown: Shown = as_recorded) -> str:
    """One entry that arrived after someone else's: ``The verdict of {9} was changed by bob at
    09:12, after you opened this run: bob's PASS stays; your FAIL is kept in its history.``
    (who, as *shown*)."""
    mine, theirs = o["entry"], o["current"]
    name = who(theirs, shown)
    clock = when_text(theirs.get("saved_at", ""), short=True)
    at = f" at {clock}" if clock else ""
    if mine["kind"] == OVERRIDE:
        what = f"The verdict of {label}"
        stays = f"{name}'s {value_of(theirs)} stays" if value_of(theirs) else "the run's own stays"
        kept = f"your {value_of(mine)}" if value_of(mine) else "your clearing"
    else:
        what = f"The comment on {label}"
        stays = f"{name}'s stays" if value_of(theirs) else "it stays cleared"
        kept = "yours" if value_of(mine) else "your clearing"
    return (
        f"{what} was changed by {name}{at}, after you opened this run: "
        f"{stays}; {kept} is kept in its history."
    )


def line_status(line: Line, shown: Shown = as_recorded) -> str:
    """What a history line says of itself (who, as *shown*)."""
    if line.status == CURRENT:
        return "current"
    other = line.other or {}
    if line.status == REPLACED:
        clock = when_text(other.get("saved_at", ""), short=True)
        return f"replaced by {who(other, shown)}'s" + (f" at {clock}" if clock else "")
    return f"saved after {who(other, shown)}'s without seeing it: kept here, not current"
