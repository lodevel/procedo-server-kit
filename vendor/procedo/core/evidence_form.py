"""The evidence moves' common form: a project's evidence as the folder layout's files.

The owner (2026-11-06): *"a project can be copied or moved from any storage to any storage"*, and
*"everyone to a common format, then that common format to everyone"* — a hub, never a converter
per pair. The common form is the evidence folder's layout (:mod:`evidence_shared_layout`) and the
objects ``data_format.md`` names: every storage reads its evidence INTO it and writes it FROM it
(:class:`EvidenceForm`), and the move (:mod:`evidence_transfer`) only ever talks to two forms. A
folder, a share, FTPS and SFTP are that layout already (:mod:`evidence_form_tree`); the database
builds it from its rows and lands it as rows (:mod:`evidence_db.form`). A storage added later is a
form and nothing per pair.

**A file of the form** is a relative path below the evidence root (:data:`Rel`), and the move
copies it file by file. **What a file holds** (:data:`Held`) is compared as the common form
compares it (:func:`held_of`):

* a **payload** — a run's manifest and record, its pictures and logs, a report, an export — byte
  for byte: its sha256 and size;
* a **document** — an entry (a run's comment or override, a board's lot or field), a current-run
  mark, a report's or an export's record, a draft — as the JSON object it is: two spellings of one
  object hold the same thing. A database keeps an entry as its columns and writes the file again
  from them, so the object is what both sides can be held to; a file that is not JSON is compared
  byte for byte.

**Units.** A storage that keeps items rather than files (the database) lands one item at once:
a run with its files and entries, a mark, a board's entries, a report or an export with its
generations, a draft (:func:`unit_of`). The move hands each storage one unit's files at a time
(:func:`by_unit`), and a folder writes them one by one. A file that is no item of the form (the
listing index, a part file) is a unit of its own (:data:`FILE`): a folder takes it,
the database answers that it cannot hold it, and the move leaves it where it is and says so —
except the listing index, a cache every store rebuilds from what it holds (:func:`is_derived`).

Qt-free, I/O-free but for :func:`held_of_file`.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, NamedTuple, Protocol

from .evidence_artifact_generations import GENERATIONS
from .evidence_board import PARTS, split_path
from .evidence_index import INDEX
from .evidence_owner import MARKER
from .evidence_shared_layout import CONVERSIONS, _is_conversion_lock, is_reserved
from .evidence_shared_manifest import BOARD_ENTRIES, ENTRIES, MANIFEST, POINTER
from .evidence_trash import RECORD, TOMBSTONE, TRASH
from .paths import sha256_file

Rel = tuple[str, ...]  # a file's path below a store root, component by component
Held = tuple[str, int]  # (digest, size) of what a file holds, as the form compares it

#: The kinds of unit (:func:`unit_of`).
RUN, MARK, BOARD, REPORT, EXPORT, DRAFT, DELETION, TOMB, FILE = (
    "run",
    "mark",
    "board_entries",
    "report",
    "export",
    "draft",
    "deletion",
    "tombstone",
    "file",
)
EXPORTS, REPORTS, RESULTS, RUNS = "exports", "reports", "results", "runs"
META = ".meta.json"
DRAFT_SUFFIX = ".draft.json"
REPORT_SUFFIX = ".html"
_NUMBERED = re.compile(r"\d{6}\.json")


class Unit(NamedTuple):
    """One item of the form: its kind and the path that names it (a run's folder, a mark's
    file, a board's ``entries`` folder, a report's or an export's newest payload, a draft's
    file, or the file itself)."""

    kind: str
    key: Rel


def travels(rel: Rel) -> bool:
    """Whether this file is evidence that moves with the project.

    Three things under the root are the FOLDER's and not the project's: the ownership marker
    (the destination writes its own), a move's own lock, and the folder's record of the moves
    that filled or emptied it. A record that travelled would describe passes into a folder it
    is no longer in."""
    return rel not in ((MARKER,), (CONVERSIONS,)) and not _is_conversion_lock(rel[0])


def _numbered(name: str) -> bool:
    return bool(_NUMBERED.fullmatch(name))


def _artifact(kind: str, folder: Rel, rest: Rel) -> Unit | None:
    """The report or export ``rest`` (below its folder) belongs to: its payload, its record,
    or either of an earlier generation's (``.generations/<name>/<n>/…``)."""
    record = _export_of_record if kind == EXPORT else _report_of_record
    if len(rest) == 1:
        name = rest[0]
        payload = record(name)
        if payload:
            return Unit(kind, (*folder, payload))
        if kind == EXPORT or name.endswith(REPORT_SUFFIX):
            return Unit(kind, (*folder, name))
        return None
    if len(rest) == 4 and rest[0] == GENERATIONS and rest[2].isdigit():
        name = rest[1]
        if rest[3] == name or record(rest[3]) == name:
            return Unit(kind, (*folder, name))
    return None


def _export_of_record(name: str) -> str:
    """The export whose record (``<name>.meta.json``) *name* is (``""``: it is no export's)."""
    return name[: -len(META)] if name.endswith(META) else ""


def _report_of_record(name: str) -> str:
    """The report whose record (``<stem>.json``) *name* is (``""``: it is no report's)."""
    if name.endswith(".json") and not name.endswith(DRAFT_SUFFIX):
        return name[: -len(".json")] + REPORT_SUFFIX
    return ""


def unit_of(rel: Rel) -> Unit:
    """The item of the form *rel* belongs to (:data:`FILE` when it is none)."""
    file = Unit(FILE, rel)
    first = rel[0]
    if first == TRASH and len(rel) >= 3:
        return Unit(DELETION, rel[:2])  # an entry of the evidence trash, its record and payload
    if first == EXPORTS:
        if len(rel) == 2 and rel[1].endswith(TOMBSTONE):
            return Unit(TOMB, rel)
        if rel[-1].endswith((".part", TOMBSTONE)):
            return file
        return _artifact(EXPORT, (EXPORTS,), rel[1:]) or file
    if first != PARTS and (is_reserved(first) or first.startswith(".")):
        return file
    board, rest = split_path(rel)
    folder = rel[: len(rel) - len(rest)]
    if len(rest) == 2 and rest[0] == BOARD_ENTRIES and _numbered(rest[1]):
        return Unit(BOARD, (*folder, BOARD_ENTRIES))
    if len(rest) == 3 and rest[0] == RESULTS and rest[2] == POINTER:
        return Unit(MARK, rel)
    if rel[-1].endswith(TOMBSTONE) and (
        (len(rest) == 4 and rest[0] == RESULTS and rest[2] == RUNS)
        or (len(rest) == 2 and rest[0] == REPORTS)
    ):
        return Unit(TOMB, rel)  # what became of a run, a report or a draft deleted there
    if len(rest) >= 5 and rest[0] == RESULTS and rest[2] == RUNS:
        return Unit(RUN, rel[: len(folder) + 4])
    if len(rest) >= 2 and rest[0] == REPORTS and board:
        if len(rest) == 2 and rest[1].endswith(DRAFT_SUFFIX):
            return Unit(DRAFT, rel)
        return _artifact(REPORT, (*folder, REPORTS), rest[1:]) or file
    return file


def is_document(rel: Rel) -> bool:
    """Whether *rel* is a JSON object of the form rather than a payload (the module's rule)."""
    unit = unit_of(rel)
    if unit.kind in (MARK, BOARD, DRAFT, TOMB):
        return True
    if unit.kind == DELETION:  # the entry's record, or a document where the item was
        return rel[2:] == (RECORD,) or is_document(rel[2:])
    if unit.kind == RUN:
        return len(rel) == len(unit.key) + 2 and rel[-2] == ENTRIES and _numbered(rel[-1])
    if unit.kind == REPORT:
        return bool(_report_of_record(rel[-1]))
    return unit.kind == EXPORT and rel[-1].endswith(META)


def _canonical(data: bytes) -> bytes | None:
    try:
        found = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return json.dumps(found, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def held_of(rel: Rel, data: bytes) -> Held:
    """What *data*, the file *rel*, holds as the form compares it (the module's rule)."""
    if is_document(rel):
        canonical = _canonical(data)
        if canonical is not None:
            return hashlib.sha256(canonical).hexdigest(), len(canonical)
    return hashlib.sha256(data).hexdigest(), len(data)


def held_of_file(rel: Rel, path: Path) -> Held:
    """:func:`held_of` of a local file; a payload is hashed in chunks, never read whole."""
    if is_document(rel):
        return held_of(rel, path.read_bytes())
    return sha256_file(path), path.stat().st_size


def by_unit(rels: Iterable[Rel]) -> list[tuple[Unit, list[Rel]]]:
    """*rels* grouped by unit, runs first (a mark names a run, which a storage of items may
    want to hold before the mark), then every other unit in the order its first file came, the
    deletions (the trash's entries) and last what became of each item deleted (its tombstone,
    which names its entry)."""
    found: dict[Unit, list[Rel]] = {}
    for rel in rels:
        found.setdefault(unit_of(rel), []).append(rel)
    order = {RUN: 0, TOMB: 3, DELETION: 2}
    return sorted(found.items(), key=lambda item: order.get(item[0].kind, 1))


def manifest_of(unit: Unit) -> Rel:
    """A run unit's manifest file."""
    return (*unit.key, MANIFEST)


#: What a form's ``put_unit`` answers per file: ``True`` copied, ``False`` already held
#: exactly, the reason the destination does not hold it (a string), or ``None``: a file the
#: destination rebuilds itself (:func:`is_derived`), neither carried nor missed.
Answer = bool | str | None
#: A file landed without replacing (``put_unit(replace=False)``) that the destination holds
#: otherwise: kept as it is there.
THERE_OTHERWISE = "already there with other content; kept as it is there"


def is_derived(rel: Rel) -> bool:
    """A file of the form that is no evidence but a cache of it: the listing index
    (:mod:`evidence_index`), which a store without a tree rebuilds from what it holds, and a
    folder of a source left behind may drop once the move is over."""
    return rel[0] == INDEX and len(rel) > 1


Fetch = Callable[[Rel], "Path | None"]


class EvidenceForm(Protocol):
    """One storage's side of the evidence hub: its evidence read into the common form and
    written from it. The move holds two of them and nothing else."""

    #: The store this form reads or writes (its outbox, machine id and tag are the move's).
    store: Any
    #: The store in the user's words (a folder's path, a server's address and folder).
    label: str
    #: The evidence folder on THIS PC when the store is one, else ``None``: only such a
    #: source is emptied after a move (a server's evidence is never deleted by a move).
    local_root: Path | None
    #: The pass this form lands in, as a destination (set by the move before :meth:`prepare`):
    #: its cancel, and what one pass reads once (a report drawn from its record).
    #: None outside a move.
    pass_ctx: Any

    def overlap(self, other: EvidenceForm) -> str | None:
        """Why this store and *other* are not two places (``None``: they are)."""

    def reachable(self) -> str | None:
        """Why the store cannot be read now (``None``: it can)."""

    def target_problem(self, project_id: str) -> str | None:
        """Why the project's evidence may not land here (``None``: it may)."""

    def links(self) -> list[Rel]:
        """The links and junctions under a local root (never followed, never moved)."""

    def walk(self) -> list[Rel]:
        """Every file of the form that travels, sorted."""

    def stamps(self, rels: list[Rel]) -> dict[Rel, Any]:
        """What each file is now, compared before and after the copy: a change is a writer."""

    def fetch(self, rel: Rel, staging: Path) -> Path | None:
        """The file as a local file (*staging* when it is not one already); ``None``: the
        storage lists it and holds no bytes for it (a lost attachment)."""

    def where(self, rel: Rel) -> str:
        """The file *rel* of this store, in the user's words."""

    def destination_problem(self, *, scan: bool) -> str | None:
        """Why the store cannot take evidence now, asked of a destination before the user is
        asked (``None``: it can)."""

    def held(self, rels: list[Rel], each: Callable[[], None] | None = None) -> dict[Rel, Held]:
        """What each file holds as the storage reads it BACK, in the form's terms; a file it
        does not list is absent from the answer. *each* is called before every file."""

    def held_file(self, rel: Rel) -> Held | None:
        """What one file holds now, asked at the call."""

    def prepare(self, project_id: str, source: str = "") -> None:
        """Make the destination ready for this project's evidence (a folder made and marked,
        a database's project row and its record of the import from *source*)."""

    def put_unit(
        self, unit: Unit, rels: list[Rel], fetch: Fetch, *, replace: bool = True
    ) -> dict[Rel, Answer]:
        """Land one unit, its files fetched from the source by *fetch*; idempotent: what the
        storage already holds exactly is not written again. ``replace=False`` (add missing)
        replaces nothing the storage holds otherwise, asked right before each write (under
        :meth:`writers_lock`, which the caller holds): such a file answers
        :data:`THERE_OTHERWISE`, or the storage's own refusal."""

    def writers_lock(self, unit: Unit, rels: list[Rel]) -> AbstractContextManager[bool]:
        """The lock a bench's writer of *unit* takes on this storage, held while add missing
        lands the unit and reads it back: ``True`` when one is held, ``False`` for a storage
        whose landing takes its own (a database's transaction, a web server's call)."""

    def move_lock(self, project_id: str, cancel: Any) -> AbstractContextManager[Any]:
        """The source's own lock for the whole pass: one move of this project's evidence out of
        this store at a time."""

    def record_switch(self, project_root: Path, destination: EvidenceForm) -> None:
        """Right before the switch: a source this pass will empty notes that its copy is still
        to be removed (nothing for a source that is never emptied)."""

    def finish(self, counts: dict[str, int]) -> None:
        """The pass is over (the destination's record of it, for a database)."""
