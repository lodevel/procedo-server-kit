"""Equipment Profiles — behavior records keyed by (equipment_type, subtype).

Every pack ships profiles describing equipment behavior the GUI must not
hardcode: session policy, remote transport/polarity, override-variable
suffix, defaults. Declared equipment resolves by EXACT (equipment_type,
subtype) key; name/alias/pattern matching survives only for groups whose
equipment type is unknown (undeclared).

Profile record shape::

    {equipment_type, subtype ("" for single-variant types), display_name,
     category, id_pattern (advisory), aliases, session_policy_default,
     remote: {transport, enable_field, inverted}, override_suffix,
     defaults, energizes_output, driver (optional extra)}
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from . import config_manager, get_config_dir

logger = logging.getLogger(__name__)

# Module-level cache for loaded profiles
#
# Layering, lowest → highest precedence:
#   1. ``_app_profiles``     — built-in defaults (app_config/config.json)
#   2. ``_bundle_profiles``  — defaults shipped with the active bundle
#                              (``bundle.defaults["equipment_profiles"]``).
#                              Read-only — bundles are immutable.
#   3. ``_project_profiles`` — per-project overrides (project config.json)
_app_profiles: list[dict[str, Any]] = []  # base (app_config/config.json → profiles)
_bundle_profiles: list[dict[str, Any]] = []  # active bundle's pre-merged pack defaults
_project_profiles: list[dict[str, Any]] = []  # per-project (additive on top)
_project_path: Path | None = None
# ``config.json:equipment_overrides`` of the project, read with its profiles (never per lookup).
_equipment_overrides: dict[str, Any] = {}

# One-shot guard so lookups don't retry a failing app-config load forever.
_app_load_attempted = False


def _profile_key(profile: dict[str, Any]) -> tuple[str, str]:
    """Return the (equipment_type, subtype) identity key of a profile."""
    return (profile.get("equipment_type") or "", profile.get("subtype") or "")


def validate_profile(profile: dict[str, Any]) -> list[str]:
    """Validate a profile record.

    Requires a non-empty ``equipment_type`` and, when present, a
    parseable ``id_pattern``. ``subtype`` may be ``""`` (single-variant
    types) but must be a string.

    Returns:
        List of error messages (empty if valid).
    """
    if not isinstance(profile, dict):
        return ["Profile must be a dictionary"]

    errors: list[str] = []

    etype = profile.get("equipment_type")
    if not isinstance(etype, str) or not etype.strip():
        errors.append("'equipment_type' is required and must be a non-empty string")

    subtype = profile.get("subtype", "")
    if not isinstance(subtype, str):
        errors.append("'subtype' must be a string (may be empty)")

    id_pattern = profile.get("id_pattern")
    if id_pattern not in (None, ""):
        if not isinstance(id_pattern, str):
            errors.append("'id_pattern' must be a string")
        else:
            try:
                re.compile(id_pattern)
            except re.error as exc:
                errors.append(f"'id_pattern' is not a valid regex: {exc}")

    return errors


def _profile_label(profile: dict[str, Any]) -> str:
    """Human-readable identity for log/warning lines."""
    etype, subtype = _profile_key(profile)
    return f"{etype or '?'}/{subtype or '(base)'}"


def load_profiles(path: Path | None = None) -> None:
    """Load equipment profiles from the app-level ``config.json``.

    Args:
        path: Path to the app_config directory. If None, uses default location.

    Raises:
        FileNotFoundError: If the config file doesn't exist
        json.JSONDecodeError: If the JSON is malformed
        ValueError: If the JSON structure is invalid
    """
    global _app_profiles, _app_load_attempted

    _app_load_attempted = True
    if path is None:
        path = get_config_dir()

    logger.debug(f"Loading equipment profiles from: {path}")

    try:
        config_manager.maybe_migrate(path, config_manager.OLD_APP_FILES)
        data = config_manager.read_unified_config(path)
        profiles_section = data.get("profiles", {})

        equipment = profiles_section.get("equipment", [])
        if not isinstance(equipment, list):
            raise ValueError("'profiles.equipment' must be a list")

        # Validate and filter profiles
        valid_profiles = []
        for profile in equipment:
            if not isinstance(profile, dict):
                continue
            errors = validate_profile(profile)
            if errors:
                logger.error(
                    "Skipping invalid app profile '%s': %s",
                    _profile_label(profile),
                    "; ".join(errors),
                )
            else:
                valid_profiles.append(profile)

        _app_profiles = valid_profiles
        logger.info(f"Loaded {len(_app_profiles)} valid equipment profiles")

    except FileNotFoundError:
        logger.error(f"Equipment profiles config not found at: {path}")
        raise
    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON in config file: {e}")
        raise
    except Exception as e:
        logger.error(f"Error loading equipment profiles: {e}")
        raise


def _autoload_app_profiles() -> None:
    """Best-effort one-shot load of app profiles for lookup callers."""
    if _app_profiles or _app_load_attempted:
        return
    try:
        load_profiles()
    except Exception as exc:
        logger.error(f"Failed to auto-load equipment profiles: {exc}")


def set_project_path(project_path: Path | None, bundle: Any = None) -> list[str]:
    """Set the current project path for loading project-specific overrides.

    When a project path is set, this loads BOTH the bundle-supplied
    profile defaults (from the active bundle's ``defaults.json``) and
    the per-project overrides (from the project's ``config.json``).
    Layering: project > bundle > app.

    Args:
        project_path: Path to the project directory, or None to clear project context.
        bundle: The project's bundle when the caller read it already (a project open reads it
            once for every reader); None: it is read here.

    Returns:
        List of warning messages about skipped/invalid profiles and
        old-format bundles (empty if all OK).

    Example:
        >>> warnings = set_project_path(Path("/path/to/project"))
        >>> for w in warnings:
        ...     print(w)
    """
    global _project_path, _project_profiles, _bundle_profiles, _equipment_overrides

    _project_path = project_path

    if project_path is None:
        _project_profiles = []
        _bundle_profiles = []
        _equipment_overrides = {}
        logger.debug("Cleared project path, bundle, and project profiles")
        return []
    warnings = load_bundle_profiles(bundle)
    warnings.extend(load_project_overrides())
    return warnings


def load_bundle_profiles(bundle: Any = None) -> list[str]:
    """Load the active bundle's equipment profiles into the cache.

    Reads ``bundle.defaults["equipment_profiles"]`` from the bundle
    referenced by the current project. Silently sets an empty list when
    no project path is set or the project has no bundle ref.

    HARD CUT (D4): a bundle whose defaults lack ``equipment_profiles``
    is an old-format (bundle/1) bundle — the profile layer stays empty
    and a rebuild-required warning is returned. There is NO fallback to
    the legacy bundle/1 profiles key.

    Bundle profiles are read-only — the bundle is immutable. Project
    overrides win for any matching (equipment_type, subtype).

    Returns:
        List of warning messages (surfaced via ``set_project_path``).

    The layer is built first and swapped in whole: an F5 runs this on a worker, and a
    reader on the GUI thread must never meet it empty half-way. *bundle*: as read already
    (None: read here).
    """
    global _bundle_profiles

    _bundle_profiles, warnings = _read_bundle_layer(bundle)
    return warnings


def _read_bundle_layer(bundle: Any) -> tuple[list[dict[str, Any]], list[str]]:
    """The bound project's bundle profile layer and its warnings (see above)."""
    if _project_path is None:
        return [], []

    # Lazy import: bundle_registry isn't needed for the import-time
    # ``load_profiles()`` and pulling it eagerly would create a cycle.
    from . import project_bundle

    if bundle is None:
        bundle = project_bundle.read_active_bundle(_project_path)
    if bundle is None:
        return [], []

    raw = bundle.defaults.get("equipment_profiles")
    if not isinstance(raw, list):
        msg = (
            f"Bundle {bundle.id}@{bundle.version} has no equipment_profiles "
            "(old bundle format bundle/1) — rebuild the bundle"
        )
        logger.error(msg)
        return [], [msg]

    # Filter the same way load_project_overrides does so an invalid
    # bundle profile doesn't poison the runtime layering. Bundle
    # profiles failing validation are surprising — log loudly so the
    # bundle author sees the issue.
    valid: list[dict[str, Any]] = []
    for profile in raw:
        if not isinstance(profile, dict):
            continue
        errors = validate_profile(profile)
        if errors:
            logger.error(
                "Skipping invalid bundle profile '%s': %s",
                _profile_label(profile),
                "; ".join(errors),
            )
            continue
        valid.append(profile)
    logger.info(
        "Loaded %d equipment profiles from bundle %s@%s",
        len(valid),
        bundle.id,
        bundle.version,
    )
    return valid, []


def reload_bundle_layer() -> None:
    """Re-read the bundle profile layer for the bound project — no-op
    when none is bound. Called by ``bundle_registry.invalidate_caches``
    after a library mutation so this module-global copy can't go stale.
    Warnings are logged by ``load_bundle_profiles``, not returned."""
    if _project_path is None:
        return
    load_bundle_profiles()


def load_project_overrides() -> list[str]:
    """Load project-specific profile overrides from the current project.

    Reads ``profiles.equipment`` from the project's ``config.json``.
    Missing section is not an error — project overrides are optional.

    Returns:
        List of warning messages about skipped/invalid profiles (empty if all OK).
        Callers in UI layers can present these to the user if desired.

    Raises:
        json.JSONDecodeError: If the override file exists but contains invalid JSON
        ValueError: If the JSON structure is invalid
    """
    global _project_profiles, _equipment_overrides

    if _project_path is None:
        logger.debug("No project path set, skipping project override load")
        return []

    override_path = _project_path / "config"

    cfg = config_manager.read_unified_config(override_path)
    overrides = cfg.get("equipment_overrides", {})
    _equipment_overrides = overrides if isinstance(overrides, dict) else {}
    profiles_section = cfg.get("profiles", {})
    equipment = profiles_section.get("equipment", [])

    if not equipment:
        logger.debug(f"No project equipment profiles found at: {override_path}")
        _project_profiles = []
        return []

    logger.debug(f"Loading project equipment profiles from: {override_path}")

    warnings: list[str] = []
    try:
        if not isinstance(equipment, list):
            raise ValueError("'profiles.equipment' must be a list")

        # Validate and filter profiles
        valid_profiles = []
        invalid_profiles = []

        for profile in equipment:
            if not isinstance(profile, dict):
                invalid_profiles.append("  • (non-dict entry)")
                continue
            errors = validate_profile(profile)
            if errors:
                label = _profile_label(profile)
                invalid_profiles.append(f"  • {label}: {'; '.join(errors)}")
                logger.warning(f"Skipping invalid project profile '{label}': {'; '.join(errors)}")
            else:
                valid_profiles.append(profile)

        _project_profiles = valid_profiles
        logger.info(f"Loaded {len(_project_profiles)} valid project profile overrides")

        # Collect warnings for invalid profiles so callers can present them
        if invalid_profiles:
            warnings.append(
                "The following equipment profiles were skipped because they "
                "are invalid:\n\n" + "\n".join(invalid_profiles)
            )

    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON in project override file: {e}")
        raise
    except ValueError as e:
        logger.error(f"Invalid structure in project override file: {e}")
        raise
    except Exception as e:
        logger.error(f"Error loading project overrides: {e}")
        # Don't raise - allow graceful degradation
        _project_profiles = []

    return warnings


def get_all_profiles() -> list[dict[str, Any]]:
    """Get all profiles merged from all sources.

    Returns profiles in precedence order: project → bundle → app.
    Within each layer, (equipment_type, subtype) is unique. Across
    layers, a project override of a bundle/app profile shows up first;
    the lower-layer duplicates appear later. Callers walking
    left-to-right and breaking on first match get the correct
    effective profile.

    Returns:
        List of all profile dictionaries.
    """
    return _project_profiles + _bundle_profiles + _app_profiles


def get_profile_scope(profile: dict[str, Any]) -> str:
    """Return the scope string for a profile: 'project', 'bundle', or 'app'."""
    if profile in _project_profiles:
        return "project"
    if profile in _bundle_profiles:
        return "bundle"
    return "app"


def get_profile(equipment_type: str, subtype: str = "") -> dict[str, Any] | None:
    """Get the profile for an EXACT (equipment_type, subtype) key.

    Layering: project > bundle > app (first match wins). This is the
    only lookup declared equipment should use — never name matching.

    Args:
        equipment_type: Declared equipment type (e.g. "psu")
        subtype: Profile subtype; "" addresses the single-variant base
            profile of the type.

    Returns:
        Profile dictionary if found, None otherwise
    """
    _autoload_app_profiles()

    want = (equipment_type or "", subtype or "")
    for profile in get_all_profiles():
        if _profile_key(profile) == want:
            return profile
    return None


def match_profile_by_name(name: str) -> dict[str, Any] | None:
    """Match a profile by instance NAME — for UNDECLARED groups only.

    Declared equipment must resolve via :func:`get_profile` /
    :func:`get_effective_profile` exact keys. Matching is
    case-insensitive: alias → ``id_pattern`` regex, then a digit-strip
    retry (e.g. ``"IOBOX1"`` → ``"IOBOX"``). Each pass walks layers
    project → bundle → app.

    Args:
        name: Instance name to match (e.g. "IOBOX1")

    Returns:
        Profile dictionary if found, None otherwise
    """
    _autoload_app_profiles()

    def _search(profile_list: list[dict[str, Any]], lookup: str) -> dict[str, Any] | None:
        lookup_lower = lookup.lower().strip()

        # Pass 1: Alias match
        for profile in profile_list:
            for alias in profile.get("aliases") or []:
                if isinstance(alias, str) and alias.lower() == lookup_lower:
                    return profile

        # Pass 2: id_pattern match (advisory regex)
        for profile in profile_list:
            pattern = profile.get("id_pattern") or ""
            if pattern:
                try:
                    if re.search(pattern, lookup, re.IGNORECASE):
                        return profile
                except re.error as e:
                    logger.warning(f"Invalid id_pattern in profile: {pattern} - {e}")
                    continue

        return None

    candidates = [name]
    base_name = name.rstrip("0123456789")
    if base_name and base_name != name:
        candidates.append(base_name)

    for lookup in candidates:
        for profile_list, source in [
            (_project_profiles, "project"),
            (_bundle_profiles, "bundle"),
            (_app_profiles, "app"),
        ]:
            if profile_list:
                result = _search(profile_list, lookup)
                if result is not None:
                    logger.debug(
                        f"Matched '{name}' (lookup '{lookup}') to {source} "
                        f"profile: {_profile_label(result)}"
                    )
                    return result

    logger.debug(f"No equipment profile matched name: {name}")
    return None


def is_inverted_remote(equipment_type: str, base_name: str) -> bool:
    """Check if an equipment instance uses inverted remote logic.

    For inverted logic:
    - remote_value = False means the device is in REMOTE mode
    - remote_value = True means the device is in MANUAL/LOCAL mode

    Args:
        equipment_type: Declared equipment type ("" if unknown)
        base_name: Instance name to check

    Returns:
        True if the effective profile declares ``remote.inverted``.
    """
    profile = get_effective_profile(equipment_type, base_name)
    if profile is None:
        return False

    remote = profile.get("remote")
    if isinstance(remote, dict):
        return bool(remote.get("inverted", False))
    return False


def get_equipment_override(base_name: str) -> str | None:
    """Get the profile override for an equipment instance.

    Args:
        base_name: Instance key (e.g. ``"IOBOX1"``).

    Returns:
        Profile subtype string if an override is set, ``None`` otherwise.
    """
    if _project_path is None:
        return None
    entry = _equipment_overrides.get(base_name, {})  # as loaded with the project's profiles
    return entry.get("profile") if isinstance(entry, dict) else None


def set_equipment_override(base_name: str, profile_subtype: str | None) -> None:
    """Set or remove a profile override for an equipment instance.

    Args:
        base_name: Instance key (e.g. ``"IOBOX1"``).
        profile_subtype: Subtype string to override with, or ``None`` to
            remove the override.
    """
    global _equipment_overrides
    if _project_path is None:
        logger.warning("Cannot set equipment override: no project path set")
        return
    config_dir = _project_path / "config"
    overrides = config_manager.read_config_section(config_dir, "equipment_overrides")
    if profile_subtype is None:
        overrides.pop(base_name, None)
    else:
        overrides[base_name] = {"profile": profile_subtype}
    config_manager.write_config_section(config_dir, "equipment_overrides", overrides)
    _equipment_overrides = overrides
    logger.info(f"Equipment override for '{base_name}': {profile_subtype!r}")


def get_effective_profile(
    equipment_type: str,
    base_name: str,
    declared_subtype: str = "",
) -> dict[str, Any] | None:
    """Get the effective profile for an equipment instance.

    Resolution order:

    1. Instance override (project ``equipment_overrides`` section) —
       type-validated: the stored subtype must exist under
       ``equipment_type``, otherwise it is ignored with a warning.
    2. Exact ``(equipment_type, declared_subtype)`` key.
    3. ``None`` — declared equipment never falls back to name matching.

    Name matching (:func:`match_profile_by_name`) applies only when
    ``equipment_type`` is unknown/undeclared (empty). The scanner's
    ``"unknown"`` sentinel is normalized to ``""`` here, so undeclared
    groups get name matching and layer-wide override resolution.

    Args:
        equipment_type: Declared equipment type ("" or the scanner
            sentinel "unknown" if undeclared)
        base_name: Instance key (e.g. ``"IOBOX1"``).
        declared_subtype: Subtype declared for the group ("" for base).

    Returns:
        Profile dict, or ``None`` if no profile matches.
    """
    if equipment_type == "unknown":
        equipment_type = ""

    override_subtype = get_equipment_override(base_name)
    if override_subtype is not None:
        if equipment_type:
            profile = get_profile(equipment_type, override_subtype)
            if profile is not None:
                return profile
            logger.warning(
                f"Override profile '{override_subtype}' for '{base_name}' does "
                f"not exist under equipment type '{equipment_type}' — ignoring"
            )
        else:
            # Unknown etype: type validation is impossible; resolve the
            # override by subtype across layers.
            _autoload_app_profiles()
            for profile in get_all_profiles():
                if (profile.get("subtype") or "") == override_subtype:
                    return profile
            logger.warning(
                f"Override profile '{override_subtype}' for '{base_name}' "
                "matches no loaded profile — ignoring"
            )

    if equipment_type:
        return get_profile(equipment_type, declared_subtype)
    return match_profile_by_name(base_name)


# ---------------------------------------------------------------------------
# Override pattern API — per-profile regex for _MANUAL_OVERRIDE variables
# ---------------------------------------------------------------------------


def _get_override_suffix(profile: dict[str, Any]) -> str:
    """Return the override variable suffix for a profile, or empty string."""
    suffix = profile.get("override_suffix", "")
    if isinstance(suffix, str):
        return suffix.strip()
    return ""


def _build_override_pattern(profile: dict[str, Any]) -> re.Pattern | None:
    """Build an override regex from the ``override_suffix`` field.

    Constructs a pattern that matches variable declarations of the form
    ``<NAME><SUFFIX> = True|False`` where ``<NAME>`` is derived from the
    profile's ``display_name`` and uppercase-safe ``aliases``.
    """
    suffix = _get_override_suffix(profile)
    if not suffix:
        return None

    # Collect name prefixes: display_name + aliases that are purely alphanumeric/underscore
    prefixes: list[str] = []
    display = profile.get("display_name", "")
    if display:
        prefixes.append(re.escape(display.upper()))
    for alias in profile.get("aliases") or []:
        upper = alias.upper().replace("-", "_")
        if re.fullmatch(r"[A-Z][A-Z0-9_]*", upper) and upper not in prefixes:
            prefixes.append(re.escape(upper))
    if not prefixes:
        return None

    # Allow three instance-suffix styles: bare ("IOBOX"), numeric ("IOBOX1"),
    # or underscored name ("IOBOX_A", "IOBOX_B").
    alts = "|".join(f"{p}(?:[0-9]+|_[A-Z][A-Z0-9_]*)?" for p in prefixes)
    escaped_suffix = re.escape(suffix)
    regex = rf"^((?:{alts}){escaped_suffix})\s*=\s*(True|False)"
    try:
        return re.compile(regex, re.MULTILINE)
    except re.error as exc:
        logger.warning(
            "Failed to build override pattern for profile '%s': %s",
            _profile_label(profile),
            exc,
        )
        return None


def get_all_override_info() -> list[tuple[re.Pattern, str]]:
    """Return ``(compiled_pattern, suffix)`` pairs for all loaded profiles.

    The suffix is needed by the scanner to derive the equipment base-name
    from a matched variable (e.g. strip ``_MANUAL_OVERRIDE`` from
    ``IOBOX1_MANUAL_OVERRIDE``). Deduped by (equipment_type, subtype),
    first (highest-precedence) profile wins.
    """
    seen: set[tuple[str, str]] = set()
    result: list[tuple[re.Pattern, str]] = []
    for profile in get_all_profiles():
        key = _profile_key(profile)
        if key in seen:
            continue
        pat = _build_override_pattern(profile)
        if pat is None:
            continue
        seen.add(key)
        result.append((pat, _get_override_suffix(profile)))
    return result
