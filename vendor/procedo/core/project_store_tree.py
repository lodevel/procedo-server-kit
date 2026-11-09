"""Projects on a tree: :class:`project_store.ProjectSource` over a folder or FTPS.

A storage that keeps a project as a folder of files — a share, an FTPS server — does every
project action with the tree primitives the app's project sync already uses (:class:`evidence_tree.TreeIO`), and the same rules:

* **What a project is**: its folder under the storage's root, named by the project, holding
  ``config/config.json`` with its ``project_id``. Its files are what the sync carries
  (``project_sync_scan.scan_server``: the evidence, ``.trash``, build output and this machine's
  state never); ``bundle/`` is the :class:`project_store.Bundle` and ``releases/`` the release
  records, by each test's hidden id (:mod:`project_tree_releases`).
* **A test is its hidden id**, which its ``test.json`` holds (``test_format.test_id_in``): its
  releases are filed under it, and a release names it. Each change keeps the two rules of
  ``data_format.md`` §4.3 true of what it touches, as the API's server does: a test folder
  holding a file holds a ``test.json`` with a valid ``test_id``, and no two tests hold one. A
  change after which either would fail is refused alone (``invalid``); a new project that fails
  either is refused whole.
* **A version** is the sha256 of a file's bytes (``tree.sha256``).
* **A change token** holds every file's stamp (size and time), compressed:
  a later :meth:`TreeProjectSource.changes_since` lists the folder again and reads only what
  moved. A token it cannot read is ``gone``, and the app reads the project whole.
* **A write names the version it replaces**: a file that changed since is ``changed`` with what
  is there now; a write of what is there already, or a delete of what is gone, is ``saved`` and
  writes nothing (a replay). Names compare as names (NFC, case folding): a write under another
  case replaces the file and keeps its spelling; a new file takes its folders' spelling.
* **A delete moves the file into the project's ``.trash``** (``project_sync_trash``), as a sync
  pass does; a test whose last file goes leaves its releases where they are, under its id.

Not a store the app opens by itself: the project sync talks to its trees directly. This is the
same storage behind the interface the JSON API implements, so the conformance suite proves the
two do every action alike (``tests/test_project_conformance.py``). Qt-free.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import zlib
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path, PurePath

from . import (
    atomic_write,
    project_origin,
    project_sync_pass,
    project_tree_releases,
    test_format,
    test_release,
)
from . import project_store_rules as rules
from .evidence_store import EvidenceError, OpContext
from .evidence_store_fs import os_error
from .evidence_tree import TreeIO, make_dirs, name_key, resolve_child
from .project_never_travels import TRASH_DIR
from .project_origin import CONFIG_FILE, Stamp
from .project_store import (
    BUNDLE_DIR,
    FUNCTIONS_DIR,
    GONE,
    REFUSED,
    RELEASES_DIR,
    SAVED,
    TESTS_DIR,
    Bundle,
    BundleWrite,
    Change,
    Changes,
    Delete,
    Draft,
    FileRef,
    Outcome,
    ProjectInfo,
    Pushed,
    ReleaseRecord,
    ReleaseRequest,
    Snapshot,
    TestInfo,
    Write,
    file_ref,
    in_text_folder,
    pack,
    version_of,
)
from .project_sync_plan import unsafe
from .project_sync_scan import scan_server
from .project_sync_trash import grave_name, into_server_trash, prune

log = logging.getLogger(__name__)

TAG = "project"
_TOKEN = "tree:"


def _token(listing: Mapping[str, Stamp]) -> str:
    stamps = {rel: [s.size, s.mtime, s.version] for rel, s in sorted(listing.items())}
    raw = json.dumps(stamps, separators=(",", ":")).encode("utf-8")
    return _TOKEN + base64.urlsafe_b64encode(zlib.compress(raw)).decode("ascii")


def _stamps(token: str) -> dict[str, Stamp]:
    gone = EvidenceError(
        "not_found", "This storage cannot answer from that change token.", code=GONE
    )
    if not token.startswith(_TOKEN):
        raise gone
    try:
        raw = zlib.decompress(base64.urlsafe_b64decode(token[len(_TOKEN) :]))
        found = json.loads(raw.decode("utf-8"))
        return {rel: Stamp(int(s[0]), float(s[1]), str(s[2])) for rel, s in found.items()}
    except (binascii.Error, zlib.error, UnicodeDecodeError, ValueError, TypeError, IndexError):
        raise gone from None


class TreeProjectSource:
    """:class:`project_store.ProjectSource` over the projects under *root* of a tree (the
    tree's own root by default). *folders*: projects whose folder the caller knows (a working
    copy's record names its project's), never looked for by listing *root*."""

    def __init__(
        self,
        tree: TreeIO,
        root: PurePath | None = None,
        folders: Mapping[str, PurePath] | None = None,
    ) -> None:
        self.tree = tree
        self.root = root if root is not None else tree.root
        #: project_id -> its folder, as last listed (or as the caller knows it)
        self._folders: dict[str, PurePath] = dict(folders or {})

    @contextmanager
    def _scope(self, ctx: OpContext | None) -> Iterator[None]:
        """One operation on the tree (one connection, one listing per folder, the cancel), its
        failures as the store's typed errors."""
        try:
            with project_sync_pass.operation(self.tree, ctx or OpContext()):
                yield
        except OSError as exc:
            raise os_error(exc) from exc

    def _path(self, base: PurePath, rel: str) -> PurePath:
        return base.joinpath(*rel.split("/"))

    # -- projects ------------------------------------------------------------------------

    def _list(self) -> list[ProjectInfo]:
        names = self.tree.list_names(self.root)
        if names is None:
            raise EvidenceError("not_found", f"{self.root} is not a folder on the storage")
        found = []
        for name in names:
            folder = self.root / name
            if not (
                self.tree.is_dir(folder) and self.tree.is_file(self._path(folder, CONFIG_FILE))
            ):
                continue
            try:
                project_id = project_origin.project_id_of(
                    self.tree.read_bytes(self._path(folder, CONFIG_FILE))
                )
            except EvidenceError as exc:
                log.info("%s is not a project: %s", folder, exc)
                continue
            self._folders[project_id] = folder
            found.append(ProjectInfo(project_id, name))
        return sorted(found, key=lambda p: (name_key(p.name), p.project_id))

    def _base(self, project_id: str) -> PurePath:
        if project_id not in self._folders:
            self._list()
        if project_id not in self._folders:
            raise EvidenceError("not_found", f"No project {project_id} on this storage.")
        return self._folders[project_id]

    def list_projects(self, ctx: OpContext | None = None) -> list[ProjectInfo]:
        with self._scope(ctx):
            return self._list()

    def _listing(self, base: PurePath) -> dict[str, Stamp]:
        """What the sync carries of the project, names it refuses left out."""
        return {rel: s for rel, s in scan_server(self.tree, base).items() if not unsafe(rel)}

    def _ref(self, base: PurePath, rel: str) -> FileRef:
        """One file as the interface gives it: a text document read whole, a binary one by its
        sha256, which the tree may answer without reading it."""
        path = self._path(base, rel)
        if in_text_folder(rel):
            return file_ref(rel, self.tree.read_bytes(path))
        return FileRef(rel, "sha256:" + self.tree.sha256(path), self.tree.stat(path)[0])

    def _bundle(self, base: PurePath, listing: Mapping[str, Stamp]) -> Bundle | None:
        files = {
            rel.split("/", 1)[1]: self.tree.read_bytes(self._path(base, rel))
            for rel in listing
            if rules.kind_of(rel) == BUNDLE_DIR
        }
        return Bundle(files) if files else None

    def _snapshot(self, project_id: str, base: PurePath) -> Snapshot:
        listing = self._listing(base)
        return Snapshot(
            project_id=project_id,
            name=base.name,
            token=_token(listing),
            files={
                rel: self._ref(base, rel)
                for rel in sorted(listing)
                if rules.kind_of(rel) == rules.FILE
            },
            bundle=self._bundle(base, listing),
            releases=project_tree_releases.all_records(self.tree, base),
        )

    def read_project(self, project_id: str, ctx: OpContext | None = None) -> Snapshot:
        with self._scope(ctx):
            return self._snapshot(project_id, self._base(project_id))

    def changes_since(self, project_id: str, token: str, ctx: OpContext | None = None) -> Changes:
        before = _stamps(token)
        with self._scope(ctx):
            base = self._base(project_id)
            now = self._listing(base)
            moved = {rel for rel, stamp in now.items() if before.get(rel) != stamp}
            touched = moved | (set(before) - set(now))
            ids = {rel.split("/")[1] for rel in touched if rules.kind_of(rel) == RELEASES_DIR}
            return Changes(
                token=_token(now),
                files={
                    rel: self._ref(base, rel)
                    for rel in sorted(moved)
                    if rules.kind_of(rel) == rules.FILE
                },
                deleted=sorted(
                    (rel for rel in set(before) - set(now) if rules.kind_of(rel) == rules.FILE),
                    key=name_key,
                ),
                bundle=self._bundle(base, now)
                if any(rules.kind_of(rel) == BUNDLE_DIR for rel in touched)
                else None,
                releases={
                    test_id: project_tree_releases.records(self.tree, base, test_id)
                    for test_id in sorted(ids)
                    if test_format.is_test_id(test_id)
                },
            )

    def create_project(
        self,
        name: str,
        files: Mapping[str, bytes],
        bundle: Mapping[str, bytes] | None = None,
        ctx: OpContext | None = None,
    ) -> Snapshot:
        name, sent, project_id = rules.checked_new_project(name, files)
        with self._scope(ctx):
            held = {p.project_id: p for p in self._list()}
            if project_id in held:
                base = self._folders[project_id]
                snapshot = self._snapshot(project_id, base)
                same = {p: f.version for p, f in snapshot.files.items()} == {
                    p: version_of(d) for p, d in sent.items()
                }
                if (
                    same
                    and base.name == name
                    and (snapshot.bundle or Bundle({})).files == dict(bundle or {})
                ):
                    return snapshot  # the same project again
                raise EvidenceError(
                    "invalid", "This project is on the storage already.", code="exists"
                )
            if any(name_key(p.name) == name_key(name) for p in held.values()) or self.tree.exists(
                resolve_child(self.tree, self.root, name)
            ):
                raise EvidenceError(
                    "invalid", f"A folder is named {name} on the storage already.", code="exists"
                )
            base = self.root / name
            self.tree.mkdir(base)
            for path, data in [
                *sent.items(),
                *((f"{BUNDLE_DIR}/{p}", d) for p, d in (bundle or {}).items()),
            ]:
                target = self._path(base, path)
                make_dirs(self.tree, base, target.parent)
                self.tree.write_atomic(target, data, TAG)
            self._folders[project_id] = base
            return self._snapshot(project_id, base)

    # -- tests and releases ----------------------------------------------------------------

    def _tests(self, base: PurePath, listing: Mapping[str, Stamp]) -> dict[str, str]:
        """Each test the folder holds a file of: its name's key -> its spelling."""
        return {name_key(t): t for t in (rules.test_of(rel) for rel in sorted(listing)) if t}

    def _test(
        self, base: PurePath, test: str, listing: Mapping[str, Stamp], files: bool
    ) -> TestInfo:
        prefix = f"{TESTS_DIR}/{test}/"
        names = [rel[len(prefix) :] for rel in listing if rel.startswith(prefix)]
        test_json = (
            self.tree.read_bytes(self._path(base, prefix + test_format.TEST_JSON)).decode(
                "utf-8", "replace"
            )
            if test_format.TEST_JSON in names
            else None
        )
        test_id = test_format.test_id_in(test_json) if test_json is not None else ""
        held = (
            project_tree_releases.records(self.tree, base, test_id, documents=False)
            if test_id
            else []
        )
        latest = max((r.version for r in held), default=None)
        refs = None
        if files:
            refs = {
                rel: self._ref(base, rel)
                for rel in sorted(listing)
                if rel.startswith(prefix) or rel.split("/", 1)[0] == FUNCTIONS_DIR
            }
        return TestInfo(test, test_id, test_format.format_of(test_json, names), latest, refs)

    def list_tests(self, project_id: str, ctx: OpContext | None = None) -> list[TestInfo]:
        with self._scope(ctx):
            base = self._base(project_id)
            listing = self._listing(base)
            tests = self._tests(base, listing)
            return [self._test(base, tests[key], listing, False) for key in sorted(tests)]

    def read_tests(
        self, project_id: str, names: Sequence[str], ctx: OpContext | None = None
    ) -> dict[str, TestInfo]:
        with self._scope(ctx):
            base = self._base(project_id)
            listing = self._listing(base)
            tests = self._tests(base, listing)
            return {
                name: self._test(base, tests[name_key(name)], listing, True)
                for name in names
                if name_key(name) in tests
            }

    def list_releases(
        self, project_id: str, test_id: str, ctx: OpContext | None = None
    ) -> list[ReleaseRecord]:
        with self._scope(ctx):
            return project_tree_releases.records(
                self.tree, self._base(project_id), test_id, documents=False
            )

    def read_release(
        self, project_id: str, test_id: str, version: int, ctx: OpContext | None = None
    ) -> ReleaseRecord:
        with self._scope(ctx):
            for record in project_tree_releases.records(self.tree, self._base(project_id), test_id):
                if record.version == version:
                    return record
        raise EvidenceError("not_found", f"No release v{version} under the test id {test_id}.")

    def _holding(self, base: PurePath, listing: Mapping[str, Stamp], test_id: str) -> str:
        """The test whose ``test.json`` holds *test_id*, as its folder is spelled; ``""``: none."""
        for test in self._tests(base, listing).values():
            marker = f"{TESTS_DIR}/{test}/{test_format.TEST_JSON}"
            if marker in listing and rules.id_in(self._read(base, marker)) == test_id:
                return test
        return ""

    def release(
        self, project_id: str, test_id: str, request: ReleaseRequest, ctx: OpContext | None = None
    ) -> ReleaseRecord:
        if not test_format.is_test_id(test_id):
            raise EvidenceError("invalid", f"{test_id!r} is not a test id")
        with self._scope(ctx):
            base = self._base(project_id)
            listing = self._listing(base)
            spelling = self._holding(base, listing, test_id)
            if not spelling:
                raise EvidenceError("not_found", f"No test of this project holds the id {test_id}.")
            index = {name_key(rel): rel for rel in listing}
            docs = rules.release_documents(
                request.files, index, lambda stored: self._read(base, stored)
            )
            held = project_tree_releases.records(self.tree, base, test_id, documents=False)
            rules.refuse_same_content(held, request.content)
            record = ReleaseRecord(
                test_id=test_id,
                test=spelling,
                version=project_tree_releases.next_number(self.tree, base, test_id),
                format=request.format,
                fingerprint=request.fingerprint,
                content=request.content,
                documents=docs,
                note=request.note,
                released_at=test_release.now_text(),
                released_by=request.released_by,
                pc=request.pc,
                machine_id=request.machine_id,
            )
            project_tree_releases.create(self.tree, base, record)
            return replace(record, documents=None)

    def import_release(
        self, project_id: str, record: ReleaseRecord, ctx: OpContext | None = None
    ) -> ReleaseRecord:
        """Its file written as it is, under its id, never over another release's."""
        with self._scope(ctx):
            base = self._base(project_id)
            held = project_tree_releases.records(self.tree, base, record.test_id, documents=False)
            found = rules.imported(held, record)
            if found is not None:
                return found
            project_tree_releases.create(self.tree, base, record)
            return replace(record, documents=None)

    # -- changes ---------------------------------------------------------------------------

    def push_changes(
        self,
        project_id: str,
        changes: Sequence[Change],
        drafts: Sequence[Draft] = (),
        ctx: OpContext | None = None,
    ) -> Pushed:
        """Each change against what the folder holds when its turn comes, the rules of a test's
        id included (:func:`project_store_rules.breaks`). *drafts* are not kept: on a folder storage a test's
        draft is its files (``test_release.state``)."""
        with self._scope(ctx):
            base = self._base(project_id)
            index = {name_key(rel): rel for rel in self._listing(base)}
            ids: dict[str, str] = {}  # each test's id, read once when a rule needs it
            grave = grave_name(project_sync_pass.tag())
            results = []
            for change in changes:
                if isinstance(change, Write):
                    results.append(self._write(base, index, ids, change))
                elif isinstance(change, Delete):
                    results.append(self._delete(base, index, ids, change, grave))
                else:
                    results.append(self._new_bundle(base, index, change, grave))
            return Pushed(_token(self._listing(base)), tuple(results))

    def _read(self, base: PurePath, rel: str) -> bytes:
        return self.tree.read_bytes(self._path(base, rel))

    def _write(
        self, base: PurePath, index: dict[str, str], ids: dict[str, str], change: Write
    ) -> Outcome:
        rel = rules.nfc(change.path)
        why = rules.never_a_file(rel)
        if why:
            return Outcome(change.path, REFUSED, kind="invalid", message=why)
        stored = index.get(name_key(rel))
        current = self._ref(base, stored) if stored else None
        version = version_of(change.data)
        verdict = rules.write_verdict(change.path, current, version, change.replaces)
        if verdict is not None:
            return verdict
        target = stored or rules.spell(index, rel)
        why = rules.breaks(index, ids, lambda m: self._read(base, m), target, change.data)
        if why:
            return Outcome(change.path, REFUSED, kind="invalid", message=why)
        path = self._path(base, target)
        make_dirs(self.tree, base, path.parent)
        self.tree.write_atomic(path, change.data, TAG)
        rules.kept(index, ids, target, change.data)
        return Outcome(change.path, SAVED, version)

    def _trash(self, base: PurePath, rel: str, grave: str) -> str:
        """*rel* into the project's ``.trash``, as a sync pass removes a file; why not."""
        why = into_server_trash(self.tree, base, rel, grave)
        hook = getattr(self.tree, "remove_empty_folder", None)
        if not why and hook is not None:
            prune(rel, lambda folder: hook(self._path(base, folder)))
        return why

    def _delete(
        self,
        base: PurePath,
        index: dict[str, str],
        ids: dict[str, str],
        change: Delete,
        grave: str,
    ) -> Outcome:
        stored = index.get(name_key(rules.nfc(change.path)))
        current = self._ref(base, stored) if stored is not None else None
        verdict = rules.delete_verdict(change.path, current, change.replaces)
        if verdict is not None or stored is None:
            return verdict or Outcome(change.path, SAVED)
        why = rules.breaks(index, ids, lambda m: self._read(base, m), stored, None)
        if why:
            return Outcome(change.path, REFUSED, kind="invalid", message=why)
        why = self._trash(base, stored, grave)
        if why:
            return Outcome(change.path, REFUSED, kind="denied", message=why)
        rules.kept(index, ids, stored, None)
        return Outcome(change.path, SAVED)

    def _new_bundle(
        self, base: PurePath, index: dict[str, str], change: BundleWrite, grave: str
    ) -> Outcome:
        held = {
            rel: rel.split("/", 1)[1] for rel in index.values() if rules.kind_of(rel) == BUNDLE_DIR
        }
        archive = pack({name: self._read(base, rel) for rel, name in held.items()}) if held else b""
        new = Bundle(dict(change.files))
        current = (version_of(archive), len(archive)) if held else None
        verdict = rules.bundle_verdict(current, new.version, change.replaces)
        if verdict is not None:
            return verdict
        for rel, name in held.items():
            if name not in new.files:
                self._trash(base, rel, grave)
        for name, data in new.files.items():
            target = self._path(base, f"{BUNDLE_DIR}/{name}")
            make_dirs(self.tree, base, target.parent)
            self.tree.write_atomic(target, data, TAG)
        return Outcome(rules.BUNDLE_PATH, SAVED, new.version)

    def rename_test(
        self,
        project_id: str,
        old: str,
        new: str,
        folder: Mapping[str, str],
        ctx: OpContext | None = None,
    ) -> Outcome:
        """``tests/<old>`` renamed ``tests/<new>``, a folder move only: its ``test.json`` moves
        with it, so its releases (``releases/<test_id>/``) and its evidence stay filed under its
        id, untouched."""
        new = rules.nfc(new)
        with self._scope(ctx):
            base = self._base(project_id)
            listing = self._listing(base)
            tests = self._tests(base, listing)
            named = {name_key(rel): v for rel, v in folder.items()}
            verdict = rules.rename_verdict(
                old,
                new,
                tests,
                lambda test: rules.holds(
                    listing, test, named, lambda rel: self._ref(base, rel).version
                ),
            )
            if verdict is not None:
                return verdict
            source = tests[name_key(old)]
            tests_root = base / TESTS_DIR
            leftover = resolve_child(self.tree, tests_root, new)
            if leftover.name != source and self.tree.is_dir(leftover):
                # A folder of that name holding no file the sync carries: set aside in the
                # project's .trash, never removed, so the rename has its name.
                aside = base / TRASH_DIR / grave_name(project_sync_pass.tag()) / TESTS_DIR
                make_dirs(self.tree, base, aside)
                self.tree.rename(leftover, aside / leftover.name)
            self.tree.rename(tests_root / source, tests_root / new)
            return Outcome(f"{TESTS_DIR}/{old}", SAVED)

    # -- bytes -----------------------------------------------------------------------------

    def fetch(
        self, project_id: str, ref: FileRef, dest: Path, ctx: OpContext | None = None
    ) -> Path:
        with self._scope(ctx):
            data = self.tree.read_bytes(self._path(self._base(project_id), ref.path))
        if version_of(data) != ref.version:
            raise EvidenceError(
                "invalid", f"{ref.path} changed on the storage since it was listed", code="changed"
            )
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        atomic_write.write_bytes(Path(dest), data)
        return Path(dest)
