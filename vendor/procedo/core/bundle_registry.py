"""Bundle library reader (Phase 1).

A *bundle* is an immutable, pregenerated artifact built by
``external/rules_packager/tools/generate_all.py --bundle-out``. It
collapses today's per-project ``selected_packs`` + wheel-build + rule-
collection dance into a single resolvable id+version.

Layout on disk::

    <library>/<bundle_id>/<bundle_version>/
        bundle.json     # manifest (id, version, packs, sha256)
        defaults.json   # pre-merged parsers + extractors + workflows
        rules/          # collected .md docs + manifest.json
        wheels/         # built wheels + requirements.txt

Projects opt in by adding a ``bundle`` key to their ``config.json``::

    {"bundle": {"id": "baseline", "version": "1.0.0"}}

When the key is absent the GUI falls back to today's manifest-walk
through ``drivers_registry.json`` (Phase 1 keeps both paths working;
later phases delete the fallback).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import app_settings, library_entry, paths, venv_key

logger = logging.getLogger(__name__)

# Required files at the bundle root. A directory missing any of these
# is not a bundle and is skipped silently by ``enumerate_bundles``.
_REQUIRED_FILES = ("bundle.json", "defaults.json")


class BundleCollisionError(FileExistsError):
    """Raised on import when an ``<id>@<version>`` is already
    installed AND the incoming bundle has a different content hash.

    Distinct from a plain ``FileExistsError`` (re-import of the SAME
    bundle, which is now silent) so callers can present a clearer
    message: the user has TWO bundles claiming the same name, and
    using ``overwrite=True`` would clobber the installed one.

    Subclasses ``FileExistsError`` so older callers that catch the
    parent class keep working unchanged.
    """


def bundle_store_path() -> Path:
    """Return the bundle store directory inside the repo.

    Build Bundle writes its output to
    ``<repo_root>/bundle/<bundle_id>/<bundle_version>/``. The store is
    versioned (many ``<id>@<version>`` can coexist) and gitignored.
    Projects pull bundles out of the store via the Apply / Import
    actions, which copy into ``<project>/bundle/``.

    ``<repo_root>`` is the dir that holds ``src/``, resolved from
    ``Path(__file__).parents[3]``. Created on first access.
    """
    path = Path(__file__).resolve().parents[3] / "bundle"
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass(frozen=True)
class Bundle:
    """A resolved bundle on disk.

    Immutable from the GUI's perspective. Carry the parsed manifest
    + lazy paths so callers don't re-read the JSON. ``defaults`` is
    eagerly loaded since every extractor lookup needs it.
    """

    id: str
    version: str
    root: Path
    manifest: dict[str, Any]
    defaults: dict[str, Any]

    @property
    def rules_dir(self) -> Path:
        return self.root / "rules"

    @property
    def bundle_sha256(self) -> str:
        v = self.manifest.get("bundle_sha256")
        return v if isinstance(v, str) else ""

    @property
    def packs(self) -> list[dict[str, Any]]:
        v = self.manifest.get("packs")
        return v if isinstance(v, list) else []

    @property
    def packages(self) -> list[dict[str, Any]]:
        """The packages the bundle was built from (``[]`` when it records none)."""
        v = self.manifest.get("packages")
        return v if isinstance(v, list) else []

    @property
    def built_from(self) -> str:
        """The packages as one line: ``Procedo, labscpi 2.2.0 (3f2a9c1)`` (name, version
        when set, commit when set); ``""`` when none are recorded."""
        parts = []
        for p in self.packages:
            if not isinstance(p, dict):
                continue
            text = " ".join(str(x) for x in (p.get("name") or p.get("id"), p.get("version")) if x)
            if p.get("commit"):
                text += f" ({p['commit']})"
            if text:
                parts.append(text)
        return ", ".join(parts)

    def verify_integrity(self) -> bool:
        """Recompute the bundle's sha256 and compare to the stored
        value. Returns False when the manifest carries no sha or any
        tracked file has changed since build. Skipped during cheap
        enumeration; UI calls this before installing into a venv."""
        stored = self.bundle_sha256
        if not stored:
            return False
        return _compute_bundle_sha256(self.root) == stored


def _compute_bundle_sha256(bundle_root: Path) -> str:
    """The bundle's content hash — ``library_entry``'s tree digest, which is
    this rule lifted so every library kind shares one implementation.

    It must stay a mirror of ``generate_all._compute_bundle_sha256``, otherwise
    the GUI reports bundles as corrupt that the builder considered valid."""
    return library_entry.content_sha256(library_entry.BUNDLE, bundle_root)


def _safe_read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.debug("No %s", path)
        return None
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not read %s: %s", path, exc)
        return None


def read_defaults(path: Path | None) -> dict[str, Any] | None:
    """The bundle ``defaults.json`` at ``path`` (the one reader of it), or None when
    there is no path or file, it cannot be read, or it is not a JSON object."""
    if path is None:
        return None
    data = _safe_read_json(path)
    return data if isinstance(data, dict) else None


def bench_fields(defaults_file: Path | None) -> dict[str, list]:
    """The per-equipment-type config fields ``{etype: [{name, type, label, default,
    required?, choices?}]}`` the bundle at ``defaults_file`` declares
    (``pack_dispatch.bench_fields``, from the packs' rules_index.json): the single source
    the equipment-config editor renders from and codegen derives its defaults from.
    Empty when there is no bundle or it predates bench_fields."""
    dispatch = (read_defaults(defaults_file) or {}).get("pack_dispatch")
    bf = dispatch.get("bench_fields") if isinstance(dispatch, dict) else None
    return bf if isinstance(bf, dict) else {}


def bench_constant_names(etype: str, eid: str, defaults_file: Path | None) -> list[str]:
    """The generated-code constant names of one equipment instance, e.g.
    ``["PSU1_VISA", "PSU1_REMOTE", ...]``: ``<sanitized-upper eid>_<FIELD.upper()>`` per
    field :func:`bench_fields` declares for ``etype``, in that order. Empty when it
    declares none."""
    prefix = re.sub(r"\W", "_", str(eid)).upper()
    return [
        f"{prefix}_{f['name'].upper()}"
        for f in bench_fields(defaults_file).get(etype, [])
        if isinstance(f, dict) and isinstance(f.get("name"), str)
    ]


def op_namespace(etype: str, subtype: str, defaults_file: Path | None) -> str | None:
    """The pack op namespace that serves a DECLARED equipment type and subtype in the bundle
    at ``defaults_file`` (``""`` is a single-variant type's subtype): its
    ``pack_dispatch.equipment_dispatch``, the map the engine's registry loads and resolves
    with ``get_namespace``. A pack's lifecycle (connect factory, cleanup) is keyed by it,
    and it need not be the type. None when no pack in the bundle claims the pair."""
    dispatch = (read_defaults(defaults_file) or {}).get("pack_dispatch")
    by_type = dispatch.get("equipment_dispatch") if isinstance(dispatch, dict) else None
    subtypes = by_type.get(etype) if isinstance(by_type, dict) else None
    namespace = subtypes.get(subtype) if isinstance(subtypes, dict) else None
    return namespace if isinstance(namespace, str) and namespace else None


def htf_plugs(defaults_file: Path | None) -> dict[str, dict]:
    """The OpenHTF plug a code test imports for each equipment type, ``{etype: {"module",
    "plug"}}`` (``{"psu": {"module": "labscpi_pack.htf", "plug": "Psu"}}``), as the bundle at
    ``defaults_file`` declares them (``pack_dispatch.htf_plugs``, from the packs'
    rules_index.json), in its packs' order. Empty when it declares none."""
    dispatch = (read_defaults(defaults_file) or {}).get("pack_dispatch")
    plugs = dispatch.get("htf_plugs") if isinstance(dispatch, dict) else None
    return plugs if isinstance(plugs, dict) else {}


def load_bundle(bundle_root: Path) -> Bundle | None:
    """Load a single bundle dir into a Bundle, or None when malformed.

    Strict on required files (bundle.json + defaults.json) because a
    half-built directory shouldn't be silently usable. Returns None
    rather than raising so ``enumerate_bundles`` can skip-and-log
    instead of crashing the dialog.
    """
    for name in _REQUIRED_FILES:
        if not (bundle_root / name).exists():
            logger.info("Skipping bundle dir %s — missing %s", bundle_root, name)
            return None
    manifest = _safe_read_json(bundle_root / "bundle.json")
    if not isinstance(manifest, dict):
        return None
    defaults = read_defaults(bundle_root / "defaults.json")
    if defaults is None:
        return None
    bid = manifest.get("id")
    bver = manifest.get("version")
    if not isinstance(bid, str) or not isinstance(bver, str):
        logger.info("Skipping bundle %s — manifest missing id/version", bundle_root)
        return None
    return Bundle(
        id=bid,
        version=bver,
        root=bundle_root,
        manifest=manifest,
        defaults=defaults,
    )


def enumerate_bundles(library_path: Path | None = None) -> list[Bundle]:
    """Enumerate every bundle in the library.

    Layout assumption: ``<library>/<id>/<version>/``. Anything that
    doesn't look like a bundle dir is skipped (so the user can drop
    other folders in the library without breaking discovery).

    Order: by ``(id, version)`` ascending. The new-project dialog
    typically picks the newest by version per id.
    """
    if library_path is None:
        library_path = bundle_store_path()
    if not library_path.is_dir():
        return []
    out: list[Bundle] = []
    for id_dir in sorted(library_path.iterdir()):
        if not id_dir.is_dir():
            continue
        # Skip the import pipeline's internal staging / backup dirs.
        # A crash mid-import of a wrapped zip can leave .staging-<uuid>/
        # holding a real bundle layout; without this filter that
        # orphan shows up as a ghost row.
        if id_dir.name.startswith("."):
            continue
        for ver_dir in sorted(id_dir.iterdir(), key=lambda p: library_entry.version_key(p.name)):
            # A swap backup (``.<version>.old-<uuid>``) holds a whole bundle too.
            if not ver_dir.is_dir() or ver_dir.name.startswith("."):
                continue
            b = load_bundle(ver_dir)
            if b is not None:
                out.append(b)
    return out


def latest_bundle(bundles: list[Bundle]) -> Bundle | None:
    """The newest bundle by version (semver-aware), tie-broken by id — the
    default selection for any bundle picker. ``None`` for an empty list."""
    if not bundles:
        return None
    return max(bundles, key=lambda b: (library_entry.version_key(b.version), b.id))


def find_bundle(
    bundle_id: str,
    bundle_version: str,
    library_path: Path | None = None,
) -> Bundle | None:
    """Look up a specific bundle by id+version.

    Returns None for any unsafe id/version (path traversal, leading
    dot, illegal characters) so callers don't accidentally resolve
    operator-supplied refs to a parent directory.
    """
    if not is_safe_component(bundle_id) or not is_safe_component(bundle_version):
        return None
    if library_path is None:
        library_path = bundle_store_path()
    bundle_root = library_path / bundle_id / bundle_version
    if not bundle_root.is_dir():
        return None
    return load_bundle(bundle_root)


def install_bundle_from_dir(
    source_root: Path,
    *,
    overwrite: bool = False,
    library_path: Path | None = None,
) -> Bundle:
    """Install a bundle from an already-extracted directory.

    ``source_root`` points at the bundle's content root (the dir
    containing ``bundle.json`` + ``defaults.json``). It can be
    anywhere on disk — the helper copies the content into the
    library filesystem before the atomic swap.

    Validation, sha-integrity, and collision-detection mirror
    :func:`bundle_archive.import_bundle_from_zip`; that function now delegates here
    after the zip extraction step. The third caller is the template
    importer in :mod:`config_templates`, which extracts a template zip
    to a tmp dir and finds a bundled ``__bundle__/<id>/<version>/``
    inside.

    Raises:
        ValueError: missing/malformed manifest, integrity mismatch,
            unsafe id/version, missing required files.
        BundleCollisionError: same id+version installed with a
            different content hash; ``overwrite=False``.
        FileExistsError: same id+version installed but one side has
            no sha to compare; ``overwrite=False``.
    """
    import os
    import shutil
    import uuid

    if library_path is None:
        library_path = bundle_store_path()
    library_path.mkdir(parents=True, exist_ok=True)

    manifest = _safe_read_json(source_root / "bundle.json")
    if not isinstance(manifest, dict):
        raise ValueError("bundle.json is not a JSON object")
    bid = manifest.get("id")
    bver = manifest.get("version")
    if not isinstance(bid, str) or not isinstance(bver, str):
        raise ValueError("bundle.json missing string id/version")
    check_safe_component(bid, "id")
    check_safe_component(bver, "version")
    for name in _REQUIRED_FILES:
        if not (source_root / name).exists():
            raise ValueError(f"Bundle missing required file: {name}")
    stored_sha = manifest.get("bundle_sha256")
    if isinstance(stored_sha, str) and stored_sha:
        computed = _compute_bundle_sha256(source_root)
        if computed != stored_sha:
            raise ValueError(
                f"Bundle integrity check failed "
                f"(stored {stored_sha[:12]}…, computed {computed[:12]}…)"
            )

    target = library_path / bid / bver
    if target.exists() and not overwrite:
        # See import_bundle_from_zip: same / different / missing-sha
        # cases produce different errors so the user knows whether to
        # bump the version or just retry.
        installed_sha = ""
        installed_manifest = _safe_read_json(target / "bundle.json")
        if isinstance(installed_manifest, dict):
            v = installed_manifest.get("bundle_sha256")
            if isinstance(v, str):
                installed_sha = v
        incoming_sha = stored_sha if isinstance(stored_sha, str) else ""
        if installed_sha and incoming_sha:
            if installed_sha == incoming_sha:
                bundle = load_bundle(target)
                if bundle is None:  # pragma: no cover
                    raise RuntimeError(f"Existing bundle at {target} failed to load")
                return bundle
            raise BundleCollisionError(
                f"Bundle {bid}@{bver} is already installed at "
                f"{target} with a DIFFERENT content hash "
                f"(installed {installed_sha[:12]}…, incoming "
                f"{incoming_sha[:12]}…). Bump the version or "
                f"delete the existing bundle before importing."
            )
        raise FileExistsError(f"Bundle {bid}@{bver} already installed at {target}")

    # Stage on library FS for atomic swap. Copy from source_root to
    # the staging dir; if source_root is already a library staging
    # tree (the import_bundle_from_zip path), the copy could be
    # avoided — but the second copy is cheap relative to wheels and
    # keeps this function robust regardless of caller.
    staging = library_path / f".staging-{uuid.uuid4().hex[:8]}"
    os.makedirs(staging, exist_ok=False)

    def fill(staged: Path) -> None:
        for child in source_root.iterdir():
            dst = staged / child.name
            if child.is_dir():
                shutil.copytree(child, dst)
            else:
                shutil.copy2(child, dst)
        target.parent.mkdir(parents=True, exist_ok=True)

    paths.replace_with(target, staging, fill)

    invalidate_caches()
    bundle = load_bundle(target)
    if bundle is None:  # pragma: no cover
        raise RuntimeError(f"Imported bundle at {target} failed to load")
    return bundle


# Characters allowed in manifest id/version. Path separators, NUL,
# and control bytes would either punch out of the library tree or
# confuse downstream tools. Keep the alphabet conservative; users
# pick these names by hand.
SAFE_COMPONENT_RE = re.compile(r"[A-Za-z0-9._-]+")

# 64 chars is generous for a bundle id/version (semantic versions
# rarely exceed 32; bundle ids are short by convention) and well
# under any FS path limit, leaving room for the
# ``<library>/<id>/<version>/`` triple plus subdirs.
_MAX_COMPONENT_LEN = 64


def check_safe_component(value: str, field: str) -> None:
    """Reject id/version values that aren't safe filesystem components.

    Public because both the import path (manifest-derived) and the
    Bundle Generator path (user-typed) need the exact same constraint.
    Blocks path separators, ``..``/``.``, leading-dot (collides with
    the import pipeline's ``.staging-*`` dirs and the swap's
    ``.<version>.old-*`` backups),
    and any char outside ``[A-Za-z0-9._-]`` (NUL, control bytes,
    Windows-reserved punctuation).

    The bundle's library path is ``<library>/<id>/<version>/``; an
    unsafe component would either punch out (``../../etc``) or land
    on a Windows reserved name that the FS rejects.
    """
    if not is_safe_component(value):
        raise ValueError(f"Invalid bundle {field}: {value!r}")


def is_safe_component(value: object) -> bool:
    """Non-raising variant of :func:`check_safe_component`.

    Returns False for non-strings, empty strings, ``.``/``..``,
    leading-dot, values outside ``[A-Za-z0-9._-]``, values longer
    than 64 chars, or Windows reserved device names (CON, NUL,
    COM1..9, LPT1..9 — case-insensitive, matched by stem so
    ``CON.json`` is also blocked). Used by classify + find_bundle
    to fail safely on operator-supplied refs without forcing every
    caller to try/except.
    """
    if not isinstance(value, str):
        return False
    if (
        not value
        or value in ("..", ".")
        or value.startswith(".")
        or not SAFE_COMPONENT_RE.fullmatch(value)
        or len(value) > _MAX_COMPONENT_LEN
    ):
        return False
    # Windows reserved device names, matched by stem so ``CON.json``,
    # ``con.tar.gz`` etc. are all blocked.
    return not paths.is_windows_reserved(value, strict=True)


def delete_bundle(bundle: Bundle) -> None:
    """Remove a bundle from the library, given the already-resolved
    Bundle object.

    Operates on ``bundle.root`` directly — never reconstructs the
    path from manifest-derived ``id`` / ``version`` strings, which
    a hostile bundle could spoof to traverse outside the library.
    Empty parent id-dir is cleaned up afterwards so the library
    stays tidy.

    When the deleted bundle is the recorded self-test default, the
    setting is cleared so the Library UI / Self-Test window don't
    keep showing a stale ref.

    Raises ``OSError`` on file-system errors (permission denied,
    files locked on Windows, etc.) — callers display these to the
    user. Caches are invalidated on success.
    """
    import shutil

    if not bundle.root.is_dir():
        raise FileNotFoundError(f"Bundle not installed at {bundle.root}")
    shutil.rmtree(bundle.root)
    # Drop the now-empty id dir too so the library stays tidy. Ignore
    # cleanup failures — they're harmless and rare.
    parent = bundle.root.parent
    try:
        if parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
    except OSError as exc:
        # Best-effort tidy-up only — the bundle itself is already gone.
        logger.debug("Could not remove empty bundle id dir %s: %s", parent, exc)
    _clear_self_test_default_if_matches(bundle)
    invalidate_caches()


def _clear_self_test_default_if_matches(bundle: Bundle) -> None:
    """If the deleted bundle was the persisted self-test default,
    drop the setting. Otherwise the next UI render shows a stale
    "(self-test default)" tag against a now-missing bundle."""
    ref = get_self_test_default()
    if not ref:
        return
    if ref.get("id") == bundle.id and ref.get("version") == bundle.version:
        # Merge-safe and best-effort: re-reads another window's settings first, and a
        # preference that cannot be persisted still must not fail deleting a bundle.
        app_settings.update_app_settings(
            lambda settings: settings.pop("self_test_bundle", None), best_effort=True
        )


def find_labscpi_wheel(bundle: Bundle) -> Path | None:
    """Return the ``labscpi-*.whl`` the bundle's requirements.txt names and it holds, or
    None (a leftover labscpi wheel it does not name is not the bundle's).

    Bundles built by the rules_packager always ship a labscpi wheel
    when the labscpi pack is enabled in the registry; otherwise the
    bundle can't drive equipment self-test. Callers use this to gate
    the "Set as Self-Test Default" action.
    """
    return next(
        (
            w
            for w in venv_key.wheel_files(bundle.root)
            if w.name.startswith("labscpi-") and w.is_file()
        ),
        None,
    )


def get_self_test_default() -> dict[str, Any] | None:
    """Read the self-test default bundle ref from app settings.

    Returns ``{"id", "version", "labscpi_version"}`` or None when no
    default is configured. The Self-Test window uses this to render
    the "labscpi X.Y from bundle id@ver" stamp and to fall back to
    a sensible default when no project is open.
    """
    settings = app_settings.load_app_settings()
    ref = settings.get("self_test_bundle")
    return ref if isinstance(ref, dict) else None


def set_self_test_default(bundle: Bundle, labscpi_version: str) -> None:
    """Record ``bundle`` as the self-test default in app settings.

    Caller is responsible for actually installing labscpi from the
    bundle's wheels dir into the running interpreter (see
    ``bundle_installer.configure_self_test_from_bundle``). This
    function only updates the persisted preference.
    """
    app_settings.update_app_settings(
        lambda settings: settings.update(
            {
                "self_test_bundle": {
                    "id": bundle.id,
                    "version": bundle.version,
                    "labscpi_version": labscpi_version,
                }
            }
        ),
        best_effort=True,
    )


def invalidate_caches() -> None:
    """Notify downstream consumers that the bundle library may have
    changed (add / delete / overwrite). Three module-global caches
    hang off the bundle: the extractor-defaults aggregate in
    ``pack_extractors``, plus the bundle pattern/profile layers in
    ``equipment_patterns`` / ``equipment_profiles`` (loaded at their
    set_project_path). All three are refreshed here.

    UI call sites (Bundle Library dialog actions) MUST call this
    after mutating the library; otherwise stale extractor lookups
    point at deleted bundles.
    """
    # Late imports: equipment_patterns / equipment_profiles import
    # bundle_registry in their load paths — top-level would cycle.
    from . import equipment_patterns, equipment_profiles, pack_extractors

    pack_extractors.invalidate_caches()
    equipment_patterns.reload_bundle_layer()
    equipment_profiles.reload_bundle_layer()
