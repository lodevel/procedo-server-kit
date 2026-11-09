"""What a bundle's environment is CALLED: the key, and what it costs to ask.

An environment holds exactly what its bundle installs, so two bundles with the same
content need the same environment and any difference in that content needs another
one. This module is the whole of that judgement — :mod:`venv_pool` owns the folders,
the build protocol and the limit, and never decides what anything is called.

**The key** is a sha256 over the interpreter that will create the venv
(:func:`bundle_python`: implementation, ``major.minor`` and platform), the bundle's id
and version, every wheel's *name and sha256*, and the bytes of ``requirements.txt``. A bundle rebuilt under the same
version has other wheel bytes and is therefore another environment — the trap an
``id@version`` install record fell into, silently, for as long as it existed.

**Asking must be cheap.** Hashing every wheel on every project open, over a share,
is not affordable, so the answer is cached MACHINE-LOCALLY — never beside the bundle,
which may itself be on a share or a synced folder — against a stat fingerprint of the
same files, and the per-project answer is memoized against a smaller one. A share
that has gone away serves the memo rather than answering "no environment" for an
interpreter sitting on the local disk.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
from pathlib import Path

from . import atomic_write, project_local_state
from .paths import sha256_file

logger = logging.getLogger(__name__)

#: Bundle dir -> key, against a stat fingerprint. Machine-local, beside the pool.
_KEY_CACHE_NAME = "key_cache.json"

#: Why a project can have no environment of its own, in the words a user reads.
NO_BUNDLE = (
    "This project has no bundle at <project>/bundle/ (or it carries no "
    "wheels/requirements.txt, or its requirements.txt names a wheel it does not hold). "
    "A Python environment is built from a bundle's wheels, "
    "so there is nothing to build one from. Import one via Packages -> Bundle Library."
)


def builder_tag() -> str:
    """The current app/build interpreter's tag, recorded by the Bundle Generator."""
    import sysconfig

    impl = "cp" if sys.implementation.name == "cpython" else sys.implementation.name
    platform = sysconfig.get_platform().replace("-", "_").replace(".", "_")
    return f"{impl}{sys.version_info[0]}{sys.version_info[1]}-{platform}"


def wheels_dir(bundle_dir: Path) -> Path:
    return bundle_dir / "wheels"


def requirements_file(bundle_dir: Path) -> Path:
    return wheels_dir(bundle_dir) / "requirements.txt"


def _manifest(bundle_dir: Path) -> dict | None:
    """The bundle's manifest, or ``None`` when it is absent or not an object.

    Read here rather than through ``bundle_registry``: the key must exist for any
    bundle pip can be pointed at, and loading a Bundle also parses ``defaults.json``,
    which has nothing to do with what lands in site-packages.
    """
    try:
        data = json.loads((bundle_dir / "bundle.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def bundle_ref(bundle_dir: Path) -> tuple[str, str]:
    """``(id, version)`` as the bundle declares them — ``("", "")`` when it does not."""
    manifest = _manifest(bundle_dir) or {}
    ref = (manifest.get("id"), manifest.get("version"))
    return (ref[0] if isinstance(ref[0], str) else "", ref[1] if isinstance(ref[1], str) else "")


def bundle_python(bundle_dir: Path) -> str:
    """The interpreter tag the bundle was built for, or ``""`` when it names none.

    Every bundle built now records it (its pins were resolved for that Python);
    only a bundle built before pinning names none.
    """
    tag = (_manifest(bundle_dir) or {}).get("python")
    return tag if isinstance(tag, str) else ""


def wheel_files(bundle_dir: Path) -> list[Path]:
    """The bundle's own wheels: the ``.whl`` lines of its requirements.txt. A wheel file
    it does not name (left by copying a newer project over an older one) is not the
    bundle's, and is never hashed or installed. A named wheel may be missing; callers
    that need it present check."""
    try:
        lines = requirements_file(bundle_dir).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    names = (name for name in map(str.strip, lines) if name.endswith(".whl"))
    return sorted(wheels_dir(bundle_dir) / name for name in names)


def missing_wheels(bundle_dir: Path) -> list[str]:
    """The wheel files the bundle's requirements.txt names and it does not hold."""
    return [wheel.name for wheel in wheel_files(bundle_dir) if not wheel.exists()]


def fingerprint(bundle_dir: Path) -> tuple | None:
    """A stat-only signature of everything the key is computed from.

    Cheap enough to take on every lookup; the hashing behind :func:`venv_key` is not.
    ``None`` when the bundle has no manifest or no requirements — such a folder is
    not a bundle pip can install, and gets no environment.
    """
    manifest = bundle_dir / "bundle.json"
    requirements = requirements_file(bundle_dir)
    try:
        man_stat = manifest.stat()
        req_stat = requirements.stat()
    except OSError:
        return None
    if _manifest(bundle_dir) is None:
        return None
    wheels = []
    for wheel in wheel_files(bundle_dir):
        try:
            st = wheel.stat()
        except OSError:
            return None
        wheels.append((wheel.name, st.st_size, st.st_mtime_ns))
    return (
        bundle_python(bundle_dir),
        (man_stat.st_size, man_stat.st_mtime_ns),
        (req_stat.st_size, req_stat.st_mtime_ns),
        tuple(wheels),
    )


def _compute_key(bundle_dir: Path) -> str:
    """Hash the interpreter, the bundle ref, every wheel's bytes and requirements.txt."""
    bundle_id, version = bundle_ref(bundle_dir)
    lines = [
        "venv-key-v1",
        f"python={bundle_python(bundle_dir)}",
        f"bundle={bundle_id}@{version}",
    ]
    for wheel in wheel_files(bundle_dir):
        lines.append(f"wheel={wheel.name}={sha256_file(wheel)}")
    lines.append(f"requirements={sha256_file(requirements_file(bundle_dir))}")
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


#: The key cache as this process last read or wrote it: ``{bundle realpath: [fp, key]}``.
_KEY_CACHE: dict[str, tuple[list, str]] | None = None


def _cache_root() -> Path:
    """Where the cache file sits: beside the environments it names, asked for from the
    one module allowed to compose anything under the app-data root."""
    return project_local_state.venvs_root()


def _key_cache_path() -> Path:
    return _cache_root() / _KEY_CACHE_NAME


def _load_key_cache() -> dict[str, tuple[list, str]]:
    global _KEY_CACHE
    if _KEY_CACHE is not None:
        return _KEY_CACHE
    cache: dict[str, tuple[list, str]] = {}
    try:
        data = json.loads(_key_cache_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    if isinstance(data, dict):
        for where, entry in data.items():
            if isinstance(entry, list) and len(entry) == 2 and isinstance(entry[1], str):
                cache[where] = (entry[0], entry[1])
    _KEY_CACHE = cache
    return cache


def _save_key_cache(cache: dict[str, tuple[list, str]]) -> None:
    try:
        _cache_root().mkdir(parents=True, exist_ok=True)
        atomic_write.write_json(_key_cache_path(), {k: [v[0], v[1]] for k, v in cache.items()})
    except OSError:
        logger.debug("Could not write the venv key cache", exc_info=True)


def venv_key(bundle_dir: Path | str) -> str | None:
    """This bundle's environment key, or ``None`` when it is not an installable bundle.

    Answered from the machine-local cache whenever the stat fingerprint is unchanged,
    so opening a project costs a handful of stats rather than a sha256 of every wheel
    on a share.
    """
    bundle_dir = Path(bundle_dir)
    fp = fingerprint(bundle_dir)
    if fp is None:
        return None
    where = os.path.normcase(os.path.realpath(bundle_dir))
    cache = _load_key_cache()
    cached = cache.get(where)
    # JSON has no tuples: the cached fingerprint comes back as nested lists.
    if cached is not None and json.loads(json.dumps(fp)) == cached[0]:
        return cached[1]
    key = _compute_key(bundle_dir)
    cache[where] = (json.loads(json.dumps(fp)), key)
    _save_key_cache(cache)
    return key


def forget_key_cache() -> None:
    """Drop this process's copy of the key cache. For tests that move the pool root."""
    global _KEY_CACHE
    _KEY_CACHE = None


# --------------------------------------------------------------------------- #
# Project -> key -> folder                                                    #
# --------------------------------------------------------------------------- #


def project_bundle_dir(project_path: Path | str) -> Path:
    """``<project>/bundle/`` — the one thing a project's environment depends on."""
    return Path(project_path) / "bundle"


#: Per-project memo: project root -> (cheap fingerprint, key). The share is read to
#: resolve a venv that sits on the LOCAL disk, and ``project_identity`` already taught
#: this codebase what a dropped SMB packet does to a lookup that re-reads it.
_KEYS: dict[str, tuple[tuple, str]] = {}


def _cheap_fingerprint(bundle_dir: Path) -> tuple:
    """Four stats that move whenever a bundle CHANGES. Raises on an unreachable share.

    The bundle directory's own mtime catches a whole-bundle swap (``import_bundle_to_
    project`` renames a staged copy into place). It does NOT catch a sync pull, which
    replaces individual wheels *inside* ``bundle/wheels/`` and leaves ``bundle/``
    untouched — measured: the bench then ran a whole session in the environment built
    from the wheels the pull had just replaced. The wheels directory's mtime moves
    whenever a file in it is created, renamed or removed (the pull writes ``.part``
    and renames), and ``requirements.txt`` moves when what to install changes.
    """
    bundle_stat = bundle_dir.stat()
    manifest_stat = (bundle_dir / "bundle.json").stat()
    wheels_stat = wheels_dir(bundle_dir).stat()
    requirements_stat = requirements_file(bundle_dir).stat()
    return (
        bundle_stat.st_mtime_ns,
        manifest_stat.st_size,
        manifest_stat.st_mtime_ns,
        wheels_stat.st_mtime_ns,
        requirements_stat.st_size,
        requirements_stat.st_mtime_ns,
    )


def key_for_project(project_path: Path | str) -> str | None:
    """The environment key for this project's bundle, memoized for the process.

    The memo is checked against a two-stat fingerprint, so a bundle imported into the
    open project is followed without anyone having to remember to invalidate — and a
    share that has gone away serves the memo rather than answering "no venv" for an
    interpreter sitting on the local disk.
    """
    where, fp, remembered = _memo(project_path)
    if remembered is not None:
        return remembered
    if fp is None:
        return None
    key = venv_key(project_bundle_dir(project_path))
    if key is not None:
        _KEYS[where] = (fp, key)
    return key


def asks_the_bundle(project_path: Path | str) -> bool:
    """Whether :func:`key_for_project` would ask :func:`venv_key`, which hashes every wheel of
    a bundle it has not seen (one that another PC just swapped in, over a share: seconds):
    the bundle answers and the memo no longer holds. False for a project with no bundle, a
    share that does not answer, and an unchanged bundle — each answered by a few stats."""
    _where, fp, remembered = _memo(project_path)
    return fp is not None and remembered is None


def _memo(project_path: Path | str) -> tuple[str, tuple | None, str | None]:
    """(memo key, the bundle's cheap fingerprint — None when it does not answer, the memoized
    environment key when it still holds — also for a bundle that does not answer)."""
    where = os.path.normcase(os.path.abspath(Path(project_path)))
    remembered = _KEYS.get(where)
    try:
        fp = _cheap_fingerprint(project_bundle_dir(project_path))
    except OSError:
        return where, None, remembered[1] if remembered is not None else None
    held = remembered is not None and remembered[0] == fp
    return where, fp, remembered[1] if held else None


def forget_resolved(project_path: Path | str | None = None) -> None:
    """Forget the memoized key for *project_path* (all of them when ``None``).

    Called by every path that replaces a project's bundle: the fingerprint catches a
    swap on its own, but a bundle rebuilt in place under the same name and second is
    exactly the case a stat cannot see.
    """
    if project_path is None:
        _KEYS.clear()
        return
    _KEYS.pop(os.path.normcase(os.path.abspath(Path(project_path))), None)
