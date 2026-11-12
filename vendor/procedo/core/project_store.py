"""Every action on a project's storage, on the library and on the bytes they hold: the interfaces.

The owner (``docs/plans/company_storage_design.md`` §6c): an interface is the complete list of
what can be done with what is stored, and the API is derived from that list. Three of them:

* :class:`ProjectSource` — a project's files, tests and releases on one storage: list and create
  projects, open one whole or by what changed since a token, read tests and releases, push
  changes (one conflict never blocks the rest), rename a test as one move, release a test, and
  fetch a binary file's bytes. A test's releases and its draft are filed under its hidden id
  (``test_format.TEST_ID``, which its ``test.json`` holds), never under its name
  (``data_format.md`` §4.3, §4.4). The calls of ``web_api_projects.md`` (``P1``–``P11``) are its
  actions on the JSON API (:mod:`project_store_api`); a folder and an FTPS or SFTP server do the
  same over a tree (:mod:`project_store_tree`), the evidence database over its rows and its
  attachment storage (:mod:`project_store_db`), each by the same rules
  (:mod:`project_store_rules`).
* :class:`LibrarySource` — the library on a server: list, download into this PC's library,
  publish from it (L1–L3; :class:`library_client.ServerLibrary` over a tree,
  :class:`library_api.ApiLibrary` over the JSON API).
* :class:`BlobStore` — one file's bytes alone, by its owner (a ``project_id`` or ``library``) and
  its sha256: the JSON API's large-file calls (B1, B2; :mod:`project_api_blobs`).

:mod:`project_actions` maps each method to the calls that perform it, and
``tests/test_project_conformance.py`` runs every action on every storage, stored then read back.

**What a project file is** (``data_format.md`` §2, §4.1): a path (``/``, relative to the project
folder, as the storage spells it), a version (``sha256:<hex>`` of its bytes) and a size. A text
document — a file under ``tests/``, ``functions/``, ``config/`` or ``scenarios/`` whose bytes are
UTF-8 (:func:`is_text`) — carries its text; every other file is fetched (:meth:`ProjectSource.
fetch`). ``bundle/`` and ``releases/`` are not files of a project here: the bundle is one object
(:class:`Bundle`, its version the sha256 of its folder packed as one archive, :func:`pack`, so
every storage gives the same bundle the same version) and the releases are records
(:class:`ReleaseRecord`), by their test's id.

Qt-free, data only.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from . import zip_safety
from .evidence_store import OpContext
from .library_entry import Entry

#: The folders whose UTF-8 files travel as text, whatever their size (``data_format.md`` §2).
TEXT_FOLDERS = ("tests", "functions", "config", "scenarios")
#: A binary file larger than this travels alone (B1, B2); one of at most this rides as a part of
#: the JSON request or answer (``data_format.md`` §3.4b; contract_questions.md P2).
PART_MAX = 1 << 20
#: The two folders of a project folder that travel as objects, never as files (§4.2).
BUNDLE_DIR = "bundle"
RELEASES_DIR = "releases"
#: A test's folder in the project.
TESTS_DIR = "tests"
FUNCTIONS_DIR = "functions"
#: The results of :meth:`ProjectSource.push_changes`, one per change.
SAVED, CHANGED, REFUSED = "saved", "changed", "refused"
#: The ``code`` of the refusal a change token gets when the storage cannot answer from it: the
#: app then reads the project whole (``410 gone``).
GONE = "gone"
#: The ``code`` of a call the server does not take: a server that stopped offering P3 answers
#: ``since`` so (``501 unsupported``, contract §6.1), and the app reads the project whole too.
UNSUPPORTED = "unsupported"
#: The date every member of a packed archive carries, so one folder packs to one archive.
_ZIP_DATE = (1980, 1, 1, 0, 0, 0)


def version_of(data: bytes) -> str:
    """A file's version: ``sha256:<hex>`` of its bytes."""
    return "sha256:" + hashlib.sha256(data).hexdigest()


def in_text_folder(path: str) -> bool:
    """Whether *path* is under one of the four folders whose UTF-8 files are text (the folder
    compared as a name: ``Tests/`` is ``tests/``)."""
    return "/" in path and path.split("/", 1)[0].casefold() in TEXT_FOLDERS


def is_text(path: str, data: bytes) -> bool:
    """Whether the file at *path* holding *data* travels as text: under one of the four text
    folders, and valid UTF-8."""
    if not in_text_folder(path):
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def pack(files: Mapping[str, bytes]) -> bytes:
    """A folder's files as one zip archive, the same bytes for the same files on every PC:
    members by path, every one dated alike, compressed alike."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        for name in sorted(files):
            member = zipfile.ZipInfo(name, date_time=_ZIP_DATE)
            member.compress_type = zipfile.ZIP_DEFLATED
            member.external_attr = 0o644 << 16
            archive.writestr(member, files[name])
    return out.getvalue()


def unpack(data: bytes) -> dict[str, bytes]:
    """The files of an archive :func:`pack` made, each member checked first
    (:func:`zip_safety.check_archive`). ``ValueError`` for an archive that is not one."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            zip_safety.check_archive(archive)
            return {
                m.filename: archive.read(m)
                for m in archive.infolist()
                if not m.filename.endswith("/")
            }
    except zipfile.BadZipFile as exc:
        raise ValueError(f"not a zip archive: {exc}") from exc


# ---------------------------------------------------------------------------
# What a storage holds
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FileRef:
    """One project file as a storage holds it: a text document with its text, or a binary file
    whose bytes :meth:`ProjectSource.fetch` brings, from the storage ``store_id`` names on a
    server that keeps files in several (``None``: its only one, or the active one)."""

    path: str
    version: str
    size: int
    text: str | None = None
    store_id: int | None = None


def file_ref(path: str, data: bytes) -> FileRef:
    """The :class:`FileRef` of *data* stored at *path*."""
    text = data.decode("utf-8") if is_text(path, data) else None
    return FileRef(path, version_of(data), len(data), text)


@dataclass(frozen=True)
class Bundle:
    """The project's ``bundle/`` folder: its files by their path in it, and its version (the
    sha256 of :func:`pack` of them)."""

    files: Mapping[str, bytes]

    @property
    def version(self) -> str:
        return version_of(pack(self.files))

    @property
    def manifest(self) -> dict[str, Any]:
        """Its ``bundle.json``, ``{}`` when it holds none that reads."""
        try:
            found = json.loads(self.files.get("bundle.json", b"{}").decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return {}
        return found if isinstance(found, dict) else {}


@dataclass(frozen=True)
class ReleaseRecord:
    """One release of a test (``data_format.md`` §4.4): filed under its test's hidden id
    (``test_id``), numbered from 1 within it by the storage, never changed afterwards; ``test``
    the name it was released under, a record. ``documents`` by their names in the test; ``None``
    in a list read without them."""

    test_id: str
    test: str
    version: int
    format: str
    fingerprint: str
    content: str
    documents: Mapping[str, bytes] | None = None
    note: str = ""
    released_at: str = ""
    released_by: str = ""
    pc: str = ""
    machine_id: str = ""
    numbered_before_releases: bool = False


@dataclass(frozen=True)
class ReleaseRequest:
    """What :meth:`ProjectSource.release` releases: the version of each project file it keeps
    (``files``: the test's documents and the ``functions/`` files they import, as the app chose
    them), with their format, fingerprint and content as ``test_release`` computes them."""

    format: str
    fingerprint: str
    content: str
    files: Mapping[str, str]
    note: str = ""
    released_by: str = ""
    pc: str = ""
    machine_id: str = ""


@dataclass(frozen=True)
class ProjectInfo:
    """A project in a storage's list, without its files."""

    project_id: str
    name: str


@dataclass(frozen=True)
class Snapshot:
    """A whole project: its files (``bundle/`` and ``releases/`` left out), its bundle, every
    test's releases with their documents by its ``test_id`` (a deleted test's included), and the
    token a later
    :meth:`ProjectSource.changes_since` starts from. ``missing`` names the files whose bytes
    the storage cannot give, by path (the bundle's archive as ``bundle/``): ``not_found`` or
    ``damaged`` (``data_format.md`` §4.2); a missing bundle reads as ``None``, and
    ``bundle_version`` still names the version the storage holds (``""``: it names none)."""

    project_id: str
    name: str
    token: str
    files: Mapping[str, FileRef]
    bundle: Bundle | None
    releases: Mapping[str, Sequence[ReleaseRecord]]
    missing: Mapping[str, str] = field(default_factory=dict)
    bundle_version: str = ""


@dataclass(frozen=True)
class Changes:
    """What changed since a token: each file changed as it is now, each path deleted, the bundle
    when it changed, and each ``test_id`` whose releases changed (one made) with its whole list
    now; ``missing`` and ``bundle_version`` as a :class:`Snapshot`'s, for
    what changed."""

    token: str
    files: Mapping[str, FileRef]
    deleted: Sequence[str]
    bundle: Bundle | None
    releases: Mapping[str, Sequence[ReleaseRecord]]
    missing: Mapping[str, str] = field(default_factory=dict)
    bundle_version: str = ""


@dataclass(frozen=True)
class TestInfo:
    """One test: its name (its folder's), its hidden id and its format, both as its ``test.json``
    holds them, its latest release under that id; its draft (``None``: not known, the app works
    it out from the files); with its files (every file under ``tests/<test>/`` and under
    ``functions/``) when read, ``None`` in a list."""

    test: str
    test_id: str
    format: str
    latest_release: int | None = None
    files: Mapping[str, FileRef] | None = None
    draft: Draft | None = None


@dataclass(frozen=True)
class Write:
    """A file written: its bytes, and the version it replaces (``None``: a new file)."""

    path: str
    data: bytes
    replaces: str | None


@dataclass(frozen=True)
class Delete:
    path: str
    replaces: str


@dataclass(frozen=True)
class BundleWrite:
    """A new bundle: the files of the ``bundle/`` folder, and the version it replaces."""

    files: Mapping[str, bytes]
    replaces: str | None


Change = Write | Delete | BundleWrite


@dataclass(frozen=True)
class Draft:
    """A test's documents' fingerprints as the app computed them when it changed them
    (``data_format.md`` §4.3), with the test's name and hidden id: kept only while that test's
    ``test.json`` holds the id. The storage never computes them."""

    test: str
    test_id: str
    fingerprint: str
    content: str
    based_on: int | None = None


@dataclass(frozen=True)
class Outcome:
    """One change's result: ``saved`` (``version`` the new one), ``changed`` (the file changed
    on the storage since the version it replaces: nothing written, ``current`` what it holds
    now) or ``refused`` (the storage's own rule, ``kind`` and ``message``). A test's move also
    lists the boards whose results moved with it (``moved_evidence``)."""

    path: str
    status: str
    version: str = ""
    current: FileRef | None = None
    kind: str = ""
    message: str = ""
    moved_evidence: tuple[dict[str, str], ...] = ()


@dataclass(frozen=True)
class Pushed:
    """:meth:`ProjectSource.push_changes`'s answer: the project's token now, one outcome per
    change in order."""

    token: str
    results: tuple[Outcome, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# The interfaces
# ---------------------------------------------------------------------------


class ProjectSource(Protocol):
    """Every action on the projects of one storage. A storage that cannot answer raises
    :class:`evidence_store.EvidenceError` with the kind the app acts on. Each call may wait on
    a server: on a worker, never on the GUI thread."""

    def list_projects(self, ctx: OpContext | None = None) -> list[ProjectInfo]:
        """Every project whose files this storage keeps, by name (case folded)."""
        ...

    def create_project(
        self,
        name: str,
        files: Mapping[str, bytes],
        bundle: Mapping[str, bytes] | None = None,
        ctx: OpContext | None = None,
    ) -> Snapshot:
        """A new project in the folder *name*: its files (``config/config.json`` holds its
        ``project_id``) and its bundle's files. Refused (``invalid``, code ``exists``) when the
        storage holds that project or that name, except the same project again."""
        ...

    def read_project(self, project_id: str, ctx: OpContext | None = None) -> Snapshot: ...

    def changes_since(self, project_id: str, token: str, ctx: OpContext | None = None) -> Changes:
        """What changed since *token*; refused with code :data:`GONE` when the storage cannot
        answer from it (the app then reads the project whole)."""
        ...

    def list_tests(self, project_id: str, ctx: OpContext | None = None) -> list[TestInfo]: ...

    def read_tests(
        self, project_id: str, names: Sequence[str], ctx: OpContext | None = None
    ) -> dict[str, TestInfo]:
        """Those tests with their files, keyed by each name as asked; a name the project holds
        no test of is left out."""
        ...

    def list_releases(
        self, project_id: str, test_id: str, ctx: OpContext | None = None
    ) -> list[ReleaseRecord]:
        """The releases filed under *test_id*, oldest first, without documents; ``[]`` for an id
        with none (a deleted test's are still there)."""
        ...

    def read_release(
        self, project_id: str, test_id: str, version: int, ctx: OpContext | None = None
    ) -> ReleaseRecord:
        """One release under *test_id* with its documents; ``not_found`` for a version the id
        does not hold."""
        ...

    def push_changes(
        self,
        project_id: str,
        changes: Sequence[Change],
        drafts: Sequence[Draft] = (),
        ctx: OpContext | None = None,
    ) -> Pushed:
        """Each change all-or-nothing for its path, the request as a whole not a transaction,
        checked in the order sent against the project as it is then. A change after which a test
        folder holding a file holds no ``test.json`` with a valid ``test_id``, or two tests hold
        one, is refused alone (kind ``invalid``): a new test's ``test.json`` goes first, a
        deleted test's last. A test whose last file goes is deleted: its releases stay under its
        id. A draft is kept only while its test's ``test.json`` holds the draft's ``test_id``."""
        ...

    def rename_test(
        self,
        project_id: str,
        old: str,
        new: str,
        folder: Mapping[str, str],
        ctx: OpContext | None = None,
    ) -> Outcome:
        """``tests/<old>`` moved to ``tests/<new>`` in one step, *folder* naming every file it
        holds (its path in the folder → its version). Its ``test.json`` moves with it, so it
        keeps its id and every release and run filed under it: nothing else moves. ``changed``
        when the folder holds other files or the new one any."""
        ...

    def release(
        self, project_id: str, test_id: str, request: ReleaseRequest, ctx: OpContext | None = None
    ) -> ReleaseRecord:
        """A release of the test whose ``test.json`` holds *test_id*, recording its name now:
        the next number, one more than the id's highest. Refused: an id no test holds
        (``not_found``), a file named that changed (code ``changed``), a content a release under
        the id has (code ``same_as_release``)."""
        ...

    def import_release(
        self, project_id: str, record: ReleaseRecord, ctx: OpContext | None = None
    ) -> ReleaseRecord:
        """A release landed AS IT IS (``data_format.md`` §6.10): its number, when and by whom
        it was made, the name it was released under and its documents whole, filed under its
        ``test_id`` whether or not a test holds that id now. What a move into the storage uses,
        so a project's releases are the same numbers and dates everywhere. The same release
        again (its ``content`` and ``fingerprint``) is answered as it is; another under its
        number is refused (code ``changed``)."""
        ...

    def fetch(
        self, project_id: str, ref: FileRef, dest: Path, ctx: OpContext | None = None
    ) -> Path:
        """The bytes of one file the project holds, written to *dest*."""
        ...


class LibrarySource(Protocol):
    """Every action on the library of one server: what this PC's library dialog does."""

    def list(self, kind: str, ctx: OpContext | None = None) -> list[Entry]:
        """Every ``<id>@<version>`` of *kind* the server publishes."""
        ...

    def download(
        self,
        kind: str,
        item_id: str,
        version: str,
        ctx: OpContext | None = None,
        *,
        into: Path | None = None,
    ) -> Path:
        """``<id>@<version>`` into this PC's library (*into*: ``<into>/<id>/<version>``, which
        no discovery scans): its folder. The same content here already is no download; other
        content under that name is a conflict, nothing written."""
        ...

    def publish(self, kind: str, item_id: str, version: str, ctx: OpContext | None = None) -> Entry:
        """This PC's ``<id>@<version>`` onto the server, once: a version there already is
        refused whatever it holds (``library_client.LibraryConflict``), nothing written."""
        ...


class BlobStore(Protocol):
    """One file's bytes alone, by its owner (a ``project_id``, or ``library``) and version."""

    def upload(self, owner: str, data: bytes, ctx: OpContext | None = None) -> str:
        """The bytes stored with *owner*; their version."""
        ...

    def download(
        self, owner: str, version: str, size: int, dest: Path, ctx: OpContext | None = None
    ) -> Path:
        """The bytes of *version* (*size* long) written to *dest*; a broken download resumes
        where it stopped on the next attempt."""
        ...
