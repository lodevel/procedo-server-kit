"""The ``api`` storage's bytes: one blob per sha256, in a temp folder (data_format.md §3.3).

Laid out as §3.2 lays out the other storages, ``<root>/<project_id>/<sha[0:2]>/<sha256>``: a
blob belongs to the project it was sent to (§3.5 "it sent it before to this project"), a file
uploaded alone (§3.4b) included, since its call names its owner. A library archive sits under
``library``.
"""

from __future__ import annotations

import hashlib
import os
import secrets
from pathlib import Path

LIBRARY = "library"  # library archives belong to no project (§3.2)


class BlobStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, project_id: str, sha: str) -> Path:
        return self.root / project_id / sha[:2] / sha

    def check(self, project_id: str, sha: str, size: int | None = None) -> str | None:
        """``None`` when the blob is there whole; else ``not_found`` or ``damaged``."""
        _, status = self.read(project_id, sha, size)
        return status

    def read(
        self, project_id: str, sha: str, size: int | None = None
    ) -> tuple[bytes | None, str | None]:
        """The blob's bytes when its size and hash are right; else why not."""
        try:
            data = self.path(project_id, sha).read_bytes()
        except FileNotFoundError:
            return None, "not_found"
        if (size is not None and len(data) != size) or hashlib.sha256(data).hexdigest() != sha:
            return None, "damaged"
        return data, None

    def owner_holding(self, sha: str) -> str | None:
        """An owner holding these bytes whole, whoever it is (only a fault looks across owners)."""
        for found in sorted(self.root.glob(f"*/{sha[:2]}/{sha}")):
            if self.check(found.parent.parent.name, sha) is None:
                return found.parent.parent.name
        return None

    def put(self, project_id: str, sha: str, data: bytes) -> None:
        """Write ``<sha>.<writer>.part``, then rename it to ``<sha>`` (§3.2, Upload)."""
        target = self.path(project_id, sha)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f"{sha}.{secrets.token_hex(4)}.part")
        temporary.write_bytes(data)
        os.replace(temporary, target)

    def remove(self, project_id: str, sha: str) -> None:
        try:
            self.path(project_id, sha).unlink()
        except FileNotFoundError:
            pass
