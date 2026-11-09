"""Shared path helpers — single source of truth, for Bench and Studio alike.

``walk_up_for`` finds an in-tree submodule from discovery code (``bundle_generator``
finding the rules_packager submodule + its script).
``is_link`` is the one link check (``links_under`` / ``folder_link_under`` the one walk for
links in a tree) and ``rmtree`` the one tree delete, both valid on every
supported Python (3.10+); ``swap_into_place`` the one folder replace; ``sha256_file``
the one file hash; ``exists_quiet`` / ``is_file_quiet`` / ``is_dir_quiet`` the display/open
checks a Qt slot makes on a path that can live on a share, ``unreachable`` the guard of a write;
``inside`` the one path-under-a-folder test by spelling; ``universal`` / ``place_key`` / ``same_place``
/ ``inside_place`` the one mapped-drive-to-share spelling (``warm_universal_in_background`` asks every
letter at start, off the GUI thread), the one key of a place, the one compare
of two recorded spellings and the one path-under-a-folder test across them;
``is_windows_reserved`` the one Windows device-name test (two levels) and ``is_safe_name``
the one portable file-name test.
"""

from __future__ import annotations

import hashlib
import logging
import ntpath
import os
import re
import shutil
import stat
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: ``copytree(ignore=...)`` that leaves Python bytecode behind, so a copied tool, wizard or
#: skill never carries a ``__pycache__`` (the one definition; every folder copy uses it).
SKIP_BYTECODE = shutil.ignore_patterns("__pycache__", "*.pyc")

#: Names Windows takes as a device, whatever the extension and case (``con.json`` is the
#: console too). What an evidence key refuses: a store may already hold any other name.
WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {prefix + digit for prefix in ("COM", "LPT") for digit in "123456789"}
)
#: :data:`WINDOWS_RESERVED_NAMES` plus the console buffers and the superscript ports newer
#: Windows also reserves — for names that come from an archive or name a new template or
#: bundle, where nothing already stored is at stake.
WINDOWS_RESERVED_NAMES_STRICT = (
    WINDOWS_RESERVED_NAMES
    | {"CONIN$", "CONOUT$"}
    | {prefix + digit for prefix in ("COM", "LPT") for digit in "¹²³"}
)


def is_windows_reserved(name: str, *, strict: bool = False) -> bool:
    """*name* is a Windows device name: its part before the first dot, in any case, is
    in :data:`WINDOWS_RESERVED_NAMES` (``strict``: :data:`WINDOWS_RESERVED_NAMES_STRICT`)."""
    names = WINDOWS_RESERVED_NAMES_STRICT if strict else WINDOWS_RESERVED_NAMES
    return name.split(".")[0].upper() in names


def is_safe_name(name: object) -> bool:
    """*name* is one file or folder name every supported OS creates as spelled: a
    non-empty string, not ``.``/``..``, no trailing dot or space, no control character
    or any of ``<>:"/\\|?*``, and not a Windows device name (the strict set)."""
    return (
        isinstance(name, str)
        and bool(name)
        and name not in {".", ".."}
        and not name.endswith((".", " "))
        and not any(ord(c) < 32 or c in '<>:"/\\|?*' for c in name)
        and not is_windows_reserved(name, strict=True)
    )


#: The reparse tags of a symlink and of a junction or volume mount point.
_LINK_TAGS = {
    getattr(stat, "IO_REPARSE_TAG_SYMLINK", -1),
    getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", -1),
}


def is_link(path: str | os.PathLike[str]) -> bool:
    """Whether *path* is a link — a symlink, a Windows junction or a volume mount point.

    ``Path.is_symlink`` misses a junction (it walks like a folder), and
    ``os.path.isjunction`` / ``Path.is_junction`` / ``DirEntry.is_junction`` only
    exist from Python 3.12 while the project supports 3.10. Other reparse points
    (cloud-file placeholders such as OneDrive Files-On-Demand, deduplicated files)
    are regular. A path that cannot be read is not a link, as ``os.path.islink``
    answers.
    """
    try:
        st = os.lstat(path)
    except (OSError, ValueError):
        return False
    return stat.S_ISLNK(st.st_mode) or getattr(st, "st_reparse_tag", 0) in _LINK_TAGS


def links_under(folder: Path) -> Iterator[tuple[Path, bool]]:
    """Every link (:func:`is_link`) under *folder*, with whether it leads to a folder,
    never looking inside one. Raises ``OSError`` for a folder it cannot list."""
    stack = [folder]
    while stack:
        current = stack.pop()
        for entry in list(os.scandir(current)):
            # A followed ``is_dir``: a POSIX folder symlink answers False without following.
            # A plain file is passed without an ``lstat`` (a pool tree holds thousands).
            to_folder = entry.is_dir()
            if not (to_folder or entry.is_symlink()):
                continue
            if is_link(entry.path):
                yield Path(entry.path), to_folder
            elif to_folder:
                stack.append(Path(entry.path))


def folder_link_under(folder: Path) -> Path | None:
    """The first folder link under *folder* — a door out of the tree a delete could go
    through (a file link is harmless: removing it unlinks the name) — or a folder that
    cannot be listed, so a delete refuses rather than going blind. ``None``: neither."""
    try:
        return next((link for link, to_folder in links_under(folder) if to_folder), None)
    except OSError as exc:
        return Path(exc.filename) if exc.filename else folder


def plain_file(folder: Path, name: str) -> Path | None:
    """``folder/name`` (*name* a ``/``-separated path) when it is a regular file inside
    *folder*, reached through no link or junction; ``None`` otherwise. What may be read,
    embedded or removed as one of that folder's own files: asked right before doing so."""
    parts = name.split("/")
    if "\\" in name or ":" in name or any(p in ("", ".", "..") for p in parts):
        return None
    path = Path(folder)
    for part in parts:
        path = path / part
        if is_link(path):
            return None
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return None
        return path if path.resolve().is_relative_to(Path(folder).resolve()) else None
    except (OSError, ValueError):
        return None


def is_absent(path: str | os.PathLike[str]) -> bool:
    """Whether *path* is certainly not there: ``stat`` (Win32) says not found. Raises when
    it cannot tell (a share that is down: WinError 53/67 — Python maps those to ENOENT too,
    so ``exists()`` and open() would call it gone)."""
    try:
        os.stat(path)
    except FileNotFoundError as exc:
        if getattr(exc, "winerror", None) in (None, 2, 3):  # the file (or folder) is absent
            return True
        raise  # 53, 67...: the share, not the file
    return False


#: A drive-letter path: its letter, then a separator or nothing.
_DRIVE = re.compile(r"^([A-Za-z]:)(?:[\\/]|$)")
#: Each drive letter's ``\\server\share`` — or, when it is no network drive or could not be
#: asked, the time it was asked. Listings call :func:`universal` on the GUI thread, so a share
#: found is asked once per session and a miss again at most every :data:`_RETRY_S` seconds: a
#: drive mapped or reconnected later is seen without restarting the app.
_REMOTES: dict[str, str | float] = {}
_RETRY_S = 30.0


def _remote_name(drive: str) -> str | None:
    """The ``\\\\server\\share`` the mapped drive ``drive`` (``"G:"``) stands for; ``None``
    off Windows, for a local drive, or when the network provider cannot say. Never raises."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        get = ctypes.WinDLL("mpr").WNetGetConnectionW
        get.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        get.restype = wintypes.DWORD
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        # 1201 ERROR_CONNECTION_UNAVAIL: a remembered drive not reconnected yet. The Win32 doc
        # does not say the name is filled then, so only a filled ``\\`` name is taken.
        if get(drive, buf, ctypes.byref(size)) not in (0, 1201):
            return None
    except (OSError, AttributeError, ValueError):
        return None
    return buf.value if buf.value.startswith("\\\\") else None


def _asked(letter: str) -> str | float:
    """*letter*'s :data:`_REMOTES` entry, asked of the network providers first when it has
    none or a miss is due again."""
    known = _REMOTES.get(letter)
    if known is None or (isinstance(known, float) and time.monotonic() - known >= _RETRY_S):
        remote = _remote_name(letter)
        known = _REMOTES[letter] = remote if remote is not None else time.monotonic()
    return known


def universal(path: str | os.PathLike[str]) -> str:
    """*path* in its ``\\\\server\\share\\...`` form when it starts with a mapped drive letter;
    every other path (local drive, UNC, POSIX, a drive that cannot be resolved) as typed.
    No disk access; each drive letter is resolved once per session (one that does not
    resolve is asked again at most every :data:`_RETRY_S` seconds). Never raises."""
    text = os.fspath(path)
    match = _DRIVE.match(text)
    if match is None:
        return text
    known = _asked(match.group(1).upper())
    return text if isinstance(known, float) else known.rstrip("\\/") + text[2:]


def _drive_letters() -> list[str]:
    """The drive letters this PC has (``["C:", "G:"]``), a remembered network drive that is
    not reconnected included; none off Windows."""
    if sys.platform != "win32":
        return []
    import ctypes

    mask = ctypes.windll.kernel32.GetLogicalDrives()  # type: ignore[attr-defined]
    return [f"{chr(ord('A') + bit)}:" for bit in range(26) if mask >> bit & 1]


def warm_universal_in_background() -> None:
    """Ask the network providers about every drive letter now, on a daemon thread, so the
    first :func:`universal` on the GUI thread finds its letter answered: the ask can take
    seconds (``WNetGetConnectionW`` waits for every provider). Call it once at start."""

    def warm() -> None:
        for letter in _drive_letters():
            _asked(letter)

    threading.Thread(target=warm, name="drive-names-warm", daemon=True).start()


def place_key(text: str) -> str:
    """The one spelling every recorded spelling of a place shares: :func:`universal`,
    then — for a Windows path only (a drive letter or a ``\\\\`` / ``//`` share) — case
    and separators folded and a trailing separator dropped. Anything else (``ftps://``,
    ``https://``, a POSIX path: case may matter there) as typed."""
    text = universal(text)
    if _DRIVE.match(text) or text.startswith(("\\\\", "//")):
        return ntpath.normcase(text).rstrip("\\")
    return text


def same_place(a: object, b: object) -> bool:
    """Whether two recorded path spellings name one place: equal, or one
    :func:`place_key` (a mapped drive and its share, in any case). Anything but two
    strings compares by ``==`` only."""
    if a == b:
        return True
    return isinstance(a, str) and isinstance(b, str) and place_key(a) == place_key(b)


def inside(path: str | os.PathLike[str], folder: str | os.PathLike[str]) -> bool:
    """Whether *path* is *folder* or lies under it, by spelling: both made absolute and
    normalized (``..``, separators, and case on Windows), never resolved.

    No filesystem access, so it can be asked about a share that is no longer there, on the
    GUI thread, on every edit. A caller that needs identity through links resolves both
    paths first. ``C:\\Proj2`` is not inside ``C:\\Proj``; a drive or share root holds
    every path on it.
    """
    child = os.path.normcase(os.path.abspath(path))
    parent = os.path.normcase(os.path.abspath(folder))
    return child == parent or child.startswith(parent.rstrip("\\/") + os.sep)


def inside_place(path: str | os.PathLike[str], folder: str | os.PathLike[str]) -> bool:
    """:func:`inside` once both are in their :func:`universal` spelling: a mapped drive and
    its share name one folder. No disk access."""
    return inside(universal(path), universal(folder))


def walk_up_for(rel_path: Path, *, anchor: Path, max_levels: int = 8) -> Path | None:
    """Find ``<some_ancestor>/{rel_path}`` by walking up from ``anchor``.

    Used to locate sibling repo paths (in-tree submodule files) when
    the importing module doesn't know its absolute repo root. Returns
    None if not found within ``max_levels`` parents.

    ``anchor`` is typically the calling module's ``__file__``; the
    walk starts at its parent. Passing the path explicitly (rather
    than discovering ``__file__`` here) keeps the helper testable
    against synthetic trees.
    """
    cur = anchor.resolve()
    for _ in range(max_levels):
        candidate = cur.parent / rel_path
        if candidate.exists():
            return candidate
        if cur.parent == cur:
            break
        cur = cur.parent
    return None


def _quiet(check: Callable[[Path], bool], path: Path | str) -> bool:
    """*check* on *path*, False when the volume cannot be asked. Python 3.13 re-raises
    Windows errors like ``WinError 64`` (a dropped share) from ``exists()`` and friends; a
    Qt slot must read that as "not there", not die of it."""
    try:
        return check(Path(path))
    except OSError as exc:
        logger.debug("cannot check %s: %s", path, exc)
        return False


def unreachable(path: Path | str) -> str | None:
    """Why *path*'s volume cannot be asked (a dropped share), or None when it answers.
    What a write, overwrite or refuse-guard checks first: there "absent" is the dangerous
    answer, so the action stops with this sentence instead."""
    try:
        Path(path).exists()
    except OSError as exc:
        return f"{path} cannot be reached ({exc.strerror or exc}). Check the network connection and try again."
    return None


def exists_quiet(path: Path | str) -> bool:
    """``Path.exists()`` that reads an unreachable volume as absent."""
    return _quiet(Path.exists, path)


def is_file_quiet(path: Path | str) -> bool:
    """``Path.is_file()`` that reads an unreachable volume as absent."""
    return _quiet(Path.is_file, path)


def is_dir_quiet(path: Path | str) -> bool:
    """``Path.is_dir()`` that reads an unreachable volume as absent."""
    return _quiet(Path.is_dir, path)


def rmtree(path: Path, *, ignore_errors: bool = False) -> None:
    """``shutil.rmtree`` that clears a read-only attribute and retries a failed removal
    once — Windows refuses to delete a read-only file, and a test script may write one
    into a payload (its outbox entry would then block every later upload) or a test
    folder Studio deletes."""

    def writable_then_retry(func: Callable[..., Any], p: str, exc: BaseException) -> None:
        try:
            if func not in (os.unlink, os.rmdir):
                raise exc
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            if not ignore_errors:
                raise

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=writable_then_retry)
    else:  # ``onexc`` arrived in 3.12; the project supports 3.10+ (``onerror`` passes exc_info)
        shutil.rmtree(path, onerror=lambda func, p, info: writable_then_retry(func, p, info[1]))


def sha256_file(path: str | os.PathLike[str]) -> str:
    """The sha256 hex digest of the file at *path*, read in chunks (never whole into memory)."""
    with open(path, "rb") as f:
        if sys.version_info >= (3, 11):
            return hashlib.file_digest(f, "sha256").hexdigest()
        h = hashlib.sha256()  # ``file_digest`` arrived in 3.11; the project supports 3.10+
        for chunk in iter(lambda: f.read(1 << 18), b""):
            h.update(chunk)
        return h.hexdigest()


class SwapIncomplete(OSError):
    """:func:`swap_into_place` moved the target aside and could neither move the new
    content in nor put the old back. Its message names the folders that hold the
    previous and the new content and how to recover."""


def _siblings(target: Path, pattern: str) -> list[Path]:
    """Folders beside *target* whose name fully matches *pattern* (never a link)."""
    regex = re.compile(pattern)
    try:
        siblings = list(target.parent.iterdir())
    except OSError:
        return []
    return sorted(p for p in siblings if regex.fullmatch(p.name) and p.is_dir() and not is_link(p))


def _backups(target: Path) -> list[Path]:
    """The backups :func:`swap_into_place` makes of *target*: ``.<name>.old-<8 hex>``."""
    return _siblings(target, rf"\.{re.escape(target.name)}\.old-[0-9a-f]{{8}}")


def new_copy_folder(target: Path) -> Path:
    """A new, empty folder beside *target* to build its replacement in (``.<name>.<8>.new``)."""
    return Path(tempfile.mkdtemp(prefix=f".{target.name}.", suffix=".new", dir=target.parent))


def swap_leftovers(target: Path) -> tuple[list[Path], list[Path]]:
    """What swaps of *target* left beside it, for a person to recover from: the
    previous contents (``.<name>.old-*``) and the new copies that were being swapped
    in (``.<name>.*.new``). Nothing deletes them but that person."""
    return _backups(target), _siblings(target, rf"\.{re.escape(target.name)}\.[a-z0-9_]{{8}}\.new")


def swap_into_place(source: Path, target: Path) -> None:
    """Atomically replace ``target`` with ``source``.

    When ``target`` exists, the old content is renamed to a sibling
    ``.<target name>.old-<uuid>`` (atomic), the new content is renamed in
    (atomic), and the old content is removed. If the new rename fails, the old
    is restored from the backup; if that fails too, :class:`SwapIncomplete` names
    the backup and nothing is deleted. A ``target`` that cannot be renamed
    (Windows: a file in it is held open by another process) fails before anything
    moved. Only the backup this call made is ever deleted; one that cannot be
    deleted stays, logged (see :func:`swap_leftovers`).

    Both ``source`` and ``target`` must live on the same filesystem.
    """
    if not target.exists():
        os.replace(source, target)
        return
    backup = target.parent / f".{target.name}.old-{uuid.uuid4().hex[:8]}"
    os.replace(target, backup)
    try:
        os.replace(source, target)
    except BaseException as exc:
        try:
            os.replace(backup, target)
        except OSError as restore_exc:
            saved = f"The previous content is saved in {backup}; the new one in {source}."
            if target.exists():  # another process created it between the two renames
                message = (
                    f"A folder named '{target.name}' was created while {target} was being replaced. "
                    f"{saved} Move the '{target.name}' folder aside, then rename {backup.name} to "
                    f"'{target.name}'."
                )
            else:
                message = f"{target} is missing. {saved} Rename {backup.name} to '{target.name}' to recover it."
            message += f" (Replacing it failed: {exc}; putting it back failed: {restore_exc}.)"
            logger.error("%s", message)
            raise SwapIncomplete(message) from exc
        raise
    rmtree(backup, ignore_errors=True)
    if backup.exists():
        logger.warning(
            "Could not remove %s (the replaced %s, possibly partly deleted); it stays.",
            backup,
            target.name,
        )


def replace_with(target: Path, staging: Path, fill: Callable[[Path], object]) -> None:
    """Build *target*'s replacement with ``fill(staging)`` (a new folder beside it,
    or a path ``fill`` creates) and :func:`swap_into_place` it. Any failure deletes
    *staging* — except a swap that failed half-way: then *staging* is the new
    content :class:`SwapIncomplete` names, and it stays."""
    try:
        fill(staging)
        swap_into_place(staging, target)
    except SwapIncomplete:
        raise
    except BaseException:
        rmtree(staging, ignore_errors=True)
        raise
