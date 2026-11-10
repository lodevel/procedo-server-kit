"""Everything the reference server holds, in memory, behind one lock.

Each request runs whole under ``State.lock`` and checks everything before it changes
anything, so a request either all happens or none of it does. Only bytes live on disk
(``blobs.BlobStore``): the ``api`` storage's, and those of the other storages the server
reaches: an own ``filesystem`` storage its administrator set (§7.1, ``own_storage``), and a
declared one whose folder lies in a folder it mounts (§7.3, ``reaches``).
"""

from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import faults, offer
from .blobs import BlobStore
from .validate import name_key, nfc

API_STORE_ID = 1
OWN_STORE_ID = 2  # the own ``filesystem`` storage, when the administrator set one
MARKER = "procedo-attachments.json"  # a storage's marker (§7.2)
KNOWN_ROLES = ("bench", "publisher", "importer", "manager", "reader")

# A board is (part number key, revision key, serial key); None is "none" (§4.11).
BoardKey = tuple


@dataclass
class User:
    name: str
    roles: frozenset[str]


@dataclass
class StoredRun:
    run_id: str
    obj: dict  # the Run as sent, less the answer fields and `import`
    raw_sha: str  # sha256 of the JSON's bytes: a replay is the same bytes (§6.1)
    pushed_at: str
    board_key: BoardKey
    test_id: str  # the test it is filed under (§4.5)
    deleted: dict | None = None  # the deleted_* fields (§4.5)
    entries: list[dict] = field(default_factory=list)  # the annotation's entries, as received


@dataclass
class Board:
    key: BoardKey
    serial: str
    part_number: str | None
    board_revision: str | None
    created_at: str
    entries: list[dict] = field(default_factory=list)  # lot and field entries, as received


@dataclass
class ArtifactItem:
    obj: dict  # the Artifact as sent, less the answer fields (generation set by the server)


@dataclass
class ArtifactSlot:
    """Everything under one kind + board + name: a report's generations, or one draft."""

    kind: str
    board_key: BoardKey
    name: str  # the first spelling met, in NFC
    items: list[ArtifactItem] = field(default_factory=list)  # by generation, oldest first
    deleted: dict | None = None

    def latest(self) -> ArtifactItem:
        return self.items[-1]


@dataclass
class StoredFile:
    """One project file (§4.1): its path as first spelled, in NFC, and its File."""

    path: str
    obj: dict  # version, size, mtime, then text or store_id; unknown fields kept


@dataclass
class Project:
    project_id: str
    name: str | None
    created_at: str
    runs: dict[str, StoredRun] = field(default_factory=dict)
    boards: dict[BoardKey, Board] = field(default_factory=dict)
    pointers: dict[tuple[BoardKey, str], dict] = field(default_factory=dict)  # by test_id
    artifacts: dict[tuple[str, BoardKey, str], ArtifactSlot] = field(default_factory=dict)
    purged: set[str] = field(default_factory=set)  # run_ids purged: a push of one is refused
    # Its files (P1-P11). A project P11 never created is known by its evidence only: its
    # files live elsewhere, and P2-P10 answer 404 for it (data_format.md §5.1).
    held: bool = False
    path: str | None = None  # the server's folder name for it (P1)
    created_by: str | None = None
    updated: str | None = None
    extra: dict = field(default_factory=dict)  # the Project's fields this server does not know
    files: dict[str, StoredFile] = field(default_factory=dict)  # by path key
    bundle: dict | None = None
    drafts: dict[str, dict] = field(default_factory=dict)  # test key -> Draft (with test_id)
    releases: dict[str, list[dict]] = field(default_factory=dict)  # test_id -> oldest first
    test_updated: dict[str, str] = field(default_factory=dict)  # test key -> its last change
    token: int = 0  # the change token (rule 9): one more at each request that changes the files
    log: list[tuple[int, tuple]] = field(default_factory=list)  # (token, what changed)
    log_floor: int = 0  # the oldest token ``since`` still answers; older is 410 gone


def board_key(part_number: str | None, board_revision: str | None, serial: str) -> BoardKey:
    return (
        name_key(part_number) if part_number else None,
        name_key(board_revision) if board_revision else None,
        name_key(serial),
    )


def parse_tokens(tokens: dict) -> dict[str, User]:
    """``{token: [roles]}`` or ``{token: {"name": ..., "roles": [...]}}``."""
    users = {}
    for token, spec in tokens.items():
        if isinstance(spec, dict):
            roles = spec.get("roles", [])
            name = spec.get("name")
        else:
            roles = [spec] if isinstance(spec, str) else list(spec)
            name = None
        unknown = set(roles) - set(KNOWN_ROLES)
        if unknown:
            raise ValueError(f"unknown roles {sorted(unknown)} for a token")
        users[token] = User(
            name=name or "+".join(sorted(roles)) or "nobody", roles=frozenset(roles)
        )
    return users


class State:
    def __init__(
        self,
        root: Path,
        tokens: dict,
        *,
        max_upload_bytes: int,
        retention_days: int,
        max_file_bytes: int = 256 * 1024 * 1024,
        instance_id: str | None,
        max_changes: int = 10000,
        own_storage: Path | None = None,
        reaches: tuple[Path, ...] = (),
        body_timeout: float = 60.0,
        calls: Iterable[str] | None = None,
        people: list[dict] | None = None,
        boards: dict[str, list[dict]] | None = None,
        unnamed_refused: bool = False,
    ) -> None:
        self.lock = threading.RLock()
        self.users = parse_tokens(tokens)
        # The server's users U1 lists (§6.8), and the optional calls it offers (§6.1).
        self.people = offer.check_users(people)
        self.calls = offer.chosen(calls, self.people)
        #: With U1, a run or a release naming nobody is refused (§6.8: the server's choice).
        self.unnamed_refused = unnamed_refused
        self.blobs = BlobStore(root / "api_storage")
        self.max_upload_bytes = max_upload_bytes  # a JSON request, its parts included
        self.max_file_bytes = max_file_bytes  # one large file sent alone (B1, §3.4b)
        self.retention_days = retention_days
        self.instance_id = instance_id or str(uuid.uuid4())
        self.token_prefix = self.instance_id.replace("-", "")[:8]
        self.max_changes = max_changes  # the changes a project keeps for ``since``
        self.projects: dict[str, Project] = {}
        self.library: dict[tuple[str, str, str], dict] = {}  # (kind, id, version) -> item
        self.storages: dict[int, dict] = {
            API_STORE_ID: {
                "format_version": 1,
                "store_id": API_STORE_ID,
                "backend": "api",
                "params": {},
                "identity": "api",
                "active": True,
                "declared": False,
                "reachable": True,
            }
        }
        # the blobs of every storage but ``api`` this server reaches, by store_id
        self.reached: dict[int, BlobStore] = {}
        self.reaches = tuple(Path(folder).resolve() for folder in reaches)
        self.body_timeout = body_timeout  # a body stalled this long is ``408`` (§4.3)
        if own_storage is not None:
            self._add_own_storage(Path(own_storage))
        # declared storage -> project_id -> sha256 hex nothing in that project references (S3)
        self.to_delete: dict[int, dict[str, set[str]]] = {}
        self.benches: dict[str, dict] = {}
        self.clock = lambda: datetime.now(timezone.utc)
        # §3.3 boards_registered: the boards its administrator registered, by project; a run on
        # any other is refused (E1). None: any serial run becomes a board.
        self.registered = boards is not None
        #: The boards registered, by project (an import's new board joins them): E4 lists
        #: exactly these (``registration``).
        self.registered_boards: dict[str, set] = {}
        #: The fault of 3.3.listed-taken only: boards E4 lists that are not registered.
        self.listed_only: dict[str, set] = {}
        for project_id, held in (boards or {}).items():
            project = self.ensure_project(project_id, None)
            for board in held:
                made = self.ensure_board(
                    project, board["serial"], board.get("part_number"), board.get("board_revision")
                )
                # the fault of 3.3.listed-taken: listed by E4, yet not registered
                kept = self.listed_only if faults.on("3.3.listed-taken") else self.registered_boards
                kept.setdefault(project_id, set()).add(made.key)

    def _add_own_storage(self, root: Path) -> None:
        """An own ``filesystem`` storage (§7.1): marked for this server (§7.2), reached, never
        active, its references checked and its unreferenced blobs reclaimed."""
        self.reached[OWN_STORE_ID] = BlobStore(root)
        marker = {"schema_version": 1, "instance_id": self.instance_id}
        (root / MARKER).write_text(json.dumps(marker), encoding="utf-8")
        self.storages[OWN_STORE_ID] = {
            "format_version": 1,
            "store_id": OWN_STORE_ID,
            "backend": "filesystem",
            "params": {"root": str(root)},
            "identity": str(root),
            "active": False,
            "declared": False,
            "reachable": True,
        }

    def reaches_folder(self, root: object) -> bool:
        """Whether a declared ``filesystem`` storage's *root* lies in a folder this server
        mounts (``reaches``) and is there."""
        if not isinstance(root, str) or not root:
            return False
        path = Path(root)
        if not path.is_absolute() or not path.is_dir():
            return False
        resolved = path.resolve()
        return any(resolved == folder or folder in resolved.parents for folder in self.reaches)

    def broken(self, rule: str) -> bool:
        """Whether this server breaks *rule* on purpose (:mod:`faults`): never, unless told."""
        return faults.on(rule)

    # ---- Time ------------------------------------------------------------------------------

    def now(self) -> str:
        moment = self.clock()
        if faults.on("3.7.G3-one-clock"):
            moment += timedelta(hours=1)
        return moment.strftime("%Y-%m-%dT%H:%M:%SZ")

    def now_ms(self) -> str:
        moment = self.clock()
        return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"

    # ---- Projects, boards, tests, benches ----------------------------------------------------

    def ensure_project(self, project_id: str, name: str | None) -> Project:
        project = self.projects.get(project_id)
        if project is None:
            project = Project(project_id=project_id, name=name, created_at=self.now())
            self.projects[project_id] = project
        return project

    def ensure_board(
        self, project: Project, serial: str, part_number: str | None, board_revision: str | None
    ) -> Board:
        key = board_key(part_number, board_revision, serial)
        board = project.boards.get(key)
        if board is None:
            board = Board(
                key=key,
                serial=nfc(serial),
                part_number=nfc(part_number) if part_number else None,
                board_revision=nfc(board_revision) if board_revision else None,
                created_at=self.now(),
            )
            project.boards[key] = board
        return board

    def seen_bench(self, machine_id: str, label: str | None = None) -> None:
        now = self.now()
        bench = self.benches.get(machine_id)
        if bench is None:
            bench = {"format_version": 1, "machine_id": machine_id, "first_seen": now}
            self.benches[machine_id] = bench
        bench["last_seen"] = now
        if label and not (self.broken("5.14.label") and "label" in bench):
            bench["label"] = label  # the latest station named; none named leaves it (§5.14)

    def storage_of(self, store_id: int) -> dict | None:
        return self.storages.get(store_id)
