"""Which files of a run are not where its manifest says — one answer for every backend
(``docs/plans/evidence_db_design.md`` §"Missing attachments").

A missing picture never hides its run: the run is listed, reported and exported, and what it
cannot show is named with why:

* ``not_found`` — the store proved the file is not there (a server's ``Absent``; a folder
  without it);
* ``unreachable`` — the store did not answer for it. A server store serves the run without
  it and fetches it again on the next read: it is never cached as missing;
* ``damaged`` — bytes other than the ones the manifest records (their size; on a server
  store their sha256 too). They are never served.

A server store's materialization records why in ``.missing.json`` beside the cached
generation (:mod:`evidence_remote_cache`), and looks again for a ``not_found`` or ``damaged``
file once per session (a file put back on the storage shows again); a folder store's run
folder has no such record and is checked against its manifest alone, on every read.
:func:`missing_files` reads both the same way.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from .evidence_shared_manifest import MANIFEST
from .evidence_store import EvidenceError
from .evidence_store_fs import _read_json
from .evidence_tree import safe_relative

#: Why each file of a cached generation is not in it, written by its fetch.
MISSING = ".missing.json"
NOT_FOUND = "not_found"
UNREACHABLE = "unreachable"
DAMAGED = "damaged"


#: What a report and a history row say of each reason, as the owner approved them: the same
#: words on every storage.
_WHY = {
    NOT_FOUND: "not found",
    DAMAGED: "damaged, not shown",
    UNREACHABLE: "storage not reachable, will retry",
}


def missing_text(name: str, why: str) -> str:
    """What a report shows in place of a run's file it cannot show:
    ``File missing: scope_1.png (not found)``."""
    return f"File missing: {name} ({_WHY.get(why, why)})"


def list_text(missing: Mapping[str, str]) -> str:
    """Each missing file with why, one per line, as the hover over a history row's
    "N files missing" lists them: ``scope_1.png — not found``; ``""`` for none."""
    return "\n".join(f"{name} — {_WHY.get(why, why)}" for name, why in missing.items())


def count_text(missing: int) -> str:
    """What a history row says of a run's files not there: ``2 files missing``; ``""`` for none."""
    if not missing:
        return ""
    return f"{missing} file{'s' * (missing != 1)} missing"


def missing_files(run_dir: Path, manifest: dict | None = None) -> dict[str, str]:
    """``{name: why}`` of each file the manifest in ``run_dir`` lists that is not there to read,
    in manifest order; ``{}`` for a run folder without a manifest. Why is what the fetch
    recorded, else ``not_found`` for an absent file, ``unreachable`` for one the disk did not
    answer for and ``damaged`` for one of another size. A file placed since the record was
    written (a retried fetch) is not missing. *manifest*: the run's, read already (a history's
    listing), so it is not opened again."""
    recorded = _read_json(run_dir / MISSING)
    if manifest is None:
        manifest = _read_json(run_dir / MANIFEST)
    found: dict[str, str] = {}
    for entry in manifest.get("files") or []:
        name = str(entry.get("name", ""))
        try:
            size = run_dir.joinpath(*safe_relative(name, "posix")).stat().st_size
        except EvidenceError:
            found[name] = DAMAGED  # a name no file can be read under
        except FileNotFoundError:
            found[name] = recorded.get(name, NOT_FOUND)
        except OSError:
            found[name] = UNREACHABLE
        else:
            if size != entry.get("size"):
                found[name] = DAMAGED
    return found
