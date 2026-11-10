"""The project storage's conformance suite: every action of :class:`project_store.ProjectSource`,
stored then read back through the interface, on every storage (``docs/plans/
company_storage_design.md`` §6c), as ``test_evidence_conformance.py`` does for the evidence.

A project storage carries what the evidence does not: the project's binaries (the board
archive, ``documents/``, a bundle of wheels) next to its text documents. Each case stores
through one bench and reads back through another, and compares everything: every file byte
for byte (a text document as its text, a binary one fetched), the bundle's files, the releases
with their documents, which change was saved, which met a conflict and what it found.

The storages: the local folder here, a real FTPS server in ``test_project_conformance_ftps.py``,
a real SFTP server in ``test_project_conformance_sftp.py``, the evidence database (PostgreSQL and
its attachment storage) in ``test_project_conformance_db.py``, and the JSON API's client against the reference server here (``tests/reference_server``, built from the contract alone), or the server the environment names (``server_target``). The files are
chosen so the JSON API moves every kind both ways: text inside the JSON (one over 1 MiB), a
binary of at most 1 MiB as a part, binaries over 1 MiB alone (B1 up, B2 down).

* **Coverage by introspection**: every action of the interface has a case
  (:func:`test_every_action_has_a_case`), and a case names exactly the actions it calls
  (checked as it runs).
* **Every storage but the folder starts a server** (marker ``servers``): run with
  ``--servers``. Over the JSON API each call an action makes is one the action map names for it
  (:data:`project_actions.ACTIONS`).
* **A storage that cannot do a case yet fails**, never skips: :data:`KNOWN_GAPS` is the one
  table of what a storage cannot do yet, each a strict xfail that turns into a failure the day
  it works, until its line is removed.
* **A test's hidden id** (``data_format.md`` §2, §4.3): every test folder holds a ``test.json``
  with its ``test_id``; its releases are filed under that id, never under its name (§4.4, §6.9).
  The cases about it are written from the contract alone, as the reference server was.
* **Each case its own project** over the JSON API: its id and name are new (:func:`own_project`),
  so a server holding other projects answers it as an empty one would, a list read back
  filtered to the case's own (:func:`_listed`).
* **Every path that never travels** (``data_format.md`` §4.2, P11) is refused by a new project
  and by a push, one case per path :mod:`never_travels` generates from the app's own exclusion
  list: a name the app adds there is checked here with no change.
"""

from __future__ import annotations

import hashlib
import json
import threading
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import never_travels
import pytest
import server_target

from procedo.core import (
    project_sync,
    project_sync_api,
    project_sync_release,
    test_format,
    test_release,
)
from procedo.core.api_session import ApiSession
from procedo.core.evidence_actions import DISCOVERY
from procedo.core.evidence_api_http import ApiError
from procedo.core.evidence_store import EvidenceError
from procedo.core.evidence_store_fs_shared import FsTree
from procedo.core.project_actions import ACTIONS
from procedo.core.project_store import (
    CHANGED,
    GONE,
    PART_MAX,
    REFUSED,
    SAVED,
    BundleWrite,
    Delete,
    Draft,
    ProjectInfo,
    ReleaseRecord,
    ReleaseRequest,
    Write,
    version_of,
)
from procedo.core.project_store_api import ApiProjectSource
from procedo.core.project_store_tree import TreeProjectSource

PID = "7e1f0a52-1111-4000-8000-00000000c0de"
OTHER = "7e1f0a52-2222-4000-8000-00000000c0de"
NAME = "Ripple bench"
PROTOCOL = frozenset(name.split(".", 1)[1] for name in ACTIONS if name.startswith("project."))
BACKENDS = ("folder", "ftps", "sftp", "api", "db")
#: Tests' hidden ids (``data_format.md`` §2): lower-case UUIDs, one per test of a project.
T1_ID = "3f9c2a1e-7b4d-4e8a-9c61-2d5b8e0f4a17"
T2_ID = "8d02e6b1-5c3a-4f19-b7e2-90a4c6d1e358"
C1_ID = "c1a2b3c4-d5e6-4f70-8a91-b2c3d4e5f607"
S1_ID = "51d0e7a3-9b2c-4d18-a6f5-03e4b7c9d2a1"
E1_ID = "e1f2a3b4-c5d6-4e7f-8091-a2b3c4d5e6f7"
NEW_ID = "4b7e9d21-0c3f-4a56-8e1b-d7f2c6a9b304"
NEVER_ID = "00000000-0000-4000-8000-0000000000ff"


def the_test_json(test_id: str, active: str = "text") -> bytes:
    """A test's ``test.json``: its format and its hidden id (``data_format.md`` §4.3)."""
    return json.dumps({"active": active, "test_id": test_id}).encode() + b"\n"


#: What a storage cannot do yet, by (backend, action): each case naming the action is a strict
#: xfail on that backend, with this reason. Strict: the day it works, the case fails until the
#: line is removed. A gap that only one case's check meets is keyed by (backend, case) instead.
#: Every storage does every action today.
KNOWN_GAPS: dict[tuple[str, str], str] = {}


def noise(seed: str, size: int) -> bytes:
    """*size* bytes nothing compresses, the same for the same seed."""
    out = bytearray()
    block = hashlib.sha256(seed.encode()).digest()
    while len(out) < size:
        block = hashlib.sha256(block).digest()
        out += block
    return bytes(out[:size])


CONFIG = json.dumps({"project_id": PID}).encode()
BIG_TEXT = ("# Notes\n" + "Étape de mesure: tension et courant.\n" * 40000).encode("utf-8")
FILES: dict[str, bytes] = {
    "config/config.json": CONFIG,
    "config/logo.png": b"\x89PNG\r\n\x1a\n" + noise("logo", 2000),  # binary under a text folder
    "tests/T1/test.json": the_test_json(T1_ID),
    "tests/T1/procedure.json": b'{"id": "T1", "steps": []}',
    "tests/T1/test.py": b"print('T1')\n",
    "tests/T1/procedure_text.md": "# T1\nMesure: 12 V ± 5 %\n".encode(),
    "tests/T1/notes.md": BIG_TEXT,  # text over 1 MiB: still inside the JSON
    "functions/helpers.py": b"def helper():\n    return 1\n",
    "documents/manual.pdf": b"%PDF-1.4\n" + noise("manual", 40000),  # a part
    "documents/board.pdf": b"%PDF-1.4\n" + noise("board", PART_MAX + 10),  # alone
    "AX-2210.tgz": noise("odb", 3 * PART_MAX),  # the board archive, alone
}
BUNDLE: dict[str, bytes] = {
    "bundle.json": json.dumps({"id": "baseline", "version": "3.1.0", "packs": []}).encode(),
    "defaults.json": b"{}",
    "rules/doc.md": b"# rules\n",
    "wheels/labscpi-1.0-py3-none-any.whl": noise("wheel", 2 * PART_MAX),
}
assert len(BIG_TEXT) > PART_MAX


# ---------------------------------------------------------------------------
# The registry: each case and the actions it calls
# ---------------------------------------------------------------------------


@dataclass
class Storage:
    """One storage under test. :meth:`open` is one bench's view of it; every action a case
    calls is noted in ``called``."""

    name: str
    opener: Callable[[], Any]
    called: set[str] = field(default_factory=set)
    #: A bench's view signed in with the login that moves data in (the roles bench and
    #: importer, §3.5), where the storage tells logins apart (the API); None: :meth:`open`'s.
    mover: Callable[[], Any] | None = None

    def open(self) -> Any:
        return self._noted(self.opener())

    def open_mover(self) -> Any:
        """A view that may import (P10 with ``"import": true``)."""
        return self._noted((self.mover or self.opener)())

    def _noted(self, source: Any) -> Any:
        for action in PROTOCOL:
            setattr(source, action, self._noting(action, getattr(source, action)))
        return source

    def _noting(self, action: str, method: Callable[..., Any]) -> Callable[..., Any]:
        def call(*args: Any, **kwargs: Any) -> Any:
            self.called.add(action)
            return method(*args, **kwargs)

        return call


@dataclass(frozen=True)
class Case:
    check: Callable[[Storage, Path], None]
    actions: frozenset[str]
    #: The contract rules it catches (``procedo_server_contract.md``'s ``rule-<id>`` anchors).
    rules: tuple[str, ...] = ()

    @property
    def name(self) -> str:
        return self.check.__name__


CASES: list[Case] = []


def conformance(*actions: str, rules: tuple[str, ...] = ()) -> Callable:
    def register(check: Callable[[Storage, Path], None]) -> Callable[[Storage, Path], None]:
        CASES.append(Case(check, frozenset(actions), tuple(rules)))
        return check

    return register


def params(backend: str, *marks: Any) -> list:
    """``(case, backend)`` for every case; a known gap of the backend makes it a strict xfail."""
    out = []
    for case in CASES:
        gaps = sorted(a for a in (*case.actions, case.name) if (backend, a) in KNOWN_GAPS)
        why = "; ".join(f"{backend} cannot {a} yet: {KNOWN_GAPS[backend, a]}" for a in gaps)
        xfail = [pytest.mark.xfail(strict=True, reason=why)] if gaps else []
        out.append(pytest.param(case, backend, marks=[*marks, *xfail], id=f"{case.name}-{backend}"))
    return out


def run_case(case: Case, storage: Storage, tmp_path: Path) -> None:
    case.check(storage, tmp_path)
    assert sorted(storage.called - case.actions) == [], f"{case.name} calls these unnamed"
    assert sorted(case.actions - storage.called) == [], f"{case.name} names these, never calls"


# ---------------------------------------------------------------------------
# What is stored, and how it reads back
# ---------------------------------------------------------------------------


def _create(source: Any, files: dict[str, bytes] = FILES, bundle: dict | None = BUNDLE) -> Any:
    return source.create_project(NAME, files, bundle)


def _bytes_of(source: Any, ref: Any, tmp_path: Path) -> bytes:
    """A file's bytes as the storage gives them: its text, or fetched."""
    if ref.text is not None:
        return ref.text.encode("utf-8")
    dest = tmp_path / "fetched" / hashlib.sha256(ref.path.encode()).hexdigest()
    return source.fetch(PID, ref, dest).read_bytes()


def _same_files(source: Any, refs: dict, sent: dict[str, bytes], tmp_path: Path) -> None:
    assert sorted(refs) == sorted(sent)
    for path, ref in refs.items():
        assert (ref.version, ref.size) == (version_of(sent[path]), len(sent[path])), path
        assert _bytes_of(source, ref, tmp_path) == sent[path], path


def _versions(snapshot: Any, *paths: str) -> dict[str, str]:
    return {p: snapshot.files[p].version for p in paths}


def _docs(test: str = "T1", files: dict[str, bytes] = FILES) -> tuple[dict[str, bytes], dict]:
    """A text test's documents as a release keeps them, and the project paths it names."""
    named = {
        f"tests/{test}/procedure.json": "procedure.json",
        f"tests/{test}/test.py": "test.py",
        f"tests/{test}/procedure_text.md": "procedure_text.md",
        "functions/helpers.py": "functions/helpers.py",
    }
    return {name: files[path] for path, name in named.items()}, named


def _request(test: str = "T1", files: dict[str, bytes] = FILES, note: str = "") -> ReleaseRequest:
    docs, named = _docs(test, files)
    fmt = test_format.TEXT
    return ReleaseRequest(
        format=fmt,
        fingerprint=test_release.fingerprint_of(fmt, docs),
        content=test_release.content_of(docs),
        files={path: version_of(files[path]) for path in named},
        note=note,
        released_by="alice",
        pc="BENCH-1",
        machine_id="0000a001",
    )


def _code(call: Callable[..., Any], *args: Any) -> str:
    with pytest.raises(EvidenceError) as ei:
        call(*args)
    return ei.value.code


def _kind(call: Callable[..., Any], *args: Any) -> str:
    with pytest.raises(EvidenceError) as ei:
        call(*args)
    return ei.value.kind


def _test_files(test: str, files: dict[str, bytes] = FILES) -> dict[str, bytes]:
    return {p: d for p, d in files.items() if p.startswith(f"tests/{test}/")}


def _renamed(files: dict[str, bytes], old: str, new: str) -> dict[str, bytes]:
    """*files* with the test folder *old* named *new*, its bytes as they are."""
    return {p.replace(f"tests/{old}/", f"tests/{new}/", 1): d for p, d in files.items()}


def _delete_test(source: Any, test: str) -> list[str]:
    """Every file of the test removed in one push, its ``test.json`` last (``data_format.md``
    §4.3: the app deletes it after the other files)."""
    files = source.read_project(PID).files
    mine = sorted(
        (p for p in files if p.startswith(f"tests/{test}/")), key=lambda p: p.endswith("/test.json")
    )
    gone = [Delete(p, files[p].version) for p in mine]
    return [r.status for r in source.push_changes(PID, gone).results]


def _folder(source: Any, test: str) -> dict[str, str]:
    files = source.read_project(PID).files
    prefix = f"tests/{test}/"
    return {p[len(prefix) :]: ref.version for p, ref in files.items() if p.startswith(prefix)}


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------


def _listed(source: Any) -> list[ProjectInfo]:
    """The projects *source* lists that are this case's: on a server the environment names,
    the ones under its id or name; every one on a storage of its own."""
    listed = source.list_projects()
    if not server_target.external():
        return listed
    names = {NAME.casefold()}
    return [p for p in listed if p.project_id in (PID, OTHER) or p.name.casefold() in names]


@conformance("list_projects")
def an_empty_storage_lists_no_project(s: Storage, tmp_path: Path) -> None:
    server_target.local_only("it needs an empty server")
    assert s.open().list_projects() == []


@conformance(
    "create_project", "list_projects", "read_project", "fetch",
    rules=(
        "2.9.text-or-reference", "5.1.file", "5.2.project-answer",
        "6.2.multipart-form", "6.2.large-in-answer", "P1.list",
        "P11.create", "B1.upload", "B2.download",
    ),
)  # fmt: skip
def a_project_created_reads_back_byte_for_byte(s: Storage, tmp_path: Path) -> None:
    """Every file, the bundle's files and no release, as they went in, through another bench."""
    made = _create(s.open())
    reader = s.open()
    assert _listed(reader) == [ProjectInfo(PID, NAME)]
    read = reader.read_project(PID)
    assert (read.project_id, read.name, read.releases) == (PID, NAME, {})
    # A change token, but "" from a server without P3 (§6.3, P2).
    session = getattr(reader, "session", None)
    assert bool(read.token) is (session is None or "P3" in session.offered()), read.token
    _same_files(reader, dict(read.files), FILES, tmp_path)
    assert dict(read.bundle.files) == BUNDLE and read.bundle.version == made.bundle.version
    # A text document travels as its text whatever its size; a binary one never, even there.
    assert read.files["tests/T1/notes.md"].text == BIG_TEXT.decode("utf-8")
    assert read.files["config/logo.png"].text is None
    assert sorted(made.files) == sorted(FILES)


@conformance(
    "create_project", "list_projects",
    rules=("5.2.project-name-unique", "P11.exists", "P11.replay"),
)  # fmt: skip
def creating_it_again_is_a_replay_and_another_under_its_id_or_name_is_refused(
    s: Storage, tmp_path: Path
) -> None:
    source = s.open()
    _create(source)
    assert _create(s.open()).project_id == PID  # the same request again
    other = {**FILES, "config/config.json": json.dumps({"project_id": OTHER}).encode()}
    assert _code(source.create_project, NAME.upper(), other, None) == "exists"  # the name
    changed = {**FILES, "tests/T1/test.py": b"print('changed')\n"}
    assert _code(source.create_project, "Elsewhere", changed, None) == "exists"  # the id
    assert _listed(source) == [ProjectInfo(PID, NAME)]


@conformance(
    "create_project", "read_project", "push_changes", "changes_since", "fetch",
    rules=("6.2.text-once", "P3.changes", "P9.results"),
)  # fmt: skip
def changes_since_a_token_bring_what_moved_and_nothing_else(s: Storage, tmp_path: Path) -> None:
    writer, reader = s.open(), s.open()
    _create(writer)
    before = reader.read_project(PID)
    new_odb = noise("odb v2", 3 * PART_MAX + 7)
    new_bundle = {**BUNDLE, "rules/doc.md": b"# rules, edited\n"}
    del new_bundle["wheels/labscpi-1.0-py3-none-any.whl"]
    pushed = writer.push_changes(
        PID,
        [
            Write(
                "tests/T1/test.py",
                b"print('T1, edited')\n",
                before.files["tests/T1/test.py"].version,
            ),
            Write("tests/T2/test.json", the_test_json(T2_ID), None),  # a new test's, first
            Write("tests/T2/procedure.json", b'{"id": "T2"}', None),
            Write("AX-2210.tgz", new_odb, before.files["AX-2210.tgz"].version),
            Delete("documents/manual.pdf", before.files["documents/manual.pdf"].version),
            BundleWrite(new_bundle, before.bundle.version),
        ],
    )
    assert [r.status for r in pushed.results] == [SAVED] * 6
    assert pushed.results[3].version == version_of(new_odb)
    moved = reader.changes_since(PID, before.token)
    assert sorted(moved.files) == [
        "AX-2210.tgz",
        "tests/T1/test.py",
        "tests/T2/procedure.json",
        "tests/T2/test.json",
    ]
    assert list(moved.deleted) == ["documents/manual.pdf"]
    assert dict(moved.bundle.files) == new_bundle and moved.releases == {}
    assert _bytes_of(reader, moved.files["AX-2210.tgz"], tmp_path) == new_odb
    assert moved.files["tests/T1/test.py"].text == "print('T1, edited')\n"
    again = reader.changes_since(PID, moved.token)
    assert (dict(again.files), list(again.deleted), again.bundle) == ({}, [], None)


@conformance("create_project", "changes_since", rules=("P3.gone",))
def a_token_the_storage_cannot_answer_from_is_gone(s: Storage, tmp_path: Path) -> None:
    source = s.open()
    _create(source)
    assert _code(source.changes_since, PID, "not a token of this storage") == GONE


@conformance("create_project", "read_project", "push_changes")
def a_write_over_a_file_changed_meanwhile_is_changed_and_the_rest_still_applies(
    s: Storage, tmp_path: Path
) -> None:
    first, second = s.open(), s.open()
    _create(first)
    seen = second.read_project(PID)
    old = seen.files["tests/T1/test.py"].version
    theirs = b"print('theirs')\n"
    assert (
        first.push_changes(PID, [Write("tests/T1/test.py", theirs, old)]).results[0].status == SAVED
    )
    mine = second.push_changes(
        PID,
        [
            Write("tests/T1/test.py", b"print('mine, longer')\n", old),
            Write("functions/more.py", b"X = 2\n", None),
            Delete("documents/board.pdf", version_of(b"not what is there")),
        ],
    )
    statuses = [(r.path, r.status) for r in mine.results]
    assert statuses == [
        ("tests/T1/test.py", CHANGED),
        ("functions/more.py", SAVED),
        ("documents/board.pdf", CHANGED),
    ]
    current = mine.results[0].current
    assert (current.version, current.text) == (version_of(theirs), theirs.decode())
    assert mine.results[2].current.version == version_of(FILES["documents/board.pdf"])
    # A replay of what is saved is saved, whatever it says it replaces; a delete of what is
    # gone too.
    replay = second.push_changes(
        PID, [Write("functions/more.py", b"X = 2\n", None), Write("tests/T1/test.py", theirs, old)]
    )
    assert [r.status for r in replay.results] == [SAVED, SAVED]
    read = second.read_project(PID)
    gone = second.push_changes(
        PID, [Delete("functions/more.py", read.files["functions/more.py"].version)] * 2
    )
    assert [r.status for r in gone.results] == [SAVED, SAVED]


@conformance("create_project", "push_changes", "read_project")
def a_path_that_never_travels_as_a_file_is_refused(s: Storage, tmp_path: Path) -> None:
    source = s.open()
    _create(source)
    pushed = source.push_changes(
        PID,
        [
            Write("results/SN1/run.json", b"{}", None),
            Write("tests/T1/__pycache__/x.pyc", b"\x00", None),
            Write("tests/T1/extra.md", b"kept\n", None),
        ],
    )
    assert [(r.status, r.kind) for r in pushed.results] == [
        (REFUSED, "invalid"),
        (REFUSED, "invalid"),
        (SAVED, ""),
    ]
    assert sorted(source.read_project(PID).files) == sorted([*FILES, "tests/T1/extra.md"])


@conformance("create_project", "read_project", "push_changes", rules=("2.10.path-case",))
def a_name_compares_by_its_nfc_form_and_folded_case(s: Storage, tmp_path: Path) -> None:
    """A write under another case replaces the file and keeps its spelling; a new file takes its
    folders' spelling; a name typed in another Unicode form is the same name."""
    source = s.open()
    files = {
        **FILES,
        "tests/Étape/test.json": the_test_json(E1_ID),
        "tests/Étape/procedure.json": b'{"id": "\\u00c9tape"}',
    }
    _create(source, files)
    read = source.read_project(PID)
    nfd = unicodedata.normalize("NFD", "tests/étape/procedure.json")
    pushed = source.push_changes(
        PID,
        [
            Write(
                "TESTS/t1/TEST.PY", b"print('T1 again')\n", read.files["tests/T1/test.py"].version
            ),
            Write("tests/t1/new.md", b"new\n", None),
            Write(nfd, b'{"id": "edited"}', read.files["tests/Étape/procedure.json"].version),
        ],
    )
    assert [r.status for r in pushed.results] == [SAVED, SAVED, SAVED]
    after = source.read_project(PID).files
    assert after["tests/T1/test.py"].text == "print('T1 again')\n"
    assert after["tests/T1/new.md"].text == "new\n"
    assert after["tests/Étape/procedure.json"].text == '{"id": "edited"}'
    assert sorted(after) == sorted([*files, "tests/T1/new.md"])


# ---------------------------------------------------------------------------
# Tests and releases: a test is its hidden id (data_format.md §2, §4.3, §4.4, §6.9)
# ---------------------------------------------------------------------------

CODE_TEST = {
    "tests/C1/test.json": the_test_json(C1_ID, "code"),
    "tests/C1/openhtf/test.py": b"import openhtf\n",
    "tests/C1/procedure.json": b'{"id": "C1"}',
}
#: A test.json naming ``code`` beside a procedure text: its format is what test.json says.
SAID = {"tests/S1/test.json": the_test_json(S1_ID, "code"), "tests/S1/procedure_text.md": b"# S1\n"}
#: The storages that keep a draft sent with a change. A folder storage's draft is its files: it
#: answers none, "not known", and the app works it out from them (data_format.md §4.3, Test).
KEEPS_DRAFTS = frozenset({"api", "db"})


@conformance(
    "create_project", "list_tests", "read_tests",
    rules=(
        "2.5.test-id-from-test-json", "5.3.format-from-test-json", "5.3.test-files-with-functions",
        "6.1.item-not-held-404", "P5.keyed-as-asked",
    ),
)  # fmt: skip
def the_tests_read_back_with_their_format_and_files(s: Storage, tmp_path: Path) -> None:
    """§4.3: each test's format and its hidden id, both as its ``test.json`` holds them."""
    source = s.open()
    _create(source, {**FILES, **CODE_TEST, **SAID})
    listed = source.list_tests(PID)  # in no order the contract gives (P4)
    assert sorted((t.test, t.test_id, t.format, t.latest_release, t.files) for t in listed) == [
        ("C1", C1_ID, "code", None, None),
        ("S1", S1_ID, "code", None, None),
        ("T1", T1_ID, "text", None, None),
    ]
    read = s.open().read_tests(PID, ["t1", "C1", "nope"])
    assert sorted(read) == ["C1", "t1"]  # keyed by the name as asked
    t1 = read["t1"]
    assert (t1.test, t1.test_id, t1.format) == ("T1", T1_ID, "text")
    expected = {**_test_files("T1"), "functions/helpers.py": FILES["functions/helpers.py"]}
    _same_files(source, dict(t1.files), expected, tmp_path)
    assert (read["C1"].test_id, read["C1"].format) == (C1_ID, "code")
    said = s.open().read_tests(PID, ["S1"])["S1"]
    assert (said.test_id, said.format) == (S1_ID, "code")
    assert s.open().read_tests(PID, ["nope"]) == {}


@conformance(
    "create_project", "release", "read_release", "list_releases", "read_tests", "read_project",
    rules=(
        "5.3.latest-release", "5.4.release-fields", "5.4.document-names", "P8b.one-release",
        "P10.changed", "P10.same-as-release", "P10.only-named-files",
    ),
)  # fmt: skip
def a_release_reads_back_with_its_documents(s: Storage, tmp_path: Path) -> None:
    """§4.4: filed under its test's id, numbered from 1, keeping the name it was released
    under; a Project's releases are keyed by the id (§4.2)."""
    source = s.open()
    _create(source)
    request = _request(note="first")
    made = source.release(PID, T1_ID, request)
    assert (made.test_id, made.test, made.version, made.documents) == (T1_ID, "T1", 1, None)
    reader = s.open()
    read = reader.read_release(PID, T1_ID, 1)
    docs, _named = _docs()
    assert (read.test_id, read.test, read.version) == (T1_ID, "T1", 1)
    assert dict(read.documents) == docs
    assert (read.format, read.fingerprint, read.content) == (
        request.format,
        request.fingerprint,
        request.content,
    )
    assert (read.note, read.released_by, read.pc, read.machine_id) == (
        "first",
        "alice",
        "BENCH-1",
        "0000a001",
    )
    assert read.released_at and read.released_at == made.released_at
    listed = reader.list_releases(PID, T1_ID)
    assert [(r.test_id, r.test, r.version, r.documents) for r in listed] == [(T1_ID, "T1", 1, None)]
    t1 = reader.read_tests(PID, ["T1"])["T1"]
    assert (t1.test_id, t1.latest_release) == (T1_ID, 1)
    whole = reader.read_project(PID).releases
    assert list(whole) == [T1_ID] and dict(whole[T1_ID][0].documents) == docs
    # The same content again is that release; a file changed since it was named is refused.
    assert _code(source.release, PID, T1_ID, request) == "same_as_release"
    stale = replace(request, content="0" * 64, files={"tests/T1/test.py": version_of(b"old")})
    assert _code(source.release, PID, T1_ID, stale) == "changed"
    assert [r.version for r in reader.list_releases(PID, T1_ID)] == [1]


@conformance("create_project", "import_release", "read_release", "list_releases")
def a_release_moved_in_lands_as_it_was(s: Storage, tmp_path: Path) -> None:
    """§6.10: a release imported keeps its number, its date, who made it, the name it was
    released under and its documents (one of them a binary), under its id whether or not a test
    holds it now; the same release again is answered as it is, another one under its number is
    refused (``changed``). Over the API, with the login the app moves data in with: the roles
    bench and importer (§3.5)."""
    source = s.open_mover()
    _create(source)
    docs, _named = _docs()
    docs = {**docs, "board.png": b"\x89PNG\r\n\x1a\n" + bytes(range(256))}
    request = _request()
    sent = [
        ReleaseRecord(
            test_id=test_id,
            test=name,
            version=version,
            format=request.format,
            fingerprint=test_release.fingerprint_of(request.format, docs),
            content=content,
            documents=docs,
            note=f"v{version}",
            released_at=f"2026-0{version}-02T03:04:05Z",
            released_by="carol",
            pc="BENCH-9",
            machine_id="0000a009",
        )  # fmt: skip
        for test_id, name, version, content in (
            (T1_ID, "T1", 3, "3" * 64),
            (T1_ID, "T1 old name", 1, "1" * 64),
            (NEVER_ID, "Deleted", 2, "2" * 64),
        )
    ]
    for record in sent:
        landed = source.import_release(PID, record)
        assert (landed.test_id, landed.version, landed.released_at) == (
            record.test_id, record.version, record.released_at,
        )  # fmt: skip
    reader = s.open()
    for record in sent:
        assert reader.read_release(PID, record.test_id, record.version) == record
    assert [r.version for r in reader.list_releases(PID, T1_ID)] == [1, 3]
    assert source.import_release(PID, sent[0]).version == 3  # the same again: answered
    other = replace(sent[0], content="9" * 64)
    assert _code(source.import_release, PID, other) == "changed"
    assert reader.read_release(PID, T1_ID, 3) == sent[0]


@conformance(
    "create_project", "release", "read_project", "rename_test", "read_tests", "list_releases",
    "changes_since", "push_changes",
    rules=(
        "5.3.latest-release", "P3.rename-no-release", "P7.one-test", "P8.list", "P9.shapes",
        "P10.same-as-release", "P10.number-and-name",
    ),
)  # fmt: skip
def a_rename_keeps_its_releases(s: Storage, tmp_path: Path) -> None:
    """§6.9: the folder moves with its ``test.json``, so the test keeps its id and every release
    filed under it, each keeping the name it was released under; a rename changes no release
    (§5.1, P3). Its documents unchanged, the renamed test's content is v1's (§4.3, §7); the next
    release follows v1 and records the new name (§4.4)."""
    source = s.open()
    _create(source)
    source.release(PID, T1_ID, _request())
    token = source.read_project(PID).token
    assert source.rename_test(PID, "T1", "T9", _folder(source, "T1")).status == SAVED
    reader = s.open()
    assert reader.read_tests(PID, ["T1"]) == {}
    renamed = reader.read_tests(PID, ["T9"])["T9"]
    assert (renamed.test, renamed.test_id, renamed.latest_release) == ("T9", T1_ID, 1)
    assert renamed.files["tests/T9/test.json"].version == version_of(FILES["tests/T1/test.json"])
    held = reader.list_releases(PID, T1_ID)
    assert [(r.test_id, r.test, r.version) for r in held] == [(T1_ID, "T1", 1)]
    assert reader.changes_since(PID, token).releases == {}
    files = _renamed(FILES, "T1", "T9")
    assert _code(source.release, PID, T1_ID, _request("T9", files)) == "same_as_release"
    edited = {**files, "tests/T9/test.py": b"print('T9')\n"}
    write = Write(
        "tests/T9/test.py", edited["tests/T9/test.py"], version_of(files["tests/T9/test.py"])
    )
    assert [r.status for r in source.push_changes(PID, [write]).results] == [SAVED]
    made = source.release(PID, T1_ID, _request("T9", edited))
    assert (made.test_id, made.test, made.version) == (T1_ID, "T9", 2)
    assert [(r.test, r.version) for r in reader.list_releases(PID, T1_ID)] == [("T1", 1), ("T9", 2)]


@conformance(
    "create_project", "release", "read_project", "push_changes", "list_tests", "list_releases",
    rules=("5.4.numbering-per-id",),
)  # fmt: skip
def a_new_test_with_a_deleted_tests_name_starts_at_v1_and_sees_no_old_release(
    s: Storage, tmp_path: Path
) -> None:
    """§4.4, §6.9: nothing reserves a deleted test's name; a test made under it has its own id,
    no release until it is released, and numbers its own from 1, even with the old v1's
    content (``same_as_release`` compares under one id only, §7)."""
    source = s.open()
    _create(source)
    request = _request()
    source.release(PID, T1_ID, request)
    assert set(_delete_test(source, "T1")) == {SAVED}
    again = {**_test_files("T1"), "tests/T1/test.json": the_test_json(NEW_ID)}
    first = sorted(again, key=lambda p: not p.endswith("/test.json"))  # its test.json first
    made_again = source.push_changes(PID, [Write(p, again[p], None) for p in first])
    assert {r.status for r in made_again.results} == {SAVED}
    reader = s.open()
    listed = reader.list_tests(PID)
    assert [(t.test, t.test_id, t.latest_release) for t in listed] == [("T1", NEW_ID, None)]
    assert reader.list_releases(PID, NEW_ID) == []
    made = source.release(PID, NEW_ID, request)
    assert (made.test_id, made.test, made.version) == (NEW_ID, "T1", 1)
    old = reader.list_releases(PID, T1_ID)
    assert [(r.test_id, r.test, r.version) for r in old] == [(T1_ID, "T1", 1)]
    assert sorted(reader.read_project(PID).releases) == sorted([T1_ID, NEW_ID])


@conformance(
    "create_project", "release", "read_project", "push_changes", "list_tests", "read_tests",
    "list_releases", "read_release", "changes_since",
    rules=("P2.whole", "P3.changes", "P9.last-file-deletes-test"),
)  # fmt: skip
def a_deleted_tests_releases_stay_under_its_id(s: Storage, tmp_path: Path) -> None:
    """§4.4, §6.9: a test whose last file is removed leaves its releases where they are, under
    its id, documents and all; a delete makes no release (§5.1, P3)."""
    source = s.open()
    _create(source)
    source.release(PID, T1_ID, _request(note="first"))
    token = source.read_project(PID).token
    assert set(_delete_test(source, "T1")) == {SAVED}
    reader = s.open()
    assert reader.list_tests(PID) == [] and reader.read_tests(PID, ["T1"]) == {}
    held = reader.list_releases(PID, T1_ID)
    assert [(r.test_id, r.test, r.version) for r in held] == [(T1_ID, "T1", 1)]
    kept = reader.read_release(PID, T1_ID, 1)
    docs, _named = _docs()
    assert (kept.test_id, kept.note, dict(kept.documents)) == (T1_ID, "first", docs)
    whole = reader.read_project(PID).releases
    assert list(whole) == [T1_ID] and dict(whole[T1_ID][0].documents) == docs
    moved = reader.changes_since(PID, token)
    assert sorted(moved.deleted) == sorted(_test_files("T1")) and moved.releases == {}


@conformance(
    "create_project", "list_projects", "push_changes", "read_project",
    rules=("2.12.gzip-json-body", "5.3.test-id-rules-p9", "P9.results"),
)  # fmt: skip
def a_change_leaving_a_test_without_a_valid_test_id_is_refused_alone(
    s: Storage, tmp_path: Path
) -> None:
    """§4.3: a test folder holding a file holds a ``test.json``, a JSON object (a byte-order
    mark before it allowed) whose ``test_id`` is a lower-case UUID. P9 checks each change in the
    order sent, against the project as it is then, and refuses alone (kind ``invalid``) one
    after which that would not hold, nothing written for it: so a new test's ``test.json`` goes
    first and is deleted last. P11 checks the project whole: refused, nothing written (§7)."""
    source = s.open()
    no_id = {**FILES, "tests/T1/test.json": b'{"active": "text"}'}
    assert _kind(source.create_project, NAME, no_id, None) == "invalid"
    without = {p: d for p, d in FILES.items() if p != "tests/T1/test.json"}
    assert _kind(source.create_project, NAME, without, None) == "invalid"
    assert _listed(source) == []
    _create(source)
    t1_json = source.read_project(PID).files["tests/T1/test.json"].version
    t3_json = the_test_json(T2_ID)
    bom = b"\xef\xbb\xbf" + the_test_json(NEW_ID)
    pushed = source.push_changes(
        PID,
        [
            Write("tests/T2/procedure.json", b'{"id": "T2"}', None),  # before its test.json
            Write("tests/T3/test.json", t3_json, None),
            Write("tests/T3/procedure.json", b'{"id": "T3"}', None),  # after it
            Write("tests/T4/test.json", b'{"active": "text"}\n', None),  # no id
            Write("tests/T5/test.json", the_test_json(T2_ID.upper()), None),  # not lower case
            Write("tests/T6/test.json", b'{"active": "text", "test_id": "T6"}', None),  # no UUID
            Write("tests/T7/test.json", b"not json\n", None),
            Write("tests/T8/test.json", json.dumps([NEW_ID]).encode(), None),  # not an object
            Write("tests/T9/test.json", bom, None),  # a byte-order mark before it
            Write("tests/T1/test.json", b'{"active": "text"}\n', t1_json),  # its id taken out
            Delete("tests/T1/test.json", t1_json),  # its other files still there
            Delete("tests/T3/test.json", version_of(t3_json)),  # before its procedure.json
            Delete("tests/T3/procedure.json", version_of(b'{"id": "T3"}')),
            Delete("tests/T3/test.json", version_of(t3_json)),  # last: the test is deleted
        ],
    )
    no, ok = (REFUSED, "invalid"), (SAVED, "")
    assert [(r.status, r.kind) for r in pushed.results] == [
        *(no, ok, ok),
        *(no, no, no, no, no),
        ok,
        *(no, no, no),
        *(ok, ok),
    ]
    after = source.read_project(PID).files
    assert sorted(after) == sorted([*FILES, "tests/T9/test.json"])
    assert after["tests/T1/test.json"].version == t1_json
    assert after["tests/T9/test.json"].version == version_of(bom)


@conformance(
    "create_project", "list_projects", "push_changes", "read_project", "list_tests",
    rules=("5.3.test-id-rules-p9",),
)  # fmt: skip
def two_tests_with_one_test_id_are_refused(s: Storage, tmp_path: Path) -> None:
    """§4.3: no two tests of a project hold one ``test_id`` (a folder copied by hand). P9
    refuses alone the change that would make two (kind ``invalid``), checked against the project
    as it is at that point, so an id freed earlier in the same request may be taken; a
    ``test.json`` written with another id makes its folder another test. P11 refuses the
    project whole, nothing written (§7)."""
    source = s.open()
    copied = {**FILES, **_renamed(_test_files("T1"), "T1", "T2")}
    assert _kind(source.create_project, NAME, copied, None) == "invalid"
    assert _listed(source) == []
    _create(source)
    t1_json = source.read_project(PID).files["tests/T1/test.json"].version
    pushed = source.push_changes(
        PID,
        [
            Write("tests/T2/test.json", the_test_json(T1_ID), None),  # T1's id
            Write("tests/T2/procedure.json", b'{"id": "T2"}', None),  # so no test.json there
            Write("tests/T1/test.json", the_test_json(NEW_ID), t1_json),  # T1 is another test
            Write("tests/T2/test.json", the_test_json(T1_ID), None),  # the id is free now
            Write("tests/T3/test.json", the_test_json(NEW_ID), None),  # T1's id now
        ],
    )
    no, ok = (REFUSED, "invalid"), (SAVED, "")
    assert [(r.status, r.kind) for r in pushed.results] == [no, no, ok, ok, no]
    listed = source.list_tests(PID)  # in no order the contract gives (P4)
    assert sorted((t.test, t.test_id) for t in listed) == [("T1", NEW_ID), ("T2", T1_ID)]
    files = source.read_project(PID).files
    assert [p for p in files if p.startswith(("tests/T2/", "tests/T3/"))] == ["tests/T2/test.json"]


def _draft(test_id: str, seed: str) -> Draft:
    """A draft of T1 naming *test_id*, its hashes made from *seed* (the app computes them; a
    storage never does)."""
    return Draft(
        test="T1",
        test_id=test_id,
        fingerprint=hashlib.sha256(f"fingerprint {seed}".encode()).hexdigest(),
        content=hashlib.sha256(f"content {seed}".encode()).hexdigest(),
    )


def _held(draft: Any) -> tuple | None:
    if draft is None:
        return None
    return (draft.test_id, draft.fingerprint, draft.content, draft.based_on)


@conformance(
    "create_project", "read_project", "push_changes", "read_tests",
    rules=("5.3.draft-kept-when",),
)  # fmt: skip
def a_draft_is_kept_only_with_its_tests_id(s: Storage, tmp_path: Path) -> None:
    """§4.3 (when a draft is kept): its test exists after the request, its ``test_id`` is the one
    that test's ``test.json`` holds, and every change of the request under its folder and under
    ``functions/`` was saved. A test whose files the request changed without a draft kept loses
    the one it held; a change under ``functions/`` alone drops none."""
    source, reader = s.open(), s.open()
    _create(source)

    def write(path: str, data: bytes, *drafts: Draft) -> list[str]:
        held = source.read_project(PID).files.get(path)
        change = Write(path, data, held.version if held else None)
        return [r.status for r in source.push_changes(PID, [change], drafts).results]

    def kept(draft: Draft) -> tuple | None:
        return _held(draft) if s.name in KEEPS_DRAFTS else None

    def draft_now() -> tuple | None:
        return _held(reader.read_tests(PID, ["T1"])["T1"].draft)

    mine = _draft(T1_ID, "1")
    assert write("tests/T1/test.py", b"print(1)\n", mine) == [SAVED]
    assert draft_now() == kept(mine)
    # Another id than its test.json's: not kept, and the test loses the draft it held.
    assert write("tests/T1/test.py", b"print(2)\n", _draft(NEW_ID, "2")) == [SAVED]
    assert draft_now() is None
    again = _draft(T1_ID, "3")
    assert write("tests/T1/test.py", b"print(3)\n", again) == [SAVED]
    assert draft_now() == kept(again)
    assert write("functions/helpers.py", b"def helper():\n    return 3\n") == [SAVED]
    assert draft_now() == kept(again)
    # One change under its folder met a conflict: not kept; another was written, so the test
    # loses the draft it held.
    stale = Write("tests/T1/test.py", b"print(4)\n", version_of(b"not what is there"))
    more = Write("tests/T1/more.md", b"more\n", None)
    pushed = source.push_changes(PID, [more, stale], [_draft(T1_ID, "4")])
    assert [r.status for r in pushed.results] == [SAVED, CHANGED]
    assert draft_now() is None


@conformance(
    "create_project", "release", "read_release", "list_releases", "read_project", "push_changes",
    rules=(
        "2.5.test-id-from-test-json", "5.3.other-id-another-test", "6.1.item-not-held-404",
        "P10.not-found",
    ),
)  # fmt: skip
def a_release_of_an_id_no_test_holds_is_not_found(s: Storage, tmp_path: Path) -> None:
    """P10 releases the test whose ``test.json`` holds the id it names (§4.3, §5.1): an id no
    test holds is ``not_found`` (§7), the former id of a ``test.json`` written with another
    included (the folder is another test now). P8 answers ``[]`` for an id with no release;
    P8b ``not_found`` for a version the id does not hold."""
    source = s.open()
    _create(source)
    request = _request()
    assert source.list_releases(PID, NEVER_ID) == []
    assert _kind(source.release, PID, NEVER_ID, request) == "not_found"
    assert _kind(source.read_release, PID, NEVER_ID, 1) == "not_found"
    t1_json = source.read_project(PID).files["tests/T1/test.json"].version
    swap = Write("tests/T1/test.json", the_test_json(NEW_ID), t1_json)
    assert [r.status for r in source.push_changes(PID, [swap]).results] == [SAVED]
    assert _kind(source.release, PID, T1_ID, request) == "not_found"
    made = source.release(PID, NEW_ID, request)
    assert (made.test_id, made.test, made.version) == (NEW_ID, "T1", 1)
    assert _kind(source.read_release, PID, NEW_ID, 2) == "not_found"
    assert source.list_releases(PID, T1_ID) == []


def _checkout(s: Storage) -> tuple[Path, Callable[[], Any]]:
    """This PC's working copy of the project on *s*, made by the app's sync (through a bench
    of its own, so its calls are no case's), and what syncs it again (a full pass)."""
    raw = s.opener()
    if isinstance(raw, TreeProjectSource):
        base = raw.root / NAME
        copy = project_sync.checkout(raw.tree, base, backend=s.name, params={})
        return copy, lambda: project_sync.pull(copy, raw.tree, base)
    identity = raw.identity
    copy = project_sync_api.checkout(raw, f"/{NAME}", backend=s.name, identity=identity, params={})
    return copy, lambda: project_sync_api.pull(copy, raw)


@conformance("create_project", "release", "read_release", "push_changes")
def a_release_made_in_a_working_copy_is_the_storages_and_the_same_file_in_the_copy(
    s: Storage, tmp_path: Path
) -> None:
    """P10 from a working copy (``project_sync_release``, test-id plan S5): the storage numbers
    the release and keeps the copy's documents; the copy writes the same release in its
    ``releases/<test_id>/``, and the next sync brings nothing and meets no conflict. A test
    changed here and not synced, or changed on the storage since the last sync, is refused
    (the owner's sentence, §7.4), nothing released; once synced it is released."""
    with pytest.MonkeyPatch.context() as patch:
        # Released by one of the kit's users: a server listing U1 takes no other (§6.8).
        patch.setattr(test_release, "user", lambda: "kit")
        _release_from_a_working_copy(s)


def _release_from_a_working_copy(s: Storage) -> None:
    source = s.open()
    _create(source)
    copy, sync = _checkout(s)
    script = copy / "tests" / "T1" / "test.py"
    _fmt, docs = test_release.documents(copy, "T1")
    assert project_sync_release.release(copy, source, "T1", " first ") == 1
    made = source.read_release(PID, T1_ID, 1)
    assert (made.test_id, made.test, made.note, dict(made.documents)) == (
        T1_ID,
        "T1",
        "first",
        docs,
    )
    here = test_release.releases_of(copy, T1_ID)
    assert [(r.number, r.altered, r.documents, r.released_at) for r in here] == [
        (1, False, docs, made.released_at)
    ]
    report = sync()
    assert (report.conflicts, report.downloaded) == ([], [])
    assert test_release.state(copy, "T1").label == "v1"

    script.write_bytes(b"print('T1, edited here')\n")  # not synced yet
    with pytest.raises(test_release.Refused) as refused:
        project_sync_release.release(copy, source, "T1")
    assert str(refused.value) == test_release.NOT_SYNCED
    assert sync().uploaded == ["tests/T1/test.py"]
    theirs = Write(
        "tests/T1/test.py",
        b"print('T1, edited on the other bench')\n",
        version_of(script.read_bytes()),
    )
    assert [r.status for r in source.push_changes(PID, [theirs]).results] == [SAVED]
    with pytest.raises(test_release.Refused) as refused:
        project_sync_release.release(copy, source, "T1")
    assert str(refused.value) == (
        "This test changed on the server since your last sync: sync first, then release."
    )
    assert _kind(source.read_release, PID, T1_ID, 2) == "not_found"
    assert sync().downloaded == ["tests/T1/test.py"]
    assert project_sync_release.release(copy, source, "T1") == 2
    _fmt, docs = test_release.documents(copy, "T1")
    assert dict(source.read_release(PID, T1_ID, 2).documents) == docs
    assert test_release.state(copy, "T1").label == "v2"


def test_every_action_has_a_case():
    named = {a for case in CASES for a in case.actions}
    assert sorted(PROTOCOL - named) == []


def test_every_known_gap_is_a_storage_and_an_action_or_a_case():
    covered = frozenset().union(*(case.actions for case in CASES), (case.name for case in CASES))
    for backend, action in KNOWN_GAPS:
        assert backend in BACKENDS and action in covered, (backend, action)


def test_every_storage_runs_the_suite():
    """The folder and the JSON API run here; every other storage in its own file."""
    here = Path(__file__).resolve().parent
    for backend in set(BACKENDS) - {"folder", "api"}:
        assert (here / f"test_project_conformance_{backend}.py").is_file(), backend


# ---------------------------------------------------------------------------
# The storages
# ---------------------------------------------------------------------------


@pytest.fixture
def folder_storage(tmp_path) -> Storage:
    share = tmp_path / "share"
    share.mkdir()
    return Storage("folder", lambda: TreeProjectSource(FsTree(share)))


@pytest.mark.parametrize(("case", "backend"), params("folder"))
def test_every_action_reads_back_as_it_went_in_on_a_folder(case, backend, folder_storage, tmp_path):
    run_case(case, folder_storage, tmp_path)


@pytest.fixture
def reference(tmp_path):
    """The reference server with the kit's logins: a bench (``bench``, ``publisher``) and the
    mover (``bench``, ``importer``), as a server the environment names gives them."""
    logins = server_target.LOCAL_LOGINS
    tokens = {token: logins[token] for token in ("t-bench", "t-mover")}
    with server_target.serve(tmp_path / "reference", tokens) as (base_url, _running):
        yield base_url


def api_session(base_url: str, role: str = "bench") -> ApiSession:
    """A session of the login holding *role* (``bench``; ``mover``: an unset mover's token
    skips the case, saying which variable to set)."""
    token = server_target.token(role)
    return ApiSession(server_target.endpoint(base_url), lambda _identity: (role, token))


def own_project(monkeypatch) -> None:
    """This case's project: a new id and name (another project's id too), its
    ``config/config.json`` holding that id."""
    pid = server_target.fresh_id()
    monkeypatch.setitem(globals(), "PID", pid)
    monkeypatch.setitem(globals(), "OTHER", server_target.fresh_id())
    monkeypatch.setitem(globals(), "NAME", f"Ripple bench {server_target.fresh_suffix()}")
    monkeypatch.setitem(FILES, "config/config.json", json.dumps({"project_id": pid}).encode())


def recording(source: Any, calls: list[tuple[str, str]], protocol: frozenset = PROTOCOL) -> Any:
    """*source* noting, for each call, the action of *protocol* it is made for (the outermost)."""
    acting = threading.local()

    def noting(action: str, method: Callable[..., Any]) -> Callable[..., Any]:
        def call(*args: Any, **kwargs: Any) -> Any:
            outer = getattr(acting, "name", None)
            acting.name = outer or action
            try:
                return method(*args, **kwargs)
            finally:
                acting.name = outer

        return call

    for action in protocol:
        setattr(source, action, noting(action, getattr(source, action)))
    wire = source.session.wire
    for name in ("call", "upload", "download"):
        sent = getattr(wire, name)

        def through(op: str, *args: Any, _sent: Any = sent, **kwargs: Any) -> Any:
            calls.append((getattr(acting, "name", None) or "", op))
            return _sent(op, *args, **kwargs)

        setattr(wire, name, through)
    return source


def outside_the_map(calls: list[tuple[str, str]], interface: str) -> list[tuple[str, str]]:
    """The calls made for an action that the action map does not name for it (discovery is
    every action's, once)."""
    return sorted(
        {
            (action, op)
            for action, op in calls
            if op not in DISCOVERY and op not in ACTIONS[f"{interface}.{action}"].calls
        }
    )


@pytest.fixture
def api_calls() -> list[tuple[str, str]]:
    return []


@pytest.fixture
def api_storage(reference, api_calls, tmp_path, monkeypatch) -> Storage:
    own_project(monkeypatch)
    benches = iter(range(1000))

    def opener(role: str) -> Callable[[], Any]:
        def open_one() -> Any:
            session = api_session(reference, role)
            source = ApiProjectSource(session, tmp_path / f"cache-{next(benches)}")
            return recording(source, api_calls)

        return open_one

    return Storage("api", opener("bench"), mover=opener("mover"))


@pytest.mark.parametrize(("case", "backend"), params("api", pytest.mark.servers))
def test_every_action_reads_back_as_it_went_in_over_the_json_api(
    case, backend, api_storage, api_calls, tmp_path
):
    run_case(case, api_storage, tmp_path)
    assert outside_the_map(api_calls, "project") == []
    if case.name == "a_project_created_reads_back_byte_for_byte":
        ops = {op for _action, op in api_calls}
        assert {"uploadBlob", "downloadBlob"} <= ops  # the large files went alone, both ways


# ---------------------------------------------------------------------------
# Every path that never travels, refused (generated from the app's own list)
# ---------------------------------------------------------------------------

#: One case per path :mod:`never_travels` generates from the app's exclusion list, on the folder
#: and over the JSON API. The folder's refusal is the FTPS and SFTP storages' too (one tree
#: code, ``project_store_tree``); :func:`a_path_that_never_travels_as_a_file_is_refused` runs
#: there.
NEVER = never_travels.paths()
KIT_BYTES = b"kit\n"
KEPT = "tests/T1/kept.md"


def _on(*backends: str) -> list:
    return [
        pytest.param(
            path,
            backend,
            marks=[pytest.mark.servers] if backend == "api" else [],
            id=f"{path}-{backend}",
        )
        for backend in backends
        for path in NEVER
    ]


def _small() -> dict[str, bytes]:
    """A project of one test, this case's own (its ``config/config.json`` holds ``PID``)."""
    return {
        "config/config.json": FILES["config/config.json"],
        "tests/T1/test.json": the_test_json(T1_ID),
        "tests/T1/test.py": FILES["tests/T1/test.py"],
    }


def _versions_of(source: Any) -> dict[str, str]:
    return {path: ref.version for path, ref in source.read_project(PID).files.items()}


@pytest.mark.parametrize(("path", "backend"), _on("folder", "api"))
@pytest.mark.rule("5.2.never-travel-p11")
def test_a_new_project_holding_a_path_that_never_travels_is_refused_whole(path, backend, request):
    """P11: ``400 bad_request`` (kind ``invalid``), and nothing created: no project under its
    id, none listed under its name."""
    storage = request.getfixturevalue(f"{backend}_storage")
    source = storage.open()
    with pytest.raises(EvidenceError) as refused:
        source.create_project(NAME, {**_small(), path: KIT_BYTES}, None)
    assert refused.value.kind == "invalid", refused.value
    if isinstance(refused.value, ApiError):
        assert (refused.value.status, refused.value.code) == (400, "bad_request"), refused.value
    reader = storage.open()
    assert _kind(reader.read_project, PID) == "not_found"
    assert _listed(reader) == []


@pytest.mark.parametrize(("path", "backend"), _on("folder", "api"))
@pytest.mark.rule("5.2.never-travel-p9")
def test_a_push_of_a_path_that_never_travels_is_refused_alone(path, backend, request):
    """P9: that change ``refused`` (kind ``invalid``) and nothing written for it; the change
    after it in the same request saved."""
    storage = request.getfixturevalue(f"{backend}_storage")
    source = storage.open()
    source.create_project(NAME, _small(), None)
    before = _versions_of(source)
    pushed = source.push_changes(PID, [Write(path, KIT_BYTES, None), Write(KEPT, b"kept\n", None)])
    assert [(r.path, r.status, r.kind) for r in pushed.results] == [
        (path, REFUSED, "invalid"),
        (KEPT, SAVED, ""),
    ]
    assert _versions_of(storage.open()) == {**before, KEPT: version_of(b"kept\n")}
