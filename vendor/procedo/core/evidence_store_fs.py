"""Filesystem primitives every evidence backend shares: how an ``OSError`` reads as an
:class:`EvidenceError`, whether a root is remote, whether its volume answers, and the
artifact sidecar names. The filesystem store itself is
:class:`evidence_store_fs_shared.FilesystemStore`; the old in-project results layout
is read (never written) by :mod:`evidence_legacy`.
"""

from __future__ import annotations

import errno
import json
import os
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path, PureWindowsPath

from .evidence_store import (
    Absent,
    ArtifactInfo,
    AuthRequired,
    EvidenceError,
    EvidenceKey,
)

#: winbase.h DRIVE_UNKNOWN, DRIVE_NO_ROOT_DIR, DRIVE_REMOTE: a drive this process cannot
#: classify (an elevated process does not see mapped drives) reads as remote too.
_REMOTE_DRIVE_TYPES = frozenset({0, 1, 4})
# Dead share / no route: winerror (bad netpath, net name deleted, unexpected net
# error, sem timeout, network/host unreachable) or the POSIX equivalents.
_NETWORK_WINERRORS = frozenset({51, 53, 54, 55, 59, 64, 67, 121, 1203, 1231, 1232})
_NETWORK_ERRNOS = frozenset(
    {errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ENETDOWN, errno.EHOSTDOWN, errno.ETIMEDOUT}
)


def os_error(exc: OSError, key: EvidenceKey | None = None) -> EvidenceError:
    """Classify an OSError: dead share -> ``unreachable``; anything else -> ``denied``.

    The network check runs FIRST and keeps its meaning: a share whose ``ETIMEDOUT``
    the OS raises as a ``TimeoutError`` stays ``unreachable`` (the host is gone), and
    only a transport that timed out with no such errno — an FTPS socket budget —
    reports ``timeout``. The three subclass rules exist for a tree that has no errno
    to offer: :class:`evidence_store.AuthRequired` for rejected credentials,
    ``TimeoutError`` for a stalled transfer and ``ConnectionError`` for a refused or
    dropped connection.
    """
    if getattr(exc, "winerror", None) in _NETWORK_WINERRORS or exc.errno in _NETWORK_ERRNOS:
        return EvidenceError("unreachable", str(exc), key)
    if isinstance(exc, Absent):
        return EvidenceError("not_found", str(exc), key)
    if isinstance(exc, AuthRequired):
        return EvidenceError("auth", str(exc), key)
    if isinstance(exc, TimeoutError):
        return EvidenceError("timeout", str(exc), key)
    if isinstance(exc, ConnectionError):
        return EvidenceError("unreachable", str(exc), key)
    return EvidenceError("denied", str(exc), key)


# ---------------------------------------------------------------------------
# Root classification
# ---------------------------------------------------------------------------


def _drive_type(drive: str) -> int:
    import ctypes

    return ctypes.windll.kernel32.GetDriveTypeW(drive)  # type: ignore[attr-defined]


def is_remote_root(root: Path) -> bool:
    """UNC path, (Windows) mapped network drive or a drive Windows cannot classify ->
    remote; a fixed local disk -> local. Read threading only: every shared-layout write
    goes through the outbox whatever this answers."""
    text = str(root)
    if text.startswith(("\\\\", "//")):
        return True
    if sys.platform != "win32":
        return False
    drive = PureWindowsPath(text).drive
    return bool(drive) and _drive_type(drive + "\\") in _REMOTE_DRIVE_TYPES


def require_volume(path: Path) -> None:
    """Raise ``ConnectionError`` when the volume holding ``path`` does not answer.

    ``Path.is_dir``/``exists`` answer False for a dead share or a disconnected mapped
    drive, so a folder that is not there reads as EMPTY only once its volume (the
    share or the drive) answers; otherwise the read is ``unreachable`` (:func:`os_error`).
    """
    anchor = path.anchor or str(path)
    try:
        os.stat(anchor)
    except OSError as exc:
        raise ConnectionError(f"{anchor} does not answer: {exc}") from exc


# ---------------------------------------------------------------------------
# Artifact sidecars and small file helpers
# ---------------------------------------------------------------------------


def _sidecars(kind: str, payload: Path) -> tuple[Path, ...]:
    """Files committed beside an artifact payload — the first one is THE sidecar
    (report: ``<stem>.json`` + the per-report draft; export: ``<name>.meta.json``;
    a board's report draft has none)."""
    if kind == "report":
        return payload.with_suffix(".json"), payload.with_name(f"{payload.stem}.draft.json")
    if kind == "draft":
        return ()
    return (payload.with_name(f"{payload.name}.meta.json"),)


def default_sidecar(kind: str, info: ArtifactInfo) -> bytes | None:
    """The sidecar synthesized from ``info`` when the writer hands none over —
    reports only: an export's sidecar is the template's metadata (written when
    the export has any), a draft has none."""
    if kind != "report":
        return None
    return json.dumps({"metadata": asdict(info)}, indent=2).encode("utf-8")


def _format_mtime(seconds: float) -> str:
    """How both layouts spell a file's time in a listing row."""
    return datetime.fromtimestamp(seconds).strftime("%Y-%m-%d %H:%M:%S")


def _read_json(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}
