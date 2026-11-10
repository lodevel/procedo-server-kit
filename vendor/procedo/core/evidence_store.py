"""Evidence store seam — keys, types, typed errors, protocol and backend registry.

Evidence = per-board results, reports and exports. Consumers address it by
:class:`EvidenceKey` (never by path) through a store obtained from
``ProjectModel.evidence``; :meth:`EvidenceStore.local_path` is the one hinge
that hands back a real path for ``open()`` / ``QUrl.fromLocalFile``.
Qt-free. Design: ``docs/plans/evidence_store_design.md``.
"""

from __future__ import annotations

import copy
import os
import queue
import re
import threading
import unicodedata
from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from . import app_settings
from .paths import is_windows_reserved
from .test_format import is_test_id

if TYPE_CHECKING:
    from .evidence_search import Filters, ResultRow
    from .evidence_search_choices import Choices
    from .evidence_trash import Deletion, TrashItem

KINDS: tuple[str, ...] = ("run", "report", "export", "draft")
CURRENT_RUN = "current"  # the run the test's pointer names (``current.json``)
RESULT_FILE = "results.json"  # canonical run payload the test script writes
FUNCTIONS = "functions"  # a project's functions folder, and the snapshot a run imports them from


@dataclass
class OpContext:
    """Per-operation cancel/progress/timeout budget; defaults suit inline local I/O."""

    cancel: threading.Event = field(default_factory=threading.Event)
    progress: Callable[[int, int], None] | None = None  # (done, total); total 0 = unknown
    timeout_s: float = 30.0
    #: Scratch shared by the calls of ONE logical operation. The service builds a
    #: context per request and hands the same one to every call of a walk, so work
    #: that is per-walk rather than per-call (the evidence index parses its shards
    #: once, not once per board) has somewhere to live that cannot outlive the
    #: request — a store-level cache would go stale, which is what the tree contract
    #: forbids when it says a listing is never cached across operations.
    memo: dict[Any, Any] = field(default_factory=dict)
    #: Filled with the holder's owner record when a store-side lock wait has lasted
    #: :data:`evidence_tree.HELD_NOTICE_S` on another writer's lock, so the person waiting
    #: can be told who (``evidence_view.call_modal``); empty otherwise.
    lock_held: dict[str, Any] = field(default_factory=dict)
    #: Set by the person waiting (*Stop waiting*): the lock wait gives up at once with the
    #: holder named, as its timeout would (:class:`evidence_lock_holder.EvidenceLocked`).
    stop_lock_wait: threading.Event = field(default_factory=threading.Event)


#: The rows a search answers at most, the newest (owner, 2026-11-07): a person narrows the
#: search to see the rest.
NEWEST = 500

#: How many reads of ONE operation a store serves at once against a remote root
#: (``EvidenceStore.read_parallelism``); a local root always reads one by one.
READ_WORKERS = 8


def store_operation(store: Any, ctx: OpContext | None) -> Any:
    """The store's tree operation scope — one leased connection, one listing per folder,
    one cancel flag — for one test's read; a store without a tree has no scope."""
    from .evidence_tree import tree_operation  # the tree module builds on this one

    return tree_operation(getattr(store, "io", None), ctx.cancel if ctx is not None else None)


def read_each(
    store: Any,
    items: list,
    read: Callable[[Any], Any],
    ctx: OpContext | None = None,
    done: Callable[[int, Any], None] | None = None,
) -> list:
    """``read(item)`` for every item, answered in ``items``' order.

    The first read runs alone on the calling thread: it proves the store answers (a
    wrong password, a refused server) before more connections are opened. Against a
    remote root the rest then run up to ``store.read_parallelism`` at once on daemon
    threads; a local root reads one by one on the calling thread. Each read is ONE
    operation of the store's tree (:func:`store_operation`): one leased connection for the
    whole read, handed back to the pool when it ends — so a connection one test's
    read left in doubt is never handed to the next, and logins stay bounded by the
    parallelism. ``done(index, answer)`` runs on the CALLING thread as each read
    lands; if it raises, no further read starts and its error is raised.

    Once a read fails or ``ctx`` is cancelled no further read starts; the reads
    already started finish and the earliest item's error is raised."""
    n = len(items)
    answers: list = [None] * n

    def one(i: int) -> Any:
        if ctx is not None and ctx.cancel.is_set():
            raise EvidenceError("cancelled", "the evidence read was cancelled")
        with store_operation(store, ctx):
            return read(items[i])

    def land(i: int, answer: Any) -> None:
        answers[i] = answer
        if done is not None:
            done(i, answer)

    remote = store is not None and store.latency_class() == "remote"
    width = min(store.read_parallelism if remote else 1, n - 1)
    first = n if width <= 1 else 1  # one by one: every read; else the first alone
    for i in range(first):
        land(i, one(i))
    if width <= 1:
        return answers
    todo = iter(range(1, n))
    lock = threading.Lock()
    stop = threading.Event()
    landed: queue.SimpleQueue = queue.SimpleQueue()

    def work() -> None:
        try:
            while not stop.is_set():
                with lock:
                    i = next(todo, None)
                if i is None:
                    return
                try:
                    landed.put((i, one(i), None))
                except BaseException as exc:  # reported on the calling thread
                    stop.set()
                    landed.put((i, None, exc))
        finally:
            landed.put(None)  # this worker started its last read

    for _ in range(width):
        threading.Thread(target=work, name="evidence-read", daemon=True).start()
    errors: dict[int, BaseException] = {}
    running = width
    try:
        while running:
            got = landed.get()
            if got is None:
                running -= 1
            elif got[2] is None:
                land(got[0], got[1])
            else:
                errors[got[0]] = got[2]
    finally:
        stop.set()  # whatever ends the wait — a raising ``done`` included — ends the reads
    if errors:
        raise errors[min(errors)]
    return answers


@dataclass(frozen=True)
class EvidenceKey:
    """One key shape for every kind; ``name == ""`` addresses the run/artifact directory."""

    project_id: str
    board: str  # the board's key (:mod:`evidence_board`)
    kind: str  # "run" | "report" | "export" | "draft"
    test_id: str | None = None  # runs only: the test's hidden id (check_test_id), never its name
    run_id: str | None = None  # runs only; "current" = the one the pointer names
    name: str = ""  # payload filename (runs) or artifact name


@dataclass(frozen=True)
class StagedRun:
    """A run opened for an external writer: ``path`` receives its payload files,
    ``commit_run`` publishes it. Legacy: ``run_id == "current"``, in-place path."""

    run_id: str
    path: Path
    key: EvidenceKey


@dataclass(frozen=True)
class RunInfo:
    run_id: str
    started: str
    ended: str
    verdict: str
    machine_id: str
    complete: bool
    #: The name the run's test had when it ran, as recorded (NFC), never respelled: a record for
    #: a person to read. The run is filed under :attr:`test_id`, which a rename keeps.
    test: str = ""
    test_id: str = ""  # the test's hidden id (``test_format.TEST_ID``)
    schema_version: int = 1


@dataclass(frozen=True)
class ArtifactInfo:
    """The sidecar fields the report browser lists.

    ``tests``: the test ids a REPORT covers (empty for an export or a draft, and
    for a report whose sidecar names none — one written before this field
    existed, or with no ``tests`` list of its own). Search foundations
    (:mod:`evidence_index`, :mod:`evidence_search`): a listing index record is
    built from this and read back into it, so the field lives here once for
    both directions rather than being re-declared per caller.
    """

    name: str
    generated: str
    scenario: str
    operator: str
    overall_verdict: str
    machine_id: str
    tests: tuple[str, ...] = ()
    schema_version: int = 1


@dataclass(frozen=True)
class OutboxItem:
    """One entry the outbox still holds (pending) or refused (quarantined): a run
    or an artifact, with the routing the entry carries."""

    id: str  # run_id, or the artifact entry id
    kind: str  # "run" | "artifact"
    project_id: str
    board: str  # the board's key (:mod:`evidence_board`)
    test: str | None = None  # runs and comments: the name the run recorded, for a person to read
    test_id: str = ""  # runs and comments: the test's hidden id, what the entry is filed under
    artifact_kind: str | None = None  # artifacts: "report" | "export" | "draft"
    name: str | None = None  # artifacts: the payload filename
    reason: str = ""  # quarantined: why the store refused it
    root: str = ""  # the evidence folder (store identity) it waits for
    queued_from: str = ""  # the project folder that queued it ("" : none recorded)
    project_name: str = ""  # the label it had when it was sent away from that folder

    @property
    def project_label(self) -> str:
        """The project it is of, as a person reads it: its folder's name, else the name it
        was sent away under, else its id."""
        return Path(self.queued_from).name or self.project_name or self.project_id


@dataclass(frozen=True)
class ParamSpec:
    key: str
    label: str
    type: str
    required: bool
    default: Any
    secret: bool = False
    #: This PC's setting for the server, never a project's: kept in Settings ▸ Server alone and
    #: dropped from every entry a project records (:func:`evidence_config.project_entry`).
    this_pc: bool = False
    #: The ``choice:…|on`` param this one only means something with: greyed while that is not
    #: ``on`` (Accept any certificate needs TLS).
    needs: str = ""
    #: What the form says under the field while it is ``on``, in the warning colour.
    warning: str = ""


CAPABILITIES_PROBED = frozenset({"list", "write", "rename", "delete"})


@dataclass
class AccessReport:
    """One line per capability, never a bare bool."""

    reachable: bool
    can_list: bool
    can_write: bool
    can_rename: bool
    can_delete: bool
    per_file_ms: float | None
    clock_skew_s: float | None
    notes: list[str] = field(default_factory=list)
    #: Which capabilities this probe actually ATTEMPTED. A capability outside it was
    #: not tested, which is not the same as one that failed: a sign-in probe writes
    #: nothing by design, and rendering "Write: FAILED" for it sends the user to the
    #: server's ACL to fix a permission nobody asked about. Defaults to all four, so
    #: a full probe reads exactly as before.
    probed: frozenset[str] = CAPABILITIES_PROBED
    schema_version: int = 1


@dataclass(frozen=True)
class AccessAnswer:
    """What *Check access* of a project's storage says, in words: the whole report, and for a
    storage that keeps a run's files apart (the database) where it keeps them (``""``: not
    read, or none)."""

    text: str
    attachments: str = ""


class Absent(FileNotFoundError):
    """The store PROVED the entry is not there, rather than failing to reach it.

    A bare ``FileNotFoundError`` classifies as ``denied``, because a filesystem
    ``ENOENT`` on a share equally means the share is gone and reporting "no such run"
    would hide that. A tree that asked the server — FTPS ``550`` on a path its parent
    does not list — knows better, and :func:`evidence_store_fs.os_error` reads this
    subclass as ``not_found``: the kind the UI shows as "no report for this run"
    instead of an error state.
    """


class AuthRequired(PermissionError):
    """Credentials rejected by the store (FTPS ``530``), not a permission on a path.

    A tree may only raise ``OSError``s, and ``PermissionError`` alone classifies as
    ``denied`` ("check the ACL"), which sends the user to the wrong place when the
    password is what is wrong: :func:`evidence_store_fs.os_error` reads this subclass
    as ``auth`` and the GUI offers the credentials editor.
    """


class SignIn(tuple):
    """What a credentials lookup answers: ``(user, secret)`` — every backend unpacks exactly
    that — plus what an SSH server also needs from this PC, under the same server key:
    ``key_file`` (the private key to sign in with; the secret is then its passphrase) and
    ``host_key`` (the server's key this PC confirmed). Both ``""`` for every other backend."""

    key_file: str
    host_key: str

    def __new__(cls, user: str, secret: str, *, key_file: str = "", host_key: str = "") -> Any:
        made = super().__new__(cls, (user, secret))
        made.key_file = key_file
        made.host_key = host_key
        return made


def host_key_state(err: BaseException) -> str:
    """``"unknown"`` or ``"changed"`` when *err* (or what it was raised from) is an SSH
    server's host key refused on this PC, else ``""``: what a message strip words differently
    (the user goes to Test sign-in, not to a password)."""
    seen: set[int] = set()
    found: BaseException | None = err
    while found is not None and id(found) not in seen:
        seen.add(id(found))
        state = getattr(found, "host_key_state", "")
        if state:
            return str(state)
        found = found.__cause__ or found.__context__
    return ""


class EvidenceError(Exception):
    """kind: invalid | not_found | auth | unreachable | timeout | corrupt | denied |
    foreign | switched | cancelled | disk_full.

    ``invalid`` = a refusal or bad input (``bad_key`` marks a bad key component); ``denied`` = the backend
    refused (permissions, existing file in the way); ``foreign`` = the evidence folder
    belongs to ANOTHER project (``evidence_owner``) — its own kind because ``denied``
    is also an ACL, an anti-virus lock or a share that went away, and the GUI shuts a
    project's doors on the first while keeping the offline path for the second;
    ``switched`` = the project's ``config.json`` names another store now — nothing was
    written, and nothing is wrong with what was being written: it waits for the store
    the project names (never quarantined); ``cancelled`` = the caller stopped the
    operation (a closed dialog, a project switch, a stopped transfer) — nothing was
    written, and it is offered again rather than reported as a failure; ``disk_full`` =
    this PC's disk has no room for what a project sync was landing (it ends the pass).

    ``code`` names a refusal the contract names (``data_format.md`` §7), on an ``invalid``:
    :data:`DELETED` (a run a manager purged, an artifact name a manager deleted, an entry for
    a deleted run) or :data:`CHANGED` (a report draft changed since it was read)."""

    def __init__(
        self,
        kind: str,
        message: str,
        key: EvidenceKey | None = None,
        *,
        bad_key: bool = False,
        code: str = "",
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.key = key
        self.bad_key = (
            bad_key  # an ``invalid`` that names a key component: shown as "Invalid evidence key"
        )
        self.code = code


SWITCHED = "switched"  # see :class:`EvidenceError`
#: The contract's refusal codes an ``invalid`` may carry (:attr:`EvidenceError.code`).
DELETED = "deleted"
CHANGED = "changed"


# ---------------------------------------------------------------------------
# Key safety. Reads only guard traversal and keep the name verbatim (an NFD
# folder stays visible); creating a component applies the full rule set.
# ---------------------------------------------------------------------------

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def guard_component(value: str | None) -> str:
    """Reject empty, ``.``/``..``, separators and control characters; verbatim otherwise."""
    name = value or ""
    if (
        not name
        or name in (".", "..")
        or "/" in name
        or "\\" in name
        or _CONTROL_CHARS.search(name) is not None
    ):
        raise EvidenceError("invalid", f"unsafe evidence key component: {value!r}", bad_key=True)
    return name


def safe_component(value: str | None, *, siblings: Iterable[str] = ()) -> str:
    """Full rule set for a component about to be CREATED: :func:`guard_component`,
    NFC-normalized, no trailing dot/space, no Windows reserved device name, and no
    case-insensitive collision with an existing *sibling* of different spelling.
    ``_unsorted`` passes. Raises ``EvidenceError("invalid")``."""
    name = unicodedata.normalize("NFC", guard_component(value))
    if name[-1] in ". " or is_windows_reserved(name):
        raise EvidenceError("invalid", f"unsafe evidence key component: {value!r}", bad_key=True)
    folded = name.casefold()
    for sibling in siblings:
        if sibling != name and sibling.casefold() == folded:
            raise EvidenceError(
                "invalid", f"{value!r} collides case-insensitively with {sibling!r}", bad_key=True
            )
    return name


def check_test_id(value: str | None) -> str:
    """*value* when it is a test's hidden id (a lower-case UUID, ``test_format.is_test_id``),
    which every action naming a test takes; anything else is refused (``invalid``), a test's
    name included, before anything is read, written or queued (``data_format.md`` §2)."""
    if not is_test_id(value):
        raise EvidenceError("invalid", f"{value!r} is not a test's id", bad_key=True)
    return str(value)


def is_safe_component(value: str | None) -> bool:
    try:
        safe_component(value)
    except EvidenceError:
        return False
    return True


def same_folder(a: Path, b: Path) -> bool:
    """The folder rule's comparison: ``a`` names the same folder as ``b`` — by
    identity when both exist (``samefile``), else by normalized spelling
    (``realpath`` + ``normcase``), so a mapped drive and the UNC path of one
    share compare equal. Touches the filesystem: call it where I/O may run."""
    a, b = Path(a), Path(b)
    try:
        if a.exists() and b.exists():
            return os.path.samefile(a, b)
    except OSError:
        pass
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def within_folder(child: Path, parent: Path) -> bool:
    """``child`` is ``parent`` or lies inside it — ancestor by ancestor through
    :func:`same_folder`, so another spelling of ``parent`` still counts, and from
    ``child``'s real path, so a junction or symlink in its existing prefix cannot
    hide where it lands. I/O."""
    node = Path(os.path.realpath(child))
    return any(same_folder(candidate, parent) for candidate in (node, *node.parents))


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class EvidenceStore(Protocol):
    """Every call takes an optional :class:`OpContext` and may raise :class:`EvidenceError`.

    Beyond the design's contract this carries what consumers used to do by
    hand: the external-writer flow (``stage`` → ``commit_run``),
    the run-history actions (``delete_run`` / ``promote_run`` / ``demote_run``)
    and ``delete_artifact``.
    Every ``test_id`` parameter (and ``EvidenceKey.test_id``) is the test's hidden id, a lower-case
    UUID (:func:`check_test_id`), never its name: runs and current-run marks are filed under
    it, and a run records the name its test had when it ran (``RunInfo.test``).
    Artifact kinds: ``report`` (+ ``<stem>.json`` sidecar), ``export``
    (+ ``<name>.meta.json``), ``draft`` (the board's report draft, no sidecar).
    """

    ID: str  # "filesystem" | "ftps"
    PARAMS: list[ParamSpec]  # declared config fields — the GUI renders these
    CAPABILITIES: frozenset[str]  # {"read", "write", "query", "sweep"}
    # The run filters of a search this storage answers (``evidence_search.RUN_FILTERS``: the
    # operator, the bench, a measurement): a folder's index records the operator and the bench,
    # no measurement; a search naming one it does not answer is refused.
    SEARCH_RUN_FILTERS: frozenset[str]
    # What its ``search_choices`` proposes (``evidence_search_choices``: the part numbers, the
    # boards, the tests, the operators, the benches, a test's readings): a field it does not
    # declare is empty, never invented.
    SEARCH_CHOICES: frozenset[str]
    # How many reads of one operation may run at once against a remote root
    # (:func:`read_each`); each read is one operation of the store's tree.
    read_parallelism: int

    def latency_class(self) -> str: ...  # "local" | "remote", per CONFIGURED root
    def check_access(self, ctx: OpContext | None = None) -> AccessReport: ...
    # Optional: a backend reached through a credential proves the credential ALONE —
    # signing in and listing its root, writing nothing — so the machine-settings
    # editor can test one against an endpoint the user has not configured yet.
    # ``evidence_ops.check_endpoint`` refuses a store that does not offer it rather
    # than falling back to a probe that writes.
    def sign_in(self, ctx: OpContext | None = None) -> AccessReport: ...

    def list_boards(self, project_id: str, ctx: OpContext | None = None) -> list[str]: ...

    def list_runs(
        self, project_id: str, board: str, test_id: str, ctx: OpContext | None = None
    ) -> list[RunInfo]: ...
    def current_run(
        self, project_id: str, board: str, test_id: str, ctx: OpContext | None = None
    ) -> RunInfo | None: ...
    def read_run(
        self, project_id: str, board: str, test_id: str, run_id: str, ctx: OpContext | None = None
    ) -> dict: ...
    def write_run(
        self,
        project_id: str,
        board: str,
        test_id: str,
        manifest: dict,
        files: dict[
            str, bytes | str
        ],  # path in the run ("plots/x.png") -> bytes | text (platform newlines)
        ctx: OpContext | None = None,
    ) -> RunInfo: ...
    # External-writer flow (a test script writes its own payload files):
    def stage(self, key: EvidenceKey, ctx: OpContext | None = None) -> StagedRun: ...
    def discard_run(self, staged: StagedRun) -> None: ...  # a staged run that never started
    def commit_run(
        self, staged: StagedRun, manifest: dict, ctx: OpContext | None = None
    ) -> RunInfo: ...  # RunInfo.complete tells whether results.json was produced
    # manifest: "operator", "serial_number", "run_fields"? (the board's details,
    # run_fields.RunFields.recorded), "started"?, and "provenance"? (a RunProvenance the
    # run took at its start: run_provenance.read_start; without one, it is read at the commit)
    # Run history (Test Details context menu):
    def delete_run(
        self,
        project_id: str,
        board: str,
        test_id: str,
        run_id: str,
        ctx: OpContext | None = None,
        *,
        deletion: Deletion,
    ) -> None: ...  # moved into the evidence trash with the reason (``evidence_trash``)
    def promote_run(
        self, project_id: str, board: str, test_id: str, run_id: str, ctx: OpContext | None = None
    ) -> None: ...  # archived run becomes current; the old current is demoted first
    def demote_run(
        self, project_id: str, board: str, test_id: str, ctx: OpContext | None = None
    ) -> str | None: ...  # "Mark as not run": the pointer names no run any more

    def list_artifacts(
        self, kind: str, project_id: str, board: str, ctx: OpContext | None = None
    ) -> list[ArtifactInfo]: ...
    def read_artifact(
        self, kind: str, project_id: str, board: str, name: str, ctx: OpContext | None = None
    ) -> bytes: ...
    def write_artifact(
        self,
        kind: str,
        project_id: str,
        board: str,
        name: str,
        data: bytes,
        info: ArtifactInfo,
        *,
        sidecar: bytes | None = None,  # written verbatim as the sidecar; else synthesized from info
        replaces: str | None = None,  # a draft: the version read (``evidence_draft``); None: new
        ctx: OpContext | None = None,
    ) -> None: ...  # the shared layout queues the write in the outbox (never inline)
    def delete_artifact(
        self,
        kind: str,
        project_id: str,
        board: str,
        name: str,
        ctx: OpContext | None = None,
        *,
        deletion: Deletion,
    ) -> None: ...  # moved into the evidence trash with its sidecars, as a run is
    # A report or an export sent again under its name is kept (data_format.md §6.6): each
    # generation, oldest first, the newest last (the one ``read_artifact`` reads), and one.
    def list_generations(
        self, kind: str, project_id: str, board: str, name: str, ctx: OpContext | None = None
    ) -> list[tuple[int, ArtifactInfo]]: ...
    def read_generation(
        self,
        kind: str,
        project_id: str,
        board: str,
        name: str,
        generation: int,
        ctx: OpContext | None = None,
    ) -> bytes: ...
    # The evidence trash: what a delete moved, restored or removed for good after 90 days.
    def list_trash(self, project_id: str, ctx: OpContext | None = None) -> list[TrashItem]: ...
    def restore_trash(
        self, project_id: str, entry: str, ctx: OpContext | None = None
    ) -> bool: ...  # whether a run that was Current is Current again
    def purge_trash(
        self,
        project_id: str,
        entries: Collection[str] = (),
        ctx: OpContext | None = None,
        *,
        now: float | None = None,
    ) -> list[str]: ...  # *entries* and every expired entry, removed for good

    # Run annotations (comments / verdict overrides), entries never replaced
    # (``annotation_entries``): the run's entries in the order received, and add some.
    def read_run_annotation(
        self, project_id: str, board: str, test_id: str, run_id: str, ctx: OpContext | None = None
    ) -> dict | None: ...  # {"entries", "execution", …}; None when the run is not there
    def add_run_annotation_entries(
        self,
        project_id: str,
        board: str,
        test_id: str,
        run_id: str,
        entries: list[dict],
        ctx: OpContext | None = None,
    ) -> list[dict]: ...  # per entry: annotation_entries.outcome
    # A board's lot and fields (data_format.md §4.11), entries never replaced by the same
    # rule: the board's in the order received, and add some (a board not there is created).
    def read_board_entries(
        self, project_id: str, board: str, ctx: OpContext | None = None
    ) -> list[dict]: ...
    def add_board_entries(
        self, project_id: str, board: str, entries: list[dict], ctx: OpContext | None = None
    ) -> list[dict]: ...  # per entry: annotation_entries.outcome

    # Search (data_format.md §6.2): the project's reports and runs the filters name, the newest
    # *limit*, in the contract's order (``evidence_search``), the same rows on every storage.
    def search(
        self,
        project_id: str,
        filters: Filters,
        ctx: OpContext | None = None,
        *,
        limit: int = NEWEST,
    ) -> list[ResultRow]: ...

    # What the search's fields propose for the project (``evidence_search_choices``): every
    # value the storage holds of what it declares (``SEARCH_CHOICES``), never only what earlier
    # searches brought back; and a test's readings (its name compared by its fold), refused
    # (``invalid``) by a storage that records none.
    def search_choices(self, project_id: str, ctx: OpContext | None = None) -> Choices: ...
    def search_readings(
        self, project_id: str, test: str, ctx: OpContext | None = None
    ) -> list[tuple[str, str]]: ...

    def local_path(self, key: EvidenceKey, ctx: OpContext | None = None) -> Path: ...
    def sweep_incomplete(self, project_id: str, ctx: OpContext | None = None) -> list[str]: ...

    # Durable outbox fronting every store:
    on_outbox_entry: Callable[[], None] | None  # the service hooks this to schedule a push

    def pending_items(
        self, project_id: str, ctx: OpContext | None = None
    ) -> list[OutboxItem]: ...  # runs and artifacts still waiting to be pushed
    def quarantined_items(
        self, project_id: str, ctx: OpContext | None = None
    ) -> list[OutboxItem]: ...  # entries the store refused (invalid / corrupt), parked for a human
    def flush_outbox(self, ctx: OpContext | None = None) -> list[str]: ...  # pushed entry ids


# ---------------------------------------------------------------------------
# Registry — name -> class. Backends register themselves on import.
# ---------------------------------------------------------------------------

REGISTRY: dict[str, type] = {}


def register(cls: type) -> type:
    REGISTRY[cls.ID] = cls
    return cls


def endpoint_params(backend: str) -> tuple[str, ...]:
    """The params *backend* declares as its endpoint identity (``ENDPOINT_PARAMS``) —
    the fields its credential is filed under. ``()`` for a backend that signs in
    nowhere (a folder) or is not registered."""
    return tuple(getattr(REGISTRY.get(backend), "ENDPOINT_PARAMS", ()))


def endpoint_identity(spec: dict[str, Any]) -> str:
    """The identity a store built from ``spec`` would have — asked of the backend that
    composes it, never spelled elsewhere: the same string routes durable outbox
    entries. ``""`` for a backend that needs none (or is not registered)."""
    identity = getattr(REGISTRY.get(spec.get("backend", "")), "endpoint_identity", None)
    return identity(spec) if identity is not None else ""


def credential_key(spec: dict[str, Any]) -> str:
    """The key the sign-in of a store built from ``spec`` is filed under — one per
    SERVER (its address), so one sign-in serves every folder on it — asked of the
    backend, never spelled elsewhere: a key spelled by hand files a secret no store
    reads. ``""`` for a backend that signs in nowhere (or is not registered)."""
    key = getattr(REGISTRY.get(spec.get("backend", "")), "credential_key", None)
    return key(spec) if key is not None else ""


def is_server_backend(backend: str) -> bool:
    """Whether *backend* is a server: reached through a credential, which it says by
    declaring the params that form its endpoint (:func:`endpoint_params`)."""
    return bool(endpoint_params(backend))


def open_store(
    spec: dict[str, Any],
    *,
    project_root: Path,
    machine_id: Any = None,
    credentials: Callable[[str], tuple[str, str]] | None = None,
) -> EvidenceStore:
    """Build a store from a ``config.json:evidence_store`` entry
    (:func:`evidence_config.read_spec`). ``machine_id`` (value or callable) is this
    machine's identity for layouts that need one.

    ``credentials`` is a lookup ``identity -> (user, password)``, injected because no
    backend may read the settings module itself: a secret never reaches a project's
    ``config.json`` (which travels to shares and packages), so a backend that needs
    one asks this callable with its own endpoint identity. A backend whose root needs
    no credential (the filesystem one) accepts and ignores it.
    """
    backend = spec.get("backend", "")
    try:
        cls = REGISTRY[backend]
    except KeyError:
        raise EvidenceError("invalid", f"unknown evidence backend: {backend!r}") from None
    store = cls.from_config(
        spec, project_root=project_root, machine_id=machine_id, credentials=credentials
    )
    store.spec = copy.deepcopy(spec)  # what an outbox entry records to be sent here
    return store


def open_entry_store(spec: dict[str, Any], project_id: str) -> EvidenceStore:
    """The store an outbox entry recorded (its ``config.json`` entry), for
    ``project_id``, as this machine opens it (:func:`open_project_store`) — bound to no
    project's configuration, and writing only into a folder whose marker names the
    project (``requires_marker``, :func:`evidence_owner.check_owner`)."""
    store = open_project_store(spec, project_root=None)
    store.project_id, store.requires_marker = project_id, True
    return store


def open_project_store(
    spec: dict[str, Any],
    *,
    project_root: Path,
    credentials: Callable[[str], tuple[str, str]] | None = None,
) -> EvidenceStore:
    """:func:`open_store` as this machine opens a project's store: this machine's id
    (``app_settings.get_machine_id``, resolved on first use) and the credentials saved
    on it (``evidence_credentials.lookup``). ``credentials`` replaces those for a
    caller testing ones that are typed but not saved yet. Opening builds the store
    only: nothing is created, adopted or written until the store is used."""
    from . import evidence_credentials  # it imports this module

    return open_store(
        spec,
        project_root=project_root,
        machine_id=app_settings.get_machine_id,
        credentials=evidence_credentials.lookup if credentials is None else credentials,
    )
