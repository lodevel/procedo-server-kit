"""The one place that finds packages and the items they offer.

A package is a folder with one sub-folder per kind (``configs/``, ``skills/``, ...) and an
optional ``package.json``. Packages live under ``<repo>/packages/``: ``builtin/<id>/`` ships
with the app, ``local/`` is the user's own loose items, ``installed/<id>/`` holds copied
folders and git clones. Nothing is cached: a scan per call, the folders are small.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import app_settings
from .library_entry import version_key

logger = logging.getLogger(__name__)

KINDS = ("capabilities", "configs", "exports", "reports", "wizards", "skills", "tools")

#: Kinds a menu lists by label, so the same id in two packages is fine. Every other kind is
#: referenced by id alone, so an id two switched-on packages offer is ambiguous.
_LABELLED = frozenset({"configs", "exports", "reports"})

_SINGULAR = {
    "capabilities": "Capability",
    "configs": "Config",
    "exports": "Export template",
    "reports": "Report template",
    "wizards": "Wizard",
    "skills": "Skill",
    "tools": "Tool",
}

_MARKERS = {
    "configs": "config.json",
    "exports": "schema.json",
    "reports": "schema.json",
    "wizards": "skill.md",
    "skills": "skill.md",
    "tools": "tools.json",
}

_OFF_KEY = "packages_off"
LOCAL_ID = "local"
LOCAL_NAME = "Local"


def packages_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent.parent / "packages"


def builtin_root() -> Path:
    return packages_root() / "builtin"


#: The app's own builtin package: the templates, wizards, skills and tools this repository ships.
APP_PACKAGE = "procedo"


def app_package_root() -> Path:
    return builtin_root() / APP_PACKAGE


def local_root() -> Path:
    return packages_root() / "local"


def installed_root() -> Path:
    return packages_root() / "installed"


@dataclass(frozen=True)
class Package:
    id: str
    name: str
    version: str
    root: Path
    place: str  # "builtin" | "local" | "installed"
    is_git: bool
    enabled: bool


@dataclass(frozen=True)
class Item:
    kind: str
    id: str
    version: str  # "" when the item is not versioned
    folder: Path
    package: Package


def _subfolders(parent: Path) -> list[Path]:
    """Visible sub-folders of *parent*, sorted by name; none when it does not exist."""
    if not parent.is_dir():
        return []
    return sorted(
        (
            p
            for p in parent.iterdir()
            if p.is_dir() and p.name[0] != "." and p.name != "__pycache__"
        ),
        key=lambda p: p.name,
    )


def _manifest(root: Path) -> dict[str, Any]:
    path = root / "package.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.warning("Ignoring unreadable %s: %s", path, e)
        return {}
    if not isinstance(data, dict):
        logger.warning("Ignoring %s: it is not a JSON object", path)
        return {}
    return data


def _text(manifest: dict[str, Any], key: str, default: str) -> str:
    value = manifest.get(key)
    return value if isinstance(value, str) and value else default


def identity(root: Path, fallback: str | None = None) -> tuple[str, str, str]:
    """The ``(id, name, version)`` the package folder *root* declares in its
    ``package.json``; *fallback* (default: the folder name) stands for a missing id or
    name, ``""`` for a missing version."""
    manifest = _manifest(root)
    name = fallback or root.name
    return (
        _text(manifest, "id", name),
        _text(manifest, "name", name),
        _text(manifest, "version", ""),
    )


def _switched_off() -> set[str]:
    value = app_settings.load_app_settings().get(_OFF_KEY)
    return {v for v in value if isinstance(v, str)} if isinstance(value, list) else set()


def _package(root: Path, place: str, off: set[str]) -> Package:
    if place == "local":
        package_id, name, version = LOCAL_ID, LOCAL_NAME, ""
    else:
        package_id, name, version = identity(root)
    return Package(
        id=package_id,
        name=name,
        version=version,
        root=root,
        place=place,
        is_git=(root / ".git").exists(),
        enabled=package_id not in off,
    )


def packages() -> list[Package]:
    """Every package: builtin by name, then the local one, then installed by name."""
    off = _switched_off()
    found = [_package(root, "builtin", off) for root in _subfolders(builtin_root())]
    found.append(_package(local_root(), "local", off))
    found += [_package(root, "installed", off) for root in _subfolders(installed_root())]
    return found


def _split_duplicates(found: list[Package]) -> tuple[list[Package], list[tuple[Package, Package]]]:
    """The packages that count, in listing order, and each (winner, loser) pair sharing an id.
    The first listed wins, except that Local always does: a package naming itself "local"
    must not hide the user's own items."""
    winners: dict[str, Package] = {}
    for package in sorted(found, key=lambda p: p.place != "local"):
        winners.setdefault(package.id, package)
    counting = [p for p in found if winners[p.id] is p]
    clashes = [(winners[p.id], p) for p in found if winners[p.id] is not p]
    return counting, clashes


def set_enabled(package_id: str, on: bool) -> None:
    def mutate(data: dict[str, Any]) -> Any:
        off = {v for v in data.get(_OFF_KEY, []) if isinstance(v, str)}
        if on:
            off.discard(package_id)
        else:
            off.add(package_id)
        data[_OFF_KEY] = sorted(off)

    app_settings.update_app_settings(mutate, best_effort=True)


def _holds_marker(folder: Path, kind: str) -> bool:
    marker = _MARKERS[kind]
    return any(p.is_file() and p.name.lower() == marker for p in folder.iterdir())


def _is_capability(folder: Path) -> bool:
    return next(folder.glob("src/*/rules/*/rules_index.json"), None) is not None


def _package_items(package: Package, kind: str) -> list[Item]:
    items = []
    for child in _subfolders(package.root / kind):
        if kind == "capabilities":
            if _is_capability(child):
                items.append(Item(kind, child.name, "", child, package))
        elif _holds_marker(child, kind):
            items.append(Item(kind, child.name, "", child, package))
        else:
            versions = [v for v in _subfolders(child) if _holds_marker(v, kind)]
            if versions:
                newest = max(versions, key=lambda v: version_key(v.name))
                items.append(Item(kind, child.name, newest.name, newest, package))
    return items


def _by_id(kind: str) -> dict[str, list[Item]]:
    """Items of *kind* from the switched-on packages that count, grouped by id."""
    grouped: dict[str, list[Item]] = {}
    counting, _ = _split_duplicates(packages())
    for package in counting:
        if package.enabled:
            for item in _package_items(package, kind):
                grouped.setdefault(item.id, []).append(item)
    return grouped


def items(kind: str) -> list[Item]:
    """The items of *kind* in switched-on packages, in package order. An id offered by two
    packages is dropped for the kinds that are referenced by id (see :func:`problems`)."""
    grouped = _by_id(kind)
    if kind in _LABELLED:
        return [item for group in grouped.values() for item in group]
    return [group[0] for group in grouped.values() if len(group) == 1]


def contents(package: Package) -> dict[str, list[str]]:
    """The item ids *package* holds, per kind, whether it is switched on or off."""
    return {kind: [item.id for item in _package_items(package, kind)] for kind in KINDS}


def find_item(kind: str, package_id: str, item_id: str) -> Item | None:
    """The item *item_id* of *kind* from package *package_id*, whether that package is
    switched on or off (a project seeded from one now off still finds its folder); the
    newest version of a versioned one. ``None`` when the package or the item is missing."""
    counting, _ = _split_duplicates(packages())
    for package in counting:
        if package.id == package_id:
            return next((i for i in _package_items(package, kind) if i.id == item_id), None)
    return None


def rules_index(capability: Item) -> Path:
    """A capability's newest ``src/<name>/rules/<ver>/rules_index.json``."""
    return max(
        capability.folder.glob("src/*/rules/*/rules_index.json"),
        key=lambda f: version_key(f.parent.name),
    )


def ble_name_prefixes() -> list[str]:
    """The advertised-name prefixes of the BLE terminals the switched-on capabilities
    declare (``pack_capabilities.ble_name_prefixes`` of each newest rules_index.json)."""
    found: list[str] = []
    for capability in items("capabilities"):
        try:
            index = json.loads(rules_index(capability).read_text(encoding="utf-8"))
            prefixes = index["pack_capabilities"].get("ble_name_prefixes") or []
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue
        found += [p for p in prefixes if isinstance(p, str) and p]
    return list(dict.fromkeys(found))


def _names(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def problems() -> list[str]:
    """What the Packages window shows: duplicate package ids and ambiguous item ids."""
    found: list[str] = []
    _, clashes = _split_duplicates(packages())
    for first, later in clashes:
        found.append(
            f'Package id "{first.id}" is used by {first.root.name} and {later.root.name}: '
            f"only {first.root.name} is read."
        )
    for kind in KINDS:
        if kind in _LABELLED:
            continue
        for item_id, group in _by_id(kind).items():
            if len(group) > 1:
                names = _names([item.package.name for item in group])
                found.append(f"{_SINGULAR[kind]} {item_id} is in {names}: switch one off.")
    return found


def ensure_local_layout() -> None:
    for kind in KINDS:
        (local_root() / kind).mkdir(parents=True, exist_ok=True)
