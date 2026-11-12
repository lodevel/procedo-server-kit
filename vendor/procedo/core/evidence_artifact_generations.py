"""A report or an export sent again is kept, never replaced (``data_format.md`` §6.6).

::

    <folder>/<name> + its sidecar                      the newest generation
            /.generations/<name>/<n>/<name> + sidecar  each earlier one, n = 1, 2, …

``<folder>`` is a board's ``reports/`` or the evidence folder's ``exports/``; the sidecar is
the one the push lands with the payload (a report's ``<stem>.json``, an export's
``<name>.meta.json``). The push of a report or an export (:func:`publish`) compares what it
brings with what is at ``<name>``:

* the same payload and sidecar is the same artifact sent again: nothing is written;
* other bytes first move the payload and sidecar there into ``.generations/<name>/<n>/``,
  ``<n>`` the next free number, then write the new ones in place.

So ``<name>`` is always the newest, and an earlier generation is never rewritten: a report
somebody sent to a customer is always there. It all happens under the artifact folder's own
lock (``<folder>/.lock``), so two benches never both take one number, nor both replace
``<name>``; and a move never replaces what is at its destination where the tree can refuse it
(a server's ``:move``). ``.generations`` appears once an earlier generation is kept. A draft is
not kept: each save replaces it.

The first one sent is generation 1, and ``<name>`` is one more than the highest folder
(:func:`listing`). A listing of ``<folder>`` matches reports (their record ``*.json``, or page) or
files (exports), so ``.generations`` is never an artifact; the trash takes
``.generations/<name>`` with its artifact and puts it back with it
(``SharedLayout.delete_artifact``).

Every store read and write goes through the store's tree. Qt-free, stdlib only.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePath
from typing import Any

from .evidence_store import OpContext
from .evidence_store_fs import _sidecars
from .evidence_tree import lock_wait_s, tree_lock

GENERATIONS = ".generations"
KEPT = ("report", "export")  # the kinds whose earlier generations are kept
_NUMBER = re.compile(r"[1-9][0-9]*")


def folder_of(root: PurePath, name: str) -> PurePath:
    """Where the earlier generations of the artifact ``root/name`` are."""
    return root / GENERATIONS / name


def _numbers(io: Any, folder: PurePath) -> list[int]:
    """The numbers taken in a ``.generations/<name>/`` folder, in order."""
    return sorted(int(n) for n in io.list_names(folder) or [] if _NUMBER.fullmatch(n))


def listing(io: Any, root: PurePath, name: str, kind: str) -> list[tuple[int, PurePath]]:
    """``(number, payload)`` of every generation of ``root/name``, oldest first: each
    numbered folder holding it, then ``<name>`` itself, one more than the highest number
    taken. A report is there by its record, its page there or not (contract §5.10). ``[]``:
    the artifact is not there."""

    def there(payload: PurePath) -> bool:
        record = _sidecars(kind, payload)[:1] if kind == "report" else ()
        return any(io.is_file(p) for p in (payload, *record))

    folder = folder_of(root, name)
    numbers = _numbers(io, folder)
    found = [(n, folder / str(n) / name) for n in numbers if there(folder / str(n) / name)]
    if there(root / name):
        found.append((numbers[-1] + 1 if numbers else 1, root / name))
    return found


def _same(io: Any, root: PurePath, there: list[PurePath], names: list[str], digests: dict) -> bool:
    """Whether the files ``there`` are exactly the ones brought: the same names, each with
    the size and sha256 the outbox entry recorded (the size first: it costs no read)."""
    if sorted(p.name for p in there) != sorted(names):
        return False
    for f in names:
        brought = digests.get(f) or {}
        if io.stat(root / f)[0] != brought.get("size"):
            return False
        if io.sha256(root / f) != brought.get("sha256"):
            return False
    return True


def same_as(io: Any, root: PurePath, kind: str, names: list[str], digests: dict) -> bool:
    """Whether the artifact at ``root/names[0]`` (with its sidecar) is exactly the one
    brought: :func:`_same` over what is there (``False``: nothing is)."""
    payload = root / names[0]
    there = [p for p in (payload, *_sidecars(kind, payload)[:1]) if io.is_file(p)]
    return bool(there) and _same(io, root, there, names, digests)


def publish(
    store: Any,
    kind: str,
    root: PurePath,
    src: Path,
    names: list[str],
    digests: dict,
    ctx: OpContext | None = None,
) -> bool:
    """Land the files ``names`` (the payload first, then its sidecar) from this PC's folder
    ``src`` in the artifact folder ``root``, which exists, keeping the generation that was
    there (the module's docstring). ``digests``: ``{name: {"size", "sha256"}}`` of what is
    brought. False: the same bytes were there already, and nothing was written."""
    io = store.io
    payload = root / names[0]
    with tree_lock(
        io,
        root / ".lock",
        store.tag,
        timeout_s=lock_wait_s(ctx),
        cancel=ctx.cancel if ctx else None,
        ctx=ctx,
    ):
        if same_as(io, root, kind, names, digests):
            return False
        there = [p for p in (payload, *_sidecars(kind, payload)[:1]) if io.is_file(p)]
        if there:
            folder = folder_of(root, payload.name)
            kept = folder / str((_numbers(io, folder) or [0])[-1] + 1)
            store._mkdir_safe(kept)
            for p in there:
                io.rename(p, kept / p.name)
        for f in names:
            io.put_file(src / f, root / f, store.tag, verify=True)  # checked before it is visible
    return True
