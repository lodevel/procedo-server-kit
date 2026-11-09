"""The steps of a pass of :mod:`project_sync_api` — what the server holds, and each change
carried out, over a :class:`project_store.ProjectSource`.

* **The read** (:func:`read`): what changed since the record's token on top of the listing it
  keeps, or the whole project (:func:`whole`); a text document the read did not bring needs a
  whole read (:func:`needs_whole`).
* **This PC's side**: the working copy's files the sync carries and its ``bundle/`` folder as
  one stamp (:func:`scan`); the bundle written whole (:func:`land_bundle`); the drafts of the
  tests a request changes (:func:`drafts`).
* **The steps**: conflicts settled by their bytes (:func:`settle`), the downloads
  (:func:`downloads`), the deletes here into this PC's trash (:func:`local_deletes`), the
  uploads and approved deletes as ONE request with each outcome recorded (:func:`changes_of`,
  :func:`send`, :func:`record_outcome`), the bundle both ways (:func:`bundle_both_ways`,
  :func:`restore_bundle`) and the releases written (:func:`releases`). What only the read
  that saw it change brings (the bundle, the releases) and the pass did not apply keeps the
  token where it was (:func:`bundle_owed`, :func:`releases`' answer).

Qt-free; every call that reaches the server runs on the pass's worker.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from . import (
    atomic_write,
    project_local_state,
    project_origin,
    project_sync,
    project_tree_releases,
    test_format,
    test_release,
)
from .evidence_store import EvidenceError, OpContext
from .evidence_store_fs_shared import FsTree
from .evidence_tree import name_key
from .paths import sha256_file
from .project_never_travels import is_object
from .project_origin import Conflict, Origin, Row, Stamp
from .project_store import (
    BUNDLE_DIR,
    CHANGED,
    FUNCTIONS_DIR,
    GONE,
    RELEASES_DIR,
    SAVED,
    TESTS_DIR,
    Bundle,
    BundleWrite,
    Change,
    Delete,
    Draft,
    FileRef,
    Outcome,
    ProjectSource,
    Pushed,
    ReleaseRecord,
    Write,
    pack,
    version_of,
)
from .project_sync_apply import Saver
from .project_sync_pass import FATAL, check_cancel, typed_failure
from .project_sync_plan import (
    DELETE_LOCAL,
    DELETE_REMOTE,
    DELETED_EDITED,
    DOWNLOAD,
    EDITED_DELETED,
    EDITED_EDITED,
    NEW_NEW,
    UPLOAD,
    Plan,
)
from .project_sync_report import SyncReport
from .project_sync_scan import local_now, local_path
from .project_sync_transfer import land_part, part_of, record
from .project_sync_trash import grave_name, move_aside, take_out

log = logging.getLogger(__name__)

#: The path a conflict on the project's bundle is recorded under: the bundle is one object.
BUNDLE = BUNDLE_DIR
#: How a read's ``missing`` names the bundle's archive (``data_format.md`` §4.2).
MISSING_BUNDLE = f"{BUNDLE_DIR}/"


def server_version(ref: FileRef) -> Stamp:
    """A file's stamp on a server that versions its files: its size and its version."""
    return Stamp(ref.size, 0.0, ref.version)


# ---------------------------------------------------------------------------
# This PC's side
# ---------------------------------------------------------------------------


def scan(working_copy: Path, refused: list | None = None) -> tuple[dict[str, Stamp], dict]:
    """The working copy's files the sync carries, and its ``bundle/`` files by their path in
    the folder (``releases/`` is written from the server, never read here)."""
    files: dict[str, Stamp] = {}
    bundle: dict[str, Stamp] = {}
    for rel, stamp in project_sync.scan_local(working_copy, refused=refused).items():
        if not is_object(rel):
            files[rel] = stamp
        elif rel.split("/", 1)[0].casefold() == BUNDLE_DIR:
            bundle[rel.split("/", 1)[1]] = stamp
    return files, bundle


def bundle_stamp(stamps: Mapping[str, Stamp]) -> str:
    """This PC's ``bundle/`` folder as one stamp: it changed when one of its files did."""
    if not stamps:
        return ""
    raw = json.dumps(sorted((rel, s.size, s.mtime) for rel, s in stamps.items()))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def bundle_files(working_copy: Path, stamps: Mapping[str, Stamp]) -> dict[str, bytes]:
    return {rel: local_path(working_copy, f"{BUNDLE_DIR}/{rel}").read_bytes() for rel in stamps}


def unsendable(files: Mapping[str, bytes]) -> str:
    """Why this bundle cannot go to the server (``""``: it can): the server knows a bundle
    by the id and version its ``bundle.json`` names."""
    manifest = Bundle(files).manifest
    if isinstance(manifest.get("id"), str) and isinstance(manifest.get("version"), str):
        return ""
    return "its bundle.json names no id and version, so the bundle was not sent"


def land_bundle(working_copy: Path, bundle: Bundle, grave: Path | None) -> dict[str, Stamp]:
    """*bundle* as this PC's ``bundle/`` folder: each file written whole, a file it no longer
    has moved into this PC's trash. The folder's stamps once written."""
    _files, before = scan(working_copy)
    for rel in sorted(set(before) - set(bundle.files)):
        if grave is not None:
            move_aside(local_path(working_copy, f"{BUNDLE_DIR}/{rel}"), grave / BUNDLE_DIR / rel)
    for rel, data in sorted(bundle.files.items()):
        target = local_path(working_copy, f"{BUNDLE_DIR}/{rel}")
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write.write_bytes(target, data)
    return scan(working_copy)[1]


def drafts(working_copy: Path, paths: Iterable[str]) -> list[Draft]:
    """The draft of each test whose documents a change of *paths* touches, as the app computes
    it (``data_format.md`` §4.3), with the test's hidden id: a file under ``tests/<test>/``, or a
    ``functions/`` file the test's documents import. A test that is no longer here, that has no
    id, or whose documents are not whole (no fingerprint yet), goes without one."""
    tests: set[str] = set()
    functions: set[str] = set()
    for path in paths:
        parts = path.split("/")
        if len(parts) > 2 and parts[0] == TESTS_DIR:
            tests.add(parts[1])
        elif len(parts) > 1 and parts[0] == FUNCTIONS_DIR:
            functions.add(path)
    root = Path(working_copy) / TESTS_DIR
    candidates = set(tests)
    if functions:  # which tests import them is read from every test's documents
        candidates |= {p.name for p in root.iterdir() if p.is_dir()} if root.is_dir() else set()
    made = []
    for test in sorted(t for t in candidates if (root / t).is_dir()):
        try:
            fmt, docs = test_release.documents(working_copy, test)
        except OSError:
            continue
        if test not in tests and not functions & set(docs):
            continue
        content, fingerprint = test_release.content_of(docs), test_release.fingerprint_of(fmt, docs)
        test_id = test_release.test_id_of(working_copy, test)
        if content and fingerprint and test_id:
            latest = test_release.latest(working_copy, test)
            made.append(Draft(test, test_id, fingerprint, content, latest))
    return made


# ---------------------------------------------------------------------------
# The server's side
# ---------------------------------------------------------------------------


@dataclass
class Read:
    """What one read of the server gave: the token it answered, every file it holds (the
    listing) and the storage each binary version is in (``stores``), the files this read
    brought (text included), the bundle and the releases when they came, what it cannot give,
    and the version of the bundle it named (given or not)."""

    token: str
    listing: dict[str, Stamp]
    refs: dict[str, FileRef]
    bundle: Bundle | None
    releases: Mapping[str, Sequence[ReleaseRecord]]
    missing: dict[str, str]
    whole: bool
    stores: dict[str, int]
    bundle_version: str


def stores_of(refs: Iterable[FileRef]) -> dict[str, int]:
    """The storage each binary file's version is in, as the server named it."""
    return {ref.version: ref.store_id for ref in refs if ref.store_id is not None}


def whole(source: ProjectSource, origin: Origin, ctx: OpContext) -> Read:
    found = source.read_project(origin.project_id, ctx)
    return Read(
        found.token,
        {path: server_version(ref) for path, ref in found.files.items()},
        dict(found.files),
        found.bundle,
        found.releases,
        dict(found.missing),
        True,
        stores_of(found.files.values()),
        found.bundle_version or (found.bundle.version if found.bundle is not None else ""),
    )


def read(source: ProjectSource, origin: Origin, ctx: OpContext) -> Read:
    """What the server holds: what changed since the record's token on top of the listing it
    keeps, or the whole project."""
    if origin.since:
        try:
            found = source.changes_since(origin.project_id, origin.since, ctx)
        except EvidenceError as exc:
            if exc.code != GONE:
                raise
            log.info("project sync: the server cannot answer from the token; reading it whole")
        else:
            moved = {name_key(path) for path in (*found.deleted, *found.files)}
            listing = {p: s for p, s in origin.listing.items() if name_key(p) not in moved}
            listing.update((path, server_version(ref)) for path, ref in found.files.items())
            return Read(
                found.token,
                listing,
                dict(found.files),
                found.bundle,
                found.releases,
                dict(found.missing),
                False,
                {**origin.stores, **stores_of(found.files.values())},
                found.bundle_version or (found.bundle.version if found.bundle is not None else ""),
            )
    return whole(source, origin, ctx)


def kept_stores(read: Read) -> dict[str, int]:
    """The storages the record keeps: those of the versions the listing holds."""
    held = {stamp.version for stamp in read.listing.values()}
    return {version: store for version, store in read.stores.items() if version in held}


def ref(read: Read, rel: str) -> FileRef:
    """*rel* as the server holds it: the file the read brought, else its listing's version in
    the storage an earlier read named for it."""
    if rel in read.refs:
        return read.refs[rel]
    stamp = read.listing[rel]
    return FileRef(rel, stamp.version, stamp.size, store_id=read.stores.get(stamp.version))


def needs_whole(read: Read, made: Plan) -> bool:
    """Whether a download of *made* is a file this read did not bring and whose storage no read
    named: a text document (its text comes only in a read), so the project is read whole."""
    return not read.whole and any(
        a.kind == DOWNLOAD
        and a.path not in read.refs
        and read.listing[a.path].version not in read.stores
        for a in made.actions
    )


# ---------------------------------------------------------------------------
# The steps of a pass
# ---------------------------------------------------------------------------


def settle(working_copy: Path, origin: Origin, made: Plan, report: SyncReport) -> None:
    """A conflict whose two copies hold the same bytes is none: the server's version IS the
    sha256 of its bytes, so this side's file alone is read."""
    for conflict in made.conflicts:
        here, there = conflict.local, conflict.server
        same = (
            here is not None
            and there is not None
            and here.size == there.size
            and local_now(working_copy, conflict.path) == here
            and there.version == f"sha256:{sha256_file(local_path(working_copy, conflict.path))}"
        )
        if same:
            record(origin, conflict.path, Row(there, here))  # type: ignore[arg-type]
            continue
        log.warning("project sync: %s is in conflict (%s)", conflict.path, conflict.kind)
        report.conflicts.append(conflict)


#: What :func:`download` answers when a test started running from the copy while the bytes
#: came: nothing landed, and the next pass brings it.
HELD = "held"


def download(
    source: ProjectSource,
    working_copy: Path,
    origin: Origin,
    ref: FileRef,
    scanned: Stamp | None,
    ctx: OpContext,
    *,
    aside: Path | None = None,
    hold: Callable[[], bool] | None = None,
) -> Conflict | str | None:
    """Land *ref* as the working copy's file, unless that file is no longer *scanned*: then the
    conflict, nothing landed; or unless *hold* answers True once the bytes are here: then
    :data:`HELD`, nothing landed (the bytes stay in this PC's cache, never in the copy)."""
    server = server_version(ref)
    part = part_of(local_path(working_copy, ref.path), server)
    source.fetch(origin.project_id, ref, part, ctx)
    if hold is not None and hold():
        with suppress(OSError):
            part.unlink()
        return HELD
    if land_part(working_copy, ref.path, part, server, origin, expect=scanned, aside=aside):
        return None
    now = local_now(working_copy, ref.path)
    kind = NEW_NEW if scanned is None else (DELETED_EDITED if now is None else EDITED_EDITED)
    return Conflict(ref.path, kind, now, server)


def downloads(
    source: ProjectSource,
    working_copy: Path,
    origin: Origin,
    made: Plan,
    read: Read,
    scanned: Mapping[str, Stamp],
    ctx: OpContext,
    report: SyncReport,
    *,
    defer: bool,
    hold: Callable[[], bool] | None,
    progress: Callable[[str, Stamp | None], None] | None,
) -> None:
    """Every download of *made*: the deferred class (the board archive, documents, large files)
    last and largest last, or left for later with *defer*; nothing while *hold* answers True,
    asked before each one and again right before it lands. The record is saved as the pass
    goes (:class:`project_sync_apply.Saver`)."""
    late_rule = project_sync._deferred_rule(working_copy)
    wanted = [a for a in made.actions if a.kind == DOWNLOAD]
    late = sorted(
        (a for a in wanted if late_rule(a.path, read.listing[a.path].size)),
        key=lambda a: (read.listing[a.path].size, a.path),
    )
    first = [a for a in wanted if a not in late]
    if defer:
        report.deferred.extend(a.path for a in late)
        late = []
    here = {name_key(rel): stamp for rel, stamp in scanned.items()}
    saver = Saver(origin, working_copy)
    try:
        for action in first + late:
            check_cancel(ctx)
            if hold is not None and hold():
                report.held.append(action.path)
                continue
            if action.path in read.missing:
                report.skipped.append(
                    (action.path, f"the server cannot give it ({read.missing[action.path]})")
                )
                continue
            if progress is not None:
                progress(action.path, read.listing.get(action.path))
            try:
                landed = download(
                    source,
                    working_copy,
                    origin,
                    ref(read, action.path),
                    here.get(name_key(action.path)),
                    ctx,
                    hold=hold,
                )
            except (OSError, EvidenceError) as exc:
                failure = typed_failure(exc)
                if failure.kind in FATAL:
                    raise failure from exc
                report.skipped.append((action.path, str(failure)))
                continue
            if landed == HELD:
                report.held.append(action.path)
                continue
            if isinstance(landed, Conflict):
                report.conflicts.append(landed)
                continue
            report.downloaded.append(action.path)
            saver.moved(read.listing[action.path].size)
    finally:
        saver.flush()


def local_deletes(
    working_copy: Path,
    origin: Origin,
    made: Plan,
    ctx: OpContext,
    report: SyncReport,
    writer: str,
    hold: Callable[[], bool] | None,
) -> None:
    """Every delete here: into this PC's trash, never to nothing; none while *hold* answers."""
    grave = trash_of(working_copy, writer)
    for action in made.actions:
        if action.kind != DELETE_LOCAL:
            continue
        check_cancel(ctx)
        if hold is not None and hold():
            report.held.append(action.path)
            continue
        row = origin.files.get(action.path)
        if row is None:
            report.skipped.append((action.path, "the manifest row that justified it is gone"))
            continue
        try:
            why = take_out(working_copy, origin, action.path, row, grave, back=lambda: False)
        except (OSError, EvidenceError) as exc:
            why = str(typed_failure(exc))
        if why:
            report.skipped.append((action.path, why))
            continue
        report.deleted_local.append(action.path)
        if grave is not None:
            report.local_trash = str(grave)
        project_origin.save(origin, working_copy)


def trash_of(working_copy: Path, writer: str) -> Path | None:
    trash = project_local_state.local_trash_dir(working_copy)
    return None if trash is None else trash / grave_name(writer)


@dataclass(frozen=True)
class Sent:
    """What one change of a request was made from: this PC's file as it was read."""

    change: Change
    here: Stamp | None


def changes_of(
    working_copy: Path,
    origin: Origin,
    made: Plan,
    approved: frozenset[str],
    report: SyncReport,
) -> list[Sent]:
    """The uploads and the approved deletes of *made*, each naming the version it replaces
    (the one the manifest records); a delete not approved stays, in ``pending_deletes``. A
    test's ``test.json`` is written before every other change and deleted after them
    (:func:`marker_order`), so no change leaves a test without its id."""
    sent = []
    for action in made.actions:
        row = origin.files.get(action.path)
        if action.kind == UPLOAD:
            try:
                src = local_path(working_copy, action.path)
                stat = src.stat()
                data = src.read_bytes()
            except (OSError, EvidenceError) as exc:
                report.skipped.append((action.path, str(typed_failure(exc))))
                continue
            replaces = row.server.version if row is not None else None
            here = Stamp(stat.st_size, stat.st_mtime)
            sent.append(Sent(Write(action.path, data, replaces), here))
        elif action.kind == DELETE_REMOTE:
            if action.path not in approved:
                report.pending_deletes.append(action.path)
            elif row is None:
                report.skipped.append((action.path, "the manifest row that justified it is gone"))
            else:
                sent.append(Sent(Delete(action.path, row.server.version), None))
    return marker_order(sent)


def _is_marker(path: str) -> bool:
    """Whether *path* is a test's own ``test.json`` (``tests/<test>/test.json``)."""
    parts = path.split("/")
    return len(parts) == 3 and parts[0] == TESTS_DIR and parts[2] == test_format.TEST_JSON


def marker_order(sent: list[Sent]) -> list[Sent]:
    """*sent* in the order the server checks a test's id by (``data_format.md`` §4.3): each
    test's ``test.json`` written first, so a new test holds its id before its other files land,
    and deleted last, once the test's other files are gone; the rest as it came."""

    def rank(item: Sent) -> int:
        if not _is_marker(item.change.path):
            return 1
        return 0 if isinstance(item.change, Write) else 2

    return sorted(sent, key=rank)


def bundle_write(working_copy: Path, stamps: Mapping[str, Stamp], replaces: str) -> Sent:
    files = bundle_files(working_copy, stamps)
    return Sent(BundleWrite(files, replaces or None), Stamp(0, 0.0, bundle_stamp(stamps)))


def send(
    source: ProjectSource,
    working_copy: Path,
    origin: Origin,
    sent: list[Sent],
    listing: dict[str, Stamp],
    ctx: OpContext,
    report: SyncReport,
) -> None:
    """One request for every change (``P9``) with the drafts of the tests it touches, and each
    outcome recorded: saved, a conflict (``changed``: settled when the server holds these very
    bytes), or refused by the server's own rule."""
    if not sent:
        return
    changes = [s.change for s in sent]
    paths = [c.path for c in changes if not isinstance(c, BundleWrite)]
    pushed = source.push_changes(origin.project_id, changes, drafts(working_copy, paths), ctx)
    for item, outcome in zip(sent, outcomes(pushed, len(sent)), strict=True):
        record_outcome(origin, item, outcome, listing, report)
    project_origin.save(origin, working_copy)


def outcomes(pushed: Pushed, count: int) -> tuple[Outcome, ...]:
    """The server's outcome of each of *count* changes, in order. An answer that does not hold
    one for each is refused: none of it is recorded, and the next pass meets each file as the
    server holds it then."""
    if len(pushed.results) != count:
        raise EvidenceError(
            "corrupt",
            f"The server answered {len(pushed.results)} outcomes for {count} changes, so none "
            "was recorded.",
        )
    return pushed.results


def record_outcome(
    origin: Origin, item: Sent, outcome: Outcome, listing: dict[str, Stamp], report: SyncReport
) -> None:
    change = item.change
    if isinstance(change, BundleWrite):
        if outcome.status == SAVED:
            origin.bundle_server = outcome.version or version_of(pack(change.files))
            origin.bundle_local = item.here.version  # type: ignore[union-attr]
            report.uploaded.append(BUNDLE)
        elif outcome.status == CHANGED:
            there = outcome.current.version if outcome.current is not None else ""
            report.conflicts.append(
                Conflict(BUNDLE, EDITED_EDITED, item.here, Stamp(0, 0.0, there))
            )
        else:
            report.skipped.append((BUNDLE, outcome.message or outcome.kind))
        return
    path = change.path
    row = origin.files.get(path)
    if outcome.status == SAVED:
        if isinstance(change, Write):
            server = Stamp(len(change.data), 0.0, outcome.version or version_of(change.data))
            record(origin, path, Row(server, item.here))  # type: ignore[arg-type]
            listing[path] = server
            report.uploaded.append(path)
        else:
            origin.files.pop(path, None)
            listing.pop(path, None)
            report.deleted_remote.append(path)
        return
    if outcome.status != CHANGED:
        report.skipped.append((path, outcome.message or outcome.kind or outcome.status))
        return
    there = server_version(outcome.current) if outcome.current is not None else None
    if there is None:
        listing.pop(path, None)
    else:
        listing[path] = there
    if isinstance(change, Write):
        if there is not None and there.version == version_of(change.data):
            record(origin, path, Row(there, item.here))  # type: ignore[arg-type]
            return  # two benches saved the same bytes: no conflict
        kind = EDITED_DELETED if there is None else (NEW_NEW if row is None else EDITED_EDITED)
        report.conflicts.append(Conflict(path, kind, item.here, there))
    elif there is not None:
        report.conflicts.append(Conflict(path, DELETED_EDITED, None, there))
    else:
        origin.files.pop(path, None)  # gone on both sides


def bundle_both_ways(
    working_copy: Path,
    origin: Origin,
    read: Read | None,
    stamps: Mapping[str, Stamp],
    report: SyncReport,
    writer: str,
    hold: Callable[[], bool] | None,
    approved: frozenset[str] = frozenset(),
) -> Sent | None:
    """The bundle: written here when only the server's changed, sent (the returned change) when
    only this PC's did, a conflict when both did and they differ. A read that brought no
    bundle (a delta where it did not change, or a project with none) changes nothing here; one
    the server cannot give is reported. A ``bundle/`` folder removed here is a delete waiting
    for its answer, as a file's is (``pending_deletes``); the server keeps a project's bundle,
    so an approved one is refused with why."""
    here = bundle_stamp(stamps)
    changed_here = here != origin.bundle_local
    there = read.bundle if read is not None else None
    changed_there = there is not None and there.version != origin.bundle_server
    if read is not None and bundle_unavailable(read, origin):
        why = read.missing[MISSING_BUNDLE]
        report.skipped.append((BUNDLE, f"the server cannot give it ({why})"))
    if BUNDLE in origin.conflicts:
        report.conflicts.append(origin.conflicts[BUNDLE])
        return None
    if changed_there and changed_here:
        if bundle_version(working_copy, stamps) == there.version:  # type: ignore[union-attr]
            origin.bundle_server, origin.bundle_local = there.version, here  # type: ignore[union-attr]
            return None
        local, server = Stamp(0, 0.0, here), Stamp(0, 0.0, there.version)  # type: ignore[union-attr]
        report.conflicts.append(Conflict(BUNDLE, EDITED_EDITED, local, server))
        return None
    if changed_there:
        if hold is not None and hold():
            report.held.append(BUNDLE)
            return None
        landed = land_bundle(working_copy, there, trash_of(working_copy, writer))  # type: ignore[arg-type]
        origin.bundle_server, origin.bundle_local = there.version, bundle_stamp(landed)  # type: ignore[union-attr]
        report.downloaded.append(BUNDLE)
        return None
    if changed_here and not stamps:
        if BUNDLE in approved:
            report.skipped.append((BUNDLE, BUNDLE_KEPT))
        else:
            report.pending_deletes.append(BUNDLE)
        return None
    if changed_here:
        item = bundle_write(working_copy, stamps, origin.bundle_server)
        why = unsendable(item.change.files)  # type: ignore[union-attr]
        if why:
            report.skipped.append((BUNDLE, why))
            return None
        return item
    return None


#: Why a removed ``bundle/`` folder is not removed on the server too.
BUNDLE_KEPT = (
    "the server keeps a project's bundle: it is replaced by another, never removed. Bring it "
    "back, or install another bundle"
)


def restore_bundle(
    working_copy: Path, origin: Origin, read: Read, report: SyncReport, writer: str
) -> None:
    """The server's bundle back as this PC's ``bundle/`` folder, only while that folder holds no
    file (one put back here meanwhile is a conflict, nothing landed)."""
    there = read.bundle
    if there is None:
        report.skipped.append((BUNDLE, "the server holds no bundle for it to come back from"))
        return
    _files, stamps = scan(working_copy)
    if stamps:
        local, server = Stamp(0, 0.0, bundle_stamp(stamps)), Stamp(0, 0.0, there.version)
        report.conflicts.append(Conflict(BUNDLE, NEW_NEW, local, server))
        return
    landed = land_bundle(working_copy, there, trash_of(working_copy, writer))
    origin.bundle_server, origin.bundle_local = there.version, bundle_stamp(landed)
    report.downloaded.append(BUNDLE)


def bundle_version(working_copy: Path, stamps: Mapping[str, Stamp]) -> str:
    return version_of(pack(bundle_files(working_copy, stamps))) if stamps else ""


def bundle_unavailable(read: Read, origin: Origin) -> bool:
    """Whether the read named a bundle the server cannot give that is not the one this PC
    already holds (one it landed before is no loss)."""
    return MISSING_BUNDLE in read.missing and read.bundle_version != origin.bundle_server


def bundle_owed(read: Read, origin: Origin, report: SyncReport) -> bool:
    """Whether the bundle this read brought was not landed (held while a test ran from the
    copy), or the read named one the server cannot give that this PC does not hold: a read by
    what changed brings it once. One in conflict is not owed: its answer reads the project
    whole."""
    if MISSING_BUNDLE in read.missing:
        return bundle_unavailable(read, origin)
    there = read.bundle
    return (
        there is not None
        and there.version != origin.bundle_server
        and not any(c.path == BUNDLE for c in report.conflicts)
    )


def releases(
    working_copy: Path, read: Read, report: SyncReport, hold: Callable[[], bool] | None
) -> dict[str, str] | None:
    """The releases the read brought, written as this PC's ``releases/<test_id>/`` files (P22),
    each test id on its own: one this PC cannot write never keeps the others out. ``None`` while
    *hold* answers True (nothing written); else the test ids not written, with why."""
    if not read.releases:
        return {}
    if hold is not None and hold():
        report.held.append(RELEASES_DIR)
        return None
    tree, failed = FsTree(working_copy), {}
    for test_id, held in sorted(read.releases.items()):
        try:
            project_tree_releases.write(tree, working_copy, {test_id: held})
        except (OSError, EvidenceError) as exc:
            failed[test_id] = str(typed_failure(exc))
    return failed
