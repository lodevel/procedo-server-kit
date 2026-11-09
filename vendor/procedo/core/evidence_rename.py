"""Checked Windows directory relocation, with recovery across the config switch.

Only a missing destination and a positively owned shared filesystem root qualify.
The OS decides whether the directories share a filesystem; a drive letter does not.
The intent lives outside the evidence root so an interrupted rename is recoverable.
"""

from __future__ import annotations

import copy
import errno
import json
import os
import stat
import sys
import time
import uuid
from pathlib import Path

from . import atomic_write, evidence_config
from .evidence_board import split_path
from .evidence_owner import MARKER
from .evidence_store import EvidenceError, same_folder, within_folder
from .evidence_store_fs_shared import FilesystemStore
from .project_identity import project_config_lock
from .project_local_state import is_machine_state

JOURNAL = ".evidence-rename.json"
_PROJECT_NAMES = {"venv", "__pycache__", ".cache", ".git", "tests", "config", "bundle"}


def _error(message: str) -> EvidenceError:
    return EvidenceError("invalid", message)


def _exists(path: Path) -> bool:
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False


def _linked(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _identity(path: Path) -> list[int]:
    info = path.lstat()
    if _linked(info) or not stat.S_ISDIR(info.st_mode) or not info.st_ino or not info.st_dev:
        raise _error(f"Cannot prove the identity of the evidence folder {path}.")
    return [info.st_dev, info.st_ino]


def _real_chain(path: Path) -> None:
    for node in (path, *path.parents):
        info = node.lstat()
        if _linked(info) or not stat.S_ISDIR(info.st_mode):
            raise _error(f"{node} is not a real directory; evidence was not relocated.")


def _owner(path: Path) -> str | None:
    marker = path / MARKER
    try:
        if _linked(marker.lstat()):
            raise _error(f"The evidence ownership marker is a link: {marker}.")
        data = json.loads(marker.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (ValueError, UnicodeError) as exc:
        raise _error(f"Cannot read the evidence ownership marker {marker}.") from exc
    return data.get("project_id") if isinstance(data, dict) else None


def _raw(project: Path) -> tuple[dict, str]:
    data = evidence_config.read_config(project)
    if not data or not isinstance(data.get(evidence_config.SECTION), dict):
        raise _error(f"Cannot read the configured evidence folder for {project}.")
    project_id = data.get("project_id")
    if not isinstance(project_id, str) or not project_id:
        raise _error(f"{project} has no project identity.")
    return copy.deepcopy(data[evidence_config.SECTION]), project_id


def _spec_matches(spec: dict, root: Path) -> bool:
    params = spec.get("params")
    return bool(
        spec.get("backend") == "filesystem"
        and isinstance(params, dict)
        and params.get("root")
        and same_folder(Path(params["root"]), root)
    )


def _separate(project: Path, source: Path, destination: Path) -> None:
    for a, b in ((project, source), (project, destination), (source, destination)):
        if within_folder(a, b) or within_folder(b, a):
            raise _error(f"Cannot relocate overlapping folders: {a} and {b}.")


def eligible(project_root: Path, source, destination) -> bool:
    """Cheap shape/ownership eligibility, without enumerating evidence files."""
    if sys.platform != "win32":
        return False
    if not isinstance(source, FilesystemStore) or not isinstance(destination, FilesystemStore):
        return False
    src, dst = Path(source.root), Path(destination.root)
    if _exists(dst) or not src.is_dir() or not dst.parent.is_dir():
        return False
    live, project_id = _raw(Path(project_root))
    if not _spec_matches(live, src):
        raise _error(f"{src} is not the project's configured evidence folder.")
    owner = _owner(src)
    if owner is None:
        return False
    if owner != project_id:
        raise _error(f"{src} belongs to a different project.")
    if within_folder(src, Path(project_root)) or within_folder(dst, Path(project_root)):
        return False  # a folder inside the project (its default evidence) moves by copying
    _separate(Path(project_root), src, dst)
    _real_chain(src)
    _real_chain(dst.parent)
    _identity(src)
    return True


def _cancel(cancel) -> None:
    if cancel is not None and cancel.is_set():
        raise EvidenceError("cancelled", "The evidence move was cancelled before relocation.")


def _scan(root: Path, own_lock: str, cancel) -> None:
    """Windows DirEntry caches metadata: no payload reads or per-file network stats."""
    todo = [root]
    while todo:
        _cancel(cancel)
        folder = todo.pop()
        with os.scandir(folder) as entries:
            children = list(entries)
        names = {entry.name.casefold() for entry in children}
        inside = split_path(folder.relative_to(root).parts)[1]  # below the board's folder
        if (
            len(inside) == 4
            and inside[0].casefold() == "results"
            and inside[2].casefold() == "runs"
        ):
            if "manifest.json" not in names:
                raise _error(
                    f"An unfinished test result is still in {folder}; finish it before moving evidence."
                )
        for entry in children:
            _cancel(cancel)
            name = entry.name.casefold()
            info = entry.stat(follow_symlinks=False)
            if _linked(info):
                raise _error(f"Evidence contains a link or junction: {entry.path}.")
            if folder == root and entry.name == own_lock:
                continue
            if folder == root and (name in _PROJECT_NAMES or is_machine_state(name)):
                raise _error(
                    f"{root} contains project files or caches ({entry.name}); choose the results folder."
                )
            # A held lock is a write in progress: a run's pointer, or a folder of entries,
            # annotations, reports or exports (``.lock``).
            if name in {".owner", ".pointer.lock", ".lock"} or (
                name.startswith(".conversion-") and name.endswith(".lock")
            ):
                raise _error(
                    f"Evidence may still be being written in {entry.path}; finish writing before moving."
                )
            if stat.S_ISDIR(info.st_mode):
                todo.append(Path(entry.path))


def _journal(project: Path) -> Path:
    return project / "config" / JOURNAL


def _prove(root: Path, record: dict) -> None:
    _real_chain(root)
    if _identity(root) != record["identity"] or _owner(root) != record["project_id"]:
        raise _error(f"The evidence folder changed identity or ownership: {root}.")


def _lock_record(root: Path, record: dict) -> dict:
    lock = root / record["lock_name"]
    if not _exists(lock):
        return {}
    if _identity(lock) != record["lock_identity"]:
        raise _error(f"The evidence move lock changed: {lock}.")
    owner = lock / "owner.json"
    if _linked(owner.lstat()):
        raise _error(f"The evidence move lock is a link: {owner}.")
    data = json.loads(owner.read_text(encoding="utf-8"))
    if data.get("token") != record["token"]:
        raise _error(f"Another operation owns the evidence move lock: {lock}.")
    return data


def _release(root: Path, record: dict, *, partial: bool = False) -> None:
    _prove(root, record)
    lock = root / record["lock_name"]
    if partial and _exists(lock) and not _exists(lock / "owner.json"):
        if _identity(lock) != record["lock_identity"]:
            raise _error(f"The evidence move lock changed: {lock}.")
        lock.rmdir()
        return
    if not _lock_record(root, record):
        return
    # Remove only our owner record and the empty lock directory, never arbitrary files.
    _prove(root, record)
    _lock_record(root, record)
    (lock / "owner.json").unlink()
    if _identity(lock) != record["lock_identity"]:
        raise _error(f"The evidence move lock changed: {lock}.")
    lock.rmdir()


def _clear(project: Path, record: dict) -> None:
    path = _journal(project)
    if _linked(path.lstat()) or json.loads(path.read_text(encoding="utf-8")) != record:
        raise _error(f"The evidence move record changed: {path}.")
    path.unlink()


def _location(record: dict) -> Path:
    src, dst = Path(record["source"]), Path(record["destination"])
    present = [path for path in (src, dst) if _exists(path)]
    if len(present) != 1:
        raise _error(f"Cannot recover the evidence move; inspect both folders: {src} and {dst}.")
    _prove(present[0], record)
    return present[0]


def _owned_location(record: dict) -> Path:
    """Cleanup may release our lock while preserving a new unrelated destination."""
    found = []
    for path in (Path(record["source"]), Path(record["destination"])):
        if not _exists(path):
            continue
        try:
            identity = _identity(path)
        except EvidenceError:
            continue
        if identity == record["identity"]:
            found.append(path)
    if len(found) != 1:
        raise _error(
            f"Cannot locate the original evidence identity at {record['source']} or {record['destination']}."
        )
    _prove(found[0], record)
    return found[0]


def _read_record(project: Path) -> dict:
    path = _journal(project)
    if _linked(path.lstat()):
        raise _error(f"The evidence move record is a link: {path}.")
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        required = {
            "source",
            "destination",
            "identity",
            "project_id",
            "project_identity",
            "old_spec",
            "new_spec",
            "lock_name",
            "lock_identity",
            "token",
        }
        if not isinstance(record, dict) or not required <= record.keys():
            raise ValueError("incomplete record")
        if _identity(project) != record["project_identity"]:
            raise _error(
                f"This evidence move belongs to another project folder; inspect {record['source']} and {record['destination']}."
            )
        if record["lock_name"] != f".conversion-{record['project_id'][:8]}.lock":
            raise ValueError("invalid lock")
        src, dst = Path(record["source"]), Path(record["destination"])
        if not src.is_absolute() or not dst.is_absolute():
            raise ValueError("relative folder")
        if not _spec_matches(record["old_spec"], src) or not _spec_matches(record["new_spec"], dst):
            raise ValueError("mismatched configuration")
        _separate(project, src, dst)
        return record
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise _error(
            f"Cannot read the evidence move record {path}; inspect it before moving evidence."
        ) from exc


def _recover_locked(project: Path) -> None:
    from .evidence_folder_access import rename_guard

    if not _exists(_journal(project)):
        return
    record = _read_record(project)
    live, project_id = _raw(project)
    if project_id != record["project_id"] or live not in (record["old_spec"], record["new_spec"]):
        raise _error(
            f"Evidence configuration changed during the interrupted move from {record['source']} to {record['destination']}."
        )
    root = _location(record)
    with rename_guard(root):
        _prove(root, record)
        _finish_recovery(project, record, root, live, project_id)


def _finish_recovery(project: Path, record: dict, root: Path, live: dict, project_id: str) -> None:
    expected = record["old_spec"] if root == Path(record["source"]) else record["new_spec"]
    if live != expected:
        try:
            evidence_config.write_spec(project, expected)
        except Exception:
            if _raw(project) != (expected, project_id):
                raise
    if _raw(project) != (expected, project_id):
        raise _error(f"The recovered evidence configuration was not saved for {root}.")
    _release(root, record, partial=True)
    _clear(project, record)


def recover(project_root: Path) -> None:
    """Repair only a recorded interrupted relocation, before callers resolve a store."""
    project = Path(project_root)
    if not _exists(_journal(project)):
        return
    with project_config_lock(project, strict=True):
        _recover_locked(project)


def _before_rename(project: Path, record: dict, cancel) -> None:
    src, dst = Path(record["source"]), Path(record["destination"])
    _cancel(cancel)
    if _raw(project) != (record["old_spec"], record["project_id"]):
        raise _error(f"The configured evidence folder changed for {project}.")
    _separate(project, src, dst)
    _prove(src, record)
    _real_chain(dst.parent)
    if _identity(dst.parent) != record["parent_identity"] or _exists(dst):
        raise _error(f"The evidence destination changed or is occupied: {dst}.")
    if not _lock_record(src, record):
        raise _error(f"The evidence move lock disappeared from {src}.")


def _commit(project: Path, record: dict) -> None:
    src, dst = Path(record["source"]), Path(record["destination"])
    _prove(dst, record)
    if _raw(project) != (record["old_spec"], record["project_id"]):
        raise _error(f"Configuration changed; inspect evidence at {src} and {dst}.")
    try:
        evidence_config.write_spec(project, record["new_spec"])
    except Exception:
        live, project_id = _raw(project)
        if project_id == record["project_id"] and live == record["new_spec"]:
            return  # The atomic replacement committed before its writer raised.
        if project_id != record["project_id"] or live != record["old_spec"]:
            raise _error(f"Configuration changed; inspect evidence at {src} and {dst}.") from None
        _prove(dst, record)
        _real_chain(src.parent)
        if _exists(src) or _identity(src.parent) != record["source_parent_identity"]:
            raise _error(f"Cannot restore evidence to {src}; it remains at {dst}.") from None
        os.rename(dst, src)  # Windows refuses to overwrite an entry created meanwhile.
        _prove(src, record)
        raise
    if _raw(project) != (record["new_spec"], record["project_id"]):
        raise _error(f"The evidence configuration was not saved; inspect {src} and {dst}.")


def _perform(project: Path, source, destination, target_spec: dict, progress, cancel) -> bool:
    src, dst = Path(source.root), Path(destination.root)
    live, project_id = _raw(project)
    if not _spec_matches(live, src):
        raise _error(f"{src} is not the project's configured evidence folder.")
    if not _spec_matches(target_spec, dst):
        raise _error(f"The target configuration does not name {dst}.")
    record = {
        "source": str(src),
        "destination": str(dst),
        "identity": _identity(src),
        "project_id": project_id,
        "project_identity": _identity(project),
        "old_spec": live,
        "new_spec": copy.deepcopy(target_spec),
        "parent_identity": _identity(dst.parent),
        "source_parent_identity": _identity(src.parent),
        "lock_name": f".conversion-{project_id[:8]}.lock",
        "token": uuid.uuid4().hex,
    }
    lock = src / record["lock_name"]
    _cancel(cancel)
    _prove(src, record)
    try:
        lock.mkdir()
    except FileExistsError as exc:
        raise _error(f"Another evidence move holds {lock}; retry after it finishes.") from exc
    record["lock_identity"] = _identity(lock)
    journal_written = False
    try:
        atomic_write.write_json(
            lock / "owner.json",
            {"owner": source.tag, "token": record["token"], "acquired_at": time.time()},
        )
        atomic_write.write_json(_journal(project), record)
        journal_written = True
        if progress:
            progress(0, 0)
        _scan(src, record["lock_name"], cancel)
        _before_rename(project, record, cancel)
        try:
            os.rename(src, dst)
        except OSError as exc:
            # Unknown network outcomes require recovery, never a speculative copy.
            if exc.errno == errno.EXDEV or getattr(exc, "winerror", None) == 17:
                _prove(src, record)
                if not _exists(dst):
                    return False
            raise
        _commit(project, record)
        if progress:
            progress(1, 1)
        return True
    finally:
        # A hard interruption leaves the durable record. Cleanup never guesses which
        # directory moved, and never removes evidence or a different holder's lock.
        root = _owned_location(record)
        _release(root, record, partial=True)
        if journal_written and sys.exc_info()[0] not in (SystemExit, KeyboardInterrupt):
            current, _ = _raw(project)
            expected = record["old_spec"] if root == src else record["new_spec"]
            if current == expected:
                _clear(project, record)


def try_rename(
    project_root: Path, source, destination, target_spec: dict, *, progress=None, cancel=None
) -> bool:
    """True: folder and config moved. False: ineligible or definite EXDEV, unchanged."""
    from .evidence_folder_access import rename_guard

    project = Path(project_root)
    if not eligible(project, source, destination):
        return False
    with project_config_lock(project, strict=True):
        _recover_locked(project)
        if not eligible(project, source, destination):
            return False
        with rename_guard(Path(source.root)):
            return _perform(project, source, destination, target_spec, progress, cancel)
