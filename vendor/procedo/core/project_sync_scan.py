"""What a project-sync pass sees: the working copy and the server, scanned, and one file looked
at again on either side.

* **A scan that cannot see everything FAILS.** ``os.walk`` hands back what it could read and says
  nothing about the rest, and a scan short of a folder is a plan to delete that folder on the
  server. So the walk re-raises, and a ``stat`` that fails does too.
* **A link is never followed and never synced.** On Windows ``Path.is_symlink()`` answers False
  for a junction, which made a junction an escape hatch in both directions — the scan walked out
  of the working copy and a server-side delete unlinked a file that was never part of the
  project. :func:`is_link` asks about the reparse point, and :func:`local_path` re-proves
  containment before every landing and every unlink.

Qt-free, stdlib only.
"""

from __future__ import annotations

import logging
import os
import stat as stat_module
from pathlib import Path, PurePath

from .evidence_store import EvidenceError
from .evidence_tree import TreeIO
from .project_never_travels import exclusion
from .project_origin import Stamp
from .project_sync_plan import Refusal

log = logging.getLogger(__name__)


_REPARSE_POINT = getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def is_link(path: Path) -> bool:
    """Whether *path* is a link of ANY kind — a symlink or a Windows junction.

    ``Path.is_symlink()`` is not that question: CPython reports a symlink only for
    ``IO_REPARSE_TAG_SYMLINK``, so a junction (``mklink /J``) answers False, walks
    like a folder, and puts files from anywhere on the disk inside the working
    copy. An entry that is not there is not a link; anything else ``lstat`` refuses
    is a scan that failed.
    """
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    if stat_module.S_ISLNK(info.st_mode):
        return True
    return bool(getattr(info, "st_file_attributes", 0) & _REPARSE_POINT)


def local_path(working_copy: Path, rel: str) -> Path:
    """The working copy's file for a relative POSIX path, PROVED to be inside it.

    Not a test on the spelling: a junction anywhere along the chain makes a path
    that is inside the working copy to read and outside it on disk, and that is
    exactly how a server-side delete unlinked a file nobody had ever synced. So the
    path is RESOLVED and must still be the one the plain join names. That one
    comparison answers every shape at once — a ``..`` that climbs out, an absolute
    name, a junction, a symlink, and a link that points back inside — which is why
    the spelling test and the per-component link test that used to sit in front of
    it were both removed: a mutation proved neither could ever fire first.

    Called before every landing and before every unlink — the two places where
    being wrong destroys something.
    """
    parts = rel.split("/")
    out = working_copy.joinpath(*parts)
    expected = os.path.join(os.path.realpath(working_copy), *parts)
    if os.path.normcase(os.path.realpath(out)) != os.path.normcase(expected):
        raise EvidenceError(
            "invalid",
            f"{rel!r} does not resolve inside {working_copy} — a link, a junction or a "
            "name that climbs out of the project is not synced",
        )
    return out


def _walk_failed(exc: OSError) -> None:
    """``os.walk`` drops a folder it cannot read unless it is told not to, and a scan
    short of a folder is a plan to DELETE that folder on the server. So a partial
    scan is a failed pass, never a list of files somebody removed."""
    raise exc


def scan_local(root: Path, *, refused: list[Refusal] | None = None) -> dict[str, Stamp]:
    """Every file of the project in the working copy, by relative POSIX path.

    Excluded folders are PRUNED rather than walked, and so are links — a link is
    never followed and never synced: what it points at is this machine's business,
    and *refused* collects them for the report.
    """
    root = Path(root)
    found: dict[str, Stamp] = {}
    for dirpath, dirnames, filenames in os.walk(root, onerror=_walk_failed):
        here = Path(dirpath).relative_to(root).as_posix()
        prefix = "" if here == "." else f"{here}/"
        kept = []
        for name in sorted(dirnames):
            rel = f"{prefix}{name}"
            if exclusion(rel):
                continue
            if is_link(Path(dirpath) / name):
                _link_refused(refused, rel, "folder")
                continue
            kept.append(name)
        dirnames[:] = kept
        for name in sorted(filenames):
            rel = f"{prefix}{name}"
            if exclusion(rel):
                continue
            path = Path(dirpath) / name
            if is_link(path):
                _link_refused(refused, rel, "file")
                continue
            info = path.stat()
            found[rel] = Stamp(info.st_size, info.st_mtime)
    return found


def _link_refused(refused: list[Refusal] | None, rel: str, kind: str) -> None:
    why = f"a linked {kind}: what it points at is this machine's, not the project's"
    log.info("project sync: %s is %s", rel, why)
    if refused is not None:
        refused.append(Refusal(rel, "local", why))


def server_stamp(tree: TreeIO, path: PurePath) -> Stamp:
    """One server entry as the manifest records it."""
    size, mtime = tree.stat(path)
    return Stamp(size, mtime)


def scan_server(tree: TreeIO, base: PurePath) -> dict[str, Stamp]:
    """The project's files on the server, by relative POSIX path.

    **The one server walk**, and it takes the tree, so a transport that can answer
    a whole subtree in a single call has one place to say so: a tree offering
    ``walk_files(base, prune) -> {relative posix path: Stamp}`` is asked that instead of
    being walked folder by folder (a share: one enumeration per folder, several at once,
    excluded folders never entered; it raises for an absent base, as ``list_names``
    answering ``None`` does here).

    Every gap RAISES: a base path that is not a folder, and a folder the listing
    named as a folder and then would not list. Reading either as "no files" plans
    the deletion of everything below it in the working copy.
    """
    whole = getattr(tree, "walk_files", None)
    if whole is not None:  # *prune*: a tree that walks folder by folder never enters excluded ones
        return {
            rel: stamp for rel, stamp in whole(base, prune=exclusion).items() if not exclusion(rel)
        }
    names = tree.list_names(base)
    if names is None:
        raise EvidenceError("not_found", f"the project folder is not on the server: {base}")
    found: dict[str, Stamp] = {}
    stack: list[tuple[str, list[str]]] = [("", names)]
    while stack:
        here, entries = stack.pop()
        folder = base / here if here else base
        for name in sorted(entries):
            rel = f"{here}/{name}" if here else name
            if exclusion(rel):
                continue
            child = folder / name
            if tree.is_dir(child):
                below = tree.list_names(child)
                if below is None:
                    raise EvidenceError(
                        "not_found",
                        f"the server listed {rel} as a folder and then could not list it; "
                        "the scan is incomplete and nothing will be deleted from it",
                    )
                stack.append((rel, below))
            else:
                found[rel] = server_stamp(tree, child)
    return found


def local_now(working_copy: Path, rel: str) -> Stamp | None:
    """The working copy's file as it is NOW, ``None`` when it is not there."""
    try:
        here = local_path(working_copy, rel).stat()
    except FileNotFoundError:
        return None
    return Stamp(here.st_size, here.st_mtime)


def server_now(tree: TreeIO, remote: PurePath) -> Stamp | None:
    """The server's copy as it is NOW, ``None`` when it is not there."""
    try:
        return server_stamp(tree, remote)
    except FileNotFoundError:
        return None
