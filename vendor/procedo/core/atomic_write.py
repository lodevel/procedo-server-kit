"""Replace a file's contents in one step: temp beside it, fsync, ``os.replace``.

``Path.write_text`` truncates the real file and writes into it, so a kill, a full
disk or a dropped SMB connection mid-write leaves a torn file where the last good
one was — and on a share the reader is another bench, not this process. Writing a
temp file in the *same directory*, flushing it to the device and then replacing
means every reader sees either the old bytes or the new bytes, never half of
either, and the replace is atomic on NTFS/SMB as well as POSIX.

This is where the config and settings writers get it from, and it survives the
Windows rename race documented on :func:`replace`. A temp a killed writer left is
removed by :func:`sweep_stale_temps`, which Bench and Studio run at start over the
app-data folders (``temp_root.start``).
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .paths import is_link
from .process_alive import pid_alive

log = logging.getLogger(__name__)

#: The rename is retried this many times, backing off 20 ms more each round
#: (0.9 s in total), before a Windows ``ACCESS_DENIED`` is believed.
_REPLACE_ATTEMPTS = 10
_REPLACE_BACKOFF_S = 0.02
#: The random fragment of a temp name: this many lowercase hex digits.
_FRAGMENT_LEN = 8
_FRAGMENT = re.compile(f"[0-9a-f]{{{_FRAGMENT_LEN}}}", re.ASCII)
#: No OS hands out a larger pid (and Windows' process probe cannot even take one).
_PID_MAX = 2**31 - 1


def _temp_name(name: str, pid: int, fragment: str) -> str:
    """The one spelling of a temp name for a file called *name*: ``.<name>.<pid>.<fragment>.tmp``."""
    return f".{name}.{pid}.{fragment}.tmp"


def _temp_owner(name: str) -> int | None:
    """The pid that wrote *name* as a temp, or None when :func:`_temp_name` would never
    have spelled *name* for any target."""
    parts = name.rsplit(".", 3)
    if len(parts) != 4 or not (parts[1].isascii() and parts[1].isdigit()):
        return None
    target, pid, fragment = parts[0][1:], int(parts[1]), parts[2]
    if not target or not 0 < pid <= _PID_MAX or not _FRAGMENT.fullmatch(fragment):
        return None
    if _temp_name(target, pid, fragment) != name:
        return None
    return pid


def sweep_stale_temps(folder: Path) -> None:
    """Remove the temps :func:`write_text` left in *folder* in processes that died
    between creating one and renaming it — a hard kill skips every cleanup.

    Only an exact temp name, only a plain file (never a folder, never a link), and
    only when its pid is not alive: a live process may be mid-write. Pids are unique
    per MACHINE, so this is for folders this machine alone writes; on a share a pid
    dead here may be another bench's write in flight. Best-effort: what cannot be
    listed or removed, or that any check fails on, stays for the next sweep.
    """
    if is_link(folder):  # read again here: the folder may have been swapped since it was listed
        return
    try:
        with os.scandir(folder) as listing:
            entries = list(listing)
    except OSError:
        return
    for entry in entries:
        pid = _temp_owner(entry.name)
        if pid is None:
            continue
        try:
            # The type is read again right before the unlink, not taken from the listing.
            if pid_alive(pid) or not stat.S_ISREG(os.lstat(entry.path).st_mode):
                continue
            os.unlink(entry.path)
        except OSError:
            pass  # gone already, or held: the next sweep tries again
        except Exception:  # one odd entry must never break the sweep of the others
            log.exception("stale temp sweep skipped %s", entry.path)


def replace(tmp: Path, path: Path) -> None:
    """``os.replace``, retried briefly through a Windows ``ACCESS_DENIED``.

    Public because writing the bytes and putting them in place are not always the
    same caller's job: the project sync streams a download into a temp file of its
    own and then needs exactly this, not a bare ``os.replace``.

    Renaming a file created microseconds ago races whatever opened it when it was
    closed — the malware scanner, the search indexer — because ``MoveFileEx``
    needs a handle nobody else holds: it fails with ``ERROR_ACCESS_DENIED``
    although nothing is wrong and the handle is gone milliseconds later. This is
    not theoretical: measured on this bench, an in-place ``write_text`` of the
    project config never failed in 15 runs and the temp+rename failed 3 times in
    15. The evidence store's writers (``evidence_store_fs_shared``) hit it too: the
    window's ``config/`` watch reads ``config.json`` as the Move's temp file appears
    beside it, and that reader's handle refused the switch's rename.

    A denial that outlives the whole window is a real one and propagates.
    """
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == _REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(_REPLACE_BACKOFF_S * (attempt + 1))


def write_text(path: Path, text: str, *, encoding: str = "utf-8", mode: int | None = None) -> None:
    """Make *path* hold *text*, atomically — with permission bits *mode* when given,
    set before the file is put in place.

    Newline translation is the platform default, exactly as ``Path.write_text``
    does it, so the bytes that land are the bytes that landed before. An
    ``OSError`` propagates with *path* still holding whatever it held.
    """
    _write_through_temp(path, text, replace, encoding=encoding, mode=mode)


def write_bytes(path: Path, data: bytes) -> None:
    """Make *path* hold *data*, atomically, byte for byte (its line endings as they are)."""
    _write_through_temp(path, data, replace)


def create_bytes(path: Path, data: bytes) -> None:
    """Make *path* hold *data* as a NEW file, all or nothing, never replacing one:
    ``FileExistsError`` when it exists. The temp is put in place with ``os.rename``,
    which refuses an existing target on Windows, so a file appearing meanwhile is
    not overwritten either (POSIX ``rename`` would replace it)."""
    if path.exists():
        raise FileExistsError(f"{path} already exists")
    _write_through_temp(path, data, os.rename)


def _write_through_temp(
    path: Path,
    payload: str | bytes,
    publish: Callable[[Path, Path], object],
    *,
    encoding: str = "utf-8",
    mode: int | None = None,
) -> None:
    """*payload* into a temp beside *path* (text in the platform's newlines, bytes
    verbatim), then ``publish(tmp, path)``; the temp is removed when that fails.

    The temp name carries a random fragment as well as the pid, because the
    directory it is created in may be a share: pids are unique per MACHINE, so
    two benches writing one project's config are perfectly likely to hold the
    same one, and ``"w"`` truncates. Both would then write into a single temp
    file at their own offsets and ``os.replace`` a NUL-spliced config into
    place, which ``project_open_problem`` reads as a project that cannot be
    opened at all.
    """
    tmp = path.with_name(_temp_name(path.name, os.getpid(), uuid.uuid4().hex[:_FRAGMENT_LEN]))
    try:
        if isinstance(payload, bytes):
            handle = open(tmp, "wb")
        else:
            handle = open(tmp, "w", encoding=encoding)
        with handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        publish(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass  # nothing to clean up, or the same failure again
        raise


def write_json(path: Path, data: Any) -> None:
    """:func:`write_text` of *data* in this project's config style."""
    write_text(path, json.dumps(data, indent=2, ensure_ascii=False))
