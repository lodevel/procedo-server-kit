"""Every action of the evidence store, and the API call that performs it on a server.

The owner (``docs/plans/company_storage_design.md`` §6c): the store interface is the complete
list of what can be done with stored evidence, and the API is derived from that list.
:data:`ACTIONS` maps each method of :class:`evidence_store.EvidenceStore` either to the calls of
``docs/plans/procedo_api.openapi.yaml`` (their ``operationId``) that perform it, or to LOCAL,
with why: a mechanic of this PC that never reaches a server. What of an action no call performs
is written beside it (``missing``): a gap in the contract, closed in the contract.
:data:`API_ONLY` is the other direction: the evidence and storage calls that perform no store
action, each with the decision taken (``missing`` again for a call whose action no interface
holds yet). :data:`DISCOVERY` holds the one call that sits outside the map, and why.

``tests/test_evidence_actions.py`` holds the table to the interface and to the contract, and
``tests/test_evidence_conformance.py`` runs every action that is not LOCAL on every storage.
Data only, Qt-free.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass

from .evidence_store import EvidenceStore


@dataclass(frozen=True)
class Action:
    """How one store action reaches a server: the calls that perform it, or why none does."""

    calls: tuple[str, ...] = ()  # the contract's operationIds
    local: str = ""  # LOCAL: why it never reaches a server
    missing: str = ""  # what of it no call performs: a gap in the contract


def api(*calls: str, missing: str = "") -> Action:
    return Action(calls=calls, missing=missing)


def local(why: str) -> Action:
    return Action(local=why)


def protocol_actions() -> list[str]:
    """The methods :class:`evidence_store.EvidenceStore` declares, in its order."""
    return [
        name
        for name, value in vars(EvidenceStore).items()
        if inspect.isfunction(value) and not name.startswith("_")
    ]


ACTIONS: dict[str, Action] = {
    "latency_class": local("where this PC's configured root is: how soon a view says it loads"),
    "check_access": local("this PC probes its own reach and rights on the store"),
    "sign_in": local("this PC proves a credential it holds; getInfo answers the login, not data"),
    "list_boards": api("listBoards"),
    "list_runs": api("getBoard", "readBoard"),
    "current_run": api("getBoard", "readBoard"),
    # A run named ``current`` (CURRENT_RUN) is the one its test's pointer names: E5 finds it.
    "read_run": api("getRun", "readRun", "getBoard"),
    # A push puts the bytes in the storage new files go to, which S1 names (data_format.md
    # §3.5): read once per store, so a push may make it before its own call.
    "write_run": api("listStorages", "pushRun"),
    "stage": local("a test script writes its run into this PC's outbox; nothing is sent yet"),
    "discard_run": local("a staged run that never started: nothing of it left this PC"),
    "commit_run": api("listStorages", "pushRun"),
    # A run named for a delete or a comment is read first (E3; ``current`` through E5): it must
    # be that board's and that test id's, deleted or not, before anything is sent.
    "delete_run": api("deleteRun", "getRun", "readRun", "getBoard"),
    "promote_run": api("setCurrent"),
    "demote_run": api("setCurrent"),
    # E8 leaves out each artifact's record (``info``): a report's scenario is read with E9.
    "list_artifacts": api("listArtifacts", "getArtifact"),
    "read_artifact": api("getArtifact"),
    "write_artifact": api("listStorages", "pushArtifact"),
    "delete_artifact": api("deleteArtifact"),
    # E8 generations=all&include=info lists each generation with its record; E9 generation=n.
    "list_generations": api("listArtifacts"),
    "read_generation": api("getArtifact"),
    # The trash as lists show deleted items: E2 and E8 with deleted=only, each with its
    # deleted_* fields (the reason, the bench, whether the run was current).
    "list_trash": api("searchRuns", "listArtifacts"),
    # A search (data_format.md §6.2): runs by E2, reports by E8 (kind=report, include=info),
    # the server filtering, each row then decided here as on every storage.
    "search": api("searchRuns", "listArtifacts"),
    # What the search's fields propose: the boards and their part numbers by E4, the project's
    # tests by P4, the benches the server has seen by E14, the operators its runs recorded by
    # E16; a test's readings by E17.
    "search_choices": api("listBoards", "listTests", "listBenches", "listOperators"),
    "search_readings": api("listReadings"),
    # A run current when deleted is current again by the pointer rule (data_format.md §6.7).
    "restore_trash": api("restoreRun", "restoreArtifact"),
    # E13 with items purges chosen deleted items now.
    "purge_trash": api("purge"),
    "read_run_annotation": api("getRun", "readRun", "getBoard"),
    "add_run_annotation_entries": api("saveAnnotation", "getRun", "readRun", "getBoard"),
    "read_board_entries": api("getBoard", "readBoard"),
    "add_board_entries": api("saveBoardEntries"),
    "local_path": local("this PC's copy of a stored file, for open(); the bytes come from E3/E9"),
    "sweep_incomplete": local(
        "removes what a dead writer half-wrote: in this PC's outbox, and in a folder layout's "
        "tree; a push to the API is all or nothing"
    ),
    "pending_items": local("what this PC's outbox still holds"),
    "quarantined_items": local("what this PC's outbox parked for a person"),
    "flush_outbox": local("sends what this PC's outbox holds, each item by its action's call"),
}


@dataclass(frozen=True)
class ApiOnly:
    """An evidence or storage call that performs no store action, and why."""

    why: str
    missing: bool = False  # True: the action belongs in an interface none holds yet


API_ONLY: dict[str, ApiOnly] = {
    "declareStorage": ApiOnly(
        "a project's own storage declared to the server: the BlobStore interface "
        "(company_storage_design.md §6)",
        missing=True,
    ),
    "listBlobsToDelete": ApiOnly(
        "blobs nothing references in a declared storage: the BlobStore interface (§6)",
        missing=True,
    ),
    "reportBlobsDeleted": ApiOnly(
        "blobs the app deleted from a declared storage: the BlobStore interface (§6)",
        missing=True,
    ),
}

#: The calls outside the map: discovery, which performs no action and comes before the first
#: call of every one, once per store. Not mapped to every action (it would name a call that
#: performs none of them), nor API-only (it is not an evidence call: ``web_api_spec.md`` §3.1).
DISCOVERY: dict[str, str] = {
    "getInfo": (
        "the API's major versions the server serves, its limits and this login's roles: read "
        "once, before a store's first call, so a server this app must not read is refused "
        "rather than read as empty"
    ),
}
