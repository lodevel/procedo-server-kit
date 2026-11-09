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
"""

from __future__ import annotations

import stat
import zipfile
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
    seen = set()
    components = {}
    for member in zf.infolist():
        name = _member_path(member)
        if root is not None and not (root / name).resolve().is_relative_to(root):
            raise ValueError(f"Zip entry escapes target dir: {member.filename!r}")
        mode = member.external_attr >> 16
        if stat.S_ISLNK(mode):
            raise ValueError(f"Symlink entries are not supported: {member.filename!r}")
        if stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR):
            raise ValueError(f"Special archive entry: {member.orig_filename!r}")
        parts = name.rstrip("/").split("/")
        if not all(is_safe_name(part) for part in parts):
            raise ValueError(f"Unsafe archive path: {member.orig_filename!r}")
        key = "/".join(parts).casefold()
        if key in seen:
            raise ValueError(f"Duplicate archive path: {member.orig_filename!r}")
        seen.add(key)
        for index in range(1, len(parts) + 1):
            spelling = "/".join(parts[:index])
            directory = index < len(parts) or name.endswith("/")
            previous = components.setdefault(spelling.casefold(), (spelling, directory))
            if previous != (spelling, directory):
                raise ValueError(f"Conflicting archive path: {member.orig_filename!r}")


def safe_extract(zf: zipfile.ZipFile, target: Path) -> None:
    """Extract every member of *zf* into *target* after :func:`check_archive` passed —
    each under its checked name, so ``a\\b`` lands as ``a/b`` on every OS."""
    check_archive(zf, target)
    members = zf.infolist()
    for member in members:
        member.filename = _member_path(member)
    zf.extractall(target, members=members)  # by entry: a lookup by the new name would miss
