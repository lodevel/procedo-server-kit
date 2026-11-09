"""One project-sync pass as a unit: how it is scoped and cancelled, and which failures end it.

* :func:`tag` names every temp file of the pass, on both sides.
* :func:`operation` is the pass's scope on a tree (one leased connection, one cached listing per
  folder, the cancel flag); :func:`check_cancel` ends the pass when somebody pressed Cancel.
* **One bad file does not end the pass.** A destination somebody holds open, a read-only file, a
  name the filesystem will not take: :func:`typed_failure` classifies it, :func:`why_failed` words
  it, and the pass reports it against the file and goes on. Only cancellation, a link that is gone
  or refuses the credential, and a full disk (:data:`FATAL`) stop it, because those are about the
  pass, not a file.

Qt-free, stdlib only.
"""

from __future__ import annotations

import errno
import os
import uuid
from typing import Any

from .evidence_store import EvidenceError, OpContext
from .evidence_store_fs import os_error
from .evidence_tree import TreeIO, tree_operation

#: Failures about the PASS, not about one file: they end it. Everything else is
#: reported against the file it happened to and the pass carries on, because one
#: unreadable name must not keep a whole project from ever syncing again.
FATAL = frozenset({"cancelled", "unreachable", "auth", "disk_full"})


def tag() -> str:
    """The writer tag naming every temp file of this pass, on both sides.

    A pid is unique per MACHINE and the destination may be shared, so it carries a
    random fragment too — the trap ``atomic_write`` documents.
    """
    return f"sync.{os.getpid()}.{uuid.uuid4().hex[:8]}"


def operation(tree: TreeIO, ctx: OpContext) -> Any:
    """The tree's operation scope (one leased connection, one cached listing per folder
    — a filesystem tree's too — and the cancel flag): ``evidence_tree.tree_operation``
    over an ``OpContext``."""
    return tree_operation(tree, ctx.cancel)


def check_cancel(ctx: OpContext) -> None:
    if ctx.cancel.is_set():
        raise EvidenceError("cancelled", "the project sync was cancelled")


def disk_full(exc: BaseException) -> bool:
    return isinstance(exc, OSError) and (
        exc.errno == errno.ENOSPC or getattr(exc, "winerror", None) == 112
    )


def why_failed(exc: OSError) -> str:
    """An operating-system error in words a person reads.

    ``str(OSError)`` renders the filename with ``repr``, so every separator in a
    Windows path is doubled: ``'C:\\\\Users\\\\...'``. That text went straight into
    the report the GUI shows. The reason and the path, plainly, instead.
    """
    reason = getattr(exc, "strerror", None) or str(exc)
    name = getattr(exc, "filename", None)
    return f"{reason}: {name}" if name else str(reason)


def typed_failure(exc: BaseException) -> EvidenceError:
    """The typed error for one file's failure — classified by the store's own rule,
    worded by :func:`why_failed` so a report never carries a ``repr``."""
    if isinstance(exc, EvidenceError):
        return exc
    if disk_full(exc):
        # Every later file would fail the same way: the pass ends, the copy as it was.
        return EvidenceError("disk_full", f"this PC's disk is full: {why_failed(exc)}")  # type: ignore[arg-type]
    typed = os_error(exc)  # type: ignore[arg-type]
    return EvidenceError(typed.kind, why_failed(exc), typed.key)  # type: ignore[arg-type]
