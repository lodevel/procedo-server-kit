"""One virtual environment per BUNDLE, shared by every project that uses it.

A project venv holds exactly what its bundle installs — the wheels its
``wheels/requirements.txt`` names plus the pins it pulls from PyPI — and nothing of the
project itself. So it is a pure function of *(bundle content, interpreter)*, and
keeping one per project was keeping N identical copies of a 300 MB tree: every
project on the baseline bundle had its own, and creating a project meant waiting
for pip to build a venv this machine already had.

Here a venv is keyed by its CONTENT (:func:`venv_key`) and lives in a pool under
the per-user app-data root. Two projects on one bundle share one folder; a project
whose bundle changes simply resolves to another key, which is why there is no
"declared != installed, reinstall in place" any more. The pool is bounded: past
``app_settings.get_max_venvs`` environments the least recently used are deleted,
and a project that needs one back rebuilds it on its next open.

**The key** is a sha256 over the interpreter that will create the venv (implementation,
``major.minor`` and platform), the bundle's id and version, every wheel's *name and
sha256*, and the bytes of ``requirements.txt``. Hashing wheels is far too slow to do on every
project open over a share, so the answer is cached MACHINE-LOCALLY (never beside the
bundle, which may be on a share or a synced folder) against a stat fingerprint of the
same files: a rebuilt bundle that kept its version changes the wheel bytes, the
fingerprint moves, and the key is recomputed.

**A folder is matched on its marker, never on its name** — the same rule the project
state folders follow. The name (``<id>-<version>__<key12>``) is a label for a human
reading a directory listing; ``.venv_key`` inside holds the key in full.

**A build is complete or invisible.** The venv is created AT its final path (a venv is
not relocatable in general: on Windows the console-script launchers in ``Scripts/``
embed the interpreter's absolute path, so a build-then-rename would ship broken
entry points) and the folder is ignored by resolution until ``.complete`` is written
LAST. An interrupted build leaves a folder with a ``.building.<pid>`` file, no
``.complete``, and is removed by the next sweep once that pid is gone. A build's
processes, and theirs, run in one :class:`~procedo.core.process_job.ProcessJob`: a
build that is stopped (:func:`stop_builds`) ends them, waits, and removes its folder.
On Windows an app that dies mid-build also takes them with it (a kill-on-close job)
instead of leaving them holding files open in a folder nobody can clear.

**Nothing deletes a folder it cannot prove is the pool's**: directly under the pool
root, carrying a ``.venv_key`` marker, held by no live process, and containing no
directory link that a delete could escape through. ``.complete`` is removed FIRST, so
a delete that dies half-way (a Windows file still open) leaves a tree that can never
be resolved as a venv, and the next sweep finishes the job.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from . import app_settings, atomic_write, project_local_state, python_runtime
from .paths import folder_link_under, is_link
from .process_alive import pid_alive
from .process_job import ProcessJob, Stopped
from .venv_key import (
    NO_BUNDLE,
    bundle_python,
    bundle_ref,
    key_for_project,
    missing_wheels,
    project_bundle_dir,
    requirements_file,
    venv_key,
    wheel_files,
    wheels_dir,
)

logger = logging.getLogger(__name__)

#: The environment itself, inside the pool folder that describes it.
_VENV_DIR_NAME = ".venv"
#: The key IN FULL. Resolution matches on this file and never on the folder name.
_KEY_MARKER = ".venv_key"
#: Written LAST. Its absence means "not a venv yet", whatever else is in the folder.
_COMPLETE_MARKER = ".complete"
#: The verified base executable must outlive every venv based on it.
_BASE_PYTHON = ".base_python.json"
#: Epoch seconds, rewritten on project open and before a run. Drives the LRU.
_LAST_USED = ".last_used"
#: ``in_use.<pid>`` — this machine, that process, right now. A dead pid is ignored.
_IN_USE_PREFIX = "in_use."
#: ``.building.<pid>`` — a build in flight. A dead pid means an interrupted one.
_BUILDING_PREFIX = ".building."
#: What the last successful install put in this venv (id, version, wheels, when).
_INSTALL_RECORD_NAME = ".bundle_installed.json"

#: ``python -m venv`` on a cold cache with a virus scanner watching: ~10-30 s.
_VENV_TIMEOUT_S = 180
#: A full bundle install (several wheels + transitive deps off PyPI): 60-180 s.
_PIP_TIMEOUT_S = 600


class BuildInProgress(RuntimeError):
    """Another window on this machine is building the very environment we want."""


class BuildFailed(RuntimeError):
    """``python -m venv`` or pip refused. Carries the subprocess output."""


class BuildStopped(BuildFailed):
    """The build was stopped before it finished (:func:`stop_builds`)."""


STOPPED_REASON = "the build was interrupted"


def stopped_text(reason: str) -> str:
    """The one sentence for a setup that did not finish, stopped or failed: *reason* is
    the first meaningful line of what was said (the rest stays with the caller)."""
    first = next((ln.strip() for ln in reason.splitlines() if ln.strip()), "").rstrip(".:!? ")
    return f"Setup stopped: {first or STOPPED_REASON}. It runs again the next time you open the project."


@dataclass(frozen=True)
class Ensured:
    """The outcome of :func:`ensure`: where the environment is and what it cost."""

    folder: Path
    key: str
    bundle_id: str
    bundle_version: str
    wheel_count: int
    #: False when this machine already had the environment — no venv, no pip, no wait.
    built: bool
    pip_log: str


# --------------------------------------------------------------------------- #
# Where the pool is                                                           #
# --------------------------------------------------------------------------- #


def pool_root() -> Path:
    """The folder every shared environment hangs under. Asked for, never composed."""
    return project_local_state.venvs_root()


def venv_in(folder: Path) -> Path:
    """The environment inside a pool folder."""
    return folder / _VENV_DIR_NAME


def install_record_in(folder: Path) -> Path:
    """The install record beside the environment it describes."""
    return folder / _INSTALL_RECORD_NAME


def marker_key(folder: Path) -> str:
    """The key a pool folder claims, or ``""`` when it claims none."""
    try:
        return (folder / _KEY_MARKER).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def is_complete(folder: Path) -> bool:
    """Whether a pool folder finished building. Nothing else may be resolved."""
    return (folder / _COMPLETE_MARKER).is_file()


def _usable(folder: Path) -> bool:
    """Ready without starting Python: a completed venv and its recorded base still exist."""
    if not is_complete(folder) or not project_local_state.venv_python(venv_in(folder)).is_file():
        return False
    try:
        record = json.loads((folder / _BASE_PYTHON).read_text(encoding="utf-8"))
    except FileNotFoundError:
        # Completed environments already in the pool remain reusable.
        return True
    except (OSError, ValueError):
        return False
    executable = record.get("executable") if isinstance(record, dict) else None
    return (
        isinstance(executable, str)
        and Path(executable).is_absolute()
        and Path(executable).is_file()
    )


def folder_name(bundle_dir: Path, key: str) -> str:
    """``<id>-<version>__<key12>`` — a label for a human, never something matched on."""
    bundle_id, version = bundle_ref(bundle_dir)
    label = project_local_state.readable_name(f"{bundle_id or 'bundle'}-{version or '0'}")
    return f"{label}__{key[:12]}"


def _pool_folders() -> list[Path]:
    root = pool_root()
    try:
        return sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        return []


#: (pool root, key) -> the folder found claiming the key: asked again, it is served from here
#: while its marker still says so (a local read), so no project's ``bundle.json`` (on a share,
#: a round trip) is read again to name it.
_FOUND: dict[tuple[Path, str], Path] = {}


def folder_for_key(bundle_dir: Path, key: str) -> Path:
    """Where an environment for *key* is, or would be built.

    The named folder when it is this key's, otherwise whichever folder claims the key
    — a pool folder renamed by hand is still found, because the marker is the proof.
    """
    found = _FOUND.get((pool_root(), key))
    if found is not None and marker_key(found) == key:
        return found
    named = pool_root() / folder_name(bundle_dir, key)
    claimed = (
        named
        if marker_key(named) == key
        else next((f for f in _pool_folders() if marker_key(f) == key), None)
    )
    if claimed is None:
        return named
    _FOUND[(pool_root(), key)] = claimed
    return claimed


def resolve(project_path: Path | str) -> Path | None:
    """The COMPLETE pool folder this project runs in, or ``None`` when it has none."""
    key = key_for_project(project_path)
    if key is None:
        return None
    folder = folder_for_key(project_bundle_dir(project_path), key)
    return folder if _usable(folder) and marker_key(folder) == key else None


def placement(project_path: Path | str) -> Path | None:
    """Where this project's environment is, or WOULD be — ``None`` without a bundle.

    What the editor bridge shows a user who has no venv yet, and where the install
    record for one is written.
    """
    key = key_for_project(project_path)
    if key is None:
        return None
    return folder_for_key(project_bundle_dir(project_path), key)


# --------------------------------------------------------------------------- #
# Holding one: last_used and in_use.<pid>                                     #
# --------------------------------------------------------------------------- #


def touch(folder: Path) -> None:
    """Stamp a folder as used now. Best-effort: an LRU is not worth failing a run for."""
    try:
        (folder / _LAST_USED).write_text(f"{time.time():.3f}", encoding="utf-8")
    except OSError:
        logger.debug("Could not stamp %s as used", folder, exc_info=True)


def last_used(folder: Path) -> float:
    """When this environment was last used. ``0.0`` when nothing says — evicted first."""
    try:
        return float((folder / _LAST_USED).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0.0


def touch_project(project_path: Path | str) -> None:
    """Stamp the environment this project runs in, when it has one."""
    folder = resolve(project_path)
    if folder is not None:
        touch(folder)


#: The folder the GUI has OPEN in this process, so eviction never deletes what this
#: window is looking at even if the claim file could not be written. It is only the
#: window's: a run and a bench session keep claims of their own (:func:`claim`), and
#: :func:`hold` must never release those.
_HELD: Path | None = None

#: What holds an environment, in the claim file's name. Every one of them is a
#: process that would break if the tree went away under it — and they are NOT one
#: per process: a bench opens project B while project A's test is still running, so
#: one app process can legitimately hold two environments for two different reasons.
OPEN = "open"  #: the GUI has this project open
RUN = "run"  #: a test subprocess is executing out of it (the CHILD's pid)
BENCH = "bench"  #: a bench-session daemon is holding instruments from it
ENGINE = "engine"  #: an engine process answering an app's ops (the CHILD's pid, see engine_token)


def engine_token(owner_pid: int | None = None) -> str:
    """``engine-<app pid>``: the app that owns the engine is in the name, so a delete
    can tell its own engines (it stops them) from another window's (it refuses)."""
    return f"{ENGINE}-{os.getpid() if owner_pid is None else owner_pid}"


def _build_claim(pid: int) -> str:
    """``.building.<pid>`` — the same liveness rule, its own namespace."""
    return _claim_name(pid, prefix=_BUILDING_PREFIX)


def _claim_name(pid: int, token: str = "", prefix: str = _IN_USE_PREFIX) -> str:
    """``in_use.<pid>.<why>`` — the pid answers "is anybody still there", the token
    answers "who", so two holders never write, or remove, one another's file."""
    return f"{prefix}{pid}.{token}" if token else f"{prefix}{pid}"


def claim(folder: Path | None, pid: int, token: str) -> None:
    """Record that *pid* is using this environment, for the reason *token*.

    Best-effort by design: a claim that cannot be written must not fail a run. The
    holder that can still be asked in memory is :data:`_HELD`, and a child process
    has the operating system to answer for it.
    """
    if folder is None:
        return
    try:
        (folder / _claim_name(pid, token)).write_text("", encoding="utf-8")
        touch(folder)
    except OSError:
        logger.debug("Could not claim %s for %s.%s", folder, pid, token, exc_info=True)


def release(folder: Path | None, pid: int, token: str) -> None:
    """Drop one claim. A claim left behind by a crash is a DEAD pid, which the
    liveness probe already ignores — this is tidiness, not correctness."""
    if folder is None:
        return
    try:
        (folder / _claim_name(pid, token)).unlink(missing_ok=True)
    except OSError:
        logger.debug("Could not release %s for %s.%s", folder, pid, token, exc_info=True)


def claim_child(project_path: Path | str, pid: int, token: str) -> Path | None:
    """Claim this project's environment for a CHILD process, and say which folder.

    The pid is the child's, so the claim lives exactly as long as the thing that
    would break: a test that runs for twenty minutes keeps it for twenty minutes,
    and one that is killed leaves a pid the operating system reports as gone.
    """
    folder = resolve(project_path)
    claim(folder, pid, token)
    return folder


def held() -> Path | None:
    """The environment this process has OPEN, if any."""
    return _HELD


def hold(folder: Path | None) -> None:
    """Declare the environment this window has open; releases the previous one.

    Only the window's own claim: a test launched from the project being closed is
    still running in its environment, and so is a bench session's daemon. Releasing
    theirs here is how opening a second project deleted the first one's venv out from
    under a live interpreter.
    """
    global _HELD
    previous = _HELD
    if previous is not None and previous != folder:
        release(previous, os.getpid(), OPEN)
    _HELD = folder
    claim(folder, os.getpid(), OPEN)


def _live_claims(folder: Path, prefix: str) -> list[str]:
    """The ``<prefix><pid>[.<token>]`` entries in *folder* whose process is alive.

    ``["?"]`` when the folder cannot be listed: a place that cannot be inspected is
    treated as held, never as free. A use claim whose process is gone is deleted on
    the way (:func:`_drop_dead_claim`).
    """
    try:
        names = [p.name for p in folder.iterdir()]
    except OSError:
        return ["?"]
    live = []
    for name in names:
        if not name.startswith(prefix):
            continue
        pid = name[len(prefix) :].split(".", 1)[0]
        if pid.isdigit() and pid_alive(int(pid)):
            live.append(name)
        elif prefix == _IN_USE_PREFIX:
            _drop_dead_claim(folder / name)
    return live


def _drop_dead_claim(path: Path) -> None:
    """Delete a use claim left by a process that died without releasing it (a crashed
    app's engine, a killed test): the app leaves nothing behind. Best-effort."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.debug("Could not remove the dead claim %s", path, exc_info=True)


def in_use(folder: Path) -> bool:
    """Whether a LIVE process on this machine is using this environment."""
    return folder == _HELD or bool(_live_claims(folder, _IN_USE_PREFIX))


def busy_claims(folder: Path) -> list[str]:
    """Live claims other than this window's own — what makes a delete unsafe NOW.

    What the project-configuration *Reinstall* path asks before it deletes: this
    window is about to give its own claim up, and :func:`destroy` stops this window's
    own engines; everything that is left is a test subprocess, a bench daemon or
    another window's engine that would lose the tree it is executing out of.
    """
    mine = _claim_name(os.getpid(), OPEN)
    return sorted(name for name in _others_claims(folder) if name != mine)


def _others_claims(folder: Path) -> list[str]:
    """Live claims, minus this process's own engines (``destroy`` stops those itself)."""
    own = "." + engine_token()
    return [name for name in _live_claims(folder, _IN_USE_PREFIX) if not name.endswith(own)]


def being_built(folder: Path) -> bool:
    """Whether a LIVE process on this machine is building this environment."""
    return bool(_live_claims(folder, _BUILDING_PREFIX))


# --------------------------------------------------------------------------- #
# Deleting one, provably                                                      #
# --------------------------------------------------------------------------- #


def stop_engines(
    *, venv_folder: Path | None = None, project_root: Path | None = None, wait: bool = False
) -> None:
    """End this app's engine processes running out of ``venv_folder`` (before it is
    deleted under them) or serving ``project_root`` (closed, or its bundle changed;
    ``wait`` until they are gone). Their claims name this app (:func:`engine_token`),
    so the proofs in :func:`destroy` let them through; another window's engine
    refuses the delete."""
    try:
        from procedo.engine_client import engine
    except ImportError:
        return
    engine.stop(venv_folder=venv_folder, project_root=project_root, wait=wait)


def destroy(folder: Path) -> bool:
    """Delete a pool folder, having re-proved right here that it may be deleted.

    Proved again at the call, not inherited from the caller that chose it: directly
    under the pool root, carrying this pool's marker, held by nobody alive but this
    process's own engines, and with no directory link a delete could escape through.
    Only then are those engines stopped, so a refused delete never stops one.

    ``.complete`` goes FIRST, so a delete that dies half-way (Windows keeps a file
    open) leaves a tree nothing can resolve as an environment instead of a gutted one
    a run would launch. Returns whether the folder is gone.
    """
    root = pool_root()
    if folder.parent != root or is_link(folder) or not folder.is_dir():
        # A JUNCTION answers False to ``is_symlink`` and True to ``is_dir`` on Windows,
        # so without ``is_link`` this reads as an ordinary pool folder: the marker
        # and ``.complete`` it finds are the TARGET's, and the target's ``.complete``
        # is what gets unlinked before ``rmtree`` refuses the reparse point.
        logger.warning("Refusing to delete %s: not a folder directly under %s", folder, root)
        return False
    if not marker_key(folder):
        logger.warning("Refusing to delete %s: it carries no %s marker", folder, _KEY_MARKER)
        return False
    if folder == _HELD or _others_claims(folder) or being_built(folder):
        logger.info("Not deleting %s: a live process holds it", folder)
        return False
    link = folder_link_under(folder)  # this repo has already paid for deleting through one
    if link is not None:
        logger.warning("Refusing to delete %s: it contains a directory link (%s)", folder, link)
        return False
    stop_engines(venv_folder=folder)
    try:
        (folder / _COMPLETE_MARKER).unlink(missing_ok=True)
    except OSError:
        logger.warning("Could not un-complete %s; leaving it alone", folder, exc_info=True)
        return False
    try:
        shutil.rmtree(folder)
    except OSError as exc:
        logger.warning("Could not finish deleting %s (%s); the next sweep retries it", folder, exc)
        return False
    return True


# --------------------------------------------------------------------------- #
# Building one                                                                #
# --------------------------------------------------------------------------- #


def begin_build(folder: Path, key: str) -> None:
    """Claim a folder for a build: the folder itself, then our pid, then the marker.

    ``mkdir`` without ``exist_ok`` IS the claim — one syscall the operating system
    makes exactly one caller win, so two windows starting on the same bundle at the
    same instant cannot both run ``python -m venv`` into one tree. The loser gets
    ``FileExistsError``, which :func:`ensure` turns into "somebody is building this".

    The BUILD claim goes in before the key marker, and that order matters: a sweep
    running in the gap sees a folder with no live builder and no marker, decides it
    is debris, and deletes it — and the build then recreates the tree with no marker
    at all, stamps it complete, and leaves an environment nothing can resolve, evict
    or rebuild. With the pid first, the sweep sees a live builder and leaves it be.

    Raises:
        FileExistsError: somebody else claimed it first.
    """
    folder.parent.mkdir(parents=True, exist_ok=True)
    folder.mkdir()
    (folder / _build_claim(os.getpid())).write_text("", encoding="utf-8")
    (folder / _KEY_MARKER).write_text(key, encoding="utf-8")


def finish_build(folder: Path, key: str) -> None:
    """The last step: prove the folder is still ours, stamp it used, then ``.complete``.

    The marker is re-read HERE rather than trusted from :func:`begin_build`: between
    the two lies the whole pip run, and a folder whose marker went missing or changed
    in that time is not the environment this build believes it made. Stamping it
    complete would publish a tree under somebody else's key.

    ``.complete`` is written LAST, after the stamp and after the build claim is
    dropped, because it is the one file resolution looks at — anything written after
    it is written to a folder a run can already be launching out of.

    Raises:
        BuildFailed: the folder no longer carries this build's marker.
    """
    found = marker_key(folder)
    if found != key:
        raise BuildFailed(
            f"{folder} no longer carries this build's marker "
            f"({found or 'none'} instead of {key}); refusing to publish it."
        )
    touch(folder)
    try:
        (folder / _build_claim(os.getpid())).unlink(missing_ok=True)
    except OSError:
        logger.debug("Could not drop the build claim in %s", folder, exc_info=True)
    (folder / _COMPLETE_MARKER).write_text("", encoding="utf-8")


#: The job of the build each thread is running (thread id -> job): where :func:`_run`
#: starts its processes, and what :func:`stop_builds` ends.
_BUILD_JOBS: dict[int, ProcessJob] = {}
_PROGRESS: ContextVar[Callable[[str], None]] = ContextVar(
    "bundle_setup_progress", default=lambda _message: None
)


@contextmanager
def progress_to(callback: Callable[[str], None]) -> Iterator[None]:
    """Send phases from this setup worker to its progress window."""
    token = _PROGRESS.set(callback)
    try:
        yield
    finally:
        _PROGRESS.reset(token)


def stop_builds() -> None:
    """Stop every build this process is running: its processes and theirs end now (this
    waits until they are gone), and each build then removes its own half-built folder
    and raises :class:`BuildStopped`. A build that has not started a process yet is not
    stopped. Called from any thread."""
    for job in list(_BUILD_JOBS.values()):
        job.stop()


def _run(cmd: list[str], *, cwd: Path | None, timeout: int, what: str) -> str:
    logger.info("Running %s: %s", what, " ".join(cmd))
    job = _BUILD_JOBS.get(threading.get_ident())
    if job is None:
        raise RuntimeError(
            f"{what}: a build step runs only inside ensure(), which owns its processes"
        )
    try:
        proc = job.run(cmd, cwd=str(cwd) if cwd is not None else None, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise BuildFailed(f"{what} timed out after {timeout}s.") from exc
    except Stopped as exc:
        raise BuildStopped(stopped_text(STOPPED_REASON)) from exc
    log = f"=== {what} ===\nSTDOUT:\n{proc.stdout.strip()}\n\nSTDERR:\n{proc.stderr.strip()}"
    if proc.returncode != 0:
        raise BuildFailed(f"{what} failed (exit {proc.returncode}):\n{log}")
    return log


def _pip_install(python: Path, bundle_dir: Path, *, progress: Callable[[str], None]) -> str:
    """The bundle's wheels into *python*'s environment, in two passes.

    Two passes for the reason the old project install documented: ``--upgrade``
    alone no-ops when pip sees the version already installed, so a bundle rebuilt at
    the same version would never land. Pass 1 installs ``requirements.txt``: the
    bundle's wheels (``--find-links`` prefers its ``wheels/`` folder) and the exact
    third-party pins, from pip's configured index (the owner: "I will always have
    internet"). Pass 2 force-reinstalls the bundle's own wheel files only; the pins
    are already at their exact versions.
    """
    wheels = wheels_dir(bundle_dir)
    own = [str(w) for w in wheel_files(bundle_dir)]
    pip = [str(python), "-m", "pip", "install"]
    passes = [
        (
            "pip install (resolve)",
            ["--upgrade", "--find-links", str(wheels), "-r", str(requirements_file(bundle_dir))],
        )
    ]
    if own:  # pip refuses an install that names nothing
        passes.append(
            (
                "pip install (force-reinstall bundle wheels)",
                ["--force-reinstall", "--no-deps", *own],
            )
        )
    progress("Installing project packages…")
    log = ""
    for what, args in passes:
        # cwd is the wheels dir so a ``--find-links .`` inside requirements.txt resolves.
        log += _run(pip + args, cwd=wheels, timeout=_PIP_TIMEOUT_S, what=what) + "\n\n"
    return log


def build_into(venv_dir: Path, bundle_dir: Path, progress: Callable[[str], None]) -> str:
    """Create the environment and install the bundle's wheels into it.

    The one seam the pool's own tests replace: everything else about the pool — the
    key, the marker protocol, the LRU, the re-proofs — is exercised against a builder
    that writes a stub interpreter in milliseconds, because a real ``python -m venv``
    on this drive costs tens of seconds and proves nothing about any of it.

    One pip install call: the owner always has a network, so there is no offline
    pass to try first and no online pass to fall back to — a failure here is simply
    reported, whatever it names.
    """
    job = _BUILD_JOBS[threading.get_ident()]
    tag = bundle_python(bundle_dir)
    try:
        progress("Preparing required runtime…")
        runtime = python_runtime.ensure(tag, job=job, progress=progress)
        progress("Creating environment…")
        log = _run(
            [str(runtime), "-m", "venv", str(venv_dir)],
            cwd=None,
            timeout=_VENV_TIMEOUT_S,
            what="python -m venv",
        )
        python = project_local_state.venv_python(venv_dir)
        base = python_runtime.verify(python, tag, job=job)
        if not base.samefile(runtime):
            raise BuildFailed("The environment was created with a different runtime.")
        atomic_write.write_json(venv_dir.parent / _BASE_PYTHON, {"executable": str(base)})
        return log + "\n\n" + _pip_install(python, bundle_dir, progress=progress)
    except Stopped as exc:
        raise BuildStopped(stopped_text(STOPPED_REASON)) from exc
    except (python_runtime.RuntimeSetupError, OSError) as exc:
        raise BuildFailed(str(exc)) from exc


def _write_record(folder: Path, bundle_dir: Path, wheel_count: int) -> None:
    bundle_id, version = bundle_ref(bundle_dir)
    atomic_write.write_json(
        install_record_in(folder),
        {
            "id": bundle_id,
            "version": version,
            "wheel_count": wheel_count,
            "installed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    )


def ensure(bundle_dir: Path | str, *, progress: Callable[[str], None] | None = None) -> Ensured:
    """Make sure this machine has the environment for *bundle_dir*, building it if not.

    The whole point of the pool: a second project on a bundle this machine already
    built for costs one stat sweep and no pip at all.

    Raises:
        BuildFailed: the bundle is not installable (none, or built for another
            Python), or venv/pip refused.
        BuildInProgress: another window on this machine is building this very key.
    """
    bundle_dir = Path(bundle_dir)
    say = progress or _PROGRESS.get()
    key = venv_key(bundle_dir)
    if key is None:
        missing = missing_wheels(bundle_dir)
        raise BuildFailed(
            f"{NO_BUNDLE}\n\nMissing from {wheels_dir(bundle_dir)}: {', '.join(missing)}"
            if missing
            else NO_BUNDLE
        )
    bundle_id, version = bundle_ref(bundle_dir)
    built_for = bundle_python(bundle_dir)
    try:
        python_runtime.validate_tag(built_for)
    except python_runtime.RuntimeSetupError as exc:
        raise BuildFailed(str(exc)) from exc
    wheel_count = len(wheel_files(bundle_dir))

    folder = folder_for_key(bundle_dir, key)
    if _usable(folder) and marker_key(folder) == key:
        touch(folder)
        return Ensured(folder, key, bundle_id, version, wheel_count, built=False, pip_log="")
    if folder.exists():
        # Not complete. Either somebody is building it right now, or a build died.
        if being_built(folder):
            raise BuildInProgress(
                "Another window on this computer is building this Python environment. "
                "Wait for it to finish and open the project again."
            )
        if not destroy(folder):
            raise BuildInProgress(
                f"A half-built Python environment at {folder} could not be cleared. "
                "Close any window using it and try again."
            )

    thread = threading.get_ident()
    # The job exists BEFORE the folder is claimed: a job that cannot be made must not
    # leave a live ``.building.<pid>`` behind.
    with ProcessJob() as job:  # nothing the build starts outlives it
        try:
            begin_build(folder, key)
        except FileExistsError as exc:
            # Somebody claimed it between the check above and the mkdir. Their build is
            # the one that will exist; ours must not start on top of it.
            if _usable(folder) and marker_key(folder) == key:
                touch(folder)
                return Ensured(
                    folder, key, bundle_id, version, wheel_count, built=False, pip_log=""
                )
            raise BuildInProgress(
                "Another window on this computer is building this Python environment. "
                "Wait for it to finish and open the project again."
            ) from exc
        _BUILD_JOBS[thread] = job
        try:
            pip_log = build_into(venv_in(folder), bundle_dir, say)
            job.publish(lambda: finish_build(folder, key))
        except BaseException as exc:
            # Our own half-built folder: it carries our marker and our pid, so the proof
            # destroy() insists on is satisfied once nothing of the build holds a file in
            # it. If it cannot go now, it is still invisible to resolution (no .complete)
            # and the next sweep retries it.
            job.stop()
            try:
                (folder / _build_claim(os.getpid())).unlink(missing_ok=True)
            except OSError:
                pass
            destroy(folder)
            if isinstance(exc, Stopped):
                raise BuildStopped(stopped_text(STOPPED_REASON)) from exc
            raise
        finally:
            del _BUILD_JOBS[thread]
    # AFTER ``.complete``, and never fatal: the record is advisory now. Nothing
    # decides anything from its contents — drift asks the pool, not the record —
    # it is a bundle name inside an opaquely-named folder and an mtime the editor's
    # bundle-stamp watch and the display snapshot use as a change signal. Throwing a
    # three-minute pip run away because a metadata file could not be written is a
    # trade nobody would make, and it is what writing it inside the build did.
    try:
        _write_record(folder, bundle_dir, wheel_count)
    except OSError:
        logger.warning(
            "Environment built, but its install record could not be written at %s",
            install_record_in(folder),
            exc_info=True,
        )
    # The pool just grew: this is the moment the limit is applied. Here rather
    # than at the call site because every builder runs off the GUI thread already,
    # and an eviction that fails is never a failed install.
    try:
        evict()
    except Exception:  # noqa: BLE001 — a full pool is not a broken environment
        logger.warning("Could not apply the environment limit", exc_info=True)
    return Ensured(folder, key, bundle_id, version, wheel_count, built=True, pip_log=pip_log)


# --------------------------------------------------------------------------- #
# Keeping the pool bounded                                                    #
# --------------------------------------------------------------------------- #


def complete_folders() -> list[Path]:
    """Every finished environment in the pool, most recently used first."""
    folders = [f for f in _pool_folders() if is_complete(f) and marker_key(f)]
    return sorted(folders, key=lambda f: (-last_used(f), f.name))


def usage() -> tuple[int, int]:
    """``(environments, bytes)``. Walks the pool — never call it on the GUI thread."""
    total = 0
    folders = complete_folders()
    for folder in folders:
        for current, _dirs, files in os.walk(folder):
            for name in files:
                try:
                    total += os.stat(os.path.join(current, name)).st_size
                except OSError:
                    pass
    return len(folders), total


#: When this process started. The start-up sweep runs BEFORE the first project is
#: open, so anything stamped since then is being taken up by this very session and is
#: not the sweep's to reclaim — a claim file that has not been written yet cannot say
#: so, and the race is otherwise won by whichever thread got there first.
_STARTED = time.time()


def evict(keep: int | None = None, *, used_before: float | None = None) -> list[Path]:
    """Delete the least recently used environments past the limit. Returns what went.

    Never the one this process holds, never one a live process on this machine has
    open or is building, and never one :func:`destroy` cannot prove is the pool's. A
    folder that survives for any of those reasons is simply left for the next sweep.

    *used_before* additionally spares anything stamped since that moment. The start-up
    sweep passes its own start time: it runs in parallel with the window opening a
    project, and "this one was touched after I began" is the only thing that can be
    known about an environment whose claim file is still being written.
    """
    limit = app_settings.get_max_venvs() if keep is None else max(1, keep)
    removed = []
    for folder in complete_folders()[limit:]:
        if used_before is not None and last_used(folder) >= used_before:
            logger.info("Not deleting %s: it has been used since this sweep began", folder)
            continue
        if destroy(folder):
            removed.append(folder)
    return removed


def _clear_abandoned_builds() -> list[Path]:
    """Remove pool folders that were never finished and have no live builder."""
    removed = []
    for folder in _pool_folders():
        if is_complete(folder) or being_built(folder) or in_use(folder):
            continue
        if destroy(folder):
            removed.append(folder)
    return removed


def sweep() -> None:
    """Everything the pool owes the disk, off the GUI thread, once at start-up.

    It runs in parallel with the window opening its first project, so the eviction it
    does is the CONSERVATIVE one: anything stamped since this process began is being
    taken up right now and is left alone (:data:`_STARTED`). The limit is applied in
    full after every build, when the project being opened has already stamped its own.

    Also removes the per-project ``.venv`` folders the one-venv-per-project layout left
    behind. They are app-owned, re-creatable and can be hundreds of megabytes each —
    freeing that space is the whole point of this change — and nothing reads them any
    more. Everything else in a project's state folder (its media cache, the sync's
    origin record and trash, this machine's working copies) is untouched.
    """
    try:
        _clear_abandoned_builds()
        evict(used_before=_STARTED)
        project_local_state.remove_legacy_venvs()
    except Exception:  # noqa: BLE001 — a background sweep may never take the app down
        logger.warning("Virtual-environment sweep failed", exc_info=True)
