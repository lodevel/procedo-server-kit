"""The library over the JSON API: :class:`project_store.LibrarySource` for a server that speaks
``data_format.md`` §5.3 (L1–L3), the twin of :class:`library_client.ServerLibrary` (a tree).

A version is one archive here (:func:`project_store.pack` of its folder, the manifest as a
publish stamps it: ``library_entry.stamped_manifest``), its content the hash that manifest
declares. The rules are the tree's, by the same functions: a download lands through
:func:`library_client.install` (the same content here is no download, other content a conflict,
what arrived checked against the declared hash), a publish starts from
:func:`library_client.to_publish` and a version the server holds already is refused whatever it
holds (``already`` included), as on a tree.

``contains_code`` is this client's word (contract question P29): a capability, wizard, tool or
package always holds code, and any item holding Python (a ``.py`` file, a wheel) does too. Only a
login with the publisher role publishes one, and :func:`code_refused` tells the window which items
a login without it cannot send, before it tries.

The library of a web server (``library_client.configured``). Every call waits on the server:
on a worker, never on the GUI thread. Qt-free.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import library_client, library_entry
from .api_session import ApiSession
from .evidence_api_http import ApiError
from .evidence_store import EvidenceError, OpContext
from .library_client import LibraryConflict
from .library_entry import KINDS, Entry
from .project_api_blobs import ApiBlobs
from .project_store import pack, unpack, version_of

ITEM_FORMAT = 1
#: The kinds that always hold code a PC would run (contract question P29).
ALWAYS_CODE = frozenset({"capability", "wizard", "tool", "package"})
#: What makes a file Python a PC would run.
_CODE_SUFFIXES = (".py", ".pyw", ".pyz", ".whl")
#: The owner of the library's large files (B1, B2).
OWNER = "library"
#: The role that publishes an item holding code (``data_format.md`` §5.3).
PUBLISHER = "publisher"
#: Why *Publish* is greyed for an item :func:`code_refused` names.
CODE_NEEDS_PUBLISHER = "Publishing code needs a login with the publisher role."


def holds_code(kind: str, names: list[str]) -> bool:
    """Whether an item of *kind* with files *names* holds Python a PC would run."""
    return kind in ALWAYS_CODE or any(n.lower().endswith(_CODE_SUFFIXES) for n in names)


def _paths(root: Path) -> list[Path]:
    """The files of this PC's folder *root* a publish sends, the shell's own left out."""
    return [
        p
        for p in sorted(root.rglob("*"))
        if p.is_file() and p.name not in library_entry.OS_SHELL_BASENAMES
    ]


def _files(kind: str, entry: Entry) -> dict[str, bytes]:
    """The files of this PC's *entry* as a publish sends them: the manifest stamped."""
    spec = KINDS[kind]
    root = Path(entry.root or ".")
    files = {p.relative_to(root).as_posix(): p.read_bytes() for p in _paths(root)}
    files[spec.manifest] = library_entry.stamped_manifest(kind, root)
    return files


def code_refused(library: object, entries: list[Entry], ctx: OpContext | None = None) -> set[str]:
    """The refs of this PC's *entries* that *library*'s login may not publish: the ones holding
    code, when the login lacks the publisher role (``data_format.md`` §5.3). A server of folders
    has no roles: none. Reads the server's ``/info`` and this PC's folders: on a worker."""
    if not isinstance(library, ApiLibrary):
        return set()
    with library.session.operation(ctx):
        if PUBLISHER in library.session.roles():
            return set()
    return {
        e.ref
        for e in entries
        if e.root is not None
        and holds_code(e.kind, [p.relative_to(e.root).as_posix() for p in _paths(e.root)])
    }


class ApiLibrary:
    """The library of one JSON API server. A downloaded large archive is kept in *cache_dir*."""

    def __init__(self, session: ApiSession, cache_dir: Path) -> None:
        self.session = session
        self.blobs = ApiBlobs(session, Path(cache_dir) / "blobs")

    def list(self, kind: str, ctx: OpContext | None = None) -> list[Entry]:
        with self.session.operation(ctx):
            items = self.session.call("listLibrary", "GET", "library", query={"kind": kind}).data
        return sorted(
            (
                Entry(kind, str(i["id"]), str(i["version"]), str(i["content_sha256"]),
                      int(i.get("content_bytes") or 0))
                for i in items.get("items") or []
            ),
            key=lambda e: (e.id, e.version),
        )  # fmt: skip

    def download(self, kind: str, item_id: str, version: str, ctx: OpContext | None = None) -> Path:
        with self.session.operation(ctx):
            try:
                answer = self.session.call(
                    "getLibraryItem", "GET", "library", kind, item_id, version
                )
            except ApiError as exc:
                if exc.status == 404:
                    raise EvidenceError(
                        "not_found",
                        f"{item_id}@{version} is not in the {KINDS[kind].label} library there.",
                    ) from exc
                raise
            item = answer.data
            archive = item["archive"]
            data = answer.parts.get(str(archive["version"]))
            if data is None:
                kept = self.blobs.kept(
                    OWNER,
                    str(archive["version"]),
                    int(archive["size"]),
                    ctx,
                    archive.get("store_id"),
                )
                data = kept.read_bytes()
        remote = Entry(
            kind, item_id, version, str(item["content_sha256"]), int(item.get("content_bytes") or 0)
        )

        def fill(staging: Path) -> None:
            try:
                files = unpack(data)
            except ValueError as exc:
                raise EvidenceError("corrupt", f"{remote.ref} is not an archive: {exc}") from exc
            for name, content in files.items():
                target = staging.joinpath(*name.split("/"))
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)

        return library_client.install(kind, remote, fill)

    def publish(self, kind: str, item_id: str, version: str, ctx: OpContext | None = None) -> Entry:
        entry = library_client.to_publish(kind, item_id, version)
        files = _files(kind, entry)
        archive = pack(files)
        with self.session.operation(ctx):
            store = self.session.store_id()
            body = {
                "format_version": ITEM_FORMAT,
                "kind": kind,
                "id": item_id,
                "version": version,
                "content_sha256": entry.content_sha256,
                "content_bytes": entry.size,
                "manifest": json.loads(files[KINDS[kind].manifest].decode("utf-8")),
                "contains_code": holds_code(kind, list(files)),
                "archive": {
                    "version": version_of(archive),
                    "size": len(archive),
                    "store_id": store,
                },
            }
            parts = self.blobs.parts(OWNER, {"archive.zip": archive}, body, ctx)
            try:
                answer = self.session.call(
                    "publishLibraryItem", "POST", "library", kind, body=body, parts=parts or None
                )
            except ApiError as exc:
                if exc.status == 409:
                    raise LibraryConflict(
                        library_client.exists_sentence(KINDS[kind].label, entry)
                    ) from exc
                raise
        if answer.data.get("status") == "already":
            raise LibraryConflict(library_client.exists_sentence(KINDS[kind].label, entry))
        return entry
