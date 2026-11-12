"""``config.json:evidence_store`` — the entry's lifecycle and its declared shape.

The entry is *structural*: every project carries one. ``ensure_spec`` gap-fills
:func:`default_spec` on open — the shared layout in ``<project>\\evidence``, written
as an ABSOLUTE path (owner 2026-10-23: copies of a project in different places share
one folder) — and creates that folder (:func:`create_root`). There is one layout, so
an entry names none (a ``layout`` key a config still carries is ignored); files a
project holds in the old in-project results trees are left where they are.

A config template may carry an entry so a project seeded from it inherits the team
convention — but only a project still free to choose (``accepts_template_entry``):
its default folder, holding no evidence yet. ``project_id`` never travels with it.
``validate_spec`` checks an entry against the backend's declared ``PARAMS`` (the
Evidence tab renders from the same declarations); ``read_spec`` / ``write_spec`` are
the one place the entry is read and written. ``config.json`` is written through
``config_manager``'s canonical writer, imported lazily (config_manager imports this
module). Qt-free.
"""

from __future__ import annotations

import copy
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import (  # noqa: F401  (backends register on import)
    evidence_db_backend,
    evidence_http,
    evidence_legacy,
    evidence_remote_cache,
    evidence_store_api,
    evidence_store_fs_shared,
    evidence_store_ftps,
    evidence_store_sftp,
)
from .evidence_board import PARTS
from .evidence_owner import MARKER
from .evidence_shared_layout import is_reserved
from .evidence_store import (
    CAPABILITIES_PROBED,
    REGISTRY,
    EvidenceError,
    ParamSpec,
    open_project_store,
    within_folder,
)
from .evidence_trash import TRASH
from .paths import inside_place, same_place, universal
from .project_never_travels import EVIDENCE_DIRNAME

log = logging.getLogger(__name__)

SECTION = "evidence_store"
#: A TEMPLATE's database entry may name where a project's runs' files go
#: (:func:`evidence_db_place.template_place`); a project's own entry never carries it: the
#: database keeps a project's place.
FILES_KEY = "files"


def default_spec(project_root: Path) -> dict[str, Any]:
    """The entry a project gets when it has none: its own ``evidence`` folder, as an ABSOLUTE path — a copy of the project
    elsewhere names the same folder, so every copy files its runs in one place (owner
    2026-10-23) — through a mapped drive in its share's spelling (:func:`saved_form`)."""
    root = Path(universal(os.path.abspath(project_root))) / EVIDENCE_DIRNAME
    return {"backend": "filesystem", "params": {"root": str(root)}}


def _config_file(project_root: Path) -> Path:
    return Path(project_root) / "config" / "config.json"


def load_config(project_root: Path) -> dict[str, Any]:
    """``config.json`` as a dict — ``{}`` when absent. ``EvidenceError("unreachable")``
    when it is there and cannot be read as a JSON object, a share that is down included:
    such a file is never rewritten from ``{}``, and an entry that cannot be read is never
    taken for :func:`default_spec` — a run would be filed in the project folder instead
    of the store the project names. ``unreachable``, because it is retried rather than
    refused: a push that meets it waits in the outbox, never in quarantine. Read by
    :func:`config_manager.load_unified_config`, the one reader."""
    from . import config_manager

    try:
        return config_manager.load_unified_config(_config_file(project_root).parent)
    except (OSError, ValueError) as exc:
        raise EvidenceError("unreachable", f"the project settings cannot be read ({exc})") from exc


def read_config(project_root: Path) -> dict[str, Any] | None:
    """:func:`load_config`, ``None`` when it cannot be read."""
    try:
        return load_config(project_root)
    except EvidenceError:
        return None


def read_spec(project_root: Path) -> dict[str, Any]:
    """The project's entry (a copy), or :func:`default_spec` when it has none yet.
    ``EvidenceError("unreachable")`` when ``config.json`` cannot be read
    (:func:`load_config`)."""
    from .evidence_rename import recover

    recover(Path(project_root))
    spec = load_config(project_root).get(SECTION)
    if not isinstance(spec, dict):
        return default_spec(project_root)
    return project_entry(spec)


def write_spec(project_root: Path, spec: dict[str, Any]) -> None:
    """Replace the entry (:func:`project_entry`: never this PC's own settings). ``EvidenceError``
    when ``config.json`` exists but cannot be read: the rest of that file would be lost."""
    from . import config_manager

    data = load_config(project_root)
    data[SECTION] = project_entry(spec)
    config_manager.write_unified_config(_config_file(project_root).parent, data)


def project_entry(spec: dict[str, Any]) -> dict[str, Any]:
    """*spec* as a project keeps it (a copy): without the params that are this PC's setting
    for its server (``ParamSpec.this_pc``: the web server's Accept any certificate), which
    stay in Settings ▸ Server. Read and written so, a project never carries one, and never
    turns one on for a PC that opens it."""
    entry = copy.deepcopy(spec)
    params = entry.get("params")
    if isinstance(params, dict):
        for p in backend_params(str(entry.get("backend", ""))):
            if p.this_pc:
                params.pop(p.key, None)
    return entry


def with_this_pc(entry: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    """*entry* (a project's) with this PC's own choices for its server as *source* (Settings ▸
    Server's entry) has them: what a test of that server as this PC reaches it connects with."""
    out = project_entry(entry)
    for p in backend_params(str(out.get("backend", ""))):
        if p.this_pc and param_value(source, p.key) not in (None, ""):
            out = with_param(out, p.key, param_value(source, p.key))
    return out


def _home(project_root: Path) -> Path:
    """Where the project's evidence belongs: beside the project on its share for this PC's
    working copy of a share project (``project_source.shown_path``) — evidence never moves
    onto one PC with the copy — else the project folder itself."""
    from .project_source import shown_path

    return shown_path(project_root)


def ensure_spec(project_root: Path) -> bool:
    """Fill the missing entry with :func:`default_spec` of the project's home (:func:`_home`:
    the share folder for a working copy of one) and create that folder (:func:`create_root`).
    A real entry is never touched; an unparseable or unwritable ``config.json`` is left as it
    is (the project still opens on the default). True when the entry was written.
    Run under the project's config lock, after its ``project_id`` is minted."""
    from . import config_manager
    from .project_identity import read_project_id

    data = read_config(project_root)
    if data is None:
        log.warning("%s cannot be read; evidence_store not written", _config_file(project_root))
        return False
    entry = data.get(SECTION)
    if isinstance(entry, dict):
        moved = _moved_with_the_project(project_root, entry, data.get("project_id"))
        if moved is None:
            return False
        data[SECTION] = moved
        try:
            config_manager.write_unified_config(_config_file(project_root).parent, data)
        except OSError as exc:
            log.warning("evidence_store not re-pointed for %s (%s)", project_root, exc)
            return False
        log.info(
            "%s was moved: its evidence folder is %s now", project_root, param_value(moved, "root")
        )
        return True
    spec = default_spec(_home(project_root))
    data[SECTION] = spec
    try:
        config_manager.write_unified_config(_config_file(project_root).parent, data)
    except OSError as exc:
        log.warning("evidence_store not written for %s (%s); opening without it", project_root, exc)
        return False
    log.info("evidence_store entry written for %s: %s", project_root, spec["params"]["root"])
    try:
        create_root(project_root, spec, read_project_id(project_root))
    except EvidenceError as exc:  # the uploads wait for it and say so; the project opens
        log.warning("evidence folder not created for %s: %s", project_root, exc)
    return True


def _moved_with_the_project(
    project_root: Path, entry: dict[str, Any], project_id: Any
) -> dict | None:
    """The entry re-pointed at the project's own ``evidence`` folder when the project
    folder was renamed or moved outside Bench (``None``: nothing to do). The entry names
    ``<old folder>\\evidence``, which is gone from a volume that answers, while this
    folder's ``evidence`` carries this project's marker — the folder moved WITH the
    project, as a relocate would have re-pointed it. A copy elsewhere that shares one
    folder (the old one still there) and a share that is down are left alone."""
    from .evidence_store_fs import _read_json, require_volume

    root = param_value(entry, "root") if entry.get("backend") == "filesystem" else None
    here = Path(str(param_value(default_spec(_home(project_root)), "root")))
    if not root or not project_id or Path(str(root)).name.casefold() != EVIDENCE_DIRNAME:
        return None
    try:
        require_volume(Path(str(root)))
        if Path(str(root)).exists() or not here.is_dir():
            return None
    except OSError:
        return None
    if _read_json(here / MARKER).get("project_id") != project_id:
        return None
    return with_param(entry, "root", str(here))


def create_root(project_root: Path, spec: dict[str, Any], project_id: str) -> None:
    """Create the evidence folder ``spec`` names, now that it is CHOSEN — a new
    project, Apply in the project settings — and mark it as this project's
    (``evidence_store_fs_shared.create_folder``). A write never creates it. A server's
    base path is never created by Bench: nothing to do for one."""
    if spec.get("backend") != "filesystem":
        return
    store = open_project_store(spec, project_root=Path(project_root))
    try:
        evidence_store_fs_shared.create_folder(store, project_id)
    finally:
        store.close()


def drop_unused_root(project_root: Path, spec: dict[str, Any], applied: dict[str, Any]) -> None:
    """The project moved off ``spec`` to ``applied`` before anything was filed there: the
    in-project folder Bench made for it goes too, when it holds nothing but its marker.
    Anything else in it is left as it is, and so is a folder ``applied`` still names
    under another spelling (an 8.3 name, another server name of one share): asked of
    the folders themselves (:func:`within_folder`), right before the delete. I/O."""
    if not is_in_project(spec, project_root):
        return
    folder = Path(str(param_value(spec, "root")))
    kept = param_value(applied, "root") if applied.get("backend") == "filesystem" else None
    try:
        if kept and within_folder(Path(str(kept)), folder):
            log.info("%s kept: the evidence entry still names it", folder)
            return
        if {p.name for p in folder.iterdir()} <= {MARKER}:
            (folder / MARKER).unlink(missing_ok=True)
            folder.rmdir()
    except OSError as exc:
        log.info("%s kept: %s", folder, exc)


def is_in_project(spec: dict[str, Any], project_root: Path) -> bool:
    """Whether this entry keeps the project's evidence INSIDE the project folder.

    The one question a caller about to delete or move a project folder, or to open
    one it shares with other benches, has to ask: such evidence lives in the folder,
    the sync excludes it, so every run recorded against such an entry exists only
    where that folder is. A mapped drive and its share spell one folder."""
    root = param_value(spec, "root") if spec.get("backend") == "filesystem" else None
    return bool(root) and inside_place(str(root), project_root)


def _holds_evidence(folder: Path) -> bool:
    """A FILE in ``folder`` that is not the store's own bookkeeping at its top (the
    marker, the index…); what its evidence trash keeps counts, as it can still be
    restored, and so do the boards under ``parts/`` (reserved because it is no board,
    not because it is bookkeeping). A folder that cannot be listed counts as holding
    some."""
    if not folder.is_dir():
        return False
    denied: list[OSError] = []
    for dirpath, dirnames, filenames in os.walk(folder, onerror=denied.append):
        if Path(dirpath) == folder:
            dirnames[:] = [d for d in dirnames if not is_reserved(d) or d in (TRASH, PARTS)]
            filenames = [f for f in filenames if not is_reserved(f)]
        if filenames:
            return True
    return bool(denied)


def keeps_local_evidence(spec: dict[str, Any], project_root: Path) -> bool:
    """Whether results may exist ONLY in the project folder: its entry keeps them there
    (:func:`is_in_project`), or the project's own ``evidence`` folder holds files
    whatever the entry says — an entry spelled by an alias no spelling compare sees (an
    8.3 name, another server name of one share). What a delete of the project folder or
    a move of it onto a server asks. I/O."""
    return is_in_project(spec, project_root) or _holds_evidence(
        Path(project_root) / EVIDENCE_DIRNAME
    )


def in_project_file_count(project_root: Path, spec: dict[str, Any]) -> int:
    """How many files the project's own ``evidence`` folder, an in-project entry's
    folder and the old results folders hold; ``-1`` when one cannot be read. What
    *Forget this working copy* names before it removes the folder: those files are the
    only copy there is."""
    denied: list[OSError] = []
    count = 0
    own = Path(project_root) / EVIDENCE_DIRNAME
    folders = [*evidence_legacy.trees(project_root), own]
    if is_in_project(spec, project_root):
        root = Path(str(param_value(spec, "root")))
        if not within_folder(root, own):  # counted once, however it is spelled
            folders.append(root)
    for folder in folders:
        if folder.is_dir():
            for _dirpath, _dirnames, filenames in os.walk(folder, onerror=denied.append):
                count += len(filenames)
    return -1 if denied else count


def accepts_template_entry(project_root: Path, current: dict[str, Any]) -> bool:
    """Whether the project is still free to choose its store (a template's entry, a
    change in the settings): its entry keeps the evidence in the project folder and
    nothing is filed there, nor in the old results folders. Anything else would hide
    what the project has; it moves instead."""
    if not is_in_project(current, project_root):
        return False
    folder = Path(str(param_value(current, "root")))
    return not _holds_evidence(folder) and not evidence_legacy.has_files(project_root)


def template_entry(spec: dict[str, Any]) -> dict[str, Any]:
    """A template's entry as a project may take it: a team CONVENTION, never an
    identity. ``project_id`` is minted per project and names whose evidence a folder
    holds; a template that carried one would hand every project seeded from it the
    same claim, and the second would be refused from its own evidence folder. Nor its files'
    place (:data:`FILES_KEY`): the database keeps a project's."""
    entry = copy.deepcopy(spec)
    entry.pop("project_id", None)
    entry.pop(FILES_KEY, None)
    return entry


def seed_spec(src_cfg: dict[str, Any], project_data: dict[str, Any], project_root: Path) -> None:
    """A config template's entry for a project being seeded (see
    :func:`accepts_template_entry`). ``project_id`` never rides."""
    spec = src_cfg.get(SECTION)
    if not isinstance(spec, dict):
        return
    current = project_data.get(SECTION)
    if not isinstance(current, dict):
        current = default_spec(project_root)
    if accepts_template_entry(project_root, current):
        project_data[SECTION] = template_entry(spec)
    else:
        log.info("%s keeps its evidence_store: it already holds evidence", Path(project_root).name)


def apply_problem(
    project_root: Path, shadow_root: Path, loaded: dict[str, Any] | None
) -> str | None:
    """The refusal to commit a configuration dialog's staged entry (``None``: commit).
    ``loaded`` is the live entry when the dialog loaded. A live entry changed
    elsewhere meanwhile (a Move, another bench) is kept when the staged entry was
    left alone — it is written into what is committed — and refused when both
    changed; a changed staged entry must pass :func:`validate_spec` and, since it is
    about to become a folder on this machine, :func:`uncreatable_root`."""
    live, staged = read_spec(project_root), read_spec(shadow_root)
    if loaded is not None and live != loaded:
        if staged != loaded:
            return (
                "The evidence store was changed elsewhere while this dialog was open — "
                "close it and reopen."
            )
        try:
            write_spec(shadow_root, live)  # an untouched staged entry never rolls the live one back
        except EvidenceError as exc:
            return str(exc)
        return None
    problems = validate_spec(staged, project_root)
    if staged != live:
        # The one filesystem question an entry raises, asked ONCE here rather than on
        # every keystroke of the Evidence tab (:func:`uncreatable_root`).
        problems += uncreatable_root(staged)
    if problems and staged != live:
        return (
            "The evidence store settings cannot be applied:\n\n"
            + "\n".join(problems)
            + "\n\nFix them on the Evidence tab, or Cancel."
        )
    if staged != live and not accepts_template_entry(project_root, live):
        return (
            "This project now holds evidence, so its evidence store cannot be changed "
            "directly. Close and reopen this dialog, then move the evidence instead."
        )
    return None


def backend_params(backend: str) -> list[ParamSpec]:
    cls = REGISTRY.get(backend)
    return list(getattr(cls, "PARAMS", [])) if cls is not None else []


@dataclass(frozen=True)
class StoreType:
    """One storage type the user picks: a backend, under the name that backend gives it.
    It is one or the other — never a box that takes both a folder and a server.

    ``server`` names the server of Settings ▸ Server this choice IS (its label is that
    name): a picker offers each server by name, never a server type with empty fields."""

    backend: str
    label: str
    server: str = ""


def type_label(backend: str) -> str:
    """The name a backend gives its storage type (``TYPE_LABEL``, beside its
    ``PARAMS``); its bare id when it names none."""
    return getattr(REGISTRY.get(backend), "TYPE_LABEL", backend)


def declares_user(backend: str) -> bool:
    """Whether *backend* declares its own ``user`` (FTPS does; the web server signs in
    with a token alone)."""
    return any(p.key == "user" for p in backend_params(backend))


def access_probes(backend: str) -> frozenset[str]:
    """What *Check access* proves on *backend* (``ACCESS_PROBED``): list, write, rename
    and delete unless the backend says it writes nothing (the web server lists only)."""
    return frozenset(getattr(REGISTRY.get(backend), "ACCESS_PROBED", CAPABILITIES_PROBED))


def secret_word(backend: str) -> str:
    """What *backend*'s secret is, in a sentence: ``password``, or ``token`` for the web
    server (which says so with ``SECRET_LABEL``)."""
    return str(getattr(REGISTRY.get(backend), "SECRET_LABEL", "Password")).lower()


def sign_in_words(backend: str) -> str:
    """What a sign-in to *backend* is made of, as a person types it: ``the user and
    password`` where the backend declares a user, else its secret alone (``the token``)."""
    secret = secret_word(backend)
    return f"the user and {secret}" if declares_user(backend) else f"the {secret}"


def sign_in_page(backend: str) -> str:
    """The page of Settings that keeps *backend*'s sign-in (``SIGN_IN_PAGE``): ``Server``, or
    ``Database`` for the evidence database, whose sign-in lives on its own page."""
    return str(getattr(REGISTRY.get(backend), "SIGN_IN_PAGE", "Server"))


def attachments_apart(backend: str) -> bool:
    """Whether a store of *backend* always keeps a run's pictures and files apart from its
    record (``ATTACHMENTS_APART``: the database), so a project on it keeps "Separate"."""
    return bool(getattr(REGISTRY.get(backend), "ATTACHMENTS_APART", False))


def moves_evidence(backend: str) -> bool:
    """Whether the evidence move takes *backend* as a source or a destination
    (``MOVES_EVIDENCE``; the database does not until its import, slice 11)."""
    return bool(getattr(REGISTRY.get(backend), "MOVES_EVIDENCE", True))


def store_types() -> list[StoreType]:
    """Every storage type on offer — one per non-hidden backend, named by the backend
    itself.

    DERIVED, never listed: a backend that registers is offered by every picker at once,
    and one registered but not configurable yet (``hidden``) by none."""
    return [
        StoreType(backend, type_label(backend))
        for backend in sorted(REGISTRY)
        if not getattr(REGISTRY[backend], "hidden", False)
    ]


def unknown_type(spec: dict[str, Any]) -> StoreType:
    """The type to show for an entry no OFFERED type matches, named for what is
    actually wrong with it: a backend that is not installed, or one not configurable
    yet."""
    backend = spec.get("backend", "")
    if REGISTRY.get(backend) is None:
        return StoreType(backend, f"{backend} (not installed)")
    return StoreType(backend, f"{backend} (not configurable yet)")


def param_value(spec: dict[str, Any], key: str) -> Any:
    """A declared param's value in the entry (``None`` when unset)."""
    params = spec.get("params")
    return params.get(key) if isinstance(params, dict) else None


def with_param(spec: dict[str, Any], key: str, value: Any) -> dict[str, Any]:
    """A copy of the entry with one declared param set (``None``/"" unsets it)."""
    out = copy.deepcopy(spec)
    target = out.setdefault("params", {})
    if value in (None, ""):
        target.pop(key, None)
    else:
        target[key] = value
    return out


def saved_form(spec: dict[str, Any]) -> dict[str, Any]:
    """The entry as Bench saves a folder the user picked or typed: a filesystem root
    through a mapped drive letter in its ``\\\\server\\share`` spelling
    (:func:`paths.universal`), so a colleague without that mapping reaches the same
    folder (owner 2026-10-26), with Windows separators. Anything else as given, a bare
    ``G:`` (no folder: refused as not absolute) included. An entry already saved is
    never rewritten for it: an older drive-letter root keeps working, compared by
    place. No disk access."""
    root = param_value(spec, "root")
    if (
        spec.get("backend") != "filesystem"
        or not isinstance(root, str)
        or root[1:2] != ":"
        or root[2:3] not in ("\\", "/")
    ):
        return spec
    shared = universal(root)
    return spec if shared == root else with_param(spec, "root", shared.replace("/", "\\"))


def same_entry(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Whether two entries name one store: equal but for their filesystem roots, which
    name one place (:func:`paths.same_place`: a drive letter and its share, in any case).
    What decides whether a folder was LEFT — never by spelling, or the folder the entry
    still names is taken for one it left. No disk access."""
    return with_param(a, "root", None) == with_param(b, "root", None) and same_place(
        param_value(a, "root"), param_value(b, "root")
    )


def _declared_problems(spec: dict[str, Any], backend: str) -> list[str]:
    """What every backend's declared ``PARAMS`` promise, whatever the backend is: a
    required value (a declared default satisfies it) and a ``choice`` among its
    options."""
    problems: list[str] = []
    for p in backend_params(backend):
        raw = param_value(spec, p.key)
        value = raw if raw not in (None, "") else p.default
        if value in (None, ""):
            if p.required:
                problems.append(f"{p.label} is required")
        elif p.type.startswith("choice:"):
            options = p.type.split(":", 1)[1].split("|")
            if str(value) not in options:
                problems.append(f"{p.label} must be one of {', '.join(options)}")
    return problems


def _filesystem_problems(spec: dict[str, Any], project_root: Path | None) -> list[str]:
    """A filesystem entry names an absolute folder that is neither the project folder
    itself nor inside its old results folders (a folder INSIDE the project is the
    user's choice, the default one included).

    **Pure string tests, no filesystem access**, because this runs on every keystroke
    in the Evidence tab. Whether the folder can actually be CREATED is
    :func:`uncreatable_root`'s question and is asked once, at Apply. Compared by place
    (:func:`paths.inside_place`): a root saved as a share meets a project opened through
    its drive letter; that asks the network provider which share a letter maps, once
    per letter per session — never the folder, never the share."""
    problems: list[str] = []
    for p in backend_params(spec.get("backend", "")):
        if p.type != "path":
            continue
        value = param_value(spec, p.key) or p.default
        if value in (None, ""):
            continue  # already reported as required
        if not Path(str(value)).is_absolute():
            problems.append(f"{p.label}: the evidence folder must be an absolute path")
        elif project_root is not None and (
            (inside_place(project_root, str(value)) and inside_place(str(value), project_root))
            or any(inside_place(str(value), tree) for tree in evidence_legacy.trees(project_root))
        ):
            problems.append(
                f"{p.label}: {value} is the project folder or one of its old results "
                f"folders — choose a folder of its own (e.g. {default_spec(project_root)['params']['root']})"
            )
    return problems


def uncreatable_root(spec: dict[str, Any]) -> list[str]:
    """Why this entry's evidence folder could not be created (``[]`` when it could).

    The owner's create rule — the project's folder is made for it, the folders above it
    are not — is the one entry check that must touch the filesystem, so it is asked
    HERE and not in :func:`validate_spec`: that one runs on every ``textChanged`` of
    the Evidence tab, and stat-ing a half-typed UNC root cost 21 s per keystroke on an
    offline share. Apply asks it once, *Check access* proves it on its worker and the
    Move proves it again before copying, which is the whole of the owner's "refused at
    Apply and in Check access". I/O.
    """
    if spec.get("backend") != "filesystem":
        return []  # a server's base path is never created
    for p in backend_params(spec.get("backend", "")):
        if p.type != "path":
            continue
        value = param_value(spec, p.key) or p.default
        root = Path(str(value)) if value else None
        if root is None or not root.is_absolute():
            continue  # absent or relative: validate_spec already refused it, without I/O
        problem = evidence_store_fs_shared.missing_parent(root)
        if problem:
            return [f"{p.label}: {problem}"]
    return []


def _ftps_problems(spec: dict[str, Any], project_root: Path | None) -> list[str]:
    """Server settings, not filesystem paths. A port is a number in range, and the two
    transport flags contradict each other in BOTH directions — refused here rather than at the first
    connection, where the run is already over."""
    problems: list[str] = []
    port = str(param_value(spec, "port") or "").strip() or "21"
    if not (port.isdigit() and 1 <= int(port) <= 65535):
        problems.append(f"Port: {port!r} is not a port number between 1 and 65535")
    params = spec.get("params")
    params = params if isinstance(params, dict) else {}
    # The store's own reader, so "what the tab refuses" and "what a built store does"
    # can never drift apart on the spelling of a flag.
    tls = evidence_remote_cache.flag(params, "tls", True)
    insecure = evidence_remote_cache.flag(params, "insecure", False)
    if tls and insecure:
        problems.append(
            "TLS and Plain FTP cannot both be on — turn TLS off to accept sending the "
            "password and all evidence in clear, or clear Plain FTP to keep the "
            "connection encrypted"
        )
    elif not tls and not insecure:
        problems.append(
            "TLS is off but Plain FTP is not asked for — plain FTP sends the password "
            "and all evidence in clear and must be chosen explicitly"
        )
    return problems


def _http_problems(spec: dict[str, Any], project_root: Path | None) -> list[str]:
    """A server address, not a filesystem path. The address is one this app can spell
    (and never one that already ends in ``/api/v1``, which the app appends), it is
    ``https`` unless plain HTTP is explicitly asked for, and the two transport flags
    contradict each other in BOTH directions — refused here rather than at the first upload,
    where the run is already over.

    **No I/O**: this runs on every keystroke of the Evidence tab. Whether the CA
    bundle is readable belongs at Apply.
    """
    problems: list[str] = []
    params = spec.get("params")
    params = params if isinstance(params, dict) else {}
    # The store's own readers, so "what the tab refuses" and "what a built store does"
    # can never drift apart on the spelling of a flag or of an address.
    tls = evidence_remote_cache.flag(params, "tls", True)
    insecure = evidence_remote_cache.flag(params, "insecure", False)
    if tls and insecure:
        problems.append(
            "TLS and Plain HTTP cannot both be on — turn TLS off to accept sending the "
            "token and all evidence in clear, or clear Plain HTTP to keep the "
            "connection encrypted"
        )
    elif not tls and not insecure:
        problems.append(
            "TLS is off but Plain HTTP is not asked for — plain HTTP sends the token "
            "and all evidence in clear and must be chosen explicitly"
        )
    raw = str(param_value(spec, "base_url") or "").strip()
    if raw:
        try:
            scheme = evidence_http.normalize_base(raw).split(":", 1)[0]
        except EvidenceError as exc:
            problems.append(f"Server address: {exc}")
        else:
            if scheme != ("http" if insecure else "https"):
                problems.append(
                    f"Server address: {raw} is {scheme}:// — an https address is required "
                    "unless Plain HTTP is chosen"
                )
    timeout = str(param_value(spec, "timeout") or "").strip()
    if timeout and not (timeout.replace(".", "", 1).isdigit() and float(timeout) > 0):
        problems.append(f"Timeout (s): {timeout!r} is not a number of seconds")
    return problems


def _postgres_problems(spec: dict[str, Any], project_root: Path | None) -> list[str]:
    """A port is a number in range; the host and the database are required (declared). The
    login is this PC's, on Settings ▸ Database, never the entry's."""
    port = str(param_value(spec, "port") or "").strip() or "5432"
    if not (port.isdigit() and 1 <= int(port) <= 65535):
        return [f"Port: {port!r} is not a port number between 1 and 65535"]
    return []


def _sftp_problems(spec: dict[str, Any], project_root: Path | None) -> list[str]:
    """A port is a number in range; the rest of an SFTP server is its key and sign-in, which
    are this PC's, never the entry's."""
    port = str(param_value(spec, "port") or "").strip() or "22"
    if not (port.isdigit() and 1 <= int(port) <= 65535):
        return [f"Port: {port!r} is not a port number between 1 and 65535"]
    return []


#: Backend id -> its own rules. A backend with none gets the declared checks alone.
_BACKEND_RULES: dict[str, Callable[[dict[str, Any], Path | None], list[str]]] = {
    "filesystem": _filesystem_problems,
    "ftps": _ftps_problems,
    "sftp": _sftp_problems,
    "http": _http_problems,
    "postgres": _postgres_problems,
}


def validate_spec(spec: dict[str, Any], project_root: Path | None = None) -> list[str]:
    """Problems with an entry, in the user's words; ``[]`` when it can be opened.

    Two layers: what every backend's declared ``PARAMS`` promise (required values,
    ``choice`` options), then the backend's OWN rules — a filesystem root is a folder
    on this machine, an FTPS endpoint is a host, a port and a path on a server.
    Applying the folder rules to every ``path``-shaped param is what rejected a POSIX
    ``base_path`` on Windows, so the type of a param is read by the backend that
    declared it and by nobody else.
    """
    backend = spec.get("backend", "")
    if backend not in REGISTRY:
        return [f"unknown evidence backend {backend!r}"]
    if getattr(REGISTRY[backend], "hidden", False):
        # Registered so code and tests can build it, but not offered as a configuration
        # until everything it needs exists (what FTPS carried until slice 5a-3).
        return [f"the {backend.upper()} backend is not configurable yet"]
    rules = _BACKEND_RULES.get(backend)
    problems = _declared_problems(spec, backend)
    return problems + (rules(spec, project_root) if rules is not None else [])
