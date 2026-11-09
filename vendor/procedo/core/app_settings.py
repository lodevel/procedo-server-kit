"""Global application settings — operator management, serial history, export folder.

Settings are stored in a single JSON file at:

- **Windows**: ``%APPDATA%/Procedo/app_settings.json``
- **Unix**:    ``~/.config/Procedo/app_settings.json``

The file contains:

- ``operators``          – list of known operator names
- ``current``            – currently selected operator (or *None*)
- ``serial_history``     – last 20 boards entered, by their key (``evidence_board``)
- ``last_export_folder`` – most-recently used export directory
- ``default_config``     – name of the default customer config (or *None*)
- ``recent_projects``    – last 10 project folder paths opened
- ``theme``              – application theme id (``original`` by default)
- ``layout``             – application layout id (``classic`` by default)
- ``indicators``         – passive status indicator style (``classic`` by default)
- ``max_venvs``          – shared Python environments kept on this PC (5 by default)

This module is intentionally free of any project-model or config-manager
imports so that both of those higher-level modules can depend on it without
circular imports.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import socket
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import app_identity, atomic_write

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def get_app_data_dir() -> Path:
    """Get the application data directory for storing global settings."""
    app_dir = app_identity.roaming_data_dir()
    app_dir.mkdir(parents=True, exist_ok=True)
    if os.name != "nt" and stat.S_IMODE(app_dir.stat().st_mode) != 0o700:
        # This directory holds evidence-store credentials: this account alone.
        app_dir.chmod(0o700)
    return app_dir


def get_app_settings_file() -> Path:
    """Get the path to the application settings file."""
    return get_app_data_dir() / "app_settings.json"


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


def _migrate_operators_file() -> None:
    """Transparently migrate legacy operators.json -> app_settings.json."""
    app_dir = get_app_data_dir()
    old_file = app_dir / "operators.json"
    new_file = app_dir / "app_settings.json"
    if old_file.exists() and not new_file.exists():
        try:
            old_file.rename(new_file)
            logger.info("Migrated operators.json -> app_settings.json")
        except OSError as e:
            logger.warning("Could not migrate operators.json: %s", e)


# ---------------------------------------------------------------------------
# Load / Save
# ---------------------------------------------------------------------------


def load_app_settings() -> dict[str, Any]:
    """Load application settings from global file.

    Returns:
        Dict with keys: 'operators' (list), 'current' (str or None),
        'serial_history' (list), 'last_export_folder' (str or None),
        'default_config' (str or None)
    """
    _migrate_operators_file()
    settings_file = get_app_settings_file()
    if settings_file.exists():
        try:
            with open(settings_file, encoding="utf-8") as f:
                data = json.load(f)
                # Ensure all keys exist
                if "operators" not in data:
                    data["operators"] = []
                if "current" not in data:
                    data["current"] = None
                if "serial_history" not in data:
                    data["serial_history"] = []
                if "last_export_folder" not in data:
                    data["last_export_folder"] = None
                if "default_config" not in data:
                    data["default_config"] = None
                if "recent_projects" not in data:
                    data["recent_projects"] = []
                if "theme" not in data:
                    data["theme"] = "original"
                if "indicators" not in data:
                    data["indicators"] = "classic"
                return data
        except (json.JSONDecodeError, OSError) as e:
            # Corrupt/unreadable settings file: fall back to defaults, but
            # surface it — the user's saved settings are silently reset.
            logger.warning("Could not read app settings (%s); using defaults: %s", settings_file, e)

    # Return default structure
    return {
        "operators": [],
        "current": None,
        "serial_history": [],
        "last_export_folder": None,
        "default_config": None,
        "recent_projects": [],
        "theme": "original",
        "indicators": "classic",
    }


def save_app_settings(data: dict[str, Any]) -> None:
    """Write the settings file atomically, and RAISE when it cannot be written.

    This file stopped being only conveniences: it holds, since slice 5a-3, evidence-store
    credentials. The previous implementation truncated the real file and wrote into it,
    so a full disk or a kill mid-write left a truncated file whose next load silently
    returned bare defaults: no default evidence root, no recent projects, no
    operators. It also swallowed the error, so saving a credential could report
    success having stored nothing.

    Through :func:`atomic_write.write_text`: a reader sees the old file or the new one
    and never a half-written one, and a rename Windows refuses for a moment is retried.
    A caller whose action must not fail on this uses :func:`save_app_settings_best_effort`.
    """
    atomic_write.write_text(
        get_app_settings_file(),
        json.dumps(data, indent=2),
        mode=0o600 if os.name != "nt" else None,  # it may hold a secret: this account alone
    )


def save_app_settings_best_effort(data: dict[str, Any]) -> bool:
    """:func:`save_app_settings` for a caller whose action must not fail with it — the
    GUI setters, where a theme that could not be persisted is not worth an exception.
    Logs, and answers whether the write landed."""
    try:
        save_app_settings(data)
    except OSError as e:
        logger.error("Failed to save app settings: %s", e)
        return False
    return True


def update_app_settings(
    mutate: Callable[[dict[str, Any]], Any], *, best_effort: bool = False
) -> bool:
    """Re-read the file, apply ``mutate`` to it, write it back atomically.

    The lost-update fix. Every setter used to load a copy, sit on it while a window
    stayed open, then write the WHOLE file back — so the last writer erased whatever
    another window had saved meanwhile: a credential saved in one window vanished
    when another saved a theme, with no error anywhere. Re-reading immediately before
    the write narrows that window to the write itself. ``mutate`` returning ``False``
    means "nothing changed" and nothing is written.
    """
    data = load_app_settings()
    if mutate(data) is False:
        return False
    if best_effort:
        return save_app_settings_best_effort(data)
    save_app_settings(data)
    return True


def _set_key(key: str, value: Any) -> None:
    """One key of the settings file, merge-safe and best-effort (a GUI setter)."""
    update_app_settings(lambda data: data.update({key: value}), best_effort=True)


# ---------------------------------------------------------------------------
# Operator management
# ---------------------------------------------------------------------------


def add_operator(name: str) -> None:
    """Add a new operator to the list."""

    def mutate(data: dict[str, Any]) -> Any:
        if not name or name in data["operators"]:
            return False
        data["operators"].append(name)
        data["operators"].sort()

    update_app_settings(mutate, best_effort=True)


def remove_operator(name: str) -> None:
    """Remove an operator from the list."""

    def mutate(data: dict[str, Any]) -> Any:
        if name not in data["operators"]:
            return False
        data["operators"].remove(name)
        if data["current"] == name:
            data["current"] = None

    update_app_settings(mutate, best_effort=True)


def set_current_operator(name: str | None) -> None:
    """Set the current operator."""
    _set_key("current", name)


# ---------------------------------------------------------------------------
# Serial history & export folder
# ---------------------------------------------------------------------------


def add_board_to_history(board: str) -> None:
    """Add a board, by its key (:mod:`evidence_board`), to the history (keep last 20)."""

    def mutate(data: dict[str, Any]) -> Any:
        if not board or board in data["serial_history"]:
            return False
        data["serial_history"].insert(0, board)
        data["serial_history"] = data["serial_history"][:20]  # Keep last 20

    update_app_settings(mutate, best_effort=True)


def add_recent_project(path: str) -> None:
    """Record a project path in the recent-projects list (keep last 10)."""

    def mutate(data: dict[str, Any]) -> Any:
        recent: list = data.get("recent_projects", [])
        # Move to front if already present, otherwise insert
        if path in recent:
            recent.remove(path)
        recent.insert(0, path)
        data["recent_projects"] = recent[:10]

    update_app_settings(mutate, best_effort=True)


def set_last_export_folder(folder: str) -> None:
    """Remember the last export folder used."""
    _set_key("last_export_folder", folder)


# ---------------------------------------------------------------------------
# Bundle Generator: remembered capability and skill subsets
# ---------------------------------------------------------------------------


def _subset(key: str) -> list[str] | None:
    val = load_app_settings().get(key)
    return [s for s in val if isinstance(s, str)] if isinstance(val, list) else None


def get_bundle_capability_subset() -> list[str] | None:
    """The capability ids last built into a bundle; None when none was remembered yet (the
    caller defaults to all), ``[]`` for an explicit "none"."""
    return _subset("bundle_capability_subset")


def set_bundle_capability_subset(capability_ids: list[str]) -> None:
    """Remember the capability ids chosen for a bundle."""
    _set_key("bundle_capability_subset", list(capability_ids))


def get_bundle_skill_subset() -> list[str] | None:
    """The authoring-skill ids last built into a bundle; None when none was remembered
    yet (the caller defaults to all), ``[]`` for an explicit "no skills"."""
    return _subset("bundle_skill_subset")


def set_bundle_skill_subset(skill_ids: list[str]) -> None:
    """Remember the authoring-skill ids chosen for a bundle."""
    _set_key("bundle_skill_subset", list(skill_ids))


# ---------------------------------------------------------------------------
# Theme
# ---------------------------------------------------------------------------


def get_theme() -> str:
    """Return the configured application theme id."""
    value = load_app_settings().get("theme", "original")
    return value if isinstance(value, str) else "original"


def set_theme(theme_name: str) -> None:
    """Set the application theme id."""
    _set_key("theme", theme_name)


# ---------------------------------------------------------------------------
# Passive status indicators
# ---------------------------------------------------------------------------


def get_indicator_style() -> str:
    """Return the configured passive status indicator style id."""
    value = load_app_settings().get("indicators", "classic")
    return value if isinstance(value, str) else "classic"


def set_indicator_style(style_name: str) -> None:
    """Set the passive status indicator style id."""
    _set_key("indicators", style_name)


def get_alert_sound() -> bool:
    """Whether the guided-manual auto-run plays an alert beep when it pauses
    for the operator (action needed) and on finish. Defaults to True."""
    value = load_app_settings().get("alert_sound", True)
    return bool(value) if isinstance(value, bool) else True


def set_alert_sound(enabled: bool) -> None:
    """Enable/disable the guided-manual auto-run alert beep."""
    _set_key("alert_sound", bool(enabled))


#: The settings that remember a side pane folded: the interactive run's Values / Terminals /
#: Explorer pane, Studio's right panel (Assistant / Phases), the Export window's preview.
RUN_SIDE_PANE, STUDIO_SIDE_PANE = "side_pane_collapsed", "studio_side_pane_collapsed"
EXPORT_PREVIEW_PANE = "export_preview_collapsed"


def get_side_pane_collapsed(key: str, *, default: bool = False) -> bool:
    """Whether the side pane *key* remembers (:data:`RUN_SIDE_PANE`, :data:`STUDIO_SIDE_PANE`)
    starts folded; *default* until the person folds or opens it once."""
    held = load_app_settings().get(key)
    return held if isinstance(held, bool) else default


def set_side_pane_collapsed(key: str, collapsed: bool) -> None:
    _set_key(key, bool(collapsed))


#: How many shared Python environments this computer keeps (``venv_pool``). Each
#: is a bundle's wheels and their dependencies — hundreds of megabytes — and one
#: is shared by every project on that bundle, so a handful covers a bench.
_DEFAULT_MAX_VENVS = 5


def get_max_venvs() -> int:
    """How many shared Python environments to keep on this computer.

    Never below one: zero would delete the environment of the project being
    opened the moment it was built, and every run after that would refuse.
    """
    value = load_app_settings().get("max_venvs", _DEFAULT_MAX_VENVS)
    if isinstance(value, bool) or not isinstance(value, int):
        return _DEFAULT_MAX_VENVS
    return max(1, value)


def set_max_venvs(count: int) -> None:
    """Set the environment limit. Clamped to at least one, as the getter is."""
    _set_key("max_venvs", max(1, int(count)))


_MACHINE_ID_FILE = "machine_id"
_MACHINE_ID_LEN = 8
#: Beside the id: a fingerprint of the operating system's installation it was minted on.
_INSTALL_FILE = "machine_id.install"
#: An id minted while it could not be saved: kept for this process, never a new one per call.
_unsaved_id: str | None = None


def _install_fingerprint() -> str:
    """This operating-system installation, hashed (``""`` when it cannot be told): Windows'
    ``MachineGuid``, ``/etc/machine-id`` elsewhere. A copied profile or local data folder, or a
    sysprepped image, lands on another installation; a raw disk clone keeps it and is not told
    apart."""
    raw = ""
    try:
        if os.name == "nt":
            import winreg

            key = winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Cryptography",
                0,
                winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
            )
            with key:
                raw = str(winreg.QueryValueEx(key, "MachineGuid")[0])
        else:
            raw = Path("/etc/machine-id").read_text(encoding="ascii").strip()
    except (OSError, ValueError):
        return ""
    return hashlib.sha256(raw.encode()).hexdigest()[:16] if raw else ""


def _bind(folder: Path) -> None:
    """Record this installation beside a newly minted id."""
    here = _install_fingerprint()
    if not here:
        return
    try:
        atomic_write.write_text(folder / _INSTALL_FILE, here, encoding="ascii")
    except OSError:
        logger.warning("Could not record this installation in %s", folder, exc_info=True)


def _bound_elsewhere(folder: Path) -> bool:
    """Whether the id in *folder* was minted on another installation (this PC was cloned, or
    its local data copied from another); the fingerprint is recorded when it is missing."""
    here = _install_fingerprint()
    if not here:
        return False
    path = folder / _INSTALL_FILE
    try:
        bound = path.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        bound = ""
    except (OSError, ValueError):
        return False
    if not bound:
        _bind(folder)  # an id from before the fingerprint: this installation adopts it
        return False
    return bound != here


def _read_machine_id(path: Path) -> str | None:
    """The id in *path*; ``None`` when there is no file or it holds no id. Any other
    ``OSError`` (a scanner holding the file) propagates: an id that exists is never
    taken for missing."""
    try:
        held = path.read_text(encoding="ascii").strip()
    except (FileNotFoundError, ValueError):
        return None
    return held if len(held) == _MACHINE_ID_LEN else None


def get_machine_id() -> str:
    """This PC's evidence-store identity: 8 hex digits generated once and kept in the
    LOCAL data folder (the hostname is a label only, never the key). Not in the settings
    file: that one is roaming, and two PCs sharing a profile would share one id — each
    would then judge the other's live ``.owner`` leases by its own process table.

    Minting is merge-safe and adopts a value another window wrote first, because this
    id is the writer identity in every ``.owner`` and the routing key of every outbox
    entry: two windows minting different ids for one machine would split them. A file
    that does not hold an id is replaced; one that cannot be read raises.

    **One id per PC, even cloned.** An id minted on another installation (a copied local data
    folder, a sysprepped image: :func:`_install_fingerprint`) is replaced by a new one: two PCs
    sharing an id would each take the other's live locks for its own dead ones
    (docs/plans/company_storage_design.md §8, F11). An id that cannot be saved is minted once
    for this process, never anew at each call.
    """
    global _unsaved_id
    path = app_identity.local_data_dir() / _MACHINE_ID_FILE
    held = _read_machine_id(path)
    if held and _bound_elsewhere(path.parent):
        minted = secrets.token_hex(_MACHINE_ID_LEN // 2)
        logger.warning(
            "This PC's machine id %s was made on another installation (a cloned PC or copied "
            "local data): it now has its own, %s",
            held,
            minted,
        )
        atomic_write.write_text(path, minted, encoding="ascii")
        _bind(path.parent)
        return minted
    if held:
        return held
    if _unsaved_id is not None:
        return _unsaved_id
    minted = secrets.token_hex(_MACHINE_ID_LEN // 2)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write.create_bytes(path, minted.encode("ascii"))
        _bind(path.parent)
        return minted
    except FileExistsError:
        pass  # another window got there first, or the file is damaged
    except OSError:
        logger.warning("The machine id could not be saved in %s", path, exc_info=True)
        _unsaved_id = minted
        return minted
    held = _read_machine_id(path)
    if held:
        return held
    atomic_write.write_text(path, minted, encoding="ascii")  # damaged: replaced
    return minted


_STATION_NAME_FILE = "station_name"


def get_station_name() -> str:
    """The station name this PC writes in every result, "" when none was given. Kept in the
    LOCAL data folder beside the machine id, never in the roaming settings file: two PCs
    sharing a profile are two stations. A file that cannot be read reads as none."""
    try:
        path = app_identity.local_data_dir() / _STATION_NAME_FILE
        return path.read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return ""


def set_station_name(name: str) -> None:
    """Set this PC's station name; "" removes it (the computer's name again)."""
    path = app_identity.local_data_dir() / _STATION_NAME_FILE
    if not name.strip():
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write.write_text(path, name.strip(), encoding="utf-8")


def station_id() -> str:
    """The station a result records (OpenHTF's ``station_id``): this PC's station name, else
    the computer's name."""
    return get_station_name() or socket.gethostname()


def get_default_evidence_root() -> str:
    """Machine-wide PARENT for evidence folders (empty = none).

    A project's evidence folder is the path its own entry names, exactly as typed
    (2026-09-19) -- the store adds no level of its own -- so this is what the
    Evidence tab pre-fills a NEW folder under (``<default>/<project name>``,
    editable), never the folder itself: one folder shared by every project on the
    bench would interleave their boards.
    """
    value = load_app_settings().get("default_evidence_root", "")
    return value if isinstance(value, str) else ""


def set_default_evidence_root(path: str) -> None:
    _set_key("default_evidence_root", path.strip())


# ---------------------------------------------------------------------------
# Image generator settings
# ---------------------------------------------------------------------------


def _image_generator_defaults() -> dict[str, Any]:
    """Return the default image-generator settings dict."""
    return {
        "img_size": 1024,
        "render_size": 4096,
        "window_mm_zoomed": 40.0,
        "window_mm_wide": 200.0,
        "cross_arm_mm_zoomed": 3.0,
        "cross_thickness_px_zoomed": 6,
        "cross_arm_mm_wide": 6.0,
        "cross_thickness_px_wide": 12,
        "parallel_render": False,
        "parallel_export": True,
        "max_workers": 0,
        "batch_size": 50,
    }


def get_image_generator_settings(project_dir: Path | None = None) -> dict[str, Any]:
    """Image-generator settings: the built-in defaults overlaid with the
    project's ``config/config.json:image_generator`` override (when *project_dir*
    is given). Per-project only — there is no app-wide layer. Read with plain
    ``json`` to keep this module free of config-manager imports."""
    settings = _image_generator_defaults()
    if project_dir is not None:
        try:
            data = json.loads(
                (Path(project_dir) / "config" / "config.json").read_text(encoding="utf-8")
            )
            override = data.get("image_generator")
            if isinstance(override, dict):
                settings.update(override)
        except (OSError, ValueError) as e:
            # Best-effort project override: a missing config/config.json is
            # normal for projects without one — keep built-in defaults.
            logger.debug("No image_generator override from %s: %s", project_dir, e)
    return settings


# ---------------------------------------------------------------------------
# Search dialog: remembered filters
# ---------------------------------------------------------------------------


def get_last_search_filters() -> tuple[str, str, str]:
    """The search dialog's last part number, board and test filters, remembered per machine
    (verdict and the date range describe a moment, not a habit, so they are not)."""
    data = load_app_settings()
    found = (data.get(f"search_last_{name}", "") for name in ("part_number", "board", "test"))
    part_number, board, test = (value if isinstance(value, str) else "" for value in found)
    return part_number, board, test


def set_last_search_filters(part_number: str, board: str, test: str) -> None:
    """Persist the search dialog's part number, board and test filters."""

    def mutate(data: dict[str, Any]) -> Any:
        data["search_last_part_number"] = part_number
        data["search_last_board"] = board
        data["search_last_test"] = test

    update_app_settings(mutate, best_effort=True)
