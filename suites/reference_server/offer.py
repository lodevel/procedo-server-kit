"""The calls this server offers (contract §6.1): the core calls every server takes, and the
optional ones it chooses, listed in ``/info``'s ``calls`` (§3.3). An optional call it does not
list answers ``501 unsupported``, kind ``invalid``, as a call it does not have.

By default the reference server offers every optional call; U1 (the server's users, §6.8) only
when it is given its users. ``start(..., calls=...)`` names the optional calls a server offers,
so the conformance kit can be run against a server offering some of them only.
"""

from __future__ import annotations

from collections.abc import Iterable

from . import faults
from .errors import ApiError
from .validate import name_key

#: The calls every server takes (``/info`` besides), by their ids in §6.1.
CORE = (
    "P1", "P2", "P2b", "P7", "P9", "P10", "P11",
    "E1", "E3", "E4", "E5", "E7", "E8", "E9", "E10", "S1", "B1", "B2",
)  # fmt: skip
#: The calls a server may offer or not, by their ids in §6.1.
OPTIONAL = (
    "P3", "P4", "P5", "P6", "P8", "P8b",
    "E2", "E6", "E11", "E12", "E13", "E14", "E15", "E16", "E17",
    "L1", "L2", "L3", "S2", "S3", "S4", "U1",
)  # fmt: skip
#: A bench's run pushed (E1 without ``import``): what a server registering its boards refuses
#: on a board it does not list (§3.3). Noted beside the optional calls.
BENCH_RUN = "E1"
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
    raise ApiError(
        501,
        "unsupported",
        "invalid",
        f"This server does not offer {call_id}: /info's calls does not list it.",
    )


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
    offered: frozenset[str], users: list[dict] | None, name: object, what: str
) -> None:
    """With U1: *name* (a run's ``operator``, a release's ``released_by``: *what*) is one of the
    users', by case folding, else ``409 unknown_operator`` (§6.8). Absent: nothing to check."""
    if "U1" not in offered or users is None or name is None or faults.on("U1.operator-known"):
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
