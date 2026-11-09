"""Projects over the JSON API: :class:`project_store.ProjectSource` for a server that speaks
``docs/plans/web_api_projects.md`` (``P1``–``P11``), Procedo's client of it.

Each action is the calls :data:`project_actions.ACTIONS` names for it, and no other:

* **Reads** (``P2``, ``P3``, ``P4``–``P8b``) name the versions this client holds already
  (``have``), so their bytes travel once; a ``have`` too long for a URL goes in the body of the
  read's ``POST …/read`` twin. A text document comes inside the JSON; a binary one of at most
  1 MiB as a part of the answer, kept here by its version; a larger one alone, after the JSON,
  when it is fetched (B2, resumed with ``Range``: :class:`project_api_blobs.ApiBlobs`). The bundle
  is one archive (:func:`project_store.pack`), unpacked into its folder's files; the releases are
  records with their documents.
* **Writes** (``P9``, ``P10``, ``P11``) send a text document inside the JSON whatever its size,
  a binary one of at most 1 MiB as a part of the same request, a larger one alone first (B1).
  Each change of ``P9`` answers on its own: ``saved``, ``changed`` (with what the server holds
  now) or ``refused``; one conflict never blocks the rest. A test is renamed by ``P9``'s folder
  move, naming every file it moves: its ``test.json`` moves with it, so it keeps its id.
* **A test is its hidden id** (``data_format.md`` §4.3, §4.4): a Test and a Draft carry the
  ``test_id`` its ``test.json`` holds, and its releases are read and made under
  ``releases/{test_id}`` (``P8``, ``P8b``, ``P10``), keyed by it in a Project.
* A token the server cannot answer from is ``410 gone`` (code :data:`project_store.GONE`): the
  app then reads the project whole.

Discovery and the storage new files go to are the session's (:class:`api_session.ApiSession`).
The projects of a web server: the working copy's sync over it is :mod:`project_sync_api`.
Every call waits on the server: on a worker, never on the GUI thread.
Qt-free.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from . import atomic_write
from .api_session import ApiSession
from .evidence_api_http import HAVE_IN_QUERY, Answer, ApiError
from .evidence_store import EvidenceError, OpContext
from .project_api_blobs import ApiBlobs
from .project_origin import CONFIG_FILE, project_id_of
from .project_store import (
    BUNDLE_DIR,
    PART_MAX,
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
    is_text,
    pack,
    unpack,
    version_of,
)

PROJECT_FORMAT = 1
_SHA_HEX = re.compile(r"[0-9a-f]{64}")


#: How ``missing`` names the bundle's archive (``data_format.md`` §4.2).
_BUNDLE = f"{BUNDLE_DIR}/"


def _missing(data: Mapping[str, Any]) -> dict[str, str]:
    """A Project's or a ProjectChanges' ``missing`` (P28): the files the server cannot give, by
    path; their bytes are never asked for."""
    found = data.get("missing")
    return {str(k): str(v) for k, v in found.items()} if isinstance(found, dict) else {}


def _bundle_version(data: Mapping[str, Any]) -> str:
    """The version of the bundle a Project or a ProjectChanges names, its bytes given or not."""
    bundle = data.get("bundle")
    archive = bundle.get("archive") if isinstance(bundle, dict) else None
    return str(archive.get("version") or "") if isinstance(archive, dict) else ""


def _draft(test: str, obj: Any) -> Draft | None:
    """A Test's ``draft``, ``None`` when it holds none (not known)."""
    if not isinstance(obj, dict):
        return None
    based_on = obj.get("based_on")
    return Draft(
        test=test,
        test_id=str(obj["test_id"]),
        fingerprint=str(obj["fingerprint"]),
        content=str(obj["content"]),
        based_on=based_on if isinstance(based_on, int) and not isinstance(based_on, bool) else None,
    )


def _optional(**fields: Any) -> dict[str, Any]:
    """The fields that have a value: a request leaves out what is not known."""
    return {key: value for key, value in fields.items() if value not in (None, "")}


class ApiProjectSource:
    """The projects of one JSON API server. A downloaded large file is kept in *cache_dir*."""

    def __init__(self, session: ApiSession, cache_dir: Path) -> None:
        self.session = session
        self.blobs = ApiBlobs(session, Path(cache_dir) / "blobs")
        #: The bytes of every part received or sent, by version: what ``have`` names.
        self._held: dict[str, bytes] = {}

    @property
    def identity(self) -> str:
        """The server's identity: its sign-in's key."""
        return str(self.session.identity)

    def close(self) -> None:
        """Its connection to the server, closed (a gateway call closes what it built)."""
        self.session.close()

    # -- the bytes -------------------------------------------------------------------------

    def _have(self) -> list[str]:
        return sorted(self._held)

    def _read(
        self,
        op: str,
        twin: str,
        segments: tuple[str, ...],
        query: Mapping[str, str] | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> Answer:
        """A read with ``have``: GET with it in the query, or its ``POST …/read`` twin with it in
        the body when it is too long for a URL. The parts it brought are kept."""
        have = self._have()
        if len(",".join(have)) > HAVE_IN_QUERY:
            answer = self.session.call(
                twin, "POST", *segments, "read", body={**(body or {}), "have": have}
            )
        else:
            sent = dict(query or {})
            if have:
                sent["have"] = ",".join(have)
            answer = self.session.call(op, "GET", *segments, query=sent or None)
        self._held.update(answer.parts)
        return answer

    def _bytes(self, owner: str, obj: Mapping[str, Any], ctx: OpContext | None) -> bytes:
        """A File's bytes: its text, a part held, or downloaded alone."""
        if isinstance(obj.get("text"), str):
            return str(obj["text"]).encode("utf-8")
        version = str(obj["version"])
        if version not in self._held:
            kept = self.blobs.kept(owner, version, int(obj["size"]), ctx, obj.get("store_id"))
            return kept.read_bytes()
        return self._held[version]

    def _sent(self, owner: str, files: Mapping[str, bytes]) -> None:
        """What a write sent is held now: it never comes back down."""
        for data in files.values():
            if len(data) <= PART_MAX:
                self._held[version_of(data)] = data
            else:
                kept = self.blobs.cached(owner, version_of(data))
                kept.parent.mkdir(parents=True, exist_ok=True)
                atomic_write.write_bytes(kept, data)

    def _ref_json(self, data: bytes, store_id: int) -> dict[str, Any]:
        return {"version": version_of(data), "size": len(data), "store_id": store_id}

    def _file_json(self, path: str, data: bytes, store_id: int) -> dict[str, Any]:
        if is_text(path, data):
            return {"version": version_of(data), "size": len(data), "text": data.decode("utf-8")}
        return self._ref_json(data, store_id)

    def _bundle_json(self, files: Mapping[str, bytes], archive: bytes) -> dict[str, Any]:
        manifest = Bundle(files).manifest
        if not isinstance(manifest.get("id"), str) or not isinstance(manifest.get("version"), str):
            raise EvidenceError("invalid", "The bundle's bundle.json names no id and version.")
        sha = manifest.get("bundle_sha256")
        return _optional(
            bundle_id=manifest["id"],
            bundle_version=manifest["version"],
            bundle_sha256=sha if isinstance(sha, str) and _SHA_HEX.fullmatch(sha) else None,
        )

    # -- what the server answers --------------------------------------------------------------

    def _file(self, path: str, obj: Mapping[str, Any]) -> FileRef:
        """A File as the interface gives it, with the storage its bytes are in (B2 reads there)."""
        text, store = obj.get("text"), obj.get("store_id")
        return FileRef(
            path,
            str(obj["version"]),
            int(obj["size"]),
            text if isinstance(text, str) else None,
            store if isinstance(store, int) and not isinstance(store, bool) else None,
        )

    def _bundle(self, owner: str, obj: Any, ctx: OpContext | None) -> Bundle | None:
        if not isinstance(obj, dict):
            return None
        try:
            return Bundle(unpack(self._bytes(owner, obj["archive"], ctx)))
        except ValueError as exc:
            raise EvidenceError(
                "corrupt", f"The project's bundle is not an archive: {exc}"
            ) from exc

    def _release(
        self, owner: str, obj: Mapping[str, Any], ctx: OpContext | None, documents: bool = True
    ) -> ReleaseRecord:
        docs = obj.get("documents")
        return ReleaseRecord(
            test_id=str(obj["test_id"]),
            test=str(obj["test"]),
            version=int(obj["version"]),
            format=str(obj["format"]),
            fingerprint=str(obj["fingerprint"]),
            content=str(obj["content"]),
            documents={name: self._bytes(owner, f, ctx) for name, f in docs.items()}
            if documents and isinstance(docs, dict)
            else None,
            note=str(obj.get("note", "")),
            released_at=str(obj.get("released_at", "")),
            released_by=str(obj.get("released_by", "")),
            pc=str(obj.get("pc", "")),
            machine_id=str(obj.get("machine_id", "")),
            numbered_before_releases=bool(obj.get("numbered_before_releases", False)),
        )

    def _releases(
        self, owner: str, obj: Any, ctx: OpContext | None
    ) -> dict[str, list[ReleaseRecord]]:
        return {
            test_id: [self._release(owner, r, ctx) for r in held]
            for test_id, held in (obj or {}).items()
        }

    def _test(self, obj: Mapping[str, Any], files: bool) -> TestInfo:
        latest = obj.get("latest_release")
        return TestInfo(
            test=str(obj["test"]),
            test_id=str(obj["test_id"]),
            format=str(obj["format"]),
            latest_release=int(latest) if isinstance(latest, int) else None,
            files={p: self._file(p, f) for p, f in (obj.get("files") or {}).items()}
            if files
            else None,
            draft=_draft(str(obj["test"]), obj.get("draft")),
        )

    def _outcome(self, result: Mapping[str, Any]) -> Outcome:
        current = result.get("current")
        return Outcome(
            path=str(result["path"]),
            status=str(result["status"]),
            version=str(result.get("version", "")),
            current=self._file(str(result["path"]), current) if isinstance(current, dict) else None,
            kind=str(result.get("kind", "")),
            message=str(result.get("message", "")),
            moved_evidence=tuple(result.get("moved_evidence") or ()),
        )

    # -- projects --------------------------------------------------------------------------

    def list_projects(self, ctx: OpContext | None = None) -> list[ProjectInfo]:
        with self.session.operation(ctx):
            listed = self.session.call("listProjects", "GET", "projects").data
        return [
            ProjectInfo(str(p["project_id"]), str(p.get("name") or p.get("path") or ""))
            for p in listed.get("projects") or []
        ]

    def _snapshot(self, data: Mapping[str, Any], ctx: OpContext | None) -> Snapshot:
        owner = str(data["project_id"])
        missing = _missing(data)
        return Snapshot(
            project_id=owner,
            name=str(data.get("name", "")),
            token=str(data.get("token", "")),
            files={p: self._file(p, f) for p, f in (data.get("files") or {}).items()},
            bundle=None if _BUNDLE in missing else self._bundle(owner, data.get("bundle"), ctx),
            releases=self._releases(owner, data.get("releases"), ctx),
            missing=missing,
            bundle_version=_bundle_version(data),
        )

    def create_project(
        self,
        name: str,
        files: Mapping[str, bytes],
        bundle: Mapping[str, bytes] | None = None,
        ctx: OpContext | None = None,
    ) -> Snapshot:
        if CONFIG_FILE not in files:
            raise EvidenceError("invalid", f"A new project's files hold {CONFIG_FILE}.")
        owner = project_id_of(files[CONFIG_FILE])
        with self.session.operation(ctx):
            store = self.session.store_id()
            body: dict[str, Any] = {
                "format_version": PROJECT_FORMAT,
                "name": name,
                "files": {path: self._file_json(path, data, store) for path, data in files.items()},
            }
            binaries = {p: d for p, d in files.items() if not is_text(p, d)}
            if bundle:
                archive = pack(bundle)
                body["bundle"] = {
                    **self._bundle_json(bundle, archive),
                    "archive": self._ref_json(archive, store),
                }
                binaries[f"{BUNDLE_DIR}.zip"] = archive
            parts = self.blobs.parts(owner, binaries, body, ctx)
            data = self.session.call(
                "createProject", "POST", "projects", body=body, parts=parts or None
            ).data
            self._sent(owner, binaries)
            return self._snapshot(data, ctx)

    def read_project(self, project_id: str, ctx: OpContext | None = None) -> Snapshot:
        with self.session.operation(ctx):
            answer = self._read("getProject", "readProject", ("projects", project_id))
            return self._snapshot(answer.data, ctx)

    def changes_since(self, project_id: str, token: str, ctx: OpContext | None = None) -> Changes:
        with self.session.operation(ctx):
            data = self._read(
                "getProject",
                "readProject",
                ("projects", project_id),
                query={"since": token},
                body={"since": token},
            ).data
            missing = _missing(data)
            return Changes(
                token=str(data["token"]),
                files={p: self._file(p, f) for p, f in (data.get("files") or {}).items()},
                deleted=list(data.get("deleted") or []),
                bundle=None
                if _BUNDLE in missing
                else self._bundle(project_id, data.get("bundle"), ctx),
                releases=self._releases(project_id, data.get("releases"), ctx),
                missing=missing,
                bundle_version=_bundle_version(data),
            )

    # -- tests and releases ----------------------------------------------------------------

    def list_tests(self, project_id: str, ctx: OpContext | None = None) -> list[TestInfo]:
        with self.session.operation(ctx):
            data = self.session.call("listTests", "GET", "projects", project_id, "tests").data
        return [self._test(t, files=False) for t in data.get("tests") or []]

    def read_tests(
        self, project_id: str, names: Sequence[str], ctx: OpContext | None = None
    ) -> dict[str, TestInfo]:
        """``P7`` for one name, ``P5`` for several, ``P6`` when a name holds a comma or the
        names are too long for a URL."""
        segments = ("projects", project_id, "tests")
        with self.session.operation(ctx):
            if len(names) == 1:
                try:
                    data = self._read("getTest", "readTest", (*segments, names[0])).data
                except ApiError as exc:
                    if exc.status == 404:
                        return {}
                    raise
                return {names[0]: self._test(data, files=True)}
            joined = ",".join(names)
            if any("," in n for n in names) or len(joined) > HAVE_IN_QUERY:
                answer = self.session.call(
                    "readTests",
                    "POST",
                    *segments,
                    "read",
                    body={"names": list(names), "have": self._have()},
                )
                self._held.update(answer.parts)
            else:
                answer = self._read(
                    "listTests",
                    "readTests",
                    segments,
                    query={"names": joined},
                    body={"names": list(names)},
                )
            found = answer.data.get("tests") or {}
            return {name: self._test(t, files=True) for name, t in found.items()}

    def list_releases(
        self, project_id: str, test_id: str, ctx: OpContext | None = None
    ) -> list[ReleaseRecord]:
        segments = ("projects", project_id, "releases", test_id)
        with self.session.operation(ctx):
            data = self.session.call("listReleases", "GET", *segments).data
        return [self._release(project_id, r, ctx, False) for r in data.get("releases") or []]

    def read_release(
        self, project_id: str, test_id: str, version: int, ctx: OpContext | None = None
    ) -> ReleaseRecord:
        segments = ("projects", project_id, "releases", test_id, str(version))
        with self.session.operation(ctx):
            data = self._read("getRelease", "readRelease", segments).data
            return self._release(project_id, data, ctx)

    def release(
        self, project_id: str, test_id: str, request: ReleaseRequest, ctx: OpContext | None = None
    ) -> ReleaseRecord:
        body = _optional(
            format=request.format,
            fingerprint=request.fingerprint,
            content=request.content,
            note=request.note,
            released_by=request.released_by,
            pc=request.pc,
            machine_id=request.machine_id,
        )
        body["files"] = dict(request.files)
        segments = ("projects", project_id, "releases", test_id)
        with self.session.operation(ctx):
            data = self.session.call("release", "POST", *segments, body=body).data
        return self._release(project_id, data, ctx, False)

    def import_release(
        self, project_id: str, record: ReleaseRecord, ctx: OpContext | None = None
    ) -> ReleaseRecord:
        """P10 with ``"import": true`` (an importer login, §6.10): the number, the date, the name
        released under and the documents whole — text, or a reference whose bytes ride as a part.
        The server answers the same release again as it is, another number ``409 changed``."""
        body = _optional(
            format=record.format,
            fingerprint=record.fingerprint,
            content=record.content,
            note=record.note,
            released_by=record.released_by,
            pc=record.pc,
            machine_id=record.machine_id,
            released_at=record.released_at,
            test=record.test,
        )
        body.update({"import": True, "version": record.version})
        if record.numbered_before_releases:
            body["numbered_before_releases"] = True
        segments = ("projects", project_id, "releases", record.test_id)
        with self.session.operation(ctx):
            store = self.session.store_id()
            documents: dict[str, Any] = {}
            binaries: dict[str, bytes] = {}
            for name, data in sorted((record.documents or {}).items()):
                try:
                    text = data.decode("utf-8")
                except UnicodeDecodeError:
                    documents[name] = self._ref_json(data, store)
                    binaries[name] = data
                else:
                    documents[name] = {"version": version_of(data), "size": len(data), "text": text}
            body["documents"] = documents
            parts = self.blobs.parts(project_id, binaries, body, ctx)
            data = self.session.call(
                "release", "POST", *segments, body=body, parts=parts or None
            ).data
            self._sent(project_id, binaries)
        return self._release(project_id, data, ctx, False)

    # -- changes ---------------------------------------------------------------------------

    def push_changes(
        self,
        project_id: str,
        changes: Sequence[Change],
        drafts: Sequence[Draft] = (),
        ctx: OpContext | None = None,
    ) -> Pushed:
        with self.session.operation(ctx):
            store = self.session.store_id()
            sent: list[dict[str, Any]] = []
            binaries: dict[str, bytes] = {}
            for change in changes:
                if isinstance(change, Write):
                    sent.append(
                        {
                            "path": change.path,
                            "replaces": change.replaces,
                            **self._file_json(change.path, change.data, store),
                        }
                    )
                    if not is_text(change.path, change.data):
                        binaries[change.path] = change.data
                elif isinstance(change, Delete):
                    sent.append({"path": change.path, "replaces": change.replaces, "delete": True})
                elif isinstance(change, BundleWrite):
                    archive = pack(change.files)
                    sent.append(
                        {
                            "bundle": self._bundle_json(change.files, archive),
                            "replaces": change.replaces,
                            **self._ref_json(archive, store),
                        }
                    )
                    binaries[f"{BUNDLE_DIR}.zip"] = archive
            body: dict[str, Any] = {"changes": sent}
            if drafts:
                body["drafts"] = [
                    _optional(
                        test=d.test,
                        test_id=d.test_id,
                        fingerprint=d.fingerprint,
                        content=d.content,
                        based_on=d.based_on,
                    )
                    for d in drafts
                ]
            parts = self.blobs.parts(project_id, binaries, body, ctx)
            data = self.session.call(
                "pushChanges",
                "POST",
                "projects",
                project_id,
                "changes",
                body=body,
                parts=parts or None,
            ).data
            self._sent(project_id, binaries)
        return Pushed(
            str(data["token"]), tuple(self._outcome(r) for r in data.get("results") or [])
        )

    def rename_test(
        self,
        project_id: str,
        old: str,
        new: str,
        folder: Mapping[str, str],
        ctx: OpContext | None = None,
    ) -> Outcome:
        """One ``P9`` folder move: the server moves the test's files (its ``test.json``, so its
        id) and its draft together, or nothing."""
        move = {"path": f"tests/{old}", "replaces_folder": dict(folder), "move_to": f"tests/{new}"}
        with self.session.operation(ctx):
            data = self.session.call(
                "pushChanges", "POST", "projects", project_id, "changes", body={"changes": [move]}
            ).data
        return self._outcome((data.get("results") or [{}])[0])

    # -- bytes -----------------------------------------------------------------------------

    def fetch(
        self, project_id: str, ref: FileRef, dest: Path, ctx: OpContext | None = None
    ) -> Path:
        if ref.text is not None or ref.version in self._held:
            data = ref.text.encode("utf-8") if ref.text is not None else self._held[ref.version]
            Path(dest).parent.mkdir(parents=True, exist_ok=True)
            atomic_write.write_bytes(Path(dest), data)
            return Path(dest)
        with self.session.operation(ctx):
            return self.blobs.download(
                project_id, ref.version, ref.size, Path(dest), ctx, ref.store_id
            )
