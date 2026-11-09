"""A project's board archive (ODB++ ``.tgz``): which file it is, and its checksum.

Both are file reads on the project folder — on a share, slow — so the project open asks them
off the GUI thread and hands the answers to the media panel.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)
_warned_ambiguous: set[Path] = set()  # folders already warned about: find() runs on every refresh


def configured(project_dir: Path) -> str:
    """The archive ``config.json:odb_archive`` names inside the project, as a relative POSIX
    path, whether or not it is there yet (a working copy downloads it after the open);
    ``""`` when it names none, or one outside the project."""
    try:
        data = json.loads((project_dir / "config" / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    raw = data.get("odb_archive") if isinstance(data, dict) else None
    if not isinstance(raw, str) or not raw:
        return ""
    named = Path(raw)
    if not named.is_absolute():
        return named.as_posix()
    try:
        return named.relative_to(project_dir).as_posix()
    except ValueError:
        return ""


def find(project_dir: Path) -> Path | None:
    """The project's archive: the ``config.json:odb_archive`` override (relative to the
    project root, or absolute) when that file exists, else the first ``*.tgz`` in the
    project root; None when there is none."""
    try:
        data = json.loads((project_dir / "config" / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    raw = data.get("odb_archive") if isinstance(data, dict) else None
    if isinstance(raw, str) and raw:
        configured = Path(raw)
        if not configured.is_absolute():
            configured = project_dir / raw
        if configured.exists():
            return configured
    tgz_files = sorted(project_dir.glob("*.tgz"))
    if len(tgz_files) > 1 and project_dir not in _warned_ambiguous:
        _warned_ambiguous.add(project_dir)
        log.warning(
            "Ambiguous ODB archive: %d .tgz files in %s and no config.json odb_archive "
            "override — picking %s",
            len(tgz_files),
            project_dir,
            tgz_files[0].name,
        )
    return tgz_files[0] if tgz_files else None


def checksum(archive: Path | None) -> str:
    """MD5 of *archive*; "" when there is none. Reads the whole file."""
    if not archive or not archive.exists():
        return ""
    md5 = hashlib.md5()
    with open(archive, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            md5.update(chunk)
    return md5.hexdigest()
