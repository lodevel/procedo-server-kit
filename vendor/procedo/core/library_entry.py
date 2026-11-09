"""What identifies one item of a library: ``id@version`` and a content hash.

Four kinds of thing are kept in a library and moved between this PC and the
server — a bundle, an export template package, a report template package and a
configuration — and until now only a bundle could be compared with its twin
somewhere else, because only a bundle carried a hash. This module is the ONE
place that says, for every kind:

* which file is its **manifest** (``bundle.json`` / ``schema.json`` /
  ``config.json``) and where the id and the version sit in it;
* how its **content hash** is computed — the tree digest the bundle builder
  already used (``tools/generate_all.py:_compute_bundle_sha256``), lifted here
  so there is one implementation and not four;
* where a **declared** hash is written, for a copy that travelled.

**Computed here, declared there.** A folder on THIS PC is compared by computing
the digest of its bytes: a configuration is edited in place, so a hash recorded
when it was written would answer for content it no longer has. A folder on the
SERVER is compared by the hash its manifest declares, because reading that one
file is a round trip and downloading the folder to hash it is many. The
publisher stamps the field on the way up (:func:`stamp`), which is why the two
sides can be compared at all.

The digest **excludes the manifest itself**, so stamping a hash into a manifest
never changes the hash — the same reason the bundle builder excludes
``bundle.json``.

Qt-free, and it imports nothing of the app: the three library modules import
THIS one.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .paths import sha256_file

logger = logging.getLogger(__name__)

#: Shell droppings that are not content. A folder that has been opened in
#: Explorer or on a Mac must hash the same as the one that was published.
OS_SHELL_BASENAMES = frozenset({"desktop.ini", "Thumbs.db", ".DS_Store"})

BUNDLE = "bundle"
EXPORT = "export"
REPORT = "report"
CONFIG = "config"


@dataclass(frozen=True)
class Kind:
    """One library kind: its manifest, and where identity lives inside it."""

    name: str
    label: str
    manifest: str
    id_field: str
    version_field: str
    #: The manifest section the three fields sit in (``""`` = the top level). A
    #: configuration keeps its metadata under ``manifest``; the others are flat.
    section: str = ""
    #: The manifest key a travelled copy declares its content hash under. A
    #: bundle spells it ``bundle_sha256`` — its builder writes that name and a
    #: rename would make every built bundle unreadable.
    hash_field: str = "content_sha256"
    #: The manifest key it declares its total size under, so a picker can say
    #: what a download weighs without walking the folder file by file.
    size_field: str = "content_bytes"
    #: Whether the LOCAL library keeps a version level under the id. A
    #: configuration does not (one version per name on a bench,
    #: ``company_storage_design.md`` §5); its version is what its manifest says.
    local_versions: bool = True
    #: Where it lives on a server of folders: the store entry, then the
    #: folder under it that holds ``<id>/<version>/``.
    store: str = "packages"
    subpath: str = "bundles"


KINDS: dict[str, Kind] = {
    BUNDLE: Kind(BUNDLE, "Bundle", "bundle.json", "id", "version", hash_field="bundle_sha256"),
    EXPORT: Kind(
        EXPORT, "Export template", "schema.json", "package_id", "package_version", subpath="exports"
    ),
    REPORT: Kind(
        REPORT, "Report template", "schema.json", "package_id", "package_version", subpath="reports"
    ),
    CONFIG: Kind(
        CONFIG,
        "Configuration",
        "config.json",
        "name",
        "version",
        section="manifest",
        local_versions=False,
        store="configs",
        subpath="",
    ),
}


@dataclass(frozen=True)
class Entry:
    """One library item, wherever it is: the identity and what it holds."""

    kind: str
    id: str
    version: str
    content_sha256: str
    size: int = 0
    #: The folder on this PC, or ``None`` for an item that is only on the server.
    root: Path | None = None

    @property
    def ref(self) -> str:
        return f"{self.id}@{self.version}"

    @property
    def short_sha(self) -> str:
        """The first 8 characters — what a refusal names, never the whole hash."""
        return self.content_sha256[:8]


def tree_digest(root: Path, *, manifest_name: str) -> tuple[str, int]:
    """``(content hash, total bytes)`` of the folder *root*.

    The rule the bundle builder set and every kind now shares: every file except
    the root manifest and the OS shell droppings, ordered by the byte-ordinal
    sort of its relative POSIX path — the same order on every OS, which
    ``sorted(Path)`` is not (Windows collates path parts case-insensitively) —
    each contributing its path, a NUL, its own sha256 and a NUL.

    The size counts exactly the files that were hashed — the manifest is left out
    of both, so recording the size INSIDE the manifest cannot change it either.
    """
    h = hashlib.sha256()
    size = 0
    files = sorted(
        (p for p in Path(root).rglob("*") if p.is_file()),
        key=lambda p: p.relative_to(root).as_posix(),
    )
    for path in files:
        if path.name in OS_SHELL_BASENAMES:
            continue
        rel = path.relative_to(root).as_posix()
        if rel == manifest_name:
            continue
        size += path.stat().st_size
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(sha256_file(path).encode("ascii"))
        h.update(b"\0")
    return h.hexdigest(), size


def content_sha256(kind: str, root: Path) -> str:
    """The digest of what *root* holds RIGHT NOW — the local side of every
    comparison."""
    return tree_digest(Path(root), manifest_name=KINDS[kind].manifest)[0]


def read_manifest(kind: str, root: Path) -> dict[str, Any]:
    """The manifest's identity section, or ``{}`` when *root* is not this kind.

    Never raises: a half-copied folder in a library is skipped, not a crash in
    the picker that listed it.
    """
    spec = KINDS[kind]
    try:
        data = json.loads((Path(root) / spec.manifest).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.info("Not a %s at %s: %s", spec.name, root, exc)
        return {}
    if not isinstance(data, dict):
        return {}
    section = data.get(spec.section) if spec.section else data
    return section if isinstance(section, dict) else {}


def declared_sha256(kind: str, manifest: dict[str, Any]) -> str:
    """The hash a manifest DECLARES — what a copy on the server is compared by."""
    value = manifest.get(KINDS[kind].hash_field)
    return value if isinstance(value, str) else ""


def declared_size(kind: str, manifest: dict[str, Any]) -> int:
    """What a manifest says the folder weighs; ``0`` when it does not say."""
    value = manifest.get(KINDS[kind].size_field)
    return value if isinstance(value, int) and value >= 0 else 0


def read(kind: str, root: Path) -> Entry | None:
    """The entry *root* holds, or ``None`` when it is not one of this kind.

    The hash is computed from the folder, never read off the manifest: this is
    the local side.
    """
    spec = KINDS[kind]
    manifest = read_manifest(kind, root)
    item_id = manifest.get(spec.id_field)
    version = manifest.get(spec.version_field)
    if not isinstance(item_id, str) or not item_id or not isinstance(version, str) or not version:
        return None
    sha, size = tree_digest(Path(root), manifest_name=spec.manifest)
    return Entry(kind, item_id, version, sha, size, Path(root))


def stamped_manifest(kind: str, root: Path) -> bytes:
    """This folder's manifest with the hash and the size it DECLARES, as bytes.

    What publishing sends up, so the copy that lands on the server states what
    it holds. Returned rather than written: publishing must not rewrite a file
    in this PC's library, least of all when the server is about to refuse it.
    Neither field can change the answer — the digest and the size both leave the
    manifest out.
    """
    spec = KINDS[kind]
    data = json.loads((Path(root) / spec.manifest).read_text(encoding="utf-8"))
    sha, size = tree_digest(Path(root), manifest_name=spec.manifest)
    target = data.setdefault(spec.section, {}) if spec.section else data
    target[spec.hash_field] = sha
    target[spec.size_field] = size
    return (json.dumps(data, indent=2) + "\n").encode("utf-8")


def version_key(version: str) -> tuple:
    """Sort key for a version: dotted numeric segments compare as ints (2.0.0 >
    1.0.10 > 1.0.2), anything else as a string. The ``isascii`` guard is load-
    bearing — ``isdigit()`` accepts digits ``int()`` refuses (superscripts), and
    sorting a library must never raise on a folder somebody named oddly."""
    return tuple(
        (1, int(seg)) if seg.isascii() and seg.isdigit() else (0, seg)
        for seg in re.split(r"[.+\-]", version or "")
    )
