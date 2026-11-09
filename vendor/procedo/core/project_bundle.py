"""A project's own bundle: where it lives (``<project>/bundle/``), how to read it, and how to copy a
library bundle into it and make sure this machine has the venv it asks for."""

from __future__ import annotations

from pathlib import Path

from . import bundle_registry, paths, venv_key

#: A project runs only in its bundle's environment: without ``bundle/`` it is refused (Run,
#: the setup at open, the runner itself), never run with the app's own Python.
BUNDLE_MISSING = (
    "This project's bundle/ folder is missing: restore it, or apply the project's template again."
)


def project_bundle_path(project_path: Path) -> Path:
    """The bundle directory inside a project: ``<project>/bundle/``. The build output lives in
    the repo-local bundle store (:func:`bundle_registry.bundle_store_path`) and is copied here
    by :func:`import_bundle_to_project`."""
    return project_path / "bundle"


def defaults_path(project_path: Path) -> Path:
    """Where a project's bundle keeps its ``defaults.json``."""
    return project_bundle_path(project_path) / "defaults.json"


def read_active_bundle(project_path: Path) -> bundle_registry.Bundle | None:
    """The project's bundle at ``<project>/bundle/``, or None when absent or malformed.
    Bundles are per-project: there is no library fallback."""
    return read_bundle_at(project_bundle_path(project_path))


def read_bundle_at(root: Path) -> bundle_registry.Bundle | None:
    """The bundle in folder *root* (a project's ``bundle/``), or None when absent or malformed."""
    if not root.is_dir():
        return None
    return bundle_registry.load_bundle(root)


def active_rules_dir(project_path: Path) -> Path | None:
    """The rules folder of the project's active bundle, or None when the project
    has no loadable bundle or its bundle has no ``rules/``. Studio's rules for
    every project it opens."""
    bundle = read_active_bundle(project_path)
    return bundle.rules_dir if bundle is not None and bundle.rules_dir.is_dir() else None


def import_bundle_to_project(
    project_path: Path,
    source: bundle_registry.Bundle,
) -> bundle_registry.Bundle:
    """Copy a library bundle into ``<project>/bundle/`` via an atomic
    staging swap, then make sure this machine has the venv that bundle
    asks for. If a bundle already lives in the project, it's replaced.
    Returns the freshly-loaded project bundle.

    Staging dir is created as a sibling of the target so the final
    rename is on the same filesystem (atomicity guarantee from
    :func:`paths.swap_into_place`). On any failure, partial staging is
    cleaned up and the previous bundle (if any) is restored.

    Resolving the venv is integral to import: a project with copied
    bundle files and no environment holding their wheels is broken and
    silent about it. The environment is keyed by bundle content, so this
    is free when another project on this machine already uses the same
    bundle. Raises :class:`bundle_installer.InstallError` if the build
    fails.
    """
    import shutil
    import uuid

    target = project_bundle_path(project_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.parent / f".bundle-staging-{uuid.uuid4().hex[:8]}"
    paths.replace_with(target, staging, lambda staged: shutil.copytree(source.root, staged))
    bundle = bundle_registry.load_bundle(target)
    if bundle is None:
        raise RuntimeError(
            f"Imported bundle at {target} failed to load post-swap. Source bundle may be malformed."
        )
    # Project's bundle state changed — any cached extractor results
    # keyed on this project are now stale, and so is the venv this
    # process remembers for it: the venv follows the BUNDLE, and a
    # rebuild at the same version under the same name in the same second
    # is exactly what a stat fingerprint cannot see.
    from . import pack_extractors, project_local_state

    pack_extractors.invalidate_caches()
    project_local_state.forget_resolved(project_path)
    # Integrity gate before touching the venv: every wheel named in the
    # bundle's requirements.txt must exist in the copied bundle. A
    # missing wheel would otherwise make pip fall back to PyPI for that
    # name (requirements deliberately lacks --no-index) or fail after
    # the venv is already half-modified — both silent-ish. Seen in the
    # field: a bundle shipped without rules_packager_base while
    # requirements still listed it, leaving a stale wheel installed.
    missing = venv_key.missing_wheels(target)
    if missing:
        raise RuntimeError(
            "Bundle integrity: requirements.txt lists wheel(s) missing "
            f"from the bundle: {', '.join(missing)}. Rebuild the bundle "
            "(the build now verifies this) before importing."
        )

    # Install wheels into the project venv. Lazy import avoids a
    # circular bundle_registry <-> bundle_installer dependency.
    from . import bundle_installer

    bundle_installer.install_bundle_into_project(project_path, bundle)
    return bundle
