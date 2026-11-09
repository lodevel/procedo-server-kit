"""The evidence database as the registry and the forms know it (``backend: postgres``): its
name, its parameters, how its address reads, where it signs in, what it can do. Everything a
list of backends, a form or a sign-in asks of it, and nothing that reaches a database.

The store itself, :class:`evidence_store_db.DatabaseStore`, inherits this, and is imported
only when one is opened (:meth:`DatabaseBackend.from_config`) or an entry's address is
composed: a program that never opens the database (the API client alone, as the server
conformance kit runs it) loads none of its code.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from .evidence_search import RUN_FILTERS
from .evidence_search_choices import DATABASE_CHOICES
from .evidence_store import ParamSpec, register


def _store() -> type:
    """The database store, imported when one is needed."""
    from .evidence_store_db import DatabaseStore

    return DatabaseStore


@register
class DatabaseBackend:
    """The evidence database's entry in the backend registry; its store is
    :class:`evidence_store_db.DatabaseStore`, which overrides each method below."""

    ID = "postgres"
    PARAMS = [
        ParamSpec("host", "Host", "str", True, None),
        ParamSpec("port", "Port", "str", False, "5432"),
        ParamSpec("dbname", "Database", "str", True, "procedo"),
        ParamSpec("ca_bundle", "CA bundle (optional)", "str", False, ""),
        ParamSpec(
            "insecure",
            "Plain TCP (sends the password and all evidence in clear)",
            "choice:off|on",
            True,
            "off",
        ),
    ]
    TYPE_LABEL = "Database (PostgreSQL)"
    ENDPOINT_PARAMS = ("host", "port", "dbname")
    #: Where this PC's login to the database is added (``project_sync_gateway``'s cue).
    SIGN_IN_HINT = "Add the database's login in Settings ▸ Database"
    #: How a person reads its address (``server_settings.address_text``): Waiting uploads'
    #: *Waiting for* and *Destination*, the sign-in's name.
    ADDRESS = "{host}:{port}/{dbname}"
    CAPABILITIES = frozenset({"read", "write", "query", "sweep"})
    #: Its sign-in is on Settings ▸ Database, never among Settings ▸ Server's (owner, 2026-11-06).
    SIGN_IN_PAGE = "Database"
    #: A run's pictures and files always go to the database's attachment storage, apart from
    #: its record: a project on it keeps "Separate" (owner, 2026-11-06).
    ATTACHMENTS_APART = True
    #: A source and a destination of the evidence move (slice 11: :mod:`evidence_db.form`). The
    #: one switch that offers the database in *Move or copy the evidence* and *Move or Copy
    #: Project* (:mod:`evidence_move_targets`).
    MOVES_EVIDENCE = True
    #: A run's operator, its bench and its measurements are rows of their own: a search names
    #: them (:mod:`evidence_db.search`).
    SEARCH_RUN_FILTERS = RUN_FILTERS
    #: Every field the search proposes, a test's readings among them
    #: (:mod:`evidence_db.search_choices`).
    SEARCH_CHOICES = DATABASE_CHOICES

    @classmethod
    def from_config(
        cls,
        spec: dict[str, Any],
        *,
        project_root: Path | None,
        machine_id: Any = None,
        credentials: Callable[[str], tuple[str, str]] | None = None,
    ) -> Any:
        """The store this entry opens (:meth:`evidence_store_db.DatabaseStore.from_config`)."""
        return _store().from_config(
            spec, project_root=project_root, machine_id=machine_id, credentials=credentials
        )

    @classmethod
    def endpoint_identity(cls, spec: dict[str, Any]) -> str:
        return _store().endpoint_identity(spec)

    @classmethod
    def credential_key(cls, spec: dict[str, Any]) -> str:
        return _store().credential_key(spec)

    @classmethod
    def sync_params(cls, spec: dict[str, Any]) -> dict[str, Any]:
        return _store().sync_params(spec)
