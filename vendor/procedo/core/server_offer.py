"""What a web server offers of the contract's optional calls (contract §6.1), and what the apps
grey on a server that does not offer a feature (the approved mock-up *Server lists*, section 4).

A server lists the optional calls it offers in ``/info``'s ``calls`` (§3.3); one it does not list
answers ``501``. The app offers a feature only when the server lists every call of its group
(:data:`SEARCH`, :data:`SET_CURRENT`, :data:`DELETE` (the same as :data:`TRASH`),
:data:`LIBRARY`, :data:`BOARD_ENTRIES`), and **sends an optional call only to a server listing it**:

* **What each server offers** is noted here, in memory, by the server's identity, each time its
  ``/info`` is read (:func:`note`, from ``ApiWire.info``), and from what this PC kept of it the
  last time (``server_lists.kept``). Asked on the GUI thread from memory alone (:func:`offered`,
  :func:`lacks`): ``None`` while it is not known yet, and then every feature shows as today.
* **The guard** (:func:`refused`): ``ApiWire.call`` asks it before every call; an optional call
  the server's ``/info`` (read on that wire) does not list is refused there, nothing sent, in the
  feature's own words (:data:`REASONS`), with the code a server's ``501`` carries
  (``unsupported``), so a caller that handles the server's refusal handles this one.
* **The rule on screen: grey, never hide**, the reason naming what the server does not offer.

Every other storage (a folder, a share, FTPS, SFTP, the database) offers every feature. Qt-free.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

#: The web server's store id (``evidence_store_api.ApiStore.ID``): the one storage with an offer.
WEB_SERVER = "http"

#: Each feature's calls (§6.1's table): the app offers it only when the server lists them all.
SEARCH = ("E2",)
SET_CURRENT = ("E6",)
#: Delete and trash are one group: a delete is offered only where its trash can be opened.
DELETE = TRASH = ("E2", "E11", "E12", "E13")
LIBRARY = ("L1", "L2", "L3")
BOARD_ENTRIES = ("E15",)
#: What Search Results' fields propose beside E4 (a core call): tests, benches, operators,
#: readings.
TESTS, BENCHES, OPERATORS, READINGS = "P4", "E14", "E16", "E17"

#: What a greyed entry point says (the approved words).
NO_SEARCH = "{server} does not offer search"
NO_BENCHES = "{server} does not list its benches."
NO_SET_CURRENT = "This server does not let Bench choose the current run."
NO_DELETE = "This server does not offer deleting results."
NO_TRASH = "This server has no trash."
NO_LIBRARY = "This server does not offer a library."
NO_BOARD_ENTRIES = "This server does not keep board fields: recorded with this run."

#: What the guard says of a call it does not send, by its id: the feature's words.
REASONS: dict[str, str] = {
    "E2": "This server does not offer search.",
    "E6": NO_SET_CURRENT,
    "E11": NO_DELETE,
    "E12": NO_DELETE,
    "E13": NO_TRASH,
    "E14": "This server does not list its benches.",
    "E15": "This server does not keep board fields.",
    "L1": NO_LIBRARY,
    "L2": NO_LIBRARY,
    "L3": NO_LIBRARY,
}
#: The code a refused call carries: the one a server's ``501`` carries (§4.4).
UNSUPPORTED = "unsupported"

#: The optional calls by the operationId the app sends them with (``procedo_api.openapi.yaml``).
#: P2 with ``since`` is P3, P4 with ``names`` is P5 (:func:`call_id`).
OPTIONAL_OPS: dict[str, str] = {
    "listTests": "P4",
    "readTests": "P6",
    "listReleases": "P8",
    "getRelease": "P8b",
    "readRelease": "P8b",
    "searchRuns": "E2",
    "setCurrent": "E6",
    "deleteRun": "E11",
    "restoreRun": "E11",
    "deleteArtifact": "E12",
    "restoreArtifact": "E12",
    "purge": "E13",
    "listBenches": "E14",
    "saveBoardEntries": "E15",
    "listOperators": "E16",
    "listReadings": "E17",
    "listLibrary": "L1",
    "getLibraryItem": "L2",
    "readLibraryItem": "L2",
    "publishLibraryItem": "L3",
    "declareStorage": "S2",
    "listBlobsToDelete": "S3",
    "reportBlobsDeleted": "S4",
    "listUsers": "U1",
}

#: What each server offers, by its identity, as last read on this PC (in memory).
_offers: dict[str, frozenset[str]] = {}


def note(identity: str, calls: Iterable[str]) -> None:
    """The server *identity* offers *calls* (its ``/info``'s, read now or kept on this PC)."""
    _offers[identity] = frozenset(calls)


def offered(identity: str) -> frozenset[str] | None:
    """The optional calls the server *identity* offers; ``None`` while not known. From memory."""
    return _offers.get(identity)


def of_store(store: Any) -> frozenset[str] | None:
    """What *store*'s server offers; ``None`` for a store that is no web server (it offers every
    feature) or one whose offer is not known yet."""
    if store is None or getattr(store, "ID", "") != WEB_SERVER:
        return None
    return offered(str(store.io.identity))


def lacks(store: Any, group: Iterable[str]) -> bool:
    """Whether *store*'s server does not offer the feature *group*: known, and one of its calls
    missing. A store that is no web server, or whose offer is not known yet, lacks nothing."""
    calls = of_store(store)
    return calls is not None and not set(group) <= calls


def lacks_on(identity: str, group: Iterable[str]) -> bool:
    """:func:`lacks` for the web server *identity*."""
    calls = offered(identity)
    return calls is not None and not set(group) <= calls


def call_id(op: str, query: Mapping[str, Any] | None = None) -> str:
    """The optional call's id that *op* (an operationId) with *query* is; ``""`` for a core
    call."""
    if op in ("getProject", "readProject"):
        return "P3" if query and "since" in query else ""
    if op == "listTests" and query and "names" in query:
        return "P5"
    return OPTIONAL_OPS.get(op, "")


def refused(op: str, query: Mapping[str, Any] | None, body: Any, calls: frozenset[str]) -> str:
    """Why the call *op* is not sent to a server offering *calls*: its feature's words, ``""``
    when it is sent (a core call, or one listed). ``readProject`` names ``since`` in its body."""
    asked = dict(query or {})
    if isinstance(body, dict) and "since" in body:
        asked["since"] = body["since"]
    found = call_id(op, asked)
    if not found or found in calls:
        return ""
    return REASONS.get(found, f"This server does not offer {found}.")
