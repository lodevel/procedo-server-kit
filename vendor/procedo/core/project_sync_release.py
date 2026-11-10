"""A release made in a working copy of a project on a server (the web server, FTPS).

The server numbers it: the copy sends the test's documents as they are here, by their versions
(:class:`project_store.ReleaseRequest`, ``ProjectSource.release``, ``P10``), and the server keeps
them as its release when its own files are those versions. The copy then writes the record the
server answered, with the same documents, into its ``releases/<test_id>/``
(:func:`project_tree_releases.write`), the bytes the server's tree holds or the next read brings
(the sync takes a file with the same bytes on both sides as no conflict).

Refused (``test_release.Refused``, the message for the user):

* the test's documents or its ``test.json`` changed here since the last sync, or wait on a
  conflict, or the last read did not finish: :data:`test_release.NOT_SYNCED`;
* a document changed on the server since (``changed``), or no test there holds the id any more
  (``not_found``): :data:`test_release.CHANGED_ON_SERVER` (the owner's sentence, 2026-11-06);
* the checks :func:`test_release.releasable` makes (no id, nothing runs, the files are a
  release), and a content the server holds a release of already (``same_as_release``).

Held under the working copy's sync lock: no pass moves its files meanwhile. It waits on the
server: never on the GUI thread. Qt-free.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path

from . import app_settings, project_sync, project_tree_releases, test_release
from .evidence_store import EvidenceError, OpContext
from .evidence_store_fs_shared import FsTree
from .project_origin import Origin
from .project_store import ProjectSource, ReleaseRequest, version_of
from .project_sync_scan import local_now
from .test_format import TEST_JSON

log = logging.getLogger(__name__)


def release(
    working_copy: Path,
    source: ProjectSource,
    test: str,
    note: str = "",
    *,
    ctx: OpContext | None = None,
    released_by: str | None = None,
) -> int:
    """Release *test* of *working_copy* on its server *source* by *released_by* (``None``:
    ``test_release.user()``; a server that lists its users checks the name:
    ``unknown_operator``), then write the same release into the
    copy; the number the server gave it. Raises ``test_release.Refused``."""
    working_copy = Path(working_copy)
    project_sync._require_project_folder(working_copy)
    with project_sync._lock_for(working_copy):
        origin = project_sync._origin_of(working_copy)
        project_sync._require_same_project(working_copy, origin)
        ready = test_release.releasable(working_copy, test)
        named = {test_release.project_path(test, name): name for name in ready.docs}
        if not _synced(working_copy, origin, [*named, f"tests/{test}/{TEST_JSON}"]):
            raise test_release.Refused(test_release.NOT_SYNCED)
        request = ReleaseRequest(
            format=ready.fmt,
            fingerprint=ready.state.fingerprint,
            content=ready.state.content,
            files={path: version_of(ready.docs[name]) for path, name in named.items()},
            note=note.strip(),
            released_by=test_release.user() if released_by is None else released_by,
            pc=test_release.pc_name(),
            machine_id=app_settings.get_machine_id(),
        )
        try:
            made = source.release(origin.project_id, ready.test_id, request, ctx or OpContext())
        except EvidenceError as exc:
            raise _refused(exc) from exc
        record = replace(made, documents=ready.docs)
        try:
            project_tree_releases.write(
                FsTree(working_copy), working_copy, {ready.test_id: [record]}
            )
        except (OSError, EvidenceError) as exc:
            log.warning(
                "v%s of %s is on the server, not in the working copy yet (the next sync "
                "brings it): %s",
                made.version,
                test,
                exc,
            )
        return made.version


def _synced(working_copy: Path, origin: Origin, paths: list[str]) -> bool:
    """Whether each of *paths* is here as the last sync left it: recorded, not changed since,
    not waiting on a conflict, and the last read finished."""
    if origin.needs_pull:
        return False
    for rel in paths:
        row = origin.files.get(rel)
        if row is None or rel in origin.conflicts or local_now(working_copy, rel) != row.local:
            return False
    return True


def _refused(exc: EvidenceError) -> test_release.Refused:
    """What the user is told of a release the server refused."""
    if exc.code == "changed" or exc.kind == "not_found":
        return test_release.Refused(test_release.CHANGED_ON_SERVER)
    if exc.code in ("same_as_release", "unknown_operator"):
        return test_release.Refused(str(exc))
    if exc.kind in ("unreachable", "timeout"):
        return test_release.Refused(test_release.SERVER_UNREACHABLE)
    return test_release.Refused(f"The server did not take the release: {exc}")
