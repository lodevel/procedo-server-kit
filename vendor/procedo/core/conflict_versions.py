"""Each side's version of a test in conflict, as the conflict dialog's columns say it
(docs/plans/test_release_design.md, slice 2): this PC's copy and the server's files, each told as
``test_release.state`` tells a project's — ``v4``, ``Draft (from v4)``, ``Draft`` —, never by the
number a ``test.json`` was stamped with. Read on the sync's worker, never on the GUI thread: the
server's side is read on the server.

The server's side is first read into a folder of its own laid out as a project (the test's
documents, the ``functions/`` they may import, its releases: ``releases/<test_id>``, the id its
``test.json`` on the server holds), so both sides are told by one rule. Only the conflicted tests are read, when the dialog asks for its rows: opening a project
reads no release.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path, PurePath

from . import test_format, test_release
from .evidence_store import EvidenceError
from .evidence_tree import TreeIO

log = logging.getLogger(__name__)

#: A test's own files read from the server, beside its ``openhtf/`` folder.
_OWN = (*test_release.TEXT_DOCUMENTS, test_format.TEST_JSON)


def here(working_copy: Path, test: str) -> str | None:
    """The version *test*'s files in *working_copy* are; None when they cannot be read."""
    try:
        return test_release.state(Path(working_copy), test).label
    except OSError as exc:
        log.warning("The version of %s in %s is not read: %s", test, working_copy, exc)
        return None


class There:
    """The version of each test the server holds under *base* in *tree*, its files read into
    *scratch* (an empty folder the caller removes; the ``functions/`` read once for all)."""

    def __init__(self, tree: TreeIO, base: PurePath, scratch: Path) -> None:
        self.tree, self.base, self.scratch = tree, base, Path(scratch)
        self._functions = False

    def __call__(self, test: str) -> str:
        """*test*'s version on the server. What the server cannot read raises, as any read of
        the conflicts does."""
        if not self._functions:
            _copy(self.tree, self.base / "functions", self.scratch / "functions", _python)
            self._functions = True
        source, target = self.base / "tests" / test, self.scratch / "tests" / test
        for name in _OWN:
            data = read_or_none(self.tree, source / name)
            if data is not None:
                target.mkdir(parents=True, exist_ok=True)
                (target / name).write_bytes(data)
        _copy(self.tree, source / test_format.CODE_DIR, target / test_format.CODE_DIR)
        test_id = test_release.test_id_of(self.scratch, test)  # the server's test.json's
        if test_id:
            _copy(
                self.tree,
                self.base / test_release.RELEASES / test_id,
                test_release.folder_of(self.scratch, test_id),
                lambda name: name.endswith(".json"),
            )
        return test_release.state(self.scratch, test).label


class ThereFrom:
    """The version of each test a server that keeps projects itself holds (the web server):
    *fill* lays the test out as a project in *scratch* (its files, the ``functions/``, its
    releases: ``project_sync_api.remote_test``), told by the same rule as this PC's side."""

    def __init__(self, fill: Callable[[str, Path], None], scratch: Path) -> None:
        self.fill, self.scratch = fill, Path(scratch)

    def __call__(self, test: str) -> str:
        self.fill(test, self.scratch)
        return test_release.state(self.scratch, test).label


def _python(name: str) -> bool:
    return name.endswith(".py")


def _copy(
    tree: TreeIO, source: PurePath, target: Path, keep: Callable[[str], bool] = lambda _n: True
) -> None:
    """Every file under *source* on the server whose name *keep* takes, into *target*; never a
    dot-name or ``__pycache__``. Nothing when *source* is no folder."""
    names = tree.list_names(source)
    for name in names or []:
        if name.startswith(".") or name == "__pycache__":
            continue
        path = source / name
        if tree.is_dir(path):
            _copy(tree, path, target / name, keep)
        elif keep(name) and (data := read_or_none(tree, path)) is not None:
            target.mkdir(parents=True, exist_ok=True)
            (target / name).write_bytes(data)


def read_or_none(tree: TreeIO, path: PurePath) -> bytes | None:
    """*path*'s bytes on the server, None when it is not there."""
    try:
        return tree.read_bytes(path)
    except FileNotFoundError:
        return None
    except EvidenceError as exc:
        if exc.kind == "not_found":
            return None
        raise
