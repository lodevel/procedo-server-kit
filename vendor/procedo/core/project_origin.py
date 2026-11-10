"""Where a project that lives on a SERVER was checked out from, and what was synced.

A project on an FTPS server is opened through a **working copy**: an ordinary
project folder on the local disk (``project_local_state.working_copy_dir``), which
the runner, the editor, the bundle install and the configs all use unchanged. This
module holds the one record that says where that folder came from — the endpoint,
the base path on the server, the backend parameters needed to reconnect — and the
**manifest**: for every synced file, its size and mtime ON THE SERVER and its size
and mtime LOCALLY, as of the last sync. The planner
(:mod:`project_sync_plan`) decides everything from those four numbers.

**The record lives in machine-local state, never in the project folder**, which
would push it to the server on the next save and hand every other bench this
bench's view of the world. It is written through :mod:`atomic_write`, because the
pass that updates it is the pass that is deleting files.

**No credential is ever in it.** The reconnect parameters are the backend's
declared ``PARAMS``, which by contract name no secret (``evidence_store.open_store``);
the password is looked up by endpoint identity from :mod:`evidence_credentials`,
injected into the tree as a callable exactly as the evidence store does it.

**The chicken and the egg.** The state folder is keyed by ``project_id``, which
lives in the project's ``config/config.json`` — on the server. So a first checkout
fetches that one file by hand (:data:`CONFIG_FILE`), learns the id, resolves the
working copy, lands the file and records its row; everything after that is an
ordinary sync pass.

Qt-free, stdlib only.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import atomic_write, project_local_state
from .evidence_store import EvidenceError
from .server_certificate import without_choice  # this PC's, never a record's

log = logging.getLogger(__name__)

RECORD_NAME = "origin.json"
SCHEMA_VERSION = 2
#: The one file a first checkout fetches before it knows where to put anything.
CONFIG_FILE = "config/config.json"


def folder_key(path: Path | str) -> str:
    """One folder's identity as a comparison key: resolved, case-folded.

    The same rule the pass lock uses, and for the same reason: one folder has
    several spellings (an 8.3 short name, a junction, a drive mapping) and two of
    them must never read as two folders.
    """
    return os.path.normcase(os.path.realpath(path))


@dataclass(frozen=True)
class Stamp:
    """One side's state of a file — the whole of what "changed" means here.

    The planner only ever tests a stamp for EQUALITY: a server time is only compared
    with a server time and a local one with a local one, never across machines.

    ``version`` is the storage's own "these are still the same bytes" token: the
    ``sha256:`` of the bytes on a server that versions its files (the web server, whose
    stamps carry no time). FTPS and a filesystem have nothing of the sort and leave it
    ``""``: they compare on ``(size, mtime)``.
    """

    size: int
    mtime: float
    version: str = ""


@dataclass(frozen=True)
class Row:
    """What one synced file looked like on BOTH sides at the end of the last sync, and
    the MD5 of the bytes that landed when a download streamed it (``""`` otherwise): the
    checksum a reader of a large file (the board archive) takes from here instead of
    reading the whole file again."""

    server: Stamp
    local: Stamp
    md5: str = ""


@dataclass
class Origin:
    """The working copy's origin record: where it came from, and the manifest."""

    backend: str  # "ftps" | "filesystem" (a filesystem tree is what the tests drive)
    identity: str  # the tree's stable printable key; never holds a secret
    base: str  # the project's base path on the server, as the server spells it
    params: dict[str, Any]  # what rebuilds the tree — the backend's declared params
    project_id: str
    #: WHICH FOLDER this record describes (:func:`folder_key`). The record is found
    #: by ``project_id``, and one project_id in several folders is ordinary here —
    #: an Explorer copy of a project off the share carries it, and so does a restored
    #: backup. Without this field every one of those folders answered "I am the
    #: working copy": opening the copy pushed its private files onto the shared
    #: server, and *Forget this working copy* ``rmtree``d a plain project folder.
    working_copy: str = ""
    files: dict[str, Row] = field(default_factory=dict)  # relative POSIX path -> row
    #: The manifest is not a picture of the server until a pull has finished. Set
    #: when one starts (and when the manifest had to be thrown away), cleared when
    #: one completes; ``project_sync.push`` refuses while it is set, because an
    #: upload planned against a stale manifest is a blind overwrite.
    needs_pull: bool = False
    #: The files nothing may act on until somebody answers (``project_sync.resolve``),
    #: as the last pass that met them saw them. A pull replaces the set with what it
    #: found; a push adds what it meets and sends none of them; offline status counts
    #: them; an answer is checked against the copies recorded here.
    conflicts: dict[str, Conflict] = field(default_factory=dict)
    #: A storage read by what changed (the JSON API, ``project_sync_api``): the change
    #: token the next read starts from (``since``), every file it held at the last read
    #: (synced or not: a file left for later is still there to bring), the storage the bytes
    #: of each of their versions are in (``stores``: a binary file's, by version, where a
    #: download reads them), and the bundle's version there and the stamp of this PC's
    #: ``bundle/`` folder as of the last sync. Empty for a tree, which is listed whole.
    since: str = ""
    listing: dict[str, Stamp] = field(default_factory=dict)
    stores: dict[str, int] = field(default_factory=dict)
    #: The tests whose releases a read brought and this PC could not write (a name its file
    #: system refuses, a full disk), with why: said at every pass until a read brings them
    #: again and they are written (``project_sync_api``).
    releases_failed: dict[str, str] = field(default_factory=dict)
    bundle_server: str = ""
    bundle_local: str = ""


@dataclass(frozen=True)
class Conflict:
    """A file nothing may act on until somebody says which copy is kept: changed on both
    sides, new on both, or changed on one and deleted on the other (*kind*:
    ``project_sync_plan.EDITED_EDITED``…). *local* and *server* are the stamps the pass
    saw (``None``: not there)."""

    path: str
    kind: str
    local: Stamp | None
    server: Stamp | None


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------


def _stamp(raw: Any) -> Stamp | None:
    if not isinstance(raw, list) or len(raw) != 3:
        return None
    try:
        return Stamp(int(raw[0]), float(raw[1]), str(raw[2]))
    except (TypeError, ValueError):
        return None


def _raw(stamp: Stamp) -> list[Any]:
    return [stamp.size, stamp.mtime, stamp.version]


def to_json(origin: Origin) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "backend": origin.backend,
        "identity": origin.identity,
        "base": origin.base,
        "params": without_choice(origin.params),
        "project_id": origin.project_id,
        "working_copy": origin.working_copy,
        "needs_pull": origin.needs_pull,
        "conflicts": {
            path: {
                "kind": c.kind,
                "local": None if c.local is None else _raw(c.local),
                "server": None if c.server is None else _raw(c.server),
            }
            for path, c in sorted(origin.conflicts.items())
        },
        "files": {
            path: {
                "server": [row.server.size, row.server.mtime, row.server.version],
                "local": [row.local.size, row.local.mtime, row.local.version],
                **({"md5": row.md5} if row.md5 else {}),
            }
            for path, row in sorted(origin.files.items())
        },
        "since": origin.since,
        "listing": {path: _raw(stamp) for path, stamp in sorted(origin.listing.items())},
        "stores": dict(sorted(origin.stores.items())),
        "releases_failed": dict(sorted(origin.releases_failed.items())),
        "bundle": {"server": origin.bundle_server, "local": origin.bundle_local},
    }


def from_json(data: dict[str, Any]) -> Origin:
    """The record back.

    **One unreadable row makes the whole MANIFEST unreadable.** Dropping just that
    row was worse than dropping the file: the row's own file then read as "not on
    the server" to a push, which uploaded a two-day-old local copy over a
    minute-old remote one without the both-changed row ever being reached. A
    manifest that is not whole is not a picture of the server, so it is thrown away
    and the record asks for a pull — which settles every file by its bytes, or asks.

    The connection fields are kept whatever happens to the rows: they are how the
    server is reached at all, and losing them would strand the working copy.

    **A record that does not name its folder is not a record.** It is the field
    that says which of the folders holding this ``project_id`` is the working copy,
    and a record without it would be believed by all of them — which is the defect
    it was added to close. There is no compatibility path: a record from before it
    reads as unreadable and the project is checked out again.
    """
    files: dict[str, Row] = {}
    unreadable = False
    raw_files = data.get("files")
    for path, raw in (raw_files if isinstance(raw_files, dict) else {}).items():
        server = _stamp(raw.get("server")) if isinstance(raw, dict) else None
        local = _stamp(raw.get("local")) if isinstance(raw, dict) else None
        if server is None or local is None:
            log.error(
                "origin record: the manifest row for %s cannot be read, so the whole "
                "manifest is discarded and a pull is owed before anything is pushed",
                path,
            )
            unreadable = True
            break
        files[str(path)] = Row(server, local, str(raw.get("md5") or ""))
    params = data.get("params")
    folder = str(data.get("working_copy") or "")
    if not folder:
        log.error("origin record: it does not name the folder it describes; it is not a record")
        raise EvidenceError(
            "corrupt",
            "the origin record does not name the working copy it describes; check the "
            "project out again",
        )
    return Origin(
        backend=str(data.get("backend") or ""),
        identity=str(data.get("identity") or ""),
        base=str(data.get("base") or ""),
        params=without_choice(params) if isinstance(params, dict) else {},
        project_id=str(data.get("project_id") or ""),
        working_copy=folder,
        files={} if unreadable else files,
        needs_pull=unreadable or bool(data.get("needs_pull")),
        conflicts=_conflicts(data.get("conflicts")),
        **_read_by_changes(data, unreadable),
    )


def _read_by_changes(data: dict[str, Any], unreadable: bool) -> dict[str, Any]:
    """The token, the listing and the bundle's versions back; all dropped with an unreadable
    manifest or listing, so the next read is a whole one."""
    listing: dict[str, Stamp] = {}
    raw = data.get("listing")
    for path, item in (raw if isinstance(raw, dict) else {}).items():
        stamp = _stamp(item)
        if stamp is None:
            unreadable = True
            break
        listing[str(path)] = stamp
    raw_stores = data.get("stores")
    stores = {
        str(version): store
        for version, store in (raw_stores if isinstance(raw_stores, dict) else {}).items()
        if isinstance(store, int) and not isinstance(store, bool)
    }
    raw_failed = data.get("releases_failed")
    failed = {
        str(test): str(why)
        for test, why in (raw_failed if isinstance(raw_failed, dict) else {}).items()
    }
    bundle = data.get("bundle") if isinstance(data.get("bundle"), dict) else {}
    if unreadable:
        return {}
    return {
        "since": str(data.get("since") or ""),
        "listing": listing,
        "stores": stores,
        "releases_failed": failed,
        "bundle_server": str(bundle.get("server") or ""),
        "bundle_local": str(bundle.get("local") or ""),
    }


def _conflicts(raw: Any) -> dict[str, Conflict]:
    """The recorded conflicts; one that cannot be read is dropped: the next pull meets it
    again, and an answer to it is refused meanwhile ("not in conflict")."""
    found: dict[str, Conflict] = {}
    for path, item in (raw if isinstance(raw, dict) else {}).items():
        if not isinstance(item, dict) or not isinstance(item.get("kind"), str):
            continue
        local, server = item.get("local"), item.get("server")
        local_stamp = None if local is None else _stamp(local)
        server_stamp = None if server is None else _stamp(server)
        if (local is not None and local_stamp is None) or (
            server is not None and server_stamp is None
        ):
            continue
        found[str(path)] = Conflict(str(path), item["kind"], local_stamp, server_stamp)
    return found


# ---------------------------------------------------------------------------
# Where it lives
# ---------------------------------------------------------------------------


def record_path(working_copy: Path, *, create: bool = False) -> Path | None:
    """The record's file, or ``None`` when this machine has no state folder for the
    project (which, before its ``config/config.json`` has landed, it has not)."""
    state = (
        project_local_state.state_dir(working_copy)
        if create
        else project_local_state.existing_state_dir(working_copy)
    )
    return None if state is None else state / RECORD_NAME


def load(working_copy: Path) -> Origin | None:
    """The record for the project in *working_copy*, or ``None`` when there is none.

    ``None`` means "not a checkout of a server project" to every caller, which is
    also the honest answer for a record that cannot be parsed: nothing is known
    about what was last synced, and the next pass compares every file again.

    **It does not say that THIS folder is the working copy** — the record is found
    by ``project_id`` and several folders can hold one. :func:`describes` is that
    question, and every caller that is about to write, delete or sync asks it.
    """
    path = record_path(working_copy)
    return read_record(path) if path is not None else None


def read_record(path: Path) -> Origin | None:
    """The record in the file *path* (:func:`load`'s reading, for a caller that found the file
    before its folder moved), ``None`` when it is absent or unreadable."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        return from_json(data)
    except EvidenceError:
        return None  # a record that names no folder is no record at all


def landed_md5(working_copy: Path, path: Path) -> str | None:
    """The MD5 a download recorded for *path* when it landed in *working_copy*, while the
    file is still exactly what landed; ``None`` when this is no working copy of this
    folder, the file has no such row, or it changed since (it must be read again)."""
    origin = load(working_copy)
    if origin is None or not describes(origin, working_copy):
        return None
    try:
        rel = Path(path).resolve().relative_to(Path(working_copy).resolve()).as_posix()
        here = Path(path).stat()
    except (OSError, ValueError):
        return None
    row = origin.files.get(rel)
    if row is None or not row.md5 or row.local != Stamp(here.st_size, here.st_mtime):
        return None
    return row.md5


def describes(origin: Origin, folder: Path | str) -> bool:
    """Whether *folder* is the very folder this record was written for."""
    return bool(origin.working_copy) and origin.working_copy == folder_key(folder)


def save(origin: Origin, working_copy: Path) -> None:
    """Write the record, atomically. Raises when the project has no id to key it by.

    The folder is stamped HERE, from the path being written to, so a record can
    never name a folder it was not saved beside.
    """
    path = record_path(working_copy, create=True)
    if path is None:
        raise EvidenceError("invalid", project_local_state.NO_PROJECT_ID)
    origin.working_copy = folder_key(working_copy)
    atomic_write.write_json(path, to_json(origin))


def repoint(
    working_copy: Path,
    origin: Origin,
    *,
    backend: str,
    identity: str,
    base: str,
    params: dict[str, Any],
) -> Origin:
    """Rewrite WHERE this working copy's project lives, and nothing else.

    The recovery for a server that moved — a renamed NAS, FTPS replaced by the web
    server. Today every pass refuses such a working copy (``_refuse_other_origin``
    cannot tell a moved server from somebody else's) and the only exit is *Forget*,
    which deletes the folder with the unpushed work in it.

    **The manifest is KEPT**: every row still describes the bytes that are here and
    the bytes that were there, and throwing it away would make the next pass read
    every file as new on both sides. ``needs_pull`` is set instead, so the new server
    is listed and every file settled by its bytes (or asked) before anything is pushed.

    This writes. That the new address holds the SAME project is proved first, by the
    caller that can reach it (``project_sync_gateway.SyncGateway.repoint``).
    """
    origin.backend = backend
    origin.identity = identity
    origin.base = base
    origin.params = without_choice(params)
    origin.needs_pull = True
    # What the old address answered says nothing of the new one: its first read is whole.
    origin.since, origin.listing, origin.stores, origin.releases_failed = "", {}, {}, {}
    origin.bundle_server = ""
    save(origin, working_copy)
    log.info("%s now points at %s %s; a pull is owed", working_copy, identity, base)
    return origin


def project_id_in(project_folder: Path) -> str:
    """The ``project_id`` a project FOLDER declares — never ``""``.

    A half-written, empty, truncated or foreign ``config/config.json`` passes
    ``is_file()`` and declares no identity, and a pass that trusted the file's mere
    existence uploaded that emptiness over a real project and deleted its tests. So
    the identity is READ, not assumed to be there.
    """
    path = project_folder.joinpath(*CONFIG_FILE.split("/"))
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise EvidenceError("not_found", f"{path} cannot be read ({exc})") from exc
    return project_id_of(raw)


def project_id_of(config_bytes: bytes) -> str:
    """The ``project_id`` out of a project's ``config/config.json``.

    A server project without one cannot be checked out: the working copy, the venv
    and the evidence store are all keyed by it, and keying on the path instead is
    the bug ``project_local_state`` exists to remove.
    """
    try:
        data = json.loads(config_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise EvidenceError("corrupt", f"the project's {CONFIG_FILE} is not JSON: {exc}") from exc
    found = data.get("project_id") if isinstance(data, dict) else None
    if not isinstance(found, str) or not found:
        raise EvidenceError(
            "invalid",
            f"the project on the server has no project_id in {CONFIG_FILE}; open it once "
            "from a folder it can be written in, then check it out",
        )
    return found
