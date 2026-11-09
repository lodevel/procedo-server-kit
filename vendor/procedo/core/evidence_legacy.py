"""The OLD results layout, read-only — what a project from before the evidence store holds.

    <project>/serial_numbers/<serial>/results/<test>/{results.json, .metadata.json, prev_<ts>/}
                                     /reports/<name>.html + <name>.json
    <project>/exports/<name>

Bench neither writes nor reads this layout any more (owner 2026-10-24: legacy dropped,
no conversion). What is left is the knowledge that such files may still lie in a
project folder, so they are never mistaken for absent: :func:`has_files` (a project
that still holds them is not free to choose its store) and the two folder names (the
sync excludes them, the evidence folder may not be inside them, *Forget this working
copy* counts them). This module is the one place the layout is spelled
(``tests/test_evidence_structure.py`` polices it).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

SERIALS = "serial_numbers"
EXPORTS = "exports"


def trees(project_root: Path) -> tuple[Path, Path]:
    """The two in-project trees of the old layout: ``serial_numbers/``, ``exports/``."""
    root = Path(project_root)
    return root / SERIALS, root / EXPORTS


def _files_of(project_root: Path, denied: list[OSError]) -> Iterator[Path]:
    """Every FILE under the legacy trees. Walked with ``os.walk(onerror=…)`` and not
    ``rglob``, which swallows every ``scandir`` error: a subtree this account cannot list
    would read as *no files*."""
    for tree in trees(project_root):
        if not tree.is_dir():
            continue
        for dirpath, _dirnames, filenames in os.walk(tree, onerror=denied.append):
            yield from (Path(dirpath) / name for name in filenames)


def has_files(project_root: Path) -> bool:
    """The legacy trees hold a FILE — folders alone (an empty serial) are not evidence.
    Stops at the first file; a tree that cannot be listed counts as holding one (never
    assume old results are absent)."""
    denied: list[OSError] = []
    for _ in _files_of(project_root, denied):
        return True
    return bool(denied)
