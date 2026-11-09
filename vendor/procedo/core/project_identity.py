"""``project_id`` — minted once per project on open, inside a lock, never overwritten.

Standalone (no ``config_manager`` import) so ``ensure_project_config`` can
call it without a cycle. A copied project must NOT inherit the source's id:
the config seeders transfer explicit sections only, so an id never travels.
A project that cannot be written (read-only share) simply has no id yet.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import socket
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from . import atomic_write, paths

logger = logging.getLogger(__name__)

_CONFIG = Path("config") / "config.json"
LOCK_NAME = ".project_id.lock"
_LOCK_WAIT_S = 30.0
#: The lock is taken on a byte PAST everything the owner record occupies, so waiting
#: writers can still read that record: a Windows byte-range lock refuses reads of the
#: locked region, while ``flock`` locks no bytes at all.
_LOCK_BYTE = 1 << 20
#: The only errnos that mean "another process holds it". Everything else an OS lock
#: call raises means the filesystem does not implement locking (:func:`lock_file`).
_HELD_ERRNOS = frozenset(
    getattr(errno, name)
    for name in ("EACCES", "EAGAIN", "EWOULDBLOCK", "EDEADLK")
    if hasattr(errno, name)
)


class ConfigLockBusy(Exception):
    """Another process is holding this project's configuration lock.

    Deliberately NOT an ``OSError``. ``TimeoutError`` is one, and every caller here
    catches ``OSError`` to mean "this project cannot be written; open it anyway" — so
    a lock somebody was actively HOLDING was swallowed into a log line, and the open
    went on to skip the gap-fill, the identity mint and ``ensure_spec`` in silence.
    Typing it out of that family is the fix; no caller has to remember the trap.
    """


def _load(path: Path) -> dict | None:
    """``config.json`` as a dict: ``{}`` when absent, ``None`` when unreadable or
    not a JSON object — such a file is never rewritten."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


#: ``config/config.json`` is not there: the folder is not a project.
MISSING = "missing"
#: It is there and this machine cannot read it. NOT the same answer as absent — the
#: first-open writers used to treat both as "no config yet" and write one, which is
#: how picking ``Documents`` by mistake turned it into a project.
UNREADABLE = "unreadable"
PRESENT = "present"


def config_state(project_path: Path) -> tuple[str, str]:
    """What ``config/config.json`` is for this folder: ``(state, reason)``.

    THE rule for "is this a project": it has one. There are no old projects to be
    kind to, so a folder without it is refused rather than seeded — and a folder
    whose config cannot be READ is refused as its own thing, because offering to
    create a project over an unreadable one is how work gets overwritten.
    """
    try:
        (Path(project_path) / _CONFIG).read_bytes()
    except FileNotFoundError:
        return MISSING, ""
    except OSError as exc:
        return UNREADABLE, str(exc)
    return PRESENT, ""


def folder_leftovers(folder: Path, *, action: str, where: str) -> str:
    """What an interrupted *action* (``"Apply"``, ``"Save"``) left next to *folder*
    (``""``: nothing): the previous contents first, then the copy that was being swapped in."""
    previous, applied = paths.swap_leftovers(folder)
    parts = [
        f"{', '.join(str(p) for p in folders)} {'is' if len(folders) == 1 else 'are'} {what}"
        for folders, what in (
            (previous, "the previous configuration"),
            (applied, "the one that was being applied"),
        )
        if folders
    ]
    return f"A previous {action} left folders next to {where}: {'; '.join(parts)}." if parts else ""


def config_leftovers(project_path: Path) -> str:
    """What an Apply of Project Configuration left next to ``config/`` (``""``: nothing)."""
    return folder_leftovers(
        Path(project_path) / _CONFIG.parent, action="Apply", where="the project's config folder"
    )


def project_open_problem(project_path: Path) -> str | None:
    """Why a project must not be opened (``None``: it may). It has no
    ``config/config.json`` at all, or one that cannot be read, or one that exists and
    is not a JSON object — every first-open writer would replace it, and a new
    ``project_id`` would hide and split a shared project's evidence — or its saved
    ``evidence_store`` entry is not valid (the store could not be opened)."""
    path = Path(project_path) / _CONFIG
    tail = "fix it or restore it — the project was not opened."
    state, why = config_state(project_path)
    if state == MISSING:
        refusal = f"{project_path} is not a project (no {_CONFIG.as_posix()})."
        if saved := config_leftovers(project_path):  # a replace of config/ failed half-way
            aside = (
                " (move the 'config' folder that is there aside first)"
                if (Path(project_path) / _CONFIG.parent).exists()
                else ""
            )
            refusal += (
                f"\n\n{saved} Rename the previous configuration to 'config' to recover it{aside}."
            )
        return refusal
    if state == UNREADABLE:
        return f"cannot read {project_path}: {why}"
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return f"cannot read {project_path}: {exc}"
    if raw.startswith(b"\xef\xbb\xbf"):
        return f"{path}\n\nconfig.json starts with a UTF-8 BOM (byte-order mark): save it without the BOM — {tail}"
    try:
        data = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError:
        return f"{path}\n\nconfig.json is not UTF-8 text: {tail}"
    except json.JSONDecodeError as exc:
        return (
            f"{path}\n\nconfig.json is not valid JSON (line {exc.lineno}, col {exc.colno}): {tail}"
        )
    if not isinstance(data, dict):
        return f"{path}\n\nconfig.json does not hold a JSON object: {tail}"
    spec = data.get("evidence_store")
    if isinstance(spec, dict):
        from . import evidence_config  # lazy: evidence_config's imports reach this module

        problems = evidence_config.validate_spec(spec, Path(project_path))
        if problems:
            return (
                f"{path}\n\nThe evidence_store entry is not valid ({'; '.join(problems)}): {tail}"
            )
    return None


def read_project_id(project_path: Path) -> str | None:
    pid = (_load(project_path / _CONFIG) or {}).get("project_id")
    return pid if isinstance(pid, str) and pid else None


def lock_file(fd: int, *, take: bool, strict: bool = False) -> bool:
    """Take or drop the operating system's exclusive lock on ``fd``; ``False`` when
    another process is holding it.

    Only :data:`_HELD_ERRNOS` mean held. Every other ``OSError`` means this filesystem
    does not implement locking at all — ``fcntl.flock`` answers ``ENOLCK``/``EOPNOTSUPP``
    on some NFS and CIFS mounts, and ``msvcrt.locking`` fails the same way on a share
    with no byte-range locking. Reading those as "held" made the project UNOPENABLE
    there: 30 s of frozen GUI thread, then a modal blaming another bench, every single
    time. So an unsupported lock is logged and the caller proceeds UNLOCKED — the same
    degradation the create-if-absent marker gave, which is worse than serialized and
    far better than refused.
    """
    try:
        if os.name == "nt":
            import msvcrt

            os.lseek(fd, _LOCK_BYTE, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK if take else msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, (fcntl.LOCK_EX | fcntl.LOCK_NB) if take else fcntl.LOCK_UN)
    except OSError as exc:
        if exc.errno in _HELD_ERRNOS:
            return False
        if strict:
            raise
        logger.warning(
            "This filesystem cannot lock %r (%s); continuing without the configuration "
            "lock. Two benches initializing this project at the same moment are not "
            "serialized here.",
            LOCK_NAME,
            exc,
        )
    return True


def _lock_owner(lock: Path) -> str:
    """Who holds it, for the message a human reads — ``""`` when nobody said."""
    try:
        record = json.loads(lock.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        return ""
    return f" (held by {record['host']} pid {record.get('pid')})" if record.get("host") else ""


def _acquire_lock(lock: Path, *, strict: bool = False, wait_s: float | None = None) -> int:
    """An exclusive lock held by the OPERATING SYSTEM, on a file that is never removed.

    The lock is the kernel's, not the file's existence, so it is dropped when this
    process exits — however it exits. That buys "a lock is never stolen by age" for
    nothing and, unlike a create-if-absent marker, it cannot leak: one bench that died
    mid-open used to freeze EVERY later open of that project by 30 s, for good, and
    silently skip the gap-fill and the identity mint behind it. The file itself stays
    so that every process locks the SAME object — removing it on release would let a
    newcomer create a second file and lock that one instead.

    :class:`ConfigLockBusy` means a live holder, and reaches the user. ``wait_s`` is how
    long a held lock is waited for (``None``: :data:`_LOCK_WAIT_S`, ``0``: not at all).
    """
    deadline = time.monotonic() + (_LOCK_WAIT_S if wait_s is None else wait_s)
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0), 0o644)
    while True:
        try:
            acquired = lock_file(fd, take=True, strict=True) if strict else lock_file(fd, take=True)
        except BaseException:
            os.close(fd)
            raise
        if acquired:
            return fd
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            os.close(fd)
            raise ConfigLockBusy(
                f"Timed out waiting for project configuration lock {lock}"
                f"{_lock_owner(lock)}. Another window or another bench is still "
                "initializing this project; open it again once that has finished."
            )
        time.sleep(min(0.05, remaining))


def _mint(path: Path) -> str:
    data = _load(path)
    if data is None:  # never replace a config.json that does not parse
        raise OSError(f"{path} is not a JSON object; no project_id minted")
    minted = str(uuid.uuid4())
    data["project_id"] = minted
    atomic_write.write_json(path, data)
    return minted


def _record_owner(fd: int) -> None:
    """Say who holds the lock, in the bytes before the locked one (:func:`_lock_owner`)."""
    owner = {"host": socket.gethostname(), "pid": os.getpid(), "acquired_at": time.time()}
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, json.dumps(owner).encode("utf-8"))


def _release_lock(fd: int) -> None:
    lock_file(fd, take=False)
    os.close(fd)


@contextmanager
def project_config_lock(project_path: Path, *, strict: bool = False) -> Iterator[None]:
    """Serialize first-open config updates and identity minting across benches."""
    path = project_path / _CONFIG
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_name(LOCK_NAME)
    fd = _acquire_lock(lock, strict=True) if strict else _acquire_lock(lock)
    try:
        _record_owner(fd)
        yield
    finally:
        _release_lock(fd)


def config_lock_problem(project_path: Path) -> str:
    """Who is holding this project's configuration lock (``""`` when nobody is).

    The lock, taken and released, and NOTHING ELSE — no gap-fill, no identity, not a
    byte of ``config/config.json``. It exists so a caller can find out whether a
    project can be initialized *before* it commits to anything: opening B used to
    ask by running the whole gap-fill, and that gap-fill rewrote B's
    ``config/config.json`` with a fresh mtime before the pull had run, so the next
    pass read it as "changed locally, newer" and pushed the app's own defaults over
    another bench's edit.

    A filesystem that cannot lock at all answers ``""`` — the same degradation
    :func:`lock_file` documents, and the same one the gap-fill itself takes.
    """
    try:
        with project_config_lock(Path(project_path)):
            return ""
    except ConfigLockBusy as exc:
        return str(exc)
    except OSError as exc:  # the lock file itself is unreachable: not somebody holding it
        logger.warning("config lock could not be probed for %s (%s)", project_path, exc)
        return ""


def ensure_project_id_locked(project_path: Path) -> str:
    """Mint/read the identity while the caller holds :func:`project_config_lock`."""
    minted = read_project_id(project_path) or _mint(project_path / _CONFIG)
    winner = read_project_id(project_path) or minted
    if winner != minted:
        logger.info("project_id race: adopting %s over %s", winner, minted)
    return winner


def new_project_id(project_path: Path) -> str:
    """A NEW identity for the project at ``project_path``, in place of the one it has — a copy made
    by hand (Explorer) carries its original's id, and the one-time converter gives the copy its own
    (procedo.core.convert_records); the original keeps its id. Raises OSError when the config
    cannot be written."""
    with project_config_lock(project_path):
        return _mint(project_path / _CONFIG)


def ensure_project_id(project_path: Path) -> str:
    """Return the project's id, minting it in a critical section when absent.

    Re-reads inside the lock, so two benches opening a shared project converge
    on one id; the read-back after the write adopts any lockless writer that
    landed in between. Returns ``""`` when the config cannot be written.

    A HELD lock is not "no id" and is never swallowed (:class:`ConfigLockBusy` is not
    an ``OSError``): the identity may be minted a moment from now, and opening without
    one would give this project a second identity on the shared store — and no store
    at all, since the shared layout refuses an empty ``project_id``."""
    existing = read_project_id(project_path)
    if existing:
        return existing
    try:
        with project_config_lock(project_path):
            return ensure_project_id_locked(project_path)
    except OSError as exc:
        logger.warning("project_id not minted for %s (%s); opening without one", project_path, exc)
        return ""
