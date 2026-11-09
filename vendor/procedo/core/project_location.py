"""Where a project itself lives: a folder, or a server — one or the other.

The owner's rule: *"it mixes path folder and ftp, horrible — it's one or the other,
with an enum"*. So a project's location is a TYPE, and the types are DERIVED, never
listed by a dialog: a backend is a server type a project can live on when it is both
an offered storage type (``evidence_config.store_types``, so its fields are declared
and the same widget renders them) and a transport a sync engine can rebuild
(``project_transport.TREE_FACTORIES``, or ``SOURCE_FACTORIES`` for a server that keeps its
projects itself). A transport added later is a type Settings ▸
Server offers at once, and one that cannot be synced is offered by none. The pickers
offer a folder plus this PC's servers by name (``server_settings.locations``).

``filesystem`` is deliberately NOT a server location: a folder — local, UNC or
mapped — is opened IN PLACE (the design's decision 1), never through a working
copy. It is the registry's test transport, and offering it here would give the user
two different ways to open the same folder, one of which copies it.

Qt-free.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import evidence_config, project_transport
from .evidence_config import StoreType

#: The kind of a location that is a plain folder — the only one that is not a backend.
FOLDER = ""


@dataclass(frozen=True)
class LocationType:
    """One project location the user picks: a plain folder, or one backend.

    ``server`` names the server of Settings ▸ Server this location IS (its label is
    that name): a picker offers each server by name (``server_settings.locations``),
    and only Settings itself offers bare server TYPES (:func:`server_types`)."""

    kind: str  # "" (a folder) or the backend id
    label: str
    server: str = ""

    @property
    def is_server(self) -> bool:
        return self.kind != FOLDER


FOLDER_TYPE = LocationType(FOLDER, "Folder (this PC or a network share)")


def server_types() -> list[LocationType]:
    """Every server TYPE a project can live on, derived from the two registries —
    what Settings ▸ Server offers a server to be; every other picker offers the
    servers themselves, by name. The evidence database keeps projects too (slice 11) but is
    set on its own page, Settings ▸ Database (``evidence_config.sign_in_page``), never as one
    of Settings ▸ Server's."""
    return [
        LocationType(store_type.backend, store_type.label)
        for store_type in evidence_config.store_types()
        if (
            store_type.backend in project_transport.TREE_FACTORIES
            or project_transport.reads_by_changes(store_type.backend)
        )
        and store_type.backend != project_transport.FILESYSTEM
        and evidence_config.sign_in_page(store_type.backend) == "Server"
    ]


def store_type_of(location: LocationType) -> StoreType | None:
    """The storage type whose declared fields describe this location's endpoint."""
    if not location.is_server:
        return None
    return StoreType(location.kind, location.label, location.server)
