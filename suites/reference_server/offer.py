"""The calls this server offers (contract §6.1): the core calls every server takes, and the
optional ones it chooses, listed in ``/info``'s ``calls`` (§3.3). An optional call it does not
list answers ``501 unsupported``, kind ``invalid``, as a call it does not have. ``import`` is
listed the same way: a server not listing it grants no login the importer role and answers an
``"import": true`` ``501`` too (:func:`granted`, :data:`IMPORT`).

By default the reference server offers every optional call; U1 (the server's users, §6.8) only
when it is given its users. ``start(..., calls=...)`` names the optional calls a server offers,
so the conformance kit can be run against a server offering some of them only.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable

from . import faults
from .errors import ApiError
from .validate import name_key

#: The calls every server takes (``/info`` besides), by their ids in §6.1.
CORE = (
    "P1", "P2", "P2b", "P7", "P9", "P10",
    "E1", "E3", "E4", "E5", "E7", "E8", "E9", "E10", "S1", "B1", "B2",
)  # fmt: skip
#: The calls a server may offer or not, by their ids in §6.1.
OPTIONAL = (
    "P3", "P4", "P5", "P6", "P8", "P8b", "P11",
    "E2", "E6", "E11", "E12", "E13", "E14", "E15", "E16", "E17",
    "L1", "L2", "L3", "S2", "S3", "S4", "U1", "import",
)  # fmt: skip
#: The id of the right to move data in (§6.7): ``"import": true`` on E1, E6, E7, E10, E15, P10
#: and the deletes of E11 and E12, taken only by a server listing it.
IMPORT = "import"
#: A bench's run pushed (E1 without ``import``): what a server registering its boards refuses
#: on a board it does not list (§3.3). Noted beside the optional calls.
BENCH_RUN = "E1"
#: A project's creation (P11, optional, owner 2026-11-10): what a case needing a fresh project
#: makes whatever the server offers.
CREATE = "P11"
#: The calls a case makes whatever the server offers, kept in its record even when it makes
#: its other optional calls only where they are listed (``offer_aware``).
MADE_ANYWAY = (BENCH_RUN, CREATE)
#: The calls this process's reference servers answered, while someone listens (:func:`note`):
#: the optional ones and a bench's run, in order. None: nobody listens.
NOTED: list[str] | None = None
#: True while a case makes optional calls it makes only when the server offers them: those are
#: not noted (a bench's run still is).
QUIET = False


def note(call_id: str) -> None:
    """Note that a reference server of this process answered *call_id* (an optional call, or
    :data:`BENCH_RUN`), for the kit's record of the calls each case makes."""
    noted = NOTED
    if noted is None:
        return
    if call_id == BENCH_RUN or (call_id in OPTIONAL and not QUIET):
        noted.append(call_id)


def chosen(calls: Iterable[str] | None, users: list[dict] | None) -> frozenset[str]:
    """The optional calls a server offers: *calls*, or every one (U1 only with *users*)."""
    if calls is None:
        return frozenset(c for c in OPTIONAL if c != "U1" or users is not None)
    offered = frozenset(calls)
    unknown = sorted(offered - set(OPTIONAL))
    if unknown:
        raise ValueError(f"not optional calls of the contract: {unknown}")
    if "U1" in offered and users is None:
        raise ValueError("U1 lists the server's users: give it users")
    return offered


def check_users(users: list[dict] | None) -> list[dict] | None:
    """The users U1 answers, each ``{"name", "display_name"}`` (``display_name``: the name when
    none is given)."""
    if users is None:
        return None
    found = []
    for user in users:
        name = user["name"] if isinstance(user, dict) else str(user)
        shown = user.get("display_name") if isinstance(user, dict) else None
        if not name:
            raise ValueError("a user's name is never empty")
        found.append({"name": name, "display_name": shown or name})
    return found


def refuse_unoffered(offered: frozenset[str], call_id: str) -> None:
    """``501 unsupported`` for an optional call this server does not list (§6.1)."""
    if call_id in ("S1", "P2b") and faults.on("6.1.core-calls"):
        raise ApiError(501, "unsupported", "invalid", f"This server does not take {call_id}.")
    if call_id not in OPTIONAL or call_id in offered:
        return
    if faults.on("6.1.unlisted-501"):
        return  # served as if listed
    if call_id == CREATE and faults.on("6.1.create-unlisted-501"):
        return  # a project created where none may be
    raise ApiError(
        501,
        "unsupported",
        "invalid",
        f"This server does not offer {call_id}: /info's calls does not list it.",
    )


def refuse_import(offered: frozenset[str], call_id: str) -> None:
    """An ``"import": true`` on *call_id* (§6.1, §6.7): ``501 unsupported``, as an optional call
    not listed, where the server does not list ``import``."""
    if IMPORT in offered or _import_served(call_id):
        return
    refuse_unoffered(offered, IMPORT)


def _import_served(call_id: str) -> bool:
    """The fault of 6.1.import-unlisted-501: *call_id* takes an import as if ``import`` were
    listed. Only the call ``faults.CALL_ENV`` names, else every call but E1 (a server that
    refuses an imported run alone)."""
    if not faults.on("6.1.import-unlisted-501"):
        return False
    only = faults.only_call()
    return call_id == only if only else call_id != "E1"


def granted(offered: frozenset[str], user):
    """*user* (a ``state.User``) as this server knows it: without the importer role where the
    server does not list ``import`` (§3.5, §6.1)."""
    kept = faults.on("3.5.importer-only-with-import")  # the fault: the role kept
    if IMPORT in offered or "importer" not in user.roles or kept:
        return user
    return dataclasses.replace(user, roles=user.roles - {"importer"})


def listed(offered: frozenset[str]) -> list[str]:
    """``/info``'s ``calls``: the optional calls offered, in §6.1's order."""
    calls = [c for c in OPTIONAL if c in offered]
    if faults.on("3.3.info-calls"):
        calls.append("E1")  # a core call listed
    return calls


def users_answer(users: list[dict]) -> dict:
    """U1: the users, by display name then name, each by case folding (§6.8)."""
    if faults.on("U1.users"):
        return {"users": sorted(users, key=lambda u: u["name"])}

    def order(user: dict) -> tuple[str, str, str]:
        return name_key(user["display_name"]), name_key(user["name"]), user["name"]

    return {"users": sorted(users, key=order)}


def known_operator(
    offered: frozenset[str],
    users: list[dict] | None,
    name: object,
    what: str,
    *,
    unnamed_refused: bool = False,
) -> None:
    """With U1: *name* (a run's ``operator``, a release's ``released_by``: *what*) is one of the
    users', by case folding, else ``409 unknown_operator`` (§6.8). Absent: the server's choice,
    taken unless *unnamed_refused*, then refused as an unknown name."""
    if "U1" not in offered or users is None or faults.on("U1.operator-known"):
        return
    if name is None and faults.on("U1.unnamed-choice"):  # the fault: refused, kind denied
        raise ApiError(409, "unknown_operator", "denied", f"This server needs the {what}.")
    if name is None and not unnamed_refused:
        return
    key = name_key(name) if isinstance(name, str) else None
    if key is not None and any(name_key(u["name"]) == key for u in users):
        return
    raise ApiError(
        409,
        "unknown_operator",
        "invalid",
        f"{name!r} is none of this server's users: pick the {what} from its list.",
    )
