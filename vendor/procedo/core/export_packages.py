"""Export packages — per-folder export templates, discovered by scanning.

An **export package** is a folder holding a ``schema.json`` manifest (the
report-package schema :mod:`report_schema` validates — ``package_id`` /
``package_version`` / ``template`` / ``fields``, plus the optional ``title``
and ``kind`` keys) beside the template file it names. Two tiers are scanned:
the project's own (``<project>/config/templates/exports/<folder>/``, source
``"project"``), then every switched-on package, through :mod:`package_store`
(its ``exports/`` and ``reports/`` folders; docx/markdown and html), whose
``source`` is the package's name. Nothing is deduplicated: two packages that
offer the same ``package_id`` are both listed, each labelled with its package.
A lookup by id takes the first in discovery order — the project's own, then the
packages in :mod:`package_store` order — which is how a project deliberately
overrides a shipped template.

``html`` report packages (the results-report template) use the same manifest
and discovery with a second project-tier root, ``config/templates/reports/``
(:func:`project_reports_dir`), and their own default slot.

A package's identity in a library is ``package_id@package_version`` plus the
content hash of its folder (:mod:`library_entry`), which is what lets the copy
on this PC be compared with the one on the server without trusting either name.

A package may also ship an optional ``extract.py`` beside its template — a
best-effort inverse of the template for the Word dialog's "Import from
document" (see :func:`load_metadata_extractor`).

The project's default package ids are stored under
``origin.default_export_package`` / ``origin.default_report_package`` in
``config/config.json``; :func:`get_default_export_package` and
:func:`get_default_report_package` resolve them with fallbacks so callers
always get a usable package when any is discoverable.
"""

from __future__ import annotations

import importlib.util
import logging
from dataclasses import dataclass
from pathlib import Path

from procedo.core import config_manager, library_entry, package_store, report_schema

log = logging.getLogger(__name__)

_DEFAULT_KEY = "default_export_package"
_REPORT_DEFAULT_KEY = "default_report_package"
BUILTIN_WORD_PACKAGE_ID = "builtin_word_export"
BUILTIN_HTML_PACKAGE_ID = "builtin_html_report"


@dataclass(frozen=True)
class ExportPackage:
    package_id: str
    version: str
    title: str
    kind: str  # "docx" | "markdown" | "html"
    root: Path
    template_path: Path  # root / schema["template"], verified is_file()
    schema: dict
    source: str  # "project", or the name of the package it comes from
    #: The content hash the manifest DECLARES — stamped when the package was
    #: published to a server library, ``""`` for one that never travelled. What
    #: the folder holds right now is ``library_entry.content_sha256``, which is
    #: what any comparison uses: a package edited in place would otherwise be
    #: mistaken for the copy that was published.
    declared_sha256: str = ""


# Subdir names, shared with install_helper (single source: the two roots form
# ONE identity namespace, split only by render kind).
LOCAL_EXPORTS_SUBDIR = "exports"
LOCAL_REPORTS_SUBDIR = "reports"

#: The render kinds (``schema.json``'s ``kind``) each template library kind holds.
RENDER_KINDS: dict[str, tuple[str, ...]] = {
    library_entry.EXPORT: ("docx", "markdown"),
    library_entry.REPORT: ("html",),
}


def local_exports_dir() -> Path:
    """The Local package's docx/markdown root: ``packages/local/exports/`` holding
    ``<package_id>/<version>/`` folders (the server library downloads into it)."""
    return package_store.local_root() / LOCAL_EXPORTS_SUBDIR


def local_reports_dir() -> Path:
    """The Local package's html root: ``packages/local/reports/``."""
    return package_store.local_root() / LOCAL_REPORTS_SUBDIR


def project_exports_dir(project_path: Path) -> Path:
    """Project-tier package roots: ``<project>/config/templates/exports/``."""
    return Path(project_path) / "config" / "templates" / "exports"


def project_reports_dir(project_path: Path) -> Path:
    """Project-tier roots for ``html`` report packages:
    ``<project>/config/templates/reports/``."""
    return Path(project_path) / "config" / "templates" / "reports"


def load_export_package(folder: Path, source: str) -> ExportPackage:
    """Load one package folder, raising on anything malformed or incomplete.

    Raises ``OSError`` when ``schema.json`` is unreadable,
    :class:`report_schema.SchemaError` (a ``ValueError``) on an invalid
    manifest, and ``ValueError`` when the manifest's ``template`` resolves
    outside *folder* (absolute path or ``..``) or the template file is
    missing. Discovery catches these and skips (never raises); the Templates
    tab surfaces them to the user before copying a package in."""
    folder = Path(folder)
    schema = report_schema.load_schema(folder / "schema.json")
    template_path = folder / schema["template"]
    if folder.resolve() not in template_path.resolve().parents:
        raise ValueError(f"template {schema['template']!r} resolves outside the package folder")
    if not template_path.is_file():
        raise ValueError(f"template file {schema['template']!r} missing from the package")
    return ExportPackage(
        package_id=schema["package_id"],
        version=schema["package_version"],
        title=schema.get("title") or schema["package_id"],
        kind=schema.get("kind") or "docx",
        root=folder,
        template_path=template_path,
        schema=schema,
        source=source,
        declared_sha256=library_entry.declared_sha256(library_entry.EXPORT, schema),
    )


EXTRACTOR_FILENAME = "extract.py"


def load_metadata_extractor(pkg: ExportPackage):
    """The package's optional document-metadata importer, or ``None``.

    A package may ship an ``extract.py`` beside its template defining
    ``extract_metadata(docx_path, schema) -> (values, warnings)``: a
    best-effort inverse of its template, where *values* is a partial sidecar
    dict of the fields recovered from the rendered (possibly hand-edited)
    document and *warnings* are human-readable notes on anything ambiguous.
    Raises ``ValueError`` when the file exists but cannot be loaded or does
    not define the function."""
    path = pkg.root / EXTRACTOR_FILENAME
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location(f"export_extract_{pkg.package_id}", path)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise ValueError(f"{path} failed to load: {exc}") from exc
    extract = getattr(module, "extract_metadata", None)
    if not callable(extract):
        raise ValueError(f"{path} defines no extract_metadata() function")
    return extract


def _project_folders(base: Path | None) -> list[Path]:
    """Each ``<base>/<folder>/schema.json`` is a project package; an unreadable
    root is logged and contributes nothing."""
    if base is None or not Path(base).is_dir():
        return []
    try:
        return [f for f in sorted(Path(base).iterdir()) if (f / "schema.json").is_file()]
    except OSError as exc:
        log.warning("skipping unreadable package tier %s: %s", base, exc)
        return []


def _in_discovery_order(project_path: Path | None, project_dir: Path | None) -> list[ExportPackage]:
    """Every template, in the order a lookup by id resolves: the project's own,
    then the switched-on packages' exports and reports in :mod:`package_store`
    order. A folder that fails to load is logged and skipped."""
    if project_dir is None and project_path is not None:
        project_dir = project_exports_dir(project_path)
    folders = [(f, "project") for f in _project_folders(project_dir)]
    for kind in ("exports", "reports"):
        folders += [(i.folder, i.package.name) for i in package_store.items(kind)]
    found = []
    for folder, source in folders:
        try:
            found.append(load_export_package(folder, source))
        except (OSError, ValueError, AttributeError) as exc:
            log.warning("skipping export package %s: %s", folder, exc)
    return found


def discover_export_packages(
    project_path: Path | None, *, project_dir: Path | None = None
) -> list[ExportPackage]:
    """Every template visible to *project_path*, sorted by (title, id, source).

    The project's own folders, then every item of the switched-on packages; none
    is hidden by another (see the module docstring). Discovery never raises.
    ``project_dir`` overrides the project root — for tests and the Templates tab."""
    return sorted(
        _in_discovery_order(project_path, project_dir),
        key=lambda p: (p.title, p.package_id, p.source),
    )


def find_export_package(
    project_path: Path | None, package_id: str, *, project_dir: Path | None = None
) -> ExportPackage | None:
    """The first template with *package_id* in discovery order, or None."""
    packages = _in_discovery_order(project_path, project_dir)
    return next((p for p in packages if p.package_id == package_id), None)


def _default_package(
    project_path: Path | None,
    *,
    key: str,
    builtin_id: str,
    kind: str | None,
    project_dir: Path | None,
) -> ExportPackage | None:
    """The stored-default -> named-builtin -> first-discovered chain shared by
    :func:`get_default_export_package` and :func:`get_default_report_package`.

    *kind* filters the WHOLE chain, stored default included — a stored id
    whose package fails the filter (or is not discoverable at all) is skipped
    with a debug log and the chain continues."""
    packages = _in_discovery_order(project_path, project_dir)
    if kind is not None:
        packages = [p for p in packages if p.kind == kind]
    by_id: dict[str, ExportPackage] = {}
    for p in packages:
        by_id.setdefault(p.package_id, p)
    stored = _get_default_id(project_path, key) if project_path is not None else None
    if stored is not None:
        if stored in by_id:
            return by_id[stored]
        log.debug("stored %s %r not a usable %s package — skipped", key, stored, kind or "any")
    if builtin_id in by_id:
        return by_id[builtin_id]
    return min(packages, key=lambda p: (p.title, p.package_id, p.source), default=None)


def get_default_export_package(
    project_path: Path | None, kind: str | None = None, *, project_dir: Path | None = None
) -> ExportPackage | None:
    """The effective default package: the stored project default, else the
    built-in Word package, else the first discovered package, else None.

    *kind* filters the WHOLE chain, stored default included — a stored
    markdown default never satisfies a docx-only caller; it is skipped and
    the chain continues."""
    return _default_package(
        project_path,
        key=_DEFAULT_KEY,
        builtin_id=BUILTIN_WORD_PACKAGE_ID,
        kind=kind,
        project_dir=project_dir,
    )


def get_default_report_package(project_path: Path | None) -> ExportPackage | None:
    """The effective default ``html`` report package (results-report render):
    the stored ``origin.default_report_package``, else the built-in HTML
    package, else the first discovered html package, else None. Project-tier
    html packages live under :func:`project_reports_dir`; with *project_path*
    None (preview / single-test paths) the builtin resolves."""
    return _default_package(
        project_path,
        key=_REPORT_DEFAULT_KEY,
        builtin_id=BUILTIN_HTML_PACKAGE_ID,
        kind="html",
        project_dir=project_reports_dir(project_path) if project_path is not None else None,
    )


def _get_default_id(project_path: Path, key: str) -> str | None:
    section = config_manager.read_config_section(Path(project_path) / "config", "origin")
    return section.get(key)


def _set_default_id(project_path: Path, key: str, package_id: str | None) -> None:
    config_dir = Path(project_path) / "config"
    section = config_manager.read_config_section(config_dir, "origin")
    if package_id:
        section[key] = package_id
    else:
        section.pop(key, None)
    config_manager.write_config_section(config_dir, "origin", section)


def get_default_export_package_id(project_path: Path) -> str | None:
    """The stored default package id (``origin.default_export_package``), or None."""
    return _get_default_id(project_path, _DEFAULT_KEY)


def set_default_export_package_id(project_path: Path, package_id: str | None) -> None:
    """Persist the project's default package id; None removes the key."""
    _set_default_id(project_path, _DEFAULT_KEY, package_id)


def get_default_report_package_id(project_path: Path) -> str | None:
    """The stored default report package id (``origin.default_report_package``), or None."""
    return _get_default_id(project_path, _REPORT_DEFAULT_KEY)


def set_default_report_package_id(project_path: Path, package_id: str | None) -> None:
    """Persist the project's default html-report package id; None removes the key."""
    _set_default_id(project_path, _REPORT_DEFAULT_KEY, package_id)
