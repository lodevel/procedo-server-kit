"""One JSON API server as the project and library clients reach it: the wire, discovery once, the
limits it publishes, the login's roles, and the storage new files go to.

The evidence client (:class:`evidence_store_api.ApiStore`) holds the same three on its own: it
also needs the board identity and the retention period of ``/info``. Both read ``/info`` through
:func:`evidence_api_http.require_json_api` and the storages through
:func:`evidence_api_http.active_store`, so a server is accepted, and its storage chosen, by one
rule. Qt-free; every call may wait on the server: on a worker, never on the GUI thread.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any

from .evidence_api_http import (
    Answer,
    ApiEndpoint,
    ApiWire,
    active_store,
    boards_registered,
    offered_calls,
    require_json_api,
)
from .evidence_store import OpContext


class ApiSession:
    """The calls of one server, each after its ``/info`` was read once (``getInfo``: discovery,
    outside every action map)."""

    def __init__(
        self, endpoint: ApiEndpoint, credentials: Callable[[str], tuple[str, str]] | None = None
    ) -> None:
        self.wire = ApiWire(endpoint, credentials)
        self.identity = endpoint.identity
        self._lock = threading.Lock()
        self._limits: dict[str, Any] | None = None
        self._roles: frozenset[str] = frozenset()
        self._calls: frozenset[str] = frozenset()
        self._boards_registered = False
        self._store_id: int | None = None

    def operation(self, ctx: OpContext | None) -> AbstractContextManager[Any]:
        """The scope of one action: its cancel flag, its kept-open connection."""
        return self.wire.operation(ctx.cancel if ctx is not None else None)

    def close(self) -> None:
        self.wire.close()

    def ready(self) -> None:
        """Discovery, once: a server this app must not talk to is refused before any call."""
        with self._lock:
            if self._limits is None:
                info = require_json_api(self.wire.info())
                limits = info.get("limits")
                user = info.get("user") if isinstance(info.get("user"), dict) else {}
                self._roles = frozenset(str(r) for r in user.get("roles") or [])
                self._calls = offered_calls(info)
                self._boards_registered = boards_registered(info)
                self._limits = limits if isinstance(limits, dict) else {}

    def call(self, op: str, method: str, *segments: str | int, **kwargs: Any) -> Answer:
        """One call of the contract by its operationId (:meth:`ApiWire.call`)."""
        self.ready()
        return self.wire.call(op, method, tuple(str(s) for s in segments), **kwargs)

    def roles(self) -> frozenset[str]:
        """This login's roles, as ``/info`` names them (``user.roles``, ``data_format.md`` §5.3)."""
        self.ready()
        return self._roles

    def offered(self) -> frozenset[str]:
        """The optional calls this server offers, by their ids (``/info``'s ``calls``, contract
        §6.1): every core call besides. A publish reads it: a project is created only on a
        server listing P11, its releases land only on one listing ``import``
        (``project_sync_api.publish_refused``)."""
        self.ready()
        return self._calls

    def boards_registered(self) -> bool:
        """Whether the server registers its boards itself (``/info``'s ``boards_registered``,
        contract §3.3): a bench then picks a board from E4's list only. Kept for that choice;
        nothing reads it yet."""
        self.ready()
        return self._boards_registered

    def limit(self, name: str) -> int | None:
        """A limit ``/info`` publishes (``max_upload_bytes``, ``max_file_bytes``); ``None`` when
        it publishes none that is a positive number."""
        self.ready()
        value = (self._limits or {}).get(name)
        positive = isinstance(value, int) and not isinstance(value, bool) and value > 0
        return value if positive else None

    def store_id(self) -> int:
        """The storage new files go to (S1, ``listStorages``), read once."""
        if self._store_id is None:
            storages = self.call("listStorages", "GET", "storages").data.get("storages") or []
            self._store_id = active_store(storages)
        return self._store_id
