"""A test's releases and its draft (docs/plans/test_release_design.md, slice 1).

A **release** (v1, v2, …) exists only when someone releases the test (Studio's *Release…*): its
documents as they were then, kept for good in one file, ``releases/<test_id>/vN.json``, outside
``tests/`` so the test list never reads it. The **draft** is the test's files between releases.
Opening or saving a test never numbers anything.

A test's releases are filed under its hidden id (``test_format.TEST_ID``, in its ``test.json``),
never under its name (``docs/plans/data_format.md`` §4.4): a rename keeps them, a deleted test's
stay under its id (a run that cites one still finds it), and a new test, a new id, starts at v1
even under a deleted test's name. A test without an id has no release and cannot be released
(:data:`NO_TEST_ID`). Each release file records the id; one whose id is not its folder's is
altered.

A release file holds the test's documents (a text test: ``procedure_text.md``, ``procedure.json``,
``test.py``; a code test: its ``openhtf/`` files; both: the ``functions/`` files those import),
their ``fingerprint`` (``run_provenance.definition_sha256``: what runs) and their ``content``: the
same documents hashed with the test's name (its visible ``id``, never the hidden one) taken out of
``procedure.json`` and of the text's ``# <id>`` line, and their line endings made one, so a
renamed test is still the release it was.
Each read checks both against the documents: a file altered by hand is said to be altered and
never trusted.

A test's :class:`State` compares its content with its releases': equal to one, it is that
release (``v4``); else ``Draft (from v4)``, v4 the latest release; never released, ``Draft``.
A number ``test.json`` was stamped with before releases existed (``version`` /
``version_sha256``) counts as a release whose documents were not kept: the test is that number
while its fingerprint is the stamped one, and the next release is one more.

Where a release is written (:func:`release`): a project on this PC, in its own ``releases/``; a
working copy of a share, on the share first — :func:`atomic_write.create_bytes` never replaces a
file, so two PCs never both make vN: the one that finds vN taken makes the next number —, then
the same bytes into the copy (the sync takes a file with the same bytes on both sides as no
conflict). A share that cannot be reached refuses. A working copy of a project on a server (the
web server, FTPS) releases on the server first, which numbers it, then writes the same release
into the copy (``project_sync_release``): a test changed on the server since the last sync, or
changed here and not synced yet, is refused (:data:`CHANGED_ON_SERVER`, :data:`NOT_SYNCED`).

Qt-free and engine-free.
"""

from __future__ import annotations

import base64
import getpass
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import (
    app_settings,
    atomic_write,
    paths,
    project_local_state,
    project_origin,
    project_source,
    run_provenance,
    test_format,
)
from .evidence_store import FUNCTIONS

log = logging.getLogger(__name__)

#: The project's folder of release files: ``releases/<test_id>/vN.json``.
RELEASES = "releases"
#: A text test's documents, in its folder.
TEXT_DOCUMENTS = ("procedure_text.md", "procedure.json", "test.py")

#: What the user is told when a release is refused.
SHARE_UNREACHABLE = "The share can't be reached: release it once it's back."
SERVER_UNREACHABLE = "The server can't be reached: release it once it's back."
NOTHING_RUNS = "Nothing to release yet: the test has nothing that runs."
SAME_AS_RELEASE = "Nothing to release: the files are exactly v{number}."
NO_TEST_ID = (
    "This test can't be released: its test.json can't be read. Open it in Studio and save it again."
)
CHANGED_ON_SERVER = (
    "This test changed on the server since your last sync: sync first, then release."
)
NOT_SYNCED = "This test has changes not synced yet: sync first, then release."

#: Where a project's releases are written (:func:`where`).
LOCAL = "local"  # a project on this PC: its own releases/
SHARE = "share"  # a working copy of a share: the share first, then the copy
SERVER = "server"  # a working copy of a project on a server: the server first, then the copy

_FILE = re.compile(r"v([1-9][0-9]*)\.json")
#: How many numbers taken meanwhile a release steps over before it gives up.
_ATTEMPTS = 100


class Refused(Exception):
    """A release that cannot be made; the message is for the user."""


@dataclass(frozen=True)
class Release:
    """One release of a test, as its file holds it (or a number stamped before releases)."""

    number: int
    released_at: str = ""
    released_by: str = ""
    pc: str = ""
    note: str = ""
    fingerprint: str = ""
    content: str = ""
    #: The documents, by their names; None: a number stamped before releases, nothing kept.
    documents: dict[str, bytes] | None = None
    #: Its file does not hold what it says (changed by hand, or not a release): never trusted.
    altered: bool = False
    #: The documents' format (``test_format.TEXT`` or ``CODE``); "" when none were kept.
    fmt: str = ""

    @property
    def stamped(self) -> bool:
        """A number from before releases: its documents were not kept."""
        return self.documents is None and not self.altered


@dataclass(frozen=True)
class State:
    """What a test's files are now: a release (:attr:`number`), or a draft from :attr:`latest`."""

    number: int | None  # the release the files are; None: a draft
    latest: int | None  # the test's latest release; None: never released
    fingerprint: str  # what runs (run_provenance.definition_sha256); "" nothing runs
    content: str  # the documents' content, the id taken out; "" no documents
    #: A trusted release file holds exactly these documents (its fingerprint is theirs): a run
    #: of them points to that release and keeps no copy of its own.
    held: bool = False

    @property
    def label(self) -> str:
        """``v7``, ``Draft (from v4)`` or ``Draft``."""
        if self.number is not None:
            return f"v{self.number}"
        return f"Draft (from v{self.latest})" if self.latest is not None else "Draft"

    @property
    def tone(self) -> str:
        """The chip's tone: a release green, a draft from one amber, a draft never released
        grey."""
        if self.number is not None:
            return "success"
        return "warning" if self.latest is not None else "neutral"

    @property
    def run_fields(self) -> tuple[int | None, bool]:
        """``(test_version, test_version_edited)`` a run of these files records: a release
        ``(N, False)``, a draft from vN ``(N, True)``, a draft never released ``(None, False)``."""
        if self.number is not None:
            return self.number, False
        return (self.latest, True) if self.latest is not None else (None, False)


@dataclass(frozen=True)
class Where:
    """Where a project's releases are written: :data:`LOCAL`, :data:`SHARE` (*share*: the share
    folder of the working copy) or :data:`SERVER`."""

    kind: str
    share: Path | None = None


@dataclass(frozen=True)
class Preview:
    """What *Release…* shows before releasing (:func:`preview`)."""

    state: State
    where: Where
    number: int  # the number the release would take now
    reachable: bool  # the share answers (always True off a share)
    #: The documents changed since the latest release, and those unchanged; None: the latest
    #: release kept no documents to compare (or the test was never released).
    changed: tuple[list[str], list[str]] | None
    released_by: str  # who releases (:func:`user`)
    pc: str  # on which PC (:func:`pc_name`)


# ---- The documents ---------------------------------------------------------------------------


def documents(project_root: Path, test: str) -> tuple[str, dict[str, bytes]]:
    """``(format, {name: bytes})``: the documents of *test* as they are now (the module's
    docstring); ``{}`` when it has none. An unreadable file raises OSError."""
    root = Path(project_root)
    test_dir = root / "tests" / test
    fmt = test_format.active_format(test_dir)
    if fmt == test_format.CODE:
        found = run_provenance.code_files(root, test)
    else:
        found = {name: test_dir / name for name in TEXT_DOCUMENTS if (test_dir / name).is_file()}
        if "test.py" in found:
            found.update(run_provenance.functions_imported(root, [found["test.py"]]))
    return fmt, {name: path.read_bytes() for name, path in found.items()}


def fingerprint_of(fmt: str, docs: dict[str, bytes]) -> str:
    """What runs of *docs*, as ``run_provenance.definition_sha256`` hashes it on disk."""
    if fmt == test_format.CODE:
        return run_provenance.code_digest(docs) if test_format.CODE_SCRIPT in docs else ""
    if "procedure.json" not in docs or "test.py" not in docs:
        return ""
    return run_provenance.text_digest(docs["procedure.json"], docs["test.py"])


def content_of(docs: dict[str, bytes]) -> str:
    """sha256 over *docs* with the test's name (its visible id) taken out and one line ending;
    ``""`` for none."""
    if not docs:
        return ""
    h = hashlib.sha256()
    for name in sorted(docs):
        data = _without_id(name, docs[name]).replace(b"\r\n", b"\n")
        h.update(f"{name}\0{len(data)}\0".encode())
        h.update(data)
    return h.hexdigest()


def _without_id(name: str, data: bytes) -> bytes:
    """*data* of the document *name* without the test's id: ``procedure.json`` without its
    top-level ``"id"`` (written again in one spelling: a rename rewrites the file), the text
    without its ``# <id>`` first line (``test_id.force_text_id_line``'s rule). A document that
    does not parse is kept as it is."""
    if name == "procedure.json":
        try:
            parsed = json.loads(data.decode("utf-8-sig"))
        except ValueError:
            return data
        if isinstance(parsed, dict):
            parsed.pop("id", None)
        return json.dumps(parsed, sort_keys=True, ensure_ascii=False).encode("utf-8")
    if name == "procedure_text.md":
        first, newline, rest = data.partition(b"\n")
        heading = first.lstrip()
        if heading.startswith(b"#") and not heading.startswith(b"##"):
            return rest if newline else b""
    return data


def changed_documents(
    docs: dict[str, bytes], release: Release
) -> tuple[list[str], list[str]] | None:
    """``(changed, unchanged)``: the names of *docs* (and of *release*'s documents they no
    longer have) that differ from *release*'s, the id taken out; None when it kept none."""
    if release.documents is None:
        return None
    kept = release.documents
    changed, unchanged = [], []
    for name in sorted(set(docs) | set(kept)):
        same = (
            name in docs
            and name in kept
            and content_of({name: docs[name]}) == content_of({name: kept[name]})
        )
        (unchanged if same else changed).append(name)
    return changed, unchanged


# ---- The release files -----------------------------------------------------------------------


def test_id_of(project_root: Path, test: str) -> str:
    """The hidden id of *test* (its folder's name in ``tests/``), as its ``test.json`` holds it
    (``test_format.read_test_id``); "" when it has none."""
    return test_format.read_test_id(Path(project_root) / "tests" / test)


def folder(project_root: Path, test: str) -> Path | None:
    """``releases/<test_id>`` of *test* (its folder's name in ``tests/``), its id read from its
    ``test.json`` (:func:`test_id_of`); None when it has none."""
    test_id = test_id_of(project_root, test)
    return folder_of(project_root, test_id) if test_id else None


def folder_of(project_root: Path, test_id: str) -> Path:
    """``releases/<test_id>`` of the project in *project_root*. A *test_id* that is not a test's
    hidden id (a name) raises ValueError: releases are never filed by name."""
    if not test_format.is_test_id(test_id):
        raise ValueError(f"{test_id!r} is not a test id")
    return Path(project_root) / RELEASES / test_id


def numbers_in(release_folder: Path) -> list[int]:
    """The numbers of the release files in *release_folder* (none when it is not there)."""
    try:
        names = [p.name for p in Path(release_folder).iterdir()]
    except FileNotFoundError:
        return []
    return sorted(int(m.group(1)) for m in map(_FILE.fullmatch, names) if m)


def read_releases(project_root: Path, test: str) -> list[Release]:
    """*test*'s release files, oldest first, each checked (:attr:`Release.altered`); none for a
    test without an id."""
    test_id = test_id_of(project_root, test)
    return releases_of(project_root, test_id) if test_id else []


def releases_of(project_root: Path, test_id: str) -> list[Release]:
    """The release files of the test whose id is *test_id*, oldest first, each checked
    (:attr:`Release.altered`). A folder that cannot be read is logged and reads as none."""
    found = folder_of(project_root, test_id)
    try:
        numbers = numbers_in(found)
    except OSError as exc:
        log.warning("The releases of %s cannot be read: %s", test_id, exc)
        return []
    return [_read(found / f"v{n}.json", n, test_id) for n in numbers]


def _read(path: Path, number: int, test_id: str) -> Release:
    """The release in *path*, numbered *number* by its name, of the test whose id is *test_id*
    (its folder's); altered when it does not hold what it says."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        docs = {name: decoded(value) for name, value in data["documents"].items()}
        fmt = data["format"]
        if (
            data["version"] != number
            or data["test_id"] != test_id
            or data["fingerprint"] != fingerprint_of(fmt, docs)
            or data["content"] != content_of(docs)
        ):
            raise ValueError("its documents are not the ones it says")
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        log.warning("%s is not trusted: %s", path, exc)
        return Release(number, altered=True)
    return Release(
        number,
        released_at=str(data.get("released_at", "")),
        released_by=str(data.get("released_by", "")),
        pc=str(data.get("pc", "")),
        note=str(data.get("note", "")),
        fingerprint=data["fingerprint"],
        content=data["content"],
        documents=docs,
        fmt=fmt,
    )


def encoded(data: bytes) -> str | dict:
    """A document as its file keeps it: its text, or ``{"base64": …}`` for one that is not
    UTF-8 text."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return {"base64": base64.b64encode(data).decode("ascii")}


def decoded(value: str | dict) -> bytes:
    if isinstance(value, str):
        return value.encode("utf-8")
    return base64.b64decode(value["base64"], validate=True)


def now_text() -> str:
    """The time a release file records: UTC, seconds, as the contract spells a time and every
    storage gives it back (``2026-10-26T22:20:42Z``)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def release_record(
    test: str,
    test_id: str,
    number: int,
    fmt: str,
    docs: dict[str, bytes],
    *,
    note: str = "",
    released_by: str = "",
    pc: str = "",
    stamped: bool = False,
) -> dict:
    """What the file of release *number* of *test* (its name then; *test_id* its hidden id)
    holding *docs* records (*stamped*: the number was given before releases existed, and the
    file is written for it now — with no time, person or PC, so every PC that writes it writes
    the same bytes and the sync never meets two of them)."""
    record = {
        "test": test,
        "test_id": test_id,
        "version": number,
        "format": fmt,
        "released_at": "" if stamped else now_text(),
        "released_by": "" if stamped else released_by,
        "pc": "" if stamped else pc,
        "machine_id": "" if stamped else app_settings.get_machine_id(),
        "note": note,
        "fingerprint": fingerprint_of(fmt, docs),
        "content": content_of(docs),
        "documents": {name: encoded(docs[name]) for name in sorted(docs)},
    }
    if stamped:
        record["numbered_before_releases"] = True
    return record


def record_bytes(record: dict) -> bytes:
    """A release file holding *record*."""
    return (json.dumps(record, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def release_bytes(
    test: str, test_id: str, number: int, fmt: str, docs: dict[str, bytes], **fields
) -> bytes:
    """The file of release *number* of *test* (*test_id*) holding *docs*
    (:func:`release_record`)."""
    return record_bytes(release_record(test, test_id, number, fmt, docs, **fields))


def release_number(name: str) -> int | None:
    """N of a release file's name ``vN.json``; None for any other name."""
    found = _FILE.fullmatch(name)
    return int(found.group(1)) if found else None


# ---- The state -------------------------------------------------------------------------------


def stamped_before(project_root: Path, test: str, releases: list[Release]) -> Release | None:
    """The number ``test.json`` was stamped with before releases, as a release that kept no
    documents; None when it has none, or a release file of that number holds it now."""
    held = test_format.read_version(Path(project_root) / "tests" / test)
    if held is None or held[0] in {r.number for r in releases}:
        return None
    return Release(held[0], fingerprint=held[1])


def state(project_root: Path, test: str) -> State:
    """What *test*'s files are now (:class:`State`). An unreadable document raises OSError."""
    fmt, docs = documents(project_root, test)
    return state_of(project_root, test, fmt, docs)


def state_of(project_root: Path, test: str, fmt: str, docs: dict[str, bytes]) -> State:
    """What *docs* (*test*'s documents, of format *fmt*, as :func:`documents` read them) are
    against *test*'s releases: the state of a run of exactly those bytes."""
    releases = read_releases(project_root, test)
    before = stamped_before(project_root, test, releases)
    content, fingerprint = content_of(docs), fingerprint_of(fmt, docs)
    equal = [r.number for r in releases if not r.altered and content and r.content == content]
    if before is not None and fingerprint and before.fingerprint == fingerprint:
        equal.append(before.number)
    numbers = [r.number for r in releases] + ([before.number] if before is not None else [])
    number = max(equal) if equal else None
    held = bool(fingerprint) and any(
        r.number == number and not r.altered and r.fingerprint == fingerprint for r in releases
    )
    return State(number, max(numbers) if numbers else None, fingerprint, content, held)


def latest(project_root: Path, test: str) -> int | None:
    """*test*'s latest release number now (:attr:`State.latest`): its release files' (under its
    id) and a number stamped before releases, read without its documents; None: never released.
    A folder that cannot be read raises OSError."""
    found = folder(project_root, test)
    numbers = numbers_in(found) if found is not None else []
    held = test_format.read_version(Path(project_root) / "tests" / test)
    if held is not None:
        numbers.append(held[0])
    return max(numbers, default=None)


def release_documents(
    project_root: Path, test_id: str, number: int, fingerprint: str
) -> dict[str, bytes] | None:
    """The documents release *number* of the test whose id is *test_id* holds, when its file is
    there, trusted and holds what ran with *fingerprint*; None otherwise (a release made before
    releases kept documents, one changed by hand, or another *number* made on another PC before
    releases). The test's folder may be gone: a deleted test's releases stay under its id."""
    path = folder_of(project_root, test_id) / f"v{number}.json"
    if not fingerprint or not path.is_file():
        return None
    found = _read(path, number, test_id)
    if found.altered or found.documents is None or found.fingerprint != fingerprint:
        return None
    return found.documents


def history(project_root: Path, test: str) -> list[Release]:
    """*test*'s releases, newest first, with the number stamped before releases (*Versions…*)."""
    releases = read_releases(project_root, test)
    before = stamped_before(project_root, test, releases)
    return sorted(releases + ([before] if before else []), key=lambda r: -r.number)


# ---- Releasing -------------------------------------------------------------------------------


def where(project_root: Path) -> Where:
    """Where *project_root*'s releases are written. Reads only this PC's state folder."""
    root = Path(project_root)
    if not paths.inside(root, project_local_state.working_copies_root()):
        return Where(LOCAL)
    origin = project_origin.load(root)
    if origin is None or not project_origin.describes(origin, root):
        return Where(LOCAL)
    share = project_source.share_of(origin)
    return Where(SHARE, share) if share is not None else Where(SERVER)


def _answers(share: Path) -> bool:
    return paths.unreachable(share) is None and paths.is_dir_quiet(share)


def _next_number(project_root: Path, test: str, test_id: str, place: Where) -> int:
    """One more than every number *test* (whose id is *test_id*) has had: on the share (for a
    copy of one), in the project, and stamped before releases. Never a number given before.
    Raises OSError when the share does not answer."""
    roots = [Path(project_root)] + ([place.share] if place.kind == SHARE else [])
    numbers = []
    for root in roots:
        numbers += numbers_in(folder_of(root, test_id))
    held = test_format.read_version(Path(project_root) / "tests" / test)
    if held is not None:
        numbers.append(held[0])
    return max(numbers, default=0) + 1


def preview(project_root: Path, test: str) -> Preview:
    """What releasing *test* now would make: its state, where it is written, its number (read
    on the share for a copy of one), the documents changed since the latest release. Touches
    the share: never on the GUI thread. Raises :class:`Refused` for a test without an id
    (:data:`NO_TEST_ID`)."""
    test_id = test_id_of(project_root, test)
    if not test_id:
        raise Refused(NO_TEST_ID)
    fmt, docs = documents(project_root, test)
    now = state_of(project_root, test, fmt, docs)
    place = where(project_root)
    reachable = place.kind != SHARE or _answers(place.share)
    try:
        number = _next_number(project_root, test, test_id, place if reachable else Where(LOCAL))
    except OSError:
        reachable, number = False, _next_number(project_root, test, test_id, Where(LOCAL))
    latest = next((r for r in history(project_root, test) if r.number == now.latest), None)
    changed = changed_documents(docs, latest) if latest is not None else None
    return Preview(now, place, number, reachable, changed, user(), pc_name())


@dataclass(frozen=True)
class Releasable:
    """A test that can be released as it is now (:func:`releasable`)."""

    test_id: str
    fmt: str
    docs: dict[str, bytes]
    state: State


def releasable(project_root: Path, test: str) -> Releasable:
    """*test*'s id, documents and state, once it can be released as it is now. Raises
    :class:`Refused`: no id (:data:`NO_TEST_ID`), nothing runs, the files are a release."""
    test_id = test_id_of(project_root, test)
    if not test_id:
        raise Refused(NO_TEST_ID)
    fmt, docs = documents(project_root, test)
    now = state_of(project_root, test, fmt, docs)
    if not now.fingerprint:
        raise Refused(NOTHING_RUNS)
    if now.number is not None:
        raise Refused(SAME_AS_RELEASE.format(number=now.number))
    return Releasable(test_id, fmt, docs, now)


def project_path(test: str, name: str) -> str:
    """The project path of *test*'s document *name* (:func:`documents`): ``functions/`` files
    are the project's, the others its folder's."""
    return name if name.startswith(f"{FUNCTIONS}/") else f"tests/{test}/{name}"


def release(project_root: Path, test: str, note: str = "", released_by: str | None = None) -> int:
    """Release *test* as it is now, with *note*: its file written where :func:`where` says;
    the number it took. *released_by*: who releases (``None``: :func:`user`; a server that lists
    its users is given one of them, ``release_users``). Raises :class:`Refused` (the message for
    the user). A working copy of a project on a server releases there
    (``project_sync_gateway.SyncGateway.release``)."""
    root = Path(project_root)
    place = where(root)
    by = user() if released_by is None else released_by
    if place.kind == SERVER:
        from .project_sync_gateway import gateway  # the sync sits above this module

        return gateway().release(root, test, note, released_by=by)
    ready = releasable(root, test)
    test_id, fmt, docs = ready.test_id, ready.fmt, ready.docs
    if place.kind == SHARE and not _answers(place.share):
        raise Refused(SHARE_UNREACHABLE)
    if place.kind == SHARE:  # released on another PC already, not synced here yet
        shared = releases_of(place.share, test_id)
        same = [r.number for r in shared if not r.altered and r.content == ready.state.content]
        if same:
            raise Refused(SAME_AS_RELEASE.format(number=max(same)))
    target = folder_of(place.share if place.kind == SHARE else root, test_id)

    def made(number: int) -> bytes:
        return release_bytes(
            test, test_id, number, fmt, docs, note=note.strip(), released_by=by, pc=pc_name()
        )

    try:
        target.mkdir(parents=True, exist_ok=True)
        number, data = _create(target, _next_number(root, test, test_id, place), made)
    except OSError as exc:
        if place.kind == SHARE and not _answers(place.share):
            raise Refused(SHARE_UNREACHABLE) from exc
        raise Refused(f"The release could not be written: {exc}") from exc
    if place.kind == SHARE:
        _copy_into(folder_of(root, test_id) / f"v{number}.json", data)
    return number


def _create(target: Path, number: int, made) -> tuple[int, bytes]:
    """``(number, bytes)`` of the release file created in *target*: *number*, or the next one
    free when it was taken meanwhile."""
    for candidate in range(number, number + _ATTEMPTS):
        data = made(candidate)
        try:
            atomic_write.create_bytes(target / f"v{candidate}.json", data)
            return candidate, data
        except FileExistsError:
            continue
    raise OSError(f"no free number from v{number} to v{number + _ATTEMPTS - 1} in {target}")


def _copy_into(path: Path, data: bytes) -> None:
    """The release just written on the share, the same bytes into the working copy; one that
    cannot be written is logged (the next sync brings it)."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write.create_bytes(path, data)
    except FileExistsError:
        log.warning("%s is in the working copy already; the sync settles it", path)
    except OSError as exc:
        log.warning("%s is on the share, not in the working copy yet: %s", path, exc)


def keep_stamped(project_root: Path, test: str) -> bool:
    """The number *test* was stamped with before releases, kept as a release file in the
    project while its files are still that number's (before a rename takes the name it was
    stamped with out of them): ``releases/<test_id>/vN.json`` written once. Whether it wrote
    one; never for a test without an id."""
    root = Path(project_root)
    test_id = test_id_of(root, test)
    if not test_id:
        return False
    fmt, docs = documents(root, test)
    before = stamped_before(root, test, releases_of(root, test_id))
    if before is None or not docs or before.fingerprint != fingerprint_of(fmt, docs):
        return False
    target = folder_of(root, test_id)
    target.mkdir(parents=True, exist_ok=True)
    data = release_bytes(test, test_id, before.number, fmt, docs, stamped=True)
    try:
        atomic_write.create_bytes(target / f"v{before.number}.json", data)
    except FileExistsError:
        return False
    return True


def user() -> str:
    """Who releases: this PC's user name ("" when it cannot be told)."""
    try:
        return getpass.getuser()
    except (OSError, KeyError, ImportError):
        return ""


def pc_name() -> str:
    """The PC a release is made on: its station name, else the computer's name."""
    return app_settings.station_id()
