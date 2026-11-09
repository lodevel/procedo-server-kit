"""Every action on projects, the library and their bytes, and the API calls that perform it.

The twin of :mod:`evidence_actions` for the interfaces of :mod:`project_store`
(``docs/plans/company_storage_design.md`` §6c: the interface is the complete list of what can be
done, and the API is derived from it). :data:`ACTIONS` maps each method of
:class:`project_store.ProjectSource`, :class:`project_store.LibrarySource` and
:class:`project_store.BlobStore` to the calls of ``docs/plans/procedo_api.openapi.yaml`` (their
``operationId``) that perform it: every action reaches the server, none is LOCAL. Every project, library and large-file
call of the contract (``P1``–``P11``, L1–L3, B1–B2) is named by an action; discovery
(``getInfo``) stays outside the map, as for the evidence (:data:`evidence_actions.DISCOVERY`).

What a call performs that a folder storage does differently is said beside it (``note``): the
same action, read back the same on every storage (``tests/test_project_conformance.py``).

``tests/test_project_actions.py`` holds the table to the interfaces and to the contract. Data
only, Qt-free.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass

from .project_store import BlobStore, LibrarySource, ProjectSource

#: The interfaces whose methods are actions, by the name the table keys them under.
INTERFACES = {"project": ProjectSource, "library": LibrarySource, "blob": BlobStore}


@dataclass(frozen=True)
class Action:
    """How one action reaches the JSON API: the calls that perform it."""

    calls: tuple[str, ...]  # the contract's operationIds
    note: str = ""  # how a folder storage does the same


def api(*calls: str, note: str = "") -> Action:
    return Action(calls=calls, note=note)


def protocol_actions() -> list[str]:
    """``<interface>.<method>`` for every method the three interfaces declare, in their order."""
    return [
        f"{prefix}.{name}"
        for prefix, protocol in INTERFACES.items()
        for name, value in vars(protocol).items()
        if inspect.isfunction(value) and not name.startswith("_")
    ]


#: A binary file's bytes travel as a part of the JSON when they are at most 1 MiB, alone above
#: it (B1 before the write, B2 after the read: ``data_format.md`` §3.4b); a write first reads
#: where new files go (S1, ``listStorages``, once per source).
_WRITES_FILES = ("listStorages", "uploadBlob")
_READS_FILES = ("downloadBlob",)

ACTIONS: dict[str, Action] = {
    "project.list_projects": api(
        "listProjects", note="each folder under the storage's root that holds config/config.json"
    ),
    "project.create_project": api("createProject", *_WRITES_FILES),
    # P2, or its twin P2b when ``have`` is too long for a URL.
    "project.read_project": api("getProject", "readProject", *_READS_FILES),
    "project.changes_since": api(
        "getProject",
        "readProject",
        *_READS_FILES,
        note="the token holds each file's stamp; a file whose stamp moved is read again",
    ),
    "project.list_tests": api("listTests"),
    # P7 for one name, P5 for several, P6 when the names are too long for a URL.
    "project.read_tests": api("getTest", "readTest", "listTests", "readTests", *_READS_FILES),
    "project.list_releases": api("listReleases", note="releases/<test_id>/v<N>.json"),
    "project.read_release": api("getRelease", "readRelease", *_READS_FILES),
    "project.push_changes": api(
        "pushChanges",
        *_WRITES_FILES,
        note="a draft is the files themselves there (test_release.state): none is kept; the "
        "rules of a test's id (test.json) checked change by change, as the server does",
    ),
    "project.rename_test": api(
        "pushChanges",
        note="the folder tests/<old> renamed, its test.json with it: its releases and its "
        "evidence stay under its id, nothing else moves",
    ),
    "project.release": api(
        "release",
        note="the test whose test.json holds the id found; releases/<test_id>/v<N>.json created, "
        "never replaced",
    ),
    "project.import_release": api(
        "release",
        *_WRITES_FILES,
        note="releases/<test_id>/v<N>.json written as it is, never over another; P10 with "
        '"import": true and the documents whole (an importer login, data_format.md §6.10)',
    ),
    "project.fetch": api(*_READS_FILES, note="the file at its path"),
    "library.list": api("listLibrary"),
    "library.download": api("getLibraryItem", "readLibraryItem", *_READS_FILES),
    "library.publish": api("publishLibraryItem", *_WRITES_FILES),
    "blob.upload": api("uploadBlob"),
    "blob.download": api("downloadBlob"),
}
