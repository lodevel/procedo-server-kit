"""The evidence JSON API as an evidence store: Procedo's client of ``docs/plans/data_format.md``.

:class:`ApiStore` is one :class:`evidence_store.EvidenceStore` over a server that speaks the
JSON API (``procedo_api.openapi.yaml``). Every action that is not LOCAL goes over HTTP through
the calls :data:`evidence_actions.ACTIONS` names for it, and through no other:

* **Writes go through this PC's outbox**, as on every store (:class:`LocalStaging`): a run or
  an artifact is staged and committed locally, then the flush sends each entry by its call
  (:meth:`send_entry`, :mod:`evidence_api_push`), so a bench works offline and a replay is
  recognised. A comment, an override or a board's lot made with the server in reach is saved
  at once (E7, E15), as on a folder.
* **Reads** are E4 (the boards), E5 (one board: its tests' pointers, their runs, its lot and
  fields), E3 (one run, its files as parts) and E8/E9 (artifacts and their generations). A
  run read or opened is placed in the per-machine cache (:mod:`evidence_api_cache`), and the
  next read states what it holds (``have``), so its bytes travel once.
* **A manager's actions** (delete, restore, purge: E11–E13) go with a manager's login when
  one is given (``manager``, asked for at the moment), else with the store's own.
* **Set as current** names the pointer this bench last saw (:class:`evidence_api_push.Seen`),
  and a draft the version its person read (``replaces``): one moved or saved since is
  answered ``changed``, nothing written.

Discovery, ``getInfo``, once per store before its first call (outside the action map,
:data:`evidence_actions.DISCOVERY`): a server of another major version is refused rather than
read as empty (``web_api_spec.md`` §3.1), and its retention period dates the trash
(``data_format.md`` §6.7). A push reads
``listStorages`` once, for the storage new files go to (§3.5). Only an ``api`` storage is
written: another backend waits for the BlobStore interface (``company_storage_design.md`` §6),
and its entries wait in the outbox.

Registered as the **web server** (``backend: http``): Settings ▸ Server's "Web server" is this
store for the evidence, :class:`project_store_api.ApiProjectSource` for the projects
(:mod:`project_sync_api`) and :class:`library_api.ApiLibrary` for the library, all of one
address and one token. The server decides where it keeps things: there is no folder to name.
Qt-free.
"""

from __future__ import annotations

import calendar
import hashlib
import json
import time
import unicodedata
from collections.abc import Callable, Collection
from contextlib import AbstractContextManager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import evidence_artifact_generations, evidence_outbox, server_certificate
from .evidence_api_cache import (
    forget_artifact,
    held,
    place_artifact,
    place_run,
    sha256_of,
)
from .evidence_api_http import (
    HAVE_IN_QUERY,
    Answer,
    ApiEndpoint,
    ApiError,
    ApiWire,
    active_store,
    boards_registered,
    offered_calls,
    require_json_api,
    version,
)
from .evidence_api_push import (
    UNKNOWN,
    Seen,
    board_query,
    push_artifact,
    push_run,
    save_annotation,
    save_board_entries,
)
from .evidence_api_run import board_key as _key
from .evidence_api_run import manifest_of, run_info
from .evidence_api_search import artifacts
from .evidence_api_search import runs as search_runs
from .evidence_board import board_of, segments
from .evidence_index import info_of_report
from .evidence_local_staging import LocalStaging
from .evidence_remote_cache import default_cache_dir, flag
from .evidence_shared_layout import check_new_key
from .evidence_shared_manifest import annotation_of
from .evidence_store import (
    CURRENT_RUN,
    READ_WORKERS,
    RESULT_FILE,
    AccessReport,
    ArtifactInfo,
    EvidenceError,
    EvidenceKey,
    OpContext,
    ParamSpec,
    RunInfo,
    check_test_id,
    guard_component,
    register,
)
from .evidence_store_api_search import ApiSearch
from .evidence_store_fs import _format_mtime
from .evidence_trash import RETENTION_DAYS, Deletion, TrashItem, require_reason

_ARTIFACT_KINDS = ("report", "export", "draft")
#: ``ctx.memo`` slots: one board's E5 answer, one run's E3 answer and its cached folder.
_BOARD, _RUN = "evidence_api.board", "evidence_api.run"


def _endpoint(params: dict[str, Any]) -> ApiEndpoint:
    """An endpoint from a config entry's declared params (never the credential, never Accept
    any certificate: this PC's, asked by the wire before each call, :mod:`server_certificate`)."""
    base_url = str(params.get("base_url") or "").strip()
    if not base_url:
        raise EvidenceError("invalid", "the web server needs an address")
    try:
        timeout = float(str(params.get("timeout") or 30).strip())
    except (TypeError, ValueError):
        raise EvidenceError("invalid", f"not a timeout: {params.get('timeout')!r}") from None
    return ApiEndpoint(
        base_url=base_url,
        tls=flag(params, "tls", True),
        insecure=flag(params, "insecure", False),
        ca_bundle=str(params.get("ca_bundle") or ""),
        timeout_s=timeout,
        proxy=str(params.get("proxy") or ""),
    )


def _serial(key: str) -> str:
    """The board's serial as its path segment: any character (``board_identity.md``, doubt 3),
    each segment percent-encoded on its own so a ``/`` stays inside it (``web_api_spec.md``,
    path segments). Only an empty serial is refused."""
    serial = board_of(key).serial
    if not serial:
        raise EvidenceError("invalid", f"no serial in board key {key!r}", bad_key=True)
    return serial


def _fold(name: Any) -> str:
    """A name as the contract compares it: NFC, then case folding (§2)."""
    return unicodedata.normalize("NFC", str(name or "")).casefold()


def _of(run: dict[str, Any], key: str, test_id: str) -> bool:
    """Whether *run* (a Run an answer gave) is the board's whose key is *key* and the test's
    whose id is *test_id*."""
    return _fold(_key(run)) == _fold(key) and run.get("test_id") == test_id


def _entry(e: dict[str, Any]) -> dict[str, Any]:
    """An entry as the app keeps it: ``current`` is computed on every read (§4.9), never kept."""
    return {k: v for k, v in e.items() if k != "current"}


def _epoch(stamp: Any) -> float:
    try:
        return float(calendar.timegm(time.strptime(str(stamp), "%Y-%m-%dT%H:%M:%SZ")))
    except ValueError:
        return 0.0


def _shown(stamp: Any) -> str:
    """A time of an answer as a listing row shows one (the folder stores' spelling)."""
    return _format_mtime(_epoch(stamp)) if stamp else ""


@register
class ApiStore(ApiSearch, LocalStaging):
    """``backend: http``: the evidence of the web server, which speaks the JSON API."""

    ID = "http"
    PARAMS = [
        ParamSpec("base_url", "Server address", "str", True, ""),
        ParamSpec("tls", "TLS", "choice:on|off", True, "on"),
        ParamSpec(
            "insecure",
            "Plain HTTP (sends the token and all evidence in clear)",
            "choice:off|on",
            True,
            "off",
        ),
        ParamSpec("ca_bundle", "CA bundle (optional)", "str", False, ""),
        server_certificate.PARAM,  # Accept any certificate: this PC's, never a project's
        ParamSpec("timeout", "Timeout (s)", "str", False, "30"),
        ParamSpec("proxy", "Proxy", "str", False, ""),
    ]
    #: The name a picker offers this backend under.
    TYPE_LABEL = "Web server"
    #: The declared params that form :meth:`endpoint_identity`: what the credentials editor
    #: asks for to compute the key a token is filed under.
    ENDPOINT_PARAMS = ("base_url",)
    SECRET_LABEL = "Token"
    #: What *Check access* proves here: the login, and nothing written (it reads ``/info``).
    ACCESS_PROBED = frozenset({"list"})
    CAPABILITIES = frozenset({"read", "write", "sweep"})
    read_parallelism = READ_WORKERS
    #: The server files each entry under its project's id: there is no folder to mark.
    requires_marker = False

    def __init__(
        self,
        endpoint: ApiEndpoint,
        *,
        machine_id: str | Callable[[], str],
        project_root: Path | None = None,
        project_id: str = "",
        outbox: Path | None = None,
        cache_dir: Path | None = None,
        credentials: Callable[[str], tuple[str, str]] | None = None,
        manager: Callable[[], str] | None = None,
    ) -> None:
        """*credentials* looks up this PC's token by the endpoint's identity; *manager*
        gives a manager's token at the moment of a delete, restore or purge (``None``: the
        store's own login, which then needs the manager role)."""
        self.endpoint = endpoint
        self.io = ApiWire(endpoint, credentials)
        self.cache_dir = Path(cache_dir) if cache_dir else default_cache_dir()
        self.seen = Seen()
        self._manager = manager
        self._ready_checked = False
        #: How long this server keeps a deletion (``limits.retention_days``).
        self.retention_days = RETENTION_DAYS
        #: The optional calls this server offers (``/info``'s ``calls``, contract §6.1).
        self.calls: frozenset[str] = frozenset()
        #: Whether it registers its boards itself (``/info``'s ``boards_registered``, §3.3).
        self.boards_registered = False
        self._store_id: int | None = None
        #: How many files of the last evidence move onto this store reached it (what a move
        #: that failed midway tells the user stays there unused).
        self.files_landed = 0
        self._init_staging(
            machine_id=machine_id, project_root=project_root, project_id=project_id, outbox=outbox
        )

    @classmethod
    def from_config(
        cls,
        spec: dict[str, Any],
        *,
        project_root: Path,
        machine_id: Any = None,
        credentials: Callable[[str], tuple[str, str]] | None = None,
    ) -> ApiStore:
        if machine_id is None:
            raise EvidenceError("invalid", "the web server needs a machine_id")
        endpoint = _endpoint(spec.get("params") or {})
        return cls(
            endpoint, machine_id=machine_id, project_root=project_root, credentials=credentials
        )

    @classmethod
    def endpoint_identity(cls, spec: dict[str, Any]) -> str:
        try:
            return _endpoint(spec.get("params") or {}).identity
        except EvidenceError:
            return ""

    #: The identity names a server and nothing below it, so it is the sign-in's key too.
    credential_key = endpoint_identity

    @classmethod
    def credential_params(cls, key: str) -> dict[str, str]:
        """The inverse of :meth:`credential_key`: what the credentials editor fills its fields
        with when the user picks a saved sign-in (``{}`` for a key that is not one of this
        backend's). The key says nothing about TLS: the address comes back as ``https://``."""
        try:
            parts = urlsplit(key)
        except ValueError:
            return {}
        if parts.scheme != cls.ID or not parts.netloc:
            return {}
        prefix = parts.path.strip("/")
        return {"base_url": f"https://{parts.netloc}/" + (f"{prefix}/" if prefix else "")}

    @classmethod
    def sync_params(cls, spec: dict[str, Any]) -> dict[str, Any]:
        """This entry's server as the parameters a project's origin record reconnects with
        (:func:`project_transport.source_for`); never a credential, never Accept any
        certificate (this PC's, read when it connects)."""
        endpoint = _endpoint(spec.get("params") or {})
        return {
            "base_url": endpoint.base_url,
            "tls": endpoint.tls,
            "insecure": endpoint.insecure,
            "ca_bundle": endpoint.ca_bundle,
            "timeout_s": endpoint.timeout_s,
            "proxy": endpoint.proxy,
        }

    def latency_class(self) -> str:
        return "remote"

    def close(self) -> None:
        self.io.close()

    def evidence_form(self) -> Any:
        """This store's evidence as the moves' common form: read through its own reads into a
        copy on this PC; landed through the import's calls (:mod:`evidence_form_api`)."""
        from .evidence_form_api import ApiForm

        return ApiForm(self)

    # -- the calls ---------------------------------------------------------------------

    def _op(self, ctx: OpContext | None) -> AbstractContextManager[Any]:
        """The operation scope of one store call (joined when the caller opened one)."""
        return self.io.operation(ctx.cancel if ctx is not None else None)

    def _call(self, op: str, method: str, *segments: str, **kwargs: Any) -> Answer:
        self._ready()
        return self.io.call(op, method, segments, **kwargs)

    def _manager_call(self, op: str, *segments: str, body: dict[str, Any]) -> Answer:
        """A manager's call (E11–E13): with the manager's login asked for now, never kept."""
        token = self._manager() if self._manager is not None else None
        return self._call(op, "POST", *segments, body=body, token=token)

    def _ready(self) -> None:
        """``/info`` once: a server that does not serve the API's major version this app
        speaks is refused rather than read as empty. Its retention period
        (``limits.retention_days``) dates what its trash lists; one that is not a positive
        number of days is the contract's 90 (§6.7)."""
        if self._ready_checked:
            return
        info = require_json_api(self.io.info())
        limits = info.get("limits") if isinstance(info.get("limits"), dict) else {}
        days = limits.get("retention_days")
        positive = isinstance(days, int) and not isinstance(days, bool) and days > 0
        self.retention_days = days if positive else RETENTION_DAYS
        self.calls = offered_calls(info)
        self.boards_registered = boards_registered(info)
        self._ready_checked = True

    def _storage(self) -> int:
        """The storage new files go to (§3.1, ``listStorages``), read once. Only the server
        itself (``api``) is written yet: another backend is the BlobStore interface's, not
        built, so the push stops and the entries wait."""
        if self._store_id is None:
            storages = self._call("listStorages", "GET", "storages").data.get("storages") or []
            self._store_id = active_store(storages)
        return self._store_id

    # -- the outbox: each entry by its call --------------------------------------------

    def send_entry(self, e: dict, payload: Path, ctx: OpContext | None) -> None:
        """One outbox entry, sent by its action's call (:mod:`evidence_api_push`)."""
        with self._op(ctx):
            self._ready()
            if evidence_outbox._is_artifact(e):
                push_artifact(self.io, self.seen, e, payload, self._storage())
            elif evidence_outbox._is_annotation(e):
                save_annotation(self.io, e, evidence_outbox._annotation_entries(e))
            elif evidence_outbox._is_board(e):
                save_board_entries(self.io, e["project_id"], e["serial"], e["entries"])
            else:
                push_run(self.io, self.seen, e, payload, self._storage())

    # -- boards and runs ---------------------------------------------------------------

    def list_boards(self, project_id: str, ctx: OpContext | None = None) -> list[str]:
        """The project's boards (E4), by key, sorted as a folder lists them."""
        with self._op(ctx):
            boards = self._call("listBoards", "GET", "projects", project_id, "boards").data
        return sorted(_key(b) for b in boards.get("boards") or [] if isinstance(b, dict))

    def _board(self, project_id: str, key: str, ctx: OpContext | None) -> dict[str, Any]:
        """The board's results (E5), read once per operation; each test's pointer is what a
        later Set as current names it replaces."""
        slot = (_BOARD, self.io.identity, project_id, key)
        found = ctx.memo.get(slot) if ctx is not None else None
        if found is not None:
            return found
        path = ("projects", project_id, "boards", _serial(key))
        found = self._call("getBoard", "GET", *path, query=board_query(key)).data
        for t in found.get("tests") or []:
            self.seen.set_pointer(project_id, key, str(t.get("test_id", "")), t.get("pointer"))
        if ctx is not None:
            ctx.memo[slot] = found
        return found

    def _test(self, project_id: str, key: str, test_id: str, ctx: OpContext | None) -> dict:
        """The board's answer for the test whose hidden id is *test_id*: its pointer and runs
        (``{}``: none). E5 groups them by test id."""
        check_test_id(test_id)
        for t in self._board(project_id, key, ctx).get("tests") or []:
            if t.get("test_id") == test_id:
                return t
        return {}

    def list_runs(
        self, project_id: str, board: str, test_id: str, ctx: OpContext | None = None
    ) -> list[RunInfo]:
        """The test's runs not deleted, newest first (E5's order, a folder's)."""
        with self._op(ctx):
            found = self._test(project_id, board, test_id, ctx)
        return [run_info(r) for r in found.get("runs") or []]

    def _current_id(
        self, project_id: str, key: str, test_id: str, ctx: OpContext | None
    ) -> str | None:
        pointer = self._test(project_id, key, test_id, ctx).get("pointer")
        return pointer.get("run_id") if isinstance(pointer, dict) else None

    def read_pointer(
        self, project_id: str, board: str, test_id: str, ctx: OpContext | None = None
    ) -> dict | None:
        """The test's current-run mark on the board as the server holds it (E5): what a push of
        a run waiting here meets (``outbox_reads``); ``None``: it has none."""
        with self._op(ctx):
            pointer = self._test(project_id, board, test_id, ctx).get("pointer")
        return pointer if isinstance(pointer, dict) else None

    def current_run(
        self, project_id: str, board: str, test_id: str, ctx: OpContext | None = None
    ) -> RunInfo | None:
        with self._op(ctx):
            found = self._test(project_id, board, test_id, ctx)
        pointer = found.get("pointer") if isinstance(found.get("pointer"), dict) else {}
        for r in found.get("runs") or []:
            if pointer.get("run_id") and r.get("run_id") == pointer.get("run_id"):
                return run_info(r)
        return None

    def _run(
        self, project_id: str, key: str, test_id: str, run_id: str | None, ctx: OpContext | None
    ) -> tuple[dict[str, Any], Path]:
        """The run the key names (the current one for ``current``, E5's pointer), read with
        E3 and placed in the cache: its Run and its folder. One deleted, or of another board
        or test id, is not this key's run (``not_found``)."""
        run, local = self._fetched(
            project_id, self._run_id(project_id, key, test_id, run_id, ctx), ctx
        )
        if run.get("deleted_at") or not _of(run, key, test_id):
            raise EvidenceError("not_found", f"no run {run.get('run_id')} for {key}/{test_id}")
        return run, local

    def _run_id(
        self, project_id: str, key: str, test_id: str, run_id: str | None, ctx: OpContext | None
    ) -> str:
        """The run id the key names: the one given, or the current one's (E5's pointer) for
        ``current``; ``not_found`` when the test has none."""
        check_test_id(test_id)
        if run_id in (None, CURRENT_RUN):
            run_id = self._current_id(project_id, key, test_id, ctx)
            if run_id is None:
                raise EvidenceError("not_found", f"no current run for {key}/{test_id}")
        return guard_component(run_id)

    def _owned(
        self, project_id: str, key: str, test_id: str, run_id: str | None, ctx: OpContext | None
    ) -> str:
        """The id of the run the key names, read (E3) to prove it is that board's and that test
        id's — deleted or not: a write naming it (a delete, a comment) is then the server's to
        answer; another's is ``not_found``, never sent."""
        run_id = self._run_id(project_id, key, test_id, run_id, ctx)
        if not _of(self._fetched(project_id, run_id, ctx)[0], key, test_id):
            raise EvidenceError("not_found", f"no run {run_id} for {key}/{test_id}")
        return run_id

    def _fetched(
        self, project_id: str, run_id: str, ctx: OpContext | None
    ) -> tuple[dict[str, Any], Path]:
        """The run read with E3, once per operation, and placed in the cache unless deleted: its
        Run and its folder."""
        slot = (_RUN, self.io.identity, project_id, run_id)
        found = ctx.memo.get(slot) if ctx is not None else None
        if found is None:
            local = self._cache_root(project_id) / "runs" / run_id
            have = [version(s) for s in held(local)]
            path = ("projects", project_id, "runs", run_id)
            if len(",".join(have)) > HAVE_IN_QUERY:
                answer = self._call("readRun", "POST", *path, "read", body={"have": have})
            else:
                answer = self._call(
                    "getRun", "GET", *path, query={"have": ",".join(have)} if have else None
                )
            run = answer.data
            if not run.get("deleted_at"):
                place_run(local, run, answer.parts)
                self.seen.hold(project_id, [str(f.get("sha256")) for f in run.get("files") or []])
            found = (run, local)
            if ctx is not None:
                ctx.memo[slot] = found
        return found

    def read_run(
        self, project_id: str, board: str, test_id: str, run_id: str, ctx: OpContext | None = None
    ) -> dict:
        with self._op(ctx):
            run, local = self._run(project_id, board, test_id, run_id, ctx)
        if not run.get("complete"):
            raise EvidenceError("not_found", f"no result for {board}/{test_id}/{run_id}")
        try:
            return json.loads((local / RESULT_FILE).read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise EvidenceError("not_found", f"no result for {board}/{test_id}/{run_id}") from None
        except (OSError, ValueError) as exc:
            raise EvidenceError("corrupt", f"unreadable result of {run_id}: {exc}") from exc

    def read_run_annotation(
        self, project_id: str, board: str, test_id: str, run_id: str, ctx: OpContext | None = None
    ) -> dict | None:
        """The run's entries in the order received (without ``current``, which a reader
        computes) and its execution block; ``None`` when the run is not there."""
        try:
            with self._op(ctx):
                run, _local = self._run(project_id, board, test_id, run_id, ctx)
        except EvidenceError as exc:
            if exc.kind == "not_found" and run_id not in (None, CURRENT_RUN):
                return None
            raise
        annotation = run.get("annotation") if isinstance(run.get("annotation"), dict) else {}
        entries = [_entry(e) for e in annotation.get("entries") or [] if isinstance(e, dict)]
        return annotation_of(manifest_of(run), entries)

    def add_run_annotation_entries(
        self,
        project_id: str,
        board: str,
        test_id: str,
        run_id: str,
        entries: list[dict],
        ctx: OpContext | None = None,
    ) -> list[dict]:
        """E7 on the run the key names (:meth:`_owned`); per entry,
        :func:`annotation_entries.outcome`'s shape."""
        check_test_id(test_id)
        with self._op(ctx):
            run_id = self._owned(project_id, board, test_id, run_id, ctx)
            path = ("projects", project_id, "runs", run_id, "annotation")
            results = self._call("saveAnnotation", "POST", *path, body={"entries": entries})
        return _outcomes(entries, results.data.get("results") or [])

    def read_board_entries(
        self, project_id: str, board: str, ctx: OpContext | None = None
    ) -> list[dict]:
        """The board's lot and fields as entries, in the order received; ``[]`` for a board
        the server does not know."""
        with self._op(ctx):
            found = self._board(project_id, board, ctx).get("board") or {}
        return [_entry(e) for e in found.get("entries") or [] if isinstance(e, dict)]

    def add_board_entries(
        self, project_id: str, board: str, entries: list[dict], ctx: OpContext | None = None
    ) -> list[dict]:
        """E15: a board the server does not know is created by its first entries."""
        check_new_key(board)
        with self._op(ctx):
            self._ready()
            results = save_board_entries(self.io, project_id, board, entries)
        return _outcomes(entries, results)

    # -- the current run (E6) --------------------------------------------------------------

    def promote_run(
        self, project_id: str, board: str, test_id: str, run_id: str, ctx: OpContext | None = None
    ) -> None:
        with self._op(ctx):
            self._set_current(project_id, board, test_id, guard_component(run_id))

    def demote_run(
        self, project_id: str, board: str, test_id: str, ctx: OpContext | None = None
    ) -> str | None:
        """ "Mark as not run": the pointer names no run, its ``ended`` kept; the run it
        named before, if any."""
        with self._op(ctx):
            replaced = self._set_current(project_id, board, test_id, None)
        return replaced.get("run_id") if replaced else None

    def _set_current(
        self, project_id: str, key: str, test_id: str, run_id: str | None
    ) -> dict | None:
        """E6, naming the pointer this bench last saw. ``changed``: someone moved it since,
        nothing was written, and that is refused (``invalid``) with what it names now — unless
        this bench had seen no pointer at all, when the request is made again on the one
        answered. Returns the pointer it replaced. The mark is the test's on that board, by its
        hidden id (``tests/{test_id}/current``)."""
        path = ("projects", project_id, "boards", _serial(key), "tests")
        path += (check_test_id(test_id), "current")
        seen = self.seen.pointer(project_id, key, test_id)
        replaces = None if seen is UNKNOWN else seen
        for attempt in range(2):
            body = {"run_id": run_id, "replaces": replaces, "machine_id": self.machine_id}
            answer = self._call("setCurrent", "POST", *path, query=board_query(key), body=body)
            now = (
                answer.data.get("pointer") if isinstance(answer.data.get("pointer"), dict) else None
            )
            self.seen.set_pointer(project_id, key, test_id, now)
            if answer.data.get("status") != "changed":
                return replaces
            if seen is not UNKNOWN or attempt:
                break
            replaces = self.seen.pointer(project_id, key, test_id)
        named = (now or {}).get("run_id") or "none"
        raise EvidenceError(
            "invalid",
            f"The current run of {test_id} changed meanwhile (it is {named} now): look again, "
            "then choose.",
        )

    # -- artifacts ---------------------------------------------------------------------------

    def _row(self, kind: str, artifact: dict[str, Any]) -> ArtifactInfo:
        """A listing row: a report's from its record (the folder stores' rule,
        :func:`evidence_index.info_of_report`), any other's its name and time."""
        name, when = str(artifact.get("name", "")), _shown(artifact.get("updated_at"))
        if kind == "report":
            info = artifact.get("info") if isinstance(artifact.get("info"), dict) else {}
            row = info_of_report(name, info)
            return row if row.generated else replace(row, generated=when)
        return ArtifactInfo(name, when, "", "", "", "")

    def list_artifacts(
        self, kind: str, project_id: str, board: str, ctx: OpContext | None = None
    ) -> list[ArtifactInfo]:
        """By name, by case folding, as on every storage (E8 answers newest first)."""
        query = {"kind": _kind(kind), **_board_of_artifact(kind, board), "include": "info"}
        with self._op(ctx):
            rows = artifacts(self, project_id, query)
        rows.sort(key=lambda a: (_fold(a.get("name")), str(a.get("name") or "")))
        return [self._row(kind, a) for a in rows]

    def _get_artifact(
        self,
        kind: str,
        project_id: str,
        key: str,
        name: str,
        *,
        have: str | None = None,
        generation: int | None = None,
    ) -> tuple[dict[str, Any], bytes | None]:
        """E9: the artifact and its file's bytes (``None``: a draft, or the bytes ``have``
        names). A deleted one is ``not_found``, and so is one whose file the server cannot
        give."""
        query: dict[str, str | int] = dict(_board_of_artifact(kind, key))
        if generation is not None:
            query["generation"] = generation
        if have:
            query["have"] = version(have)
        path = ("projects", project_id, "artifacts", _kind(kind), guard_component(name))
        answer = self._call("getArtifact", "GET", *path, query=query)
        artifact = answer.data
        if artifact.get("deleted_at"):
            raise EvidenceError("not_found", f"the {kind} {name} of {key} was deleted")
        if kind == "draft":
            return artifact, None
        sha256 = str(artifact.get("sha256") or "")
        data = answer.parts.get(version(sha256))
        if data is None and have == sha256:
            return artifact, None
        if data is None:
            why = (artifact.get("missing") or {}).get(artifact.get("name"), "not given")
            raise EvidenceError("not_found", f"the file of the {kind} {name} is {why}")
        if hashlib.sha256(data).hexdigest() != sha256:
            raise EvidenceError("corrupt", f"the {kind} {name} came with other bytes than its own")
        return artifact, data

    def _artifact_file(self, project_id: str, key: str, kind: str, name: str) -> Path:
        folder = self._cache_root(project_id).joinpath(*segments(key or "_project")) / kind
        return folder / guard_component(name) if name else folder

    def _materialize_artifact(
        self, kind: str, project_id: str, key: str, name: str, local: Path
    ) -> bool:
        """The artifact read into ``local`` (E9, stating the bytes the cache holds); ``False``
        when the server holds none, and nothing of it is left there."""
        try:
            artifact, data = self._get_artifact(
                kind, project_id, key, name, have=None if kind == "draft" else sha256_of(local)
            )
        except EvidenceError as exc:
            if exc.kind != "not_found":
                raise
            forget_artifact(local, kind)
            return False
        record = artifact.get("content") if kind == "draft" else artifact.get("info")
        place_artifact(local, kind, data, record)
        return True

    def read_artifact(
        self, kind: str, project_id: str, board: str, name: str, ctx: OpContext | None = None
    ) -> bytes:
        """A write still waiting in the outbox is readable by its writer, as on every store."""
        staged = evidence_outbox.pending_artifact(self, kind, project_id, board, name)
        if staged is not None:
            try:
                return staged.read_bytes()
            except FileNotFoundError:
                pass  # pushed while we looked
        local = self._artifact_file(project_id, board, _kind(kind), name)
        with self._op(ctx):
            if not self._materialize_artifact(kind, project_id, board, name, local):
                raise EvidenceError("not_found", f"no {kind} {name!r} for {board}")
        return local.read_bytes()

    def list_generations(
        self, kind: str, project_id: str, board: str, name: str, ctx: OpContext | None = None
    ) -> list[tuple[int, ArtifactInfo]]:
        """Every generation of a report or an export (E8 ``generations=all``, newest first),
        oldest first."""
        if kind not in evidence_artifact_generations.KEPT:
            raise EvidenceError("invalid", f"a {kind} keeps no generations")
        query = {"kind": kind, **_board_of_artifact(kind, board), "generations": "all"}
        with self._op(ctx):
            rows = artifacts(self, project_id, {**query, "include": "info"})
        found = [
            (int(a.get("generation") or 0), self._row(kind, a))
            for a in rows
            if _fold(a.get("name")) == _fold(name)
        ]
        return sorted(found, key=lambda pair: pair[0])

    def read_generation(
        self,
        kind: str,
        project_id: str,
        board: str,
        name: str,
        generation: int,
        ctx: OpContext | None = None,
    ) -> bytes:
        if kind not in evidence_artifact_generations.KEPT:
            raise EvidenceError("invalid", f"a {kind} keeps no generations")
        with self._op(ctx):
            _artifact, data = self._get_artifact(
                kind, project_id, board, name, generation=generation
            )
        return data or b""

    def delete_artifact(
        self,
        kind: str,
        project_id: str,
        board: str,
        name: str,
        ctx: OpContext | None = None,
        *,
        deletion: Deletion,
    ) -> None:
        """E12 with a manager's login: every generation of the name goes; one the server
        does not hold is a no-op, as on a folder."""
        body = {**_delete_body(self.machine_id, deletion), **_artifact_ref(kind, board, name)}
        try:
            with self._op(ctx):
                self._manager_call(
                    "deleteArtifact", "projects", project_id, "artifacts", "delete", body=body
                )
        except ApiError as exc:
            if exc.kind != "not_found":
                raise

    # -- the trash (E2, E8 deleted=only; E11–E13) -------------------------------------------

    def delete_run(
        self,
        project_id: str,
        board: str,
        test_id: str,
        run_id: str,
        ctx: OpContext | None = None,
        *,
        deletion: Deletion,
    ) -> None:
        """E11 with a manager's login on the run the key names (``current``: the current one;
        :meth:`_owned`): kept with the reason; the pointer handed on."""
        check_test_id(test_id)
        body = _delete_body(self.machine_id, deletion)
        with self._op(ctx):
            run_id = self._owned(project_id, board, test_id, run_id, ctx)
            path = ("projects", project_id, "runs", run_id, "delete")
            answer = self._manager_call("deleteRun", *path, body=body).data
        if "pointer" in answer:
            self.seen.set_pointer(project_id, board, test_id, answer.get("pointer"))

    def list_trash(self, project_id: str, ctx: OpContext | None = None) -> list[TrashItem]:
        """The deleted runs and artifacts, newest deletion first."""
        with self._op(ctx):
            runs = search_runs(self, {"project_id": project_id, "deleted": "only"})
            gone = artifacts(self, project_id, {"deleted": "only"})
        days = self.retention_days
        items = [_trashed(r, "run", days) for r in runs]
        items += [_trashed(a, str(a.get("kind", "")), days) for a in gone]
        return sorted(items, key=lambda i: i.deleted_at, reverse=True)

    # -- search (E2, E8) and what its fields propose: :class:`ApiSearch` --------------------

    def restore_trash(self, project_id: str, entry: str, ctx: OpContext | None = None) -> bool:
        """E11/E12 restore with a manager's login; whether a run is Current again."""
        what, _, rest = entry.partition("/")
        body: dict[str, Any] = {"machine_id": self.machine_id}
        with self._op(ctx):
            if what == "run":
                path = ("projects", project_id, "runs", guard_component(rest), "restore")
                answer = self._manager_call("restoreRun", *path, body=body).data
                pointer = answer.get("pointer") if isinstance(answer.get("pointer"), dict) else None
                run = answer.get("run") if isinstance(answer.get("run"), dict) else {}
                if pointer is not None:
                    test_id = str(run.get("test_id", ""))
                    self.seen.set_pointer(project_id, _key(run), test_id, pointer)
                return pointer is not None and pointer.get("run_id") == rest
            body.update(_trash_artifact(entry))
            self._manager_call(
                "restoreArtifact", "projects", project_id, "artifacts", "restore", body=body
            )
        return False

    def purge_trash(
        self,
        project_id: str,
        entries: Collection[str] = (),
        ctx: OpContext | None = None,
        *,
        now: float | None = None,
    ) -> list[str]:
        """E13 with a manager's login: *entries* now, and what the server's retention period
        has passed; the server's own clock decides what expired, so a purge cannot name
        another moment. The entries that went, as its answer lists them (``purged``, what
        expired included); one no longer held is skipped (§6.7)."""
        if now is not None:
            raise EvidenceError(
                "invalid", "The server purges by its own clock: no other moment can be named."
            )
        items = [
            {"run_id": guard_component(e.partition("/")[2])}
            if e.startswith("run/")
            else _trash_artifact(e)
            for e in entries
        ]
        body: dict[str, Any] = {"machine_id": self.machine_id}
        if items:
            body["items"] = items
        with self._op(ctx):
            went = self._manager_call("purge", "projects", project_id, "purge", body=body).data
        purged = went.get("purged") if isinstance(went.get("purged"), dict) else {}
        runs = [f"run/{r}" for r in purged.get("runs") or [] if isinstance(r, str)]
        artifacts = purged.get("artifacts") or []
        return [*runs, *(_artifact_entry(a) for a in artifacts if isinstance(a, dict))]

    # -- this PC's copy ------------------------------------------------------------------------

    def _cache_root(self, project_id: str) -> Path:
        """Keyed by the server's identity, hashed as a server store's is."""
        endpoint = hashlib.sha256(self.io.identity.encode("utf-8")).hexdigest()[:16]
        return self.cache_dir / endpoint / guard_component(project_id[:8])

    def local_path(self, key: EvidenceKey, ctx: OpContext | None = None) -> Path:
        """A real local path for a consumer that must ``open()`` it: a run as a whole folder
        (its files beside its record), an artifact as its file beside its record, and a
        report's own draft beside it."""
        with self._op(ctx):
            if key.kind == "run":
                _run, local = self._run(
                    key.project_id, key.board, key.test_id or "", key.run_id, ctx
                )
                return local / guard_component(key.name) if key.name else local
            local = self._artifact_file(key.project_id, key.board, _kind(key.kind), key.name)
            if not key.name:
                local.mkdir(parents=True, exist_ok=True)
                return local
            self._materialize_artifact(key.kind, key.project_id, key.board, key.name, local)
            if key.kind == "report":
                draft = f"{Path(key.name).stem}.draft.json"
                self._materialize_artifact(
                    "draft", key.project_id, key.board, draft, local.with_name(draft)
                )
        return local

    # -- reach -------------------------------------------------------------------------------

    def check_access(self, ctx: OpContext | None = None) -> AccessReport:
        """``/info`` with this PC's login, nothing written: whether the server answers, which
        API it speaks, and what the login's roles let it do."""
        start = time.perf_counter()
        try:
            with self._op(ctx):
                info = self.io.call("getInfo", "GET", ("info",)).data
                self._ready_checked = False
                self._ready()
        except EvidenceError as exc:
            reachable = exc.kind not in ("unreachable", "timeout")
            return AccessReport(
                reachable, False, False, False, False, None, None, [str(exc)], self.ACCESS_PROBED
            )
        elapsed = (time.perf_counter() - start) * 1000
        user = info.get("user") if isinstance(info.get("user"), dict) else {}
        roles = {str(r) for r in user.get("roles") or []}
        skew = None
        try:
            at = datetime.fromisoformat(str(info.get("server_time")).replace("Z", "+00:00"))
            skew = at.timestamp() - datetime.now(timezone.utc).timestamp()
        except ValueError:
            pass
        notes = [f"Signed in as {user.get('name', '?')} ({', '.join(sorted(roles)) or 'no role'})."]
        return AccessReport(
            True,
            True,
            "bench" in roles,
            "bench" in roles,
            "manager" in roles,
            elapsed,
            skew,
            notes,
            self.ACCESS_PROBED,
        )

    sign_in = check_access


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _kind(kind: str) -> str:
    if kind not in _ARTIFACT_KINDS:
        raise EvidenceError("invalid", f"unknown artifact kind: {kind!r}")
    return kind


def _board_of_artifact(kind: str, key: str) -> dict[str, str]:
    """An artifact's board in a query (E8, E9): a project-level export's is all empty."""
    board = board_of("" if _kind(kind) == "export" else key)
    return {
        "serial": board.serial,
        "part_number": board.part_number,
        "board_revision": board.revision,
    }


def _artifact_ref(kind: str, key: str, name: str) -> dict[str, str]:
    """An artifact in a manager's body (E12, E13): its board's absent parts left out."""
    board = _board_of_artifact(kind, key)
    found = {"kind": kind, "serial": board["serial"], "name": guard_component(name)}
    found.update({k: v for k, v in board.items() if k != "serial" and v})
    return found


def _delete_body(machine_id: str, deletion: Deletion) -> dict[str, Any]:
    """A delete's body (E11, E12, §6.7): its reason, the bench, and what the folder storages'
    trash keeps of who deleted it: the bench's ``station`` and the name typed
    (``deleted_by_name``), each when known. The server proves the login itself."""
    require_reason(deletion)
    body = {"machine_id": machine_id, "reason": deletion.reason}
    if deletion.station:
        body["station"] = deletion.station
    if deletion.deleted_by:
        body["deleted_by_name"] = deletion.deleted_by
    return body


def _artifact_entry(item: dict[str, Any]) -> str:
    """The trash entry naming an artifact, ``<kind>/<board key>/<name>``, from what an answer
    says of it: an Artifact (E8), or an artifact E13 purged."""
    kind = str(item.get("kind", ""))
    return f"{kind}/{_key(item) if kind != 'export' else ''}/{item.get('name', '')}"


def _trashed(item: dict[str, Any], what: str, retention_days: int) -> TrashItem:
    """A deleted run or artifact as the trash lists it. Its entry names it: ``run/<run_id>``,
    or :func:`_artifact_entry`. Who deleted it is the name typed at the bench
    (``deleted_by_name``), as every storage's trash keeps it; the login the server proved
    (``deleted_by``) is the server's own record."""
    run = what == "run"
    name = str(item.get("run_id" if run else "name", ""))
    return TrashItem(
        entry=f"run/{name}" if run else _artifact_entry(item),
        what=what,
        board=_key(item) if what != "export" else "",
        test=str(item.get("test", "")) if run else "",
        test_id=str(item.get("test_id", "")) if run else "",
        name=name,
        reason=str(item.get("deleted_reason", "")),
        deleted_by=str(item.get("deleted_by_name", "")),
        machine_id=str(item.get("deleted_bench", "")),
        station=str(item.get("deleted_station", "")),
        deleted_at=_epoch(item.get("deleted_at")),
        was_current=bool(item.get("deleted_was_current")),
        files=(),  # nothing of it is a file in a folder here: the server keeps it
        place="",
        retention_days=retention_days,
    )


def _trash_artifact(entry: str) -> dict[str, str]:
    """The artifact a trash entry names, as a manager's body names it."""
    parts = entry.split("/")
    if len(parts) < 3 or parts[0] not in _ARTIFACT_KINDS:
        raise EvidenceError("not_found", f"no such deletion: {entry!r}")
    return _artifact_ref(parts[0], "/".join(parts[1:-1]), parts[-1])


def _outcomes(sent: list[dict], results: list[Any]) -> list[dict]:
    """E7's or E15's answer as :func:`annotation_entries.outcome` shapes it: the entry, its
    status, and its slot's current entry (the one sent, when the answer names none)."""
    out = []
    for e, r in zip(sent, results, strict=False):
        r = r if isinstance(r, dict) else {}
        current = r.get("current")
        out.append(
            {
                "entry": dict(e),
                "status": r.get("status"),
                "current": _entry(current) if isinstance(current, dict) else dict(e),
            }
        )
    return out
