"""The Python a bundle's environment is built with, whatever the app's version.

The app's own interpreter when the bundle asks for its version; any other version is
a private runtime. uv owns downloading, extraction and installation locking. Its
installations are private to Procedo and retained: deleting one would break every
venv based on it. All processes belong to the caller's setup job, including
interpreter probes.
"""

from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from . import project_local_state, venv_key
from .process_job import ProcessJob

_QUERY_TIMEOUT_S = 30
_DOWNLOAD_TIMEOUT_S = 600
_TLS_SETTINGS = frozenset({"UV_NATIVE_TLS", "UV_SYSTEM_CERTS"})
_PROBE = (
    "import json,sys,sysconfig; print(json.dumps({"
    "'implementation':sys.implementation.name,"
    "'version':list(sys.version_info[:2]),"
    "'releaselevel':sys.version_info.releaselevel,"
    "'platform':sysconfig.get_platform().replace('-','_').replace('.','_'),"
    "'gil_disabled':bool(sysconfig.get_config_var('Py_GIL_DISABLED')),"
    "'debug':bool(sysconfig.get_config_var('Py_DEBUG')),"
    "'base_executable':getattr(sys,'_base_executable',sys.executable)}))"
)


class RuntimeSetupError(RuntimeError):
    """Runtime setup failed, with a short cause followed by diagnostic output."""


def _architecture() -> str:
    arch = platform.machine().lower()
    return {"amd64": "x86_64", "arm64": "aarch64", "i686": "i386", "x86": "i386"}.get(arch, arch)


def _mac_platform(tag: str) -> tuple[tuple[int, int], str] | None:
    match = re.fullmatch(r"macosx_(\d+)_(\d+)_(x86_64|arm64|universal2|i386)", tag)
    return ((int(match[1]), int(match[2])), match[3]) if match else None


def _native_platform(tag: str) -> bool:
    arch = _architecture()
    if sys.platform == "win32":
        return tag == {"x86_64": "win_amd64", "aarch64": "win_arm64", "i386": "win32"}.get(arch)
    if sys.platform.startswith("linux"):
        return tag == f"linux_{arch}"
    if sys.platform == "darwin":
        target = _mac_platform(tag)
        if target is None:
            return False
        os_version = tuple(int(part) for part in platform.mac_ver()[0].split(".")[:2])
        native_arch = "arm64" if arch == "aarch64" else arch
        return target[0] <= os_version and (
            target[1] == native_arch
            or target[1] == "universal2"
            and native_arch in {"arm64", "x86_64"}
        )
    return False


def validate_tag(tag: str) -> str:
    """Validate a native CPython compatibility tag and return its uv request.

    The app's supported Python versions do not constrain a bundle's Python.
    Availability of a requested release is decided by the pinned uv catalogue.
    """
    match = re.fullmatch(r"cp3(0|[1-9]\d*)-([a-z0-9_]+)", tag)
    if match is None:
        raise RuntimeSetupError(f"The bundle has an unsupported Python tag: {tag!r}.")
    if not _native_platform(match[2]):
        raise RuntimeSetupError(f"The bundle requires a platform unavailable on this PC: {tag}.")
    return f"cpython@3.{int(match[1])}"


def _environment() -> dict[str, str]:
    # Inherited uv settings must not redirect a private install or disable checking.
    env = {
        key: value
        for key, value in os.environ.items()
        if (not key.upper().startswith("UV_") or key.upper() in _TLS_SETTINGS)
        and key.upper() not in {"VIRTUAL_ENV", "CONDA_PREFIX", "CONDA_DEFAULT_ENV"}
    }
    env.update(
        UV_PYTHON_INSTALL_DIR=str(project_local_state.python_runtimes_root()),
        UV_CACHE_DIR=str(project_local_state.python_runtime_cache_root()),
        UV_PYTHON_CACHE_DIR=str(project_local_state.python_runtime_cache_root() / "archives"),
        UV_PYTHON_INSTALL_BIN="0",
        UV_PYTHON_INSTALL_REGISTRY="0",
    )
    return env


def _run(
    job: ProcessJob,
    command: list[str],
    *,
    what: str,
    timeout: int = _QUERY_TIMEOUT_S,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    try:
        return job.run(command, timeout=timeout, env=env, encoding="utf-8")
    except subprocess.TimeoutExpired as exc:
        raise RuntimeSetupError(f"{what} timed out after {timeout}s.") from exc
    except OSError as exc:
        raise RuntimeSetupError(f"{what} failed: {exc}.") from exc


def _refuse(what: str, result: subprocess.CompletedProcess) -> None:
    details = next(
        (text.strip() for text in (result.stderr, result.stdout) if text and text.strip()),
        f"Exit code {result.returncode}",
    )
    cause = details.splitlines()[-1].strip().lstrip("╰─▶ ")
    raise RuntimeSetupError(f"{what} failed: {cause}\n\n{details}")


def verify(python: Path, tag: str, *, job: ProcessJob) -> Path:
    """Verify a runtime/venv and return its base executable for local readiness checks."""
    request = validate_tag(tag)
    result = _run(
        job,
        [str(python), "-I", "-S", "-c", _PROBE],
        what="Required runtime verification",
    )
    if result.returncode:
        _refuse("Required runtime verification", result)
    try:
        info = json.loads(result.stdout)
        actual_platform = info["platform"]
        expected_version = [int(part) for part in request.removeprefix("cpython@").split(".")]
        compatible = (
            info["implementation"] == "cpython"
            and info["version"] == expected_version
            and info["releaselevel"] == "final"
            and not info["gil_disabled"]
            and not info["debug"]
            and _native_platform(actual_platform)
        )
        base = Path(info["base_executable"])
        if not compatible or not base.is_absolute() or not base.is_file():
            raise ValueError("incompatible or missing base interpreter")
    except (KeyError, TypeError, ValueError, OSError) as exc:
        raise RuntimeSetupError(
            f"The prepared runtime does not match the bundle's Python tag {tag}.\n\n"
            f"{result.stdout.strip()}"
        ) from exc
    return base


def _managed_executable(output: str) -> Path:
    try:
        python = Path(output.strip())
        root = project_local_state.python_runtimes_root().resolve()
        if not python.is_absolute() or not python.is_file():
            raise ValueError("missing or relative executable")
        python.resolve().relative_to(root)
    except (ValueError, OSError) as exc:
        raise RuntimeSetupError(
            "The runtime helper returned no usable private interpreter."
        ) from exc
    return python


def ensure(tag: str, *, job: ProcessJob, progress: Callable[[str], None]) -> Path:
    """The verified base interpreter for an environment needing *tag*.

    A bundle built for the app's Python uses the app's own: no download. Any other
    version is a private runtime, reused or obtained.
    """
    request = validate_tag(tag)
    job.check_stopped()
    if tag == venv_key.builder_tag():
        # The base, not the app's venv: a venv made from a venv is based on its base.
        python = Path(sys._base_executable)
    else:
        python = _private_runtime(request, job=job, progress=progress)
    verify(python, tag, job=job)
    job.check_stopped()
    return python


def _private_runtime(request: str, *, job: ProcessJob, progress: Callable[[str], None]) -> Path:
    """Reuse or obtain a private runtime; never delete or upgrade an installed runtime."""
    try:
        project_local_state.python_runtimes_root().mkdir(parents=True, exist_ok=True)
        project_local_state.python_runtime_cache_root().mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeSetupError(f"Required runtime storage could not be opened: {exc}.") from exc
    env = _environment()
    # Bench PCs may trust a company proxy through the operating system's certificate store.
    uv = [sys.executable, "-I", "-m", "uv", "--no-config", "--system-certs", "--color", "never"]
    find = [
        *uv,
        "python",
        "find",
        "--managed-python",
        "--system",
        "--no-project",
        "--no-python-downloads",
        request,
    ]
    result = _run(job, find, env=env, what="Required runtime lookup")
    if result.returncode:
        progress("Downloading required runtime…")
        installed = _run(
            job,
            [*uv, "python", "install", "--no-bin", "--no-registry", request],
            env=env,
            what="Required runtime download",
            timeout=_DOWNLOAD_TIMEOUT_S,
        )
        if installed.returncode:
            _refuse("Required runtime download", installed)
        result = _run(job, find, env=env, what="Required runtime lookup")
        if result.returncode:
            _refuse("Required runtime lookup", result)
    return _managed_executable(result.stdout)
