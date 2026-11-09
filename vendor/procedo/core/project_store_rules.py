"""The rules every project storage applies to what it is asked to keep, written once.

A storage that keeps a project's files itself — a tree (:mod:`project_store_tree`: a folder,
FTPS, SFTP) or the evidence database (:mod:`project_store_db`) — answers each action of
:class:`project_store.ProjectSource` by the same rules, as the JSON API's server does
(``data_format.md`` §2, §4.2–§4.4). Each rule is a function here, over what the storage holds
(an *index*: each live file's :func:`evidence_tree.name_key` → its stored path), never over how
it holds it:

* **Names compare as names** (NFC, case folding): a write under another case replaces the file
  and keeps its spelling; a new file takes its folders' spelling (:func:`spell`).
* **What never travels as a file** (:func:`never_a_file`): ``bundle/`` and ``releases/`` (objects
  of their own), the evidence, this machine's state, a name some PC cannot create.
* **A test is its hidden id** (§4.3): a test folder holding a file holds a ``test.json`` with a
  valid ``test_id``, and no two tests hold one (:func:`id_problem`); a change after which either
  would fail is refused alone (:func:`breaks`), a new project that fails either is refused whole
  (:func:`checked_new_project`).
* **A write names the version it replaces**: what is there already is a replay, ``saved``; a
  file changed since is ``changed`` (:func:`write_verdict`, :func:`delete_verdict`,
  :func:`bundle_verdict`); a rename names every file it moves (:func:`rename_verdict`).
* **A release keeps the files it names as they are now** (:func:`release_documents`), and the
  same content twice under one id is that release (:func:`refuse_same_content`).

Qt-free, data only.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence

from . import project_origin, test_format
from .evidence_store import EvidenceError
from .evidence_tree import name_key
from .project_never_travels import exclusion
from .project_origin import CONFIG_FILE
from .project_store import (
    BUNDLE_DIR,
    CHANGED,
    REFUSED,
    RELEASES_DIR,
    SAVED,
    TESTS_DIR,
    FileRef,
    Outcome,
    ReleaseRecord,
    version_of,
)
from .project_sync_plan import unsafe

#: What a path of a project folder is to the interface: a file, or part of an object.
FILE = "file"
#: How a result names the bundle (``data_format.md`` §4.2).
BUNDLE_PATH = f"{BUNDLE_DIR}/"


def nfc(path: str) -> str:
    return unicodedata.normalize("NFC", path)


def kind_of(rel: str) -> str:
    """``bundle``, ``releases`` or ``file``: what a path of the folder is to the interface."""
    top = rel.split("/", 1)[0]
    return top if top in (BUNDLE_DIR, RELEASES_DIR) and "/" in rel else FILE


def test_of(rel: str) -> str:
    """The test a file is under (``tests/<test>/…``), ``""`` for any other file."""
    parts = rel.split("/")
    return parts[1] if len(parts) > 2 and parts[0] == TESTS_DIR else ""


def test_key(rel: str) -> str:
    """The key (:func:`name_key`) of the test a file is under, ``""`` for any other file."""
    parts = rel.split("/")
    return name_key(parts[1]) if len(parts) > 2 and name_key(parts[0]) == TESTS_DIR else ""


def is_marker(rel: str) -> bool:
    """Whether *rel* is a test's own ``test.json`` (``tests/<test>/test.json``)."""
    parts = rel.split("/")
    return len(parts) == 3 and bool(test_key(rel)) and name_key(parts[2]) == test_format.TEST_JSON


def id_in(data: bytes | None) -> str:
    """The ``test_id`` a ``test.json`` holding *data* holds, ``""`` when none is valid."""
    try:
        return test_format.test_id_in(data.decode("utf-8")) if data is not None else ""
    except UnicodeDecodeError:
        return ""


def id_problem(test: str, ids: Mapping[str, str]) -> str:
    """Why the test keyed *test* breaks a rule of ``data_format.md`` §4.3, *ids* holding each
    test's id (``""``: its folder holds no ``test.json`` with a valid one); ``""`` when none."""
    held = ids.get(test, "")
    if not held:
        return (
            f"tests/{test}/ holds no test.json with its test_id, a lower-case UUID: a test's "
            "test.json goes before its other files and after them (data_format.md §4.3)."
        )
    twin = next((other for other, i in ids.items() if other != test and i == held), "")
    if twin:
        return f"tests/{test}/ and tests/{twin}/ hold one test_id, {held}: each test has its own."
    return ""


def never_a_file(rel: str) -> str:
    """Why *rel* is never a project file here (``""`` when it may be one)."""
    if kind_of(rel) != FILE:
        return f"{rel.split('/', 1)[0]}/ travels as an object of its own, never as files"
    return exclusion(rel) or unsafe(rel)


def spell(index: Mapping[str, str], rel: str) -> str:
    """A new file's path in its folders' spelling, the first met (``data_format.md`` §2)."""
    folders: dict[str, str] = {}
    for stored in index.values():
        parts = stored.split("/")
        for end in range(1, len(parts)):
            prefix = "/".join(parts[:end])
            folders.setdefault(name_key(prefix), prefix)
    parts = rel.split("/")
    for end in range(len(parts) - 1, 0, -1):
        known = folders.get(name_key("/".join(parts[:end])))
        if known is not None:
            return "/".join([known, *parts[end:]])
    return rel


def checked_new_project(name: str, files: Mapping[str, bytes]) -> tuple[str, dict[str, bytes], str]:
    """A new project's name and files as stored (NFC), and its ``project_id``; refused
    (``invalid``) whole when its name cannot name a folder on every PC, a path never travels as
    a file, it holds no ``config/config.json``, or a test breaks a rule of its id."""
    name = nfc(name)
    if "/" in name or unsafe(name):
        raise EvidenceError("invalid", f"{name!r} cannot name a project's folder on every PC.")
    sent = {nfc(path): data for path, data in files.items()}
    for path in sent:
        why = never_a_file(path)
        if why:
            raise EvidenceError("invalid", f"{path} is not a project file: {why}")
    if CONFIG_FILE not in sent:
        raise EvidenceError("invalid", f"A new project's files hold {CONFIG_FILE}.")
    ids = {test_key(path): "" for path in sent if test_key(path)}
    ids.update({test_key(p): id_in(d) for p, d in sent.items() if is_marker(p)})
    for test in sorted(ids):
        why = id_problem(test, ids)
        if why:
            raise EvidenceError("invalid", why)
    return name, sent, project_origin.project_id_of(sent[CONFIG_FILE])


# ---------------------------------------------------------------------------
# A push's rules, change by change
# ---------------------------------------------------------------------------

#: What reads a ``test.json`` the storage holds, by its stored path.
ReadMarker = Callable[[str], bytes]


def test_ids(index: Mapping[str, str], ids: dict[str, str], read: ReadMarker) -> dict[str, str]:
    """Each test the storage holds a file of now: its key -> the id its ``test.json`` holds
    (``""``: none valid), read the first time a rule asks (*ids* keeps what was read)."""
    found = {}
    for rel in index.values():
        test = test_key(rel)
        if test and test not in found:
            if test not in ids:
                marker = index.get(f"{TESTS_DIR}/{test}/{test_format.TEST_JSON}")
                ids[test] = id_in(read(marker)) if marker else ""
            found[test] = ids[test]
    return found


def breaks(
    index: Mapping[str, str], ids: dict[str, str], read: ReadMarker, rel: str, data: bytes | None
) -> str:
    """Why writing *data* at *rel* (``None``: deleting it) would break a rule of a test's id
    (:func:`id_problem`) for the test it is under; ``""`` when it keeps both."""
    test = test_key(rel)
    if not test:
        return ""
    after = test_ids(index, ids, read)
    if data is None and not any(
        test_key(other) == test and name_key(other) != name_key(rel) for other in index.values()
    ):
        return ""  # its last file: the test is deleted
    if is_marker(rel):
        after[test] = id_in(data)
    else:
        after.setdefault(test, "")
    return id_problem(test, after)


def kept(index: dict[str, str], ids: dict[str, str], stored: str, data: bytes | None) -> None:
    """*stored* written with *data* (``None``: deleted), as the index and the ids read see it."""
    if data is None:
        del index[name_key(stored)]
    else:
        index[name_key(stored)] = stored
    if is_marker(stored):
        ids[test_key(stored)] = id_in(data)


def write_verdict(
    path: str, current: FileRef | None, version: str, replaces: str | None
) -> Outcome | None:
    """A write's result when it writes nothing: a replay of what is there (``saved``), or a
    file changed since the version it replaces (``changed``); ``None``: write it."""
    if current is not None and current.version == version:
        return Outcome(path, SAVED, version)
    if (current.version if current else None) != replaces:
        return Outcome(path, CHANGED, current=current)
    return None


def delete_verdict(path: str, current: FileRef | None, replaces: str) -> Outcome | None:
    """A delete's result when it removes nothing: gone already (a replay, ``saved``), or changed
    since the version it replaces (``changed``); ``None``: remove it."""
    if current is None:
        return Outcome(path, SAVED)
    if current.version != replaces:
        return Outcome(path, CHANGED, current=current)
    return None


def bundle_verdict(
    current: tuple[str, int] | None, version: str, replaces: str | None
) -> Outcome | None:
    """A new bundle's result when it writes nothing, *current* the version and the archive's
    size held (``None``: none): the same bundle (``saved``), or another one since the version it
    replaces (``changed``, with the archive held); ``None``: write it."""
    if current is not None and current[0] == version:
        return Outcome(BUNDLE_PATH, SAVED, version)
    if (current[0] if current else None) != replaces:
        ref = None if current is None else FileRef(BUNDLE_PATH, current[0], current[1])
        return Outcome(BUNDLE_PATH, CHANGED, current=ref)
    return None


def rename_verdict(
    old: str, new: str, tests: Mapping[str, str], holds: Callable[[str], bool]
) -> Outcome | None:
    """``tests/<old>`` moved to ``tests/<new>`` (NFC): its result when it moves nothing, *tests*
    each test's key → its spelling, *holds* whether a test's folder holds exactly the files the
    move names. A new name that is none (``refused``); moved already (a replay, ``saved``); the
    folder gone, holding other files, or the new name taken (``changed``). ``None``: move it."""
    path = f"{TESTS_DIR}/{old}"
    if new == old or unsafe(new) or "/" in new:
        return Outcome(path, REFUSED, kind="invalid", message=f"{new!r} is no new name for {old}.")
    source, target = tests.get(name_key(old)), tests.get(name_key(new))
    if source is None and target is not None and holds(target):
        return Outcome(path, SAVED)
    if source is None or (target is not None and name_key(new) != name_key(old)):
        return Outcome(path, CHANGED)
    if not holds(source):
        return Outcome(path, CHANGED)
    return None


def holds(
    paths: Iterable[str], test: str, named: Mapping[str, str], version: Callable[[str], str]
) -> bool:
    """Whether ``tests/<test>`` holds exactly the files *named* (key -> version), *paths* the
    stored paths of the storage, *version* what one holds."""
    prefix = f"{TESTS_DIR}/{test}/"
    held = {name_key(rel[len(prefix) :]): version(rel) for rel in paths if rel.startswith(prefix)}
    return held == dict(named)


# ---------------------------------------------------------------------------
# A release
# ---------------------------------------------------------------------------


def release_documents(
    files: Mapping[str, str], index: Mapping[str, str], read: Callable[[str], bytes]
) -> dict[str, bytes]:
    """The documents a release keeps, by their names in the test (a ``functions/`` file by its
    path): each file *files* names (path → version) as the storage holds it, *read* its bytes.
    Refused (code ``changed``) when one is not that version any more."""
    docs: dict[str, bytes] = {}
    changed = []
    for path, version in files.items():
        stored = index.get(name_key(path))
        data = read(stored) if stored is not None else None
        if stored is None or data is None or version_of(data) != version:
            changed.append(path)
            continue
        parts = stored.split("/")
        docs["/".join(parts[2:]) if parts[0] == TESTS_DIR else stored] = data
    if changed:
        raise EvidenceError(
            "invalid",
            f"{len(changed)} of the files released changed meanwhile: {', '.join(changed)}",
            code="changed",
        )
    return docs


def refuse_same_content(held: Sequence[ReleaseRecord], content: str) -> None:
    """Refused (code ``same_as_release``) when a release under the id has *content*."""
    same = [r.version for r in held if r.content == content]
    if same:
        raise EvidenceError(
            "invalid",
            f"Nothing to release: the files are exactly v{max(same)}.",
            code="same_as_release",
        )


def imported(held: Sequence[ReleaseRecord], record: ReleaseRecord) -> ReleaseRecord | None:
    """The release *record* lands as (``ProjectSource.import_release``): ``None`` when its
    number is free under its id (land it), the release held when it is this very release again
    (its ``content`` and ``fingerprint``), else refused (code ``changed``): another release has
    that number."""
    if not test_format.is_test_id(record.test_id) or record.version < 1:
        raise EvidenceError("invalid", "An imported release names its test id and a number.")
    for found in held:
        if found.version != record.version:
            continue
        if (found.content, found.fingerprint) == (record.content, record.fingerprint):
            return found
        raise EvidenceError(
            "invalid",
            f"{record.test} has another release v{record.version} there already.",
            code="changed",
        )
    return None
