"""The library's conformance suite: every action of :class:`project_store.LibrarySource` (list,
download, publish), stored by one PC and read back by another, on every storage
(``docs/plans/company_storage_design.md`` §6c): a folder and the JSON API's client against the
reference server here, a real FTPS server in ``test_library_conformance_ftps.py`` and a real
SFTP server in ``test_library_conformance_sftp.py``.

A PC's library is its own folders (``library_client.LOCAL_ROOTS``); a case switches PC by
pointing them elsewhere. A download is compared file by file with the folder that was
published, its manifest as the publish stamped it. A bundle holding a wheel over 1 MiB makes
the JSON API send its archive alone, both ways. Over the JSON API each case publishes items of
its own (:func:`own_items`), so a server holding other items answers it as an empty one would,
a list read back filtered to the case's own (:func:`_listed`).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import server_target
from test_project_conformance import (  # noqa: F401  (reference: a fixture)
    api_session,
    noise,
    outside_the_map,
    recording,
    reference,
)

from procedo.core import (
    bundle_registry,
    config_manager,
    export_packages,
    library_client,
    library_entry,
)
from procedo.core.evidence_store import EvidenceError
from procedo.core.library_api import ApiLibrary, holds_code
from procedo.core.library_client import LibraryConflict, ServerLibrary
from procedo.core.project_actions import ACTIONS
from procedo.core.project_store import PART_MAX

BUNDLE, CONFIG, EXPORT = library_entry.BUNDLE, library_entry.CONFIG, library_entry.EXPORT
PROTOCOL = frozenset(name.split(".", 1)[1] for name in ACTIONS if name.startswith("library."))


def _bundle(folder: Path, item_id: str, version: str, wheel: bytes) -> None:
    (folder / "wheels").mkdir(parents=True)
    (folder / "wheels" / "pack-1.0-py3-none-any.whl").write_bytes(wheel)
    (folder / "defaults.json").write_text("{}", encoding="utf-8")
    manifest = {"id": item_id, "version": version, "packs": [], "bundle_sha256": ""}
    (folder / "bundle.json").write_text(json.dumps(manifest), encoding="utf-8")


def _config(folder: Path, item_id: str, version: str, body: str) -> None:
    (folder / "templates").mkdir(parents=True)
    (folder / "templates" / "t.html").write_text(body, encoding="utf-8")
    manifest = {"manifest": {"name": item_id, "version": version}, "patterns": {}}
    (folder / "config.json").write_text(json.dumps(manifest), encoding="utf-8")


def _export(folder: Path, item_id: str, version: str, body: bytes) -> None:
    folder.mkdir(parents=True)
    (folder / "t.docx").write_bytes(body)
    schema = {"package_id": item_id, "package_version": version, "template": "t.docx", "fields": []}
    (folder / "schema.json").write_text(json.dumps(schema), encoding="utf-8")


#: kind -> (id, version, maker, its content on one PC, other content under the same name)
ITEMS: dict[str, tuple[str, str, Callable, Any, Any]] = {
    BUNDLE: ("baseline", "3.1.0", _bundle, noise("wheel", 2 * PART_MAX), noise("other", 999)),
    CONFIG: ("acme", "2.0.0", _config, "<p>one</p>", "<p>two</p>"),
    EXPORT: ("sheet", "1.0.0", _export, b"PK\x03\x04 one", b"PK\x03\x04 two"),
}


@dataclass
class Library:
    """One server's library and the PCs that use it."""

    name: str
    opener: Callable[[], Any]
    monkeypatch: Any
    root: Path
    called: set[str] = field(default_factory=set)

    def open(self) -> Any:
        library = self.opener()
        for action in PROTOCOL:
            method = getattr(library, action)

            def noting(*args: Any, _action: str = action, _method: Any = method, **kw: Any) -> Any:
                self.called.add(_action)
                return _method(*args, **kw)

            setattr(library, action, noting)
        return library

    def pc(self, name: str) -> Path:
        """Point this PC's library at *name*'s folders; their root."""
        root = self.root / name
        self.monkeypatch.setattr(bundle_registry, "bundle_store_path", lambda: root / "bundle")
        self.monkeypatch.setattr(config_manager, "get_configs_dir", lambda: root / "configs")
        self.monkeypatch.setattr(export_packages, "local_exports_dir", lambda: root / "exports")
        self.monkeypatch.setattr(export_packages, "local_reports_dir", lambda: root / "reports")
        return root


def _make(kind: str, other: bool = False) -> Path:
    item_id, version, maker, content, changed = ITEMS[kind]
    folder = library_client.local_folder(kind, item_id, version)
    maker(folder, item_id, version, changed if other else content)
    return folder


def _listed(lib: Library, kind: str) -> list:
    """The versions of *kind* the library lists that are this case's: on a server the
    environment names, the ones of its item; every one on a library of its own."""
    listed = lib.open().list(kind)
    if not server_target.external():
        return listed
    return [e for e in listed if e.id == ITEMS[kind][0]]


def own_items(monkeypatch) -> None:
    """This case's items: each id made new, so no other case, no earlier run, published it."""
    suffix = server_target.fresh_suffix()
    for kind, (item_id, *rest) in list(ITEMS.items()):
        monkeypatch.setitem(ITEMS, kind, (f"{item_id}-{suffix}", *rest))


def _files(folder: Path) -> dict[str, bytes]:
    return {
        p.relative_to(folder).as_posix(): p.read_bytes() for p in folder.rglob("*") if p.is_file()
    }


@dataclass(frozen=True)
class Case:
    check: Callable[[Library], None]
    actions: frozenset[str]
    #: The contract rules it catches (``procedo_server_contract.md``'s ``rule-<id>`` anchors).
    rules: tuple[str, ...] = ()


CASES: list[Case] = []


def conformance(*actions: str, rules: tuple[str, ...] = ()) -> Callable:
    def register(check: Callable[[Library], None]) -> Callable[[Library], None]:
        CASES.append(Case(check, frozenset(actions), tuple(rules)))
        return check

    return register


def params(backend: str, *marks: Any) -> list:
    return [
        pytest.param(c, backend, marks=list(marks), id=f"{c.check.__name__}-{backend}")
        for c in CASES
    ]


def run_case(case: Case, library: Library) -> None:
    case.check(library)
    assert sorted(library.called ^ case.actions) == [], "the actions named are the ones called"


@conformance(
    "publish", "list", "download",
    rules=("6.2.large-in-answer", "L1.list", "L3.publish", "B2.download"),
)  # fmt: skip
def a_version_published_downloads_byte_for_byte_on_another_pc(lib: Library) -> None:
    lib.pc("A")
    published = {}
    for kind in ITEMS:
        folder = _make(kind)
        entry = lib.open().publish(kind, ITEMS[kind][0], ITEMS[kind][1])
        published[kind] = (folder, entry)
    lib.pc("B")
    for kind, (folder, entry) in published.items():
        listed = _listed(lib, kind)
        assert [(e.ref, e.content_sha256) for e in listed] == [(entry.ref, entry.content_sha256)]
        landed = lib.open().download(kind, ITEMS[kind][0], ITEMS[kind][1])
        expected = _files(folder)
        manifest = library_entry.KINDS[kind].manifest
        expected[manifest] = library_entry.stamped_manifest(kind, folder)
        assert _files(landed) == expected, kind
        assert library_client.local_entry(kind, entry.id, entry.version).content_sha256 == (
            entry.content_sha256
        )


@conformance("publish", "list")
def a_version_is_published_once_whatever_it_holds(lib: Library) -> None:
    item, version = ITEMS[BUNDLE][:2]
    lib.pc("A")
    _make(BUNDLE)
    lib.open().publish(BUNDLE, item, version)
    with pytest.raises(LibraryConflict):
        lib.open().publish(BUNDLE, item, version)  # the same content
    lib.pc("C")
    _make(BUNDLE, other=True)
    with pytest.raises(LibraryConflict):
        lib.open().publish(BUNDLE, item, version)  # other content
    assert [e.ref for e in _listed(lib, BUNDLE)] == [f"{item}@{version}"]


@conformance("publish", "download", rules=("6.1.item-not-held-404", "L2.item"))
def a_download_over_other_content_is_a_conflict_and_over_the_same_no_download(lib: Library) -> None:
    item, version = ITEMS[EXPORT][:2]
    lib.pc("A")
    _make(EXPORT)
    lib.open().publish(EXPORT, item, version)
    lib.pc("B")
    mine = _files(_make(EXPORT, other=True))
    with pytest.raises(LibraryConflict):
        lib.open().download(EXPORT, item, version)
    assert _files(library_client.local_folder(EXPORT, item, version)) == mine  # untouched
    lib.pc("C")
    first = lib.open().download(EXPORT, item, version)
    assert lib.open().download(EXPORT, item, version) == first
    with pytest.raises(EvidenceError) as ei:
        lib.open().download(EXPORT, item, "9.9.9")
    assert ei.value.kind == "not_found"


def test_every_action_has_a_case():
    assert sorted(PROTOCOL - {a for c in CASES for a in c.actions}) == []


def test_an_item_holds_code_by_its_kind_or_by_python_inside():
    assert holds_code("tool", ["readme.md"])
    assert holds_code("bundle", ["wheels/pack-1.0-py3-none-any.whl", "bundle.json"])
    assert holds_code("config", ["scripts/check.py"])
    assert not holds_code("export", ["t.docx", "schema.json"])


# ---------------------------------------------------------------------------
# The storages
# ---------------------------------------------------------------------------


@pytest.fixture
def folder_library(tmp_path, monkeypatch) -> Library:
    disk = tmp_path / "fileserver"
    disk.mkdir()
    return Library(
        "folder", lambda: ServerLibrary("filesystem", {"root": str(disk)}), monkeypatch, tmp_path
    )


@pytest.mark.parametrize(("case", "backend"), params("folder"))
def test_every_library_action_on_a_folder(case, backend, folder_library):
    run_case(case, folder_library)


@pytest.fixture
def api_library(reference, tmp_path, monkeypatch):  # noqa: F811
    own_items(monkeypatch)
    pcs = iter(range(1000))
    calls: list[tuple[str, str]] = []

    def opener() -> ApiLibrary:
        library = ApiLibrary(api_session(reference), tmp_path / f"cache-{next(pcs)}")
        return recording(library, calls, PROTOCOL)

    return Library("api", opener, monkeypatch, tmp_path), calls


@pytest.mark.parametrize(("case", "backend"), params("api", pytest.mark.servers))
def test_every_library_action_over_the_json_api(case, backend, api_library):
    library, calls = api_library
    run_case(case, library)
    assert outside_the_map(calls, "library") == []
    if case.check is a_version_published_downloads_byte_for_byte_on_another_pc:
        assert {"uploadBlob", "downloadBlob"} <= {op for _action, op in calls}  # alone, both ways
