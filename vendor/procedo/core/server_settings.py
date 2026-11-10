"""The servers this PC uses — Settings ▸ Server.

The owner, 2026-11-05: *"I don't mind having several servers configured and
accessed, like multiple libraries"* (``company_storage_design.md`` §4). A PC keeps a LIST
of servers, each
with a name of its own on this PC (compared case-folded), machine-scope
(:mod:`app_settings`, never a project's ``config.json``). Every picker offers a
folder plus these servers BY NAME (:func:`locations`, :func:`store_choices`) — never
a server type with empty fields to fill in. A project keeps recording its own
server's endpoint, as before: the list is for browsing and creating, never for
redirecting what already exists.

**The type list is DERIVED**, never listed here: :func:`available_types` is
:func:`project_location.server_types`, the same registry-driven list every other
picker draws from (a backend that is both an offered storage type and a transport
the sync engine can rebuild). A transport added later appears on its own.

**A server's entry is a storage-entry dict** — ``{"backend": ..., "params": {...}}`` —
the exact shape :class:`evidence_store_form.EvidenceStoreForm` reads and writes, so
the same widget renders it and the same :func:`evidence_config.validate_spec` refuses
an incomplete one. **No credential in it**: no backend declares a secret param; the
user is not a secret (it travels so another bench knows whose login to ask for), and
the secret stays in :mod:`evidence_credentials`, keyed per SERVER
(:func:`evidence_store.credential_key`: its address, never its folder), so each
server keeps its own sign-in and one sign-in serves every folder on it.

**The database** of Settings ▸ Database (one per PC, :mod:`evidence_db_settings`) is offered
beside the servers, by its type's name (:func:`database`): a picker of a project's store shows
its address read-only as it shows a server's, and :func:`chosen` / :func:`server_for` answer it
as they answer a server. It is never one of Settings ▸ Server's list.

**The folder key.** A backend that is a server of folders (FTPS) names the folder it is
rooted at with the declared key ``base_path`` — the project's own folder, or the evidence
folder, depending on who is asking. The web server declares none: it
decides where it keeps things (:func:`has_folder`). Every OTHER declared field of the endpoint
is the address: the part a picker shows read-only.

Qt-free.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from . import app_settings, evidence_config, project_location, server_certificate
from .evidence_config import StoreType
from .evidence_store import (
    REGISTRY,
    EvidenceError,
    credential_key,
    endpoint_params,
    is_server_backend,
)
from .project_location import FOLDER_TYPE, LocationType

if TYPE_CHECKING:
    from .project_origin import Origin

__all__ = [
    "FOLDER_PARAM",
    "SECTION",
    "Server",
    "address_label",
    "address_params",
    "address_text",
    "available_types",
    "certificate_line",
    "chosen",
    "database",
    "has_folder",
    "is_certificate_line",
    "locations",
    "matches_origin",
    "name_of",
    "named",
    "names_problem",
    "not_offered_note",
    "place_label",
    "same_server_problem",
    "server_for",
    "servers",
    "set_servers",
    "sign_in_label",
    "store_choices",
    "unconfigured_note",
]

#: The app_settings key the list lives under.
SECTION = "servers"
#: The declared param that names the FOLDER on the server, never the address.
FOLDER_PARAM = "base_path"


@dataclass(frozen=True)
class Server:
    """One server of this PC: the name every picker offers it by, and its entry."""

    name: str
    spec: dict[str, Any] = field(default_factory=dict)

    @property
    def backend(self) -> str:
        return str(self.spec.get("backend", ""))


def has_folder(backend: str) -> bool:
    """Whether *backend* has a folder on its server for a user to name: it declares
    :data:`FOLDER_PARAM`. FTPS does; the web server does not (it keeps its projects and its
    evidence itself), so no screen asks for a folder there."""
    return any(p.key == FOLDER_PARAM for p in evidence_config.backend_params(backend))


# -- the list -------------------------------------------------------------------------


def servers() -> list[Server]:
    """Every server this PC uses, in the order Settings ▸ Server lists them."""
    data = app_settings.load_app_settings()
    raw = data.get(SECTION)
    found = []
    for entry in raw if isinstance(raw, list) else []:
        if isinstance(entry, dict) and entry.get("name") and entry.get("backend"):
            spec = {key: copy.deepcopy(value) for key, value in entry.items() if key != "name"}
            found.append(Server(str(entry["name"]), spec))
    return found


def set_servers(found: list[Server]) -> None:
    """Replace the list. Merge-safe like every other machine setting, and RAISING: a
    list with a nameless server or two of one name is refused, and a caller that
    reports "saved" having stored nothing is the one outcome this refuses."""
    problem = names_problem([server.name for server in found]) or same_server_problem(found)[1]
    if problem:
        raise EvidenceError("invalid", problem)
    stored = [{"name": server.name.strip(), **copy.deepcopy(server.spec)} for server in found]
    app_settings.update_app_settings(lambda data: data.update({SECTION: stored}))
    server_certificate.remember(found)  # the connections of this process follow at once


def same_server_problem(found: list[Server]) -> tuple[int, str]:
    """``(row, why)`` for the first server that is another one again: a type with no folder
    (the web server) on an address an earlier one has. One server, one entry: its sign-in, and
    this PC's choices for it (Accept any certificate), are one. ``(-1, "")``: none."""
    seen: dict[tuple[str, str], str] = {}
    for row, server in enumerate(found):
        key = credential_key(server.spec) if not has_folder(server.backend) else ""
        if not key:
            continue
        first = seen.setdefault((server.backend, key), server.name.strip())
        if first != server.name.strip():
            return row, (
                f"{first} and {server.name.strip()} are the same server "
                f"({address_label(server.spec)}). Keep one of them."
            )
    return -1, ""


def names_problem(names: list[str]) -> str:
    """Why these names cannot be kept (``""``: they can): every server has one, and no
    two are the same, compared case-folded — a picker offers them by name alone."""
    seen: set[str] = set()
    for name in names:
        key = name.strip().casefold()
        if not key:
            return "Every server needs a name."
        if key in seen:
            return f"Two servers are named {name.strip()!r}. Give each a name of its own."
        seen.add(key)
    return ""


def named(name: str) -> Server | None:
    """This PC's server called *name* (case-folded), or ``None``."""
    key = name.strip().casefold()
    return next((s for s in servers() if s.name.strip().casefold() == key), None)


def database() -> Server | None:
    """The database Settings ▸ Database names on this PC, as a server a picker offers (named
    by its type, its entry the one a project keeps: :meth:`DbSettings.entry`); ``None`` when
    none is set, or when what is saved is not a whole address."""
    from . import evidence_db_settings

    settings = evidence_db_settings.load()
    if not settings.identity:
        return None
    return Server(evidence_db_settings.TYPE_LABEL, settings.entry())


def offered(name: str) -> Server | None:
    """The server a choice list names *name*: this PC's server of that name, else this PC's
    database when it is the one so named (the moves offer it beside the servers)."""
    found = named(name)
    if found is not None:
        return found
    database_ = database()
    return database_ if database_ is not None and database_.name == name else None


def chosen(choice: StoreType) -> Server | None:
    """The server a picker's *choice* stands for: this PC's database for the database's
    choice, else this PC's server of that name (``None``: none, or no longer)."""
    found = database()
    if found is not None and choice.backend == found.backend:
        return found if choice.server == found.name else None
    return named(choice.server) if choice.server else None


def server_for(spec: dict[str, Any]) -> Server | None:
    """This PC's server that *spec* is on: the same backend and the same sign-in key
    (its address, never its folder). Of several on one address, the one whose folder
    holds the entry's own folder deepest. ``None`` when the entry names none of them.
    An entry on this PC's database is that database (:func:`database`)."""
    key = credential_key(spec)
    if not key:
        return None
    found = database()
    if found is not None and spec.get("backend") == found.backend:
        return found if credential_key(found.spec) == key else None
    same = [
        s for s in servers() if s.backend == spec.get("backend") and credential_key(s.spec) == key
    ]
    folder = _parts(spec)
    holding = [s for s in same if folder[: len(_parts(s.spec))] == _parts(s.spec)]
    if holding:
        return max(holding, key=lambda s: len(_parts(s.spec)))
    return same[0] if same else None


def _parts(spec: dict[str, Any]) -> list[str]:
    if not has_folder(spec.get("backend", "")):
        return []
    folder = str(evidence_config.param_value(spec, FOLDER_PARAM) or "")
    return [part for part in folder.replace("\\", "/").split("/") if part]


# -- what the pickers offer ------------------------------------------------------------


def available_types() -> list[LocationType]:
    """Every server TYPE Settings can make a server of — derived, never listed: the
    same registries :func:`project_location.server_types` already draws from."""
    return project_location.server_types()


def locations() -> list[LocationType]:
    """Where a project can live, as every picker offers it: a folder first, then this
    PC's servers by name (each one whose type a project can live on)."""
    kinds = {t.kind for t in available_types()}
    named_servers = [s for s in servers() if s.backend in kinds]
    return [FOLDER_TYPE, *(LocationType(s.backend, s.name, s.name) for s in named_servers)]


def store_choices() -> list[StoreType]:
    """Where evidence can be kept, as every picker offers it: each storage type that is
    not a server (a folder), then this PC's servers by name, then this PC's database when
    Settings ▸ Database names one (:func:`database`)."""
    offered = evidence_config.store_types()
    kinds = {t.backend for t in offered}
    folders = [t for t in offered if not is_server_backend(t.backend)]
    found = database()
    named_servers = [s for s in servers() if s.backend in kinds]
    if found is not None and found.backend in kinds:
        named_servers.append(found)
    return [*folders, *(StoreType(s.backend, s.name, s.name) for s in named_servers)]


# -- how a server reads ----------------------------------------------------------------


def address_params(backend: str) -> tuple[str, ...]:
    """The declared params that identify *backend*'s ADDRESS: its endpoint params
    less the folder a server of folders also carries (:data:`FOLDER_PARAM`)."""
    return tuple(k for k in endpoint_params(backend) if k != FOLDER_PARAM)


def address_text(spec: dict[str, Any]) -> str:
    """The address alone — the declared params that make it up joined with ``:`` (a
    host and a port; a bare base URL is the one part on its own), or as the backend spells
    its address when it declares how (``ADDRESS``, its params by name, an unset one its
    declared default: a database's ``host:port/database``)."""
    backend = spec.get("backend", "")
    shape = getattr(REGISTRY.get(backend), "ADDRESS", "")
    if shape:
        defaults = {p.key: p.default for p in evidence_config.backend_params(backend)}
        values = {
            key: evidence_config.param_value(spec, key) or defaults.get(key) or ""
            for key in address_params(backend)
        }
        return shape.format(**values)
    return ":".join(
        str(evidence_config.param_value(spec, key))
        for key in address_params(spec.get("backend", ""))
        if evidence_config.param_value(spec, key) not in (None, "")
    )


def address_label(spec: dict[str, Any]) -> str:
    """One short, read-only line naming *spec*'s server — never the folder, never the
    user: its type, its address, and a flag when its own ``insecure`` choice is on
    (``plain FTP``, ``plain HTTP`` — the backend's own words for it). Never TLS or
    passive mode, at their (secure) default far more often than not."""
    backend = spec.get("backend", "")
    if not backend:
        return ""
    address = address_text(spec)
    if evidence_config.param_value(spec, "insecure") == "on":
        flag = f"({_plain_words(backend)})"
        address = f"{address} {flag}" if address else flag
    label = evidence_config.type_label(backend)
    return f"{label} — {address}" if address else label


def _plain_words(backend: str) -> str:
    """``plain FTP`` / ``plain HTTP``: the backend's declared label for its ``insecure``
    choice, up to the warning in brackets."""
    declared = next(
        (p.label for p in evidence_config.backend_params(backend) if p.key == "insecure"),
        "Plain connection",
    )
    words = declared.split(" (")[0]
    return words[:1].lower() + words[1:]


def place_label(spec: dict[str, Any]) -> str:
    """*spec*'s server and the folder it names, the short way: :func:`address_label`
    plus the folder (a type that has one) — never the raw identity a backend computes for
    routing."""
    address = address_label(spec)
    if not has_folder(spec.get("backend", "")):
        return address
    folder = evidence_config.param_value(spec, FOLDER_PARAM)
    return f"{address}, folder {folder}" if folder else address


def sign_in_label(backend: str, key: str) -> str:
    """A saved sign-in's key the way a person reads it: the name of this PC's server
    it serves, else its address (an https server as https, never the ``http://`` its
    key is spelled with), else the key itself."""
    name = name_of(backend, key)
    if name:
        return name
    params = getattr(REGISTRY.get(backend), "credential_params", lambda _key: {})(key)
    return address_label({"backend": backend, "params": params}) if params else key


def unconfigured_note() -> str:
    """What a place that needs a server says when this PC has none."""
    return "No server is set on this PC — add one in Settings ▸ Server."


#: The app that keeps this PC's machine settings (the servers, their sign-ins, the database).
SETTINGS_APP = "Procedo Bench"


def in_settings_app(text: str) -> str:
    """*text* as an app without machine settings of its own (Procedo Studio) says it: every
    ``Settings ▸ Server`` or ``Settings ▸ Database`` names the app that keeps them, and a
    rejected certificate's "what to do" says where its settings are
    (:func:`server_certificate.in_settings_app`)."""
    text = server_certificate.in_settings_app(text)
    return re.sub(r"Settings ▸ (Server|Database)", rf"Settings ▸ \1 (in {SETTINGS_APP})", text)


def certificate_line(name: str, err: object) -> str:
    """``<name> refused: its certificate is not trusted (self-signed). See File › Settings… ›
    Server.``: what Bench's footer says when *err* is a rejected certificate of the server
    called *name* (``""`` for any other failure)."""
    refusal = server_certificate.certificate_refusal(err)
    if refusal is None:
        return ""
    return f"{name or 'The server'} refused: {refusal.short}{_CERTIFICATE_LINE_END}"


_CERTIFICATE_LINE_END = ". See File › Settings… › Server."


def is_certificate_line(text: str) -> bool:
    """Whether *text* is a :func:`certificate_line` (what a later success takes down)."""
    return " refused: its certificate " in text and text.endswith(_CERTIFICATE_LINE_END)


def name_of(backend: str, key: str) -> str:
    """The name of this PC's server of *backend* whose sign-in key is *key*, ``""`` for none."""
    for server in servers():
        if server.backend == backend and credential_key(server.spec) == key:
            return server.name
    return ""


def sign_in_kept_note(page: str = "Server") -> str:
    """What an app without machine settings says where a sign-in is saved (*page*: the
    Settings page that keeps it, ``Server`` or ``Database``)."""
    whose = "The database's" if page == "Database" else "A server's"
    return (
        f"{whose} sign-in is saved in {SETTINGS_APP}: File › Settings… › {page}. A sign-in "
        "never travels with the project.\nSave it there, then click Check access again here."
    )


def not_offered_note(spec: dict[str, Any]) -> str:
    """Why an entry naming a server is not one a picker offers: this PC has no server
    at all, or none on that address; a database that is not the one set on this PC."""
    if evidence_config.sign_in_page(spec.get("backend", "")) == "Database":
        return (
            f"{address_label(spec)} is not the database set on this PC — set it in "
            "Settings ▸ Database."
        )
    if not servers():
        return unconfigured_note()
    return f"{address_label(spec)} is not one of this PC's servers — add it in Settings ▸ Server."


def matches_origin(origin: Origin | None) -> bool:
    """Whether *origin* — a working copy's recorded endpoint — is on one of this PC's
    servers: the same backend and the same sign-in key, asked of the SAME transport
    that rebuilt the record, never a re-derivation of it.

    ``True`` when there is nothing recorded to compare (a plain folder, no origin at
    all): nothing to flag as "different". A project's own recorded server always
    wins regardless of the answer — this is only what the status item's wording
    reads off (owner's rule: never silently switched)."""
    if origin is None or not getattr(origin, "backend", ""):
        return True
    from .evidence_store_db import DatabaseStore

    if origin.backend == DatabaseStore.ID:
        return _matches_database(origin)
    if origin.backend not in {t.kind for t in available_types()}:
        return True  # not a server backend at all: nothing of Settings' to compare
    from . import project_sync_gateway

    try:
        key = project_sync_gateway.gateway().credential_key(origin.backend, origin.params)
    except EvidenceError:
        return False
    return any(s.backend == origin.backend and credential_key(s.spec) == key for s in servers())


def _matches_database(origin: Origin) -> bool:
    """A working copy of a project in the evidence database is on this PC's database when its
    record names the database Settings ▸ Database names: the same identity
    (``postgresql://host:port/dbname``), the key its sign-in is filed under. Settings ▸ Server
    never lists the database, so its servers are not asked."""
    from . import evidence_db_settings
    from .evidence_store_db import DatabaseStore

    here = evidence_db_settings.load().identity
    return bool(here) and DatabaseStore.endpoint_identity({"params": origin.params}) == here
