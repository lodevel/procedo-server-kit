"""A project's sync with its server in the app's log, whatever the server (a folder, a share, FTPS,
SFTP, the web server, the database): each pass and each server operation of
:class:`project_sync_gateway.SyncGateway` (:func:`logged` wraps them; every one goes through it).

* A pass that moved something (or met a refusal for the first time) says what (``2 sent, 0
  received, 1 refused``) and how long it took; one with no news is a DEBUG line, so the poll
  does not fill the log.
* A path the server refused, or that did not come back, is a WARN once per session per path
  and reason.
* A failure is said once per outage (:class:`failure_log.Repeats`): an ERROR the first time,
  with the server's request id when there is one, then ``still failing`` now and then, then
  ``works again``. A cancel is no failure.

The project is named by its own name (its working copy's record), never the working copy's
folder. Only paths and counts are logged, never a file's content.
"""

from __future__ import annotations

import functools
import logging
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path, PurePath
from typing import Any

from . import failure_log, project_origin

log = logging.getLogger(__name__)

_repeats = failure_log.Repeats(log)
_said_lock = threading.Lock()
_said: set[tuple[str, str, str]] = set()  # (how, path, why) warned this session
#: A working copy's project name, by the copy: (its record, the record's mtime, the name).
_names: dict[str, tuple[Path, int | None, str]] = {}


def logged(kind: str, project_at: int = 0) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """The operation *kind* (``Pull``, ``Push``, ``Release``…) said in the log. The project is
    the argument at *project_at*: a working copy, or its place on the server (a checkout has
    no working copy yet; a publish's copy is still a local folder)."""

    def wrap(method: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(method)
        def call(self: Any, *args: Any, **kwargs: Any) -> Any:
            project = project_name(args[project_at]) if len(args) > project_at else ""
            subject = f"Sync of {project}"
            log.debug("%s started: %s", kind, project)
            started = time.monotonic()
            try:
                result = method(self, *args, **kwargs)
            except Exception as exc:
                if getattr(exc, "kind", "") == "cancelled":
                    log.info("%s of %s cancelled", kind, project)
                elif getattr(exc, "refusal", False):  # the user's to answer: no failure
                    log.warning("%s of %s refused: %s", kind, project, exc)
                else:
                    said = f"{kind} of {project} failed: {exc}{_request_of(exc)}"
                    _repeats.failed(subject, exc, said)
                raise
            _repeats.worked(subject)
            _done(kind, project, result, time.monotonic() - started)
            return result

        return call

    return wrap


def project_name(where: Any) -> str:
    """The project's own name: a working copy's from its record (its folder is named after the
    project and a hash), else the last part of *where*. A copy's name is read again only when
    its record changed (its modification time)."""
    path = Path(str(where)) if isinstance(where, (str, PurePath)) else None
    if path is not None and path.is_dir():
        name = _recorded_name(path)
        if name:
            return name
    return PurePath(str(where).replace("\\", "/")).name or str(where)


def _recorded_name(working_copy: Path) -> str:
    with _said_lock:
        kept = _names.get(str(working_copy))
    if kept is not None and _mtime(kept[0]) == kept[1]:
        return kept[2]
    record = project_origin.record_path(working_copy)
    if record is None:
        return ""
    stamp = _mtime(record)
    try:
        origin = project_origin.read_record(record)
    except (OSError, ValueError):
        origin = None
    name = (
        PurePath(origin.base.replace("\\", "/")).name or origin.base
        if origin and origin.base
        else ""
    )
    with _said_lock:
        _names[str(working_copy)] = (record, stamp, name)
    return name


def _mtime(path: Path) -> int | None:
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


def _request_of(error: BaseException) -> str:
    """`` · request <id>``: the server's request id the failure (or what caused it) carries."""
    seen: BaseException | None = error
    for _ in range(5):
        if seen is None:
            break
        found = getattr(seen, "request_id", "")
        if found:
            return f" · request {found}"
        seen = seen.__cause__ or seen.__context__
    return ""


def _warn_once(how: str, path: str, why: str) -> bool:
    """Warn of *path* (once per session per reason); whether it was said now."""
    with _said_lock:
        if (how, path, why) in _said:
            return False
        _said.add((how, path, why))
    log.warning("%s %s: %s", path, how, why)
    return True


def _done(kind: str, project: str, report: Any, seconds: float) -> None:
    """What the operation did; a warning for each path a pass refused or left out."""
    if not hasattr(report, "uploaded"):  # not a pass: a checkout, a release, a rename…
        log.info("%s of %s done in %.2f s", kind, project, seconds)
        return
    news = [_warn_once("not synced", path, why) for path, why in report.refused]
    news += [_warn_once("not brought back", path, why) for path, why in report.skipped]
    counts = [
        (len(report.uploaded), "sent"),
        (len(report.downloaded), "received"),
        (len(report.refused), "refused"),
        (len(report.deleted_local) + len(report.deleted_remote), "deleted"),
        (len(report.conflicts), "in conflict"),
        (len(report.pending_deletes), "deletes waiting for approval"),
    ]
    # What a pass did is news; a refusal said before, a conflict or a delete still waiting is
    # what every pass meets again until somebody acts: the line is then DEBUG.
    moved = any(count for count, _words in (counts[0], counts[1], counts[3])) or any(news)
    said = [f"{count} {words}" for count, words in counts[:3]]
    said += [f"{count} {words}" for count, words in counts[3:] if count]
    log.log(
        logging.INFO if moved else logging.DEBUG,
        "%s of %s done: %s, in %.2f s",
        kind,
        project,
        ", ".join(said),
        seconds,
    )
