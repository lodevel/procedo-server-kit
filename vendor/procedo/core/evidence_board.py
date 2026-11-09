"""A board is its part number, its revision and its serial (docs/plans/board_identity.md).

Two boards may share a serial: a serial means something only under its part number and
revision. This module is the one place that knows how a board becomes a folder, and how a
folder becomes a board again.

**The board key.** The evidence code carries a board as one string, its *key*, wherever it
used to carry the serial. That covers the store's calls, the outbox entries, the index
records, the trash records and the caches.

- **The "no part number" board** (neither a part number nor a revision) keys as its serial
  and lives in ``<root>/<serial>/``. That is the layout of every result recorded before the
  board identity, which therefore reads as that board with nothing converted (decision 3). A
  project that makes both fields optional writes there too.
- **Any other board** keys as ``parts/<part number>/<revision>/<serial>`` and lives in that
  folder under the evidence root. A missing revision is :data:`NONE`; a revision is never
  recorded without its part number (Bench refuses it at Run), so ``_none`` stands for an
  empty revision only.

- **The runs saved with no serial** (Bench's "No serial") key as :data:`NO_SERIAL`, one folder
  at the evidence root, whatever part number and revision they recorded. :func:`board_of`
  reads it as the serial ``_unsorted`` with no part number: the board the JSON API is told,
  its contract having no "no serial" (slice 5).

A serial that is a name the store keeps for itself at the evidence root (``parts``,
``exports``, ``index``, :data:`NO_SERIAL`…, ignoring case) has its first character escaped,
like :data:`NONE`, so any serial at all is a board of its own and the two forms never meet: a
serial's ``/`` is escaped, so the "no part number" board's key is one folder.

**Names in folders.** A part number, a revision and a serial may each hold any character,
``/`` included (``AX/2210``, a GS1 serial ``21:0042/7``). The characters a folder name cannot
hold, and ``%`` itself, are written ``%XX`` (:func:`escape`), so the folder reads back as the
same name (:func:`unescape`): store, then read back, gives what went in. A person always reads
the names as typed (:func:`label_of`), never escaped.

Qt-free, stdlib only.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import PurePath

from .paths import is_windows_reserved

PARTS = "parts"  # the reserved folder at the evidence root that holds every keyed board
NONE = "_none"  # a part the board has none of, in its folder name
SEPARATOR = "/"  # between the key's segments, never inside one (they are escaped)
#: The key of the runs saved with no serial: a name the store keeps for itself, so no typed
#: serial is ever filed there (``_unsorted`` typed is ``%5Funsorted``).
NO_SERIAL = "_unsorted"
NO_SERIAL_LABEL = "No serial"  # how a person reads it: Bench's choice, the Report Browser's card

#: Written ``%XX``: what no folder name on a supported OS may hold, the separator, the
#: escape character itself, and the control characters.
_ESCAPED = re.compile(r'[%<>:"/\\|?*\x00-\x1f\x7f]')
_HEX = re.compile(r"%([0-9A-Fa-f]{2})")


@dataclass(frozen=True)
class Board:
    """A board: ``""`` is a part it has none of (the API's "absent", data_format.md §2)."""

    part_number: str
    revision: str
    serial: str

    @property
    def unnumbered(self) -> bool:
        """The "no part number" board: neither a part number nor a revision."""
        return not self.part_number and not self.revision

    def key(self) -> str:
        """The board's key (the module's docstring)."""
        return key_of(self.part_number, self.revision, self.serial)

    def label(self, *, part_number: bool = True) -> str:
        """``AX-2210 rev B · SN0042``; the bare serial for the "no part number" board. Without
        *part_number*: ``rev B · SN0042``, under a heading that names the part number."""
        revision = f"rev {self.revision}" if self.revision else ""
        board = " ".join(x for x in (self.part_number if part_number else "", revision) if x)
        return f"{board} · {self.serial}" if board else self.serial


def _clean(value: str | None) -> str:
    """A name as stored: NFC, surrounding blanks dropped (a scanned code keeps none)."""
    return unicodedata.normalize("NFC", (value or "").strip())


def key_of(part_number: str | None, revision: str | None, serial: str) -> str:
    """The key of the board with this part number, revision and serial (``""`` or ``None``:
    none), each name cleaned (NFC, blanks around it dropped) and escaped (:func:`escape`). An
    empty serial names no board: ``""``."""
    pn, rev, sn = _clean(part_number), _clean(revision), _clean(serial)
    if not sn:
        return ""
    if not pn and not rev:
        return _serial_folder(sn)
    return SEPARATOR.join(
        (PARTS, escape(pn) if pn else NONE, escape(rev) if rev else NONE, _serial_folder(sn))
    )


def _serial_folder(serial: str) -> str:
    """A serial's folder name: :func:`escape`, and the first character escaped as well when it
    is a name the store keeps for itself at the evidence root, ignoring case (``INDEX`` is
    ``%49NDEX``), so a serial never names the store's own folders nor :data:`NO_SERIAL`."""
    # Asked here, not imported at the top: the store's layout imports this module.
    from .evidence_shared_layout import would_collide

    out = escape(serial)
    if would_collide(out) or out.casefold() == NO_SERIAL.casefold():
        out = f"%{ord(out[0]):02X}{out[1:]}"
    return out


def board_of(key: str) -> Board:
    """The board a key names, its names as typed. A key that is not ``parts/…/…/…`` is the
    "no part number" board's, one folder: its serial, escaped."""
    parts = key.split(SEPARATOR)
    if len(parts) != 4 or parts[0] != PARTS:
        return Board("", "", unescape(key))
    _, pn, rev, serial = parts
    return Board(
        "" if pn == NONE else unescape(pn),
        "" if rev == NONE else unescape(rev),
        unescape(serial),
    )


def is_keyed(key: str) -> bool:
    """The key names a board with a part number or a revision (``parts/…``)."""
    return key.startswith(PARTS + SEPARATOR)


def segments(key: str) -> tuple[str, ...]:
    """The folders from the evidence root to the board's folder, in order: ``(serial,)`` for
    the "no part number" board, ``("parts", pn, rev, serial)`` otherwise, each a single folder
    name (each name escaped)."""
    parts = key.split(SEPARATOR)
    if len(parts) == 4 and parts[0] == PARTS:
        return tuple(parts)
    return (key,)


def label_of(key: str, *, part_number: bool = True) -> str:
    """:meth:`Board.label` of a key: what a person reads where the key used to be a serial;
    :data:`NO_SERIAL_LABEL` for the runs saved with no serial."""
    if key == NO_SERIAL:
        return NO_SERIAL_LABEL
    return board_of(key).label(part_number=part_number)


def is_canonical(key: str) -> bool:
    """The key is :data:`NO_SERIAL` or the one :func:`key_of` gives its board: what a NEW
    board may be written under. ``parts/_none/_none/SN1`` (that board's key is ``SN1``), an
    unescaped or differently escaped name (``SN:1``), or blanks around one, name no board."""
    return key == NO_SERIAL or board_of(key).key() == key


def key_of_path(root: PurePath, folder: PurePath) -> str:
    """The key of the board whose folder is *folder* below the evidence root *root*: its
    folders' names joined, in the spelling they have there."""
    return SEPARATOR.join(folder.relative_to(root).parts)


def split_path(parts: Sequence[str]) -> tuple[str, tuple[str, ...]]:
    """A path below the evidence root, folder by folder, as ``(the key of the board it is
    in, the rest of it inside the board's folder)``: ``parts/<pn>/<rev>/<serial>/…`` is a
    keyed board's, anything else starts with a bare serial."""
    depth = 4 if len(parts) >= 4 and parts[0] == PARTS else 1
    return SEPARATOR.join(parts[:depth]), tuple(parts[depth:])


def escape(name: str) -> str:
    """*name* as one folder name that reads back as itself (:func:`unescape`).

    Beyond the characters :data:`_ESCAPED` covers:
    - a trailing dot or space (Windows drops it), and ``.`` / ``..``, get their last character
      escaped;
    - a Windows device name (``CON``, ``LPT1``…) gets its first character escaped;
    - the literal :data:`NONE` gets its first character escaped, so it is never read as "none".
    """
    out = _ESCAPED.sub(lambda m: f"%{ord(m.group()):02X}", name)
    if out in (".", "..") or out[-1:] in (".", " "):
        out = f"{out[:-1]}%{ord(out[-1]):02X}"
    if out == NONE or is_windows_reserved(out, strict=True):
        out = f"%{ord(out[0]):02X}{out[1:]}"
    return out


def unescape(name: str) -> str:
    """The name :func:`escape` wrote."""
    return _HEX.sub(lambda m: chr(int(m.group(1), 16)), name)
