"""The one member check of an archive about to be extracted, and the extraction it
guards — bundle imports, package installs and template imports all go through it.

A ``\\`` in a member name is a path separator, like ``/`` (old Windows tools write it),
and every part between separators is then checked. Refused: a member that would land
outside the target (``..``, an absolute path, a drive, a UNC path), a part that is not a
safe file or folder name on every OS (``:``, control characters, trailing dot/space, a
Windows device name — :func:`paths.is_safe_name`), the same path twice or spelled two
ways (``A.txt`` and ``a.txt``, ``a/b`` and ``a\\b``, a file and a folder), and any member
that is not a plain file or folder (a symlink entry: ``ZipFile.extract`` writes its link
target as a plain file's contents, other unzip tools make a real link of it).

:func:`check_names` is the same name rule over a list of names, for an archive about to be
written (the project zip's export holds its members to it before the first byte).

:func:`check_budget` is the archive's size check: what it unpacks to against the room there
is, how many members it holds, how far a member inflates. The project zip uses it
(``project_zip_read``); the other imports do not, for now.
"""

from __future__ import annotations

import stat
import zipfile
from collections.abc import Iterable
from pathlib import Path

from .paths import is_safe_name


def _member_path(member: zipfile.ZipInfo) -> str:
    """*member*'s name with ``\\`` read as ``/`` — from the raw name, before zipfile cuts
    it at a NUL (and, on Windows only, turns ``\\`` into ``/`` itself)."""
    return member.orig_filename.replace("\\", "/")


def check_archive(zf: zipfile.ZipFile, target: Path | None = None) -> None:
    """Raise ``ValueError`` unless every member of *zf* is safe to extract — into
    *target* when given (each member must resolve inside it)."""
    root = Path(target).resolve() if target is not None else None
    names = _Names()
    for member in zf.infolist():
        name = _member_path(member)
        if root is not None and not (root / name).resolve().is_relative_to(root):
            raise ValueError(f"Zip entry escapes target dir: {member.filename!r}")
        mode = member.external_attr >> 16
        if stat.S_ISLNK(mode):
            raise ValueError(f"Symlink entries are not supported: {member.filename!r}")
        if stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR):
            raise ValueError(f"Special archive entry: {member.orig_filename!r}")
        names.add(name, member.orig_filename)


def check_names(names: Iterable[str]) -> None:
    """Raise ``ValueError`` unless every name of *names* (``/`` between folders, a trailing
    ``/`` for a folder) is one an archive may hold, as :func:`check_archive` holds a member's:
    each part a safe name, each path once whatever its case, a name never both a file and a
    folder. What an archive about to be WRITTEN is checked by, before its first byte."""
    found = _Names()
    for name in names:
        found.add(name, name)


class _Names:
    """The names of one archive seen so far, and the rule each new one is held to."""

    def __init__(self) -> None:
        self.seen: set[str] = set()
        self.components: dict[str, tuple[str, bool]] = {}

    def add(self, name: str, shown: str) -> None:
        parts = name.rstrip("/").split("/")
        if not all(is_safe_name(part) for part in parts):
            raise ValueError(f"Unsafe archive path: {shown!r}")
        key = "/".join(parts).casefold()
        if key in self.seen:
            raise ValueError(f"Duplicate archive path: {shown!r}")
        self.seen.add(key)
        for index in range(1, len(parts) + 1):
            spelling = "/".join(parts[:index])
            directory = index < len(parts) or name.endswith("/")
            previous = self.components.setdefault(spelling.casefold(), (spelling, directory))
            if previous != (spelling, directory):
                raise ValueError(f"Conflicting archive path: {shown!r}")


def safe_extract(zf: zipfile.ZipFile, target: Path) -> None:
    """Extract every member of *zf* into *target* after :func:`check_archive` passed —
    each under its checked name, so ``a\\b`` lands as ``a/b`` on every OS."""
    check_archive(zf, target)
    members = zf.infolist()
    for member in members:
        member.filename = _member_path(member)
    zf.extractall(target, members=members)  # by entry: a lookup by the new name would miss


#: The most members an archive :func:`check_budget` passes: a central directory of more is
#: refused before any member is read.
MAX_MEMBERS = 1_000_000
#: The most a member may declare it inflates to, per byte it occupies in the archive. Deflate
#: cannot exceed about 1032 to 1, so a member Procedo deflated always passes; a member past it
#: was compressed by another method, or declares a size its bytes cannot hold.
MAX_RATIO = 1100


class NoRoom(ValueError):
    """The archive unpacks to more than the destination has free (:func:`check_budget`)."""

    def __init__(self, needed: int, free: int) -> None:
        super().__init__(f"The archive unpacks to {needed} bytes; {free} bytes are free.")
        self.needed = needed
        self.free = free


def check_budget(zf: zipfile.ZipFile, free_bytes: int | None = None) -> int:
    """The bytes *zf* unpacks to, by its members' declared sizes; ``ValueError`` when it has
    more than :data:`MAX_MEMBERS` members or a member declares more than :data:`MAX_RATIO`
    times its compressed size, and :class:`NoRoom` when *free_bytes* is given and the sum is
    more. The declared sizes are the ones that count: a member that inflates past its declared
    size fails its CRC in ``zipfile`` before a byte more is handed over. Reads the archive's
    directory only, never a member."""
    members = zf.infolist()
    if len(members) > MAX_MEMBERS:
        raise ValueError(f"The archive holds {len(members)} members, more than {MAX_MEMBERS}.")
    for member in members:
        if member.file_size > MAX_RATIO * max(member.compress_size, 1):
            raise ValueError(
                f"Archive entry inflates {member.file_size} bytes from "
                f"{member.compress_size}: {member.orig_filename!r}"
            )
    needed = sum(member.file_size for member in members)
    if free_bytes is not None and needed > free_bytes:
        raise NoRoom(needed, free_bytes)
    return needed
