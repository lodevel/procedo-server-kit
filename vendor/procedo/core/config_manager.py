"""Configuration Manager - handles named customer configurations.

A *configuration* is a named directory under ``packages/local/configs/`` at the repo root.
All settings live in a single ``config.json`` file with nested sections:

- ``manifest``   – metadata (name, description, author, version, content_sha256)
- ``profiles``   – controller type definitions (``controllers`` list)
- ``patterns``   – regex patterns for equipment detection
- ``packs``      – selected driver pack IDs

Additional on-disk artefacts:

- ``templates/reports/``     – HTML report package folders
- ``templates/exports/``     – export package folders (Word/Markdown)
- ``project_rules/``         – the project's own rule documents (``config/project_rules/``)

Its identity in a library is ``manifest.name@manifest.version`` plus the content
hash of the folder (:mod:`library_entry`); a bench keeps ONE version of a name.

Customer configs serve as a *template library* (:mod:`config_templates`) for seeding or
gap-filling project configurations (:mod:`project_seeding`).  The project's own ``config/``
directory is the sole runtime source; this module holds the paths and that ``config.json``.
The project ``config.json`` uses an ``origin`` section (instead of ``manifest``)
to record the source config name and default template choices.

Legacy projects that still have separate files (``manifest.json``,
``equipment_patterns.json``, ``pack_selection.json``,
``active_config.json``) are auto-migrated to the unified format on first access.
"""

from __future__ import annotations

import json
import logging
import shutil
from collections.abc import Iterable
from pathlib import Path
from typing import Any, NamedTuple

from . import atomic_write, paths, run_record

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def get_base_config_dir() -> Path:
    """Return the ``app_config/`` directory (built-in base configuration)."""
    return Path(__file__).resolve().parent.parent.parent.parent / "app_config"


def get_configs_dir() -> Path:
    """Return the customer-config library dir: ``packages/local/configs/``."""
    return get_local_packages_dir() / "configs"


def get_local_packages_dir() -> Path:
    """The Local package's folder (:func:`package_store.local_root`): the user's own loose
    items on this machine."""
    from .package_store import local_root

    return local_root()


def _legacy_configs_dir() -> Path:
    """Pre-migration home of customer configs: repo-root ``customer_configs/``."""
    return Path(__file__).resolve().parent.parent.parent.parent / "customer_configs"


def migrate_customer_configs() -> None:
    """One-shot move of legacy ``customer_configs/<name>/`` dirs into the local
    configs dir; loose files stay, collisions warn (legacy left); never raises."""
    if not (legacy := _legacy_configs_dir()).is_dir():
        return
    try:  # never-raises is load-bearing: app main() calls this pre-QApplication
        sources = sorted(p for p in legacy.iterdir() if p.is_dir())
        get_configs_dir().mkdir(parents=True, exist_ok=True)
    except OSError as exc:  # unreadable/uncreatable (Windows ACL) must not kill startup
        return logger.warning("Cannot migrate legacy configs from %s: %s", legacy, exc)
    for src in sources:
        dst = get_configs_dir() / src.name
        if dst.exists():
            logger.warning("Config %r already at %s; legacy copy left at %s", src.name, dst, src)
            continue
        try:
            shutil.move(str(src), str(dst))
            logger.info("Migrated customer config %r -> %s", src.name, dst)
        except OSError as exc:
            logger.warning("Could not migrate config %r: %s", src.name, exc)


def split_config_ref(ref: str) -> tuple[str, str]:
    """A configuration reference as (package id, name). A Local configuration is its bare
    name; one from another package is ``"<package id>/<name>"`` (a folder name holds no
    ``/``)."""
    from .package_store import LOCAL_ID

    package_id, slash, name = ref.partition("/")
    return (package_id, name) if slash else (LOCAL_ID, ref)


def get_config_path(ref: str) -> Path:
    """Return the folder of the configuration *ref* (see :func:`split_config_ref`).

    A bare name is a Local configuration, whether or not it exists yet (that is how one is
    created). A reference to another package resolves through :func:`package_store.find_item`,
    that package on or off; ``ValueError`` when it names nothing."""
    from .package_store import LOCAL_ID, find_item

    package_id, name = split_config_ref(ref)
    if package_id == LOCAL_ID:
        return get_configs_dir() / name
    item = find_item("configs", package_id, name)
    if item is None:
        raise ValueError(f'No configuration "{name}" in a package "{package_id}".')
    return item.folder


PROJECT_RULES = "project_rules"
"""A project's own rule documents: ``<project>/config/project_rules/``; a template's
``project_rules/`` replaces it on seed (New Project, Apply Template)."""

_PROJECT_RULE_SUFFIXES = (".md", ".txt", ".pdf")


def project_rule_doc_paths(folder: Path | None) -> list[Path]:
    """The rule documents in *folder*: its top-level ``*.md`` / ``*.txt`` / ``*.pdf``
    files (any case), sorted; none when the folder is missing. Anything else there
    (Thumbs.db, desktop.ini, a sub-folder) is not a rule document."""
    if folder is None or not folder.is_dir():
        return []
    return sorted(
        p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in _PROJECT_RULE_SUFFIXES
    )


def template_has_project_rules(name: str) -> bool:
    """Seeding from template *name* replaces the project's rule documents: its
    ``project_rules/`` holds at least one (see :func:`project_rule_doc_paths`)."""
    return bool(project_rule_doc_paths(get_config_path(name) / PROJECT_RULES))


def replace_folder(src: Path, dst: Path) -> None:
    """Make *dst* a copy of the folder *src*; a missing *src* leaves *dst* untouched.

    The copy goes to a new sibling first, then :func:`paths.swap_into_place` swaps
    it in: a failed copy, or a *dst* that cannot move (a file in it held open by
    another process), leaves *dst* as it was and no sibling behind. A swap that
    failed half-way (:class:`paths.SwapIncomplete`) deletes nothing: the copy stays
    too, and the error names both folders."""
    if not src.is_dir():
        return
    paths.replace_with(
        dst,
        paths.new_copy_folder(dst),
        lambda staged: shutil.copytree(src, staged, dirs_exist_ok=True),
    )


# ---------------------------------------------------------------------------
# Unified config.json helpers
# ---------------------------------------------------------------------------

UNIFIED_CONFIG = "config.json"
"""Name of the single config file used at every scope."""

# Mappings from old separate files → section keys in config.json.
OLD_CUSTOMER_FILES: dict[str, str] = {
    "manifest.json": "manifest",
    "equipment_patterns.json": "patterns",
    "pack_selection.json": "packs",
}

OLD_PROJECT_FILES: dict[str, str] = {
    "active_config.json": "origin",
    "equipment_patterns.json": "patterns",
    "pack_selection.json": "packs",
}

OLD_APP_FILES: dict[str, str] = {
    "equipment_patterns.json": "patterns",
}


def load_unified_config(config_dir: Path) -> dict[str, Any]:
    """The unified ``config.json`` in *config_dir*, ``{}`` when there is none; raises when it
    cannot be read (a share that is down, a damaged file), so a writer never replaces what it
    could not read."""
    path = config_dir / UNIFIED_CONFIG
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        # open() goes through the C runtime, which says "not found" for a share that is down
        # too (no winerror); stat() is Win32 and tells the two apart.
        if paths.is_absent(path):
            return {}
        raise exc  # there after all: it could not be read
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is damaged (not valid JSON): {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not hold a JSON object")
    return data


def read_unified_config(config_dir: Path) -> dict[str, Any]:
    """Read the unified ``config.json`` from *config_dir*, returning ``{}`` on error."""
    try:
        return load_unified_config(config_dir)
    except Exception as e:
        logger.error("Failed to read %s: %s", config_dir / UNIFIED_CONFIG, e)
        return {}


def write_unified_config(config_dir: Path, data: dict[str, Any]) -> None:
    """Replace ``config.json`` in *config_dir* with *data* — atomically; other benches read it."""
    config_dir.mkdir(parents=True, exist_ok=True)
    atomic_write.write_json(config_dir / UNIFIED_CONFIG, data)


def read_config_section(config_dir: Path, section: str) -> dict[str, Any]:
    """Return a single top-level section from ``config.json``, or ``{}``."""
    return read_unified_config(config_dir).get(section, {})


def bundle_ref(cfg: Any) -> tuple[str, str] | None:
    """``(id, version)`` of a ``config.json``'s ``bundle: {id, version}`` (*cfg* as read);
    ``None`` when *cfg* names no bundle or names one in another shape."""
    ref = cfg.get("bundle") if isinstance(cfg, dict) else None
    if not isinstance(ref, dict):
        return None
    bid, bver = ref.get("id"), ref.get("version")
    return (bid, bver) if isinstance(bid, str) and isinstance(bver, str) else None


def write_config_section(config_dir: Path, section: str, section_data: dict[str, Any]) -> None:
    """Merge *section_data* into ``config.json`` under *section*; raises, writing nothing, when
    ``config.json`` is there but cannot be read (its other sections would be lost)."""
    data = load_unified_config(config_dir)
    data[section] = section_data
    write_unified_config(config_dir, data)


#: The project's run defaults (``config.json`` -> ``runs``); a missing key reads its default here.
RUNS_DEFAULTS: dict[str, bool] = {"stop_at_first_fail": False, "switch_off_on_stop": True}


def read_runs(config_dir: Path) -> dict[str, bool]:
    """The ``runs`` section with every key present (see :data:`RUNS_DEFAULTS`)."""
    stored = read_config_section(config_dir, "runs")
    return {key: bool(stored.get(key, default)) for key, default in RUNS_DEFAULTS.items()}


#: What Run runs (``config.json`` -> ``runs.what_runs``, docs/plans/test_release_design.md,
#: "Runs"): the test's files (the default, :data:`RUNS_FILES`) or its latest release
#: (:data:`RUNS_LATEST`).
WHAT_RUNS = "what_runs"
RUNS_FILES = "files"
RUNS_LATEST = "latest_release"


def read_what_runs(config_dir: Path) -> str:
    """The project's ``runs.what_runs``: :data:`RUNS_LATEST`, else :data:`RUNS_FILES`."""
    found = read_config_section(config_dir, "runs").get(WHAT_RUNS)
    return RUNS_LATEST if found == RUNS_LATEST else RUNS_FILES


#: The project's information Bench uses (``config.json`` -> ``info``, Project Configuration ›
#: Info): ``part_numbers``, the boards the project tests, the first one the header's default.
INFO_SECTION = "info"


def read_part_numbers(config_dir: Path) -> list[str]:
    """The project's ``info.part_numbers`` as written; ``[]`` for none."""
    found = read_config_section(config_dir, INFO_SECTION).get("part_numbers")
    return [p for p in found if isinstance(p, str) and p] if isinstance(found, list) else []


def write_part_numbers(config_dir: Path, numbers: list[str]) -> None:
    """Set the project's ``info.part_numbers`` (:func:`read_part_numbers`)."""
    write_config_section(config_dir, INFO_SECTION, {"part_numbers": list(numbers)})


def split_part_numbers(text: str) -> list[str]:
    """The part numbers of the comma-separated *text* (Project Configuration › Info, New
    Project's Board step), each as typed, in order, once."""
    return list(dict.fromkeys(p.strip() for p in text.split(",") if p.strip()))


#: The project's run fields (``config.json`` -> ``run_fields.fields``: ``[{"name", "required",
#: "asked", "when"}]``, Project Configuration › Run fields): what is recorded of a board, its runs
#: and its reports besides the part number and the serial. A field not asked is asked nowhere and
#: nothing records it; one not asked is never required. Each says WHEN it is entered: at each run
#: (Bench's header, recorded with the run), once per board (Bench's header, kept with the board) or
#: in the report (the Generate Report dialog, kept with the report).
RUN_FIELDS = "run_fields"
AT_RUN, PER_BOARD, IN_REPORT = "run", "board", "report"
WHENS = (AT_RUN, PER_BOARD, IN_REPORT)
#: The fixed entries, in their order, each with its own when (shown, never chosen): Revision is
#: picked in Bench with the board and recorded with each run; Lot is the board's; Firmware version
#: the run's; Work order the report's. A field the project adds chooses its when.
REVISION, LOT, FIRMWARE, WORK_ORDER = "Revision", "Lot", "Firmware version", "Work order"
FIXED: dict[str, str] = {REVISION: AT_RUN, LOT: PER_BOARD, FIRMWARE: AT_RUN, WORK_ORDER: IN_REPORT}


class RunField(NamedTuple):
    """One run field as the project sets it (:func:`run_field` settles its when)."""

    name: str
    required: bool = False
    asked: bool = True
    when: str = ""


def run_field(name: str, required: bool = False, asked: bool = True, when: str = "") -> RunField:
    """*name*'s field with its when settled: a fixed entry's own, else *when* (one of
    :data:`WHENS`), else at each run; a field not asked is not required."""
    settled = FIXED.get(name) or (when if when in WHENS else AT_RUN)
    return RunField(name, required and asked, asked, settled)


#: A project with no ``run_fields`` section: Revision asked and required; Lot, Firmware
#: version and Work order not asked.
DEFAULT_RUN_FIELDS: tuple[RunField, ...] = (
    run_field(REVISION, True, True),
    run_field(LOT, False, False),  # off until a project asks it (the owner, 2026-11-04)
    run_field(FIRMWARE, False, False),
    run_field(WORK_ORDER, False, False),
)


def read_run_fields(config_dir: Path) -> list[RunField]:
    """The project's run fields (:data:`DEFAULT_RUN_FIELDS` without a ``run_fields`` section):
    the fixed entries in their order — one the section does not list is not asked —, then the
    fields the project added, in the section's order. A field that does not say ``asked`` is
    asked; an added one that does not say ``when`` is entered at each run."""
    section = read_unified_config(config_dir).get(RUN_FIELDS)
    if not isinstance(section, dict) or not isinstance(section.get("fields"), list):
        return list(DEFAULT_RUN_FIELDS)
    found: dict[str, RunField] = {}
    for f in section["fields"]:
        if isinstance(f, dict) and isinstance(f.get("name"), str) and f["name"]:
            when = f.get("when") if isinstance(f.get("when"), str) else ""
            asked = f.get("asked") is not False
            found.setdefault(
                f["name"], run_field(f["name"], f.get("required") is True, asked, when)
            )
    fixed = [found.pop(name, run_field(name, False, False)) for name in FIXED]
    return [*fixed, *found.values()]


def write_run_fields(config_dir: Path, fields: Iterable[tuple]) -> None:
    """Set the project's run fields (:func:`read_run_fields`); each ``(name, required, asked)``
    or a :class:`RunField`, its when settled by :func:`run_field`."""
    write_config_section(
        config_dir,
        RUN_FIELDS,
        {"fields": [run_field(*f)._asdict() for f in fields]},
    )


#: How the project's runs keep their pictures and files (``config.json`` -> ``results``):
#: ``run_record.SEPARATE`` (the default) or ``run_record.SINGLE``.
RESULTS_SECTION = "results"


def read_attachments(config_dir: Path) -> str:
    """The project's ``results.attachments``: "single", else "separate" (the default)."""
    found = read_config_section(config_dir, RESULTS_SECTION).get("attachments")
    return run_record.SINGLE if found == run_record.SINGLE else run_record.SEPARATE


def write_attachments(config_dir: Path, mode: str) -> None:
    """Set the project's ``results.attachments`` (:func:`read_attachments`)."""
    write_config_section(config_dir, RESULTS_SECTION, {"attachments": mode})


def preserve_section_into_shadow(
    real_config_dir: Path, shadow_config_dir: Path, section: str
) -> None:
    """Overlay the live ``config.json:<section>`` onto a shadow copy.

    Used by ``ProjectConfigDialog._commit_shadow`` to honor the
    single-writer contract for sections (``board_view``, the board's
    angle) that have no editor in the dialog but may have been written through a
    different code path while the dialog was open. If the live file or
    the shadow file is missing/malformed the call is a silent no-op —
    the caller proceeds with whatever the shadow contains.
    """
    live = real_config_dir / UNIFIED_CONFIG
    shadow = shadow_config_dir / UNIFIED_CONFIG
    if not live.exists() or not shadow.exists():
        return
    try:
        live_data = json.loads(live.read_text(encoding="utf-8"))
        shadow_data = json.loads(shadow.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("preserve_section_into_shadow(%s) read failed: %s", section, exc)
        return
    if not isinstance(live_data, dict) or not isinstance(shadow_data, dict):
        return
    live_value = live_data.get(section)
    if live_value is None:
        shadow_data.pop(section, None)
    else:
        shadow_data[section] = live_value
    shadow.write_text(
        json.dumps(shadow_data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def maybe_migrate(config_dir: Path, file_map: dict[str, str]) -> None:
    """Merge old separate JSON files into unified ``config.json`` if present.

    After a successful merge the legacy files are deleted.  The function is
    idempotent – if ``config.json`` already exists it only cleans up leftovers.
    """
    if not config_dir.exists():
        return

    unified_path = config_dir / UNIFIED_CONFIG
    if unified_path.exists():
        # Already migrated – remove any leftover legacy files.
        for old_file in file_map:
            (config_dir / old_file).unlink(missing_ok=True)
        return

    has_any = any((config_dir / f).exists() for f in file_map)
    if not has_any:
        return

    data: dict[str, Any] = {}
    for old_file, section in file_map.items():
        old_path = config_dir / old_file
        if not old_path.exists():
            continue
        try:
            content = json.loads(old_path.read_text(encoding="utf-8"))
            # Unwrap equipment_patterns.json → store inner "patterns" dict directly.
            if old_file == "equipment_patterns.json" and "patterns" in content:
                content = content["patterns"]
            data[section] = content
            old_path.unlink()
            logger.info("Migrated %s → config.json [%s]", old_path, section)
        except Exception as e:
            logger.error("Failed to migrate %s: %s", old_path, e)

    if data:
        write_unified_config(config_dir, data)
        logger.info("Created unified config.json at %s", config_dir)
