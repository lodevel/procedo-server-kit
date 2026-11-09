"""A report draft is replaced only when unchanged since it was read (``data_format.md`` §4.10,
§6.6; ``company_storage_design.md`` §6c).

A board's report draft (artifact kind ``draft``) is rewritten by every save, so two benches
editing it would silently replace each other's. It has a **version** instead: the sha256 of
its content written as canonical JSON (:func:`version_of`: keys sorted, no blank between
tokens, non-ASCII as itself, UTF-8), computed by the app, never by a server. A save says which
version it replaces (``replaces``: the one it read; ``None``: none), and the push
(:func:`publish`), under the artifact folder's lock, compares it with the draft held:

* none held: the draft is saved, whatever ``replaces`` says (there is nothing to keep);
* the same version as the one brought: ``already``, nothing written (a replay);
* ``replaces`` is the version held: the draft is replaced;
* anything else: refused, ``changed`` (:func:`changed`), nothing written: someone saved it
  since it was read, and the person decides (Bench's Save draft, "Use mine").

Every store read and write goes through the store's tree. Qt-free, stdlib only.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePath
from typing import Any

from .evidence_store import CHANGED, EvidenceError, OpContext
from .evidence_tree import lock_wait_s, tree_lock

#: What a draft's content says of its last save (``result_annotations.save_global_draft``).
SAVED_BY, SAVED_AT = "saved_by", "modified_at"


def version_of_content(content: Any) -> str:
    """The version of a draft's content: the sha256 of it as canonical JSON."""
    text = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def version_of(data: bytes) -> str:
    """The version of a draft as stored (its file's bytes); a file that is not JSON is
    versioned by its bytes."""
    try:
        content = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return hashlib.sha256(data).hexdigest()
    return version_of_content(content)


def who(data: bytes) -> tuple[str, str]:
    """``(by, at)``: who saved the draft *data* last, and when (``""`` when it does not say)."""
    try:
        content = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return "", ""
    if not isinstance(content, dict):
        return "", ""
    return str(content.get(SAVED_BY) or ""), str(content.get(SAVED_AT) or "")


def changed(name: str, held: bytes) -> EvidenceError:
    """The refusal of a draft whose held version is not the one the save read."""
    by, at = who(held)
    whom = f" by {by}" if by else ""
    when = f" at {at}" if at else ""
    return EvidenceError(
        "invalid",
        f"The report draft {name} was changed{whom}{when} after it was read: it was not replaced.",
        code=CHANGED,
    )


def publish(
    store: Any,
    root: PurePath,
    src: Path,
    names: list[str],
    replaces: str | None,
    ctx: OpContext | None = None,
) -> bool:
    """Land the draft ``names[0]`` from this PC's folder ``src`` in the artifact folder
    ``root``, which exists, by the module's rule. False: the same draft was there already,
    and nothing was written; a changed one raises :func:`changed`."""
    io = store.io
    payload = root / names[0]
    brought = version_of((src / names[0]).read_bytes())
    with tree_lock(
        io,
        root / ".lock",
        store.tag,
        timeout_s=lock_wait_s(ctx),
        cancel=ctx.cancel if ctx else None,
        ctx=ctx,
    ):
        if io.is_file(payload):
            held = io.read_bytes(payload)
            now = version_of(held)
            if now == brought:
                return False
            if replaces != now:
                raise changed(payload.name, held)
        for f in names:
            io.put_file(src / f, root / f, store.tag, verify=True)  # checked before it is visible
    return True
